"""CSV/xlsx 컬럼 스키마 + 행 조립·플래그 계산 (extract → verify → final 공통)."""
from __future__ import annotations
import re

from .fields import EXTRA_FIELDS, FIELDS, REQUIRED_FIELDS, to_iso
from .registry import COMPARE_FIELDS

# 날짜를 담는 항목 — 최종 산출물은 전부 YYYY-MM-DD 로 통일한다.
# 규칙은 정규화하는데 LLM 은 사이트 표기를 그대로 돌려주므로 병합 후 포맷이 섞인다.
DATE_FIELDS = ("등록연월일", "발행연월일", "창간일")

BASE_COLUMNS = ["언론사_id", "제호_등록", "메인_url", "접속결과", "HTTP상태", "최종URL",
                "리다이렉트", "도메인변경", "파킹의심", "접속경로", "js_의심", "운영상태",
                # 지침 I — 홈페이지 운영현황. 이 관문을 '있음' 으로 통과한 매체만
                # 필요적 게재사항(지침 II) 점검 대상이다. md_core/operation.py 가 채운다.
                "홈페이지_운영현황", "운영현황_로직", "매체명_동일", "뉴스_있음", "운영현황_사유"]
FIELD_SUFFIXES = ["게재값", "위치", "확신도", "추출방법", "근거"]
TAIL_COLUMNS = ["필수8_게재수", "미게재_항목", "준수여부_8항목", "불일치_항목",
                # 지침 R8 비고("어떤 날짜를 확인했는지 근거 필요") — 항목별 판단근거를
                # 한 칸에 모은다. 창간일로 발행연월일을 인정했는지, 교차규칙으로 전이했는지,
                # 청소년보호책임자로 발행인을 대체했는지가 여기서 한눈에 보여야 한다.
                "판단근거",
                # 이미지(로고)로만 표기된 항목 — 게재로 인정하되 사람 확인을 받는다
                "이미지표기_항목",
                # 비전 진단의 원인 진단. 이게 산출물에 없으면 검수자가 '왜 미검출인지' 를 모른다.
                # pilot3 30887 은 도메인 만료(expireddomains.com 리다이렉트)를 비전이 정확히
                # 진단했는데 사유에 '미검출 8항목' 만 남아 폐업 매체가 미준수로 찍혔다.
                "진단_원인",
                # 규칙 단독 기준 동결 스냅샷 — merge_llm 이 한 번만 쓰고 apply_flags 는
                # 건드리지 않는다. 항목값을 LLM 최종값으로 갱신하면 준수여부_8항목 은
                # 하류 apply_flags 재호출로 최종 판정과 같아지므로, 대조 기준을 따로 남긴다.
                "필수8_게재수_규칙", "준수여부_규칙",
                # LLM 종합 판정 — 규칙 1차결과를 LLM 에 넣고 받은 결과가 최종 정답이다.
                # 준수여부_8항목(규칙 기준)은 대조·감사용으로 남긴다.
                "필수8_게재수_LLM", "준수여부_LLM", "미게재_항목_LLM", "판정출처",
                "LLM_판단요약", "규칙_LLM_차이",
                # 발행연월일 = 최신 기사 업데이트 날짜. 게재(21조)와 최신성(운영)을 분리한다.
                "발행연월일_실제", "발행연월일_경과일", "발행연월일_확인경로",
                # 등록정보 대조는 **제21조 게재의무와 다른 사안**이다 — 사이트 표기가 정확한데
                # 등록원부가 낡은 것이므로 제9조 변경등록 미이행 의심이다. 게재의무 판정의
                # `확인필요` 에 섞으면 검수자가 우선순위를 정할 수 없다(pilot3: 확인필요 23건 중
                # 대조 단독이 15건). 별도 플래그로 분리해 별도 시트에서 다룬다.
                "등록정보_상이", "등록정보_상이_사유",
                # 날짜 정규화로 값 표기가 바뀐 건의 원 표기. 사이트가 실제로 어떻게 적었는지는
                # 근거·evidence 에도 남지만, 여기 모아두면 검수자가 한 컬럼으로 확인한다.
                "날짜_원표기",
                "검색_판정", "검색_후보URL", "확인필요", "확인필요_사유",
                # 증빙 파일 경로와 누락 사유 (ISS-033). 행정 처분은 증빙으로 다투므로
                # 판정 행에서 파일로 갈 수 있어야 한다.
                "증빙_경로", "증빙_누락", "비고"]

# 접속 불가 건의 검색 단계 판정값 (resolve_failed.py 가 기록)
검색_후보발견 = "제호일치_후보발견"
검색_미발견 = "제호일치_미발견"
검색_도메인변경 = "도메인변경_확인"
검색_도메인만료 = "도메인만료_폐업의심"
검색_대상아님 = "검색대상아님"
검색_서버다운 = "서버다운_재시도필요"


def extract_columns() -> list[str]:
    cols = list(BASE_COLUMNS)
    for f in FIELDS:
        cols += [f"{f}_{s}" for s in FIELD_SUFFIXES]
    return cols + TAIL_COLUMNS


def verify_columns() -> list[str]:
    cols = list(BASE_COLUMNS)
    for f in FIELDS:
        cols += [f"{f}_{s}" for s in FIELD_SUFFIXES]
        if f in COMPARE_FIELDS:
            cols += [f"{f}_등록값", f"{f}_대조"]
    return cols + TAIL_COLUMNS


def 판단근거_요약(findings: dict) -> str:
    """항목별 `판단근거` 를 한 칸에 모은다 (지침 R8 비고).

    라벨을 그대로 읽은 평범한 건은 적지 않는다 — **대체 인정·교차규칙으로 준수가 된 건만**
    남겨야 검수자가 볼 것을 고른다. 그게 이 컬럼의 용도다.
    """
    쓸것 = []
    for f in FIELDS:
        근거 = ((findings.get(f) or {}).get("판단근거") or "").strip()
        방법 = ((findings.get(f) or {}).get("추출방법") or "")
        if not 근거:
            continue
        if 방법 == "라벨" and "라벨" in 근거:      # 명시 라벨을 읽은 평범한 건은 생략
            continue
        쓸것.append(f"{f}={근거}")
    return " / ".join(쓸것)


def row_from_extract(meta: dict, result: dict) -> dict:
    row = {c: "" for c in extract_columns()}
    row.update({k: v for k, v in meta.items() if k in row})
    for f in FIELDS:
        r = result.get(f, {})
        row[f"{f}_게재값"] = r.get("값", "")
        row[f"{f}_위치"] = r.get("위치", "")
        row[f"{f}_확신도"] = r.get("확신도", "")
        row[f"{f}_추출방법"] = r.get("추출방법", "")
        row[f"{f}_근거"] = (r.get("근거", "") or "")[:180]
    row["판단근거"] = 판단근거_요약(result)[:400]
    apply_flags(row)
    return row


def normalize_dates(row: dict) -> dict:
    """행의 모든 날짜 컬럼을 `YYYY-MM-DD` 로 통일한다.

    `apply_flags` 맨 앞에서 호출되므로 extract·verify 양쪽 경로가 같은 포맷을 본다.
    빈 값은 건드리지 않으므로 게재수 계산(값이 있느냐)에는 영향이 없다.
    표기가 바뀐 건은 `날짜_원표기` 에 `항목=원값` 으로 남긴다.
    """
    바뀜 = []
    대상 = []
    for f in DATE_FIELDS:
        대상 += [(f, f"{f}_게재값"), (f"{f}(등록)", f"{f}_등록값")]
    대상 += [("발행연월일(실제)", "발행연월일_실제")]
    for 이름, col in 대상:
        if col not in row:
            continue
        old = row.get(col)
        old_s = ("" if old is None else str(old)).strip()
        if not old_s:
            continue
        new = to_iso(old)
        if new and new != old_s:
            row[col] = new
            바뀜.append(f"{이름}={old_s}")
    # 키가 없어도 만든다. merge_llm 은 기존 CSV 행(DictReader)에서 행을 만들 수 있어
    # 새 컬럼이 dict 에 없을 수 있다 — `in row` 로 막으면 조용히 기록을 잃는다(실측).
    if 바뀜:
        row["날짜_원표기"] = " / ".join(바뀜)
    return row


def apply_flags(row: dict) -> dict:
    """필수 8항목 게재수·준수여부·확인필요 사유 계산.

    사유는 단계 표지([수집]/[검색]/[추출]/[대조])를 붙여 누적한다. 이 함수는 extract 단계와
    verify 단계에서 모두 호출되므로, 사유를 문자열로 이어붙이지 않고 매번 행의 컬럼값에서
    재도출한다 — 그래야 단계가 진행돼도 앞 단계 사유가 유실되지 않는다.

    준수여부_8항목 3분류 (2026-08-14 지침 반영):
      준수        — 8항목 전부 게재
      미준수      — 홈페이지 있음(지침 I 통과)인데 일부 미게재
      홈페이지없음 — 지침 I 에서 '없음' 으로 걸러짐 (접속불가·파킹·매체명상이+뉴스없음·검색미발견)

    종전의 `판정불가` 는 지침에 없는 분류라 폐기하고 `홈페이지없음` 으로 흡수했다.
    **`홈페이지없음` 은 미준수가 아니다** — 게재 여부를 판단할 화면 자체가 없으므로
    과태료 대상으로 집계하면 안 된다. 지침 I 의 "홈페이지 없음 → 포털검색" 경로에 해당한다.
    """
    # ★ 판정 전에 날짜 포맷을 통일한다 — 하류(대조·통계·엑셀 정렬)가 모두 이 값을 쓴다
    normalize_dates(row)

    미게재, 저확신, 위치기타, 불일치 = [], [], [], []
    for f in REQUIRED_FIELDS:
        val = (row.get(f"{f}_게재값") or "").strip()
        if not val or val == "해당사항 없음":
            미게재.append(f)
            continue
        if (row.get(f"{f}_확신도") or "") == "low":
            저확신.append(f)
        # 발행연월일은 기사목록 표기가 정상이므로 위치=기타를 문제로 보지 않는다
        if (row.get(f"{f}_위치") or "") == "기타" and f != "발행연월일":
            위치기타.append(f)
    for f in COMPARE_FIELDS:
        if (row.get(f"{f}_대조") or "") in ("불일치", "부분일치"):
            불일치.append(f"{f}({row[f'{f}_대조']})")

    게재수 = len(REQUIRED_FIELDS) - len(미게재)
    row["필수8_게재수"] = 게재수
    row["미게재_항목"] = ", ".join(미게재)
    row["불일치_항목"] = ", ".join(불일치)

    접속불가 = row.get("접속결과") not in ("가능", "", None)
    검색판정 = (row.get("검색_판정") or "").strip()
    후보url = (row.get("검색_후보URL") or "").strip()
    파킹 = (row.get("파킹의심") or "").strip() == "Y"

    # 지침 I 의 관문. run_extract 가 md_core/operation.py 로 채워 둔다.
    # 비어 있으면(구 CSV·부분 재실행) 접속 신호만으로 보수적으로 되짚는다.
    운영현황 = (row.get("홈페이지_운영현황") or "").strip()
    if not 운영현황:
        운영현황 = "없음" if (접속불가 or 파킹) else "있음"
        row["홈페이지_운영현황"] = 운영현황
        if not (row.get("운영현황_로직") or "").strip():
            row["운영현황_로직"] = "I-4" if 운영현황 == "없음" else "I-1"

    # 포털검색으로도 제호 일치 사이트를 못 찾으면 홈페이지 없음이 **확정**된다 (지침 I-3/I-4).
    # 종전에는 이 경우를 '미준수' 로 찍었으나(과태료 대상), 지침 체계에서 홈페이지 없음은
    # 필요적 게재사항 점검 대상 자체가 아니다.
    if 검색판정 == 검색_미발견:
        운영현황 = "없음"
        row["홈페이지_운영현황"] = "없음"

    if 운영현황 == "없음":
        row["준수여부_8항목"] = "홈페이지없음"
    else:
        row["준수여부_8항목"] = "준수" if 게재수 == len(REQUIRED_FIELDS) else "미준수"

    사유 = []
    운영사유 = (row.get("운영현황_사유") or "").strip()
    if 운영현황 == "없음":
        사유.append(f"[운영현황] 홈페이지 없음({row.get('운영현황_로직') or 'I-4'}) — "
                  f"필요적 게재사항 점검 대상 아님. 미준수가 아니다"
                  + (f" / {운영사유[:150]}" if 운영사유 else ""))
    elif 운영사유 and (row.get("운영현황_로직") or "") != "I-1":
        # I-2(매체명 상이·뉴스 있음)로 통과한 건은 사람이 매체 동일성을 확인해야 한다
        사유.append(f"[운영현황] {운영사유[:180]}")
    if 접속불가:
        사유.append(f"[수집] 접속실패(HTTP {row.get('HTTP상태') or '연결불가'})")
    # 도메인 파킹·판매 페이지로 리다이렉트된 건은 '미게재' 가 아니라 폐업·도메인 소멸이다.
    # 이 신호가 없으면 검수자가 미검출 8항목만 보고 과태료 대상으로 오판한다(pilot3 30887).
    if (row.get("파킹의심") or "").strip() == "Y":
        사유.append(f"[수집] 도메인 파킹·판매 페이지로 리다이렉트 "
                  f"({(row.get('최종URL') or '')[:60]}) — 폐업·도메인 소멸 의심. "
                  f"미게재가 아니다")
    elif (row.get("도메인변경") or "").strip() == "Y":
        사유.append(f"[수집] 입력 URL 과 다른 도메인으로 리다이렉트 "
                  f"({(row.get('최종URL') or '')[:60]}) — 도메인 변경 확인 필요")
    진단 = (row.get("진단_원인") or "").strip()
    if 진단:
        사유.append(f"[비전] {진단[:180]}")
    if "ssl무검증" in (row.get("접속경로") or ""):
        사유.append("[수집] 인증서 검증 우회로 본문 확보 — 인증서 자체는 점검항목 아님")
    if "스킴교체" in (row.get("접속경로") or ""):
        사유.append("[수집] 입력 URL 스킴으로는 실패, 반대 스킴으로 성공 — 입력 URL 갱신 권장")
    if 검색판정 == 검색_도메인변경:
        사유.append(f"[검색] 필요적 게재사항이 일치하는 사이트 확인({후보url}) — 도메인 변경. "
                  f"지침 I 비고에 따라 **이 URL 로 재수집해 진행**해야 한다")
    elif 검색판정 == 검색_후보발견:
        사유.append(f"[검색] 제호 일치 후보 발견({후보url}) — 게재사항 5항목 중 일치가 없어 "
                  f"동일 매체 확인 실패. 사람 확인 필요")
    elif 검색판정 == 검색_미발견:
        사유.append("[검색] 제호 일치 사이트 미발견 — 지침 I 의 포털검색으로도 못 찾았다. "
                  "홈페이지 없음 확정")
    elif 검색판정 == 검색_도메인만료:
        사유.append("[검색] 도메인 만료·폐업 의심 — DNS 해석 실패이고 새 도메인 흔적도 없다. "
                  "재시도 대상이 아니다")
    elif 검색판정 == 검색_서버다운:
        사유.append("[검색] 사이트는 존재하나 접속 장애 — 재시도 필요")
    elif 검색판정 == 검색_대상아님:
        사유.append("[검색] 검색 대상 아님 — HTTP 응답은 정상이고 본문만 비었다. "
                  "렌더링·차단 확인 필요")
    elif 접속불가:
        사유.append("[검색] 미수행 — resolve_failed.py 로 제호 검색 필요")
    if str(row.get("js_의심")).lower() in ("true", "1", "yes"):
        사유.append("[추출] JS의심(텍스트부족)")
    # 이미지 표기는 게재로 인정하되(field_rules.md 2026-07-31 결정) 사람 확인을 받는다
    이미지 = (row.get("이미지표기_항목") or "").strip()
    if 이미지:
        사유.append(f"[비전] 이미지(로고) 표기로 게재 인정: {이미지} — 스크린샷 증빙 확인 필요")
    # 증빙이 없으면 판정을 처분 근거로 쓸 수 없다. 접속 불가 건은 애초에 찍을 화면이
    # 없으므로(이미 홈페이지없음) 중복 표시하지 않는다.
    증빙누락 = (row.get("증빙_누락") or "").strip()
    if 증빙누락 and 운영현황 == "있음":
        사유.append(f"[증빙] {증빙누락[:200]}")
    if 미게재 and 운영현황 == "있음":
        사유.append(f"[추출] 미검출 {len(미게재)}항목")
    if 저확신:
        사유.append("[추출] 저확신: " + ", ".join(저확신))
    if 위치기타:
        사유.append("[추출] 위치=기타: " + ", ".join(위치기타))
    # ★ 대조 결과는 확인필요_사유에 넣지 않는다. 등록정보_상이 로 분리한다.
    if 불일치:
        row["등록정보_상이"] = "Y"
        row["등록정보_상이_사유"] = (
            "[제9조 변경등록 확인 대상] " + ", ".join(불일치)
            + " — 사이트 표기와 등록원부가 다르다. 게재의무(제21조) 위반이 아니다")
    else:
        row["등록정보_상이"] = ""
        row["등록정보_상이_사유"] = ""
    # 발행연월일 최신성 — 21조 게재 위반이 아니라 운영 중단 의심 신호다
    try:
        경과 = int(row.get("발행연월일_경과일") or 0)
    except (TypeError, ValueError):
        경과 = 0
    if 경과 >= 180:
        사유.append(f"[발행일] 최신 기사 {경과}일 경과 — 운영 중단 의심(게재 위반 아님, "
                  f"홈페이지 운영현황 점검과 대조 필요)")
    row["확인필요"] = "Y" if 사유 else "N"
    row["확인필요_사유"] = " / ".join(사유)
    return row


__all__ = ["BASE_COLUMNS", "TAIL_COLUMNS", "FIELDS", "REQUIRED_FIELDS", "EXTRA_FIELDS",
           "COMPARE_FIELDS", "extract_columns", "verify_columns", "row_from_extract", "apply_flags"]


# ── 엑셀 셀 정화 (ISS-071) ──────────────────────────────────────────────────
# openpyxl 은 XML 1.0 이 금지하는 제어문자를 만나면 셀 하나 때문에 **파일 전체 생성을
# 중단**한다. 우리는 사이트 DOM 에서 긁은 텍스트를 그대로 셀에 넣으므로 언제든 걸린다.
# 실제로 위치지도가 이 예외로 통째로 안 만들어졌다 — 대구 한 매체의 주소 문자열에
# 눈에 보이지 않는 제어문자가 하나 섞여 있었다.
#
# 지우지 않고 **공백으로 바꾼다.** 지우면 앞뒤 단어가 붙어 원문과 달라지고, 그 값이
# 위치지도의 `일치문구`·`주변문맥` 이라 검수자가 화면에서 못 찾게 된다.
_XLSX_BAD = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def xlsx_safe(v):
    """엑셀 셀에 넣어도 되는 값으로 바꾼다. 문자열이 아니면 그대로 돌려준다."""
    if isinstance(v, str):
        return _XLSX_BAD.sub(" ", v)
    return v


def xlsx_safe_row(row):
    """행(list 또는 dict)의 모든 문자열 셀을 정화한다."""
    if isinstance(row, dict):
        return {k: xlsx_safe(v) for k, v in row.items()}
    return [xlsx_safe(v) for v in row]
