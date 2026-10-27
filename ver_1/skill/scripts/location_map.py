#!/usr/bin/env python3
"""location_map — 언론사별 '각 항목을 어디서 확인할 수 있는지' 지도를 엑셀로 남긴다.

왜 필요한가
-----------
점검은 매년 반복된다. 그런데 지금 구조는 매번 처음부터 다시 찾는다 — 규칙이 훑고, LLM 이
검토하고, 실패하면 비전으로 진단한다. 이 과정에서 **"이 매체의 발행인은 하단 `#footer` 안
`대표:` 라벨 뒤에 있다"** 는 지식이 만들어지는데, 지금은 그게 흩어진 JSON 에만 남는다.

이 스크립트는 그 지식을 한 파일로 모은다. 다음 회차에는:
  - 이 엑셀만 보고 어디를 볼지 바로 알 수 있다
  - selector 가 살아 있으면 규칙만으로 끝난다 (LLM 호출 0회)
  - 사이트가 개편돼 selector 가 깨진 건만 다시 진단한다

산출
----
  W/outputs/위치지도_{기준일}.xlsx
    시트 `항목위치`  — 언론사 × 항목 (긴 형식). 한 행이 "이 매체의 이 항목은 여기 있다"
    시트 `언론사요약` — 매체당 1행. 도메인·selector·게재수·진단이력
    시트 `재사용안내` — 다음 회차 사용법

사용
----
  python location_map.py --workdir ver_1/run2 --input ver_1/input/pilot2.xlsx --기준일 2026-07-31
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from md_core import audit  # noqa: E402
from md_core.fields import FIELDS, REQUIRED_FIELDS, to_iso  # noqa: E402
from md_core.schema import DATE_FIELDS, xlsx_safe  # noqa: E402
from md_core.http import normalize_url  # noqa: E402

# 추출방법 문자열 → 사람이 읽을 '확인방법'
방법_라벨 = {
    "라벨": "라벨 매칭",
    "형식매칭(라벨없음)": "형식 매칭(라벨 없음)",
    "라벨없음_등록이름일치": "등록정보 이름 일치(라벨 없음)",
    "등록제호_문자열일치": "등록 제호 문자열 일치",
    "title추정": "title 태그 추정(상·하단 아님)",
    "과거날짜추정(라벨없음)": "과거 날짜 추정(라벨 없음)",
    "최신기사날짜후보": "최신 기사 날짜",
    "저작권표기(확인필요)": "저작권 표기",
    "영문패턴": "영문 패턴",
    "병기(발행·편집인)": "발행·편집인 병기",
    "수동": "수동 판독(override)",
    "미검출": "미검출",
    "미발견": "미발견",
    "LLM": "LLM 텍스트 검토",
}


def domain_of(url: str) -> str:
    host = urlparse(normalize_url(url or "")).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def load_json(p: Path) -> dict:
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def 라벨_추출(근거: str, 항목: str) -> str:
    """근거 문구에서 그 항목의 **실제 라벨 표기** 를 뽑는다.

    구분자 앞을 무조건 잡으면 엉뚱한 값이 나온다(예: 등록연월일 근거에서 '138 ㅣ 팩스').
    fields.LABEL 의 항목별 정규식으로 매칭해 실제 라벨만 돌려준다.
    라벨이 없으면 `(라벨 없음)` — 그것도 다음 회차에 필요한 정보다(형식·문맥으로 찾아야 한다).
    """
    import re
    from md_core.fields import LABEL
    if not 근거:
        return ""
    pat = LABEL.get(항목)
    if pat:
        m = re.search(pat, 근거)
        if m:
            return m.group(0).strip()
    return "(라벨 없음)"


def build_rows(workdir: Path, registry: dict) -> tuple[list[dict], list[dict]]:
    항목행, 요약행 = [], []
    for p in sorted((workdir / "work").glob("*.json")):
        if audit.is_sidecar(p.name):
            continue
        d = load_json(p)
        rid = str(d.get("언론사_id") or p.stem)
        제호 = d.get("제호_등록") or registry.get(rid, {}).get("제호", "")
        url = d.get("url") or registry.get(rid, {}).get("url", "")
        dom = domain_of(url)
        ov = load_json(workdir / "overrides" / f"{dom}.json") if dom else {}
        llm = load_json(workdir / "work" / f"{rid}.llm.json")
        diag = load_json(workdir / "work" / f"{rid}.diag.json")
        au = audit.load(workdir, rid)
        단계결과 = {s.get("단계"): s.get("결과") for s in au.get("단계", [])}

        규칙 = d.get("findings") or {}
        llm_f = llm.get("검증후_findings") or {}
        diag_f = diag.get("검증후_정정") or {}
        pubd = load_json(workdir / "work" / f"{rid}.pubdate.json")
        이미지표기 = set(diag.get("이미지표기_항목") or [])
        # capture_shots 가 렌더된 DOM 에서 각 값을 실제로 찾아 남긴 위치 기록.
        # 이게 이 파일을 '다음 회차에 이것만 보면 되는' 문서로 만든다 — 항목별 CSS 셀렉터,
        # 화면영역, 좌표, 텍스트 오프셋, 주변문맥이 다 들어 있다.
        ev_items = (load_json(workdir / "evidence" / f"{rid}.json").get("항목") or {})

        게재수 = 0
        for f in FIELDS:
            # 발행연월일은 전용 검증(verify_pubdate)이 최우선 — 정의가 '최신 기사 업데이트 날짜'
            if f == "발행연월일" and pubd:
                src = "발행일검증"
                item = {"값": pubd.get("발행연월일_게재", ""),
                        "근거": pubd.get("게재_근거", ""),
                        "위치": pubd.get("게재_위치", "없음"),
                        "확신도": "high" if pubd.get("발행연월일_게재") else "low"}
                방법 = f"최신 기사 날짜 확인({pubd.get('확인경로','')})"
                값 = (item.get("값") or "").strip()
                if 값:
                    게재수 += 1
                evi = (ev_items.get(f) or {})
                좌표 = evi.get("좌표") or {}
                항목행.append({
                    "언론사_id": rid, "제호": 제호, "도메인": dom, "URL": url,
                    "항목": f, "필수여부": "필수",
                    "확정값": to_iso(값), "확인위치": item["위치"],
                    "CSS_selector": (evi.get("css")
                                     or (ov.get("footer_selector") if item["위치"] == "하단"
                                         else ov.get("header_selector") if item["위치"] == "상단"
                                         else "") or "(자동 탐지)"),
                    "화면영역": evi.get("영역", ""),
                    "좌표_xy": (f"{좌표.get('x')},{좌표.get('y')}" if 좌표 else ""),
                    "일치문구": evi.get("일치문구", ""),
                    "텍스트오프셋": ("" if evi.get("텍스트오프셋") is None
                                else evi.get("텍스트오프셋")),
                    "라벨근접": evi.get("라벨근접", ""),
                    "위치확정": ("Y" if evi.get("찾음") else ("N" if evi else "-")),
                    "주변문맥": (evi.get("주변문맥") or "")[:150],
                    "라벨표기": 라벨_추출(item.get("근거", ""), f),
                    "확인방법": 방법, "확신도": item["확신도"], "출처단계": src,
                    "이미지표기": "", "근거문구": (item.get("근거") or "")[:150],
                    "최종확인일": pubd.get("기준일") or d.get("기준일") or "",
                })
                continue
            # 출처 우선순위: 비전진단 > LLM검토 > 규칙
            if f in diag_f and diag_f[f].get("값"):
                src, item = "비전진단", diag_f[f]
                방법 = {"이미지판독": "이미지 판독", "DOM": "DOM 구조",
                      "텍스트": "텍스트 재확인"}.get(item.get("source", ""), item.get("source", ""))
            elif (llm_f.get(f) or {}).get("값"):
                src, item = "LLM검토", llm_f[f]
                # 'LLM' 만으로는 다음 회차에 쓸 정보가 없다. 라벨이 있었는지로 구분한다.
                라벨 = 라벨_추출(item.get("근거", ""), f)
                방법 = ("라벨 매칭(LLM 확인)" if 라벨 and 라벨 != "(라벨 없음)"
                      else "라벨 없음(LLM 문맥 판단)")
            else:
                src, item = "규칙", (규칙.get(f) or {})
                방법 = 방법_라벨.get(item.get("추출방법", ""), item.get("추출방법", ""))

            값 = (item.get("값") or "").strip()
            근거 = (item.get("근거") or "").strip()
            위치 = item.get("위치") or "없음"
            if f in REQUIRED_FIELDS and 값 and 값 != "해당사항 없음":
                게재수 += 1

            # CSS_selector — 항목별 실측 셀렉터를 먼저 쓴다.
            # evidence/{id}.json 은 capture_shots 가 렌더된 DOM 에서 값을 실제로 찾아
            # 그 위치를 기록한 것이다. override 의 footer_selector 는 영역 단위라 항목별
            # 재확인에는 거칠고, '(자동 탐지)' 는 다음 회차에 아무 정보도 주지 않는다.
            evi = (ev_items.get(f) or {})
            if evi.get("찾음") and evi.get("css"):
                sel = evi["css"]
            elif 위치 == "하단":
                sel = ov.get("footer_selector") or "(자동 탐지)"
            elif 위치 == "상단":
                sel = ov.get("header_selector") or "(자동 탐지)"
            else:
                sel = ""

            좌표 = evi.get("좌표") or {}
            항목행.append({
                "언론사_id": rid, "제호": 제호, "도메인": dom, "URL": url,
                "항목": f, "필수여부": "필수" if f in REQUIRED_FIELDS else "부가",
                # 날짜 항목은 YYYY-MM-DD 로 통일한다. 사이트 원 표기는 일치문구·근거문구에 남는다.
                "확정값": (to_iso(값) if f in DATE_FIELDS else 값),
                "확인위치": 위치, "CSS_selector": sel,
                # 화면에서 실제로 어디에 있었는가 — 셀렉터가 깨져도 이걸로 되짚는다
                "화면영역": evi.get("영역", ""),
                "좌표_xy": (f"{좌표.get('x')},{좌표.get('y')}" if 좌표 else ""),
                "일치문구": evi.get("일치문구", ""),
                "텍스트오프셋": ("" if evi.get("텍스트오프셋") is None
                            else evi.get("텍스트오프셋")),
                "라벨근접": evi.get("라벨근접", ""),
                "위치확정": ("Y" if evi.get("찾음") else
                          ("N" if evi else "-")),
                "주변문맥": (evi.get("주변문맥") or "")[:150],
                "라벨표기": 라벨_추출(근거, f), "확인방법": 방법,
                "확신도": item.get("확신도", ""), "출처단계": src,
                "이미지표기": "Y" if f in 이미지표기 else "",
                "근거문구": 근거[:150],
                "최종확인일": d.get("기준일") or "",
            })

        요약행.append({
            "언론사_id": rid, "제호": 제호, "도메인": dom, "URL": url,
            "접속결과": d.get("접속결과", ""), "접속경로": d.get("접속경로", ""),
            "필수8_게재수": 게재수,
            "footer_selector": ov.get("footer_selector", ""),
            "header_selector": ov.get("header_selector", ""),
            "렌더링필요": "Y" if ov.get("rendered_html") else "",
            "이미지표기_항목": ", ".join(sorted(이미지표기)),
            "LLM검토": 단계결과.get("5_LLM검토", "-"),
            "비전진단": 단계결과.get("5b_비전진단", "-"),
            # 발행연월일 = 최신 기사 업데이트 날짜. 다음 회차에 어디를 볼지 바로 알 수 있게.
            "발행일_확인경로": pubd.get("확인경로", "-"),
            "발행일_실제": to_iso(pubd.get("발행연월일_실제", "")),
            "발행일_경과일": ("" if pubd.get("경과일") is None else pubd.get("경과일")),
            "발행일_기사URL예": (pubd.get("선택된_기사") or {}).get("url", ""),
            "진단원인": (diag.get("원응답") or {}).get("cause", "")[:160],
            "override_존재": "Y" if ov else "",
            "재사용_가능": ("Y" if (ov.get("footer_selector") or 게재수 == len(REQUIRED_FIELDS))
                       else "부분"),
            "비고": (ov.get("notes") or "")[:200],
        })
    return 항목행, 요약행


def write_xlsx(path: Path, 항목행: list[dict], 요약행: list[dict], base: date) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    hdr_fill = PatternFill("solid", fgColor="DDE5F0")
    hdr_font = Font(bold=True)

    def sheet(title: str, rows: list[dict], widths: dict[str, int]) -> None:
        ws = wb.create_sheet(title)
        if not rows:
            ws["A1"] = "데이터 없음"
            return
        cols = list(rows[0].keys())
        ws.append(cols)
        for c in ws[1]:
            c.fill, c.font = hdr_fill, hdr_font
            c.alignment = Alignment(vertical="center")
        for r in rows:
            ws.append([xlsx_safe(r.get(c, "")) for c in cols])
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        for i, c in enumerate(cols, 1):
            ws.column_dimensions[get_column_letter(i)].width = widths.get(c, 14)

    sheet("항목위치", 항목행, {
        "언론사_id": 10, "제호": 16, "도메인": 20, "URL": 26, "항목": 14, "필수여부": 8,
        "확정값": 30, "확인위치": 9, "CSS_selector": 46, "라벨표기": 16, "확인방법": 22,
        "화면영역": 9, "좌표_xy": 12, "일치문구": 24, "텍스트오프셋": 11,
        "라벨근접": 9, "위치확정": 9, "주변문맥": 44,
        "확신도": 8, "출처단계": 10, "이미지표기": 9, "근거문구": 46, "최종확인일": 12})
    sheet("언론사요약", 요약행, {
        "언론사_id": 10, "제호": 16, "도메인": 20, "URL": 26, "접속결과": 10, "접속경로": 14,
        "필수8_게재수": 11, "footer_selector": 16, "header_selector": 16, "렌더링필요": 10,
        "이미지표기_항목": 18, "LLM검토": 10, "비전진단": 10,
        "발행일_확인경로": 16, "발행일_실제": 12, "발행일_경과일": 11, "발행일_기사URL예": 40,
        "진단원인": 50, "override_존재": 12, "재사용_가능": 11, "비고": 50})

    ws = wb.create_sheet("재사용안내")
    안내 = [
        ["필요적 게재사항 — 항목 위치 지도", ""],
        ["생성일", base.isoformat()],
        ["", ""],
        ["■ 이 파일의 용도", ""],
        ["", "다음 회차 점검에서 '이 매체의 이 항목이 어디 있는지' 를 다시 찾지 않기 위한 지도다."],
        ["", "시트 `항목위치` 는 언론사 × 항목 한 행씩이다. 한 행이 곧 '여기서 확인하라' 는 지시다."],
        ["", ""],
        ["■ 컬럼 읽는 법", ""],
        ["확인위치", "하단/상단 = 신문법이 요구하는 메인화면 상·하단. 기타 = title 태그 등 게재로 인정 못할 위치"],
        ["CSS_selector", "렌더된 DOM 에서 이 값을 실제로 찾은 위치의 셀렉터(evidence/{id}.json). "
                         "없으면 override 의 영역 selector, 그것도 없으면 '(자동 탐지)'"],
        ["화면영역", "상단/중간/하단 — 신문법은 '메인화면 상·하단' 게재를 요구한다"],
        ["좌표_xy", "캡처 당시 문서 좌표(뷰포트 1440x900 기준). 셀렉터가 깨졌을 때 되짚는 보조 수단"],
        ["일치문구", "화면에서 실제로 일치한 문자열. 규칙이 정규화한 값과 표기가 다를 수 있다"],
        ["텍스트오프셋", "텍스트노드 안에서 값이 시작하는 위치. 같은 값이 여러 번 나올 때 구분한다"],
        ["라벨근접", "Y = '발행인 :' 같은 라벨 바로 뒤에서 찾았다. N = 같은 값이 다른 문맥에서 "
                  "잡혔을 수 있어 셀렉터 재사용 전 확인이 필요하다"],
        ["위치확정", "Y = DOM 위치를 특정했다 / N = 화면 텍스트에서 못 찾았다(이미지 표기 등) / "
                  "- = 캡처를 아직 안 했다"],
        ["주변문맥", "그 값이 들어 있던 요소의 텍스트. 사람이 눈으로 바로 확인할 수 있게 남긴다"],
        ["라벨표기", "실제 사용된 라벨 문구. 다음 회차에 이 문구로 검색하면 바로 찾는다"],
        ["확인방법", "라벨 매칭 / 형식 매칭 / 이미지 판독 / DOM 구조 등"],
        ["출처단계", "규칙(정규식) / LLM검토(텍스트) / 비전진단(이미지·DOM)"],
        ["이미지표기", "Y = 텍스트가 아니라 이미지로만 표기됨. 실태점검에서 별도 판단이 필요하다"],
        ["", ""],
        ["■ 다음 회차 사용 절차", ""],
        ["1", "시트 `언론사요약` 의 `재사용_가능`=Y 인 매체는 override 를 그대로 복사해 쓴다"],
        ["2", "규칙 추출만 돌린다 — selector 가 살아 있으면 LLM 호출이 필요 없다"],
        ["3", "게재수가 줄어든 매체만 사이트 개편으로 보고 다시 진단한다(diagnose_site.py)"],
        ["4", "`이미지표기` 항목은 매년 사람이 확인해야 한다 — 자동 판정 대상이 아니다"],
        ["", ""],
        ["■ 주의", ""],
        ["", "이 지도는 AI 추출 결과다. `확신도`=low 와 `이미지표기`=Y 는 사람 검증 전까지 확정이 아니다."],
        ["", "사이트 개편으로 selector 는 무효화된다. 게재수가 급감하면 지도를 의심하라."],
    ]
    for row in 안내:
        ws.append(row)
    ws.column_dimensions["A"].width = 16
    ws.column_dimensions["B"].width
    ws.column_dimensions["B"].width = 104
    for c in ws["A"]:
        c.font = Font(bold=True)
    for r in ws.iter_rows(min_col=2, max_col=2):
        for c in r:
            c.alignment = Alignment(wrap_text=True, vertical="top")

    if "Sheet" in wb.sheetnames:
        del wb["Sheet"]
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        wb.save(path)
    except PermissionError:
        # 사용자가 엑셀로 열어 둔 경우. 전수 실행 마지막 단계에서 실패하면 손실이 크므로
        # 대체 파일명으로 저장하고 경로를 돌려준다.
        alt = path.with_name(f"{path.stem}_{datetime.now():%H%M%S}{path.suffix}")
        wb.save(alt)
        return alt
    return path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--input", default="", help="원본 xlsx (제호·URL 보완용, 선택)")
    ap.add_argument("--sheet")
    ap.add_argument("--map", dest="map_json", default="")
    ap.add_argument("--base-date", "--기준일", dest="base_date", default="")
    args = ap.parse_args()

    base = (datetime.strptime(args.base_date, "%Y-%m-%d").date()
            if args.base_date else date.today())
    workdir = Path(args.workdir)

    registry: dict = {}
    if args.input:
        from md_core.registry import load_registry
        override = json.loads(args.map_json) if args.map_json else None
        rows, _, _ = load_registry(args.input, override_map=override, sheet=args.sheet)
        registry = {str(r.get("id")): r for r in rows}

    항목행, 요약행 = build_rows(workdir, registry)
    if not 항목행:
        print(json.dumps({"error": "work/*.json 없음 — run_extract.py 를 먼저 돌려라"},
                         ensure_ascii=False)); sys.exit(1)

    out = workdir / "outputs" / f"위치지도_{base.isoformat()}.xlsx"
    실제 = write_xlsx(out, 항목행, 요약행, base)
    잠김안내 = ""
    if 실제 != out:
        잠김안내 = f"{out.name} 이 열려 있어 {실제.name} 로 저장했다 (엑셀을 닫고 재실행 권장)"
    out = 실제

    방법분포: dict[str, int] = {}
    출처분포: dict[str, int] = {}
    for r in 항목행:
        if r["확정값"]:
            방법분포[r["확인방법"]] = 방법분포.get(r["확인방법"], 0) + 1
            출처분포[r["출처단계"]] = 출처분포.get(r["출처단계"], 0) + 1
    print(json.dumps({
        "위치지도_xlsx": str(out),
        **({"경고": 잠김안내} if 잠김안내 else {}),
        "기준일": base.isoformat(),
        "언론사": len(요약행), "항목행": len(항목행),
        "재사용_가능": sum(1 for r in 요약행 if r["재사용_가능"] == "Y"),
        "이미지표기_보유_매체": [r["언론사_id"] for r in 요약행 if r["이미지표기_항목"]],
        "확인방법_분포": dict(sorted(방법분포.items(), key=lambda x: -x[1])),
        "출처단계_분포": dict(sorted(출처분포.items(), key=lambda x: -x[1])),
        "용도": "다음 회차에 이 파일만 보고 어디를 볼지 판단한다. 재사용_가능=Y 는 override 복사로 끝난다",
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
