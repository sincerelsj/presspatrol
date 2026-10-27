#!/usr/bin/env python3
"""identity — 동일 사이트·동일 운영주체 판별에 쓰는 정규화·지문 도구 (R5).

왜 별도 모듈인가
----------------
R5a(`fingerprint_sites.py`)가 지문을 만들고 R5b(`cluster_sites.py`)가 그 지문을 비교한다.
두 단계가 **같은 정규화**를 써야 한다 — 전화번호를 한쪽은 하이픈 제거, 다른 쪽은 유지로
비교하면 군집이 통째로 빠진다. 그래서 정규화는 여기 한 곳에만 둔다.

설계에서 실측으로 배운 것 두 가지 (2026-09-11)
-----------------------------------------------
1. **푸터 전문 비교는 안 된다.** 같은 운영주체의 두 사이트(6149 굿모닝맘스 ↔ 6187 메뉴투데이)는
   푸터 고정문구가 글자 단위로 같은데도 5-gram Jaccard 가 0.275 였다 — 푸터에 최신 기사
   헤드라인이 섞여 돌기 때문이다. 그래서 `fixed_block()` 이 회전 콘텐츠를 걷어낸다.
2. **DOM 구조 유사도는 단독 근거가 못 된다.** 렌더HTML 353건 전 쌍 비교에서 class 집합
   Jaccard = 1.00 인데 서로 무관한 매체가 쏟아졌다(어반포스트 ↔ 위클리유연 ↔ CKL뉴스,
   베스트커리어매거진 ↔ 대기구필라테스청담). 같은 CMS 벤더 템플릿일 뿐이다.
   랜덤 쌍의 4.2% 가 J>0.3 을 넘는다. 구조는 **보조 신호**로만 쓴다.
"""
from __future__ import annotations

import hashlib
import random
import re

# ── MinHash ────────────────────────────────────────────────────────────────
# datasketch 가 환경에 없어 순수 파이썬으로 둔다. 64개 순열, 4행 16밴드로 LSH 블로킹한다.
_P = (1 << 61) - 1
_PERM_N = 64
_rng = random.Random(20260911)
_PERMS = [(_rng.randrange(1, _P), _rng.randrange(0, _P)) for _ in range(_PERM_N)]


def _h64(tok: str) -> int:
    return int.from_bytes(hashlib.blake2b(tok.encode("utf-8"), digest_size=8).digest(), "big")


def minhash(tokens) -> list[int]:
    """토큰 집합 → 64개 정수 서명. 빈 집합은 []."""
    hs = [_h64(t) for t in set(tokens)]
    if not hs:
        return []
    return [min((a * h + b) % _P for h in hs) for a, b in _PERMS]


def mh_jaccard(m1, m2) -> float:
    if not m1 or not m2 or len(m1) != len(m2):
        return 0.0
    return sum(1 for x, y in zip(m1, m2) if x == y) / len(m1)


def bands(sig, rows: int = 4) -> list[str]:
    """LSH 밴드 키. 한 밴드라도 같으면 후보쌍이 된다 (전 쌍 48M 비교 회피)."""
    if not sig:
        return []
    out = []
    for i in range(0, len(sig), rows):
        chunk = sig[i:i + rows]
        if len(chunk) == rows:
            out.append(f"{i // rows}:" + hashlib.blake2b(
                ",".join(map(str, chunk)).encode(), digest_size=8).hexdigest())
    return out


def shingles(text: str, k: int = 5) -> set:
    t = re.sub(r"\s+", " ", text or "").strip()
    if len(t) < k:
        return {t} if t else set()
    return {t[i:i + k] for i in range(len(t) - k + 1)}


# ── 호스트 ─────────────────────────────────────────────────────────────────
try:
    import tldextract
    _TLD = tldextract.TLDExtract(suffix_list_urls=())      # 네트워크 조회 금지
except Exception:                                          # noqa: BLE001
    _TLD = None


def host_of(url: str) -> str:
    u = (url or "").strip().lower()
    if not u:
        return ""
    u = re.sub(r"^[a-z]+://", "", u)
    u = u.split("/")[0].split("?")[0].split("#")[0].split("@")[-1].split(":")[0]
    u = u[4:] if u.startswith("www.") else u
    # 점이 없으면 호스트가 아니다. 등록 URL 이 깨진 6건이 호스트 'http' 로 파싱돼
    # 서로 '같은 호스트' 군집을 만들었다(Z0005, 2026-09-15).
    return u if ("." in u and len(u) >= 4 and not u.startswith(".")) else ""


def reg_domain(host: str) -> str:
    """등록가능 도메인(eTLD+1). news.example.co.kr → example.co.kr"""
    h = host_of(host)
    if not h or re.fullmatch(r"[\d.]+", h):
        return h
    if _TLD is not None:
        try:
            e = _TLD(h)
            if e.domain and e.suffix:
                return f"{e.domain}.{e.suffix}"
        except Exception:                                  # noqa: BLE001
            pass
    parts = h.split(".")
    return ".".join(parts[-3:]) if len(parts) >= 3 and len(parts[-2]) <= 3 else ".".join(parts[-2:])


# 호스팅 만료·오류 안내 페이지. 같은 안내문을 보고 "같은 사이트" 라고 하면 안 된다.
# 다만 '같은 호스팅사에서 같은 날 만료' 자체는 별도 신호이므로 제외사유로 남긴다.
ERROR_HOSTS = re.compile(
    r"hostinfo\.cafe24\.com|errdoc\.gabia|error\.interbest|serviceexpire|expiration\.html|"
    r"blog\.naver\.com|cafe\.daum\.net|m\.blog\.naver|modoo\.at|"
    r"expireddomains|hugedomains|godaddy|sedo|afternic|dan\.com|parkingcrew|bodis|"
    r"domainnamesales|whois\.co\.kr|megazone", re.I)

# eTLD+1 이 같아도 같은 사이트가 아닌 플랫폼·호스팅 도메인.
# (a.tistory.com 과 b.tistory.com 은 남남이다. 반면 mw.today.kr ↔ cardnews.today.kr 은
#  투데이미디어그룹(주)이 자기 도메인에 만든 서브도메인이라 같은 사이트로 본다.)
PLATFORM_DOMAINS_RE = re.compile(
    r"^(?:tistory\.com|blogspot\.com|blogspot\.kr|wordpress\.com|wixsite\.com|weebly\.com|"
    r"cafe24\.com|mycafe24\.com|gabia\.io|modoo\.at|imweb\.me|creatorlink\.net|"
    r"naver\.com|daum\.net|google\.com|github\.io|netlify\.app|vercel\.app|"
    r"co\.kr|or\.kr|ne\.kr|pe\.kr|go\.kr|re\.kr)$", re.I)

# 호스팅사 기본 페이지·준비중 페이지. 도메인은 매체 것이지만 화면은 호스팅사 것이다.
# ERROR_HOSTS 는 호스트로만 잡아서 이걸 못 걸렀고, 그 결과 메가존 기본 페이지를 띄운 51개
# 매체가 호스팅사의 공통 연락처·저작권 문구를 공유한다는 이유로
# 한 군집(A등급)으로 묶였다(2026-09-11). 내용으로 잡아야 한다.
VENDOR_PAGE_RE = re.compile(
    r"megazone|cafe24|카페24|가비아|gabia|whois\.co\.kr|아임웹|imweb|makeshop|메이크샵|"
    r"고도몰|godomall|호스팅|hosting|홈페이지\s*준비|서비스\s*준비|준비\s*중입니다|"
    r"coming\s*soon|under\s*construction|도메인\s*(?:판매|구매|만료|등록)", re.I)


def vendor_page(저작권: str = "", 블록줄=None, 전화=None) -> str:
    """호스팅사 기본 페이지로 보이면 사유 문자열, 아니면 ''.

    판정은 보수적으로 — 저작권 표기에 벤더명이 있거나, 고정블록이 10줄 이하로 빈약한데
    그 안에 벤더·준비중 문구가 있을 때만. 기사가 도는 진짜 매체는 고정블록이 이보다 길다.
    """
    if 저작권 and VENDOR_PAGE_RE.search(저작권):
        return f"호스팅사 기본 페이지(저작권 표기: {저작권[:30]})"
    줄 = list(블록줄 or [])
    if 줄 and len(줄) <= 10:
        for ln in 줄:
            if VENDOR_PAGE_RE.search(ln):
                return f"호스팅사 기본·준비중 페이지({ln[:40]})"
    return ""


FREE_MAIL = {
    "naver.com", "daum.net", "hanmail.net", "gmail.com", "nate.com", "hotmail.com",
    "outlook.com", "yahoo.com", "yahoo.co.kr", "kakao.com", "empas.com", "korea.com",
    "dreamwiz.com", "hanmir.com", "paran.com", "chol.com", "lycos.co.kr", "icloud.com",
}

# 개별 사이트 식별력이 없는 링크 대상 — 링크그래프에서 뺀다.
COMMON_LINK_HOSTS = re.compile(
    r"facebook|twitter|x\.com|instagram|youtube|youtu\.be|naver\.com|daum\.net|kakao|"
    r"google|gstatic|linkedin|pinterest|band\.us|tistory|blogspot|wordpress\.org|"
    r"adobe\.com|w3\.org|schema\.org|kcc\.go\.kr|kpf\.or\.kr|acrc\.go\.kr|copyright", re.I)


# ── 연락처 ─────────────────────────────────────────────────────────────────
def norm_phone(s: str) -> str:
    """전화·팩스 → 숫자만. 길이가 말이 안 되면 버린다."""
    d = re.sub(r"\D", "", str(s or ""))
    if not d:
        return ""
    if d.startswith("82"):                                  # +82-2-... → 02-...
        d = "0" + d[2:]
    if len(d) == 8 and d[:2] in ("15", "16", "18"):         # 대표번호 1588·1644·1688
        return d
    return d if 9 <= len(d) <= 11 else ""


def fmt_phone(d: str) -> str:
    return f"{d[:4]}-{d[4:]}" if len(d) == 8 else d


BIZNO_RE = re.compile(r"(?:사업자\s*(?:등록)?\s*번호|사업자번호)[^0-9]{0,12}(\d{3})-?(\d{2})-?(\d{5})")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
TEL_LABEL_RE = re.compile(
    r"(?:대표\s*전화|전화\s*번호|전화|TEL|Tel|tel|연락처|문의)[^0-9+]{0,8}"
    r"(\+?[0-9][0-9\-.\s()]{6,16}[0-9])")
FAX_LABEL_RE = re.compile(r"(?:FAX|Fax|fax|팩스)[^0-9+]{0,8}(\+?[0-9][0-9\-.\s()]{6,16}[0-9])")
YOUTH_RE = re.compile(r"청소년\s*보호\s*(?:책임자|담당자)\s*[:：]?\s*([가-힣]{2,4})")
PRIVACY_RE = re.compile(r"개인정보\s*(?:보호|관리)?\s*책임자\s*[:：]?\s*([가-힣]{2,4})")
COPYRIGHT_RE = re.compile(
    r"(?:Copyright|COPYRIGHT|copyright|©|ⓒ|&copy;)\s*(?:\(c\)|©|ⓒ)?\s*"
    r"(?:(?:19|20)\d\d(?:\s*[-~]\s*(?:19|20)\d\d)?\.?)?\s*"
    r"(?:by\s+)?([A-Za-z가-힣0-9][A-Za-z가-힣0-9 .,&'’\-]{1,38})")
_REGNO_RE = re.compile(r"([가-힣]{2,4})([가-힣])(\d{4,6})")


def norm_regno(s: str) -> str:
    """게재된 등록번호 → '서울아56237' 형태. '서울,아55563' '서울 아 04358' 을 같게 만든다."""
    t = re.sub(r"[\s,\-·]+", "", str(s or ""))
    m = _REGNO_RE.search(t)
    if not m:
        return ""
    시도, 종, 번 = m.group(1), m.group(2), m.group(3)
    return f"{시도}{종}{번.zfill(5)}"


# ── 주소 ───────────────────────────────────────────────────────────────────
_SIDO = {
    "서울특별시": "서울", "서울시": "서울", "부산광역시": "부산", "부산시": "부산",
    "대구광역시": "대구", "인천광역시": "인천", "광주광역시": "광주", "대전광역시": "대전",
    "울산광역시": "울산", "세종특별자치시": "세종", "경기도": "경기", "강원특별자치도": "강원",
    "강원도": "강원", "충청북도": "충북", "충청남도": "충남", "전라북도": "전북",
    "전북특별자치도": "전북", "전라남도": "전남", "경상북도": "경북", "경상남도": "경남",
    "제주특별자치도": "제주", "제주도": "제주",
}
_ROAD_RE = re.compile(r"([가-힣A-Za-z0-9]+(?:대로|로|길))\s*(\d+(?:-\d+)?)")
_HO_RE = re.compile(r"(\d+)\s*호")


def addr_key(s: str) -> str:
    """주소 → 비교 키 '시도|도로명|건물번호|호'.

    등록원부의 '서울 예시구 샘플로 1, 비동 101호(예시동, 샘플타워)' 와
    사이트의 '서울시 예시구 샘플로 1 샘플타워 B동 101호' 를 같은 키로 만든다.
    """
    t = re.sub(r"\s+", " ", str(s or "")).strip()
    if len(t) < 6:
        return ""
    시도 = ""
    for k, v in _SIDO.items():
        if t.startswith(k):
            시도 = v
            break
    if not 시도:
        m = re.match(r"([가-힣]{2})\s", t)
        시도 = m.group(1) if m and m.group(1) in set(_SIDO.values()) else ""
    road = _ROAD_RE.search(t)
    if not road:
        return ""
    ho = _HO_RE.search(t)
    return f"{시도}|{road.group(1)}|{road.group(2)}|{ho.group(1) if ho else ''}"


# ── 고정블록 ───────────────────────────────────────────────────────────────
# 이 키워드가 든 줄은 길어도 '고정문구' 로 본다 (주소·저작권·발행정보).
_KEEP_KW = re.compile(
    r"제호|등록\s*번호|등록\s*일|등록\s*연월일|발행\s*인|편집\s*인|발행\s*일|발행\s*소|"
    r"주소|소재지|사옥|본사|전화|TEL|Tel|tel|FAX|Fax|팩스|이메일|메일|E-?mail|"
    r"대표|청소년|개인정보|저작권|Copyright|COPYRIGHT|©|ⓒ|All\s*rights|사업자|"
    r"고객센터|기사제보|광고문의|무단전재|재배포", re.I)
_DATE_ONLY = re.compile(r"^[\s\d년월일./\-:()]+$")
_DATE_ANY = re.compile(r"(?:19|20)\d\d\s*[.\-/년]\s*\d{1,2}\s*[.\-/월]\s*\d{1,2}\s*일?")
_TIME_ANY = re.compile(r"\d{1,2}\s*[:：]\s*\d{2}")
# 기사 본문·헤드라인의 문장체. 키워드가 우연히 들어간 긴 문장을 걸러낸다
# ("이 프로젝트의 핵심은 창업부터 경영, 위기 대응까지 …" 가 '사업' 때문에 통과했었다).
_PROSE = re.compile(r"(?:다|요|죠|함|임)\.(?:\s|$)|[‘“”’]|\.{2,}|…")
_MAX_LINE = 100


def fixed_block(top: str, foot: str, *, 제호: str = "", 등록번호: str = "") -> tuple:
    """상·하단 텍스트 → 회전 콘텐츠를 걷어낸 '법정표기 고정블록' (줄 목록, 마스킹 텍스트).

    남기는 줄: 고정문구 키워드가 있거나 주소형이면서, **100자 이하이고 문장체가 아닌** 줄.
    마스킹: 날짜 → <D>, 시각 → <TM>, 제호 → <T>, 등록번호 → <REG>.
    제호만 다른 형제 사이트가 서로 같은 고정블록을 갖게 만드는 것이 목적이다.

    캘리브레이션 (2026-09-11, 양성 7쌍·음성 9쌍)
      줄 집합 Jaccard  양성 0.53~0.87 / 음성 0.00~0.43   ← 이 규칙을 채택
      문자 5-gram      양성 0.35~0.82 / 음성 0.06~0.54
      15자 이하 줄까지 남기면  양성 0.26~0.86 / 음성 0.02~0.69 (겹쳐서 못 쓴다)
    """
    lines = []
    for raw in ((top or "") + "\n" + (foot or "")).splitlines():
        ln = re.sub(r"\s+", " ", raw).strip()
        if not ln or _DATE_ONLY.match(ln) or len(ln) > _MAX_LINE or _PROSE.search(ln):
            continue
        if not (_KEEP_KW.search(ln) or addr_key(ln)):
            continue
        n = (제호 or "").strip()
        if len(n) >= 2:
            ln = ln.replace(n, "<T>")
        n = (등록번호 or "").strip()
        if n:
            ln = ln.replace(n, "<REG>")
        ln = re.sub(r"[가-힣]{2,4}\s*[,\-·]?\s*[가-힣]\s*[-\s]?\d{4,6}", "<REG>", ln)
        ln = _TIME_ANY.sub("<TM>", _DATE_ANY.sub("<D>", ln))
        lines.append(ln)
    # 중복 줄은 한 번만 — 같은 문구를 두 번 쓴 사이트가 유사도에서 불이익을 받지 않게.
    seen, uniq = set(), []
    for ln in lines:
        if ln not in seen:
            seen.add(ln)
            uniq.append(ln)
    return uniq, "\n".join(uniq)
