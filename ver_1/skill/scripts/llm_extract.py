#!/usr/bin/env python3
"""llm_extract — 필요적 게재사항을 Claude API 로 추출 (규칙 추출기의 대체·교차검증용).

설계 근거
---------
파일럿 1·2회차에서 규칙 추출기가 놓친 것은 '텍스트가 없어서' 가 아니라 '라벨이 없어서' 였다.
  - 가상 예: "서울특별시 예시구 샘플로 1 ... ㅣ 샘플신문 대표:홍길동" → 주소·발행인 미검출
  - 가상 예: "서울특별시 예시구 샘플동 1ㅣ대표전화" → 주소 미검출
라벨 없는 값을 문맥으로 읽는 것은 정규식의 구조적 한계이고 LLM 이 잘하는 일이다.
그래서 fetch·zone 슬라이싱은 그대로 두고 **필드 라벨링 단계만** 교체한다.

안전장치 (행정 점검이므로 환각이 곧 오판정이다)
  1. 근거 verbatim 검증 — 모델이 낸 근거가 입력 텍스트의 부분문자열이 아니면 값을 버린다
  2. 규칙 결과와 병기 — 두 추출기의 합의/불일치를 확인필요 판단에 쓴다
  3. 기준일은 user turn 에만 넣는다 — system 에 날짜를 넣으면 프롬프트 캐시가 매번 무효화된다

사용
----
  # 1건으로 연결·비용 확인 (권장 첫 단계)
  python llm_extract.py --workdir ver_1/run2 --ids 30875 --sync

  # 소규모: 순차 호출
  python llm_extract.py --workdir ver_1/run2 --sync

  # 전수: Batch API (50% 할인, 대부분 1시간 내 완료)
  python llm_extract.py --workdir ver_1/run2 --batch-submit
  python llm_extract.py --workdir ver_1/run2 --batch-collect

  # 호출 없이 토큰·비용만 추정
  python llm_extract.py --workdir ver_1/run2 --dry-run

산출
----
  W/work/{id}.llm.json          모델 원응답 + 검증 후 findings
  W/outputs/llm_compare_{날짜}.csv   항목별 규칙값 vs LLM값 vs 합의여부
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from md_core import audit, batch, evidence  # noqa: E402
from md_core.fields import EXTRA_FIELDS, FIELDS, REQUIRED_FIELDS  # noqa: E402

def _load_dotenv() -> None:
    """이 저장소 루트의 .env 만 읽어 os.environ 에 채운다.

    실제 환경변수가 이긴다 — .env 는 이미 설정된 값을 덮어쓰지 않는다.
    키가 없어도 조용히 넘어간다(필수 검사는 load_env_key 가 한다).
    MODEL 등 모듈레벨 상수가 .env 값을 보려면 그 정의보다 먼저 돌아야 한다.
    """
    p = Path(__file__).resolve().parents[3] / ".env"
    if not p.is_file():
        return
    allowed = {"ANTHROPIC_API_KEY", "DISCLOSURE_LLM_MODEL", "DISCLOSURE_LLM_MAX_TOKENS"}
    for line in p.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k in allowed and v and k not in os.environ:
            os.environ[k] = v


_load_dotenv()

MODEL = os.environ.get("DISCLOSURE_LLM_MODEL", "claude-sonnet-5")
MAX_TOKENS = int(os.environ.get("DISCLOSURE_LLM_MAX_TOKENS", "3000"))
TOOL_NAME = "record_disclosure"

# Sonnet 5 요금 (USD / 1M tokens). 2026-08-31 까지 도입가 $2/$10 적용.
PRICE_IN, PRICE_OUT = 2.00, 10.00


# ---------------------------------------------------------------- 프롬프트

SYSTEM_PROMPT = """\
너는 한국 신문법 제21조 '필요적 게재사항' 점검관이다. 인터넷신문 메인화면의 상단·하단
텍스트를 받아 게재된 항목을 추출한다.

## 점검 항목 (필수 8)
제호 · 등록번호 · 등록연월일 · 발행인 · 편집인 · 발행연월일 · 발행소 주소 · 발행소 전화번호
## 부가 항목 (참고)
제호 영문 · 창간일

## 절대 규칙
1. **입력 텍스트에 실제로 있는 것만 적는다.** 추측·상식·외부지식으로 값을 채우지 마라.
2. 각 항목의 `근거`에는 그 값이 나온 **입력 텍스트의 원문 조각을 그대로 복사**한다.
   한 글자도 바꾸지 마라. 요약·정규화·재작성 금지. 값이 없으면 근거도 빈 문자열.
   (근거가 입력 텍스트의 부분문자열인지 프로그램이 검사한다. 불일치하면 값이 폐기된다.)
3. 없으면 값과 근거를 빈 문자열로 두고 `위치`는 "없음". 억지로 채우지 마라.

## 위치 판정
- `하단` — 입력의 [하단] 블록에서 발견
- `상단` — 입력의 [상단] 블록에서 발견
- `기타` — 어느 블록에도 없으나 텍스트 안에 있음
- `없음` — 발견하지 못함
입력은 **메인화면** 텍스트다. 위치는 기록용이며, `기타` 위치라도 게재로 인정한다
(지침 R17: 1순위 메인 하단 · 2순위 상단 로고 · **3순위 기타 위치**).

## 항목별 판정 기준 — 2026-08-13 지침을 그대로 따른다
- **제호**: 매체 이름. 하단 제호명 또는 상단 업체명/로고 어느 쪽이든 준수.
- **등록번호**: `지역명 + 아 + 숫자 5자리` (`세종아00024`, `강원아00022`). 지역명이 없기도
  하고 자릿수가 모자란 표기(`강원 아4`)도 실재하니 그것도 값으로 잡되 확신도를 낮춰라.
  `사업자등록번호`(132-14-82951 형식)와 혼동하지 마라 — 전혀 다른 항목이다.
- **등록연월일**: 다음 중 **하나라도** 있으면 값으로 잡는다.
  ① `등록연월일`·`등록일` 명칭의 날짜
  ② **명칭과 상관없이 날짜형태**가 있음 — `발행연월일`·`창간일` 등 다른 이름의 날짜도 된다
  단 **기사 업로드 날짜(기사목록·기사 옆의 날짜)는 등록연월일의 근거가 되지 않는다.**
  `등록번호: 세종 아00023(2014년2월21일)` 처럼 등록번호에 붙은 날짜가 가장 확실하다.
- **발행연월일**: 다음 중 **하나라도** 있으면 값으로 잡는다.
  ① `발행연월일`·`최종편집일시` 명칭의 날짜
  ② **명칭과 상관없이 날짜형태**가 있음 — `등록연월일`·`창간일` 등 다른 이름의 날짜도 된다
  ③ **기사 업로드 날짜** (기사목록의 날짜, 기사를 열었을 때의 날짜)
  ★ 종전 기준은 '최신 기사 갱신 날짜' 만 인정하고 과거 날짜(등록·창간)를 배제했다.
  **지침은 그 반대다.** 하단에 창간일만 있어도 발행연월일은 준수다. 오래된 날짜라는
  이유로 값을 비우지 마라.
- **날짜 항목의 유일한 제외**: '투데이'·'오늘'·'TODAY' 처럼 **현재 날짜를 실시간으로
  알려주는 표기**는 날짜로 보지 않는다. 단 제호에 '투데이' 가 들어간 매체(뉴스투데이,
  머니투데이)의 기사 날짜는 정상적인 날짜다 — 혼동하지 마라.
- **발행인 / 편집인**: 사람 이름. 다음 중 하나라도 있으면 준수다.
  ① `발행인`·`편집인`·`발행·편집인` 명칭의 성명
  ② **이름을 기재하는 항목이 하나라도** 있으면 인정한다 —
     `대표`, `대표이사`, `편집국장`, `청소년보호책임자`, `기사배열책임자`, `고충처리인` 등
  ★ 종전 기준은 `청소년보호책임자`·`편집국장` 을 명시적으로 배제했다. **지침은 반대다.**
     대체 라벨로 인정했으면 notes 에 어떤 라벨이었는지 적어라.
  ※ 라벨 뒤의 값이 사람 이름인지 확인해라 — `대표전화 : 033-…` 의 '전화' 는 이름이 아니다.
- **발행소 주소**: 시/도로 시작하는 소재지. **라벨이 없어도 인정한다.**
  `서울특별시 예시구 샘플로 1 샘플빌딩 2층` 처럼 구분자(ㅣ, |, /) 사이에
  라벨 없이 놓인 경우가 매우 흔하다. 주소로 읽히면 값으로 잡아라.
  기사 제목·약관 문장을 주소로 잡지 마라 — 도로명+건물번호·지번·읍면동 중 하나는 있어야 한다.
- **발행소 전화번호**: `지역번호-국번-가입자번호` 형식. 팩스·휴대폰과 구분하되, 대표전화가 없고
  휴대폰만 있으면 그것을 값으로 잡고 notes 에 남겨라.
- **창간일**: `창간일: 2014년 3월 5일`. 등록연월일과 다를 수 있고, 둘 다 기록한다.

## 항목 간 교차 준수 규칙 — 프로그램이 나중에 자동 적용한다
아래는 **네가 할 일이 아니다.** 각 항목은 화면에서 실제로 본 대로만 채워라. 프로그램이
지침 비고에 따라 다음을 자동 보정한다: 등록연월일↔발행연월일 전이(기사 업로드 날짜 제외),
발행인↔편집인 전이. 그러니 **한쪽을 채우려고 다른 쪽 값을 억지로 복사하지 마라.**

## 자리표시(더미값)는 미게재다
`등록번호 : 00-0000-00000`, `등록일자 : 0000-00-00`, `TEL : 000-0000-0000`,
`___`, `미정`, `준비중` 처럼 **라벨만 달고 내용을 채우지 않은 값**은 게재가 아니다.
값과 근거를 **빈 문자열로 두고** decision 을 `규칙기각` 또는 `양쪽없음` 으로 내라.
형식이 그럴듯해 보여도 마찬가지다 — 프로그램이 이 값을 자동 폐기하므로 억지로 채우면
근거만 남고 값은 사라진다.

## 애매할 때
- 매체에 유리하게 판단하되 `확신도`를 낮추고 `notes`에 이유를 남긴다.
- 이미지로만 표기된 것으로 보이면(텍스트에 없음) 값을 비우고 notes 에 "이미지 표기 의심".

## 확신도
- `high` — 명확한 라벨과 값이 함께 있다
- `mid`  — 라벨이 없으나 형식·문맥상 분명하다 (등록 정보와 일치하는 경우 포함)
- `low`  — 추정이다. 사람 확인이 필요하다

## 규칙 추출기의 1차 결과를 함께 받는다 — 정답이 아니다
정규식 기반 추출기가 먼저 훑은 결과를 [규칙 1차결과] 로 받는다. **참고자료일 뿐이며 정답이
아니다.** 규칙은 라벨이 있는 값에는 강하지만, 라벨이 없는 값을 놓치고 엉뚱한 값을 끌어오는
실패를 자주 한다. 반드시 원문에서 근거를 **다시 확인**하고, 항목마다 `decision` 을 정해라.

- `규칙확인` — 규칙 값이 맞다. 원문에서 근거를 확인했다. 같은 값을 그대로 낸다.
- `규칙수정` — 값이 부분적으로 틀렸거나 위치·형식이 잘못됐다. 고쳐서 낸다.
- `신규발견` — 규칙이 못 찾았으나 원문에 있다. 새로 낸다.
- `규칙기각` — 규칙이 값을 냈지만 **원문에 근거가 없거나 다른 항목의 값을 잘못 가져왔다.**
  값을 비우고 이 decision 을 낸다.
- `양쪽없음` — 규칙도 못 찾았고 원문에도 없다.

★ 규칙 값을 그대로 베끼지 마라. 특히 `확신도=low` 이거나 `추출방법` 에 '추정'·'라벨없음'·
'title추정' 이 붙은 항목은 규칙이 짐작한 것이므로 **의심하고 원문을 직접 봐라.**

★ 다만 `추출방법` 이 `지침R7-2`·`지침R10-2`·`지침R7-3`·`이름항목대체(...)`·
`지침교차규칙(...)` 인 항목은 **지침이 명시적으로 인정하는 대체 근거**다. '라벨이 다르다'
는 이유로 기각하지 마라 — 그 값의 원문 근거가 실제로 있는지만 확인하면 된다.
예: 하단에 창간일만 있고 등록연월일 라벨이 없어도, 규칙이 창간일 날짜를 등록연월일로 낸 것은
지침 R10-2 에 따라 **맞다**. 근거 문구가 원문에 있으면 `규칙확인` 이다.

반드시 record_disclosure 도구를 호출해 결과를 제출해라."""


ALL_FIELDS = list(FIELDS)

# JSON 스키마의 property 키는 ^[a-zA-Z0-9_.-]{1,64}$ 여야 한다 — 한글 키는 400 이다.
# ASCII 키로 주고받고 한글 항목명은 description 으로 전달한다.
FIELD_KEY = {
    "제호": "title_ko",
    "제호 영문": "title_en",
    "등록번호": "reg_no",
    "등록연월일": "reg_date",
    "발행인": "publisher",
    "편집인": "editor",
    "발행연월일": "issue_date",
    "발행소 주소": "address",
    "발행소 전화번호": "phone",
    "창간일": "founded_date",
}
KEY_FIELD = {v: k for k, v in FIELD_KEY.items()}
assert set(FIELD_KEY) == set(ALL_FIELDS), f"FIELD_KEY 불일치: {set(ALL_FIELDS) ^ set(FIELD_KEY)}"


def _field_schema(ko: str) -> dict:
    return {
        "type": "object",
        "description": f"항목: {ko}",
        "properties": {
            "value": {"type": "string", "description": f"{ko} 추출값. 없으면 빈 문자열"},
            "evidence": {"type": "string",
                         "description": "값이 나온 입력 원문 조각을 그대로 복사(한 글자도 변경 금지). "
                                        "없으면 빈 문자열"},
            "location": {"type": "string", "enum": ["하단", "상단", "기타", "없음"],
                         "description": "값을 발견한 위치"},
            "confidence": {"type": "string", "enum": ["high", "mid", "low"]},
            "decision": {"type": "string",
                         "enum": ["규칙확인", "규칙수정", "신규발견", "규칙기각", "양쪽없음"],
                         "description": "규칙 1차결과에 대한 판단. 검수자가 되짚을 근거가 된다"},
        },
        "required": ["value", "evidence", "location", "confidence", "decision"],
        "additionalProperties": False,
    }


def build_tool() -> dict:
    props = {FIELD_KEY[f]: _field_schema(f) for f in ALL_FIELDS}
    props["notes"] = {"type": "string", "description": "판정 근거·유보 사항. 없으면 빈 문자열"}
    return {
        "name": TOOL_NAME,
        "description": ("필요적 게재사항 추출 결과를 제출한다. 각 키의 한글 항목명은 "
                        + ", ".join(f"{v}={k}" for k, v in FIELD_KEY.items())),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": props,
            "required": [FIELD_KEY[f] for f in ALL_FIELDS] + ["notes"],
            "additionalProperties": False,
        },
    }


def format_rule_result(rule: dict) -> str:
    """규칙 1차결과를 LLM 이 읽을 형태로. 확신도·추출방법을 함께 줘서 의심 대상을 알린다."""
    lines = []
    for f in ALL_FIELDS:
        r = rule.get(f) or {}
        val = (r.get("값") or "").strip()
        if val:
            lines.append(f"  {f}: {val}  (위치={r.get('위치','')}, "
                         f"확신도={r.get('확신도','')}, 방법={r.get('추출방법','')})")
        else:
            lines.append(f"  {f}: (규칙 미검출)")
    return "\n".join(lines)


def build_user_turn(detail: dict, base: date, *, blind: bool = False) -> str:
    """기준일·등록정보 힌트·zone 텍스트·규칙 1차결과.

    system 에 날짜를 넣지 않는다 — 넣으면 프롬프트 캐시가 매 요청 무효화된다.
    blind=True 면 규칙 결과를 주지 않는다(독립 교차검증용 — 앵커링 측정에 쓴다).
    """
    top = (detail.get("상단_텍스트") or "").strip()
    bottom = (detail.get("하단_텍스트") or "").strip()
    hint_제호 = detail.get("제호_등록") or ""
    parts = [
        f"기준일: {base.isoformat()}",
        f"등록된 제호: {hint_제호 or '(없음)'}",
        f"사이트 title 태그: {(detail.get('title') or '')[:80]}",
        f"\n[상단]\n{top or '(없음)'}",
        f"\n[하단]\n{bottom or '(없음)'}",
    ]
    # 게재사항이 footer 밖(본문 중간 위젯)에 있는 사이트를 위한 추가 블록.
    # 이게 없으면 모델이 "입력 어디에도 근거가 없다" 며 정당한 규칙값을 전부 기각한다
    # (1012 슬기로운시니어뉴스 실측 — 하단 99자, 게재사항은 중간 <ul><li> 6항목).
    블록 = (detail.get("게재사항_블록") or "").strip()
    if 블록:
        parts.append(
            f"\n[게재사항 블록] — 상단·하단 밖에서 발견된 게재사항 밀집 구간이다. "
            f"지침 R17 은 '3순위 기타 위치' 도 게재로 인정하므로 **여기 있는 값도 게재다.** "
            f"이 블록의 문구도 근거로 그대로 인용할 수 있다.\n{블록}")
    if detail.get("하단_잘림") or detail.get("상단_잘림"):
        parts.append(
            "\n★[경고] 위 텍스트는 길이 제한으로 **일부가 잘렸다.** "
            + ("하단이 잘렸다. " if detail.get("하단_잘림") else "")
            + ("상단이 잘렸다. " if detail.get("상단_잘림") else "")
            + "규칙 1차결과의 값이 위 텍스트에서 안 보이더라도 **그것만으로 규칙기각하지 마라** — "
              "잘린 부분에 있을 수 있다. 그런 항목은 규칙확인으로 두고 확신도를 low 로 낮추고 "
              "notes 에 '텍스트 잘림으로 근거 미확인' 을 적어라.")
    if not blind:
        parts.append("\n[규칙 1차결과] — 참고자료다. 원문에서 근거를 다시 확인하고 "
                     "항목마다 decision 을 정해라.\n" + format_rule_result(detail.get("findings") or {}))
    else:
        parts.append("\n[규칙 1차결과] 제공되지 않음 — 원문만 보고 독립적으로 판단하고 "
                     "decision 은 값이 있으면 신규발견, 없으면 양쪽없음 으로 적어라.")
    return "\n".join(parts) + "\n"


# ---------------------------------------------------------------- 검증

# 자리만 잡아둔 더미값. 매체가 라벨은 달았지만 내용을 채우지 않은 것이므로 **미게재**다.
#
# 왜 코드로 막는가: 2026-09-02 실측에서 강원웰니스신문 하단이
#   `등록번호 : 00-0000-00000 | 등록일자 : 0000-00-00`
# 이었다. 규칙은 형식 기각했는데 **LLM 이 `신규발견` 으로 되살렸다.** 모델은 notes 에
# "실질적으로 미기재(더미값)에 해당함" 이라고 정확히 인식하고도 값을 채웠다 —
# 프롬프트 지시만으로는 못 막는다는 뜻이다. 근거 verbatim 검증과 같은 층에서 프로그램이 건다.
_PLACEHOLDER_VALUE = re.compile(
    r"^[\s\-_.·ㅡ—–]*$"                       # 빈칸·하이픈·언더바만
    r"|^[0O]+([\s\-.]+[0O]+)*$"               # 00-0000-00000 · 0000-00-00 · 000.000
    r"|^[xX×]+([\s\-.]+[xX×]+)*$"             # xxx-xxxx-xxxx
    r"|^(0000|9999|1111)[-./]"                # 0000-00-00 류 날짜
    r"|^(미정|미기재|없음|해당없음|추후|준비중|예정|N/?A|TBD|없슴)$",
    re.I)


def is_placeholder(v: str) -> bool:
    s = str(v or "").strip()
    if not s:
        return True
    if _PLACEHOLDER_VALUE.search(s):
        return True
    # 숫자를 담아야 하는 값인데 유효숫자가 하나도 없다 (`00-0000-00000`)
    digits = re.sub(r"\D", "", s)
    return bool(digits) and set(digits) == {"0"}


def verify_findings(raw: dict, detail: dict) -> tuple[dict, list[str]]:
    """근거 verbatim 검증 + 더미값 검증. 통과 못 하면 값을 폐기한다.

    공백만 다른 경우는 통과시킨다(모델이 개행을 공백으로 바꾸는 일이 흔하다).
    """
    haystacks = []
    for k in ("하단_텍스트", "상단_텍스트", "게재사항_블록", "title"):
        v = detail.get(k) or ""
        if v:
            haystacks.append(v)
            haystacks.append(" ".join(v.split()))
    폐기: list[str] = []
    out: dict = {}
    for f in ALL_FIELDS:
        item = raw.get(FIELD_KEY[f]) or {}
        val = str(item.get("value") or "").strip()
        ev = str(item.get("evidence") or "").strip()
        pos = str(item.get("location") or "없음")
        conf = str(item.get("confidence") or "low")
        dec = str(item.get("decision") or "")

        if not val:
            out[f] = {"값": "", "근거": "", "위치": "없음", "확신도": "low",
                      "추출방법": f"LLM_미검출({dec})" if dec else "LLM_미검출",
                      "판단": dec}
            continue

        # 더미값은 근거가 원문에 **있어도** 게재가 아니다 — 매체가 자리만 잡아둔 것이다.
        # 제호·주소처럼 자유 문자열인 항목은 대상이 아니다(숫자·날짜 항목만 본다).
        if f in ("등록번호", "등록연월일", "발행연월일", "창간일", "발행소 전화번호") \
                and is_placeholder(val):
            폐기.append(f)
            out[f] = {"값": "", "근거": f"[더미값 폐기] 자리표시 표기 — 실질 미기재: {val[:40]!r}",
                      "위치": "없음", "확신도": "low",
                      "추출방법": "LLM_더미값(폐기)", "판단": dec,
                      "폐기된_값": val[:60], "폐기된_근거": ev[:120]}
            continue

        ev_norm = " ".join(ev.split())
        ok = bool(ev) and any(ev in h or ev_norm in h for h in haystacks)
        if not ok:
            폐기.append(f)
            out[f] = {"값": "", "근거": "", "위치": "없음", "확신도": "low",
                      "추출방법": "LLM_근거검증실패(폐기)", "판단": dec,
                      "폐기된_값": val[:60], "폐기된_근거": ev[:120]}
            continue

        out[f] = {"값": val, "근거": ev[:180], "위치": pos, "확신도": conf,
                  "추출방법": "LLM", "판단": dec}
    return out, 폐기


def agreement(rule: dict, llm: dict) -> dict:
    """규칙값 vs LLM값 비교. 두 추출기의 합의를 확신도 신호로 쓴다.

    형식 차이를 실질 불일치로 오분류하지 않도록 정규화한다:
      - 날짜  `2014년 3월 5일` == `2014-03-05` == `2014.3.5`
      - 부재  `해당사항 없음` 은 값이 아니라 부재로 본다
    """
    import re as _re

    def norm(s: str) -> str:
        s = (s or "").strip()
        if not s or s in ("해당사항 없음", "해당없음", "없음"):
            return ""
        m = _re.match(r"^\D*(\d{4})\D+(\d{1,2})\D+(\d{1,2})\D*$", s)
        if m:
            y, mo, d = (int(g) for g in m.groups())
            if 1900 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31:
                return f"{y:04d}{mo:02d}{d:02d}"
        return "".join(s.split()).replace("-", "").replace(".", "").lower()

    out = {}
    for f in ALL_FIELDS:
        rn = norm((rule.get(f, {}) or {}).get("값", ""))
        ln = norm((llm.get(f, {}) or {}).get("값", ""))
        if not rn and not ln:
            v = "양쪽_미검출"
        elif rn and not ln:
            v = "규칙만"
        elif ln and not rn:
            v = "LLM만"
        elif rn == ln:
            v = "일치"
        elif rn in ln or ln in rn:
            v = "부분일치"
        else:
            v = "불일치"
        out[f] = v
    return out


# ---------------------------------------------------------------- API

def load_env_key() -> str:
    """API 키를 확정한다. 없으면 즉시 중단."""
    _load_dotenv()
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key or not key.strip() or key.strip().lower() in {"your_api_key", "your-api-key", "your_api_key_here", "changeme"}:
        raise SystemExit("ANTHROPIC_API_KEY 를 환경변수 또는 저장소 루트 .env 에 설정하세요. 예시 키는 사용할 수 없습니다.")
    return key


def system_blocks() -> list[dict]:
    """프롬프트 캐시: system 은 고정이어야 한다. 날짜·id 를 절대 넣지 마라."""
    return [{"type": "text", "text": SYSTEM_PROMPT,
             "cache_control": {"type": "ephemeral"}}]


def build_repair_turn(detail: dict, base: date, 이전: dict, 불량: dict) -> str:
    """근거가 값을 뒷받침하지 못한 항목만 다시 찾게 하는 수리 요청 (ISS-017)."""
    lines = []
    for f, why in 불량.items():
        it = 이전.get(f) or {}
        lines.append(f"  - {f}: 값 '{it.get('값','')}' / 근거 '{(it.get('근거') or '')[:70]}'\n"
                     f"      → 문제: {why}")
    return (
        build_user_turn(detail, base) +
        "\n[재확인 요청] 아래 항목은 **근거가 값을 뒷받침하지 못한다.** 프로그램이 값의 핵심\n"
        "토큰(날짜 숫자·번호·이름·주소 앞부분)이 근거에 있는지 검사해 걸러낸 것이다.\n\n"
        + "\n".join(lines) +
        "\n\n원문을 다시 보고 해당 항목의 **실제 근거를 찾아라.**\n"
        "  · 값은 맞고 근거만 잘못 골랐다면 → 올바른 원문 조각을 근거로 제시\n"
        "  · 원문에 그 값의 근거가 없다면 → **값을 비우고** decision 을 규칙기각 또는 양쪽없음 으로\n"
        "  · 다른 항목은 이전 판단을 유지해라\n"
        "근거는 반드시 입력 텍스트를 그대로 복사한다. 지어내지 마라."
    )


def call_sync(client, detail: dict, base: date, *, blind: bool = False) -> tuple[dict, dict]:
    """단건 호출 → (tool_input, usage_dict)."""
    resp = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=system_blocks(),
        tools=[build_tool()],
        tool_choice={"type": "tool", "name": TOOL_NAME},
        messages=[{"role": "user", "content": build_user_turn(detail, base, blind=blind)}],
    )
    tool_input = {}
    for b in resp.content:
        if getattr(b, "type", "") == "tool_use" and b.name == TOOL_NAME:
            tool_input = b.input
            break
    u = resp.usage
    usage = {
        "input_tokens": getattr(u, "input_tokens", 0),
        "output_tokens": getattr(u, "output_tokens", 0),
        "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
        "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
        "stop_reason": resp.stop_reason,
    }
    return tool_input, usage


def cost_of(usage: dict) -> float:
    """usage → USD. `batch=True` 면 Batches API 의 **50% 할인**을 반영한다."""
    fresh = usage.get("input_tokens", 0)
    cw = usage.get("cache_creation_input_tokens", 0)
    cr = usage.get("cache_read_input_tokens", 0)
    out = usage.get("output_tokens", 0)
    inp = fresh + cw * 1.25 + cr * 0.10
    총 = inp / 1e6 * PRICE_IN + out / 1e6 * PRICE_OUT
    return 총 * 0.5 if usage.get("batch") else 총


def build_request(d: dict, base: date, *, blind: bool = False):
    """배치용 Request 하나. `call_sync` 와 **같은 파라미터**여야 결과가 일치한다."""
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request
    return Request(
        custom_id=batch.cid(str(d.get("언론사_id"))),
        params=MessageCreateParamsNonStreaming(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=system_blocks(),
            tools=[build_tool()],
            tool_choice={"type": "tool", "name": TOOL_NAME},
            messages=[{"role": "user", "content": build_user_turn(d, base, blind=blind)}],
        ),
    )


# ---------------------------------------------------------------- 실행

def load_details(workdir: Path, ids: set[str] | None) -> list[dict]:
    out = []
    for p in sorted((workdir / "work").glob("*.json")):
        if p.name.endswith(".llm.json"):
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        rid = str(d.get("언론사_id") or p.stem)
        if ids and rid not in ids:
            continue
        if not (d.get("하단_텍스트") or d.get("상단_텍스트")):
            continue          # 수집 실패·빈 페이지는 LLM 이 할 일이 없다
        out.append(d)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--ids", default="", help="쉼표 구분 언론사_id")
    # 윈도 명령줄 상한 32,767자. R1 대상 4,233건이면 약 25,400자라 여유가 없다.
    ap.add_argument("--ids-file", dest="ids_file", default="",
                    help="언론사_id 목록 파일 (쉼표·줄바꿈 구분)")
    ap.add_argument("--base-date", "--기준일", dest="base_date", default="")
    ap.add_argument("--sync", action="store_true", help="순차 호출 (소규모)")
    ap.add_argument("--blind", action="store_true",
                    help="규칙 1차결과를 주지 않고 독립 추출 — 앵커링 측정용")
    ap.add_argument("--dry-run", action="store_true", help="호출 없이 토큰·비용 추정")
    ap.add_argument("--batch-submit", action="store_true")
    ap.add_argument("--batch-collect", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--poll", type=int, default=batch.POLL_SEC,
                    help="배치 상태 폴링 간격(초)")
    args = ap.parse_args()

    base = (datetime.strptime(args.base_date, "%Y-%m-%d").date()
            if args.base_date else date.today())
    workdir = Path(args.workdir)
    _idsrc = args.ids
    if args.ids_file:
        _idsrc += "," + Path(args.ids_file).read_text(encoding="utf-8")
    ids = {s for s in re.split(r"[,\s]+", _idsrc) if s} or None
    details = load_details(workdir, ids)
    if args.limit:
        details = details[: args.limit]
    if not details:
        print(json.dumps({"error": "대상 없음 — run_extract 를 먼저 돌리고 work/*.json 을 확인해라"},
                         ensure_ascii=False)); sys.exit(1)

    if args.dry_run:
        import re as _re
        tot_in = 0
        for d in details:
            t = SYSTEM_PROMPT + build_user_turn(d, base)
            tot_in += int(len(t) / 1.7)      # 한국어 대략 1.7자/토큰
        est_out = len(details) * 900
        cost = (tot_in / 1e6 * PRICE_IN) + (est_out / 1e6 * PRICE_OUT)
        print(json.dumps({
            "모드": "dry-run (실제 호출 없음)", "모델": MODEL, "대상": len(details),
            "추정_입력토큰": tot_in, "추정_출력토큰": est_out,
            "추정비용_USD_캐시미적용": round(cost, 4),
            "전수8000_추정_USD": round(cost / len(details) * 8000, 2),
            "주의": "문자수 기반 거친 추정이다. --sync 1건으로 실측해라",
        }, ensure_ascii=False, indent=2)); return

    load_env_key()
    import anthropic
    client = anthropic.Anthropic()

    # ── 배치 제출 ──────────────────────────────────────────────────────────
    # 여기서 끝난다. 수집은 `--batch-collect` 로 따로 부른다 — 배치는 최대 24시간이라
    # 한 프로세스로 붙잡고 있으면 중간에 끊겼을 때 이미 낸 돈을 잃는다.
    if args.batch_submit:
        reqs = [build_request(d, base, blind=args.blind) for d in details]
        meta = batch.submit(client, workdir, "llm", base.isoformat(), reqs,
                            note=f"모델={MODEL} blind={args.blind}")
        meta["다음"] = "python llm_extract.py --workdir W --기준일 D --batch-collect"
        print(json.dumps(meta, ensure_ascii=False, indent=2))
        return

    # ── 배치 수집 ──────────────────────────────────────────────────────────
    prefetched: dict = {}
    배치실패: dict = {}
    if args.batch_collect:
        meta = batch.load_meta(workdir, "llm", base.isoformat())
        if not meta.get("batch_id"):
            print(json.dumps({"error": "배치 메타가 없다 — --batch-submit 을 먼저 돌려라"},
                             ensure_ascii=False)); sys.exit(1)
        st = batch.wait_ended(
            client, meta["batch_id"], poll=args.poll,
            on_tick=lambda w, s, rc: print(
                f"  배치 대기 {w}s status={s} "
                f"처리중={getattr(rc, 'processing', '?')} 완료={getattr(rc, 'succeeded', '?')}",
                file=sys.stderr))
        if st != "ended":
            print(json.dumps({"error": f"배치가 아직 끝나지 않았다 (status={st})",
                              "batch_id": meta["batch_id"]}, ensure_ascii=False)); sys.exit(3)
        prefetched, 배치실패 = batch.results(client, meta["batch_id"], TOOL_NAME)
        details = [d for d in details if str(d.get("언론사_id")) in prefetched]
        if not details:
            print(json.dumps({"error": "배치 결과에 대상이 없다", "실패": 배치실패},
                             ensure_ascii=False)); sys.exit(1)

    rows, usages, 폐기_전체 = [], [], {}
    판단_분포: dict[str, int] = {}
    for i, d in enumerate(details, 1):
        rid = str(d.get("언론사_id"))
        try:
            if rid in prefetched:
                raw, usage = prefetched[rid]        # 배치 결과 — 재호출하지 않는다
            else:
                raw, usage = call_sync(client, d, base, blind=args.blind)
        except Exception as exc:  # noqa: BLE001
            print(f"[{i}/{len(details)}] {rid} 실패: {exc.__class__.__name__}: {exc}",
                  file=sys.stderr)
            audit.record(workdir, rid, "5_LLM검토", "실패",
                         {"모델": MODEL, "예외": f"{exc.__class__.__name__}: {exc}"[:300],
                          "blind": args.blind},
                         제호=d.get("제호_등록"), 기준일=base.isoformat())
            continue
        llm, 폐기 = verify_findings(raw, d)

        # ISS-017 수리 루프 — 근거가 값을 뒷받침하지 못하면 그 항목만 다시 찾게 한다.
        # 확신도를 낮추고 넘기는 대신 LLM 이 직접 올바른 근거를 찾거나 값을 비우게 한다.
        불량 = evidence.audit_findings(llm, FIELDS)
        수리이력: dict = {}
        if 불량:
            try:
                raw2 = client.messages.create(
                    model=MODEL, max_tokens=MAX_TOKENS, system=system_blocks(),
                    tools=[build_tool()], tool_choice={"type": "tool", "name": TOOL_NAME},
                    messages=[{"role": "user",
                               "content": build_repair_turn(d, base, llm, 불량)}],
                )
                u2 = raw2.usage
                usages.append({
                    "input_tokens": u2.input_tokens, "output_tokens": u2.output_tokens,
                    "cache_creation_input_tokens": getattr(u2, "cache_creation_input_tokens", 0) or 0,
                    "cache_read_input_tokens": getattr(u2, "cache_read_input_tokens", 0) or 0,
                    "stop_reason": raw2.stop_reason})
                fixed = next((b.input for b in raw2.content
                              if getattr(b, "type", "") == "tool_use" and b.name == TOOL_NAME), {})
                llm2, 폐기2 = verify_findings(fixed, d)
                불량2 = evidence.audit_findings(llm2, FIELDS)
                # 수리 후에도 지지되지 않는 항목은 값을 버린다 — 근거 없는 값은 남기지 않는다
                for f, why in 불량2.items():
                    llm2[f] = {"값": "", "근거": (llm2.get(f) or {}).get("근거", ""),
                               "위치": "없음", "확신도": "low",
                               "추출방법": "LLM_근거미지지(수리후폐기)",
                               "판단": (llm2.get(f) or {}).get("판단", ""),
                               "미지지_이유": why}
                수리이력 = {"1차_미지지": 불량, "수리후_미지지": 불량2,
                         "해소": sorted(set(불량) - set(불량2))}
                llm, 폐기 = llm2, sorted(set(폐기) | set(폐기2))
            except Exception as exc:  # noqa: BLE001
                수리이력 = {"1차_미지지": 불량,
                         "수리실패": f"{exc.__class__.__name__}: {exc}"[:160]}
                for f in 불량:
                    llm[f] = {**(llm.get(f) or {}), "확신도": "low",
                              "추출방법": "LLM_근거미지지(수리실패)"}

        rule = d.get("findings") or {}
        agr = agreement(rule, llm)
        if 폐기:
            폐기_전체[rid] = 폐기
        usages.append(usage)
        for f in ALL_FIELDS:
            dec = (llm.get(f, {}) or {}).get("판단") or "(미기재)"
            판단_분포[dec] = 판단_분포.get(dec, 0) + 1

        (workdir / "work" / f"{rid}.llm.json").write_text(json.dumps({
            "언론사_id": rid, "제호_등록": d.get("제호_등록", ""), "모델": MODEL,
            "모드": "blind(독립)" if args.blind else "규칙1차결과_제공",
            "기준일": base.isoformat(), "usage": usage, "비용_USD": round(cost_of(usage), 5),
            "원응답": raw, "검증후_findings": llm, "근거검증_폐기항목": 폐기,
            "근거지지_수리이력": 수리이력,
            "규칙과_합의": agr, "notes": raw.get("notes", ""),
        }, ensure_ascii=False, indent=2), encoding="utf-8")

        # 검수용 감사 추적 — 항목별 판단·근거를 남긴다
        audit.record(workdir, rid, "5_LLM검토", "성공", {
            "모델": MODEL,
            "모드": "blind(독립)" if args.blind else "규칙1차결과_제공",
            "usage": usage, "비용_USD": round(cost_of(usage), 5),
            "항목별_판단": {f: {"판단": (llm.get(f, {}) or {}).get("판단", ""),
                          "규칙값": (rule.get(f, {}) or {}).get("값", ""),
                          "LLM값": (llm.get(f, {}) or {}).get("값", ""),
                          "위치": (llm.get(f, {}) or {}).get("위치", ""),
                          "확신도": (llm.get(f, {}) or {}).get("확신도", ""),
                          "근거": (llm.get(f, {}) or {}).get("근거", "")}
                       for f in ALL_FIELDS},
            "근거검증_폐기항목": 폐기,
            "근거지지_수리이력": 수리이력,
            "모델_notes": raw.get("notes", ""),
            "판단": "근거가 값을 뒷받침하지 못한 항목은 LLM 에 되돌려 다시 찾게 했다(ISS-017)",
        }, 제호=d.get("제호_등록"), 기준일=base.isoformat())

        row = {"언론사_id": rid, "제호_등록": d.get("제호_등록", ""),
               "근거검증_폐기": ", ".join(폐기), "notes": (raw.get("notes") or "")[:200]}
        for f in ALL_FIELDS:
            row[f"{f}_규칙"] = (rule.get(f, {}) or {}).get("값", "")
            row[f"{f}_LLM"] = (llm.get(f, {}) or {}).get("값", "")
            row[f"{f}_판단"] = (llm.get(f, {}) or {}).get("판단", "")
            row[f"{f}_합의"] = agr[f]
        필수_LLM = sum(1 for f in REQUIRED_FIELDS if (llm.get(f, {}) or {}).get("값"))
        필수_규칙 = sum(1 for f in REQUIRED_FIELDS if (rule.get(f, {}) or {}).get("값"))
        row["필수8_규칙"] = 필수_규칙
        row["필수8_LLM"] = 필수_LLM
        row["근거지지_수리"] = ", ".join(수리이력.get("해소", [])) if 수리이력 else ""
        rows.append(row)
        수리표시 = ""
        if 수리이력:
            수리표시 = (f"  [근거수리 시도 {len(수리이력.get('1차_미지지', {}))}건"
                    f" / 해소 {len(수리이력.get('해소', []))}건]")
        print(f"[{i}/{len(details)}] {rid} {d.get('제호_등록','')} "
              f"규칙 {필수_규칙}/8 → LLM {필수_LLM}/8"
              + (f"  (근거폐기: {', '.join(폐기)})" if 폐기 else "") + 수리표시,
              file=sys.stderr)

    cols = (["언론사_id", "제호_등록", "필수8_규칙", "필수8_LLM"]
            + [f"{f}_{s}" for f in ALL_FIELDS for s in ("규칙", "LLM", "판단", "합의")]
            + ["근거검증_폐기", "근거지지_수리", "notes"])
    out_csv = workdir / "outputs" / f"llm_compare_{base.isoformat()}.csv"
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    tot = {k: sum(u.get(k, 0) for u in usages) for k in
           ("input_tokens", "output_tokens", "cache_creation_input_tokens",
            "cache_read_input_tokens")}
    총비용 = sum(cost_of(u) for u in usages)
    합의분포: dict[str, int] = {}
    for r in rows:
        for f in ALL_FIELDS:
            v = r[f"{f}_합의"]
            합의분포[v] = 합의분포.get(v, 0) + 1

    print(json.dumps({
        "모델": MODEL, "대상": len(rows), "기준일": base.isoformat(),
        "모드": "blind(독립 추출)" if args.blind else "규칙1차결과_제공(순차)",
        "판단_분포": dict(sorted(판단_분포.items(), key=lambda x: -x[1])),
        "usage_합계": tot,
        "총비용_USD": round(총비용, 5),
        "건당_평균_USD": round(총비용 / max(1, len(rows)), 5),
        "전수8000_추정_USD": round(총비용 / max(1, len(rows)) * 8000, 2),
        "전수8000_배치적용_USD": round(총비용 / max(1, len(rows)) * 8000 * 0.5, 2),
        "필수8_규칙_합계": sum(r["필수8_규칙"] for r in rows),
        "필수8_LLM_합계": sum(r["필수8_LLM"] for r in rows),
        "합의_분포": dict(sorted(합의분포.items(), key=lambda x: -x[1])),
        "근거검증_폐기": 폐기_전체,
        "compare_csv": str(out_csv),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
