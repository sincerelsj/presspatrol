#!/usr/bin/env python3
"""fetch_masthead — 메인 페이지의 상단/하단 블록·라벨 히트를 관찰한다 (발굴·디버깅용).

사용:
  python fetch_masthead.py "http://메인URL"
  python fetch_masthead.py "URL" --selector "#footer"      # 특정 블록 텍스트만
  python fetch_masthead.py "file:///경로/rendered.html"     # Chrome 저장 HTML 판독
  python fetch_masthead.py "URL" --full                    # 전체 텍스트도 출력
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from md_core.fields import LABEL, extract  # noqa: E402
from md_core.http import fetch_html  # noqa: E402
from md_core.zones import extract_zones  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--selector", default="", help="하단 블록 CSS selector 실험")
    ap.add_argument("--header-selector", default="")
    ap.add_argument("--full", action="store_true", help="전체 텍스트 앞부분도 출력")
    ap.add_argument("--chars", type=int, default=2000, help="블록별 출력 길이 상한")
    args = ap.parse_args()

    status, html, final_url = fetch_html(args.url)
    if not html:
        print(json.dumps({"url": args.url, "status": status, "error": "fetch 실패"},
                         ensure_ascii=False, indent=2))
        sys.exit(1)

    z = extract_zones(html, footer_selector=args.selector, header_selector=args.header_selector)
    hits = {}
    for field, pat in LABEL.items():
        for zone in ("하단", "상단", "전체"):
            for m in re.finditer(pat, z.get(zone, "")):
                s = max(0, m.start() - 10)
                hits.setdefault(field, []).append(
                    {"위치": zone, "문맥": re.sub(r"\s+", " ", z[zone][s:m.end() + 50])})
                break
            if field in hits:
                break

    out = {
        "url": args.url, "status": status, "final_url": final_url,
        "리다이렉트": final_url.rstrip("/") != args.url.rstrip("/"),
        "js_의심": z["js_의심"], "title": z["title"], "og_site_name": z["og_site_name"],
        "텍스트_길이": {"전체": len(z["전체"]), "상단": len(z["상단"]), "하단": len(z["하단"])},
        "라벨_히트": hits,
        "룰_추출_결과": extract(z),
        "하단_텍스트": z["하단"][: args.chars],
        "상단_텍스트": z["상단"][: args.chars // 2],
    }
    if args.full:
        out["전체_텍스트_앞부분"] = z["전체"][: args.chars]
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
