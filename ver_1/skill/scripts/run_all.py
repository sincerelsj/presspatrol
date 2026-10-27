#!/usr/bin/env python3
"""run_all — 4단계(매핑확인 → 추출 → 대조 → 최종xlsx)를 한 번에 실행.

네트워크 제약이 없는 환경(사용자 PC·Claude Code·WSL 등)에서 전수 점검을 한 방에 돌리기 위한 러너.
Windows cmd 에서도 안전하게 쓰도록 옵션은 전부 영문이다.

사용:
  python run_all.py --input 등록정보.xlsx
  python run_all.py --input 등록정보.xlsx --workdir W --limit 10 --workers 8
  python run_all.py --input 등록정보.xlsx --op-col 운영상태 --op-val 운영 --base-date 2026-07-28
  python run_all.py --input 등록정보.xlsx --map "{\\"url\\":\\"현재_메인URL\\"}"
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent


def step(title: str, cmd: list[str], *, required: bool = True) -> dict:
    print(f"\n{'=' * 60}\n▶ {title}\n{'=' * 60}", flush=True)
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = (p.stdout or "").strip()
    if p.stderr and p.stderr.strip():
        print("[stderr]", p.stderr.strip()[-1500:], file=sys.stderr)
    data: dict = {}
    try:
        data = json.loads(out)
        print(json.dumps(data, ensure_ascii=False, indent=2)[:4000], flush=True)
    except json.JSONDecodeError:
        print(out[-4000:], flush=True)
    if p.returncode != 0:
        print(f"❌ 실패: {title}", flush=True)
        if required:
            sys.exit(p.returncode)
    return data


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, help="등록정보 xlsx (전년도 점검 결과 등)")
    ap.add_argument("--workdir", default="disclosure_audit_work")
    ap.add_argument("--sheet", default="")
    ap.add_argument("--map", dest="map_json", default="", help='{"표준키":"실제헤더"} 강제 매핑')
    ap.add_argument("--base-date", dest="base_date", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--ids", default="")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--op-col", dest="op_col", default="", help="운영상태 필터 사용 (표준키: 운영상태)")
    ap.add_argument("--op-val", dest="op_val", default="운영")
    ap.add_argument("--skip-inspect", action="store_true")
    args = ap.parse_args()

    base = args.base_date or date.today().isoformat()
    workdir = str(Path(args.workdir).resolve())
    py = sys.executable
    common = ["--input", args.input]
    if args.sheet:
        common += ["--sheet", args.sheet]
    if args.map_json:
        common += ["--map", args.map_json]

    print(f"입력: {args.input}\n작업폴더: {workdir}\n기준일: {base}", flush=True)

    if not args.skip_inspect:
        insp = step("① 등록정보 컬럼 매핑 확인", [py, str(HERE / "inspect_registry.py"), args.input]
                    + (["--sheet", args.sheet] if args.sheet else [])
                    + (["--map", args.map_json] if args.map_json else []))
        if insp.get("경고"):
            print("⚠ 경고:", "; ".join(insp["경고"]), flush=True)
        if "url" not in (insp.get("mapping") or {}):
            print("❌ URL 컬럼을 못 찾았다. --map '{\"url\":\"실제헤더\"}' 로 지정해 다시 실행하라.")
            sys.exit(2)

    ex_cmd = [py, str(HERE / "run_extract.py"), "--workdir", workdir, "--기준일", base,
              "--workers", str(args.workers)] + common
    if args.op_col:
        ex_cmd += ["--운영컬럼", args.op_col, "--운영값", args.op_val]
    if args.limit:
        ex_cmd += ["--limit", str(args.limit)]
    if args.ids:
        ex_cmd += ["--ids", args.ids]
    ex = step("② 전수 추출 (fetch + 룰 추출)", ex_cmd)

    cm = step("③ 등록정보 대조 (diff + 확인필요 플래그)",
              [py, str(HERE / "compare_registry.py"), "--workdir", workdir, "--기준일", base] + common)

    fi = step("④ 최종 xlsx 생성",
              [py, str(HERE / "finalize.py"), "--workdir", workdir, "--기준일", base] + common)

    print(f"\n{'=' * 60}\n■ 결과 요약\n{'=' * 60}")
    print(json.dumps({
        "대상_사이트": ex.get("대상_사이트"),
        "접속실패": ex.get("접속실패"),
        "js_의심": len(ex.get("js_의심") or []),
        "8항목_전부게재(초안)": fi.get("준수(8항목 전부게재)"),
        "확인필요": fi.get("확인필요"),
        "확인필요_사유분포": fi.get("확인필요_사유분포"),
        "항목별_대조판정": cm.get("항목별_대조판정"),
        "final_xlsx": fi.get("final_xlsx"),
    }, ensure_ascii=False, indent=2))

    판독 = ex.get("판독필요_사이트") or []
    if 판독:
        print(f"\n▶ 판독 필요(미검출 3항목 이상) {len(판독)}건: {', '.join(map(str, 판독[:30]))}")
        print("  → Cowork/Claude Code 에 이 폴더를 열고 '필요적 게재사항 판독' 을 요청하면"
              " fetch_masthead.py 로 확인해 overrides/{도메인}.json 을 채운다.")
    if ex.get("접속실패"):
        print(f"\n⚠ 접속 실패 {ex['접속실패']}건 — 네트워크 차단(프록시 allowlist)·도메인 만료·"
              "차단(UA) 을 구분해야 한다. work/{id}.json 의 error 를 확인하라.")


if __name__ == "__main__":
    main()
