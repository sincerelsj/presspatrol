#!/usr/bin/env python3
"""전수 점검 결과를 **엑셀 한 권**으로 모은다.

왜 한 권인가
------------
지금 산출물이 네 파일로 흩어져 있다 — 점검결과 xlsx, 위치지도 xlsx, Phase 3 대상 xlsx,
그리고 CSV 두 개. 검수자는 매체 하나를 보려고 파일을 옮겨 다녀야 하고, 어느 파일이 최신인지
알 수 없다. 행정 처분 근거로 넘길 때는 **하나의 파일이 하나의 시점**을 가리켜야 한다.

시트 구성
---------
  0 읽는법        이 파일을 어떻게 보는가 · 판정 기준 · 주의사항
  1 요약          전체 집계와 준수율 (분모가 무엇인지 명시)
  2 판정결과      매체 1행. 최종 판정 + 8항목 게재값 + 증빙 경로
  3 확인필요      사람이 봐야 하는 건만 (사유별 정렬)
  4 미준수        과태료 검토 대상만 — 가장 무거운 시트
  5 홈페이지없음   지침 I 로 점검 대상에서 빠진 건 + 화면 성격
  5b I-3 화면성격  접속은 되나 매체가 없는 657건이 실제로 무엇인지
  6 등록정보상이   등록부와 화면이 다른 건
  7 항목위치      값이 화면 어디에 있었는가 (증빙 추적용)
  8 통계          항목별·기관별 집계
  9 실행이력      무엇을 언제 어떻게 돌렸는가 (감사 추적)

원본 파일을 지우지 않는다 — 이 파일은 **모아 보는 용도**이고 원본이 정본이다.
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import sys
from collections import Counter
from pathlib import Path

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from md_core.schema import xlsx_safe  # noqa: E402

머리 = PatternFill("solid", fgColor="1F3864")
머리글꼴 = Font(color="FFFFFF", bold=True, size=10)
강조 = PatternFill("solid", fgColor="FCE4D6")
제목글꼴 = Font(bold=True, size=13)


def 시트(wb, 이름: str, 행들: list[dict], 폭: dict | None = None, 최대: int = 0):
    ws = wb.create_sheet(이름)
    if not 행들:
        ws["A1"] = "해당 건 없음"
        return ws
    if 최대 and len(행들) > 최대:
        행들 = 행들[:최대]
    cols = list(행들[0].keys())
    ws.append(cols)
    for c in ws[1]:
        c.fill, c.font = 머리, 머리글꼴
        c.alignment = Alignment(vertical="center", wrap_text=True)
    for r in 행들:
        ws.append([xlsx_safe(r.get(c, "")) for c in cols])
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for i, c in enumerate(cols, 1):
        w = (폭 or {}).get(c) or min(max(len(str(c)) + 4, 12), 40)
        ws.column_dimensions[get_column_letter(i)].width = w
    return ws


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--base-date", "--기준일", dest="base_date", required=True)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    W = Path(args.workdir)
    D = args.base_date
    out = Path(args.out) if args.out else (
        W / "outputs" / "final" / f"{D}_필요적게재사항_점검결과_통합.xlsx")
    out.parent.mkdir(parents=True, exist_ok=True)

    # I-3 화면성격 판독(2c_I3성격). 지침 I-3 판정을 바꾸지 않고 **왜 그렇게 됐는지**만 붙인다.
    i3 = {}
    for p3 in (W / "work").glob("*.i3.json"):
        try:
            d3 = json.loads(p3.read_text(encoding="utf-8"))
            i3[str(d3.get("언론사_id"))] = d3
        except Exception:  # noqa: BLE001
            pass

    verify = W / "outputs" / f"verify_{D}.csv"
    src = verify if verify.exists() else (W / "outputs" / f"extract_{D}.csv")
    rows = list(csv.DictReader(src.open(encoding="utf-8-sig")))

    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    # ── 0 읽는법 ─────────────────────────────────────────────────────────
    ws = wb.create_sheet("읽는법")
    ws["A1"] = f"2026 인터넷신문 필요적 게재사항 실태점검 — 통합 결과 (기준일 {D})"
    ws["A1"].font = 제목글꼴
    안내 = [
        "",
        "■ 이 파일은 무엇인가",
        "  신문법 제21조가 요구하는 8개 항목을 인터넷신문 홈페이지에 게재했는지 자동 점검한 결과다.",
        f"  대상 {len(rows):,}개 매체. 판정 기준은 배포 지침(판정기준_정본_260813.xlsx) 행 3~20 이다.",
        "",
        "■ 반드시 알아야 할 것",
        "  1) 이 판정은 AI 초안이다. '확인필요' 가 붙은 건은 사람이 검증하기 전까지 확정이 아니다.",
        "  2) 준수율의 분모는 '홈페이지_운영현황 = 있음' 이다. 전체 매체 수가 아니다.",
        "     접속 불가·도메인 소멸 매체는 지침 I 에 따라 '홈페이지없음' 이며 미준수(과태료)가 아니다.",
        "  3) 최종 판정 열은 '준수여부_LLM' 이다. '준수여부_규칙' 은 대조·감사용 스냅샷이다.",
        "  4) 애매한 건은 매체에 유리하게 판정하고 근거와 플래그를 남겼다.",
        "",
        "■ 시트 안내",
        "  요약        전체 집계와 준수율",
        "  판정결과     매체 1행. 최종 판정 + 8항목 게재값 + 증빙 경로",
        "  확인필요     사람이 봐야 하는 건 (사유별)",
        "  미준수       과태료 검토 대상 — 가장 먼저 볼 시트",
        "  홈페이지없음  점검 대상에서 빠진 건 + 화면 성격",
        "  I-3 화면성격  접속은 되나 매체가 없는 657건이 실제로 무엇인지",
        "  등록정보상이  등록부와 화면 표기가 다른 건",
        "  항목위치     값이 화면 어디에 있었는가 (증빙 추적)",
        "  통계        항목별·기관별 집계",
        "  실행이력     무엇을 언제 어떻게 돌렸는가",
        "",
        "■ 8개 필요적 게재사항",
        "  제호 · 등록번호 · 등록연월일 · 발행인 · 편집인 · 발행연월일 · 발행소 주소 · 발행소 전화번호",
    ]
    for i, t in enumerate(안내, start=2):
        ws.cell(row=i, column=1, value=t)
    ws.column_dimensions["A"].width = 110

    # ── 1 요약 ───────────────────────────────────────────────────────────
    운영 = [r for r in rows if r.get("홈페이지_운영현황") == "있음"]
    판정 = Counter(r.get("준수여부_LLM") or r.get("준수여부_8항목") or "" for r in rows)
    준수 = 판정.get("준수", 0)
    미준수 = 판정.get("미준수", 0)
    없음 = 판정.get("홈페이지없음", 0)
    확인 = sum(1 for r in rows if r.get("확인필요") == "Y")
    검색됨 = sum(1 for r in rows if (r.get("검색_판정") or "").strip())
    요약행 = [
        {"구분": "대상 매체", "건수": len(rows), "비고": "입력 등록부 전건"},
        {"구분": "홈페이지 운영현황 · 있음", "건수": len(운영), "비고": "제21조 점검 대상 (준수율 분모)"},
        {"구분": "홈페이지 운영현황 · 없음", "건수": len(rows) - len(운영),
         "비고": "지침 I — 점검 대상 아님. 미준수가 아니다"},
        {"구분": "", "건수": "", "비고": ""},
        {"구분": "준수 (8항목 전부 게재)", "건수": 준수,
         "비고": f"준수율 {준수/max(len(운영),1)*100:.1f}% (분모 {len(운영):,})"},
        {"구분": "미준수", "건수": 미준수, "비고": "과태료 검토 대상"},
        {"구분": "홈페이지없음", "건수": 없음, "비고": "지침 I-3·I-4"},
        {"구분": "", "건수": "", "비고": ""},
        {"구분": "확인필요 (사람 검증 대상)", "건수": 확인,
         "비고": f"전체의 {확인/max(len(rows),1)*100:.1f}%"},
        {"구분": "", "건수": "", "비고": ""},
        {"구분": "포털검색 (지침 I 후속)", "건수": 검색됨,
         "비고": "표본 122건 실측 후 **중단 결정** — 건당 $0.183 에 새 URL 수집률 2.5%"},
        {"구분": "I-3 화면성격 판독", "건수": len(i3),
         "비고": "포털검색 대신 저장본으로 판독($1.65). 검색 값어치 '높음' 은 657건 중 2건"},
    ]
    시트(wb, "요약", 요약행, {"구분": 34, "건수": 12, "비고": 62})

    # ── 2 판정결과 ───────────────────────────────────────────────────────
    항목 = ["제호", "등록번호", "등록연월일", "발행인", "편집인",
           "발행연월일", "발행소 주소", "발행소 전화번호"]
    핵심 = ["언론사_id", "제호_등록", "기관명", "메인_url", "홈페이지_운영현황",
           "운영현황_로직", "접속결과", "준수여부_LLM", "필수8_게재수", "미게재_항목",
           "확인필요", "확인필요_사유", "증빙_경로", "증빙_누락", "판정출처"]

    def 뽑기(r: dict) -> dict:
        d = {k: r.get(k, "") for k in 핵심 if k in r or k == "기관명"}
        for f in 항목:
            d[f] = r.get(f"{f}_게재값", "")
        return d

    시트(wb, "판정결과", [뽑기(r) for r in rows],
        {"제호_등록": 22, "메인_url": 30, "확인필요_사유": 60, "미게재_항목": 30, "증빙_경로": 34})

    # ── 3~5 분할 시트 ────────────────────────────────────────────────────
    확인행 = sorted((r for r in rows if r.get("확인필요") == "Y"),
                  key=lambda r: (r.get("확인필요_사유") or "", int(r.get("언론사_id") or 0)))
    시트(wb, "확인필요", [뽑기(r) for r in 확인행], {"확인필요_사유": 70})
    시트(wb, "미준수", [뽑기(r) for r in rows
                    if (r.get("준수여부_LLM") or r.get("준수여부_8항목")) == "미준수"],
        {"미게재_항목": 36})

    없음행 = []
    for r in rows:
        if (r.get("준수여부_LLM") or r.get("준수여부_8항목")) != "홈페이지없음":
            continue
        d = {k: r.get(k, "") for k in ("언론사_id", "제호_등록", "메인_url", "접속결과",
                                       "HTTP상태", "운영현황_로직", "운영현황_사유")}
        v = i3.get(r.get("언론사_id")) or {}
        d["화면성격"] = v.get("성격", "")
        d["화면설명"] = v.get("설명", "")
        d["검색_판정"] = r.get("검색_판정", "") or "(미수행)"
        d["검색_후보URL"] = r.get("검색_후보URL", "")
        없음행.append(d)
    시트(wb, "홈페이지없음", 없음행,
        {"운영현황_사유": 46, "메인_url": 28, "검색_후보URL": 30,
         "화면성격": 20, "화면설명": 62})

    # ── 5b I-3 화면성격 ──────────────────────────────────────────────────
    i3행 = []
    for rid, v in sorted(i3.items(), key=lambda kv: int(kv[0])):
        i3행.append({
            "언론사_id": rid, "제호_등록": v.get("제호_등록", ""),
            "메인_url": v.get("메인_url", ""),
            "화면성격": v.get("성격", ""), "검색값어치": v.get("검색값어치", ""),
            "확신도": v.get("확신도", ""), "언론사인가": v.get("언론사인가"),
            "화면이름": v.get("화면이름", ""),
            "설명": v.get("설명", ""), "근거": v.get("근거", ""),
            "후속조치": v.get("후속조치", ""),
            "규칙_성격": v.get("규칙_성격", ""),
            "규칙과_일치": "Y" if v.get("규칙과_일치") else "N",
        })
    시트(wb, "I-3 화면성격", i3행,
        {"제호_등록": 20, "메인_url": 28, "화면성격": 20, "화면이름": 22,
         "설명": 70, "근거": 40, "후속조치": 40, "규칙_성격": 20})

    # ── 6 등록정보상이 ───────────────────────────────────────────────────
    대조 = ["등록번호", "발행인", "편집인", "발행소 주소", "발행소 전화번호"]
    상이 = []
    for r in rows:
        차 = [f for f in 대조 if (r.get(f"{f}_대조") or "") in ("불일치", "부분일치")]
        if not 차:
            continue
        # ★ 열은 **모든 행에 대해 같아야** 한다. 행마다 다른 키를 넣으면 시트 작성기가
        #   첫 행의 키만 열로 잡아 나머지 행의 값이 통째로 사라진다.
        d = {"언론사_id": r.get("언론사_id"), "제호_등록": r.get("제호_등록"),
             "상이항목": ", ".join(차)}
        for f in 대조:
            d[f"{f}_대조"] = r.get(f"{f}_대조", "")
            d[f"{f}_등록"] = r.get(f"{f}_등록값", "")
            d[f"{f}_화면"] = r.get(f"{f}_게재값", "")
        상이.append(d)
    시트(wb, "등록정보상이", 상이,
        {"상이항목": 26, "제호_등록": 20,
         **{f"{f}_등록": 24 for f in 대조}, **{f"{f}_화면": 24 for f in 대조}})

    # ── 7 항목위치 ───────────────────────────────────────────────────────
    위치 = []
    lm = W / "outputs" / f"위치지도_{D}.xlsx"
    if lm.exists():
        try:
            b = openpyxl.load_workbook(lm, read_only=True)
            if "항목위치" in b.sheetnames:
                s = b["항목위치"]
                it = s.iter_rows(values_only=True)
                head = list(next(it))
                keep = [h for h in ("언론사_id", "제호", "항목", "확정값", "확인위치",
                                    "CSS_selector", "화면영역", "일치문구", "확신도",
                                    "출처단계", "이미지표기") if h in head]
                idx = [head.index(h) for h in keep]
                for row in it:
                    위치.append({h: row[i] for h, i in zip(keep, idx)})
            b.close()
        except Exception as exc:  # noqa: BLE001
            위치 = [{"오류": f"위치지도 읽기 실패: {exc.__class__.__name__}"}]
    시트(wb, "항목위치", 위치, {"확정값": 30, "CSS_selector": 40, "일치문구": 26}, 최대=200_000)

    # ── 8 통계 ───────────────────────────────────────────────────────────
    통계 = []
    for f in 항목:
        게재 = sum(1 for r in 운영 if (r.get(f"{f}_게재값") or "").strip())
        통계.append({"구분": "항목별 게재", "항목": f, "건수": 게재,
                     "비율": f"{게재/max(len(운영),1)*100:.1f}%",
                     "모수": f"운영중 {len(운영):,}"})
    기관 = Counter(r.get("기관명") or "(미상)" for r in 운영)
    기관준수 = Counter(r.get("기관명") or "(미상)" for r in 운영
                   if (r.get("준수여부_LLM") or "") == "준수")
    for k, v in 기관.most_common(30):
        통계.append({"구분": "기관별", "항목": k, "건수": v,
                     "비율": f"{기관준수.get(k,0)/max(v,1)*100:.1f}%",
                     "모수": f"준수 {기관준수.get(k,0):,}"})
    시트(wb, "통계", 통계, {"항목": 26, "모수": 20})

    # ── 9 실행이력 ───────────────────────────────────────────────────────
    이력 = []
    for name, label in (("llm", "R1 텍스트검토"), ("diag", "R2 비전진단"),
                        ("pubdate", "R3 발행일검증"), ("resolved", "P3 포털검색")):
        ps = glob.glob(str(W / "work" / f"*.{name}.json"))
        비용 = 0.0
        for p in ps:
            try:
                비용 += float(json.load(open(p, encoding="utf-8")).get("비용_USD") or 0)
            except Exception:  # noqa: BLE001
                pass
        이력.append({"단계": label, "처리건수": len(ps),
                     "API비용_USD": round(비용, 4) if 비용 else 0,
                     "비고": "토큰 기준. 웹검색 서버도구 요금은 별도" if name == "resolved" else ""})
    이력.append({"단계": "증빙 스크린샷", "처리건수": len(glob.glob(str(W / "shots" / "*_top.png"))),
                "API비용_USD": 0, "비고": "상단·하단·기사 원본"})
    이력.append({"단계": "근거위치(evidence)", "처리건수": len(glob.glob(str(W / "evidence" / "*.json"))),
                "API비용_USD": 0, "비고": "값이 DOM 어디에 있었는가"})
    시트(wb, "실행이력", 이력, {"단계": 24, "비고": 46})

    wb.save(out)
    print(json.dumps({
        "파일": str(out), "크기_MB": round(os.path.getsize(out) / 1024 / 1024, 1),
        "원본": str(src.name), "시트": wb.sheetnames,
        "대상": len(rows), "운영중": len(운영),
        "준수": 준수, "미준수": 미준수, "홈페이지없음": 없음,
        "준수율": f"{준수/max(len(운영),1)*100:.1f}%",
        "확인필요": 확인,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
