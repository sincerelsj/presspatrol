#!/usr/bin/env python3
"""지침 I-3 건의 화면 성격을 **API 로** 판독한다.

무엇을 하나
-----------
지침 I 로직 3 은 "접속은 되는데 등록 매체명과 다르고 뉴스도 없다 → 홈페이지 없음 →
포털검색" 이다. 그런데 그 화면이 실제로 무엇인지는 갈린다 — 호스팅이 끊긴 것인지,
점검 중인지, 남이 도메인을 가져간 것인지. 그 구분이 다음 조치를 바꾼다:

    점검·준비 중  → 매체가 살아 있다. **재방문**이 답이지 포털검색이 아니다
    호스팅 만료   → 요금 미납. 다른 주소로 옮겼을 수 있다 → 포털검색
    무관 사업체   → 폐업 후 도메인 매각 가능성 → 포털검색
    다른 언론사   → 제호를 바꿨거나 다른 매체가 들어왔다 → 포털검색 값어치 최상

규칙 판독(`md_core/i3_reason.py`)과 **같은 성격 이름**을 쓴다. 두 결과를 대조하면
규칙이 어디서 틀리는지 보이고, 규칙만으로 충분한 구간은 API 를 안 써도 된다.

무엇을 넣나
-----------
**상단·하단 텍스트만** 넣는다. 스크린샷을 넣지 않는 이유는 세 가지다 — 이 판단에는
글자면 충분하고, 이미지는 건당 비용을 몇 배로 올리며, 657건이면 배치 256MB 상한에 걸린다.

산출
----
  W/work/{id}.i3.json   성격·설명·근거·검색값어치 + usage·비용
  W/audit/{id}.json     2c_I3성격 단계

사용
----
  python classify_i3.py --workdir W --기준일 D --dry-run          # 비용만 추정
  python classify_i3.py --workdir W --기준일 D --limit 30 --sync  # 소량 실측
  python classify_i3.py --workdir W --기준일 D --batch-submit     # 전건(50% 할인)
  python classify_i3.py --workdir W --기준일 D --batch-collect
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from md_core import audit, batch  # noqa: E402
from md_core.i3_reason import 값어치, 판별, 후속  # noqa: E402
from llm_extract import MODEL, cost_of, load_env_key  # noqa: E402

TOOL_NAME = "record_site_kind"
MAX_TOKENS = 700

# 넣는 글자 수 상한. I-3 화면은 대개 짧지만(657건 중 316건이 200자 미만) 도박 스팸이
# 채간 경우 2만 자를 넘는다. 성격 판별에는 앞부분이면 충분하고, 상한이 없으면 그런 건
# 하나가 전체 비용을 좌우한다.
HEAD_CAP = 1800
FOOT_CAP = 1200

# 규칙 판독과 **같은 이름** 을 쓴다 — 대조가 목적이다.
성격목록 = list(값어치.keys())

SYSTEM = """\
너는 한국 인터넷신문 실태점검에서 **등록된 주소에 뜬 화면이 무엇인지** 분류한다.

상황: 등록부에 A 매체의 홈페이지라고 적힌 주소로 접속했더니, 화면에 A 의 제호가 없고
뉴스·기사도 없었다. 그 화면이 실제로 무엇인지 가려야 다음 조치가 정해진다.

## 성격 (하나만 고른다)
- `도메인 판매·파킹` — 도메인 매물·주차 안내 화면(가비아·후이즈·DNSzi·sedo 등)
- `호스팅 만료·정지` — 호스팅 요금 미납·계약 만료 안내(카페24·아임웹 등의 만료 화면)
- `점검·준비 중` — 점검 중·공사 중·오픈 예정. **매체가 살아 있다는 신호다**
- `운영 중단 안내` — 매체가 스스로 중단·폐간을 알리는 화면
- `DNS·서버 응답 오류` — 502/503/504, DNS 오류, 서버를 찾을 수 없음
- `접근 차단` — 403 Forbidden, 접근 권한 없음
- `서버 기본페이지` — 웹서버 설치 직후 기본 화면(It works, Welcome to nginx, 아파치 기본)
- `빈 껍데기` — 내용이 사실상 없어 무엇인지 판단할 수 없다
- `도박·성인 스팸 사이트` — 만료 도메인을 채간 카지노·토토·슬롯·성인 사이트(외국어가 많다)
- `다른 언론사 제호` — 등록 제호와 **다른 언론사** 의 사이트다(제호 변경 또는 타 매체 입주)
- `뉴스와 무관한 사업체` — 실제로 운영 중인 회사·가게·단체 사이트지만 언론이 아니다

## 판단 요령
- 화면에 있는 것만 근거로 삼아라. 없는 것을 추측하지 마라.
- 짧다고 무조건 `빈 껍데기` 가 아니다. 회사 이름이 뚜렷하면 `뉴스와 무관한 사업체` 다.
- 외국어 도박·성인 문구가 보이면 길이와 무관하게 `도박·성인 스팸 사이트` 다.
- `점검·준비 중` 과 `운영 중단 안내` 를 혼동하지 마라 — 전자는 곧 돌아온다는 뜻,
  후자는 그만둔다는 뜻이다. 조치가 정반대다.
- `다른 언론사 제호` 는 화면에 **뉴스 매체로 보이는 이름**이 있을 때만 고른다.

## description 에 적을 것
검수자가 이 줄만 읽고 판단할 수 있게 **두 문장**으로 쓴다.
첫 문장은 화면이 무엇인지, 둘째 문장은 이 매체에 무슨 일이 있었을지에 대한 추정이다.
추정임이 드러나게 써라("~로 보인다"). 확실하지 않으면 확실하지 않다고 써라.

반드시 record_site_kind 도구를 호출해 제출해라."""


def build_tool() -> dict:
    # ★ property 키는 ASCII 만 쓴다 — 한글 키는 API 가 400 을 반환한다(CLAUDE.md).
    return {
        "name": TOOL_NAME,
        "description": "등록 주소에 뜬 화면의 성격을 제출한다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": 성격목록,
                         "description": "화면 성격 (성격 목록에서 하나)"},
                "description": {"type": "string",
                                "description": "검수자가 읽을 두 문장 설명. 화면이 무엇인지 + 무슨 일이 있었을지 추정"},
                "evidence": {"type": "string",
                             "description": "그렇게 판단한 근거 문구를 화면에서 **그대로** 옮긴다"},
                "screen_name": {"type": "string",
                                "description": "화면에 나타난 사이트·회사 이름. 없으면 빈 문자열"},
                "is_news_site": {"type": "boolean",
                                 "description": "이 화면이 언론사 사이트인가"},
                "search_worth": {"type": "string", "enum": ["높음", "보통", "낮음"],
                                 "description": "포털검색으로 진짜 홈페이지를 찾을 값어치"},
                "confidence": {"type": "string", "enum": ["high", "mid", "low"],
                               "description": "판단 확신도"},
            },
            "required": ["kind", "description", "evidence", "screen_name",
                         "is_news_site", "search_worth", "confidence"],
            "additionalProperties": False,
        },
    }


def 대상목록(W: Path, D: str, ids: set | None, limit: int, resume: bool) -> list[dict]:
    p = W / "outputs" / f"verify_{D}.csv"
    if not p.exists():
        p = W / "outputs" / f"extract_{D}.csv"
    out = []
    for r in csv.DictReader(p.open(encoding="utf-8-sig")):
        if (r.get("운영현황_로직") or "").strip() != "I-3":
            continue
        rid = r["언론사_id"]
        if ids and rid not in ids:
            continue
        if resume and (W / "work" / f"{rid}.i3.json").exists():
            continue
        out.append(r)
    return out[:limit] if limit else out


def 화면텍스트(W: Path, rid: str) -> dict:
    p = W / "work" / f"{rid}.json"
    d = {}
    if p.exists():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            d = {}
    return {
        "title": (d.get("title") or "")[:200],
        "og": (d.get("og_site_name") or "")[:120],
        "상단": re.sub(r"\s+", " ", d.get("상단_텍스트") or "")[:HEAD_CAP],
        "하단": re.sub(r"\s+", " ", d.get("하단_텍스트") or "")[:FOOT_CAP],
        "길이": (d.get("텍스트_길이") or {}).get("전체") or 0,
    }


def user_turn(r: dict, t: dict) -> str:
    return (f"등록부 정보\n"
            f"  등록 제호: {r.get('제호_등록', '')}\n"
            f"  등록 URL: {r.get('메인_url', '')}\n\n"
            f"화면에서 읽은 것 (본문 총 {t['길이']}자)\n"
            f"  <title>: {t['title'] or '(없음)'}\n"
            f"  og:site_name: {t['og'] or '(없음)'}\n"
            f"  [상단]\n{t['상단'] or '(없음)'}\n"
            f"  [하단]\n{t['하단'] or '(없음)'}")


def build_request(r: dict, t: dict):
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request
    return Request(
        custom_id=batch.cid(str(r["언론사_id"])),
        params=MessageCreateParamsNonStreaming(
            model=MODEL, max_tokens=MAX_TOKENS,
            system=[{"type": "text", "text": SYSTEM,
                     "cache_control": {"type": "ephemeral"}}],
            tools=[build_tool()],
            tool_choice={"type": "tool", "name": TOOL_NAME},
            messages=[{"role": "user", "content": user_turn(r, t)}]))


def 기록(W: Path, r: dict, raw: dict, usage: dict, D: str) -> dict:
    rid = r["언론사_id"]
    규칙 = 판별(title="", 상단="", 하단="", 본문길이=0)  # 자리표시 — 아래에서 실제로 다시 낸다
    t = 화면텍스트(W, rid)
    d0 = json.loads((W / "work" / f"{rid}.json").read_text(encoding="utf-8")) \
        if (W / "work" / f"{rid}.json").exists() else {}
    규칙 = 판별(title=d0.get("title", ""), 상단=d0.get("상단_텍스트", ""),
              하단=d0.get("하단_텍스트", ""), og_site_name=d0.get("og_site_name", ""),
              본문길이=t["길이"], 등록제호=r.get("제호_등록", ""))
    doc = {
        "언론사_id": rid, "제호_등록": r.get("제호_등록", ""), "메인_url": r.get("메인_url", ""),
        "모델": MODEL, "기준일": D,
        "성격": raw.get("kind", ""), "설명": raw.get("description", ""),
        "근거": raw.get("evidence", ""), "화면이름": raw.get("screen_name", ""),
        "언론사인가": raw.get("is_news_site"), "검색값어치": raw.get("search_worth", ""),
        "확신도": raw.get("confidence", ""),
        "후속조치": 후속.get(raw.get("kind", ""), ""),
        "규칙_성격": 규칙["성격"], "규칙_값어치": 규칙["검색값어치"],
        "규칙과_일치": 규칙["성격"] == raw.get("kind", ""),
        "본문길이": t["길이"],
        "usage": usage, "비용_USD": round(cost_of(usage), 5),
    }
    (W / "work" / f"{rid}.i3.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    audit.record(W, rid, "2c_I3성격", doc["성격"] or "판독실패", {
        "성격": doc["성격"], "설명": doc["설명"], "근거": doc["근거"],
        "화면이름": doc["화면이름"], "언론사인가": doc["언론사인가"],
        "검색값어치": doc["검색값어치"], "확신도": doc["확신도"],
        "규칙_성격": doc["규칙_성격"], "규칙과_일치": doc["규칙과_일치"],
        "판단": "지침 I-3(홈페이지 없음) 판정은 그대로다. 후속 조치 우선순위를 위한 성격 구분이다",
    }, 제호=r.get("제호_등록"), 기준일=D)
    return doc


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--base-date", "--기준일", dest="base_date", required=True)
    ap.add_argument("--ids", default="")
    ap.add_argument("--ids-file", dest="ids_file", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--resume", action="store_true", help="이미 판독한 건은 건너뛴다")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--sync", action="store_true", help="순차 호출 (소량 실측용)")
    ap.add_argument("--batch-submit", action="store_true")
    ap.add_argument("--batch-collect", action="store_true")
    ap.add_argument("--poll", type=int, default=batch.POLL_SEC)
    args = ap.parse_args()

    W = Path(args.workdir)
    D = args.base_date
    _src = args.ids + ("," + Path(args.ids_file).read_text(encoding="utf-8")
                       if args.ids_file else "")
    ids = {s for s in re.split(r"[,\s]+", _src) if s} or None
    대상 = 대상목록(W, D, ids, args.limit, args.resume)
    if not 대상:
        print(json.dumps({"대상": 0, "비고": "I-3 대상 없음(또는 --resume 로 전부 건너뜀)"},
                        ensure_ascii=False)); return

    if args.dry_run:
        입력 = 0
        for r in 대상:
            t = 화면텍스트(W, r["언론사_id"])
            입력 += len(SYSTEM) + len(user_turn(r, t))
        tok = 입력 / 2.2          # 한글 혼합 문서의 대략적 문자→토큰 비
        est = tok / 1e6 * 2.0 + len(대상) * 260 / 1e6 * 10.0
        print(json.dumps({
            "모드": "dry-run (호출 없음)", "모델": MODEL, "대상": len(대상),
            "추정_입력토큰": int(tok), "추정_출력토큰": len(대상) * 260,
            "추정비용_USD_순차": round(est, 2),
            "추정비용_USD_배치": round(est * 0.5, 2),
            "주의": "문자수 기반 거친 추정이다. --sync --limit 20 으로 실측해라",
        }, ensure_ascii=False, indent=2)); return

    load_env_key()
    import anthropic
    client = anthropic.Anthropic()

    if args.batch_submit:
        reqs = [build_request(r, 화면텍스트(W, r["언론사_id"])) for r in 대상]
        meta = batch.submit(client, W, "i3", D, reqs,
                            note=f"모델={MODEL} I-3 화면성격 판독")
        print(json.dumps(meta, ensure_ascii=False, indent=2)); return

    prefetched, 배치실패 = {}, []
    if args.batch_collect:
        meta = batch.load_meta(W, "i3", D)
        if not meta.get("batch_id"):
            print(json.dumps({"error": "배치 메타 없음 — --batch-submit 먼저"},
                             ensure_ascii=False)); sys.exit(1)
        batch.wait_ended(client, meta["batch_id"], poll=args.poll)
        prefetched, 배치실패 = batch.results(client, meta["batch_id"], TOOL_NAME)

    rows, 총 = [], 0.0
    for i, r in enumerate(대상, 1):
        rid = r["언론사_id"]
        raw = None
        if prefetched:
            got = prefetched.get(rid)
            if not got:
                사유 = 배치실패.get(rid, "결과 없음")
                print(f"[{i}/{len(대상)}] {rid} 배치 실패 — {사유}", file=sys.stderr)
                continue
            # batch.results 는 **이미 tool_use 의 input 을 꺼내서** 준다.
            # 여기서 msg.content 를 다시 뒤지면 항상 빈손이 된다.
            raw, usage = got
        else:
            t = 화면텍스트(W, rid)
            msg = client.messages.create(
                model=MODEL, max_tokens=MAX_TOKENS,
                system=[{"type": "text", "text": SYSTEM,
                         "cache_control": {"type": "ephemeral"}}],
                tools=[build_tool()], tool_choice={"type": "tool", "name": TOOL_NAME},
                messages=[{"role": "user", "content": user_turn(r, t)}])
            usage = msg.usage.model_dump() if hasattr(msg.usage, "model_dump") else dict(msg.usage)
        if raw is None:                      # 순차 경로 — 응답에서 직접 꺼낸다
            raw = {}
            for b in (msg.content if hasattr(msg, "content") else []):
                if getattr(b, "type", "") == "tool_use" and getattr(b, "name", "") == TOOL_NAME:
                    raw = b.input or {}
                    break
        if not raw:
            print(f"[{i}/{len(대상)}] {rid} 도구 미호출 — 건너뜀", file=sys.stderr)
            continue
        doc = 기록(W, r, raw, usage, D)
        총 += doc["비용_USD"]
        rows.append(doc)
        표 = "=" if doc["규칙과_일치"] else "≠"
        print(f"[{i}/{len(대상)}] {rid} {(doc['제호_등록'] or '')[:12]:<14}"
              f"{doc['성격']:<16} {표} 규칙:{doc['규칙_성격']:<16}"
              f"값어치={doc['검색값어치']} ${doc['비용_USD']:.5f}", file=sys.stderr)

    from collections import Counter
    일치 = sum(1 for r in rows if r["규칙과_일치"])
    print(json.dumps({
        "모델": MODEL, "기준일": D, "판독": len(rows),
        "총비용_USD": round(총, 5),
        "건당_평균_USD": round(총 / max(1, len(rows)), 5),
        "성격_분포": dict(Counter(r["성격"] for r in rows).most_common()),
        "검색값어치_분포": dict(Counter(r["검색값어치"] for r in rows).most_common()),
        "규칙과_일치": f"{일치}/{len(rows)} ({일치/max(1,len(rows))*100:.1f}%)",
        "불일치_표본": [{"id": r["언론사_id"], "LLM": r["성격"], "규칙": r["규칙_성격"],
                    "설명": r["설명"][:90]} for r in rows if not r["규칙과_일치"]][:10],
        "배치_실패": len(배치실패), "배치_실패_상세": dict(list(배치실패.items())[:20]),
        "다음": "성격별로 포털검색 대상을 좁힌 뒤 resolve_failed.py --ids-file 로 돌린다",
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
