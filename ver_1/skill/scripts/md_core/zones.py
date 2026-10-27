"""HTML → 상단(header)/하단(footer)/전체 텍스트 영역 분리.

필요적 게재사항은 '메인화면 상·하단' 에 있어야 인정되므로, 어디서 찾았는지(위치)가
값 자체만큼 중요하다. 이 모듈은 텍스트를 위치별로 나눠 주고 판정은 fields.py 가 한다.
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

FOOTER_HINT = re.compile(
    r"foot|copyright|copy_right|company|corp|bottom|btm|site_?info|siteinfo|"
    r"address|addr|info_?area|infoarea|masthead|publisher|media_?info",
    re.I,
)
HEADER_HINT = re.compile(r"header|head_?wrap|headwrap|^head$|top_?area|toparea|gnb|logo|masthead_?top", re.I)
DROP_TAGS = ("script", "style", "noscript", "iframe", "svg", "template")


def soupify(html: str) -> BeautifulSoup:
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:  # noqa: BLE001
        soup = BeautifulSoup(html, "html.parser")
    for t in soup.find_all(DROP_TAGS):
        t.decompose()
    return soup


def _clean(text: str) -> str:
    lines = []
    for raw in text.splitlines():
        line = re.sub(r"[ \t\xa0​]+", " ", raw).strip()
        if line:
            lines.append(line)
    out, prev = [], None
    for line in lines:            # 연속 중복 줄 제거
        if line != prev:
            out.append(line)
        prev = line
    return "\n".join(out)


def _matched_blocks(soup: BeautifulSoup, hint: re.Pattern) -> list:
    hits = []
    for el in soup.find_all(True):
        name = el.name or ""
        attrs = " ".join(
            [str(el.get("id") or "")] + list(el.get("class") or []) + [str(el.get("role") or "")]
        )
        if name in ("footer", "header") or hint.search(attrs):
            if hint is HEADER_HINT and name == "footer":
                continue
            if hint is FOOTER_HINT and name == "header":
                continue
            hits.append(el)
    # 중첩 제거: 이미 담긴 블록의 자손은 버린다
    kept = []
    for el in hits:
        if not any(anc in kept for anc in el.parents):
            kept.append(el)
    return kept


def extract_zones(html: str, *, footer_selector: str = "", header_selector: str = "") -> dict:
    """→ {'하단': str, '상단': str, '전체': str, 'title': str, 'og_site_name': str, 'js_의심': bool}"""
    soup = soupify(html)
    full = _clean(soup.get_text("\n"))

    def by_selector(sel: str) -> str:
        if not sel:
            return ""
        try:
            return _clean("\n".join(el.get_text("\n") for el in soup.select(sel)))
        except Exception:  # noqa: BLE001
            return ""

    footer = by_selector(footer_selector)
    header = by_selector(header_selector)

    if not footer:
        blocks = _matched_blocks(soup, FOOTER_HINT)
        footer = _clean("\n".join(b.get_text("\n") for b in blocks))
    if not header:
        blocks = _matched_blocks(soup, HEADER_HINT)
        header = _clean("\n".join(b.get_text("\n") for b in blocks))

    lines = full.split("\n")
    if not footer and lines:                       # 폴백: 본문 마지막 20%
        footer = "\n".join(lines[max(0, int(len(lines) * 0.8)):])
    if not header and lines:                       # 폴백: 본문 앞 12%
        header = "\n".join(lines[: max(3, int(len(lines) * 0.12))])

    # 하단/상단 블록이 페이지 전체를 삼킨 경우(레이아웃 div 오탐) 잘라낸다
    if len(footer) > max(4000, len(full) * 0.6):
        f_lines = footer.split("\n")
        footer = "\n".join(f_lines[max(0, len(f_lines) - 80):])
    if len(header) > max(3000, len(full) * 0.5):
        header = "\n".join(header.split("\n")[:60])

    title = (soup.title.get_text(strip=True) if soup.title else "") or ""
    og = ""
    for prop in ("og:site_name", "og:title", "application-name"):
        tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
        if tag and tag.get("content"):
            og = tag["content"].strip()
            break

    js_의심 = len(full) < 800 and bool(
        re.search(r'id="(root|app|__next)"|__NEXT_DATA__|window\.__NUXT__', html)
        or html.lower().count("<script") > 8
    )
    return {"하단": footer, "상단": header, "전체": full, "title": title,
            "og_site_name": og, "js_의심": js_의심}


# ── 게재사항 밀집 구간 탐지 ────────────────────────────────────────────────────
#
# 왜 필요한가 (2026-09-03 실측 — 1012 슬기로운시니어뉴스)
# 게재사항을 `<footer>` 가 아니라 **본문 중간의 위젯**에 넣는 사이트가 있다. 1012 는
# `<footer class="site-info">` 에 저작권 한 줄만 있어 하단 zone 이 99자였고, 실제
# `제호 / 등록번호 / 등록연월일 / 발행인·편집인 / 주소 / 대표번호` 블록은 페이지 중간의
# `<ul><li>` 였다. 규칙 추출기는 `전체` zone 도 보므로 값을 찾았지만, **LLM 에는 상단·하단만
# 전달돼서** 모델이 "입력 텍스트 어디에도 근거가 없다" 며 6항목을 전부 규칙기각했다.
#
# 지침 R17 은 '3순위 기타 위치' 도 게재로 인정한다. 위치가 어디든 그 블록을 찾아 판단
# 단계에 함께 넘겨야 한다.
DISCLOSURE_LABEL = re.compile(
    r"제\s*호|등\s*록\s*번\s*호|등\s*록\s*(?:연월일|년월일|일자|일)|"
    r"발\s*행\s*인|편\s*집\s*인|발\s*행\s*(?:연월일|년월일|일자|일)|최종\s*(?:수정|편집)|"
    r"주\s*소|소\s*재\s*지|발\s*행\s*소|전\s*화|대\s*표\s*번\s*호|연\s*락\s*처|"
    r"청\s*소\s*년\s*보\s*호|창\s*간"
)


# 게재사항 표기 줄의 생김새: **라벨로 시작하는 짧은 줄** (`주소: 강원…`, `등록번호 : 강원 아00410`).
# 기사 본문에도 '주소'·'등록' 같은 낱말이 나오지만 문장 한가운데에 있고 줄이 길다.
# 이 두 조건으로 가르지 않으면 기사 목록을 게재사항 블록으로 착각한다(실측).
_LABEL_LINE_HEAD = 14      # 줄 앞 몇 글자 안에서 라벨이 시작해야 하는가
_LABEL_LINE_MAX = 90       # 게재사항 줄의 최대 길이


def _label_kind(line: str) -> str:
    """이 줄이 '라벨로 시작하는 게재사항 줄' 이면 라벨 종류를, 아니면 빈 문자열."""
    s = line.strip()
    if not s or len(s) > _LABEL_LINE_MAX:
        return ""
    m = DISCLOSURE_LABEL.search(s[:_LABEL_LINE_HEAD + 8])
    if not m or m.start() > _LABEL_LINE_HEAD:
        return ""
    return re.sub(r"\s+", "", m.group(0))


def disclosure_block(full: str, *, window: int = 16, min_kinds: int = 3) -> dict:
    """게재사항 라벨이 가장 밀집한 구간 → {"텍스트", "라벨수", "시작줄"}.

    `window` 줄짜리 창을 훑으며 **서로 다른 라벨로 시작하는 짧은 줄**의 종류 수를 센다.
    같은 라벨이 반복되는 것보다 종류가 많은 쪽을 고른다 — 게재사항 바는 제호·등록번호·
    주소·전화가 한 번씩 나오지, 같은 라벨이 되풀이되지 않는다.
    `min_kinds` 미만이면 빈 텍스트를 준다(억지로 만들지 않는다).
    """
    lines = (full or "").split("\n")
    best = {"텍스트": "", "라벨수": 0, "시작줄": -1}
    if not lines:
        return best
    kinds_per_line = [_label_kind(l) for l in lines]
    for i in range(len(lines)):
        kinds = {k for k in kinds_per_line[i:i + window] if k}
        if len(kinds) > best["라벨수"]:
            # 창 안에서 라벨 줄이 실제로 있는 구간만 잘라낸다 (앞뒤 빈 줄 제거)
            seg = lines[i:i + window]
            idx = [j for j, k in enumerate(kinds_per_line[i:i + window]) if k]
            if idx:
                seg = seg[idx[0]:idx[-1] + 1]
            best = {"텍스트": "\n".join(seg).strip(), "라벨수": len(kinds), "시작줄": i}
    if best["라벨수"] < min_kinds:
        return {"텍스트": "", "라벨수": best["라벨수"], "시작줄": -1}
    return best
