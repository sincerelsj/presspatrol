#!/usr/bin/env python3
"""inspect_registry — 등록정보 xlsx 의 컬럼 자동 매핑 결과를 확인한다 (전수 실행 전 필수).

사용:
  python inspect_registry.py 등록정보.xlsx
  python inspect_registry.py 등록정보.xlsx --sheet "Sheet1" --map '{"발행인":"대표자명"}'
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from md_core.registry import ALIASES, COMPARE_FIELDS, load_registry  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("--sheet")
    ap.add_argument("--map", dest="map_json", default="", help='{"표준키":"실제헤더"} 강제 매핑')
    ap.add_argument("--sample", type=int, default=3)
    args = ap.parse_args()

    override = json.loads(args.map_json) if args.map_json else None
    rows, mapping, headers = load_registry(args.input, override_map=override, sheet=args.sheet)

    missing = [k for k in ALIASES if k not in mapping]
    out = {
        "파일": args.input, "행수": len(rows), "헤더": headers,
        "mapping": mapping,
        "미매핑_표준키": missing,
        "unmapped_헤더": [h for h in headers if h and h not in mapping.values()],
        "대조가능_항목": [f for f in COMPARE_FIELDS if f in mapping],
        "대조불가_항목": [f for f in COMPARE_FIELDS if f not in mapping],
        "sample": [{k: v for k, v in r.items() if k != "_raw"} for r in rows[: args.sample]],
    }
    경고 = []
    if "url" not in mapping:
        경고.append("URL 컬럼 미매핑 — --map '{\"url\":\"실제헤더\"}' 로 지정해야 실행 불가")
    if "id" not in mapping:
        경고.append("식별자 컬럼 미매핑 — ROW00001 형태 자동 부여됨")
    if out["대조불가_항목"]:
        경고.append(f"대조 불가 항목 있음 {out['대조불가_항목']} → 게재 여부만 점검, 일치 여부는 공란")
    out["경고"] = 경고
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
