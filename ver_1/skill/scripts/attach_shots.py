#!/usr/bin/env python3
"""attach_shots — W/shots/ 의 스크린샷을 verify CSV 의 증빙_경로 컬럼에 연결.

파일명 규칙: {언론사_id}_*.png|jpg  (예: 20001_footer.png, 20001_top.png)
여러 장이면 세미콜론으로 연결한다.

사용: python attach_shots.py --workdir W [--기준일 YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from md_core.schema import verify_columns  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--base-date", "--기준일", dest="base_date", default="")
    args = ap.parse_args()

    base = (datetime.strptime(args.base_date, "%Y-%m-%d").date()
            if args.base_date else date.today())
    workdir = Path(args.workdir)
    path = workdir / "outputs" / f"verify_{base.isoformat()}.csv"
    if not path.exists():
        cands = sorted((workdir / "outputs").glob("verify_*.csv"))
        if not cands:
            print(json.dumps({"error": "verify CSV 없음 — compare_registry.py 먼저"},
                             ensure_ascii=False)); sys.exit(1)
        path = cands[-1]

    shots: dict[str, list[str]] = {}
    for p in sorted((workdir / "shots").glob("*")):
        if p.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
            continue
        sid = p.stem.split("_")[0]
        shots.setdefault(sid, []).append(str(p))

    rows = list(csv.DictReader(path.open(encoding="utf-8-sig")))
    matched = 0
    for r in rows:
        paths = shots.get(str(r.get("언론사_id")), [])
        if paths:
            r["증빙_경로"] = ";".join(paths)
            matched += 1
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=verify_columns())
        w.writeheader()
        w.writerows(rows)

    print(json.dumps({"verify_csv": str(path), "스크린샷_사이트": len(shots),
                      "연결된_행": matched,
                      "미연결_파일": [k for k in shots
                                 if k not in {str(r.get("언론사_id")) for r in rows}]},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
