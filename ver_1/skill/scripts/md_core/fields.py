"""필요적 게재사항 항목별 룰 추출.

각 항목 결과: {"값", "근거"(원문 스니펫), "위치"(하단/상단/기타/없음), "확신도"(high/mid/low),
"추출방법", "판단근거"}
판정기준·라벨 변형은 references/field_rules.md 와 1:1 대응 — 그 문서는 2026-08-13 지침
xlsx 를 구현한 것이다. 룰이 못 잡으면 빈 값 + 확신도 low 로 남겨 사람(또는 override)이
채우게 한다 — 억지로 값을 만들지 않는다.

`판단근거` 는 지침 R8 비고("어떤 날짜를 확인했는지 근거 필요")를 만족시키기 위한 필드다.
날짜 3항목(등록연월일·발행연월일·창간일)에서 **무엇을 보고 인정했는가** 를 담고,
교차 준수 규칙(`apply_cross_rules`)이 이 값을 읽어 전이 가능 여부를 가른다.
"""
from __future__ import annotations

import re
from datetime import date, datetime

FIELDS = ["제호", "제호 영문", "등록번호", "등록연월일", "발행인", "편집인",
          "발행연월일", "발행소 주소", "발행소 전화번호", "창간일"]
REQUIRED_FIELDS = ["제호", "등록번호", "등록연월일", "발행인", "편집인",
                   "발행연월일", "발행소 주소", "발행소 전화번호"]
EXTRA_FIELDS = ["제호 영문", "창간일"]
ZONES = ("하단", "상단", "기타")

# --- 라벨 ---
LABEL = {
    "등록번호": r"(?:인터넷\s*)?(?:신문\s*)?등\s*록\s*번\s*호|정기간행물\s*등록\s*번호|등록\s*No",
    "등록연월일": r"등\s*록\s*(?:연월일|년월일|일자|날짜|일)",
    "발행인": r"발\s*행\s*[·∙・/,]?\s*편\s*집\s*인|발\s*행\s*인\s*(?:겸|및)\s*편\s*집\s*인|발\s*행\s*인|발\s*행\s*자",
    "편집인": r"편\s*집\s*[·∙・/,]?\s*발\s*행\s*인|편\s*집\s*인\s*(?:겸|및)\s*발\s*행\s*인|편\s*집\s*인|편\s*집\s*책\s*임\s*자",
    "발행소 주소": r"발\s*행\s*소\s*주\s*소|본\s*사\s*주\s*소|주\s*소|발\s*행\s*소|소\s*재\s*지|사업장\s*주소|Address",
    "발행소 전화번호": r"대\s*표\s*전\s*화|전\s*화\s*번\s*호|전\s*화|연\s*락\s*처|TEL|Tel|Phone",
    "창간일": r"창\s*간\s*(?:연월일|일자|일)?",
    "제호": r"제\s*호|신\s*문\s*명|매\s*체\s*명|사\s*이\s*트\s*명|제호명",
    "발행연월일": r"발\s*행\s*(?:연월일|년월일|일자|일)|최종\s*(?:수정|업데이트)|업데이트",
}
ANY_LABEL = re.compile("|".join(f"(?:{p})" for p in LABEL.values()) +
                       r"|팩\s*스|FAX|Fax|이\s*메\s*일|E-?mail|사업자\s*등록|청소년\s*보호|고충\s*처리|"
                       r"개인정보|기사배열|저작권|Copyright|All\s*rights")

# ── 지침 R13-2 / R15-2: 발행인·편집인 대체 인정 라벨 ────────────────────────────
#
# > "홈페이지 내 이름을 기재하는 항목이 **하나라도** 기재된 경우(대표, 편집국장, 편집인,
# >  청소년보호책임자 등) → 준수"
#
# 종전 기준은 이 라벨들을 '필요적 게재사항 8항목이 아니다' 라며 명시적으로 **배제**했다.
# 지침은 정반대이므로 대체 인정으로 바꾼다. 다만 어떤 라벨로 인정했는지를 `판단근거` 에
# 남기고 확신도를 낮춰 사람이 검수하게 한다 — 발행인 본인이 아닐 수 있기 때문이다.
#
# 순서가 중요하다: `대표이사` 를 `대표` 보다 먼저 봐야 이름을 '이사'로 자르지 않는다.
NAME_ALT_LABEL: list[tuple[str, str]] = [
    ("대표이사", r"대\s*표\s*이\s*사"),
    ("대표자", r"대\s*표\s*자"),
    ("편집국장", r"편\s*집\s*국\s*장"),
    ("보도국장", r"보\s*도\s*국\s*장"),
    ("편집위원장", r"편\s*집\s*위\s*원\s*장"),
    ("청소년보호책임자", r"청\s*소\s*년\s*보\s*호\s*책\s*임\s*자"),
    ("기사배열책임자", r"기\s*사\s*배\s*열\s*책\s*임\s*자"),
    ("고충처리인", r"고\s*충\s*처\s*리\s*(?:인|담당자?)"),
    ("대표", r"대\s*표(?![이자])"),
]

# ── 지침 R7-2 / R10-2 의 유일한 제외: '투데이' 형 실시간 현재날짜 표기 ──────────
#
# > "(투데이와 같이 현재 날짜를 알려주는 것 제외)"
#
# 이걸 인정하면 오늘 날짜 위젯을 단 모든 매체가 자동으로 발행연월일·등록연월일 준수가 된다.
#
# 판별이 까다롭다. 단순히 앞뒤 20자에 '투데이' 가 있으면 배제하는 방식은 **매체명에 걸린다** —
# `뉴스투데이`·`머니투데이`·`스포츠투데이` 는 실재하는 제호이고, 그 옆의 기사 날짜가 통째로
# 사라진다(실측: 기사목록 2026-07-25 가 '뉴스투데이' 때문에 배제됐다).
#
# 두 조건을 **함께** 만족할 때만 시계로 본다:
#   ① 표지가 날짜 **바로 앞뒤**에 붙어 있다 (사이에 구분자만 허용)
#   ② 표지가 한글 음절에 이어 붙지 않았다 (`뉴스투데이` 의 '투데이' 는 표지가 아니다)
#   ③ 그 날짜가 **기준일과 같다** — 현재 날짜를 알려주는 위젯은 정의상 오늘을 가리킨다
TODAY_MARK = re.compile(r"(?:^|[^가-힣A-Za-z])(?:투\s*데\s*이|오\s*늘|현\s*재\s*시\s*[각간]|today)$",
                        re.I)
TODAY_SEP = re.compile(r"[\s:：=|/·∙・>\-–—\[\]()]+$")
TODAY_LOOK = 14            # 날짜 바로 앞뒤로 볼 글자 수

# --- 값 패턴 ---
REGNUM = re.compile(
    r"((?:[가-힣]{2,5}(?:특별시|광역시|특별자치시|특별자치도|도)?\s*[,·]?\s*)?"
    r"[아가나다라마바사자차카타파하]\s*[-]?\s*0*\d{1,6}\s*호?)"
)
# 라벨 없이 찾을 때 쓰는 엄격판 — 지침 R16 "지역명 + 아 + 숫자 5자리".
#
# 판별자는 **자릿수가 아니라 종별문자 `아`** 다. 인터넷신문 등록의 종별문자는 `아` 이고,
# 0 패딩을 생략한 `강원 아4` 같은 정당한 표기가 실재하므로(field_rules.md 대조 규칙 3)
# 자릿수로 막으면 진짜 값을 버린다.
#   허용: `강원 아4` · `강원아00423` · 줄머리의 `아00093`
#   기각: `새롬프라자 4층` 의 `자4` · 기사 '…협회)가 2024-25시즌' 의 `가2024`
#         · '동아 4층' 처럼 한글 낱말 끝의 `아` (2026-09-02 실측 오탐)
_REG_SIDO = (r"(?:서울|부산|대구|인천|광주|대전|울산|세종|경기|강원|충북|충남|전북|전남|경북|경남|제주)"
             r"(?:특별자치시|특별시|광역시|특별자치도|도|시)?")
REGNUM_STRICT = re.compile(
    r"(" + _REG_SIDO + r"\s*[,·]?\s*아\s?-?\s?0*\d{1,6}\s*호?"      # 지역명이 붙은 형태
    r"|(?<![가-힣])아\s?-?\s?\d{3,6}\s*호?)"                        # 지역명 없으면 3자리 이상
)
PHONE = re.compile(r"(0\d{1,3}\s*[-.)]\s*\d{3,4}\s*[-.]\s*\d{4}|0\d{9,10}|1[5-9]\d{2}\s*[-.]?\s*\d{4})")
# `00-0000-0000`, `000-0000-0000` 같은 플레이스홀더. 매체가 자리만 잡아둔 것이므로
# 게재로 인정하면 안 된다 (강원웰니스신문 실측 — 하단에 `등록번호 : 00-0000-00000`).
PHONE_PLACEHOLDER = re.compile(r"^0+$")
DATE_FULL = re.compile(r"(20\d{2}|19\d{2})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})\s*일?")
DATE_YM = re.compile(r"(20\d{2}|19\d{2})\s*[.\-/년]\s*(\d{1,2})\s*월?(?![\d.\-/])")
NAME = re.compile(r"((?:\([주유사]\)\s*)?[가-힣]{2,6}(?:\s[가-힣]{2,6})?)")
ADDR_TOKEN = re.compile(r"(시|도|군|구|읍|면|동|리|로|길|가|층|호|번지)\b|[시도군구]\s")
EN_TITLE = re.compile(r"\b([A-Z][A-Za-z.]*(?:\s+[A-Z][A-Za-z.]*){0,3})\b")
EN_KEYWORD = re.compile(r"news|daily|times|press|ilbo|sinmun|shinmun|media|journal|post|today|tv|"
                        r"herald|tribune|report|wire|net|economy|biz", re.I)
EN_STOP = {"COPYRIGHT", "ALL", "RIGHTS", "RESERVED", "TEL", "FAX", "EMAIL", "E-MAIL", "HOME",
           "LOGIN", "MENU", "SEARCH", "ABOUT", "CONTACT", "US", "PC", "MOBILE", "TOP", "SNS",
           "RSS", "URL", "HTTP", "HTTPS", "WWW", "AM", "PM", "NO", "ID", "PW", "OK", "CEO"}
JUNK_VALUES = {"없음", "미상", "해당없음", "해당사항없음", "-", "무", "n/a", "na", "정보없음"}

NAME_SUFFIX = re.compile(r"\s*(기자|대표|사장|회장|국장|편집장|이사|부장|팀장|씨|님)$")
SEP_LEAD = re.compile(r"^[\s:：=|/·∙・>\-–—]+")

# 라벨 뒤 창에서 이름을 뽑을 때 이름일 리 없는 낱말. 이름 항목 대체 인정(지침 R13-2/R15-2)을
# 켜면 `대표전화 : 033-…` 의 '전화' 가 발행인으로 잡히는 사고가 난다.
NAME_STOP = {"전화", "번호", "메일", "이메일", "팩스", "주소", "회사", "소개", "약관", "정보",
             "고객", "센터", "문의", "안내", "광고", "제휴", "구독", "기사", "뉴스", "본사",
             "지사", "지역", "연락", "제보", "운영", "관리", "등록", "발행", "편집", "홈페이지"}


def _snippet(text: str, start: int, end: int, width: int = 45) -> str:
    s = max(0, start - width)
    e = min(len(text), end + width)
    return re.sub(r"\s+", " ", text[s:e]).strip()


def _window(text: str, end: int, size: int = 90) -> str:
    """라벨 뒤 창. 값이 다음 줄에 오는 표 레이아웃도 허용(줄바꿈 1회까지)."""
    w = text[end:end + size]
    w = SEP_LEAD.sub("", w)
    nl = w.find("\n")
    if nl == 0:
        w = SEP_LEAD.sub("", w[1:])
        nl = w.find("\n")
    return w[:nl] if nl > 0 else w


def _cut_at_next_label(seg: str) -> str:
    m = ANY_LABEL.search(seg)
    return seg[: m.start()] if m else seg


def norm_date(y: str, m: str, d: str | None = None) -> str:
    try:
        yy, mm = int(y), int(m)
        if not (1 <= mm <= 12):
            return ""
        if d is None:
            return f"{yy:04d}-{mm:02d}"
        dd = int(d)
        if not (1 <= dd <= 31):
            return ""
        return f"{yy:04d}-{mm:02d}-{dd:02d}"
    except ValueError:
        return ""


def _valid_iso(y: str, mo: str, d: str) -> str:
    """실제 존재하는 날짜일 때만 ISO 문자열. 2018-02-30 같은 값은 버린다."""
    try:
        return date(int(y), int(mo), int(d)).isoformat()
    except (TypeError, ValueError):
        return ""


_XL_TIME = re.compile(r"[ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?$")
_ISO_LOOSE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_YMD8 = re.compile(r"^(19\d{2}|20\d{2})(\d{2})(\d{2})$")
_YEAR_ONLY = re.compile(r"^(19\d{2}|20\d{2})\s*년?$")


def to_iso(s) -> str:
    """자유 표기 날짜를 `YYYY-MM-DD` 로 정규화한다. 못 읽으면 원문을 그대로 돌려준다.

    왜 필요한가
    -----------
    최종 산출물의 날짜 컬럼은 포맷이 하나여야 한다. 규칙 추출은 `norm_date` 로 정규화하지만
    LLM 은 사이트 표기를 그대로 돌려주고 `merge_llm` 이 그 값으로 덮어쓴다. 그래서 pilot3 의
    `등록연월일_게재값` 한 컬럼에 네 가지 포맷이 섞였다 —
    `2015-02-03`(11건) / `2018년 8월 2일`(7건) / `2017.09.11`(1건) / `2017. 09. 11`(1건).
    이 상태로는 엑셀 정렬·필터·기간 집계가 전부 깨지고, 8,000건 통계를 낼 수 없다.

    원 표기는 버리지 않는다 — `근거` 컬럼과 `evidence/{id}.json` 의 `일치문구` 에 남아 있고,
    정규화로 값이 바뀐 건은 `날짜_원표기` 컬럼에 함께 적는다.

    읽지 못한 값은 **원문을 그대로 남긴다.** 날짜가 아닌 문자열을 억지로 날짜로 만들지 않는다.
    """
    if s is None:
        return ""
    # openpyxl 이 셀을 datetime 으로 읽어오면 '2015-02-03 00:00:00' 이 된다
    if isinstance(s, (datetime, date)):
        return (s.date() if isinstance(s, datetime) else s).isoformat()
    raw = str(s).strip()
    if not raw:
        return ""
    raw = _XL_TIME.sub("", raw).strip()

    m = _ISO_LOOSE.match(raw)          # 2015-2-3 → 2015-02-03
    if m:
        return _valid_iso(*m.groups()) or raw
    m = _YMD8.match(raw)               # 20150203
    if m:
        return _valid_iso(*m.groups()) or raw
    m = DATE_FULL.search(raw)          # 2018년 8월 2일 / 2017. 09. 11 / 2019/11/13
    if m:
        return _valid_iso(*m.groups()) or raw
    m = DATE_YM.search(raw)            # 연월만 — 없는 '일' 을 만들어내지 않는다
    if m and 1 <= int(m.group(2)) <= 12:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}"
    m = _YEAR_ONLY.match(raw)          # 연도만
    if m:
        return m.group(1)
    return raw


def find_dates(text: str) -> list[tuple[str, int, int]]:
    out = []
    for m in DATE_FULL.finditer(text):
        v = norm_date(m.group(1), m.group(2), m.group(3))
        if v:
            out.append((v, m.start(), m.end()))
    spans = [(s, e) for _, s, e in out]
    for m in DATE_YM.finditer(text):
        if any(s <= m.start() < e for s, e in spans):
            continue
        v = norm_date(m.group(1), m.group(2))
        if v:
            out.append((v, m.start(), m.end()))
    return out


def _clean_name(seg: str) -> str:
    seg = re.split(r"[\n|┃/·∙・,、;]| {2,}", seg)[0]
    seg = _cut_at_next_label(seg).strip()
    m = NAME.match(seg)
    if not m:
        return ""
    val = NAME_SUFFIX.sub("", m.group(1)).strip()
    flat = val.replace(" ", "")
    if flat.lower() in JUNK_VALUES or flat in NAME_STOP:
        return ""
    return val


# ── 주소 형식 판별 (ISS-032) ────────────────────────────────────────────────
#
# 왜 필요한가: 예전에는 ADDR_TOKEN(`시|도|군|구|읍|면|동|리|로|길|가|층|호|번지`) 하나만 봤다.
# 이 패턴은 한국어 문장 대부분을 통과시킨다 — '피할 수 있어도 ' 의 `도 `, '2026년도 ' 의 `도 `,
# '현장 지도 추진' 의 `도 ` 가 모두 걸린다. 그래서 pilot4 에서 주소 게재값 23건 중 6건(26%)이
# 기사 제목·약관 문장이었다:
#   '방서장 "비는 피할 수 있어도, 방심은 피할 수 없다!"'
#   '세종시의회 예산결산특별위원회, 2026년도 세종시 제1회 추경예산안 등 심사'
# 값이 비어 있지 않으면 필수8_게재수 에 **게재로 카운트**되므로 준수 판정이 실제보다 관대해진다.
#
# 판정 방식: 주소를 이루는 신호에 점수를 주고 2점 이상만 인정한다. 단일 패턴으로는
# '기사 제목인데 지명이 들어간 경우' 를 못 가른다.
_SIDO = re.compile(
    r"(서울|부산|대구|인천|광주|대전|울산|세종|경기|강원|충북|충남|전북|전남|경북|경남|제주)"
    r"(특별자치시|특별시|광역시|특별자치도|특별자치|도|시)?"
)
_SIGUNGU = re.compile(r"[가-힣]{2,5}(시|군|구)(?=\s|,|\)|$)")
_EMD = re.compile(r"[가-힣]{1,6}(읍|면|동|리)(?=\s|,|\)|$|\d)")
_ROADNO = re.compile(r"[가-힣A-Za-z0-9]{1,12}(대로|로|길)\s*\d")
_JIBUN = re.compile(r"\d+\s*-\s*\d+|\d+\s*번지")
_BLDG = re.compile(r"\d+\s*(호|층)|빌딩|타워|센터|아파트|오피스|프라자|스퀘어|하우스")

# 기사 문장·약관 문장의 표지. 하나라도 있으면 주소가 아니다.
_NOT_ADDR = re.compile(
    r"[\"'“”‘’＂]"                       # 인용부호 — 주소에 쓰이지 않는다
    r"|[!?！？]"                          # 감탄·의문 — 기사 제목
    r"|(다|요|까|죠|함)\s*$"               # 종결어미로 끝남 (주소는 호·층·동·번지로 끝난다)
    r"|(습니다|합니다|됩니다|입니다|했다|한다|였다|밝혔다|말했다|위하여|거부)"
)


def looks_like_address(v: str) -> tuple[bool, str]:
    """주소로 볼 수 있는가. (판정, 사유) 를 돌려준다.

    사유는 기각된 값의 근거로 산출물에 남는다 — 검수자가 '왜 미검출인지' 를 알아야 한다.
    """
    s = (v or "").strip()
    if len(s) < 6:
        return False, "너무 짧다"
    if len(s) > 110:
        return False, f"주소로는 지나치게 길다({len(s)}자) — 문장으로 보인다"
    m = _NOT_ADDR.search(s)
    if m:
        return False, f"주소에 쓰이지 않는 표기 {m.group(0)!r} — 기사·약관 문장으로 보인다"
    점수 = 0.0
    있음 = []
    for pat, w, name in ((_ROADNO, 2.0, "도로명+건물번호"), (_SIDO, 1.0, "시도"),
                         (_SIGUNGU, 1.0, "시군구"), (_EMD, 1.0, "읍면동리"),
                         (_JIBUN, 1.0, "지번"), (_BLDG, 0.5, "건물")):
        if pat.search(s):
            점수 += w
            있음.append(name)
    # 시도·시군구만 있으면 주소가 아니라 '지역명' 이다. 발행소 주소는 특정 지점을 가리켜야 한다.
    # 이게 없으면 '2026년도 세종시 제1회 추경예산안 등 심사' 같은 기사 제목이 시도+시군구로
    # 2점을 받아 통과한다(실측).
    구체위치 = any(p.search(s) for p in (_ROADNO, _JIBUN, _EMD))
    if not 구체위치:
        return False, (f"구체적 위치가 없다(발견 {있음 or '없음'}) — 도로명+건물번호·지번·읍면동 "
                       f"중 하나는 있어야 주소다. 지역명만 있는 문장으로 보인다")
    if 점수 < 2.0:
        return False, (f"주소 구성요소가 부족하다(점수 {점수}, 발견 {있음 or '없음'}) — "
                       f"도로명+건물번호가 있거나 시도·시군구·읍면동 중 둘 이상이어야 한다")
    return True, f"주소 구성요소 {','.join(있음)} (점수 {점수})"


def _clean_addr(seg: str) -> str:
    seg = re.split(r"[\n|┃]| {3,}", seg)[0]
    seg = _cut_at_next_label(seg).strip(" ,·|/-")
    pm = PHONE.search(seg)                     # 주소 뒤에 붙은 전화번호 절단
    if pm and pm.start() > 5:
        seg = seg[: pm.start()]
    seg = re.sub(r"\s+", " ", seg).strip(" ,·|/-")
    if len(seg) < 6 or not ADDR_TOKEN.search(seg):
        return ""
    return seg[:100]


class Extractor:
    """zones(dict) + hints(등록정보) → 항목별 결과."""

    def __init__(self, zones: dict, *, hints: dict | None = None, base_date: date | None = None):
        self.z = zones
        self.hints = {k: (str(v).strip() if v else "") for k, v in (hints or {}).items()}
        self.base = base_date or date.today()
        self.zone_texts = [("하단", zones.get("하단", "")), ("상단", zones.get("상단", "")),
                           ("기타", zones.get("전체", ""))]

    # --- 공통: 라벨 기반 탐색 (하단 → 상단 → 기타) ---
    def _by_label(self, field: str, cleaner, *, pattern: re.Pattern | None = None) -> dict | None:
        lab = re.compile(LABEL[field])
        for zone, text in self.zone_texts:
            if not text:
                continue
            for m in lab.finditer(text):
                win = _window(text, m.end())
                if pattern is not None:
                    pm = pattern.search(win)
                    if not pm:
                        continue
                    val = re.sub(r"\s+", "", pm.group(1))
                    conf = "high"
                else:
                    val = cleaner(win)
                    if not val:
                        continue
                    conf = "high" if re.fullmatch(r"[가-힣]{2,4}", val) or len(val) > 8 else "mid"
                if zone == "기타":
                    conf = "low"
                return {"값": val, "근거": _snippet(text, m.start(), m.end() + len(win)),
                        "위치": zone, "확신도": conf, "추출방법": "라벨"}
        return None

    def 등록번호(self) -> dict:
        r = self._by_label("등록번호", None, pattern=REGNUM)
        if r:
            return r
        # ★ 라벨이 있는데 값이 형식에 안 맞으면 **미게재로 확정**한다 (실측 2026-09-02).
        #   강원웰니스신문은 하단에 `등록번호 : 00-0000-00000` 이라는 **플레이스홀더**를 뒀다.
        #   REGNUM 이 안 맞아 라벨없음 폴백으로 내려갔고, 기사 본문의 '2024-25시즌' 에서
        #   `가2024` 를 등록번호로 주워 **게재로 카운트**했다. 라벨이 있다는 건 매체가
        #   그 자리에 등록번호를 적으려 했다는 뜻이므로, 거기 없으면 다른 데서 찾지 않는다.
        #   (발행소_주소가 이미 쓰는 원칙과 같다 — 무엇을 기각했는지 근거에 남긴다.)
        lab = re.compile(LABEL["등록번호"])
        for zone, text in self.zone_texts:
            if not text:
                continue
            for m in lab.finditer(text):
                if "사업자" in text[max(0, m.start() - 4):m.start()]:
                    continue
                win = _window(text, m.end(), 60)
                return {"값": "", "확신도": "low", "위치": "없음",
                        "추출방법": "라벨매칭_형식기각", "판단근거": "",
                        "근거": (f"[등록번호 형식 기각] '{zone}' 의 등록번호 라벨 뒤에 "
                               f"`지역명+아+숫자` 형식이 없다 / 기각값: {win.strip()[:60]!r}")}
        # 라벨이 아예 없을 때만 형식 매칭. 지침 R16 형식(지역명 + `아` + 숫자 5자리)에
        # 맞는 것만 인정한다 — 종별문자 14종에 1자리 숫자까지 받으면 주소의
        # '새롬프라자 4층' 에서 `자4` 를 등록번호로 잡는다(실측).
        text = self.z.get("하단", "")
        m = REGNUM_STRICT.search(text)
        if m:
            return {"값": re.sub(r"\s+", "", m.group(1)), "근거": _snippet(text, m.start(), m.end()),
                    "위치": "하단", "확신도": "low", "추출방법": "형식매칭(라벨없음)"}
        return self._empty()

    @staticmethod
    def _real_phone(v: str) -> bool:
        """자리만 잡아둔 플레이스홀더(`00-0000-0000`)를 게재로 인정하지 않는다."""
        return not PHONE_PLACEHOLDER.match(re.sub(r"\D", "", v or ""))

    def 발행소_전화번호(self) -> dict:
        r = self._by_label("발행소 전화번호", None, pattern=PHONE)
        if r and self._real_phone(r.get("값", "")):
            return r
        if r:
            return {"값": "", "근거": f"[전화 형식 기각] 플레이스홀더 표기 / 기각값: {r.get('값')}",
                    "위치": "없음", "확신도": "low", "추출방법": "라벨매칭_형식기각",
                    "판단근거": ""}
        text = self.z.get("하단", "")
        for m in PHONE.finditer(text):
            if not self._real_phone(m.group(1)):
                continue
            return {"값": re.sub(r"\s+", "", m.group(1)), "근거": _snippet(text, m.start(), m.end()),
                    "위치": "하단", "확신도": "mid", "추출방법": "형식매칭(라벨없음)"}
        return self._empty()

    def 발행소_주소(self) -> dict:
        # ★ 라벨로 잡았더라도 주소 형식을 통과해야 게재로 인정한다 (ISS-032).
        #   '주소' 라벨 뒤에 기사 목록이 이어지는 레이아웃에서 라벨 매칭만으로는 기사 제목을
        #   주소로 잡는다. 값을 억지로 남기지 않고 기각하되, 무엇을 기각했는지 근거에 적는다 —
        #   빈 값으로만 남기면 검수자가 '미게재' 와 '추출 실패' 를 구분할 수 없다.
        r = self._by_label("발행소 주소", _clean_addr)
        if r:
            ok, 사유 = looks_like_address(r.get("값", ""))
            if ok:
                return r
            기각값 = (r.get("값") or "")[:70]
            return {"값": "", "근거": f"[주소형식 기각] {사유} / 기각값: {기각값}",
                    "위치": "없음", "확신도": "low", "추출방법": "라벨매칭_형식기각"}
        # 라벨 없이 주소 형태 줄 (하단 한정)
        for line in self.z.get("하단", "").split("\n"):
            cand = _clean_addr(line)
            if not cand:
                continue
            ok, 사유 = looks_like_address(cand)
            if ok and re.search(r"(시|도)\s*[가-힣]+(시|군|구)", cand):
                return {"값": cand, "근거": re.sub(r"\s+", " ", line)[:120], "위치": "하단",
                        "확신도": "low", "추출방법": "형식매칭(라벨없음)"}
        return self._empty()

    def _name_alt(self) -> dict | None:
        """지침 R13-2 / R15-2 — 이름을 기재하는 항목이 **하나라도** 있으면 준수.

        종전 기준은 `편집국장`·`청소년보호책임자` 를 8항목이 아니라며 배제했다. 지침은 정반대다.
        어떤 라벨로 인정했는지를 남기고 확신도를 낮춰 사람이 검수하게 한다 —
        그 사람이 실제 발행인·편집인이 아닐 수 있기 때문이다.
        """
        for 라벨명, pat in NAME_ALT_LABEL:
            lab = re.compile(pat)
            for zone, text in self.zone_texts:
                if not text:
                    continue
                for m in lab.finditer(text):
                    val = _clean_name(_window(text, m.end()))
                    if not val:
                        continue
                    return {"값": val, "근거": _snippet(text, m.start(), m.end() + 40),
                            "위치": zone,
                            "확신도": "low" if zone == "기타" else "mid",
                            "추출방법": f"이름항목대체({라벨명})",
                            "판단근거": f"지침 R13-2/R15-2 — '{라벨명}' 항목으로 성명 게재"}
        return None

    def _person(self, field: str) -> dict:
        r = self._by_label(field, _clean_name)
        if r:
            # '발행·편집인' 병기는 두 항목 모두 인정 (라벨 자체가 병기형)
            return r
        hint = self.hints.get(field, "")
        if hint and re.fullmatch(r"[가-힣]{2,6}", hint):
            for zone in ("하단", "상단"):
                text = self.z.get(zone, "")
                m = re.search(rf"(?<![가-힣]){re.escape(hint)}(?![가-힣])", text)
                if m:
                    return {"값": hint, "근거": _snippet(text, m.start(), m.end()), "위치": zone,
                            "확신도": "mid", "추출방법": "라벨없음_등록이름일치",
                            "판단근거": "등록된 성명과 문자열 일치"}
        alt = self._name_alt()
        if alt:
            return alt
        return self._empty()

    # ── 날짜 3항목 (지침 R7 · R10) ────────────────────────────────────────────
    #
    # 지침은 날짜 항목을 **서로의 근거로 인정한다.** 발행연월일 자리에 창간일이 있어도 준수고,
    # 등록연월일 자리에 발행연월일이 있어도 준수다. 종전 기준(발행연월일 = 최신 기사 날짜,
    # 과거 날짜만 있으면 미게재)과 정반대이므로 아래 우선순위 표가 그대로 지침의 구현이다.
    #
    # 유일한 제외는 '투데이' 형 실시간 현재날짜 표기다(TODAY_CTX).
    # 비대칭 하나에 주의: **기사 업로드 날짜는 발행연월일은 만족시키지만 등록연월일은 아니다**
    # (지침 R10 비고 "기사 업로드 일시로 확인한 경우 제외"). `판단근거` 가 이 구분을 나른다.

    def _today_widget(self, text: str, s: int, e: int, value: str) -> bool:
        """'투데이 2026.08.14' 처럼 현재 날짜를 알려주는 표기인가 — 지침의 유일한 제외 대상.

        표지가 날짜에 **바로 붙어 있고**(구분자만 허용) 값이 **기준일과 같을 때만** 시계로 본다.
        제호에 '투데이' 가 든 매체(뉴스투데이·머니투데이)의 기사 날짜를 죽이지 않기 위해서다.
        """
        if value != self.base.isoformat():
            return False
        앞 = TODAY_SEP.sub("", text[max(0, s - TODAY_LOOK):s])
        if TODAY_MARK.search(앞):
            return True
        뒤 = text[e:e + TODAY_LOOK]
        return bool(re.match(r"^[\s:：=|/·∙・\-–—]*(?:투\s*데\s*이|오\s*늘|today)(?![가-힣])",
                             뒤, re.I))

    def _labeled_date(self, label_key: str) -> tuple[str, str, str] | None:
        """라벨 **앞뒤** 창에서 날짜를 찾는다 → (값, 근거, 위치).

        라벨이 날짜 뒤에 오는 표기가 흔하다 — `2026.07.28 최종수정`, `2015.3.2 등록`.
        앞쪽만 보면 이런 사이트의 최종수정일을 놓치고 하단의 등록일을 발행연월일로 쓰게 된다
        (픽스처 F01 실측). 뒤쪽 창을 먼저 보고, 없을 때만 앞쪽을 본다.
        """
        lab = re.compile(LABEL[label_key])
        for zone, text in self.zone_texts:
            if not text:
                continue
            for m in lab.finditer(text):
                win = _window(text, m.end(), 60)
                for v, s, e in find_dates(win):
                    if self._today_widget(win, s, e, v):
                        continue
                    return v, _snippet(text, m.start(), m.end() + len(win)), zone
                # 라벨 앞 30자. 날짜와 라벨 사이에 **다른 라벨이 끼면 안 된다** —
                # `등록일 : 2015.3.2 | 창간일 : …` 에서 창간일이 등록일 날짜를 훔쳐가지 않게.
                앞 = text[max(0, m.start() - 30):m.start()]
                ds = find_dates(앞)
                if ds:
                    v, s, e = ds[-1]                      # 라벨에 가장 가까운 날짜
                    사이 = 앞[e:]
                    if not ANY_LABEL.search(사이) and not self._today_widget(앞, s, e, v):
                        return v, _snippet(text, max(0, m.start() - 30), m.end()), zone
        return None

    def _unlabeled_date(self, zone: str, 제외: set[str] | None = None) -> tuple[str, str] | None:
        """한 영역의 라벨 없는 날짜 중 가장 최근 것 → (값, 근거)."""
        text = self.z.get("전체" if zone == "기타" else zone, "")
        best = None
        for v, s, e in find_dates(text):
            if 제외 and v in 제외:
                continue
            if self._today_widget(text, s, e, v):
                continue
            if best is None or v > best[0]:
                best = (v, _snippet(text, s, e))
        return best

    def _regnum_adjacent_date(self) -> tuple[str, str, str] | None:
        """`등록번호 : 세종 아00023(2014년2월21일)` 형태 — 등록번호에 병기된 날짜.

        라벨은 없지만 등록연월일의 가장 강한 신호다. 이걸 먼저 보지 않으면 하단 어딘가의
        무관한 날짜가 등록연월일 값으로 들어간다 — 지침상 게재 여부는 어차피 준수지만
        산출물의 **값** 이 틀리면 검수자가 되짚을 수 없다.
        """
        for zone, text in self.zone_texts:
            if not text:
                continue
            for m in REGNUM.finditer(text):
                win = text[m.end():m.end() + 40]
                for v, s, e in find_dates(win):
                    if self._today_widget(win, s, e, v):
                        continue
                    return v, _snippet(text, m.start(), m.end() + len(win)), zone
        return None

    def _reject_today(self, r: tuple[str, str, str] | None) -> tuple[str, str, str] | None:
        """등록연월일 후보에서 **기준일(오늘) 날짜**를 뺀다.

        등록연월일은 과거에 일어난 등록 사실의 날짜다 — 오늘일 수 없다. 그런데 상단에
        라벨 없이 현재 날짜를 찍는 사이트가 많고(`2026년 09월 02일`), '투데이/오늘' 같은
        표지가 없어 `_today_widget` 이 못 거른다. 지침 R10-2 가 제외하라는 '현재 날짜를
        알려주는 것' 이 바로 이것이므로, 등록연월일에 한해 기준일 날짜를 거부한다.
        (발행연월일은 오늘 기사를 올렸을 수 있으므로 거부하지 않는다.)
        """
        return None if (r and r[0] == self.base.isoformat()) else r

    def 등록연월일(self) -> dict:
        """지침 R10 — 1) 등록연월일 명칭  2) 명칭 무관 날짜형태(발행연월일·창간일 등)."""
        r = self._labeled_date("등록연월일")
        if r:
            v, snip, zone = r
            return {"값": v, "근거": snip, "위치": zone,
                    "확신도": "high" if zone != "기타" else "low",
                    "추출방법": "라벨", "판단근거": "등록연월일 라벨"}
        r = self._reject_today(self._regnum_adjacent_date())
        if r:
            v, snip, zone = r
            return {"값": v, "근거": snip, "위치": zone, "확신도": "mid",
                    "추출방법": "등록번호 병기 날짜", "판단근거": "등록번호에 병기된 날짜"}
        for key, 근거명, 방법 in (("발행연월일", "발행연월일 라벨", "지침R10-2 대체(발행연월일)"),
                                ("창간일", "창간일 라벨", "지침R10-2 대체(창간일)")):
            r = self._reject_today(self._labeled_date(key))
            if r:
                v, snip, zone = r
                return {"값": v, "근거": snip, "위치": zone,
                        "확신도": "mid" if zone != "기타" else "low",
                        "추출방법": 방법, "판단근거": 근거명}
        # 라벨 없는 날짜는 **하단만** 본다. 상단·본문의 날짜는 기사목록의 업로드 날짜일 수
        # 있는데, 지침 R10 비고가 그것을 등록연월일 근거에서 명시적으로 제외했기 때문이다.
        u = self._unlabeled_date("하단")
        r = self._reject_today((u[0], u[1], "하단")) if u else None
        if r:
            return {"값": r[0], "근거": r[1], "위치": "하단", "확신도": "low",
                    "추출방법": "지침R10-2 날짜형태(라벨없음)",
                    "판단근거": "하단 날짜형태(라벨없음)"}
        return self._empty()

    def 발행연월일(self) -> dict:
        """지침 R7 — 1) 발행연월일·최종편집일시 명칭  2) 명칭 무관 날짜형태  3) 기사 업로드 날짜.

        지침은 셋을 **모두 준수로 인정**하므로 어느 것을 잡아도 게재 여부는 같다.
        그래서 우선순위는 '어느 값이 발행연월일로서 가장 그럴듯한가' 로 정했다 —
        게재 여부가 같아도 산출물의 **값** 이 엉뚱하면 검수자가 되짚을 수 없기 때문이다.
        상단의 라벨 없는 날짜(마스트헤드 날짜줄)를 등록연월일·창간일 라벨보다 앞에 두는
        이유가 그것이다. 실측: 상단에 `2026-07-27` 이 있는데 하단 `등록연월일 2011.05.09` 를
        발행연월일로 내면 지침상 준수여도 검수 시트가 읽히지 않는다(픽스처 F02).
        """
        r = self._labeled_date("발행연월일")
        if r:
            v, snip, zone = r
            return {"값": v, "근거": snip, "위치": zone,
                    "확신도": "high" if zone != "기타" else "low",
                    "추출방법": "라벨", "판단근거": "발행연월일·최종편집일시 라벨"}
        r = self._unlabeled_date("상단")
        if r:
            return {"값": r[0], "근거": r[1], "위치": "상단", "확신도": "mid",
                    "추출방법": "지침R7-2 날짜형태(라벨없음)",
                    "판단근거": "상단 날짜형태(라벨없음)"}
        # 상단·하단 어디에도 없고 전체에서만 나오는 날짜는 기사목록에 있는 것이다 (지침 R7-3).
        # 하단 날짜를 기사 업로드로 오인하지 않게 값으로 제외한다.
        하단날짜 = {v for v, _, _ in find_dates(self.z.get("하단", ""))}
        r = self._unlabeled_date("기타", 제외=하단날짜)
        if r:
            return {"값": r[0], "근거": r[1], "위치": "기타", "확신도": "mid",
                    "추출방법": "지침R7-3 기사 업로드 날짜", "판단근거": "기사 업로드 날짜"}
        for key, 근거명, 방법 in (("등록연월일", "등록연월일 라벨", "지침R7-2 대체(등록연월일)"),
                                ("창간일", "창간일 라벨", "지침R7-2 대체(창간일)")):
            r = self._labeled_date(key)
            if r:
                v, snip, zone = r
                return {"값": v, "근거": snip, "위치": zone,
                        "확신도": "mid" if zone != "기타" else "low",
                        "추출방법": 방법, "판단근거": 근거명}
        r = self._unlabeled_date("하단")
        if r:
            return {"값": r[0], "근거": r[1], "위치": "하단", "확신도": "low",
                    "추출방법": "지침R7-2 날짜형태(라벨없음)",
                    "판단근거": "하단 날짜형태(라벨없음)"}
        return self._empty()

    def 창간일(self) -> dict:
        lab = re.compile(LABEL["창간일"])
        for zone, text in self.zone_texts:
            for m in lab.finditer(text or ""):
                win = _window(text, m.end(), 60)
                ds = find_dates(win)
                if ds:
                    return {"값": ds[0][0], "근거": _snippet(text, m.start(), m.end() + len(win)),
                            "위치": zone, "확신도": "high" if zone != "기타" else "low",
                            "추출방법": "라벨", "판단근거": "창간일 라벨"}
        return self._empty()

    def 제호(self) -> dict:
        r = self._by_label("제호", lambda w: re.split(r"[\n|┃/·]| {2,}", _cut_at_next_label(w))[0].strip())
        if r and r["값"]:
            return r
        hint = self.hints.get("제호", "")
        if hint:
            for zone in ("하단", "상단"):
                text = self.z.get(zone, "")
                m = re.search(re.escape(hint), text)
                if m:
                    return {"값": hint, "근거": _snippet(text, m.start(), m.end()), "위치": zone,
                            "확신도": "mid", "추출방법": "등록제호_문자열일치"}
        t = (self.z.get("og_site_name") or self.z.get("title") or "").strip()
        if t:
            t = re.split(r"\s*[|·\-–—:]\s*", t)[0].strip()
            if t:
                return {"값": t, "근거": f"title/og: {self.z.get('title', '')[:60]}", "위치": "기타",
                        "확신도": "low", "추출방법": "title추정"}
        return self._empty()

    def 제호_영문(self) -> dict:
        copy_cand = None
        for zone in ("하단", "상단"):
            text = self.z.get(zone, "")
            for m in EN_TITLE.finditer(text):
                words = [w.strip(".") for w in m.group(1).split()]
                while words and words[0].upper() in EN_STOP:      # 앞뒤 불용어 절단
                    words.pop(0)
                while words and words[-1].upper() in EN_STOP:
                    words.pop()
                cand = " ".join(words).strip()
                if not cand or len(cand) < 4 or not EN_KEYWORD.search(cand):
                    continue
                snip = _snippet(text, m.start(), m.end())
                if re.search(r"(?i)copyright|reserved|ⓒ|©", _snippet(text, m.start(), m.end(), 25)):
                    copy_cand = copy_cand or {"값": cand, "근거": snip, "위치": zone,
                                              "확신도": "low", "추출방법": "저작권표기(확인필요)"}
                    continue
                return {"값": cand, "근거": snip, "위치": zone,
                        "확신도": "mid", "추출방법": "영문패턴"}
        if copy_cand:
            return copy_cand
        t = self.z.get("og_site_name") or self.z.get("title") or ""
        m = EN_TITLE.search(t)
        if m and EN_KEYWORD.search(m.group(1)):
            return {"값": m.group(1).strip(), "근거": f"title/og: {t[:60]}", "위치": "기타",
                    "확신도": "low", "추출방법": "title추정"}
        return {"값": "해당사항 없음", "근거": "상·하단에서 영문 제호 미발견", "위치": "없음",
                "확신도": "mid", "추출방법": "미발견"}

    # --- helpers ---
    @staticmethod
    def _empty() -> dict:
        return {"값": "", "근거": "", "위치": "없음", "확신도": "low", "추출방법": "미검출",
                "판단근거": ""}

    def run(self) -> dict:
        out = {
            "제호": self.제호(),
            "제호 영문": self.제호_영문(),
            "등록번호": self.등록번호(),
            "등록연월일": self.등록연월일(),
            "발행인": self._person("발행인"),
            "편집인": self._person("편집인"),
            "발행연월일": self.발행연월일(),
            "발행소 주소": self.발행소_주소(),
            "발행소 전화번호": self.발행소_전화번호(),
            "창간일": self.창간일(),
        }
        # 발행·편집인 병기 보정: 한쪽만 잡혔고 근거 스니펫이 병기형이면 다른 쪽에도 반영
        for a, b in (("발행인", "편집인"), ("편집인", "발행인")):
            if out[a]["값"] and not out[b]["값"]:
                if re.search(r"발\s*행\s*[·∙・/,]?\s*편\s*집|편\s*집\s*[·∙・/,]?\s*발\s*행|겸\s*편집인|겸\s*발행인",
                             out[a]["근거"]):
                    out[b] = dict(out[a], 추출방법="병기(발행·편집인)", 확신도="mid",
                                  판단근거="발행·편집인 병기 표기")
        return apply_cross_rules(out, base_date=self.base)


# ── 항목 간 교차 준수 규칙 (지침 비고) ─────────────────────────────────────────
#
# 지침은 항목을 독립적으로만 보지 않는다. 한 항목이 준수면 다른 항목도 준수로 **강제**하는
# 규칙이 네 개 있고, 이것이 없으면 지침상 존재할 수 없는 조합(등록연월일 준수 + 발행연월일
# 미준수)이 산출물에 그대로 남는다. pilot3 26건 중 2건이 실제로 그랬다(30897·30883).
#
#   1) 등록연월일 준수 → 발행연월일 무조건 준수                      (R7 비고)
#   2) 발행연월일 준수 → 등록연월일 준수. **기사 업로드 일시로 확인한 경우 제외** (R10 비고)
#   3) 편집인 게재 → 발행인도 준수 (최소 동일인으로라도)              (R12 비고)
#   4) 발행인 게재 → 편집인도 준수                                    (R14 비고)
#
# 규칙 1·2 의 비대칭이 핵심이다. 그래서 날짜 항목이 `판단근거` 를 나른다.
_기사업로드_근거 = "기사 업로드 날짜"


def _has(item: dict | None) -> bool:
    v = ((item or {}).get("값") or "").strip()
    return bool(v) and v != "해당사항 없음"


def apply_cross_rules(findings: dict, *, base_date: date | None = None) -> dict:
    """지침 비고의 교차 준수 규칙 4개를 적용한다.

    최종 findings 에 적용해야 한다 — 규칙 추출 직후(`Extractor.run`)와 LLM·비전·발행일검증을
    병합한 뒤(`merge_llm.effective`) 양쪽에서 호출된다. 전이로 채운 항목은
    `추출방법=지침교차규칙(...)` 로 표시해 원래 화면에서 읽은 값과 구분한다.
    """
    out = dict(findings)
    오늘 = (base_date or date.today()).isoformat()

    def 전이(src: str, dst: str, 규칙: str) -> None:
        s = out.get(src) or {}
        out[dst] = {
            "값": s.get("값", ""),
            "근거": f"[{규칙}] {src} 게재로 {dst} 준수 — 원근거: {(s.get('근거') or '')[:110]}",
            "위치": s.get("위치", "기타"),
            "확신도": "mid",
            "추출방법": f"지침교차규칙({규칙})",
            "판단근거": f"{src} 준수 → {dst} 준수 ({규칙})",
        }

    if _has(out.get("등록연월일")) and not _has(out.get("발행연월일")):
        전이("등록연월일", "발행연월일", "R7비고")
    elif _has(out.get("발행연월일")) and not _has(out.get("등록연월일")):
        근거 = (out.get("발행연월일") or {}).get("판단근거") or ""
        값 = ((out.get("발행연월일") or {}).get("값") or "").strip()
        # 전이 금지 두 가지:
        #   ① 기사 업로드 일시로 확인한 발행연월일 (지침 R10 비고의 명시적 단서)
        #   ② 기준일(오늘) 날짜 — 등록은 과거 사실이므로 등록연월일이 오늘일 수 없다.
        #      상단 시계 날짜가 발행연월일로 잡히면 전이를 타고 등록연월일까지 오염된다
        #      (강원웰니스신문 실측 — 등록연월일이 2026-09-02 로 찍혔다).
        if _기사업로드_근거 not in 근거 and 값 != 오늘:
            전이("발행연월일", "등록연월일", "R10비고")

    if _has(out.get("편집인")) and not _has(out.get("발행인")):
        전이("편집인", "발행인", "R12비고")
    elif _has(out.get("발행인")) and not _has(out.get("편집인")):
        전이("발행인", "편집인", "R14비고")
    return out


def extract(zones: dict, *, hints: dict | None = None, base_date: date | None = None) -> dict:
    return Extractor(zones, hints=hints, base_date=base_date).run()
