#!/usr/bin/env python3
"""run_extract — 전수 fetch + 룰 기반 필요적 게재사항 추출.

사용:
  python run_extract.py --workdir W --input 등록정보.xlsx [--map '{...}']
      [--기준일 2026-07-28] [--limit 20] [--ids 20001,20002] [--workers 8]
      [--운영컬럼 조사_결과_요약 --운영값 운영]

산출:
  W/work/{id}.json            사이트별 상세(zones 길이·findings·override 적용여부)
  W/outputs/extract_{기준일}.csv   와이드 CSV (게재값/위치/확신도/추출방법/근거)
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from md_core import audit, operation  # noqa: E402
from md_core.fields import FIELDS, REQUIRED_FIELDS, extract  # noqa: E402
from md_core.http import fetch_html_ex, normalize_url  # noqa: E402
from md_core.registry import load_registry  # noqa: E402
from md_core.schema import extract_columns, row_from_extract  # noqa: E402
from md_core.zones import disclosure_block, extract_zones  # noqa: E402

HINT_KEYS = ("제호", "발행인", "편집인", "등록번호", "발행소 주소", "발행소 전화번호")

# work/{id}.json 에 보존할 zone 텍스트 길이. LLM 검토·비전 진단이 이 값을 원문으로 쓰므로
# 잘리면 정당한 규칙값이 '근거 없음' 으로 기각된다(pilot3 30884). 넉넉히 둔다.
FOOTER_KEEP = 12000
HEADER_KEEP = 4000


def domain_of(url: str) -> str:
    from urllib.parse import urlparse
    host = urlparse(normalize_url(url)).netloc.lower()
    return host[4:] if host.startswith("www.") else host


# 도메인 파킹·판매 중개 페이지. 여기로 리다이렉트되면 '미게재' 가 아니라 폐업·도메인 소멸이다.
# pilot3 30887 (주)충청연합방송이 expireddomains.com 으로 리다이렉트됐는데 HTTP 200 이고
# 텍스트가 8163자 있어 '접속 가능' 으로 통과하고 미검출 8항목 → 미준수로 찍혔다.
PARKING_HOSTS = re.compile(
    r"expireddomains|godaddy|sedo(param)?|afternic|dan\.com|hugedomains|namecheap|"
    r"parkingcrew|bodis|above\.com|domainnamesales|undeveloped|porkbun|"
    r"buydomains|domain(market|agents|holdings)|whois", re.I)


def redirect_flags(url: str, final_url: str) -> tuple[str, str, str]:
    """→ (리다이렉트, 도메인변경, 파킹의심).

    한글 도메인은 punycode 로 정규화되므로 도메인변경으로 오분류하지 않는다
    (예: 리얼팩트뉴스.com → xn--vg1bx0l32hqjd47vmta.com 은 같은 도메인이다).
    """
    if not url or not final_url:
        return "", "", ""
    d0, d1 = domain_of(url), domain_of(final_url)
    if d0 == d1:
        return "", "", ""
    try:                                    # 한글 도메인 ↔ punycode 동일성 확인
        같음 = d0.encode("idna").decode("ascii") == d1.encode("idna").decode("ascii")
    except Exception:                       # noqa: BLE001
        같음 = False
    if 같음:
        return "Y", "", ""
    파킹 = "Y" if PARKING_HOSTS.search(final_url) else ""
    return "Y", "Y", 파킹


def load_override(workdir: Path, url: str) -> dict:
    d = domain_of(url)
    if not d:
        return {}
    p = workdir / "overrides" / f"{d}.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
    return {}


def 운영현황_기록(meta: dict, op: dict) -> None:
    """지침 I 판정 결과를 meta 에 실어 CSV 컬럼으로 흘려보낸다."""
    meta.update({"홈페이지_운영현황": op["운영현황"], "운영현황_로직": op["로직"],
                 "매체명_동일": op["매체명_동일"], "뉴스_있음": op["뉴스_있음"],
                 "운영현황_사유": op["사유"][:300]})


def 이전_수집기록(workdir: Path, rid: str) -> dict:
    """앞선 실행이 **실제로 사이트를 방문해** 남긴 수집 메타를 되살린다.

    R0c 는 저장된 렌더HTML 을 `file://` 로 다시 읽는다. 그때 그 로컬 경로를 `최종URL` 로
    적으면 원래 URL 과 달라 `도메인변경=Y` 가 찍힌다 — 전수에서 3,939건(29%)이 이렇게
    허위 리다이렉트가 됐고, 그게 확인필요 사유 1위였다. 검수자에게 볼 필요 없는 건을
    보라고 지시한 셈이고, 동시에 **진짜 리다이렉트·도메인 파킹을 못 잡게** 됐다
    (파킹의심 열이 13,574건 중 1건짜리 죽은 열이 됐다).

    수집 증빙(최종URL·HTTP상태·접속경로·시도)은 로컬 파일 읽기가 아니라 **원래 방문**의
    것이어야 한다. 근거를 좋은 것부터 찾는다:

      ① audit 의 `4_증빙캡처` 최종URL — Playwright 가 **실제로 도달한** 주소다.
         리다이렉트를 브라우저가 끝까지 따라간 결과라 가장 정확하다.
      ② 앞선 work/{id}.json 의 final_url — 단 그것도 file:// 이면 못 쓴다.
         R0c 를 이미 한 번 돌린 workdir 은 이 값이 오염돼 있다.
      ③ 등록부 URL — 아무 근거도 없으면 **없는 리다이렉트를 지어내지 않는다.**

    ③으로 떨어지면 리다이렉트를 놓칠 수는 있어도 **없는 것을 있다고 하지는 않는다.**
    허위 도메인변경이 3,939건 붙어 확인필요 1위를 차지했던 게 원래 문제였다.
    """
    out: dict = {}
    p = workdir / "work" / f"{rid}.json"
    if p.exists():
        try:
            w = json.loads(p.read_text(encoding="utf-8"))
            if not str(w.get("final_url") or "").startswith("file:"):
                out = dict(w)
        except Exception:  # noqa: BLE001
            pass
    try:
        for s in audit.load(workdir, rid).get("단계", []):
            if s.get("단계") != "4_증빙캡처":
                continue
            fu = str((s.get("상세") or {}).get("최종URL") or "")
            if fu and not fu.startswith("file:"):
                out["final_url"] = fu
                out.setdefault("접속경로", "브라우저 방문")
            break
    except Exception:  # noqa: BLE001
        pass
    return out


def process(rec: dict, workdir: Path, base: date) -> dict:
    rid = str(rec.get("id") or "")
    url = rec.get("url") or ""
    ov = load_override(workdir, url)
    fetch_target = ov.get("rendered_html") or url
    로컬재판독 = bool(ov.get("rendered_html")) and fetch_target != url

    meta = {"언론사_id": rid, "제호_등록": rec.get("제호", ""), "메인_url": url,
            "운영상태": rec.get("운영상태", "")}

    if not url and not ov.get("rendered_html"):
        meta.update({"접속결과": "URL없음", "HTTP상태": "", "최종URL": "", "리다이렉트": "", "js_의심": ""})
        운영현황_기록(meta, operation.evaluate(접속결과="URL없음", 등록제호=rec.get("제호", "")))
        row = row_from_extract(meta, {})
        row["비고"] = "입력에 URL 없음"
        return {"row": row, "detail": {"언론사_id": rid, "error": "URL 없음"}}

    status, html, final_url, fmeta = fetch_html_ex(fetch_target)
    시도로그 = " | ".join(fmeta.get("시도") or [])
    blocked = "blocked-by-allowlist" in html[:400] or "Connection blocked by network" in html[:400]
    if not html or blocked or status >= 400:
        # 추측을 사실처럼 적지 않는다 — 실제 시도 결과만 기록한다 (ISS-011)
        if blocked:
            사유 = ("샌드박스 프록시 allowlist 차단 — 네트워크가 열린 환경(로컬/Claude Code)에서 재실행 필요. "
                  "미게재 아님")
        elif status >= 500:
            사유 = f"HTTP {status} — 서버측 오류(전 스킴 시도). 사이트 생존 가능. 시도: {시도로그}"
        elif status >= 400:
            사유 = f"HTTP {status} — 차단(WAF/UA)·페이지 없음 가능. 시도: {시도로그}"
        else:
            사유 = f"연결 불가(전 스킴 실패). 시도: {시도로그}"
        meta.update({"접속결과": "불가", "HTTP상태": status, "최종URL": final_url,
                     "리다이렉트": "", "접속경로": fmeta.get("경로", ""), "js_의심": ""})
        운영현황_기록(meta, operation.evaluate(접속결과="불가", 등록제호=rec.get("제호", "")))
        row = row_from_extract(meta, {})
        row["비고"] = 사유[:180]
        audit.record(workdir, rid, "1_수집", "실패",
                     {"url": url, "HTTP상태": status, "접속경로": fmeta.get("경로", ""),
                      "시도": fmeta.get("시도"), "판단": 사유, "차단의심": blocked},
                     제호=rec.get("제호"), 기준일=base.isoformat())
        return {"row": row, "detail": {"언론사_id": rid, "제호_등록": rec.get("제호", ""),
                                       "url": url, "status": status, "error": 사유,
                                       "fetch_시도": fmeta.get("시도"), "network_blocked": blocked}}

    z = extract_zones(html, footer_selector=ov.get("footer_selector", ""),
                      header_selector=ov.get("header_selector", ""))

    # 본문이 완전히 비어 있으면 '미게재' 가 아니라 '수집 실패' 다 (ISS-013).
    # status 200 이어도 판정 근거가 없으므로 미준수로 찍지 않고 홈페이지없음(지침 I-4)으로 보낸다.
    #
    # 임계값은 '0자' 다. 길이 기반 임계값은 취약하다 — 픽스처 F03 은 134자로 필수 7/8 을
    # 게재하고 있어서, 200자 같은 값을 쓰면 정상 사이트를 수집실패로 오분류한다.
    # 1~50자 수준의 준-빈 페이지는 zones.py 의 js_의심 플래그가 확인필요로 잡는다(잔여 위험).
    if len(z.get("전체") or "") == 0 and not ov.get("rendered_html"):
        사유 = (f"HTTP {status} 이나 본문 텍스트 0자 — 빈 페이지·JS 렌더링 의심. "
              f"미게재 아님. Playwright/Chrome 렌더링 후 재판독 필요")
        meta.update({"접속결과": "내용없음", "HTTP상태": status, "최종URL": final_url,
                     "리다이렉트": "", "접속경로": fmeta.get("경로", ""), "js_의심": "Y"})
        운영현황_기록(meta, operation.evaluate(접속결과="내용없음", 등록제호=rec.get("제호", "")))
        row = row_from_extract(meta, {})
        row["비고"] = 사유[:180]
        audit.record(workdir, rid, "1_수집", "내용없음",
                     {"url": url, "HTTP상태": status, "접속경로": fmeta.get("경로", ""),
                      "시도": fmeta.get("시도"), "html_길이": len(html),
                      "본문길이": 0, "판단": 사유},
                     제호=rec.get("제호"), 기준일=base.isoformat())
        return {"row": row, "detail": {"언론사_id": rid, "제호_등록": rec.get("제호", ""),
                                       "url": url, "final_url": final_url, "status": status,
                                       "접속경로": fmeta.get("경로", ""), "error": 사유,
                                       "텍스트_길이": {k: len(z.get(k, "")) for k in ("전체", "상단", "하단")},
                                       "html_길이": len(html)}}

    hints = {k: rec.get(k, "") for k in HINT_KEYS}
    result = extract(z, hints=hints, base_date=base)

    # 규칙 단독 기준을 **override 적용 전** 에 확정한다.
    # 나중에 '수동 제외' 로 세면 재실행마다 값이 내려간다 — override 가 원래 규칙도 잡았던
    # 항목까지 덮어 수동으로 바뀌기 때문이다(pilot3 재실행에서 147 → 130 으로 떨어졌다).
    규칙단독_게재 = [f for f in REQUIRED_FIELDS
                if ((result.get(f) or {}).get("값") or "").strip() not in ("", "해당사항 없음")]

    # override 값 적용 (룰 결과 덮어쓰기)
    for f, spec in (ov.get("fields") or {}).items():
        if f in FIELDS and isinstance(spec, dict):
            result[f] = {"값": str(spec.get("값", "")), "근거": str(spec.get("근거", "")),
                         "위치": str(spec.get("위치", "하단")), "확신도": "high",
                         "추출방법": "수동"}

    # 수집 증빙은 **원래 사이트 방문**의 것이어야 한다. 로컬 렌더HTML 재판독(R0c)일 때
    # file:// 경로를 최종URL 로 적으면 허위 도메인변경이 되고 파킹 탐지가 죽는다(ISS-073).
    앞기록 = 이전_수집기록(workdir, rid) if 로컬재판독 else {}
    증빙_최종URL = (앞기록.get("final_url") or url) if 로컬재판독 else final_url
    증빙_상태 = (앞기록.get("status") or status) if 로컬재판독 else status
    증빙_경로 = ((앞기록.get("접속경로") or "") + " + 렌더HTML 재판독").strip(" +") \
        if 로컬재판독 else fmeta.get("경로", "")
    증빙_시도 = (앞기록.get("fetch_시도") if 로컬재판독 else fmeta.get("시도")) or []

    rd, dc, pk = redirect_flags(url, 증빙_최종URL)
    meta.update({"접속결과": "가능", "HTTP상태": 증빙_상태, "최종URL": 증빙_최종URL,
                 "리다이렉트": rd, "도메인변경": dc, "파킹의심": pk,
                 "접속경로": 증빙_경로,
                 "js_의심": "Y" if z["js_의심"] else ""})
    # ★ 지침 I — 필요적 게재사항(II)은 이 관문을 '있음' 으로 통과한 매체만 점검한다.
    #   추출은 이미 끝났으므로 화면에서 읽은 제호를 매체명 대조에 함께 쓴다.
    op = operation.evaluate(접속결과="가능", 등록제호=rec.get("제호", ""), zones=z,
                            화면제호=(result.get("제호") or {}).get("값", ""),
                            파킹의심=pk, html=html, 등록정보=hints)
    운영현황_기록(meta, op)
    row = row_from_extract(meta, result)
    row["필수8_게재수_규칙"] = len(규칙단독_게재)
    row["준수여부_규칙"] = ("홈페이지없음" if op["운영현황"] == operation.없음
                      else "준수" if len(규칙단독_게재) == len(REQUIRED_FIELDS)
                      else "미준수")
    if ov:
        row["비고"] = ("override 적용" + (f" / {ov.get('notes','')}" if ov.get("notes") else ""))[:180]

    # 단계별 감사 추적 — 검수자가 "왜 이 값이 나왔는가" 를 되짚을 수 있어야 한다
    audit.record(workdir, rid, "1_수집", "성공",
                 {"url": url, "최종URL": 증빙_최종URL, "HTTP상태": 증빙_상태,
                  "접속경로": 증빙_경로, "시도": 증빙_시도,
                  "로컬재판독": 로컬재판독,
                  "리다이렉트": row.get("리다이렉트", ""),
                  "판단": "본문 확보" + (" (인증서 검증 우회)" if fmeta.get("ssl무검증") else "")},
                 제호=rec.get("제호"), 기준일=base.isoformat())
    audit.record(workdir, rid, "2_영역분리", "성공",
                 {"텍스트_길이": {k: len(z.get(k, "")) for k in ("전체", "상단", "하단")},
                  "selector_출처": {"하단": ov.get("footer_selector") or "자동(힌트매칭/폴백)",
                                 "상단": ov.get("header_selector") or "자동(힌트매칭/폴백)"},
                  "js_의심": z["js_의심"], "title": z.get("title", ""),
                  "og_site_name": z.get("og_site_name", ""),
                  "판단": "신문법은 메인화면 상·하단 게재를 요구하므로 위치가 값만큼 중요하다"},
                 제호=rec.get("제호"), 기준일=base.isoformat())
    audit.record(workdir, rid, "2b_운영현황", op["운영현황"],
                 {"로직": op["로직"], "사유": op["사유"],
                  "매체명_동일": op["매체명_동일"], "뉴스_있음": op["뉴스_있음"],
                  "게재사항_일치": op.get("게재사항_일치") or [],
                  "등록제호": rec.get("제호", ""),
                  "화면제호": (result.get("제호") or {}).get("값", ""),
                  "포털검색_필요": op["포털검색_필요"],
                  "판단": "지침 I — 홈페이지 '있음' 인 매체만 필요적 게재사항 점검 대상이다"},
                 제호=rec.get("제호"), 기준일=base.isoformat())
    audit.record(workdir, rid, "3_규칙추출",
                 "성공" if int(row.get("필수8_게재수") or 0) == 8 else "부분",
                 {"필수8_게재수": row.get("필수8_게재수"),
                  "미게재_항목": row.get("미게재_항목"),
                  "override_적용": bool(ov),
                  "항목별": {f: {"값": (result.get(f) or {}).get("값", ""),
                             "위치": (result.get(f) or {}).get("위치", ""),
                             "확신도": (result.get(f) or {}).get("확신도", ""),
                             "추출방법": (result.get(f) or {}).get("추출방법", ""),
                             "근거": (result.get(f) or {}).get("근거", "")}
                          for f in FIELDS},
                  "판단": "정규식 라벨 매칭 → 형식 매칭 → 등록정보 일치 → title 폴백 순 우선순위"},
                 제호=rec.get("제호"), 기준일=base.isoformat())

    # 게재사항 밀집 구간 — 상단·하단 어디에도 없을 때만 판단 단계에 따로 넘긴다
    게재정보 = disclosure_block(z.get("전체", ""))
    게재블록 = 게재정보.get("텍스트") or ""
    if 게재블록:
        이미 = " ".join((z.get("상단", "") + "\n" + z.get("하단", "")).split())
        핵심 = " ".join(게재블록.split())[:60]
        if 핵심 and 핵심 in 이미:
            게재블록 = ""

    detail = {"언론사_id": rid, "제호_등록": rec.get("제호", ""), "url": url,
              "final_url": 증빙_최종URL, "로컬재판독": 로컬재판독,
              "status": 증빙_상태, "접속경로": 증빙_경로, "fetch_시도": 증빙_시도,
              "js_의심": z["js_의심"], "override": bool(ov),
              "텍스트_길이": {k: len(z.get(k, "")) for k in ("전체", "상단", "하단")},
              "title": z["title"], "og_site_name": z["og_site_name"],
              "findings": result,
              # ★ 캡을 넉넉히 둔다. 규칙은 zone 전문으로 추출하는데 LLM 에게 잘린 텍스트를
              # 주면, LLM 이 '원문에 근거가 없다' 며 정당한 규칙값을 기각한다.
              # pilot3 30884 세종포스트: 하단 3461자 중 3000자만 저장 → 게재사항 정보 바가
              # 캡 뒤에 있어 LLM 이 6항목을 규칙기각(필수8 7→2). 잘림 여부도 함께 남긴다.
              # ★ 게재사항이 footer 밖(본문 중간 위젯)에 있는 사이트가 있다. 상단·하단만
              #   LLM 에 주면 모델이 "근거가 없다" 며 정당한 규칙값을 전부 기각한다
              #   (1012 슬기로운시니어뉴스 실측 — 하단 99자, 게재사항은 중간 <ul><li>).
              #   상단·하단에 이미 담긴 내용이면 붙이지 않는다(중복 토큰 낭비 방지).
              "게재사항_블록": 게재블록,
              "게재사항_블록_라벨수": 게재정보.get("라벨수", 0),
              "하단_텍스트": z["하단"][:FOOTER_KEEP], "상단_텍스트": z["상단"][:HEADER_KEEP],
              "하단_잘림": len(z["하단"]) > FOOTER_KEEP,
              "상단_잘림": len(z["상단"]) > HEADER_KEEP}
    return {"row": row, "detail": detail}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--input", required=True)
    ap.add_argument("--sheet")
    ap.add_argument("--map", dest="map_json", default="")
    ap.add_argument("--base-date", "--기준일", dest="base_date", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--ids", default="", help="쉼표 구분 언론사_id (일부만 재실행)")
    # 윈도 CreateProcess 의 명령줄 상한은 32,767자다. R0c 는 렌더링 개선 건 전부를
    # --ids 로 넘기는데 전수 기준 약 5,800건 = 29,000자라 상한에 12% 밖에 안 남는다.
    # 8시간짜리 캡처 뒤에 명령줄 길이로 죽는 건 최악이라 파일 경로로도 받는다.
    ap.add_argument("--ids-file", dest="ids_file", default="",
                    help="언론사_id 목록 파일 (쉼표 또는 줄바꿈 구분). --ids 와 합쳐진다")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--op-col", "--운영컬럼", dest="op_col", default="", help="운영상태 필터할 표준키(운영상태)")
    ap.add_argument("--op-val", "--운영값", dest="op_val", default="운영")
    args = ap.parse_args()

    base = (datetime.strptime(args.base_date, "%Y-%m-%d").date()
            if args.base_date else date.today())
    workdir = Path(args.workdir)
    for sub in ("work", "outputs", "overrides", "shots"):
        (workdir / sub).mkdir(parents=True, exist_ok=True)

    override = json.loads(args.map_json) if args.map_json else None
    rows, mapping, _ = load_registry(args.input, override_map=override, sheet=args.sheet)
    if "url" not in mapping:
        print(json.dumps({"error": "URL 컬럼을 찾지 못했다 — inspect_registry.py 로 확인 후 --map 지정"},
                         ensure_ascii=False)); sys.exit(1)

    if args.op_col:
        before = len(rows)
        rows = [r for r in rows if args.op_val in str(r.get("운영상태", ""))]
        제외 = before - len(rows)
    else:
        제외 = 0
    id원문 = args.ids
    if args.ids_file:
        id원문 += "," + Path(args.ids_file).read_text(encoding="utf-8")
    if id원문.strip(","):
        want = {s.strip() for s in re.split(r"[,\s]+", id원문) if s.strip()}
        rows = [r for r in rows if str(r.get("id")) in want]
    if args.limit:
        rows = rows[: args.limit]

    # ★ ex.map 은 **제출 순서대로** 결과를 내준다. 한 건이 오래 걸리면 그 뒤에 이미 끝난
    #   수천 건의 work/{id}.json 이 통째로 밀린다 — 2026-09-03 전수 R0 에서 10121 하나가
    #   34분을 잡아먹는 동안 work 는 10120 에서 멈춘 채 audit 만 1,100건 앞서 나갔다.
    #   as_completed 로 바꿔 **끝나는 대로** 쓴다. 느린 건은 자기 워커 하나만 잡아먹는다.
    #   CSV 행 순서는 입력 순서를 유지해야 하므로 제출 인덱스로 되돌려 정렬한다.
    done: dict[int, dict] = {}
    t0, 다음보고 = time.time(), 1000
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futs = {ex.submit(process, r, workdir, base): i for i, r in enumerate(rows)}
        for fut in as_completed(futs):
            i = futs[fut]
            res = fut.result()
            done[i] = res
            d = res["detail"]
            # row 를 함께 넣어 둔다 — 중단 후 이어받을 때 CSV 를 재구성할 수 있어야 한다
            (workdir / "work" / f"{d['언론사_id']}.json").write_text(
                json.dumps(dict(d, row=res["row"]), ensure_ascii=False, indent=2),
                encoding="utf-8")
            if len(done) >= 다음보고:
                el = time.time() - t0
                남음 = len(rows) - len(done)
                print(f"[R0] {len(done)}/{len(rows)} ({len(done)/len(rows)*100:.1f}%) "
                      f"{el/60:.0f}분 경과 · {len(done)/el*60:.0f}건/분 · "
                      f"남은 예상 {남음/(len(done)/el)/60:.0f}분", file=sys.stderr, flush=True)
                다음보고 += 1000

    results = [done[i] for i in sorted(done)]
    out_rows = [r["row"] for r in results]
    csv_path = workdir / "outputs" / f"extract_{base.isoformat()}.csv"

    # 부분 재실행(--ids/--limit)은 기존 CSV 를 덮어쓰지 않고 병합한다 (ISS-016).
    # 인계 문서가 안내하는 복구 경로가 '--ids 로 이어가라' 이므로, 덮어쓰면 앞서 처리한
    # 수천 건이 CSV 에서 사라진다. 전수 실행일 때만 새로 쓴다.
    # ★ --ids-file 을 빠뜨리면 부분 재실행이 '전수' 로 오인돼 CSV 가 통째로 덮인다.
    #   2026-09-04 인수인계에서 734건 재수집이 13,574행을 734행으로 날릴 뻔했다.
    부분실행 = bool(args.ids or args.ids_file or args.limit)
    병합됨 = 0
    if 부분실행 and csv_path.exists():
        merged: dict[str, dict] = {}
        with csv_path.open(encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                merged[str(row.get("언론사_id"))] = row
        병합됨 = len(merged)
        for r in out_rows:
            merged[str(r.get("언론사_id"))] = r
        out_rows_write = list(merged.values())
    else:
        out_rows_write = out_rows

    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=extract_columns(), extrasaction="ignore")
        w.writeheader()
        w.writerows(out_rows_write)

    미검출_분포: dict[str, int] = {}
    for r in out_rows:
        for f in (r["미게재_항목"] or "").split(", "):
            if f:
                미검출_분포[f] = 미검출_분포.get(f, 0) + 1

    summary = {
        "기준일": base.isoformat(), "mapping": mapping, "대상_사이트": len(out_rows),
        "운영필터_제외": 제외,
        "접속실패": sum(1 for r in out_rows if r["접속결과"] != "가능"),
        "접속실패_id": [r["언론사_id"] for r in out_rows if r["접속결과"] != "가능"],
        "접속경로_분포": {k: sum(1 for r in out_rows if (r.get("접속경로") or "") == k)
                    for k in sorted({(r.get("접속경로") or "") for r in out_rows}) if k},
        "네트워크차단_의심": sum(1 for r in out_rows if "allowlist" in (r.get("비고") or "")),
        "js_의심": [r["언론사_id"] for r in out_rows if r["js_의심"] == "Y"],
        "홈페이지_있음": sum(1 for r in out_rows if r.get("홈페이지_운영현황") == "있음"),
        "홈페이지_없음": sum(1 for r in out_rows if r.get("홈페이지_운영현황") == "없음"),
        "운영현황_로직분포": {k: sum(1 for r in out_rows if (r.get("운영현황_로직") or "") == k)
                       for k in ("I-1", "I-2", "I-3", "I-4")},
        "8항목_전부게재": sum(1 for r in out_rows if r["준수여부_8항목"] == "준수"),
        "미준수": sum(1 for r in out_rows if r["준수여부_8항목"] == "미준수"),
        "홈페이지없음": sum(1 for r in out_rows if r["준수여부_8항목"] == "홈페이지없음"),
        "확인필요": sum(1 for r in out_rows if r["확인필요"] == "Y"),
        "항목별_미검출": dict(sorted(미검출_분포.items(), key=lambda x: -x[1])),
        "판독필요_사이트": [r["언론사_id"] for r in out_rows
                     if r["접속결과"] == "가능" and int(r["필수8_게재수"] or 0) <= 5][:30],
        "extract_csv": str(csv_path),
        "csv_행수": len(out_rows_write),
        "csv_병합": (f"부분 재실행 — 기존 {병합됨}행에 이번 {len(out_rows)}건을 병합"
                  if 부분실행 and 병합됨 else ("부분 재실행(기존 CSV 없음)" if 부분실행 else "전수 실행 — 새로 작성")),
        "다음": "compare_registry.py 로 등록정보 대조 → 판독필요 사이트는 fetch_masthead.py + override",
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
