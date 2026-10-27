"""HTTP 클라이언트 (news-self-production-audit 의 na_core.http 와 동일 동작).

- User-Agent 크롬 위장, EUC-KR/CP949 자동 보정 (한국 CMS 대응)
- file:// URL 지원 → Chrome 으로 저장한 렌더링 HTML 판독·오프라인 테스트용
"""
from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, unquote

import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_log = logging.getLogger("md_core.http")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/121.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT = 12

# URL 한 개에 쓰는 **총 시간** 상한 (초). requests 의 timeout 은 소켓 1회 연산의 상한일
# 뿐이라 총 시간을 못 막는다 — ISS-060:
#   · read timeout 은 바이트를 받을 때마다 리셋된다. 12초 안에 1바이트씩 흘려보내는
#     서버에는 영원히 매달린다.
#   · DNS 해석(getaddrinfo)은 timeout 이 적용되기 **전** 이라 아예 상한이 없다.
#   · urllib3 Retry(total=2) 가 붙어 있어 최악의 경우가 3배가 된다.
# 2026-09-03 전수 R0 에서 10121(www.slnews.co.kr)이 34분을 잡아먹고 파이프라인 전체를
# 세웠다. 파이썬은 블록된 스레드를 못 깨우므로, 데몬 스레드에 태워 돌리고 시간이 넘으면
# **버리고 간다**. 스레드는 남지만 데몬이라 프로세스 종료를 막지 않는다.
# 50초인 이유: urllib3 Retry(total=2, backoff 0.5)가 5xx 에 3회 재시도를 붙이므로
# 정상 경로의 최악이 12×3+backoff ≈ 37초다. 그보다 낮게 잡으면 살아 있는 5xx 사이트가
# FetchDeadline 으로 잘못 기록된다(판정은 같지만 사유가 틀린다).
HARD_DEADLINE = 50


class FetchDeadline(Exception):
    """총 시간 상한 초과 — 응답을 기다리지 않고 포기했다."""


def _with_deadline(fn, sec: int):
    box: dict = {}

    def runner():
        try:
            box["v"] = fn()
        except BaseException as exc:      # noqa: BLE001
            box["e"] = exc

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    t.join(sec)
    if t.is_alive():
        raise FetchDeadline(f"{sec}s 초과")
    if "e" in box:
        raise box["e"]
    return box["v"]


def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    })
    retry = Retry(total=2, backoff_factor=0.5,
                  status_forcelist=(500, 502, 503, 504),
                  allowed_methods=frozenset(["GET", "HEAD"]))
    adapter = HTTPAdapter(max_retries=retry, pool_connections=16, pool_maxsize=16)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s


_SESSION: Optional[requests.Session] = None


def get_session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        _SESSION = _make_session()
    return _SESSION


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if url.startswith(("http://", "https://", "file://")):
        return url
    return "http://" + url.lstrip("/")


_SCHEME_ALT = (("http://", "https://"), ("https://", "http://"))


def swap_scheme(url: str) -> str:
    """http↔https 스킴 교체. 해당 없으면 원본 반환."""
    for a, b in _SCHEME_ALT:
        if url.startswith(a):
            return b + url[len(a):]
    return url


def _decode_score(s: str) -> float:
    """디코딩 후보의 품질 점수. 한글이 많고 치환문자가 적을수록 높다.

    치환문자(U+FFFD)가 하나도 없는 모지바케 — UTF-8 본문을 ISO-8859-1 로 읽은 경우 — 는
    '치환문자 개수' 비교로는 잡히지 않는다. 한글 비율을 봐야 한다 (ISS-012).
    """
    if not s:
        return -1.0
    hangul = sum(1 for c in s if "가" <= c <= "힣")
    bad = s.count("�")
    return (hangul - bad * 2) / len(s)


def _decode(resp: requests.Response) -> str:
    """한국 CMS 응답 인코딩 보정.

    requests 는 charset 미선언 시 ISO-8859-1 로 디코드하므로 UTF-8·EUC-KR 본문이 깨진다.
    후보 인코딩을 모두 시도해 한글이 가장 많이 살아나는 디코딩을 고른다.
    """
    best = resp.text
    best_score = _decode_score(best)
    for enc in ("utf-8", "euc-kr", "cp949"):
        try:
            cand = resp.content.decode(enc, errors="replace")
        except Exception:  # noqa: BLE001
            continue
        score = _decode_score(cand)
        if score > best_score:
            best, best_score = cand, score
    return best


def _get_once(url: str, timeout: int) -> tuple[requests.Response, bool]:
    """단일 GET. SSLError 면 verify=False 로 1회 재시도. (resp, ssl무검증여부) 반환.

    인증서 만료·호스트명 불일치는 게재사항 점검 대상이 아니다 — 본문만 확보하면 된다.
    리다이렉트 도중에도 발생하므로 스킴으로 분기하지 않는다(ISS-001).
    """
    try:
        return get_session().get(url, timeout=timeout, allow_redirects=True), False
    except requests.exceptions.SSLError:
        return get_session().get(url, timeout=timeout, allow_redirects=True, verify=False), True


def fetch_html_ex(url: str, *, timeout: int = DEFAULT_TIMEOUT) -> tuple[int, str, str, dict]:
    """URL → (status_code, text, final_url, meta).

    meta = {"경로": 성공 경로 설명, "ssl무검증": bool, "시도": [시도별 결과 문자열]}

    원 스킴이 5xx 를 반환하거나 예외로 실패하면 반대 스킴을 1회 시도한다.
    5xx 는 예외가 아니라 유효한 응답이므로 예외 핸들러로는 잡히지 않는다 — ISS-009.
    """
    url = normalize_url(url)
    meta: dict = {"경로": "", "ssl무검증": False, "시도": []}

    if url.startswith("file://"):
        raw = unquote(urlparse(url).path)
        # Windows: file:///C:/x → path 가 '/C:/x' 로 와서 Path 가 '\C:\x' 를 만든다
        if re.match(r"^/[A-Za-z]:/", raw):
            raw = raw[1:]
        try:
            meta["경로"] = "file"
            return 200, Path(raw).read_text(encoding="utf-8", errors="replace"), url, meta
        except Exception as exc:  # noqa: BLE001
            _log.warning(f"file 읽기 실패 {url}: {exc}")
            meta["시도"].append(f"file: {exc.__class__.__name__}")
            return 0, "", url, meta

    alt = swap_scheme(url)
    candidates = [url] + ([alt] if alt != url else [])
    first_5xx: tuple[int, str, str] | None = None

    for idx, cand in enumerate(candidates):
        t0 = time.time()
        try:
            # 총 시간 상한을 씌운다 — 소켓 timeout 만으로는 안 끝나는 서버가 있다(ISS-060)
            resp, ssl_off = _with_deadline(lambda: _get_once(cand, timeout), HARD_DEADLINE)
        except Exception as exc:  # noqa: BLE001
            meta["시도"].append(f"{cand} → {exc.__class__.__name__}")
            _log.debug(f"GET 실패 {cand}: {exc.__class__.__name__}")
            continue

        elapsed = int((time.time() - t0) * 1000)
        text = _decode(resp)
        meta["시도"].append(
            f"{cand} → {resp.status_code}"
            + (" (ssl무검증)" if ssl_off else "")
            + f" {len(text)}자 {elapsed}ms"
        )

        if resp.status_code >= 500:
            if first_5xx is None:
                first_5xx = (resp.status_code, text, str(resp.url))
            if idx + 1 < len(candidates):
                continue        # 반대 스킴을 시도해 본다
            break

        meta["ssl무검증"] = ssl_off
        meta["경로"] = ("원스킴" if idx == 0 else "스킴교체") + (" + ssl무검증" if ssl_off else "")
        _log.debug(f"GET {resp.status_code} {cand} ({elapsed} ms, {len(text)} chars)")
        return resp.status_code, text, str(resp.url), meta

    if first_5xx is not None:      # 모든 스킴이 5xx — 첫 응답을 증거로 돌려준다
        meta["경로"] = "5xx(모든 스킴)"
        return first_5xx[0], first_5xx[1], first_5xx[2], meta

    meta["경로"] = "실패"
    _log.warning(f"GET 실패(전 스킴) {url}: {' | '.join(meta['시도'])}")
    return 0, "", url, meta


def fetch_html(url: str, *, timeout: int = DEFAULT_TIMEOUT) -> tuple[int, str, str]:
    """URL → (status_code, decoded_text, final_url). 실패 시 (0, "", url).

    fetch_html_ex 의 하위호환 래퍼 (meta 를 버린다).
    """
    status, text, final, _ = fetch_html_ex(url, timeout=timeout)
    return status, text, final
