#!/usr/bin/env python3
"""compare_registry — 게재값 ↔ 등록정보 대조(diff) + 확인필요 플래그.

사용:
  python compare_registry.py --workdir W --input 등록정보.xlsx [--map '{...}'] [--기준일 YYYY-MM-DD]

산출: W/outputs/verify_{기준일}.csv  (extract CSV + 등록값/대조 컬럼 + 플래그 재계산)
"""
from __future__ import annotations

import argparse
import csv
import re
import json
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from md_core import audit  # noqa: E402
from md_core.registry import COMPARE_FIELDS, load_registry  # noqa: E402
from md_core.registry import compare as compare_field  # noqa: E402
from md_core.schema import apply_flags, verify_columns  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--input", required=True)
    ap.add_argument("--sheet")
    ap.add_argument("--map", dest="map_json", default="")
    ap.add_argument("--base-date", "--기준일", dest="base_date", default="")
    args = ap.parse_args()

    base = (datetime.strptime(args.base_date, "%Y-%m-%d").date()
            if args.base_date else date.today())
    workdir = Path(args.workdir)
    ex_path = workdir / "outputs" / f"extract_{base.isoformat()}.csv"
    if not ex_path.exists():
        cands = sorted((workdir / "outputs").glob("extract_*.csv"))
        if not cands:
            print(json.dumps({"error": f"{ex_path} 없음 — run_extract.py 먼저 실행"},
                             ensure_ascii=False)); sys.exit(1)
        ex_path = cands[-1]

    ex_rows = list(csv.DictReader(ex_path.open(encoding="utf-8-sig")))
    override = json.loads(args.map_json) if args.map_json else None
    reg_rows, mapping, _ = load_registry(args.input, override_map=override, sheet=args.sheet)
    by_id = {str(r.get("id")): r for r in reg_rows}
    대조가능 = [f for f in COMPARE_FIELDS if f in mapping]

    판정_분포: dict[str, dict[str, int]] = {f: {} for f in COMPARE_FIELDS}
    out_rows = []
    for r in ex_rows:
        row = {c: r.get(c, "") for c in verify_columns()}
        reg = by_id.get(str(r.get("언론사_id")), {})
        for f in COMPARE_FIELDS:
            등록값 = reg.get(f, "") if f in 대조가능 else ""
            row[f"{f}_등록값"] = 등록값
            판정 = (compare_field(f, r.get(f"{f}_게재값", ""), 등록값,
                                추출방법=r.get(f"{f}_추출방법", ""))
                  if f in 대조가능 else "")
            row[f"{f}_대조"] = 판정
            if 판정:
                판정_분포[f][판정] = 판정_분포[f].get(판정, 0) + 1
        # merge_llm 이 채운 값을 그대로 이어받되, merge 없이 이 단계만 돌린 경우에도
        # 증빙이 붙도록 여기서 다시 확인한다 (ISS-033). 이 스크립트는 CSV 를 통째로
        # 다시 쓰므로, 한쪽에만 배선하면 다른 쪽 실행에서 컬럼이 비워진다.
        if not (row.get("증빙_경로") or "").strip():
            row["증빙_경로"], row["증빙_누락"] = audit.evidence_paths(
                workdir, str(r.get("언론사_id")))
        apply_flags(row)
        out_rows.append(row)

    out_path = workdir / "outputs" / f"verify_{base.isoformat()}.csv"
    with out_path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=verify_columns())
        w.writeheader()
        w.writerows(out_rows)

    사유_분포: dict[str, int] = {}
    for r in out_rows:
        for part in (r["확인필요_사유"] or "").split(" / "):
            key = re.sub(r"\s*\d+\s*항목", "", part.split(":")[0]).strip()
            if key:
                사유_분포[key] = 사유_분포.get(key, 0) + 1

    print(json.dumps({
        "기준일": base.isoformat(), "extract_csv": str(ex_path), "verify_csv": str(out_path),
        "사이트_수": len(out_rows), "대조가능_항목": 대조가능,
        "대조불가_항목": [f for f in COMPARE_FIELDS if f not in 대조가능],
        "항목별_대조판정": 판정_분포,
        "확인필요": sum(1 for r in out_rows if r["확인필요"] == "Y"),
        "확인필요_사유분포": dict(sorted(사유_분포.items(), key=lambda x: -x[1])),
        "다음": "확인필요 건 판독(override) → attach_shots.py(선택) → finalize.py",
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
