"""단계별 감사 추적 — 추후 사람 검수를 위해 각 단계의 판단 근거를 남긴다.

행정 점검 결과는 사람이 검수해 확정한다. 검수자가 "왜 이 값이 나왔는가" 를 되짚을 수 있어야
하므로, 파이프라인의 각 단계가 무엇을 보고 무엇을 판단했는지 한 파일에 누적한다.

산출: {workdir}/audit/{id}.json

    {
      "언론사_id": "30875",
      "제호_등록": "충청세종일보",
      "기준일": "2026-07-29",
      "갱신": "2026-07-29T13:02:11",
      "단계": [
        {"단계": "1_수집",     "결과": "성공",   "시각": "...", "상세": {...}},
        {"단계": "3_규칙추출",  "결과": "부분",   "시각": "...", "상세": {...}},
        {"단계": "5_LLM검토",  "결과": "성공",   "시각": "...", "상세": {...}}
      ]
    }

같은 단계를 재실행하면 해당 항목만 교체된다(전체 재실행 없이 부분 갱신 가능).
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

STAGES = ("1_수집", "1b_실패해소", "2_영역분리", "2b_운영현황", "2c_I3성격",
          "3_규칙추출", "3b_게이트", "4_증빙캡처", "4b_주석본",
          "5_LLM검토", "5b_비전진단", "5c_발행일검증", "6_대조")

# 새 단계를 추가할 때 STAGES 등록을 빠뜨려 세 번(5c_발행일검증·5b·1b_실패해소) 실행 중 크래시했다.
# 알 수 없는 단계는 이제 예외로 죽이지 않고 경고만 남기고 기록한다 — 파이프라인 중간에서
# 죽으면 이미 소비한 API 호출이 버려진다. 대신 단계명이 목록에 없으면 눈에 띄게 표시한다.
_UNKNOWN_PREFIX = "미등록:"

# work/ 안의 사이드카 파일 — {id}.json 본체가 아니다.
# work/*.json 을 glob 할 때 반드시 걸러야 한다. 빠뜨리면 한 매체가 여러 행으로 중복된다
# (실제로 .pubdate.json 을 빠뜨려 위치지도가 매체당 2행이 됐다).
SIDECAR_SUFFIXES = (".llm.json", ".diag.json", ".pubdate.json", ".resolved.json",
                    ".i3.json", ".fp.json")


def is_sidecar(name: str) -> bool:
    return str(name).endswith(SIDECAR_SUFFIXES)


def audit_path(workdir: Path | str, rid: str) -> Path:
    return Path(workdir) / "audit" / f"{rid}.json"


# 매체별 증빙 파일 — (표시명, 작업폴더 기준 상대경로 템플릿).
# 행정 처분은 증빙으로 다툰다. 판정 행에서 증빙 파일로 갈 수 없으면 결과를 쓸 수 없다 —
# pilot3·pilot4 모두 증빙_경로 컬럼이 전건 비어 있었고(0/26, 0/51), 검수자가 파일명을
# 손으로 짜맞춰야 했다(ISS-033).
EVIDENCE_FILES = (
    ("하단증빙", "shots/{rid}_footer.png"),
    ("상단증빙", "shots/{rid}_top.png"),
    ("하단근거표시", "shots/{rid}_footer_annot.png"),
    ("상단근거표시", "shots/{rid}_top_annot.png"),
    ("기사화면", "shots/{rid}_article.png"),
    ("근거위치", "evidence/{rid}.json"),
    ("렌더DOM", "shots/{rid}_rendered.html"),
    ("기사DOM", "shots/{rid}_article.html"),
    ("판단이력", "audit/{rid}.json"),
)

# 검수자가 반드시 봐야 하는 것 — 이게 없으면 증빙 불충분이다
CORE_EVIDENCE = ("하단증빙", "근거위치")


def evidence_paths(workdir: Path | str, rid: str) -> tuple[str, str]:
    """(증빙_경로 문자열, 누락 사유) 를 돌려준다.

    실제로 존재하는 파일만 적는다 — 없는 경로를 적으면 검수자가 헛걸음한다.
    핵심 증빙이 빠졌으면 캡처 단계가 남긴 실패 사유를 함께 돌려준다.
    """
    W = Path(workdir)
    있음, 없음 = [], []
    for 이름, tpl in EVIDENCE_FILES:
        rel = tpl.format(rid=rid)
        if (W / rel).exists():
            있음.append(f"{이름}={rel}")
        else:
            없음.append(이름)
    핵심누락 = [k for k in CORE_EVIDENCE if k in 없음]
    사유 = ""
    if 핵심누락:
        상세 = {}
        for s in load(W, rid).get("단계", []):
            if s.get("단계") in ("4_증빙캡처", "4b_주석본"):
                d = s.get("상세") or {}
                for k in ("결과", "예외", "기사_비고", "기사_실패"):
                    if d.get(k):
                        상세[f"{s['단계']}.{k}"] = str(d[k])[:120]
        사유 = (f"핵심 증빙 누락: {', '.join(핵심누락)}"
                + (f" — {상세}" if 상세 else " — 캡처 단계 기록 없음"))
    return " | ".join(있음), 사유


def load(workdir: Path | str, rid: str) -> dict:
    p = audit_path(workdir, rid)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return {"언론사_id": rid, "단계": []}


def record(workdir: Path | str, rid: str, 단계: str, 결과: str, 상세: dict,
           *, 제호: str | None = None, 기준일: str | None = None) -> None:
    """단계 이력에 기록. 같은 단계가 있으면 교체한다."""
    if 단계 not in STAGES:
        # 죽이지 않는다 — 이미 소비한 API 호출을 버리게 된다. 표시만 하고 기록한다.
        import sys as _sys
        print(f"[audit] 경고: STAGES 에 없는 단계 '{단계}' — md_core/audit.py 에 등록해라",
              file=_sys.stderr)
        단계 = f"{_UNKNOWN_PREFIX}{단계}"
    doc = load(workdir, rid)
    if 제호 is not None:
        doc["제호_등록"] = 제호
    if 기준일 is not None:
        doc["기준일"] = 기준일
    doc["갱신"] = datetime.now().isoformat(timespec="seconds")

    entry = {"단계": 단계, "결과": 결과,
             "시각": datetime.now().isoformat(timespec="seconds"), "상세": 상세}
    steps = [s for s in doc.get("단계", []) if s.get("단계") != 단계]
    steps.append(entry)
    doc["단계"] = sorted(steps, key=lambda s: s.get("단계", ""))

    p = audit_path(workdir, rid)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")


def summary_row(workdir: Path | str, rid: str) -> dict:
    """검수 시트용 1행 요약 — 어느 단계까지 갔고 각 단계 결과가 무엇인지."""
    doc = load(workdir, rid)
    row = {"언론사_id": rid, "제호_등록": doc.get("제호_등록", "")}
    done = {s["단계"]: s.get("결과", "") for s in doc.get("단계", [])}
    for st in STAGES:
        row[st] = done.get(st, "-")
    row["감사파일"] = str(audit_path(workdir, rid))
    return row


__all__ = ["STAGES", "audit_path", "load", "record", "summary_row"]
