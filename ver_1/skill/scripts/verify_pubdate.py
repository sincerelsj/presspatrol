#!/usr/bin/env python3
"""verify_pubdate — 발행연월일(= 최신 기사 업데이트 날짜)을 LLM 으로 확정한다.

왜 별도 단계인가
----------------
지침 R7 은 발행연월일을 세 갈래로 인정한다 — ① 발행연월일·최종편집일시 명칭 ② 명칭과
무관한 날짜형태(등록연월일·창간일 포함) ③ 기사 업로드 날짜. 화면의 날짜가 어디에 해당하는지,
'투데이' 형 현재날짜 표기인지는 문맥 판단이라 규칙만으로 확정할 수 없다.

지침 반영 (2026-08-14)
----------------------
종전에는 발행연월일을 '최신 기사 업데이트 날짜' 로만 인정하고 등록일·창간일이면 게재를
취소했다(`is_latest_article_date=false` → 폐기). **지침은 정반대다** — 날짜의 성격은
게재 여부를 좌우하지 않는다. 유일한 제외는 '투데이' 형 실시간 현재날짜 표기다.

또 기사 상세(클릭 후)에서 확인한 업로드 날짜도 **21조 준수 근거로 인정한다**(지침 R7-3).
종전에는 `발행연월일_실제` 에만 넣고 게재값은 비웠다.

두 값을 분리 기록한다
---------------------
    발행연월일        지침 R7 로 게재 인정된 날짜 (기사 상세 포함) → **신문법 21조 판정**
    발행연월일_실제    실제 최신 기사 날짜                        → **최신성·운영 판단**

메인에 없으면 LLM 이 **가장 지배적인(헤드라인) 기사** 를 골라 그 페이지에서 확인한다.
`판단근거` 에 무엇으로 인정했는지 남긴다 — 지침 R8 비고의 요구이자, 교차규칙이
'기사 업로드 날짜' 를 등록연월일로 전이시키지 않게 막는 열쇠다(지침 R10 비고).

산출
----
  W/work/{id}.pubdate.json   2단계 판정 + 선택한 기사 URL + usage
  W/overrides/{도메인}.json   발행연월일_확인경로 를 기록해 다음 회차에 바로 쓰게 한다
  W/audit/{id}.json          5c_발행일검증

사용
----
  python verify_pubdate.py --workdir W --기준일 2026-07-31           # 전건
  python verify_pubdate.py --workdir W --ids 30874,30877
  python verify_pubdate.py --workdir W --dry-run
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from md_core import audit, batch  # noqa: E402
from md_core.http import fetch_html_ex, normalize_url  # noqa: E402
from md_core.zones import extract_zones, soupify  # noqa: E402
from llm_extract import (MODEL, PRICE_IN, PRICE_OUT, cost_of,  # noqa: E402
                         is_placeholder, load_env_key)

TOOL_MAIN = "record_pubdate"
TOOL_ARTICLE = "record_article_date"
MAX_TOKENS = 2000
LINK_MAX = 25

ARTICLE_PAT = re.compile(
    r"articleView|/news/|idxno=|/article/|view\.html|/view/|read\.php|"
    r"/newsView|contents\.html|\.html\?.*no=|/\d{6,}", re.I)
NOT_ARTICLE = re.compile(
    r"login|member|join|privacy|agree|about|contact|sitemap|rss|search|"
    r"facebook|twitter|instagram|youtube|kakao|naver\.com|google", re.I)


SYSTEM_MAIN = """\
너는 한국 인터넷신문의 **발행연월일 게재 여부** 를 판정한다. 근거는 2026-08-13 지침 R7 이다.

## 발행연월일로 인정하는 것 (지침 R7)
다음 중 **하나라도** 메인화면에 있으면 게재다.
1. `발행연월일` · `최종편집일시` 명칭의 날짜
2. **명칭과 상관없이 날짜형태** 가 있음 — `등록연월일`, `창간일`, 그 밖에 이름이 무엇이든
3. **기사 업로드 날짜** — 기사목록의 각 기사 옆 날짜

★ 종전 기준은 '최신 기사 업데이트 날짜' 만 인정하고 등록일·창간일을 배제했다.
**지침은 그 반대다.** 하단에 창간일만 있어도 발행연월일은 게재된 것이다.
날짜의 성격을 이유로 게재를 부정하지 마라. 오래된 날짜도 마찬가지다.

## 유일한 제외 — '투데이' 형 현재날짜 표기
`투데이 2026.08.14`, `오늘 2026-08-14`, `TODAY 2026.08.14` 처럼 **지금 날짜를 실시간으로
알려주는 표기**만 제외한다. 지침이 명시적으로 뺀 유일한 경우다.
- 제호에 '투데이' 가 든 매체(뉴스투데이·머니투데이·스포츠투데이)의 기사 날짜는
  **정상적인 날짜다.** 제호와 시계 위젯을 혼동하지 마라.
- 판단 기준: 그 날짜가 오늘이고, '투데이/오늘/TODAY' 가 날짜에 **바로 붙어** 있는가.

## 판정할 것
1. `found_on_main` — 위 1~3 중 하나가 메인화면(상단·하단·기사목록)에 있나?
2. `date` — 그 날짜 (YYYY-MM-DD). 여러 개면 **가장 최근**. 없으면 빈 문자열
3. `evidence` — 그 날짜가 나온 **입력 텍스트를 그대로 복사**. 없으면 빈 문자열
4. `location` — 하단 / 상단 / 기사목록 / 없음
5. `date_kind` — 그 날짜를 무엇으로 보는가.
   `발행연월일` / `최종편집일시` / `등록연월일` / `창간일` / `기사 업로드 날짜` / `불명`
   (지침 R8 비고가 "어떤 날짜를 확인했는지 근거 필요" 라고 요구한다. 반드시 채워라.)
6. `is_today_widget` — 위의 '투데이' 형 현재날짜 표기인가? 맞으면 true
7. `pick_article_index` — 메인에 날짜가 없을 때만. [기사 링크 후보] 에서 **가장 지배적인
   기사**(헤드라인 — 목록 최상단, 제목이 가장 길고 구체적인 것)의 번호. 필요 없으면 -1
8. `reason` — 판단 근거를 한두 문장으로

★ 날짜를 추측하지 마라. 입력 텍스트에 있는 것만 쓴다."""

SYSTEM_ARTICLE = """\
너는 기사 상세 페이지에서 **그 기사의 작성·수정 날짜** 를 읽는다.

- `승인 2026.05.18 08:49`, `입력 2026-05-18`, `최종수정 …`, `게재일 …` 같은 표기를 찾아라
- 여러 개면 **가장 최근** 날짜를 고른다(수정일이 있으면 수정일)
- `date` 는 YYYY-MM-DD, `evidence` 는 입력 텍스트를 그대로 복사
- 찾지 못하면 date 를 빈 문자열로 두고 reason 에 이유를 적어라
- 추측 금지"""


def tool_main() -> dict:
    return {
        "name": TOOL_MAIN,
        "description": "메인화면 발행연월일 판정 결과를 제출한다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "found_on_main": {"type": "boolean"},
                "date": {"type": "string", "description": "YYYY-MM-DD 또는 빈 문자열"},
                "evidence": {"type": "string", "description": "입력 텍스트 그대로 복사"},
                "location": {"type": "string", "enum": ["하단", "상단", "기사목록", "없음"]},
                "date_kind": {"type": "string",
                              "enum": ["발행연월일", "최종편집일시", "등록연월일", "창간일",
                                       "기사 업로드 날짜", "불명"],
                              "description": "그 날짜를 무엇으로 보는가 (지침 R8 비고의 판단근거)"},
                "is_today_widget": {"type": "boolean",
                                    "description": "'투데이' 형 현재날짜 실시간 표기인가 "
                                                   "— 지침이 제외한 유일한 경우"},
                "pick_article_index": {"type": "integer",
                                       "description": "메인에 없을 때 가장 지배적인 기사 번호. 없으면 -1"},
                "reason": {"type": "string"},
            },
            "required": ["found_on_main", "date", "evidence", "location", "date_kind",
                         "is_today_widget", "pick_article_index", "reason"],
            "additionalProperties": False,
        },
    }


def tool_article() -> dict:
    return {
        "name": TOOL_ARTICLE,
        "description": "기사 상세의 작성·수정 날짜를 제출한다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {"type": "string"},
                "evidence": {"type": "string"},
                "kind": {"type": "string", "enum": ["작성일", "수정일", "게재일", "불명"]},
                "reason": {"type": "string"},
            },
            "required": ["date", "evidence", "kind", "reason"],
            "additionalProperties": False,
        },
    }


def _capture_info(workdir: Path, rid: str) -> dict:
    """capture_shots 가 audit 에 남긴 4_증빙캡처 상세를 읽는다."""
    for st in audit.load(workdir, rid).get("단계", []):
        if st.get("단계") == "4_증빙캡처":
            return st.get("상세") or {}
    return {}


def capture_article_cands(workdir: Path, rid: str) -> list[tuple[str, str]]:
    """캡처가 라이브 DOM 에서 고른 기사 후보 — (제목, URL) 목록.

    렌더HTML 을 정규식으로 다시 파싱하는 것보다 정확하다: 캡처는 글자 크기·굵기·화면
    위치를 보고 '지배적인 기사' 를 판단했고, 그 1순위 기사의 페이지가 이미 저장돼 있다.
    """
    cands = _capture_info(workdir, rid).get("기사후보") or []
    out = []
    for c in cands:
        u, t = (c.get("url") or "").strip(), (c.get("제목") or "").strip()
        if u:
            out.append((t or u, u))
    return out


def capture_article_url(workdir: Path, rid: str) -> str:
    """캡처가 실제로 방문해 저장한 기사 URL (최종 URL)."""
    return (_capture_info(workdir, rid).get("기사url") or "").strip()


def article_links(html: str, base_url: str) -> list[tuple[str, str]]:
    """(제목 텍스트, 절대 URL) — DOM 순서 유지. 헤드라인이 보통 앞에 온다."""
    out, seen = [], set()
    try:
        soup = soupify(html)
    except Exception:  # noqa: BLE001
        return out
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        if NOT_ARTICLE.search(href) or not ARTICLE_PAT.search(href):
            continue
        url = urljoin(base_url, href)
        if url in seen:
            continue
        title = " ".join((a.get_text(" ") or "").split())[:90]
        if len(title) < 6:          # 아이콘·더보기 링크 배제
            continue
        seen.add(url)
        out.append((title, url))
        if len(out) >= LINK_MAX:
            break
    return out


def norm_date(s: str) -> str:
    m = re.search(r"(20\d{2}|19\d{2})\D{1,3}(\d{1,2})\D{1,3}(\d{1,2})", s or "")
    if not m:
        return ""
    y, mo, d = (int(g) for g in m.groups())
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return ""
    return f"{y:04d}-{mo:02d}-{d:02d}"


def verbatim(ev: str, *hays: str) -> bool:
    ev = (ev or "").strip()
    if not ev:
        return False
    evn = " ".join(ev.split())
    return any(ev in h or evn in " ".join(h.split()) for h in hays if h)


def domain_of(url: str) -> str:
    host = urlparse(normalize_url(url or "")).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def call(client, system: str, tool: dict, content, name: str) -> tuple[dict, dict]:
    resp = client.messages.create(
        model=MODEL, max_tokens=MAX_TOKENS,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        tools=[tool], tool_choice={"type": "tool", "name": name},
        messages=[{"role": "user", "content": content}],
    )
    raw = next((b.input for b in resp.content
                if getattr(b, "type", "") == "tool_use" and b.name == name), {})
    u = resp.usage
    return raw, {"input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
                 "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
                 "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
                 "stop_reason": resp.stop_reason}


def prepare_main(W, d, base, *, allow_fetch: bool = False) -> dict:
    """메인 판정용 입력을 만든다 — 네트워크 방문 없이 저장본만 읽는다.

    배치 제출과 순차 호출이 **완전히 같은 입력**을 쓰도록 한 곳에 모았다.
    둘이 갈라지면 배치 결과와 재현 실행이 어긋나 감사 추적이 깨진다.
    """
    rid = str(d["언론사_id"])
    상단 = (d.get("상단_텍스트") or "")[:1500]
    하단 = (d.get("하단_텍스트") or "")[:1200]
    # 메인 전체 HTML 이 필요하다 — work/{id}.json 에는 상·하단 텍스트만 있고,
    # 기사목록의 날짜(발행연월일 정의의 핵심)와 기사 링크는 본문에 있다.
    # ★ 렌더 HTML(capture_shots)만 읽는다 — 네트워크 재방문을 하지 않는다.
    #
    # 예전에는 없으면 직접 fetch 했다. 그래서 pilot3 에서 7건이 같은 매체를 또 방문했다
    # (건당 2.1회 = 8천건 16,615회). 캡처를 판단 앞으로 옮겼으므로 이 단계에 오면
    # 렌더HTML 은 이미 있다. 없으면 캡처가 실패한 것이니 판정불가로 남기는 게 옳다 —
    # 여기서 몰래 fetch 하면 "무엇을 근거로 봤는가" 가 캡처 증빙과 어긋난다.
    전체, html, 본문출처 = "", "", ""
    shot = W / "shots" / f"{rid}_rendered.html"
    if shot.exists():
        html = shot.read_text(encoding="utf-8", errors="replace")
        본문출처 = "렌더HTML(capture_shots)"
    elif allow_fetch:
        주소 = d.get("final_url") or d.get("url") or ""
        if 주소:
            st, h, _furl, _ = fetch_html_ex(주소)
            if h:
                html, 본문출처 = h, f"직접 fetch (HTTP {st}) — --allow-fetch"
            else:
                본문출처 = f"fetch 실패 (HTTP {st})"
    else:
        본문출처 = "렌더HTML 없음 — capture_shots 를 먼저 실행해라 (재fetch 안 함)"
    if html:
        전체 = (extract_zones(html).get("전체") or "")[:3000]
    # 기사 링크: 캡처가 라이브 DOM 에서 고른 후보를 우선 쓴다. 렌더HTML 정규식보다
    # 정확하고(글자 크기·굵기·위치를 봤다), 이미 저장된 기사 페이지와 짝이 맞는다.
    links = capture_article_cands(W, rid)
    링크출처 = "캡처 선정(라이브 DOM)" if links else ""
    if not links and html:
        links = article_links(html, d.get("final_url") or d.get("url") or "")
        링크출처 = "렌더HTML 파싱"
    링크블록 = "\n".join(f"  [{n}] {t}  →  {u}" for n, (t, u) in enumerate(links))

    규칙 = (d.get("findings") or {}).get("발행연월일") or {}
    user = (
        f"언론사_id: {rid} / 제호: {d.get('제호_등록','')}\n"
        f"기준일(오늘): {base.isoformat()}\n"
        f"\n[상단]\n{상단 or '(없음)'}\n"
        f"\n[하단]\n{하단 or '(없음)'}\n"
        f"\n[메인 전체 텍스트 발췌]\n{전체 or '(없음)'}\n"
        f"\n[규칙 1차결과] 발행연월일: {규칙.get('값') or '(미검출)'} "
        f"({규칙.get('추출방법','')}, 확신도 {규칙.get('확신도','')})\n"
        f"  규칙 근거: {(규칙.get('근거') or '(없음)')[:120]}\n"
        f"\n[기사 링크 후보] (목록 순서 = 화면 배치 순서)\n{링크블록 or '  (없음)'}\n"
    )
    return {"rid": rid, "상단": 상단, "하단": 하단, "전체": 전체,
            "본문출처": 본문출처, "links": links, "링크출처": 링크출처,
            "규칙": 규칙, "user": user}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--ids", default="")
    # 윈도 명령줄 상한 32,767자. R1 대상 4,233건이면 약 25,400자라 여유가 없다.
    ap.add_argument("--ids-file", dest="ids_file", default="",
                    help="언론사_id 목록 파일 (쉼표·줄바꿈 구분)")
    ap.add_argument("--base-date", "--기준일", dest="base_date", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--allow-fetch", action="store_true",
                    help="캡처 산출물이 없을 때 직접 fetch 를 허용한다. 기본은 금지 — "
                         "같은 매체를 두 번 방문하지 않고, 증빙과 판단 근거를 일치시킨다")
    ap.add_argument("--no-article-fetch", action="store_true",
                    help="메인에 없어도 기사 상세를 열지 않는다")
    ap.add_argument("--limit", type=int, default=0)
    # 배치: 1차(메인 판정)만 배치로 던진다. 2차(기사 상세)는 1차의 pick_article_index 에
    # 의존해서 같은 배치에 못 담고, 실측상 건수도 적어 순차로 둔다(102건 중 0건).
    ap.add_argument("--batch-submit", action="store_true")
    ap.add_argument("--batch-collect", action="store_true")
    ap.add_argument("--poll", type=int, default=batch.POLL_SEC)
    args = ap.parse_args()

    base = (datetime.strptime(args.base_date, "%Y-%m-%d").date()
            if args.base_date else date.today())
    W = Path(args.workdir)
    _idsrc = args.ids
    if args.ids_file:
        _idsrc += "," + Path(args.ids_file).read_text(encoding="utf-8")
    ids = {s for s in re.split(r"[,\s]+", _idsrc) if s} or None

    대상 = []
    for p in sorted((W / "work").glob("*.json")):
        if p.name.endswith((".llm.json", ".diag.json", ".pubdate.json")):
            continue
        d = json.loads(p.read_text(encoding="utf-8"))
        rid = str(d.get("언론사_id") or p.stem)
        if ids and rid not in ids:
            continue
        if not (d.get("하단_텍스트") or d.get("상단_텍스트")):
            continue
        대상.append(d)
    if args.limit:
        대상 = 대상[: args.limit]
    if not 대상:
        print(json.dumps({"error": "대상 없음"}, ensure_ascii=False)); sys.exit(1)

    if args.dry_run:
        print(json.dumps({"모드": "dry-run", "대상": len(대상), "모델": MODEL,
                          "호출_추정": f"{len(대상)}~{len(대상)*2}회 (메인 + 필요시 기사 상세)",
                          "비용_추정_USD": round(len(대상) * 0.010, 4)},
                         ensure_ascii=False, indent=2)); return

    load_env_key()
    import anthropic
    client = anthropic.Anthropic()

    # ── 배치 제출 (1차 메인 판정) ────────────────────────────────────────────
    if args.batch_submit:
        from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
        from anthropic.types.messages.batch_create_params import Request
        reqs = []
        for d in 대상:
            ctx = prepare_main(W, d, base, allow_fetch=args.allow_fetch)
            reqs.append(Request(
                custom_id=batch.cid(ctx["rid"]),
                params=MessageCreateParamsNonStreaming(
                    model=MODEL, max_tokens=MAX_TOKENS,
                    system=[{"type": "text", "text": SYSTEM_MAIN,
                             "cache_control": {"type": "ephemeral"}}],
                    tools=[tool_main()],
                    tool_choice={"type": "tool", "name": TOOL_MAIN},
                    messages=[{"role": "user", "content": ctx["user"]}],
                )))
        meta = batch.submit(client, W, "pubdate", base.isoformat(), reqs,
                            note=f"모델={MODEL} 1차(메인) 전용")
        meta["다음"] = "python verify_pubdate.py --workdir W --기준일 D --batch-collect"
        print(json.dumps(meta, ensure_ascii=False, indent=2)); return

    prefetched: dict = {}
    배치실패: dict = {}
    if args.batch_collect:
        meta = batch.load_meta(W, "pubdate", base.isoformat())
        if not meta.get("batch_id"):
            print(json.dumps({"error": "배치 메타 없음 — --batch-submit 먼저"},
                             ensure_ascii=False)); sys.exit(1)
        st = batch.wait_ended(
            client, meta["batch_id"], poll=args.poll,
            on_tick=lambda w, s_, rc: print(
                f"  발행일 배치 대기 {w}s status={s_} 완료={getattr(rc, 'succeeded', '?')}",
                file=sys.stderr))
        if st != "ended":
            print(json.dumps({"error": f"배치 미완료 (status={st})"},
                             ensure_ascii=False)); sys.exit(3)
        prefetched, 배치실패 = batch.results(client, meta["batch_id"], TOOL_MAIN)
        대상 = [d for d in 대상 if str(d.get("언론사_id")) in prefetched]
        if not 대상:
            print(json.dumps({"error": "배치 결과에 대상 없음", "실패": 배치실패},
                             ensure_ascii=False)); sys.exit(1)

    결과, usages = [], []
    for i, d in enumerate(대상, 1):
        ctx = prepare_main(W, d, base, allow_fetch=args.allow_fetch)
        rid, 상단, 하단, 전체 = ctx["rid"], ctx["상단"], ctx["하단"], ctx["전체"]
        본문출처, links, 링크출처 = ctx["본문출처"], ctx["links"], ctx["링크출처"]
        규칙, user = ctx["규칙"], ctx["user"]
        try:
            if rid in prefetched:
                m, u1 = prefetched[rid]        # 배치 결과 — 재호출하지 않는다
            else:
                m, u1 = call(client, SYSTEM_MAIN, tool_main(), user, TOOL_MAIN)
        except Exception as exc:  # noqa: BLE001
            print(f"[{i}/{len(대상)}] {rid} 메인 판정 실패: {exc.__class__.__name__}: {exc}",
                  file=sys.stderr)
            audit.record(W, rid, "5c_발행일검증", "실패",
                         {"단계": "메인", "예외": f"{exc.__class__.__name__}: {exc}"[:240]},
                         제호=d.get("제호_등록"), 기준일=base.isoformat())
            continue
        usages.append(u1)

        게재일 = norm_date(m.get("date", ""))
        근거ok = verbatim(m.get("evidence", ""), 상단, 하단, 전체)
        # ★ 지침 R7 반영 (2026-08-14): 등록연월일·창간일도 발행연월일의 근거로 인정한다.
        #   종전에는 `is_latest_article_date=false` 면 게재를 취소했는데, 그건 지침이
        #   "명칭 상관없이 날짜형태가 있음 → 준수" 로 인정하는 바로 그 경우를 버리는 것이었다.
        #   이제는 **날짜의 성격과 무관하게 게재로 인정**하고, 무엇으로 인정했는지만 남긴다.
        #   유일한 제외는 '투데이' 형 현재날짜 표기다(`is_today_widget`).
        투데이 = bool(m.get("is_today_widget"))
        # `0000-00-00` 같은 자리표시 날짜는 게재가 아니다 (llm_extract 와 같은 방어)
        더미 = bool(게재일) and is_placeholder(게재일)
        메인게재 = (bool(m.get("found_on_main")) and bool(게재일) and 근거ok
                and not 투데이 and not 더미)
        폐기이유 = ""
        if m.get("found_on_main") and 게재일 and not 근거ok:
            폐기이유 = "근거가 입력 텍스트에 없음 — 게재 인정 안 함"
            메인게재 = False
        elif 더미:
            폐기이유 = "자리표시(더미) 날짜 — 라벨만 있고 내용이 없다"
            메인게재 = False
        elif 투데이:
            폐기이유 = ("'투데이' 형 현재날짜 표기 — 지침 R7-2 가 명시적으로 제외한 유일한 경우")
            메인게재 = False
        날짜성격 = (m.get("date_kind") or "").strip()

        실제일, 기사url, 기사근거, 기사종류 = (게재일 if 메인게재 else ""), "", "", ""
        기사출처 = ""
        u2 = None
        if not 메인게재 and not args.no_article_fetch:
            idx = m.get("pick_article_index", -1)
            if isinstance(idx, int) and 0 <= idx < len(links):
                기사url = links[idx][1]
                # ★ 캡처가 같은 세션에서 저장한 기사 페이지를 먼저 쓴다 — 방문 없음.
                #   모델이 고른 기사가 캡처가 고른 기사와 다르면 그때만(--allow-fetch) 받아온다.
                ahtml, furl = "", 기사url
                pa = W / "shots" / f"{rid}_article.html"
                캡처기사 = capture_article_url(W, rid)
                if pa.exists() and (idx == 0 or not 캡처기사 or 캡처기사 == 기사url):
                    ahtml = pa.read_text(encoding="utf-8", errors="replace")
                    furl, 기사출처 = (캡처기사 or 기사url), "저장된 기사페이지(capture_shots)"
                elif args.allow_fetch:
                    st, ahtml, furl, _ = fetch_html_ex(기사url)
                    기사출처 = f"직접 fetch (HTTP {st}) — --allow-fetch"
                else:
                    기사출처 = ("저장된 기사페이지가 모델 선택과 다름 — 재fetch 안 함"
                              if pa.exists() else "저장된 기사페이지 없음 — 재fetch 안 함")
                if ahtml:
                    atext = (extract_zones(ahtml).get("전체") or "")[:4000]
                    try:
                        a, u2 = call(client, SYSTEM_ARTICLE, tool_article(),
                                     f"기사 URL: {furl}\n기준일: {base.isoformat()}\n\n"
                                     f"[기사 페이지 텍스트]\n{atext}", TOOL_ARTICLE)
                        usages.append(u2)
                        실제일 = norm_date(a.get("date", ""))
                        기사근거 = (a.get("evidence") or "")[:140]
                        기사종류 = a.get("kind", "")
                        if 실제일 and not verbatim(a.get("evidence", ""), atext):
                            실제일, 기사근거 = "", "근거 검증 실패로 폐기"
                    except Exception as exc:  # noqa: BLE001
                        기사근거 = f"기사 판정 실패: {exc.__class__.__name__}"
                else:
                    # `st` 는 --allow-fetch 분기에서만 바인딩된다. 기본값으로 돌 때
                    # 저장된 기사페이지가 없으면 여기서 NameError 로 전수 실행이 통째로
                    # 죽는다(잠복 결함). 실제 사유는 위에서 `기사출처` 에 담겼다.
                    기사근거 = f"기사 페이지 본문 확보 실패 — {기사출처}"

        경과 = None
        if 실제일:
            y, mo, dd = map(int, 실제일.split("-"))
            경과 = (base - date(y, mo, dd)).days

        # ★ 지침 R7-3 반영 (2026-08-14) — 기사 상세의 업로드 날짜도 **21조 준수 근거다.**
        #
        #   > "3) 기사 업로드 날짜가 있음(기사 **클릭했을 때** 기재된 업로드 날짜) → 준수"
        #
        #   종전에는 "기사 상세에서 찾은 날짜는 메인 게재가 아니므로 21조 판정에 쓰지 않는다" 며
        #   `발행연월일_실제` 에만 넣고 게재값은 비웠다. 지침이 클릭 후 확인을 명시적으로
        #   인정하므로 게재값으로 승격한다. pilot3 30897(리얼팩트뉴스)이 이 경로로 미준수에서
        #   준수로 바뀐다.
        #
        #   `판단근거` 로 '기사 업로드 날짜' 임을 남기는 것이 중요하다 — 지침 R10 비고가
        #   이 근거만은 **등록연월일로 전이시키지 말라** 고 못박았고, fields.apply_cross_rules
        #   가 그 문자열을 보고 전이를 막는다.
        기사상세게재 = bool(실제일) and not 메인게재
        게재값 = 게재일 if 메인게재 else (실제일 if 기사상세게재 else "")
        판단근거 = (날짜성격 or "불명") if 메인게재 else (
            "기사 업로드 날짜" if 기사상세게재 else "")
        확인경로 = ("메인_" + (m.get("location") or "") if 메인게재
                else ("기사상세" if 실제일 else "확인실패"))
        doc = {
            "언론사_id": rid, "제호_등록": d.get("제호_등록", ""), "모델": MODEL,
            "기준일": base.isoformat(),
            "발행연월일_게재": 게재값,
            "게재_위치": (m.get("location") if 메인게재
                      else "기사상세" if 기사상세게재 else "없음"),
            "게재_근거": ((m.get("evidence") or "")[:160] if 메인게재
                      else (기사근거 or "")[:160] if 기사상세게재 else ""),
            "메인게재_여부": 메인게재, "기사상세_게재인정": 기사상세게재,
            "게재_불인정_이유": 폐기이유,
            "판단근거": 판단근거,
            "발행연월일_실제": 실제일, "경과일": 경과,
            "확인경로": 확인경로,
            "선택된_기사": {"index": m.get("pick_article_index"), "url": 기사url,
                        "제목": (links[m["pick_article_index"]][0]
                               if isinstance(m.get("pick_article_index"), int)
                               and 0 <= m["pick_article_index"] < len(links) else ""),
                        "날짜종류": 기사종류, "근거": 기사근거},
            "모델_판단": m.get("reason", ""),
            "규칙_1차결과": {"값": 규칙.get("값", ""), "추출방법": 규칙.get("추출방법", ""),
                        "확신도": 규칙.get("확신도", "")},
            "기사링크_후보수": len(links), "메인본문_출처": 본문출처,
            "기사링크_출처": 링크출처, "기사본문_출처": 기사출처,
            "usage": [u for u in (u1, u2) if u],
            "비용_USD": round(sum(cost_of(u) for u in (u1, u2) if u), 5),
        }
        (W / "work" / f"{rid}.pubdate.json").write_text(
            json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")

        # 다음 회차에 바로 볼 수 있게 override 에 확인 경로를 남긴다
        dom = domain_of(d.get("url"))
        if dom:
            op = W / "overrides" / f"{dom}.json"
            prev = {}
            if op.exists():
                try:
                    prev = json.loads(op.read_text(encoding="utf-8"))
                except Exception:  # noqa: BLE001
                    prev = {}
            prev["domain"] = dom
            prev["발행연월일_확인경로"] = {
                "경로": 확인경로,
                "메인게재": 메인게재,
                "게재_위치": doc["게재_위치"],
                "기사URL_예": 기사url,
                "확인일": base.isoformat(),
                "메모": ("메인 표기 없음 — 헤드라인 기사 상세에서 확인해야 한다"
                       if not 메인게재 else "메인에서 확인 가능"),
            }
            if 메인게재:
                fields = dict(prev.get("fields") or {})
                fields["발행연월일"] = {"값": 게재일, "근거": doc["게재_근거"],
                                  "위치": ("상단" if m.get("location") == "상단"
                                        else "하단" if m.get("location") == "하단" else "기타")}
                prev["fields"] = fields
            op.parent.mkdir(parents=True, exist_ok=True)
            op.write_text(json.dumps(prev, ensure_ascii=False, indent=2), encoding="utf-8")

        audit.record(W, rid, "5c_발행일검증", "성공" if (메인게재 or 실제일) else "확인실패", {
            "정의": "발행연월일 = 최신 기사 업데이트 날짜",
            "메인게재": 메인게재, "발행연월일_게재": doc["발행연월일_게재"],
            "게재_위치": doc["게재_위치"], "게재_불인정_이유": 폐기이유,
            "발행연월일_실제": 실제일, "경과일": 경과, "확인경로": 확인경로,
            "선택된_기사": doc["선택된_기사"], "규칙_1차결과": doc["규칙_1차결과"],
            "모델_판단": m.get("reason", ""), "호출수": len(doc["usage"]),
            "비용_USD": doc["비용_USD"],
            # ★ 이 문구는 **실제 판정과 일치해야 한다** (ISS-074).
            #   종전에는 조건 없이 "기사 상세 날짜는 21조 판정에 쓰지 않는다" 를 박아 뒀는데,
            #   2026-08-14 지침 R7-3 이 클릭 후 확인을 인정하면서 코드는 그 날짜를 게재값으로
            #   **승격**하도록 바뀌었다. 문구만 옛것으로 남아 127건에서 감사기록이 판정과
            #   정반대를 말했다 — 처분 근거로 낼 수 없는 상태였다.
            "판단": (
                "지침 R7-3 — 기사를 클릭했을 때 기재된 업로드 날짜를 발행연월일로 인정한다. "
                "이 값을 게재값으로 승격했다. 단 지침 R10 비고에 따라 등록연월일로는 전이시키지 않는다"
                if 기사상세게재 else
                f"메인화면에서 날짜를 확인했다 (성격: {날짜성격 or '불명'}, 위치: {확인경로})"
                if 메인게재 else
                "메인·기사 상세 어디에서도 날짜를 확인하지 못했다 — 발행연월일 미게재"
            ),
        }, 제호=d.get("제호_등록"), 기준일=base.isoformat())

        결과.append({"id": rid, "제호": d.get("제호_등록", ""),
                   "규칙": 규칙.get("값") or "(미검출)",
                   "게재": doc["발행연월일_게재"] or "(미게재)",
                   "실제": 실제일 or "-", "경과일": 경과,
                   "경로": 확인경로, "비용": doc["비용_USD"]})
        print(f"[{i}/{len(대상)}] {rid} {d.get('제호_등록',''):<10} "
              f"규칙 {규칙.get('값') or '-':<11} → 게재 {doc['발행연월일_게재'] or '미게재':<11} "
              f"실제 {실제일 or '-':<11} ({확인경로}) ${doc['비용_USD']}", file=sys.stderr)

    총 = sum(cost_of(u) for u in usages)
    print(json.dumps({
        "모델": MODEL, "대상": len(결과), "기준일": base.isoformat(),
        "정의": "발행연월일 = 최신 기사 업데이트 날짜",
        "메인게재": sum(1 for r in 결과 if r["게재"] != "(미게재)"),
        "기사상세로_확인": sum(1 for r in 결과 if r["경로"] == "기사상세"),
        "확인실패": sum(1 for r in 결과 if r["경로"] == "확인실패"),
        "총비용_USD": round(총, 5), "건당_평균_USD": round(총 / max(1, len(결과)), 5),
        "결과": 결과,
        "주의": ["기사 상세에서 확인한 날짜도 지침 R7-3 에 따라 발행연월일로 인정한다 "
               "(게재값으로 승격). 단 R10 비고에 따라 등록연월일로는 전이시키지 않는다",
               "경과일이 큰 매체는 운영 중단 의심 — 홈페이지 운영현황 점검과 대조하라"],
        "다음": "merge_llm.py → compare_registry.py → finalize.py → location_map.py",
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
