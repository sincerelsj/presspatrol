#!/usr/bin/env python3
"""중단된 포털검색 결과를 CSV 에 반영한다 (ISS-072 회수).

왜 필요한가
-----------
`resolve_failed.py` 는 건별로 `work/{id}.resolved.json` 을 즉시 남기지만, CSV 반영은
**루프가 전부 끝난 뒤 한 번**만 한다. 그래서 도중에 죽으면 이미 돈을 낸 결과가 CSV 에
한 줄도 안 들어간다. 게다가 `--resume` 은 `.resolved.json` 존재만 보고 건너뛰므로
다시 돌려도 복구되지 않는다 — 지불한 만큼이 영구히 사라진다.

이 스크립트는 남아 있는 `.resolved.json` 을 읽어 CSV 에 밀어 넣는다.
`resolve_failed` 의 반영 로직과 **같은 방식**이어야 결과가 어긋나지 않는다:
검색_판정·검색_후보URL 을 채우고 `apply_flags` 로 준수여부를 재도출한다.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from md_core.schema import apply_flags, extract_columns  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--base-date", "--기준일", dest="base_date", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    W = Path(args.workdir)
    csv_path = W / "outputs" / f"extract_{args.base_date}.csv"
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8-sig")))

    변경, 손상 = {}, []
    for p in sorted((W / "work").glob("*.resolved.json")):
        rid = p.stem.replace(".resolved", "")
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:                      # 반쯤 쓰인 파일 — 완료로 보면 안 된다
            손상.append(rid); continue
        판정 = d.get("최종_판정") or d.get("판정") or ""
        if not 판정:
            손상.append(rid); continue
        변경[rid] = {"검색_판정": 판정, "검색_후보URL": d.get("후보URL") or ""}

    반영 = 0
    for row in rows:
        ch = 변경.get(row["언론사_id"])
        if not ch:
            continue
        if (row.get("검색_판정") or "") == ch["검색_판정"]:
            continue                            # 이미 반영돼 있다
        row.update(ch)
        apply_flags(row)
        반영 += 1

    if not args.dry_run and 반영:
        tmp = csv_path.with_suffix(".csv.tmp")
        with tmp.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=extract_columns(), extrasaction="ignore")
            w.writeheader(); w.writerows(rows)
        tmp.replace(csv_path)                   # 원자적 교체 — 쓰다 죽어도 원본이 남는다

    import collections
    print(json.dumps({
        "resolved_파일": len(변경) + len(손상), "유효": len(변경),
        "손상_또는_미완": 손상[:20], "손상_수": len(손상),
        "CSV_반영": 반영, "dry_run": args.dry_run,
        "판정_분포": dict(collections.Counter(v["검색_판정"] for v in 변경.values()).most_common()),
        "csv": str(csv_path),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
