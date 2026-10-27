#!/usr/bin/env python3
"""5c_발행일검증 감사기록의 '판단' 문구를 실제 판정과 일치시킨다 (ISS-074).

왜 재호출 없이 고치는가
-----------------------
틀린 것은 **설명 문구 하나**뿐이고 판정값·근거·비용은 모두 정확하다. 같은 결과를 얻자고
LLM 을 1,143건 다시 부르면 약 $9 를 쓰면서 판정이 미세하게 달라질 위험만 생긴다.
감사기록 안에 이미 `메인게재`·`확인경로`·`발행연월일_게재` 가 있으므로 문구만 다시 쓴다.

무엇이 틀렸었나
---------------
2026-08-14 지침 R7-3 이 '기사를 클릭했을 때의 업로드 날짜' 를 발행연월일로 인정하면서
코드는 그 날짜를 게재값으로 **승격**하도록 바뀌었다. 그런데 감사 문구만 옛것으로 남아
"기사 상세 날짜는 21조 판정에 쓰지 않는다" 를 조건 없이 기록했다 — 같은 레코드가
게재값을 채워 놓고 그 값을 안 썼다고 적는 자기모순이라, 처분 근거로 낼 수 없었다.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def 문구(det: dict) -> str:
    메인 = bool(det.get("메인게재"))
    경로 = det.get("확인경로") or ""
    게재 = (det.get("발행연월일_게재") or "").strip()
    if 메인:
        return f"메인화면에서 날짜를 확인했다 (위치: {경로})"
    if 게재 and 경로 == "기사상세":
        return ("지침 R7-3 — 기사를 클릭했을 때 기재된 업로드 날짜를 발행연월일로 인정한다. "
                "이 값을 게재값으로 승격했다. 단 지침 R10 비고에 따라 등록연월일로는 전이시키지 않는다")
    return "메인·기사 상세 어디에서도 날짜를 확인하지 못했다 — 발행연월일 미게재"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    W = Path(args.workdir)
    검사 = 수정 = 승격 = 0
    for p in sorted((W / "audit").glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        바뀜 = False
        for s in d.get("단계", []):
            if s.get("단계") != "5c_발행일검증":
                continue
            det = s.get("상세") or {}
            검사 += 1
            새 = 문구(det)
            if det.get("판단") != 새:
                det["판단"] = 새
                바뀜 = True
                수정 += 1
                if det.get("확인경로") == "기사상세" and (det.get("발행연월일_게재") or "").strip():
                    승격 += 1
        if 바뀜 and not args.dry_run:
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(p)
    print(json.dumps({"5c_기록": 검사, "문구_수정": 수정,
                      "그중_기사상세_승격건": 승격, "dry_run": args.dry_run},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
