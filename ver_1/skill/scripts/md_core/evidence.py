"""근거가 값을 뒷받침하는지 검사한다 (ISS-017).

왜 필요한가
-----------
기존 검증은 "근거 문구가 원문에 실재하는가" 만 봤다. "그 근거가 그 값의 근거인가" 는 보지
않았다. 실제로 30877 제호에서 값 `코리아리서치`(한글)에 근거 `the Korearesearch`(영문
저작권 표기)가 붙어 **검증을 통과했다.** 근거 문자열은 원문에 있었기 때문이다.

이 모듈은 값의 **핵심 토큰**이 근거에 실제로 들어 있는지 항목 유형별로 확인한다.
지지되지 않으면 llm_extract 가 그 항목만 다시 찾도록 LLM 에 되돌려 보낸다(수리 루프).
"""
from __future__ import annotations

import re

날짜항목 = ("등록연월일", "발행연월일", "창간일")
번호항목 = ("등록번호",)
전화항목 = ("발행소 전화번호",)
이름항목 = ("발행인", "편집인")
주소항목 = ("발행소 주소",)


def _norm(s: str) -> str:
    """공백·구두점 제거 후 소문자화 — 표기 차이를 흡수한다."""
    return re.sub(r"[\s\-.,·:：|ㅣ/()\[\]]+", "", (s or "")).lower()


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _dates(s: str) -> set[str]:
    """문자열에서 YYYYMMDD 집합. 2014-03-05 / 2014년 3월 5일 / 2014.3.5 모두 인식."""
    out = set()
    for m in re.finditer(r"(19\d{2}|20\d{2})\D{0,4}(\d{1,2})\D{0,4}(\d{1,2})", s or ""):
        y, mo, d = (int(g) for g in m.groups())
        if 1 <= mo <= 12 and 1 <= d <= 31:
            out.add(f"{y:04d}{mo:02d}{d:02d}")
    return out


def supports(field: str, value: str, evidence: str) -> tuple[bool, str]:
    """→ (지지되는가, 이유). 값이 비어 있으면 검사 대상이 아니므로 True."""
    value = (value or "").strip()
    evidence = (evidence or "").strip()
    if not value or value == "해당사항 없음":
        return True, "값 없음 — 검사 대상 아님"
    if not evidence:
        return False, "근거가 비어 있다"

    v, e = _norm(value), _norm(evidence)

    if field in 날짜항목:
        dv, de = _dates(value), _dates(evidence)
        if not dv:
            return (v in e), "값에서 날짜를 못 읽음 — 문자열 포함으로 판정"
        if dv & de:
            return True, "날짜 숫자 일치"
        return False, f"근거에 값의 날짜가 없다 (값 {sorted(dv)} / 근거 {sorted(de) or '없음'})"

    if field in 전화항목:
        dv = _digits(value)
        if dv and dv in _digits(evidence):
            return True, "전화번호 숫자열 포함"
        return False, f"근거에 값의 번호 숫자열({dv})이 없다"

    if field in 번호항목:
        # 등록번호는 '세종아00024' 처럼 지역명+종별문자+숫자. 숫자와 종별문자를 함께 본다
        dv = _digits(value)
        종별 = re.search(r"[아가나다라마바사자차카타파하]", value)
        num_ok = bool(dv) and dv.lstrip("0") in _digits(evidence).replace("0" * 0, "") \
            or (bool(dv) and dv in _digits(evidence))
        if num_ok and (not 종별 or 종별.group(0) in evidence):
            return True, "등록번호 숫자·종별문자 일치"
        return False, f"근거에 등록번호 숫자({dv})나 종별문자가 없다"

    if field in 이름항목:
        if v and v in e:
            return True, "이름 문자열 포함"
        return False, f"근거에 이름 '{value}' 이 없다"

    if field in 주소항목:
        # 주소는 근거가 잘려 있을 수 있다 — 앞쪽 토큰 2개가 있으면 인정
        toks = [t for t in re.split(r"\s+", value) if len(t) >= 2][:2]
        if toks and all(_norm(t) in e for t in toks):
            return True, f"주소 앞부분 토큰 {toks} 포함"
        if v[:12] and v[:12] in e:
            return True, "주소 앞 12자 포함"
        return False, f"근거에 주소 앞부분({' '.join(toks) or value[:12]})이 없다"

    # 제호·제호 영문 등 일반
    if v in e:
        return True, "문자열 포함"
    # 영문 제호는 대소문자·공백 차이가 흔하다
    if re.sub(r"[^a-z0-9]", "", v) and re.sub(r"[^a-z0-9]", "", v) in re.sub(r"[^a-z0-9]", "", e):
        return True, "영숫자 정규화 후 포함"
    return False, f"근거에 값 '{value[:24]}' 이 없다"


def audit_findings(findings: dict, fields: tuple | list) -> dict:
    """항목별 지지 여부. → {항목: (bool, 이유)} — 실패한 것만 담는다."""
    bad = {}
    for f in fields:
        item = findings.get(f) or {}
        ok, why = supports(f, item.get("값", ""), item.get("근거", ""))
        if not ok:
            bad[f] = why
    return bad


__all__ = ["supports", "audit_findings"]
