#!/usr/bin/env python3
"""emit_overrides — 이미 찍어 둔 캡처 기록에서 overrides/{도메인}.json 을 되살린다.

왜 필요한가 (ISS-064)
---------------------
`capture_shots --emit-override` 는 렌더 본문이 원 수집보다 길 때
`overrides/{도메인}.json` 에 `rendered_html` 을 써 준다. R0c(렌더HTML 반영 재추출)는
그 파일이 있어야 저장된 DOM 을 읽는다 — 없으면 `run_extract` 가 원래 URL 을 다시
requests 로 받아오므로 **캡처를 한 의미가 사라진다.**

그런데 `orchestrate` 는 이 플래그를 단일 프로세스 경로에만 붙였고 병렬 경로
(`run_capture_parallel`)에는 안 붙였다. 그래서 전수 실행에서 override 가 0개였다.

캡처를 다시 하지 않아도 된다 — 판단에 필요한 값이 `audit/{id}.json` 의
`4_증빙캡처` 상세에 전부 남아 있다(`렌더링_개선`, `원수집_본문길이`,
`렌더HTML_zones길이`, `url`). 이 스크립트는 그 기록만 읽어 같은 파일을 만든다.
**새로운 판단을 하지 않는다** — capture_shots 가 그 자리에서 썼을 내용을 그대로 옮긴다.

사용:
  python emit_overrides.py --workdir W [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from md_core import audit  # noqa: E402
from md_core.http import normalize_url  # noqa: E402


def domain_of(url: str) -> str:
    host = urlparse(normalize_url(url)).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    W = Path(args.workdir)
    ovdir = W / "overrides"
    shots = W / "shots"
    개선_ids: list[str] = []
    쓴_도메인: set[str] = set()
    건너뜀 = {"개선아님": 0, "렌더HTML없음": 0, "도메인없음": 0}

    for p in sorted((W / "audit").glob("*.json")):
        rid = p.stem
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        det = {}
        for s in d.get("단계", []):
            if s.get("단계") == "4_증빙캡처":
                det = s.get("상세") or {}
        if not det.get("렌더링_개선"):
            건너뜀["개선아님"] += 1
            continue
        html = shots / f"{rid}_rendered.html"
        if not html.exists():
            건너뜀["렌더HTML없음"] += 1
            continue
        dom = domain_of(det.get("url") or "")
        if not dom:
            건너뜀["도메인없음"] += 1
            continue

        개선_ids.append(rid)
        if args.dry_run:
            쓴_도메인.add(dom)
            continue

        op = ovdir / f"{dom}.json"
        ovdir.mkdir(parents=True, exist_ok=True)
        prev = {}
        if op.exists():
            try:
                prev = json.loads(op.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                prev = {}
        원 = det.get("원수집_본문길이")
        렌 = det.get("렌더HTML_zones길이")
        prev.update({
            "domain": dom,
            "rendered_html": html.resolve().as_uri(),
            "notes": (prev.get("notes", "") + " / " if prev.get("notes") else "")
                     + (f"JS 렌더링 필요 — capture_shots.py 가 저장한 DOM 사용"
                        f"(원 본문 {원}자 → 렌더HTML {렌}자, 같은 추출기 기준)"),
        })
        op.write_text(json.dumps(prev, ensure_ascii=False, indent=2), encoding="utf-8")
        쓴_도메인.add(dom)
        audit.record(W, rid, "4_증빙캡처", "성공",
                     dict(det, override_생성=str(op), override_출처="emit_overrides.py"),
                     제호=d.get("제호_등록"), 기준일=d.get("기준일"))

    print(json.dumps({
        "렌더링_개선_id수": len(개선_ids),
        "override_도메인": len(쓴_도메인),
        "건너뜀": 건너뜀,
        "dry_run": args.dry_run,
        "렌더링_개선_id": 개선_ids,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
