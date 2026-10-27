#!/usr/bin/env python3
"""resolve_failed — 접속 불가 건의 실체를 API 로 판별한다 (ISS-011).

설계 원칙 (CLAUDE.md)
---------------------
이 판단은 **스크립트가 Claude API 를 호출해서** 한다. 대화 중인 에이전트가 WebSearch 로
대신하지 않는다. 검색은 API 의 **`web_search_20260209` 서버 도구**가 수행하므로 별도
검색 API 키나 스크래핑이 필요 없다.

지침 I 의 '포털검색' 단계다
--------------------------
지침 I 로직 3·4(홈페이지 없음)는 **포털검색 실시**로 이어진다. 이 스크립트가 그 단계다.
검색으로 찾은 사이트는 지침 I 비고에 따라 **필요적 게재사항 5항목 중 1개 이상**이
등록부와 같아야 진행한다 (등록번호·발행소 주소·발행소 전화번호·발행인·편집인).

무엇을 판별하나
---------------
접속 실패는 성격이 다르고 판정도 달라야 한다:

    ① 도메인 소멸/변경   새 주소에 생존 → 그 URL 로 재수집해 진행
    ② 폐업·운영중단      검색해도 사이트 없음 → **홈페이지 없음** 확정
    ③ 서버 다운·차단     도메인 살아있고 사이트도 있으나 접속만 실패 → 재시도
    ④ 빈 응답(HTTP 200)  렌더링·차단 문제. 검색 대상이 아니다

②는 종전에 '미준수'(과태료 대상)로 찍었으나, 2026-08-13 지침 체계에서 홈페이지 없음은
필요적 게재사항 점검 대상 자체가 아니다 — `홈페이지없음` 으로 기록한다.

핵심 안전장치 — 게재사항 대조
-----------------------------
검색 결과만으로 '같은 매체' 라고 단정하면 **동명·유사명 매체를 오인한다.** pilot3 에서
`세종시사뉴스` 를 검색하면 `세종시뉴스닷컴`·`세종포스트`·`세종의소리` 가 나온다.
그래서 후보 사이트를 실제로 fetch 해 게재사항 5항목을 등록부와 대조한다.
5항목은 매체를 특정하는 정보이므로 유사명 매체는 어느 것도 일치하지 않는다.

산출
----
  W/work/{id}.resolved.json   판별 결과 + 검색 근거 + 게재사항 대조
  W/outputs/extract_*.csv     검색_판정 · 검색_후보URL 컬럼 채움
  W/audit/{id}.json           1b_실패해소 단계

사용
----
  python resolve_failed.py --workdir W --base-date 2026-07-31          # 접속 불가 전건
  python resolve_failed.py --workdir W --ids 30880,30881
  python resolve_failed.py --workdir W --dry-run
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import socket
import sys
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from md_core import audit  # noqa: E402
from md_core.http import fetch_html_ex, normalize_url  # noqa: E402
from md_core.registry import COMPARE_FIELDS, load_registry, norm_regnum  # noqa: E402
from md_core.schema import extract_columns, apply_flags, 검색_미발견, 검색_후보발견  # noqa: E402
from md_core.zones import extract_zones  # noqa: E402
from llm_extract import MODEL, cost_of, load_env_key  # noqa: E402

TOOL_NAME = "record_resolution"
MAX_TOKENS = 3000
WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search", "max_uses": 4}

판정_도메인변경 = "도메인변경_확인"
판정_폐업 = 검색_미발견
판정_후보 = 검색_후보발견
판정_서버다운 = "서버다운_재시도필요"
판정_도메인만료 = "도메인만료_폐업의심"
판정_대상아님 = "검색대상아님"


SYSTEM = """\
너는 한국 인터넷신문 실태점검에서 **접속 실패한 매체의 실체**를 판별한다.

등록부에 있는 매체의 URL 로 접속이 안 됐다. 웹 검색으로 다음을 판별해라.

## 판별 순서
1. `web_search` 로 **제호를 검색**한다. 필요하면 제호 + 지역, 제호 + '인터넷신문' 으로 재검색.
2. 검색 결과에 **제호가 정확히 일치하는 언론사 웹사이트**가 있는가?
   - 유사명에 속지 마라. '세종시사뉴스' 를 찾는데 '세종시뉴스닷컴' 이 나오면 **다른 매체다.**
   - 제호가 부분만 겹치면 후보로 삼지 마라.
3. 있으면 그 URL 을 `candidate_url` 로 낸다. 없으면 빈 문자열.

## 판정 (`verdict`)
- `도메인변경_확인` — 제호가 정확히 일치하는 사이트를 찾았다. 입력 URL 과 도메인이 다르다
- `제호일치_후보발견` — 후보를 찾았으나 같은 매체인지 확신이 낮다
- `제호일치_미발견` — 검색해도 그 제호의 웹사이트가 없다. 폐업·운영중단 의심
- `서버다운_재시도필요` — 사이트는 존재하는 것으로 보이나(검색에 잡힘) 일시적 접속 장애로 보인다

## 반드시 지킬 것
- **추측하지 마라.** 검색 결과에 근거가 없으면 `제호일치_미발견` 이다.
- `evidence` 에 검색에서 실제로 본 것을 적어라 — 결과 제목·URL·근거 문구.
- SNS 계정(X·페이스북·인스타)만 있고 웹사이트가 없으면 `제호일치_미발견` 이다.
  단 `notes` 에 SNS 잔존을 적어라.
- 언론사 디렉터리·기사 인용에만 이름이 나오고 자체 사이트가 없으면 역시 `제호일치_미발견` 이다.
- 도메인 판매·파킹 페이지는 사이트가 아니다.
- **`도메인변경_확인` 또는 `제호일치_후보발견` 으로 판정하면 `candidate_url` 을 반드시 채워라.**
  근거에 URL 을 적고 candidate_url 을 비우면 프로그램이 검증할 수 없어 판정이 폐기된다.
- **입력된 URL 자체를 candidate_url 로 내지 마라.** 그건 이미 접속 실패한 곳이라 새 정보가 아니다.
  검색 캐시에 옛 흔적이 남아 있어도 마찬가지다.

반드시 record_resolution 도구를 호출해 제출해라."""


def build_tool() -> dict:
    return {
        "name": TOOL_NAME,
        "description": "접속 실패 매체의 실체 판별 결과를 제출한다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "verdict": {"type": "string",
                            "enum": [판정_도메인변경, 판정_후보, 판정_폐업, 판정_서버다운],
                            "description": "판정"},
                "candidate_url": {"type": "string",
                                  "description": "제호가 일치하는 사이트 URL. 없으면 빈 문자열"},
                "candidate_title": {"type": "string", "description": "그 사이트의 표기 제호"},
                "searched": {"type": "string", "description": "실제로 던진 검색어들"},
                "evidence": {"type": "string",
                             "description": "검색에서 본 것 — 결과 제목·URL·근거 문구"},
                "similar_but_different": {"type": "string",
                                          "description": "유사명이라 배제한 후보들 (오인 방지 기록)"},
                "notes": {"type": "string"},
            },
            "required": ["verdict", "candidate_url", "candidate_title", "searched",
                         "evidence", "similar_but_different", "notes"],
            "additionalProperties": False,
        },
    }


def dom_of(url: str) -> str:
    h = urlparse(normalize_url(url or "")).netloc.lower().split(":")[0]
    return h[4:] if h.startswith("www.") else h


def dns_ok(url: str) -> tuple[bool, str]:
    host = urlparse(normalize_url(url or "")).netloc.split(":")[0]
    if not host:
        return False, "호스트 없음"
    try:
        socket.getaddrinfo(host, None)
        return True, "DNS 해석됨"
    except Exception as exc:  # noqa: BLE001
        return False, f"DNS 실패({exc.__class__.__name__})"


# 등록번호 추출 — 반드시 엄격해야 한다.
#
# 느슨한 패턴(`[아가나다라마바사자차카타파하]\s*\d+`)은 한글 문장에서 마구 오탐한다.
# pilot3 에서 '그러나 22', '종특별자치시가 25', '되살린다 10' 을 등록번호로 잡아
# **정답 후보(news8070.com, 아00036 일치)를 '불일치' 로 배제하고 폐업으로 강등**했다.
#
# 두 가지 형태만 인정한다:
#   ① `등록번호` 라벨 뒤 60자 이내에 오는 것 (가장 신뢰도 높음)
#   ② 종별문자와 숫자가 **붙어 있고 숫자가 3자리 이상** 인 것 (아00036 / 세종아00036)
_REG_LABEL = re.compile(r"(?:인터넷\s*)?(?:신문\s*)?등\s*록\s*번\s*호|정기간행물\s*등록\s*번호")
# 라벨 뒤에서 찾을 때는 공백을 조금 허용한다 (`세종, 아00036`, `아 00036`)
_REG_NEAR = re.compile(
    r"(?:([가-힣]{2,6})\s*[,·]?\s*)?([아가나다라마바사자차카타파하])\s?-?\s?(\d{3,6})\s*호?")
# 라벨이 없을 때는 **붙어 있는 것만** 인정한다. 공백을 허용하면 한글 문장에서
# '관계자 300', '그러나 22' 같은 오탐이 난다(pilot3 에서 정답 후보를 두 번 오배제했다).
_REG_STRICT = re.compile(r"(?:([가-힣]{2,6})[,·]?)?([아가나다라마바사자차카타파하])(\d{4,6})호?")


def _find_regnums(text: str) -> list[tuple[str, str]]:
    """→ [(정규값, 원문조각)]. 라벨 근처 우선, 없으면 붙어 있는 형태만."""
    out: list[tuple[str, str]] = []
    for m in _REG_LABEL.finditer(text):
        # '사업자등록번호' 는 대상이 아니다
        앞 = text[max(0, m.start() - 4): m.start()]
        if "사업자" in 앞 or "법인" in 앞:
            continue
        for t in _REG_NEAR.finditer(text[m.end(): m.end() + 60]):
            _, v = norm_regnum(t.group(0))
            if v:
                out.append((v, t.group(0).strip()))
    if out:
        return out
    for t in _REG_STRICT.finditer(text):
        _, v = norm_regnum(t.group(0))
        if v:
            out.append((v, t.group(0).strip()))
    return out


def verify_candidate(candidate_url: str, 등록정보: dict) -> dict:
    """후보 사이트를 fetch 해 **필요적 게재사항 5항목 중 1개 이상**이 등록부와 같은지 본다.

    지침 I 비고
    -----------
    > 포털검색으로 찾은 홈페이지는 필요적 게재사항(등록번호, 주소, 전화번호, 발행인, 편집인)
    > 중 **1개 이상 동일한지 확인** 후 진행

    종전에는 **등록번호만** 봤다. 등록번호는 고유해서 확실하지만, 화면에 안 적는 매체가
    적지 않고 오탐도 났다 — pilot3 에서 한글 문장의 '그러나 22' 를 등록번호로 잡아
    정답 후보(news8070.com)를 '불일치' 로 배제하고 폐업으로 강등한 적이 있다.
    지침이 5항목 중 1개로 완화했으므로 그대로 따르되, **어떤 항목이 일치했는지** 를 남긴다.

    유사명 오인 방지라는 원래 목적은 유지된다 — 5항목은 매체를 특정하는 정보이고,
    '세종시사뉴스' 와 '세종시뉴스닷컴' 은 이 중 어느 것도 같지 않다.
    """
    out = {"시도": bool(candidate_url and any((등록정보 or {}).get(f) for f in COMPARE_FIELDS)),
           "결과": "미확인", "일치항목": [], "후보_등록번호": "", "근거": "", "대조상세": {}}
    if not out["시도"]:
        out["결과"] = "미확인(후보URL 또는 등록 대조값 없음)"
        return out
    st, html, final, _ = fetch_html_ex(candidate_url)
    if not html:
        out["결과"] = f"후보 접속 실패(HTTP {st})"
        return out

    def _scan(h: str, 출처: str) -> bool:
        z = extract_zones(h)
        hay = (z.get("하단") or "") + "\n" + (z.get("전체") or "")
        # ① 등록번호 — 고유값이라 가장 강한 증거다. 엄격 패턴으로만 찾는다.
        want_reg = norm_regnum(등록정보.get("등록번호") or "")[1]
        found = _find_regnums(hay)
        if found:
            out["후보_등록번호"] = ", ".join(x[1] for x in found[:3])
        if want_reg:
            for v, raw in found:
                if v and v == want_reg:
                    out["일치항목"].append("등록번호")
                    out["대조상세"]["등록번호"] = f"{출처}: '{raw}'"
        # ② 나머지 4항목 — 등록값 문자열이 후보 화면에 그대로 나타나는지 본다.
        #    정규화(공백·하이픈·괄호 제거)해서 대조한다. 주소는 앞 20자만 봐야
        #    '층·호' 표기차로 놓치지 않는다.
        hay_n = _squash(hay)
        for f, 최소길이, 자르기 in (("발행인", 2, 0), ("편집인", 2, 0),
                                 ("발행소 전화번호", 9, 0), ("발행소 주소", 8, 20)):
            want = _squash(등록정보.get(f) or "")
            if 자르기:
                want = want[:자르기]
            if len(want) >= 최소길이 and want in hay_n:
                out["일치항목"].append(f)
                out["대조상세"][f] = f"{출처}: 등록값 '{등록정보.get(f)}' 가 후보 화면에 있음"
        return bool(out["일치항목"])

    맞음 = _scan(html, f"후보 메인({final})")
    if not 맞음:
        # ★ 게재사항이 메인에 없고 **기사 페이지 하단**에만 있는 사이트가 흔하다.
        # pilot3 news8070.com 이 그랬다 — 메인 하단 320자에 등록번호 라벨이 없고,
        # 개별 기사 하단에 '등록번호: 세종, 아00036' 이 있었다. 메인만 보고 '불일치' 로
        # 판정해 **정답 후보를 폐업으로 오판**했다. 기사 한 편을 따라가 확인한다.
        try:
            from verify_pubdate import article_links
            links = article_links(html, final or candidate_url)
        except Exception:  # noqa: BLE001
            links = []
        for _title, aurl in links[:2]:
            _st, ahtml, afinal, _ = fetch_html_ex(aurl)
            if ahtml and _scan(ahtml, f"후보 기사({afinal})"):
                맞음 = True
                break

    if 맞음:
        out["일치항목"] = sorted(set(out["일치항목"]))
        out["결과"] = "일치"
        out["근거"] = (f"지침 I 비고 — 게재사항 5항목 중 {len(out['일치항목'])}개 일치: "
                     + ", ".join(f"{k}({v})" for k, v in out["대조상세"].items()))[:400]
        return out
    본것 = (out.get("후보_등록번호") or "").strip()
    out["결과"] = "불일치"
    out["근거"] = ("지침 I 비고의 5항목(등록번호·주소·전화번호·발행인·편집인) 중 "
                 "후보 사이트와 일치하는 것이 하나도 없다"
                 + (f" (후보에서 본 등록번호: '{본것}')" if 본것 else ""))
    return out


def _squash(s: str) -> str:
    """대조용 정규화 — 공백·하이픈·괄호·구두점을 지운다."""
    return re.sub(r"[\s\-–—.,()·∙・|/\\'\"]+", "", str(s or ""))


# ── CSV 반영 (ISS-072) ──────────────────────────────────────────────────────
# 종전에는 루프가 **전부 끝난 뒤** 한 번만 썼다. 그래서 도중에 죽으면 이미 돈을 낸 검색
# 결과가 CSV 에 한 줄도 안 들어갔다. `--resume` 은 .resolved.json 존재만 보고 건너뛰므로
# 다시 돌려도 복구되지 않는다 — 지불한 만큼이 영구히 사라진다. 실제로 143건을 잃을 뻔했다.
#
# 그래서 FLUSH_EVERY 건마다 흘려보낸다. 쓰기는 임시파일 → 교체라 쓰다 죽어도 원본이 남는다.
FLUSH_EVERY = 25


def csv_반영(rows: list, 변경: dict, csv_path) -> int:
    n = 0
    for row in rows:
        ch = 변경.get(row["언론사_id"])
        if not ch:
            continue
        if (row.get("검색_판정") or "") == ch["검색_판정"]:
            continue
        row.update(ch)
        apply_flags(row)
        n += 1
    tmp = csv_path.with_suffix(".csv.tmp")
    with tmp.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=extract_columns(), extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    tmp.replace(csv_path)
    return n


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--input", default="", help="등록정보 xlsx (등록번호 검증용)")
    ap.add_argument("--sheet")
    ap.add_argument("--map", dest="map_json", default="")
    ap.add_argument("--base-date", "--기준일", dest="base_date", default="")
    ap.add_argument("--ids", default="")
    ap.add_argument("--ids-file", dest="ids_file", default="",
                    help="언론사_id 목록 파일 (쉼표·줄바꿈 구분)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    # ── 예산 상한과 재개 ────────────────────────────────────────────────────
    # 웹검색은 검색 1,000회당 $10 이 토큰과 **별도로** 붙어 단가가 가장 비싸다.
    # 전수 4,407건이 예산을 넘길 수 있으므로, 한도에 닿으면 **깨끗이 멈추고** 어디까지
    # 했는지 남긴다. 다음 실행에서 --resume 으로 이어서 돌린다.
    ap.add_argument("--budget", type=float, default=0.0,
                    help="이번 실행의 비용 상한(USD). 넘으면 중단하고 기록한다. 0=무제한")
    ap.add_argument("--resume", action="store_true",
                    help="이미 .resolved.json 이 있는 건은 건너뛴다")
    args = ap.parse_args()

    base = (datetime.strptime(args.base_date, "%Y-%m-%d").date()
            if args.base_date else date.today())
    W = Path(args.workdir)
    D = base.isoformat()
    csv_path = W / "outputs" / f"extract_{D}.csv"
    if not csv_path.exists():
        print(json.dumps({"error": f"{csv_path} 없음 — run_extract 를 먼저 돌려라"},
                         ensure_ascii=False)); sys.exit(1)
    rows = list(csv.DictReader(io.open(csv_path, encoding="utf-8-sig")))

    등록 = {}
    if args.input:
        override = json.loads(args.map_json) if args.map_json else None
        rr, _, _ = load_registry(args.input, override_map=override, sheet=args.sheet)
        등록 = {str(r.get("id")): r for r in rr}

    _idsrc = args.ids
    if args.ids_file:
        _idsrc += "," + Path(args.ids_file).read_text(encoding="utf-8")
    ids = {s for s in re.split(r"[,\s]+", _idsrc) if s} or None
    건너뜀 = 0
    대상 = []
    for r in rows:
        rid = r["언론사_id"]
        if ids and rid not in ids:
            continue
        # 지침 I 에서 '없음' 으로 걸린 매체는 **전부** 포털검색 대상이다.
        #
        # 종전에는 `접속결과 != '가능'` 만 봤다. 그러면 I-3(매체명 상이 + 뉴스 없음)이
        # 통째로 빠진다 — 그 매체들은 접속이 **정상으로 되기** 때문이다. 전수에서 670건이
        # 이렇게 누락됐다. 하필 이들이 검색으로 뒤집힐 가능성이 가장 높은 집단이다:
        # 사이트는 살아 있는데 등록 제호와 화면 제호가 다른 경우라, 포털에서 진짜 홈페이지를
        # 찾으면 '있음' 으로 돌아선다.
        운영없음 = (r.get("홈페이지_운영현황") or "").strip() == "없음"
        접속불가 = not (r.get("접속결과") == "가능" and (r.get("파킹의심") or "") != "Y")
        if not (운영없음 or 접속불가):
            continue
        if args.resume and (W / "work" / f"{rid}.resolved.json").exists():
            건너뜀 += 1
            continue
        대상.append(r)
    if args.limit:
        대상 = 대상[: args.limit]
    if not 대상:
        print(json.dumps({"error": "접속 불가·파킹 건 없음 — 해소할 대상이 없다"},
                         ensure_ascii=False)); sys.exit(0)

    # 검색 대상 선별: HTTP 200 인데 본문만 빈 건은 렌더링·차단 문제이므로 검색이 무의미하다
    분류 = []
    for r in 대상:
        rid = r["언론사_id"]
        st = str(r.get("HTTP상태") or "0")
        파킹 = (r.get("파킹의심") or "") == "Y"
        ok, dnsmsg = dns_ok(r.get("메인_url"))
        로직 = (r.get("운영현황_로직") or "").strip()
        if 파킹:
            성격, 검색 = "도메인 파킹 — 실체 확인 필요", True
        elif 로직 == "I-3":
            # 접속은 되는데 지침 I 에서 탈락 — 등록 제호와 화면 제호가 다르고 뉴스도 없다.
            # 등록부의 제호로 진짜 홈페이지가 따로 있는지 찾아야 한다(지침 I-3 → 포털검색).
            성격, 검색 = "접속되나 매체명 상이·뉴스 없음 — 실제 홈페이지 확인 필요", True
        elif st.isdigit() and 200 <= int(st) < 400:
            # ★ 종전에는 여기서 검색=False 로 잘라냈다 (ISS-011/023, 2026-08-05).
            #   근거는 "HTTP 200 인데 본문만 비었으면 렌더링·차단 문제라 검색이 무의미하다" 였다.
            #   그런데 2026-08-13 지침이 "등록된 URL로 접속 시 **오류페이지** → 홈페이지 없음/
            #   포털검색 실시"(I-4)로 정했고, 우리 파이프라인도 이 건들을 I-4 로 판정해 놓는다.
            #   같은 실행 안에서 한쪽은 '오류페이지' 라 부르고 다른 쪽은 '오류페이지가 아니다'
            #   라며 검색을 건너뛰던 자기모순이었다 (ISS-075, 전수 274건).
            #
            #   실측이 종전 근거를 뒤집었다: 274건 중 265건은 R0b 에서 Playwright 로 이미
            #   렌더링을 받았는데도 렌더 후 본문 0자 167건, 403 Forbidden 33건, 호스팅 만료
            #   5건, 도메인 판매/파킹 4건이었다. 렌더링 문제가 아니라 지침이 말한 그 오류페이지다.
            #   게다가 호스팅 만료·도메인 판매 건은 매체가 새 도메인으로 옮겼을 개연성이
            #   가장 높은 집단이라, 검색 수익률이 최상인 대상을 골라서 빼고 있었다.
            성격, 검색 = (f"HTTP {st} 이나 본문 없음 — 오류·만료·차단 페이지. "
                       f"지침 I-4 포털검색 대상"), True
        elif not ok:
            성격, 검색 = f"{dnsmsg} — 도메인 소멸 의심", True
        else:
            성격, 검색 = f"{dnsmsg} 이나 연결 실패 — 서버 다운·차단 의심", True
        분류.append({"row": r, "id": rid, "성격": 성격, "검색대상": 검색, "dns": dnsmsg})

    if args.dry_run:
        print(json.dumps({
            "모드": "dry-run", "모델": MODEL,
            "web_search_도구": WEB_SEARCH_TOOL["type"],
            "대상": [{"id": c["id"], "제호": c["row"].get("제호_등록"),
                    "url": c["row"].get("메인_url"), "HTTP": c["row"].get("HTTP상태"),
                    "성격": c["성격"], "검색수행": c["검색대상"]} for c in 분류],
            "검색_수행건수": sum(1 for c in 분류 if c["검색대상"]),
            "추정비용_USD": round(sum(1 for c in 분류 if c["검색대상"]) * 0.02, 4),
        }, ensure_ascii=False, indent=2)); return

    load_env_key()
    import anthropic
    client = anthropic.Anthropic()
    tool = build_tool()

    결과, usages, 변경 = [], [], {}
    누적비용 = 0.0
    예산중단 = None
    for i, c in enumerate(분류, 1):
        r, rid = c["row"], c["id"]
        제호 = r.get("제호_등록") or ""

        # ── 예산 상한 ──────────────────────────────────────────────────────
        # 호출 **전에** 본다. 넘긴 뒤 멈추면 상한을 초과한 채로 끝난다.
        # 여기서 멈춰도 지금까지 쓴 .resolved.json 은 그대로 남아 --resume 으로 이어진다.
        if args.budget and 누적비용 >= args.budget and c["검색대상"]:
            남은 = [x["id"] for x in 분류[i - 1:] if x["검색대상"]]
            예산중단 = {"멈춘_지점": i, "처리": i - 1, "남은_검색대상": len(남은),
                     "누적비용_USD": round(누적비용, 4), "상한_USD": args.budget}
            남은파일 = W / "logs" / f"resolve_남은ids_{D}.txt"
            남은파일.parent.mkdir(parents=True, exist_ok=True)
            남은파일.write_text("\n".join(남은), encoding="utf-8")
            예산중단["남은_id_파일"] = str(남은파일)
            print(f"[예산] 상한 ${args.budget} 도달 (누적 ${누적비용:.4f}) — "
                  f"{len(남은)}건 남기고 중단. 재개: --resume", file=sys.stderr)
            break
        등록번호 = (등록.get(rid, {}) or {}).get("등록번호") or r.get("등록번호_등록값") or ""

        if not c["검색대상"]:
            doc = {"언론사_id": rid, "제호": 제호, "판정": 판정_대상아님,
                   "성격": c["성격"], "dns": c["dns"], "검색_수행": False}
            (W / "work" / f"{rid}.resolved.json").write_text(
                json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
            audit.record(W, rid, "1b_실패해소", "검색대상아님", doc, 제호=제호, 기준일=D)
            변경[rid] = {"검색_판정": 판정_대상아님, "검색_후보URL": ""}
            결과.append({"id": rid, "제호": 제호, "판정": 판정_대상아님, "성격": c["성격"]})
            print(f"[{i}/{len(분류)}] {rid} {제호[:12]:<14}검색 생략 — {c['성격']}", file=sys.stderr)
            continue

        user = (f"등록부 정보\n"
                f"  제호: {제호}\n"
                f"  등록번호: {등록번호 or '(없음)'}\n"
                f"  등록된 URL: {r.get('메인_url')}\n"
                f"  발행소 주소: {(등록.get(rid, {}) or {}).get('발행소 주소') or '(없음)'}\n"
                f"\n접속 시도 결과\n"
                f"  HTTP: {r.get('HTTP상태')}  DNS: {c['dns']}\n"
                f"  성격: {c['성격']}\n"
                f"\n이 매체의 웹사이트가 현재 존재하는지 검색으로 판별해라.")
        try:
            resp = client.messages.create(
                model=MODEL, max_tokens=MAX_TOKENS,
                system=[{"type": "text", "text": SYSTEM,
                         "cache_control": {"type": "ephemeral"}}],
                tools=[WEB_SEARCH_TOOL, tool],
                tool_choice={"type": "any"},
                messages=[{"role": "user", "content": user}],
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[{i}/{len(분류)}] {rid} 실패: {exc.__class__.__name__}: {exc}", file=sys.stderr)
            audit.record(W, rid, "1b_실패해소", "실패",
                         {"예외": f"{exc.__class__.__name__}: {exc}"[:300]}, 제호=제호, 기준일=D)
            continue

        raw = next((b.input for b in resp.content
                    if getattr(b, "type", "") == "tool_use" and b.name == TOOL_NAME), {})
        검색횟수 = sum(1 for b in resp.content
                   if getattr(b, "type", "") == "server_tool_use")
        u = resp.usage
        usage = {"input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
                 "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
                 "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
                 "server_tool_use": 검색횟수, "stop_reason": resp.stop_reason}
        usages.append(usage)

        판정 = raw.get("verdict") or 판정_폐업
        후보 = (raw.get("candidate_url") or "").strip()

        # 후보가 **입력 URL 과 같은 도메인**이면 새 정보가 없다 — 후보로 인정하지 않는다.
        # 모델이 검색 캐시에서 옛 흔적을 보고 원래 도메인을 후보로 내놓는 일이 잦다(pilot3 2/3건).
        동일도메인 = ""
        if 후보 and dom_of(후보) and dom_of(후보) == dom_of(r.get("메인_url")):
            동일도메인 = f"후보 도메인({dom_of(후보)})이 입력 URL 과 같아 후보로 인정하지 않음"
            후보 = ""

        # 지침 I 비고 — 게재사항 5항목(등록번호·주소·전화번호·발행인·편집인) 중 1개 이상 일치
        검증 = (verify_candidate(후보, 등록.get(rid) or {}) if 후보
              else {"시도": False, "결과": "후보없음", "일치항목": []})

        최종 = 판정
        강등 = 동일도메인
        if 후보 and 검증.get("결과") == "일치":
            최종 = 판정_도메인변경
        elif 후보 and 검증.get("결과") == "불일치":
            최종 = 판정_폐업
            강등 = ((강등 + " / " if 강등 else "")
                  + "후보 사이트에서 게재사항 5항목 중 일치하는 것이 없어 다른 매체로 판단")
            후보 = ""
        elif 후보:
            최종 = 판정_후보
            강등 = ((강등 + " / " if 강등 else "")
                  + f"동일 매체 확인 실패({검증.get('결과')}) — 사람 확인 필요")

        # DNS 가 실패했으면 도메인이 존재하지 않는다. '서버다운_재시도필요' 는 재시도하면
        # 될 것처럼 오해를 주므로 허용하지 않는다 — 도메인 만료·폐업 의심이다.
        if not c["dns"].startswith("DNS 해석") and 최종 in (판정_서버다운,):
            최종 = 판정_도메인만료
            강등 = (강등 + " / " if 강등 else "") + \
                f"{c['dns']} — 도메인이 존재하지 않으므로 재시도 대상이 아니다"

        doc = {"언론사_id": rid, "제호": 제호, "등록번호": 등록번호, "모델": MODEL,
               "기준일": D, "성격": c["성격"], "dns": c["dns"], "검색_수행": True,
               "검색횟수": 검색횟수, "모델_판정": 판정, "최종_판정": 최종, "강등이유": 강등,
               "후보URL": 후보, "후보제호": raw.get("candidate_title", ""),
               "검색어": raw.get("searched", ""), "근거": raw.get("evidence", ""),
               "배제한_유사명": raw.get("similar_but_different", ""),
               "게재사항_대조": 검증, "notes": raw.get("notes", ""),
               "usage": usage, "비용_USD": round(cost_of(usage), 5)}
        (W / "work" / f"{rid}.resolved.json").write_text(
            json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        audit.record(W, rid, "1b_실패해소", 최종, doc, 제호=제호, 기준일=D)
        변경[rid] = {"검색_판정": 최종, "검색_후보URL": 후보}
        누적비용 += doc["비용_USD"]
        if len(변경) % FLUSH_EVERY == 0:
            csv_반영(rows, 변경, csv_path)      # 죽어도 여기까지는 남는다
        결과.append({"id": rid, "제호": 제호, "판정": 최종, "후보": 후보,
                    "게재사항대조": 검증.get("결과"),
                    "일치항목": 검증.get("일치항목") or [], "강등": 강등,
                    "비용": doc["비용_USD"]})
        print(f"[{i}/{len(분류)}] {rid} {제호[:12]:<14}{최종:<18}"
              f"후보={후보[:38] or '-':<40}대조={검증.get('결과')}"
              f"({','.join(검증.get('일치항목') or []) or '-'})", file=sys.stderr)

    csv_반영(rows, 변경, csv_path)

    총 = sum(cost_of(u) for u in usages)
    from collections import Counter
    print(json.dumps({
        "모델": MODEL, "web_search_도구": WEB_SEARCH_TOOL["type"],
        "기준일": D, "대상": len(분류),
        "재개_건너뜀": 건너뜀,
        "예산중단": 예산중단,
        "재개방법": ("python resolve_failed.py --workdir W --기준일 D --resume "
                 "[--budget N] — 이미 끝난 건은 자동으로 건너뛴다"),
        "검색_수행": sum(1 for c in 분류 if c["검색대상"]),
        "판정_분포": dict(Counter(x["판정"] for x in 결과)),
        "총비용_USD": round(총, 5),
        "결과": 결과,
        "extract_csv": str(csv_path),
        "다음": "compare_registry.py → finalize.py (검색_판정 이 준수여부에 반영된다)",
        "주의": ["지침 I 비고 — 게재사항 5항목 중 1개 이상 일치해야 같은 매체로 인정한다",
               "일치가 하나도 없는 후보는 자동 배제했다 — 유사명 오인 방지",
               "도메인변경_확인 건은 URL 을 갱신해 재수집해야 한다 (지침 I: '진행')",
               "제호일치_미발견 = 홈페이지 없음 확정. 미준수(과태료 대상)가 아니다"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
