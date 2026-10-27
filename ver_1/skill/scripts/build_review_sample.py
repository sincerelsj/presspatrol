#!/usr/bin/env python3
"""사람이 직접 대조할 검증 샘플 꾸러미를 만든다.

왜 만드나
---------
전수 13,574건은 사람이 다 볼 수 없다. 그래서 **판정 경로가 서로 다른 130건**을 골라
사람이 화면을 직접 보고 시스템 판정과 맞는지 확인한다. 여기서 어긋나는 비율이
전수 판정의 신뢰도를 가늠하는 근거가 된다.

무작위로 130건을 뽑으면 준수(54%)와 홈페이지없음(33%)이 대부분을 차지해
드물지만 중요한 경로(이름항목대체·교차규칙·기사상세 날짜·비전진단)를 한 건도 못 본다.
그래서 **층화 추출**한다 — 각 판정 경로에서 정해진 수만큼 뽑는다.

무엇이 들어가나
---------------
  00_검증안내.docx      어떻게 검증하는가 · 판정 기준 요약
  01_검증표.xlsx        130행. 왼쪽=등록부·시스템 판정, 오른쪽=사람이 채울 칸
  증빙/{id}_{제호}/     매체별 폴더 — 상단·하단 원본, 주석본, 기사 화면

주석본은 시스템이 "이 값을 여기서 찾았다" 고 표시한 이미지다. 사람이 그 표시가 맞는지
바로 볼 수 있다. 원본도 함께 두는 이유는 주석이 없는 자리를 확인해야 하기 때문이다.
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import re
import shutil
import sys
from collections import Counter
from pathlib import Path

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from md_core.schema import xlsx_safe  # noqa: E402

항목 = ["제호", "등록번호", "등록연월일", "발행인", "편집인",
       "발행연월일", "발행소 주소", "발행소 전화번호"]
대조항목 = ["등록번호", "발행인", "편집인", "발행소 주소", "발행소 전화번호"]


def 안전(name: str) -> str:
    """윈도 파일명으로 쓸 수 없는 글자를 없앤다."""
    return re.sub(r'[\\/:*?"<>|]', "_", (name or "").strip())[:28] or "무제"


def 층화(rows: list[dict], 목표: int, seed: int) -> tuple[list[dict], list[dict]]:
    """판정 경로별로 나눠 뽑는다. → (표본, 층별 집계)

    앞선 층에서 이미 뽑힌 건은 뒤 층에서 제외한다 — 같은 매체를 두 번 검증시키면
    표본 수만 줄어든다.
    """
    R = random.Random(seed)
    운영 = [r for r in rows if r.get("홈페이지_운영현황") == "있음"]

    def 게재수(r):
        try:
            return int(r.get("필수8_게재수") or 0)
        except ValueError:
            return 0

    층 = [
        # (이름, 조건, 뽑을 수, 왜 보는가)
        ("준수 · 규칙만으로 판정", lambda r: r in 운영 and r.get("준수여부_LLM") == "준수"
         and r.get("판정출처", "").startswith("규칙"), 20,
         "가장 흔한 경로. 규칙이 8항목을 다 찾은 건이 정말 다 있는가"),
        ("준수 · AI 검토 거침", lambda r: r in 운영 and r.get("준수여부_LLM") == "준수"
         and r.get("판정출처") == "LLM검토", 18,
         "규칙이 놓친 것을 AI 가 찾아낸 건. 근거 없이 만들어낸 값은 없는가"),
        ("미준수 · 1~2항목 부족", lambda r: r in 운영 and r.get("준수여부_LLM") == "미준수"
         and 6 <= 게재수(r) <= 7, 14,
         "아깝게 미준수인 건. 놓친 항목이 정말 화면에 없는가"),
        ("미준수 · 절반 이하", lambda r: r in 운영 and r.get("준수여부_LLM") == "미준수"
         and 게재수(r) <= 4, 12,
         "대부분을 못 찾은 건. 수집 자체가 잘못된 것은 아닌가"),
        ("이름항목 대체 적용", lambda r: "R13-2" in (r.get("판단근거") or "")
         or "R15-2" in (r.get("판단근거") or ""), 8,
         "청소년보호책임자 등으로 발행인·편집인을 인정한 건 (지침 R13-2/R15-2)"),
        ("교차규칙 적용", lambda r: "비고" in (r.get("판단근거") or ""), 8,
         "등록연월일↔발행연월일, 발행인↔편집인 전이를 적용한 건"),
        ("발행일 · 기사상세 확인", lambda r: (r.get("발행연월일_확인경로") or "") == "기사상세", 8,
         "메인에 날짜가 없어 기사를 열어 확인한 건 (지침 R7-3)"),
        ("화면 판독(비전) 거침", lambda r: "비전진단" in (r.get("판정출처") or ""), 6,
         "텍스트로 못 읽어 화면 이미지로 판독한 건"),
        ("등록정보와 화면 상이", lambda r: any(
            (r.get(f"{f}_대조") or "") in ("불일치", "부분일치") for f in 대조항목), 10,
         "등록부와 화면 표기가 다른 건. 어느 쪽이 맞는가"),
        ("홈페이지없음 · I-3", lambda r: r.get("운영현황_로직") == "I-3", 10,
         "접속은 되나 해당 매체 홈페이지가 아니라고 본 건"),
        ("홈페이지없음 · I-4", lambda r: r.get("운영현황_로직") == "I-4", 8,
         "접속이 되지 않는다고 본 건"),
        ("매체명 상이하나 통과(I-2)", lambda r: r.get("운영현황_로직") == "I-2", 8,
         "제호가 달라도 뉴스가 있어 점검 대상으로 본 건"),
    ]

    뽑힘: list[dict] = []
    쓴id: set[str] = set()
    집계 = []
    for 이름, 조건, n, 이유 in 층:
        후보 = [r for r in rows if r["언론사_id"] not in 쓴id and 조건(r)]
        # 증빙이 있는 건을 앞세운다 — 사람이 볼 화면이 없으면 검증이 안 된다
        후보.sort(key=lambda r: (0 if r.get("증빙_경로") else 1, R.random()))
        고름 = 후보[:n]
        for r in 고름:
            r = dict(r)
            r["_층"] = 이름
            r["_이유"] = 이유
            뽑힘.append(r)
            쓴id.add(r["언론사_id"])
        집계.append({"층": 이름, "모집단": len(후보), "표본": len(고름), "왜 보는가": 이유})

    # 목표에 모자라면 운영중 전체에서 무작위로 채운다
    if len(뽑힘) < 목표:
        여유 = [r for r in rows if r["언론사_id"] not in 쓴id]
        R.shuffle(여유)
        for r in 여유[: 목표 - len(뽑힘)]:
            r = dict(r); r["_층"] = "무작위 보충"; r["_이유"] = "층화에서 남은 자리"
            뽑힘.append(r); 쓴id.add(r["언론사_id"])
        집계.append({"층": "무작위 보충", "모집단": len(여유),
                    "표본": 목표 - sum(x["표본"] for x in 집계), "왜 보는가": "층화에서 남은 자리"})
    return 뽑힘[:목표], 집계


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--base-date", "--기준일", dest="base_date", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--n", type=int, default=130)
    ap.add_argument("--seed", type=int, default=20260911)
    ap.add_argument("--no-copy", action="store_true", help="이미지 복사를 건너뛴다")
    args = ap.parse_args()

    W = Path(args.workdir)
    D = args.base_date
    OUT = Path(args.out)
    (OUT / "증빙").mkdir(parents=True, exist_ok=True)

    src = W / "outputs" / f"verify_{D}.csv"
    rows = list(csv.DictReader(src.open(encoding="utf-8-sig")))
    표본, 집계 = 층화(rows, args.n, args.seed)

    i3 = {}
    for p in (W / "work").glob("*.i3.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            i3[str(d.get("언론사_id"))] = d
        except Exception:  # noqa: BLE001
            pass

    # ── 증빙 복사 ────────────────────────────────────────────────────────
    shots = W / "shots"
    복사 = Counter()
    for r in 표본:
        rid = r["언론사_id"]
        폴더 = OUT / "증빙" / f"{rid}_{안전(r.get('제호_등록'))}"
        폴더.mkdir(parents=True, exist_ok=True)
        r["_증빙폴더"] = 폴더.name
        if args.no_copy:
            continue
        for 원, 새 in (("_top.png", "1_상단_원본.png"), ("_footer.png", "2_하단_원본.png"),
                      ("_top_annot.png", "3_상단_주석.png"),
                      ("_footer_annot.png", "4_하단_주석.png"),
                      ("_article.png", "5_기사화면.png")):
            p = shots / f"{rid}{원}"
            if p.exists():
                shutil.copy2(p, 폴더 / 새)
                복사[새] += 1
        n = len(list(폴더.glob("*.png")))
        r["_증빙수"] = n
        if n == 0:
            (폴더 / "증빙없음.txt").write_text(
                "이 매체는 화면을 확보하지 못했습니다.\n"
                f"사유: {(r.get('비고') or r.get('운영현황_사유') or '')[:200]}\n",
                encoding="utf-8")

    # ── 검증표 ───────────────────────────────────────────────────────────
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    머리 = PatternFill("solid", fgColor="1F3864")
    머리글꼴 = Font(color="FFFFFF", bold=True, size=9)
    입력머리 = PatternFill("solid", fgColor="C55A11")
    입력칸 = PatternFill("solid", fgColor="FFF2E6")

    def 시트(이름, 행들, 폭=None, 입력열=(), size=9):
        ws = wb.create_sheet(이름)
        if not 행들:
            ws["A1"] = "없음"; return ws
        cols = list(행들[0].keys())
        ws.append(cols)
        for i, c in enumerate(ws[1], 1):
            c.fill = 입력머리 if cols[i - 1] in 입력열 else 머리
            c.font = 머리글꼴
            c.alignment = Alignment(vertical="center", wrap_text=True, horizontal="center")
        for r in 행들:
            ws.append([xlsx_safe(r.get(c, "")) for c in cols])
        for i, c in enumerate(cols, 1):
            L = get_column_letter(i)
            ws.column_dimensions[L].width = (폭 or {}).get(c, min(max(len(c) + 3, 11), 34))
            if c in 입력열:
                for row in range(2, len(행들) + 2):
                    ws[f"{L}{row}"].fill = 입력칸
        ws.freeze_panes = "D2"
        ws.auto_filter.ref = ws.dimensions
        ws.row_dimensions[1].height = 32
        return ws

    # ── 검증표: 8개 항목마다 **시스템 값 바로 옆에 확인 칸** ────────────────
    # 사람이 캡처를 보며 항목 하나씩 대조하므로, 눈이 좌우로만 움직이도록 짝지어 둔다.
    항목확인 = [f"✔{f}" for f in 항목]
    종합확인 = ["✔홈페이지운영", "✔준수여부", "✔게재수", "✔시스템과일치", "✔의견"]
    입력열 = 항목확인 + 종합확인

    행들 = []
    for i, r in enumerate(표본, 1):
        rid = r["언론사_id"]
        d = {
            "번호": i, "언론사_id": rid, "제호(등록)": r.get("제호_등록", ""),
            "증빙폴더": r.get("_증빙폴더", ""), "장수": r.get("_증빙수", 0),
            "등록URL": r.get("메인_url", ""),
            "운영": r.get("홈페이지_운영현황", ""), "분기": r.get("운영현황_로직", ""),
            "판정": r.get("준수여부_LLM", ""), "게재수": r.get("필수8_게재수", ""),
        }
        for f in 항목:                      # 시스템 값 → 확인 칸 순서로 짝짓는다
            d[f] = r.get(f"{f}_게재값", "")
            d[f"✔{f}"] = ""
        for c in 종합확인:
            d[c] = ""
        d["미게재항목"] = r.get("미게재_항목", "")
        d["검증층"] = r["_층"]
        d["판정출처"] = r.get("판정출처", "")
        d["판단근거"] = (r.get("판단근거") or "")[:300]
        d["확인필요사유"] = (r.get("확인필요_사유") or "")[:300]
        v = i3.get(rid) or {}
        d["화면성격"] = v.get("성격", "")
        d["화면설명"] = v.get("설명", "")
        행들.append(d)

    폭 = {"번호": 5, "언론사_id": 10, "제호(등록)": 18, "증빙폴더": 26, "장수": 6,
         "등록URL": 24, "운영": 7, "분기": 6, "판정": 11, "게재수": 7,
         "미게재항목": 24, "검증층": 20, "판정출처": 14,
         "판단근거": 40, "확인필요사유": 40, "화면성격": 18, "화면설명": 46,
         **{f: 18 for f in 항목}, **{f"✔{f}": 7 for f in 항목},
         "✔홈페이지운영": 12, "✔준수여부": 12, "✔게재수": 8,
         "✔시스템과일치": 12, "✔의견": 36}
    ws = 시트("검증표", 행들, 폭, 입력열=입력열)

    # 증빙 폴더로 바로 가는 링크 — 파일 탐색기가 열린다
    cols = list(행들[0].keys())
    ci = cols.index("증빙폴더") + 1
    for i, r in enumerate(표본, start=2):
        c = ws.cell(row=i, column=ci)
        c.hyperlink = f"증빙/{r.get('_증빙폴더', '')}"
        c.font = Font(color="0563C1", underline="single", size=9)

    # O/X 드롭다운 — 타이핑을 줄인다
    from openpyxl.worksheet.datavalidation import DataValidation
    ox = DataValidation(type="list", formula1='"O,X,△"', allow_blank=True)
    ox.error = "O(시스템 값이 맞음) / X(틀림) / △(판단 보류) 중에서 고르십시오"
    ox.prompt = "O 맞음 · X 틀림 · △ 판단 보류"
    ws.add_data_validation(ox)
    for c in 항목확인 + ["✔시스템과일치"]:
        L = get_column_letter(cols.index(c) + 1)
        ox.add(f"{L}2:{L}{len(행들) + 1}")
    운영dv = DataValidation(type="list", formula1='"있음,없음"', allow_blank=True)
    판정dv = DataValidation(type="list", formula1='"준수,미준수,홈페이지없음"', allow_blank=True)
    # 게재수도 목록으로 — 0~8 밖의 값이 들어가면 집계가 깨진다
    수dv = DataValidation(type="list", formula1='"0,1,2,3,4,5,6,7,8"', allow_blank=True)
    for dv in (운영dv, 판정dv, 수dv):
        ws.add_data_validation(dv)
    for dv, c in ((운영dv, "✔홈페이지운영"), (판정dv, "✔준수여부"), (수dv, "✔게재수")):
        L = get_column_letter(cols.index(c) + 1)
        dv.add(f"{L}2:{L}{len(행들) + 1}")

    # 목록에 없는 값을 막는다 — 드롭다운을 두고도 직접 타이핑하면 O/o/ＯK 가 섞인다
    for dv in ws.data_validations.dataValidation:
        dv.showDropDown = False      # False 가 '드롭다운 화살표를 보인다' 는 뜻이다
        dv.allow_blank = True
        dv.showErrorMessage = True
        dv.errorStyle = "stop"
        dv.errorTitle = "목록에서 고르십시오"
        dv.promptTitle = "선택"
        dv.showInputMessage = True

    # 항목 확인 칸까지 화면에 남도록 고정 — 제호·폴더를 보면서 오른쪽을 채운다
    ws.freeze_panes = "E2"

    시트("표본설계", 집계, {"층": 26, "왜 보는가": 62})

    안내 = [
        {"항목": "검증 대상", "내용": f"{len(표본)}건 (전수 {len(rows):,}건에서 층화 추출)"},
        {"항목": "무엇을 하나",
         "내용": "증빙 폴더의 화면을 보고 시스템 판정이 맞는지 확인합니다."},
        {"항목": "채울 칸",
         "내용": "주황색 열 5개 (✔홈페이지운영 / ✔준수여부 / ✔틀린항목 / ✔시스템과일치 / ✔의견)"},
        {"항목": "증빙 폴더 구성",
         "내용": "1_상단_원본 · 2_하단_원본 · 3_상단_주석 · 4_하단_주석 · 5_기사화면"},
        {"항목": "주석 이미지란",
         "내용": "시스템이 '이 값을 여기서 찾았다' 고 표시한 화면입니다. 표시가 맞는지 보십시오."},
        {"항목": "원본도 보는 이유",
         "내용": "주석이 없는 자리에 실제로 값이 있는지 확인해야 합니다."},
        {"항목": "준수 판정 기준", "내용": "8개 항목이 모두 화면에 있으면 준수입니다."},
        {"항목": "홈페이지없음", "내용": "접속이 안 되거나 해당 매체 화면이 아닌 경우로, 미준수가 아닙니다."},
        {"항목": "발행연월일", "내용": "명칭과 무관하게 날짜 형태면 인정하며, 기사를 클릭했을 때의 업로드 날짜도 인정합니다."},
        {"항목": "발행인·편집인", "내용": "이름이 적힌 항목이 하나라도 있으면 인정합니다(청소년보호책임자 등 포함)."},
    ]
    시트("검증안내", 안내, {"항목": 20, "내용": 86})

    xlsx = OUT / "01_검증표.xlsx"
    wb.save(xlsx)

    요약 = {
        "폴더": str(OUT), "표본": len(표본),
        "검증표": xlsx.name,
        "층별": {x["층"]: x["표본"] for x in 집계},
        "증빙_복사": dict(복사),
        "증빙_없는_매체": sum(1 for r in 표본 if not r.get("_증빙수")),
        "판정_분포": dict(Counter(r.get("준수여부_LLM") for r in 표본).most_common()),
    }
    (OUT / "표본구성.json").write_text(
        json.dumps(요약, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(요약, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
