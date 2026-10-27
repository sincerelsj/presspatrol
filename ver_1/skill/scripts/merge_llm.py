#!/usr/bin/env python3
"""merge_llm — LLM 종합 판정을 CSV 에 병합한다. `준수여부_LLM` 이 최종 정답이다.

판정 근거
---------
LLM 은 규칙 1차결과를 **함께 받아** 항목마다 판단을 내렸다(규칙확인/규칙수정/신규발견/
규칙기각/양쪽없음). 따라서 LLM findings 는 규칙을 포함한 종합 결과이며, 이것을 최종 판정으로
쓴다. 규칙 기준 `준수여부_8항목` 은 대조·감사용으로 남긴다.

출처 우선순위 (뒤로 갈수록 우선)
    규칙  →  LLM 텍스트 검토  →  비전 진단(이미지·DOM)

게이트A 를 통과해 LLM 을 호출하지 않은 건은 규칙 결과를 승계하고 `판정출처=규칙(게이트통과)`
로 표기한다 — 게이트가 '규칙이 8항목을 확신했으니 검토 불필요' 로 판정한 건이다.

주의: LLM 판정은 규칙보다 **더 엄격해질 수 있다.** `규칙기각` 은 규칙이 근거 없이 낸 값을
버리는 것이므로 게재수가 줄어든다. 허위 양성 제거이므로 방향은 맞지만, 준수율이 내려간다.

사용
----
  python merge_llm.py --workdir W --기준일 2026-07-31
  (run_extract → llm_extract → [diagnose_site] → **merge_llm** → compare_registry → finalize)
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from md_core import audit  # noqa: E402
from md_core.fields import FIELDS, REQUIRED_FIELDS, apply_cross_rules  # noqa: E402
from md_core.schema import apply_flags, extract_columns, 판단근거_요약  # noqa: E402


def load(p: Path) -> dict:
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def effective(workdir: Path, rid: str, base: date) -> tuple[dict, str, dict]:
    """→ (항목별 최종 findings, 판정출처, 판단분포)

    LLM 이 없으면 규칙 결과를 그대로 승계한다(게이트A 통과 건).
    """
    rule = (load(workdir / "work" / f"{rid}.json").get("findings") or {})
    llm = (load(workdir / "work" / f"{rid}.llm.json").get("검증후_findings") or {})
    diag = (load(workdir / "work" / f"{rid}.diag.json").get("검증후_정정") or {})
    pd = load(workdir / "work" / f"{rid}.pubdate.json")

    # 발행연월일은 전용 검증(verify_pubdate)이 최우선이다 — 지침 R7 의 세 갈래 인정과
    # '투데이' 제외는 문맥 판단이라 규칙만으로 확정할 수 없다.
    # ★ `판단근거` 를 반드시 실어 나른다. 이 값이 '기사 업로드 날짜' 면 지침 R10 비고에 따라
    #   등록연월일로 전이되면 안 되고, apply_cross_rules 가 그 문자열을 보고 막는다.
    pubdate = None
    if pd:
        v = (pd.get("발행연월일_게재") or "").strip()
        pubdate = {"값": v, "근거": pd.get("게재_근거", ""),
                   "위치": ("상단" if pd.get("게재_위치") == "상단"
                          else "하단" if pd.get("게재_위치") == "하단"
                          else "기타" if v else "없음"),
                   "확신도": "high" if v else "low",
                   "추출방법": f"발행일검증({pd.get('확인경로','')})",
                   "판단근거": pd.get("판단근거", ""),
                   "판단": "전용검증", "판정출처": "발행일검증"}

    if not llm and not diag and not pubdate:
        return dict(rule), "규칙(게이트통과)", {}

    # 폴백 사슬은 항목마다 diag → (llm 있으면 llm, 없으면 rule) 이다.
    #
    # ★ llm 의 '빈 값' 은 규칙기각/양쪽없음이라는 **판단** 이므로 규칙으로 되돌리지 않는다.
    #   그러나 llm.json 자체가 없는 건(게이트A 에서 바로 진단필요로 간 건)은 판단이 없으므로
    #   규칙 값이 유효하다. 이 구분을 놓쳐 30876 이 8항목 → 2항목으로 폭락한 적이 있다.
    llm_있음 = bool(llm)
    out: dict = {}
    분포: dict[str, int] = {}
    for f in FIELDS:
        if f == "발행연월일" and pubdate is not None:
            out[f] = pubdate
            분포["발행일전용검증"] = 분포.get("발행일전용검증", 0) + 1
            continue
        # 비전 진단이 가장 나중·가장 많은 증거(이미지·DOM)를 봤다 → 최우선
        if f in diag and (diag[f].get("값") or "").strip():
            out[f] = {**diag[f], "판정출처": "비전진단"}
            분포["비전정정"] = 분포.get("비전정정", 0) + 1
            continue
        if llm_있음:
            item = llm.get(f) or {}
            판단 = item.get("판단") or ""
            if 판단:
                분포[판단] = 분포.get(판단, 0) + 1
            if (item.get("값") or "").strip():
                out[f] = {**item, "판정출처": "LLM검토"}
            else:
                out[f] = {"값": "", "근거": item.get("근거", ""), "위치": "없음",
                          "확신도": item.get("확신도", "low"),
                          "추출방법": item.get("추출방법", ""), "판단": 판단,
                          "판정출처": "LLM검토"}
        else:
            out[f] = {**(rule.get(f) or {}), "판정출처": "규칙(LLM 미호출)"}
            분포["규칙승계"] = 분포.get("규칙승계", 0) + 1
    출처 = ("비전진단+LLM검토" if (diag and llm_있음)
          else "비전진단+규칙" if diag else "LLM검토")
    # ★ 교차 준수 규칙은 **최종 findings** 에 적용해야 한다 (지침 R7·R10·R12·R14 비고).
    #   규칙 추출 직후에도 한 번 걸지만, LLM 이 값을 기각하거나 새로 찾으면 전제가 달라진다.
    #   여기서 다시 걸지 않으면 지침상 존재할 수 없는 조합이 최종 산출물에 남는다.
    return apply_cross_rules(out, base_date=base), 출처, 분포


def 차이요약(rule: dict, eff: dict) -> str:
    """규칙 대비 무엇이 늘고 줄었는지 — 검수자가 가장 먼저 보는 정보."""
    변화 = []
    for f in REQUIRED_FIELDS:
        r = ((rule.get(f) or {}).get("값") or "").strip()
        e = ((eff.get(f) or {}).get("값") or "").strip()
        if not r and e:
            변화.append(f"+{f}")
        elif r and not e:
            판단 = (eff.get(f) or {}).get("판단") or "제거"
            변화.append(f"-{f}({판단})")
        elif r and e and "".join(r.split()) != "".join(e.split()):
            변화.append(f"~{f}")
    return ", ".join(변화)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--base-date", "--기준일", dest="base_date", default="")
    args = ap.parse_args()

    base = (datetime.strptime(args.base_date, "%Y-%m-%d").date()
            if args.base_date else date.today())
    workdir = Path(args.workdir)
    csv_path = workdir / "outputs" / f"extract_{base.isoformat()}.csv"
    if not csv_path.exists():
        print(json.dumps({"error": f"{csv_path} 없음 — run_extract 를 먼저 돌려라"},
                         ensure_ascii=False)); sys.exit(1)

    with csv_path.open(encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    출처분포, 판정분포, 변화건 = {}, {}, []
    for row in rows:
        rid = str(row.get("언론사_id"))
        rule = (load(workdir / "work" / f"{rid}.json").get("findings") or {})
        eff, 출처, 분포 = effective(workdir, rid, base)

        게재 = [f for f in REQUIRED_FIELDS
              if ((eff.get(f) or {}).get("값") or "").strip() not in ("", "해당사항 없음")]
        미게재 = [f for f in REQUIRED_FIELDS if f not in 게재]
        접속불가 = row.get("접속결과") not in ("가능", "", None)

        row["필수8_게재수_LLM"] = len(게재)
        row["미게재_항목_LLM"] = ", ".join(미게재)
        row["판단근거"] = 판단근거_요약(eff)[:400]
        파킹 = (row.get("파킹의심") or "").strip() == "Y"
        # 지침 I — 홈페이지 '없음' 이면 필요적 게재사항 점검 대상이 아니다.
        # 파킹·접속불가·검색미발견이 전부 여기로 흡수된다(종전 `판정불가` 폐기).
        운영없음 = (파킹 or 접속불가
                or (row.get("홈페이지_운영현황") or "").strip() == "없음"
                or (row.get("검색_판정") or "") == "제호일치_미발견")
        if 운영없음:
            row["준수여부_LLM"] = "홈페이지없음"
        else:
            row["준수여부_LLM"] = "준수" if len(게재) == len(REQUIRED_FIELDS) else "미준수"
        row["판정출처"] = 출처
        row["LLM_판단요약"] = ", ".join(f"{k} {v}" for k, v in
                                   sorted(분포.items(), key=lambda x: -x[1])) or "-"
        # 발행연월일 최신성 — 게재(21조)와 분리해 운영 판단에 쓴다
        pd_doc = load(workdir / "work" / f"{rid}.pubdate.json")
        row["발행연월일_실제"] = pd_doc.get("발행연월일_실제", "")
        row["발행연월일_경과일"] = ("" if pd_doc.get("경과일") is None
                             else pd_doc.get("경과일"))
        row["발행연월일_확인경로"] = pd_doc.get("확인경로", "")
        # 이미지(로고) 표기 항목 — 게재로 인정하되 확인필요로 남긴다
        dg = load(workdir / "work" / f"{rid}.diag.json")
        row["이미지표기_항목"] = ", ".join(dg.get("이미지표기_항목") or [])
        # 비전 진단의 원인 진단을 산출물로 옮긴다. override notes 에만 두면 검수자가 못 본다
        # (pilot3 에서 비전이 도메인 만료를 진단했는데 사유에 '미검출 8항목' 만 남았다).
        row["진단_원인"] = ((dg.get("원응답") or {}).get("cause") or "")[:300]
        # 차이 비교는 '규칙 단독' 기준이어야 한다 — override(수동) 값을 규칙에 포함하면
        # LLM·비전·발행일검증이 만든 값을 규칙이 원래 갖고 있던 것처럼 보여 차이가 누락된다.
        규칙단독 = {f: v for f, v in (rule or {}).items()
                if (v or {}).get("추출방법") != "수동"}
        차 = 차이요약(규칙단독, eff)
        row["규칙_LLM_차이"] = 차

        # 항목별 게재값·위치·확신도·추출방법·근거를 LLM 최종값으로 갱신한다.
        # 갱신하지 않으면 검증시트가 자기모순이 된다 — 준수여부_LLM=준수 인데
        # 발행연월일_게재값 칸이 비어 있고 확인필요 사유에 '미검출' 이 남는다(30874 실측).
        # 규칙 기준 수치는 미리 보관해 되돌린다(대조·감사용).
        # 규칙 단독 기준은 **run_extract 가 override 적용 전에 이미 확정**했다.
        # 여기서 다시 세면 안 된다 — override 가 적용된 findings 를 '수동 제외' 로 세면
        # 재실행마다 값이 내려간다(pilot3: 154 → 130 → …). 읽기만 한다.
        if 운영없음 and (row.get("준수여부_규칙") or "") != "준수":
            row["준수여부_규칙"] = "홈페이지없음"
        if not (row.get("필수8_게재수_규칙") or "").strip():
            # run_extract 가 쓰기 전 버전의 CSV 를 병합하는 경우의 안전망
            규칙_원본 = load(workdir / "work" / f"{rid}.json").get("findings") or {}
            규칙게재 = [f for f in REQUIRED_FIELDS
                    if ((규칙_원본.get(f) or {}).get("값") or "").strip() not in ("", "해당사항 없음")]
            row["필수8_게재수_규칙"] = len(규칙게재)
            row["준수여부_규칙"] = ("홈페이지없음" if 운영없음
                              else "준수" if len(규칙게재) == len(REQUIRED_FIELDS)
                              else "미준수")
        for f in FIELDS:
            it = eff.get(f) or {}
            row[f"{f}_게재값"] = it.get("값", "")
            row[f"{f}_위치"] = it.get("위치", "없음")
            row[f"{f}_확신도"] = it.get("확신도", "")
            row[f"{f}_추출방법"] = it.get("추출방법", "")
            근거 = it.get("근거", "")
            if 근거:
                row[f"{f}_근거"] = 근거[:180]
        # 증빙 파일 경로를 판정 행에 붙인다 (ISS-033). apply_flags 앞에 둬야 핵심 증빙
        # 누락이 확인필요 사유에 반영된다 — 증빙 없는 판정은 처분 근거가 되지 못한다.
        row["증빙_경로"], row["증빙_누락"] = audit.evidence_paths(workdir, rid)
        apply_flags(row)          # LLM 값 기준으로 확인필요 사유·준수여부 재도출

        출처분포[출처] = 출처분포.get(출처, 0) + 1
        판정분포[row["준수여부_LLM"]] = 판정분포.get(row["준수여부_LLM"], 0) + 1
        if 차:
            변화건.append({"id": rid, "제호": row.get("제호_등록", ""),
                         "규칙": row.get("필수8_게재수_규칙"), "LLM": len(게재), "차이": 차})

        audit.record(workdir, rid, "6_대조", "LLM판정병합", {
            "준수여부_규칙": row.get("준수여부_8항목"),
            "준수여부_LLM": row["준수여부_LLM"],
            "필수8_규칙": row.get("필수8_게재수_규칙"), "필수8_LLM": len(게재),
            "판정출처": 출처, "판단분포": 분포, "규칙_LLM_차이": 차,
            "미게재_LLM": 미게재,
            "판단": "LLM 은 규칙 1차결과를 함께 받아 판단했으므로 종합 판정으로 쓴다",
        }, 제호=row.get("제호_등록"), 기준일=base.isoformat())

    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=extract_columns(), extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    # ★ 규칙 기준 수치는 반드시 **동결 컬럼**(준수여부_규칙 / 필수8_게재수_규칙)에서 읽는다.
    # 준수여부_8항목·필수8_게재수 는 항목값을 LLM 최종값으로 갱신한 뒤 apply_flags 가
    # 재계산하므로 LLM 값과 같아진다 — 그것을 '규칙 기준' 으로 보고하면 수치가 부풀려진다.
    # pilot3 에서 실제로 준수 10건을 19건으로, 필수8 합계 147을 171로 잘못 보고했다(ISS-020 재발).
    규칙준수 = sum(1 for r in rows if r.get("준수여부_규칙") == "준수")
    llm준수 = sum(1 for r in rows if r.get("준수여부_LLM") == "준수")
    print(json.dumps({
        "기준일": base.isoformat(), "대상": len(rows),
        "준수_규칙기준": 규칙준수, "준수_LLM기준": llm준수,
        "증감": llm준수 - 규칙준수,
        "필수8_합계_규칙": sum(int(r.get("필수8_게재수_규칙") or 0) for r in rows),
        "필수8_합계_LLM": sum(int(r.get("필수8_게재수_LLM") or 0) for r in rows),
        "판정출처_분포": 출처분포,
        "준수여부_LLM_분포": 판정분포,
        "규칙과_달라진_건": 변화건,
        "extract_csv": str(csv_path),
        "다음": "compare_registry.py → finalize.py (준수여부_LLM 이 최종 판정)",
        "주의": "LLM 판정은 규칙보다 엄격할 수 있다 — 규칙기각(-)은 근거 없는 값을 버린 것이다",
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
