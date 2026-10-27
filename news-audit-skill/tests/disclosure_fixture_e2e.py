#!/usr/bin/env python3
"""필요적 게재사항 스킬 E2E 픽스처 테스트 (외부망 불필요).

한국 인터넷신문 하단(masthead)에서 실제로 자주 보이는 6가지 레이아웃을 재현해
run_extract → compare_registry → finalize 를 통과시키고 기대치와 대조한다.

사용: python disclosure_fixture_e2e.py [--keep]
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
# 검증 대상 scripts 디렉터리.
# 정본(canonical)은 ver_1/skill/scripts 다 (2026-07-31 결정, ISS-015).
# plugins/ 복사본은 낡았으므로 기본값으로 쓰지 않는다. DISCLOSURE_SCRIPTS 로 덮어쓸 수 있다.
_VER1 = HERE.parents[1] / "ver_1" / "skill" / "scripts"
_PLUGIN = HERE.parent / "plugins" / "news-mandatory-disclosure-audit" / "scripts"
SCRIPTS = Path(os.environ.get("DISCLOSURE_SCRIPTS")
               or (_VER1 if (_VER1 / "run_extract.py").exists() else _PLUGIN))
BASE = "2026-07-28"

# ---------------------------------------------------------------- 픽스처 HTML
PAGE = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<title>{title}</title><meta property="og:site_name" content="{og}"></head><body>
<div id="header" class="header"><h1 class="logo">{logo}</h1>{header_extra}</div>
<div id="container"><ul class="news-list">
<li><a href="/news/1">지역 현안 점검…시민 목소리 반영</a><span class="date">{today}</span></li>
<li><a href="/news/2">여름철 폭염 대비 대책 발표</a><span class="date">{today}</span></li>
</ul></div>
{footer}
</body></html>"""

FOOTER_STD = """<div id="footer" class="footer">
<div class="company-info">
<p>제호 : 참뉴스 &nbsp;|&nbsp; 등록번호 : 강원 아00123 &nbsp;|&nbsp; 등록일 : 2015년 3월 2일
&nbsp;|&nbsp; 창간일 : 2015년 3월 10일</p>
<p>발행인 : 홍길동 &nbsp;|&nbsp; 편집인 : 김철수 &nbsp;|&nbsp; 청소년보호책임자 : 박영수</p>
<p>주소 : 강원특별자치도 춘천시 중앙로 1 참뉴스빌딩 3층 &nbsp;|&nbsp; 대표전화 : 033-123-4567
&nbsp;|&nbsp; 팩스 : 033-123-4568 &nbsp;|&nbsp; 이메일 : news@example.invalid</p>
<p class="copy">Copyright © 2015 Cham News. All rights reserved.</p>
</div></div>"""

FOOTER_DL = """<footer><dl class="info">
<dt>신문등록번호</dt><dd>서울 아51234호</dd>
<dt>등록연월일</dt><dd>2011.05.09</dd>
<dt>발행·편집인</dt><dd>이몽룡</dd>
<dt>발행소</dt><dd>서울특별시 종로구 세종대로 100, 5층 (우 03187)</dd>
<dt>전화번호</dt><dd>02-3456-7890</dd>
</dl><p>© 서울시민일보 Seoul Citizen Daily</p></footer>"""

FOOTER_NOLABEL = """<div class="bottom_area">
<p>강원 아4 · 2004-08-11</p>
<p>성춘향 &nbsp; 성춘향</p>
<p>강원도 원주시 시청로 1 &nbsp; 033-999-1234</p>
<p>발행일 : 2004년 8월 20일</p>
</div>"""

FOOTER_TOP_ONLY = """<div class="site_info_top">
<span>등록번호 : 부산 아02345</span><span>등록일자 : 2019.11.20</span>
<span>발행인/편집인 : 최무선</span><span>부산광역시 해운대구 센텀로 55</span>
<span>TEL. 051-777-8888</span></div>"""

FOOTER_MISSING = """<div id="footer"><p>회사소개 | 이용약관 | 개인정보처리방침</p>
<p>Copyright(c) NewsToday. All Rights Reserved.</p></div>"""

JS_SHELL = """<!doctype html><html><head><meta charset="utf-8"><title>제주바다뉴스</title></head>
<body><div id="root"></div><script src="/a.js"></script><script src="/b.js"></script>
<script src="/c.js"></script><script src="/d.js"></script><script src="/e.js"></script>
<script src="/f.js"></script><script src="/g.js"></script><script src="/h.js"></script>
<script src="/i.js"></script><script>window.__NUXT__={};</script></body></html>"""

# ── 2026-08-13 지침 전용 픽스처 ────────────────────────────────────────────────
# 지침이 종전 기준을 **뒤집은** 지점들이다. 이 셋이 없으면 누군가 되돌려도 테스트가 통과한다.

# ⑧ 이름 항목 대체 인정(R13-2/R15-2) + 등록번호 병기 날짜.
#    발행인·편집인 라벨이 전혀 없고 `청소년보호책임자` 만 있다 — 종전 기준은 이걸
#    "8항목이 아니다" 라며 배제했으므로 6/8 미준수였다. 지침에서는 8/8 준수다.
PAGE_NODATE = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<title>{title}</title></head><body>
<div id="header" class="header"><h1 class="logo">{logo}</h1></div>
<div id="container"><ul class="list"><li><a href="/board/1">지역 현안 점검</a></li></ul></div>
{footer}
</body></html>"""

FOOTER_ALTNAME = """<div id="footer" class="footer"><div class="company-info">
<p>제호 : 한들뉴스 &nbsp;|&nbsp; 등록번호 : 충북 아00567(2015년 3월 2일)</p>
<p>청소년보호책임자 : 박영수</p>
<p>주소 : 충청북도 청주시 상당구 상당로 155 한들빌딩 2층 &nbsp;|&nbsp; 대표전화 : 043-222-3333</p>
</div></div>"""

# ⑨ '투데이' 형 현재날짜 표기 — 지침이 명시적으로 제외한 유일한 날짜.
#    이걸 인정하면 오늘 날짜 위젯을 단 모든 매체가 자동 준수가 된다.
PAGE_TODAY = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<title>한빛일보</title></head><body>
<div id="header" class="header"><h1 class="logo">한빛일보</h1>
<span class="today">투데이 {today}</span></div>
<div id="container"><ul class="list"><li><a href="/board/1">지역 현안 점검</a></li></ul></div>
<div id="footer"><p>회사소개 | 이용약관</p></div>
</body></html>"""

# ⑩ 지침 I-3 — 접속은 되지만 매체명이 다르고 뉴스가 없다 → 홈페이지 없음.
#    종전 파이프라인에는 이 관문이 아예 없어서 남의 회사 사이트가 8항목 추출로 넘어갔다.
#    '뉴스'·'기사'·news 문자열이 하나도 없어야 한다 (지침 비고의 뉴스 판정 기준).
PAGE_COMPANY = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<title>강릉건설 주식회사</title></head><body>
<div id="header" class="header"><h1 class="logo">강릉건설</h1></div>
<div id="container"><h2>회사 소개</h2><p>토목·건축 시공 전문 업체입니다.</p>
<ul><li><a href="/company/about">회사개요</a></li><li><a href="/company/contact">오시는 길</a></li></ul>
</div>
<div id="footer"><p>강원특별자치도 강릉시 하슬라로 100 | 033-640-1234</p></div>
</body></html>"""


def build_fixtures(d: Path) -> list[dict]:
    d.mkdir(parents=True, exist_ok=True)
    files = {
        # ① 표준 라벨형 하단 + 상단 최신 날짜 + 영문제호
        "std.html": PAGE.format(title="참뉴스 - 강원 대표 인터넷신문", og="참뉴스", logo="참뉴스",
                                header_extra=f'<span class="today">{BASE.replace("-", ".")} 최종수정</span>',
                                today="2026-07-28", footer=FOOTER_STD),
        # ② dl/dt 표 레이아웃 + 발행·편집인 병기 + 주소 부분일치 + 전화 표기차
        "dl.html": PAGE.format(title="서울시민일보", og="서울시민일보", logo="서울시민일보",
                               header_extra='<span class="date">2026-07-27</span>',
                               today="2026-07-27", footer=FOOTER_DL),
        # ③ 라벨 없음(형식·이름만) + 하단 발행일만(과거) → 발행연월일 미검출
        "nolabel.html": PAGE.format(title="원주소식", og="", logo="원주소식", header_extra="",
                                    today="2004-08-20", footer=FOOTER_NOLABEL),
        # ④ 정보가 상단에만 있는 사이트
        "toponly.html": PAGE.format(title="해운대타임스 | Haeundae Times", og="해운대타임스",
                                    logo="해운대타임스",
                                    header_extra=FOOTER_TOP_ONLY + '<span>2026.07.26</span>',
                                    today="2026-07-26", footer='<div id="footer">약관</div>'),
        # ⑤ 게재사항 대부분 없음
        "missing.html": PAGE.format(title="뉴스투데이", og="뉴스투데이", logo="뉴스투데이",
                                    header_extra="", today="2026-07-25", footer=FOOTER_MISSING),
        # ⑥ JS 셸
        "js.html": JS_SHELL,
        # ⑧⑨⑩ 2026-08-13 지침 전용
        "altname.html": PAGE_NODATE.format(title="한들뉴스", logo="한들뉴스",
                                           footer=FOOTER_ALTNAME),
        "today.html": PAGE_TODAY.format(today=BASE.replace("-", ".")),
        "company.html": PAGE_COMPANY,
    }
    for name, html in files.items():
        (d / name).write_text(html, encoding="utf-8")

    return [
        {"id": "F01", "제호": "참뉴스", "file": "std.html", "등록번호": "강원 아00123",
         "발행인": "홍길동", "편집인": "김철수",
         "주소": "강원특별자치도 춘천시 중앙로 1 참뉴스빌딩 3층", "전화": "033-123-4567"},
        {"id": "F02", "제호": "서울시민일보", "file": "dl.html", "등록번호": "서울 아51234",
         "발행인": "이몽룡", "편집인": "이몽룡",
         "주소": "서울특별시 종로구 세종대로 100 7층", "전화": "02)3456-7890"},
        {"id": "F03", "제호": "원주소식", "file": "nolabel.html", "등록번호": "강원 아00004",
         "발행인": "성춘향", "편집인": "성춘향",
         "주소": "강원도 원주시 시청로 1", "전화": "033-999-1234"},
        {"id": "F04", "제호": "해운대타임스", "file": "toponly.html", "등록번호": "부산 아02345",
         "발행인": "최무선", "편집인": "최무선",
         "주소": "부산광역시 해운대구 센텀로 55", "전화": "051-777-8888"},
        {"id": "F05", "제호": "뉴스투데이", "file": "missing.html", "등록번호": "서울 아12345",
         "발행인": "장보고", "편집인": "장보고", "주소": "서울특별시 마포구 월드컵로 3",
         "전화": "02-111-2222"},
        {"id": "F06", "제호": "제주바다뉴스", "file": "js.html", "등록번호": "제주 아00777",
         "발행인": "김만덕", "편집인": "김만덕", "주소": "제주특별자치도 제주시 중앙로 7",
         "전화": "064-555-6666"},
        # ⑦ override 경로: 입력 URL 은 접속 불가(JS·차단) + override 의 rendered_html/수동값 사용
        {"id": "F07", "제호": "동해파도뉴스", "file": "std.html", "등록번호": "강원 아00123",
         "발행인": "홍길동", "편집인": "김철수", "url": "http://donghae-pado.example",
         "주소": "강원특별자치도 춘천시 중앙로 1 참뉴스빌딩 3층", "전화": "033-123-4567"},
        # ⑧ 이름 항목 대체 인정 — 등록부의 발행인·편집인은 화면에 없는 사람이다.
        #    라벨 없이 이름만 일치하는 경로로 통과하면 안 되므로 일부러 다른 이름을 넣었다.
        {"id": "F08", "제호": "한들뉴스", "file": "altname.html", "등록번호": "충북 아00567",
         "발행인": "정약용", "편집인": "정약용",
         "주소": "충청북도 청주시 상당구 상당로 155", "전화": "043-222-3333"},
        # ⑨ '투데이' 시계 — 날짜로 인정하면 안 된다
        {"id": "F09", "제호": "한빛일보", "file": "today.html", "등록번호": "경기 아00888",
         "발행인": "이순신", "편집인": "이순신",
         "주소": "경기도 수원시 팔달구 정조로 1", "전화": "031-111-2222"},
        # ⑩ 지침 I-3 — 매체명 상이 + 뉴스 없음 → 홈페이지 없음.
        #    등록 주소·전화가 화면(하슬라로 100 / 033-640-1234)과 **달라야** 한다.
        #    같으면 게재사항 관문에서 '같은 매체' 로 통과해 I-2 가 된다 — 그건 남의 회사가 아니다.
        {"id": "F10", "제호": "강릉소식", "file": "company.html", "등록번호": "강원 아00999",
         "발행인": "허난설헌", "편집인": "허난설헌",
         "주소": "강원특별자치도 강릉시 경강로 2000", "전화": "033-111-5555"},
    ]


def build_override(workdir: Path, fixture_dir: Path) -> None:
    """F07 용 override — 도메인 키로 조회되고 rendered_html·수동값이 적용되는지 확인."""
    (workdir / "overrides").mkdir(parents=True, exist_ok=True)
    (workdir / "overrides" / "donghae-pado.example.json").write_text(json.dumps({
        "domain": "donghae-pado.example",
        "rendered_html": (fixture_dir / "std.html").as_uri(),
        "fields": {"제호": {"값": "동해파도뉴스", "근거": "하단 '제호 : 동해파도뉴스'", "위치": "하단"}},
        "pinned": True,
        "notes": "JS 렌더링 — Chrome 저장 HTML 로 판독",
    }, ensure_ascii=False), encoding="utf-8")


def build_registry(path: Path, rows: list[dict], fixture_dir: Path) -> None:
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "등록정보"
    ws.append(["언론사_id", "제호", "URL", "등록번호", "발행인", "편집인",
               "발행소 주소", "발행소 전화번호", "조사_결과_요약"])
    for r in rows:
        url = r.get("url") or (fixture_dir / r["file"]).as_uri()
        ws.append([r["id"], r["제호"], url, r["등록번호"],
                   r["발행인"], r["편집인"], r["주소"], r["전화"], "운영"])
    wb.save(path)


EXPECT = {
    "F01": {"필수8_게재수": 8, "준수여부_8항목": "준수",
            "제호_게재값": "참뉴스", "등록번호_게재값": "강원아00123",
            "등록연월일_게재값": "2015-03-02", "발행인_게재값": "홍길동", "편집인_게재값": "김철수",
            "발행연월일_게재값": "2026-07-28", "발행소 전화번호_게재값": "033-123-4567",
            "등록번호_대조": "일치", "발행인_대조": "일치", "편집인_대조": "일치",
            "발행소 주소_대조": "일치", "발행소 전화번호_대조": "일치", "창간일_게재값": "2015-03-10",
            "제호 영문_게재값": "Cham News", "제호 영문_추출방법": "저작권표기(확인필요)"},
    "F02": {"필수8_게재수": 8, "발행인_게재값": "이몽룡", "편집인_게재값": "이몽룡",
            "등록번호_게재값": "서울아51234호", "등록연월일_게재값": "2011-05-09",
            "등록번호_대조": "일치", "발행소 주소_대조": "부분일치",
            "발행소 전화번호_대조": "일치", "발행연월일_게재값": "2026-07-27"},
    # ★ 2026-08-13 지침 반영으로 기대치가 바뀐 유일한 기존 픽스처.
    #   종전 기준: "하단 발행일(과거)만 있으면 발행연월일 미게재" → '' / 미준수
    #   지침 R7-1: "발행연월일 명칭으로 날짜 기재 → 준수" → 하단 `발행일 : 2004년 8월 20일`
    #   이 그대로 준수 근거다. 정책이 바뀐 것이지 테스트를 통과시키려 고친 것이 아니다.
    "F03": {"발행연월일_게재값": "2004-08-20", "준수여부_8항목": "준수",
            "발행연월일_추출방법": "라벨",
            "등록연월일_게재값": "2004-08-11", "등록연월일_추출방법": "등록번호 병기 날짜",
            "등록번호_대조": "일치", "발행인_대조": "일치(라벨없음)",
            "발행소 주소_게재값": "강원도 원주시 시청로 1", "발행소 주소_대조": "일치",
            "확인필요": "Y"},
    "F04": {"등록번호_게재값": "부산아02345", "발행인_게재값": "최무선", "편집인_게재값": "최무선",
            "등록번호_대조": "일치", "발행소 전화번호_대조": "일치"},
    "F05": {"준수여부_8항목": "미준수", "확인필요": "Y"},
    "F06": {"js_의심": "Y", "확인필요": "Y", "준수여부_8항목": "미준수"},
    "F07": {"제호_게재값": "동해파도뉴스", "제호_추출방법": "수동", "제호_확신도": "high",
            "필수8_게재수": 8, "준수여부_8항목": "준수", "등록번호_대조": "일치",
            "발행소 전화번호_대조": "일치"},
    # ⑧ 지침 R13-2/R15-2 — `청소년보호책임자` 만 있어도 발행인·편집인 준수.
    #    등록부의 정약용과 다른 사람(박영수)이므로 '등록이름 일치' 경로가 아님이 보장된다.
    #    대조는 당연히 불일치이나 그것은 제9조 변경등록 사안이지 제21조 위반이 아니다.
    "F08": {"필수8_게재수": 8, "준수여부_8항목": "준수",
            "발행인_게재값": "박영수", "편집인_게재값": "박영수",
            "발행인_추출방법": "이름항목대체(청소년보호책임자)",
            "편집인_추출방법": "이름항목대체(청소년보호책임자)",
            "등록연월일_게재값": "2015-03-02",
            "등록연월일_추출방법": "등록번호 병기 날짜",
            "홈페이지_운영현황": "있음", "운영현황_로직": "I-1",
            "등록번호_대조": "일치", "발행인_대조": "불일치"},
    # ⑨ '투데이 2026.07.28' 은 날짜가 아니다 (지침 R7-2 의 유일한 제외)
    "F09": {"발행연월일_게재값": "", "등록연월일_게재값": "",
            "준수여부_8항목": "미준수", "홈페이지_운영현황": "있음", "확인필요": "Y"},
    # ⑩ 지침 I-3 — 매체명 상이 + 뉴스 없음. 미준수(과태료 대상)가 아니라 점검 제외다.
    "F10": {"홈페이지_운영현황": "없음", "운영현황_로직": "I-3",
            "매체명_동일": "N", "뉴스_있음": "N",
            "준수여부_8항목": "홈페이지없음", "확인필요": "Y"},
}


def unit_checks() -> list[str]:
    """지침 비고의 교차 준수 규칙과 운영현황 판정 — 픽스처로는 재현하기 어려운 경로.

    교차규칙은 규칙 추출 단계에서 거의 발동하지 않는다(다른 폴백이 먼저 값을 채운다).
    실제로는 LLM·비전이 값을 기각한 뒤 `merge_llm` 에서 발동하므로 직접 호출로 검증한다.
    """
    sys.path.insert(0, str(SCRIPTS))
    from md_core.fields import apply_cross_rules
    from md_core import operation

    fails = []

    def item(v, 판단근거=""):
        return {"값": v, "근거": f"근거:{v}", "위치": "하단", "확신도": "high",
                "추출방법": "라벨", "판단근거": 판단근거}

    def chk(name, got, want):
        if got != want:
            fails.append(f"[unit] {name}: 기대 {want!r} ≠ 실제 {got!r}")

    # R7 비고 — 등록연월일 준수면 발행연월일도 무조건 준수
    r = apply_cross_rules({"등록연월일": item("2015-03-02"), "발행연월일": item("")})
    chk("R7비고 등록연월일→발행연월일", r["발행연월일"]["값"], "2015-03-02")

    # R10 비고 — 발행연월일 준수면 등록연월일도 준수
    r = apply_cross_rules({"발행연월일": item("2015-03-02", "발행연월일 라벨"),
                           "등록연월일": item("")})
    chk("R10비고 발행연월일→등록연월일", r["등록연월일"]["값"], "2015-03-02")

    # R10 비고 단서 — **기사 업로드 일시로 확인한 경우는 제외**. 전이되면 안 된다.
    r = apply_cross_rules({"발행연월일": item("2026-07-25", "기사 업로드 날짜"),
                           "등록연월일": item("")})
    chk("R10비고 단서(기사 업로드는 전이 금지)", r["등록연월일"]["값"], "")

    # R12·R14 비고 — 발행인 ↔ 편집인 양방향
    r = apply_cross_rules({"편집인": item("김철수"), "발행인": item("")})
    chk("R12비고 편집인→발행인", r["발행인"]["값"], "김철수")
    r = apply_cross_rules({"발행인": item("홍길동"), "편집인": item("")})
    chk("R14비고 발행인→편집인", r["편집인"]["값"], "홍길동")

    # 지침 I — 4분기 판정
    z_뉴스 = {"상단": "한들뉴스", "하단": "", "전체": "한들뉴스\n오늘의 뉴스", "title": "한들뉴스"}
    chk("I-1 매체명 동일",
        operation.evaluate(접속결과="가능", 등록제호="한들뉴스", zones=z_뉴스)["로직"], "I-1")
    z_타사뉴스 = {"상단": "청주포스트", "하단": "", "전체": "청주포스트\n기사 목록", "title": "청주포스트"}
    chk("I-2 매체명 상이·뉴스 있음",
        operation.evaluate(접속결과="가능", 등록제호="한들뉴스", zones=z_타사뉴스)["로직"], "I-2")
    z_회사 = {"상단": "강릉건설", "하단": "", "전체": "강릉건설\n회사 소개", "title": "강릉건설"}
    chk("I-3 매체명 상이·뉴스 없음",
        operation.evaluate(접속결과="가능", 등록제호="강릉소식", zones=z_회사)["로직"], "I-3")
    # 매체명도 다르고 뉴스 표기도 없지만 게재사항이 일치하면 같은 매체다 (지침 I 비고).
    # 가상 매체명·가상 성명으로 같은 운영현황 분기를 재현한다.
    z_사각 = {"상단": "가상교육방송", "하단": "대표자 : 홍길동 | 연락처 비공개",
            "전체": "가상교육방송\n대표자 : 홍길동", "title": "가상교육방송"}
    r = operation.evaluate(접속결과="가능", 등록제호="가상인터넷신문사", zones=z_사각,
                           등록정보={"발행인": "홍길동"})
    chk("게재사항 일치로 구제(I-2)", r["로직"], "I-2")
    chk("게재사항 일치 항목 기록", r["게재사항_일치"], ["발행인"])
    r = operation.evaluate(접속결과="가능", 등록제호="가상인터넷신문사", zones=z_사각,
                           등록정보={"발행인": "이순신"})
    chk("게재사항 불일치면 I-3", r["로직"], "I-3")
    chk("I-4 접속 실패",
        operation.evaluate(접속결과="불가", 등록제호="한들뉴스")["운영현황"], "없음")
    chk("I-4 도메인 파킹",
        operation.evaluate(접속결과="가능", 등록제호="한들뉴스", zones=z_뉴스,
                           파킹의심="Y")["운영현황"], "없음")
    # 제호에 '투데이' 가 든 매체를 시계로 오인하면 안 된다
    chk("매체명 포함관계 인정",
        operation.title_matches("데일리안", 상단="데일리안뉴스 로고")[0], True)
    return fails


def run(cmd: list[str]) -> dict:
    # 한국어 Windows 의 로케일 기본값(cp949)으로는 자식의 UTF-8 한글 출력을 디코딩 못 한다.
    # 자식 쪽도 마찬가지다 — PYTHONIOENCODING 을 주지 않으면 요약 JSON 의 '—' 하나에
    # UnicodeEncodeError 로 죽는다. orchestrate.py 는 이미 이렇게 넘긴다.
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    p = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env)
    if p.returncode != 0:
        print(p.stdout[-2000:], p.stderr[-2000:], file=sys.stderr)
        raise SystemExit(f"실패: {' '.join(cmd)}")
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError:
        print(p.stdout[-2000:])
        return {}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--out", default="", help="산출 폴더 (기본: 임시 디렉터리)")
    args = ap.parse_args()

    # 마운트된 사용자 폴더에는 삭제 권한이 없을 수 있어 기본값을 임시 디렉터리로 둔다
    tmp = Path(args.out) if args.out else Path(tempfile.gettempdir()) / "disclosure_fixture_run"
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    fx = tmp / "sites"
    rows = build_fixtures(fx)
    reg = tmp / "등록정보_픽스처.xlsx"
    build_registry(reg, rows, fx)
    wd = tmp / "work_dir"
    build_override(wd, fx)

    print("① run_extract");     s1 = run([sys.executable, str(SCRIPTS / "run_extract.py"),
                                          "--workdir", str(wd), "--input", str(reg),
                                          "--기준일", BASE, "--workers", "3"])
    print("② compare_registry"); s2 = run([sys.executable, str(SCRIPTS / "compare_registry.py"),
                                           "--workdir", str(wd), "--input", str(reg), "--기준일", BASE])
    print("③ finalize");        s3 = run([sys.executable, str(SCRIPTS / "finalize.py"),
                                          "--workdir", str(wd), "--input", str(reg), "--기준일", BASE])

    verify = list(csv.DictReader((wd / "outputs" / f"verify_{BASE}.csv").open(encoding="utf-8-sig")))
    by_id = {r["언론사_id"]: r for r in verify}

    fails: list[str] = unit_checks()
    for sid, exp in EXPECT.items():
        row = by_id.get(sid)
        if not row:
            fails.append(f"{sid}: 행 없음")
            continue
        for col, want in exp.items():
            got = str(row.get(col, "")).strip()
            if str(want).strip() != got:
                fails.append(f"{sid}.{col}: 기대 {want!r} ≠ 실제 {got!r}")

    print("\n=== 요약 ===")
    print(json.dumps({"extract": {k: s1.get(k) for k in ("대상_사이트", "접속실패", "js_의심",
                                                          "8항목_전부게재", "확인필요")},
                      "compare": {k: s2.get(k) for k in ("사이트_수", "확인필요", "항목별_대조판정")},
                      "final": {k: s3.get(k) for k in ("final_xlsx", "준수(8항목 전부게재)", "확인필요")}},
                     ensure_ascii=False, indent=2))
    print("\n=== 항목별 결과 (검증용) ===")
    cols = ["언론사_id", "홈페이지_운영현황", "운영현황_로직", "필수8_게재수", "준수여부_8항목",
            "제호_게재값", "등록번호_게재값", "등록연월일_게재값", "발행인_게재값", "편집인_게재값",
            "발행연월일_게재값", "발행소 주소_게재값", "발행소 전화번호_게재값",
            "판단근거", "확인필요_사유"]
    for r in verify:
        print(" | ".join(f"{c}={r.get(c, '')}" for c in cols))

    if fails:
        print("\n❌ 불일치 %d건" % len(fails))
        for f in fails:
            print("  -", f)
    else:
        print("\n✅ 기대치 전부 일치")
    if not args.keep:
        print(f"\n(산출물 보존: {tmp})")
    raise SystemExit(1 if fails else 0)


if __name__ == "__main__":
    main()
