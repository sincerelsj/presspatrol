#!/usr/bin/env python3
"""fingerprint_sites — R5a. 매체별 '동일 사이트 지문' 을 만든다. **사이트 재방문 없음.**

무엇을 푸는가
-------------
등록원부에는 별개 제호로 올라 있지만 실제로는 한 사이트를 스킨만 바꿔 돌리는 매체가 있다.
실측 사례(2026-09-11): `주식회사 창업경영신문사` 34개 제호 = 34개 도메인인데 푸터 고정문구가
글자 단위로 같고 대표전화·팩스·이메일 도메인·청소년보호책임자까지 같다.
`투데이미디어그룹(주)` 94개 제호는 아예 `*.today.kr` 서브도메인을 쓴다.

이 스크립트는 판정하지 않는다. **비교 가능한 지문만 만든다.** 군집·등급은 R5b 가 한다.

입력 (전부 저장본 — 네트워크 안 씀. `--dns` 를 줄 때만 DNS 조회)
  work/{id}.json                 상·하단 텍스트, findings, final_url
  shots/{id}_rendered.html       렌더된 DOM (계정토큰·구조·링크)
  shots/{id}_article.html        최신 기사 페이지 (canonical·기사ID)
  input xlsx                     등록원부 (발행소명·발행인·전화·주소)

출력
  work/{id}.fp.json              매체별 지문 (사이드카 — audit.SIDECAR_SUFFIXES 에 등록돼 있다)
  work/_dns_cache.json           --dns 일 때 호스트별 A레코드 캐시

사용
  python fingerprint_sites.py --workdir ver_1/run_full --input ver_1/input/2026_실태점검_모집단.xlsx --workers 8
  python fingerprint_sites.py --workdir ver_1/run_full --ids 6149,6187 --force
  python fingerprint_sites.py --workdir ver_1/run_full --dns-only
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import re
import socket
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from md_core import audit                                        # noqa: E402
from md_core import identity as I                                # noqa: E402

# ── 렌더HTML 파싱 (bs4 는 1.6GB 에 너무 느리다 — 정규식으로 훑는다) ──────────
RE_META = re.compile(r"<meta\b[^>]{0,400}>", re.I)
RE_LINK = re.compile(r"<link\b[^>]{0,400}>", re.I)
RE_ATTR = re.compile(r"""(\w[\w:.-]*)\s*=\s*["']([^"']*)["']""")
RE_GA = re.compile(r"\b(UA-\d{4,10}-\d{1,4}|G-[A-Z0-9]{8,12})\b")
RE_GTM = re.compile(r"\bGTM-[A-Z0-9]{4,10}\b")
RE_ADS = re.compile(r"ca-pub-\d{10,20}")
RE_CLASS = re.compile(r"""class\s*=\s*["']([^"']{1,300})["']""")
RE_TAG = re.compile(r"<([a-zA-Z][a-zA-Z0-9]{0,12})")
RE_HREF = re.compile(r"""href\s*=\s*["']https?://([A-Za-z0-9.\-]{4,80})""", re.I)
RE_TITLE = re.compile(r"<title[^>]*>(.{0,300}?)</title>", re.I | re.S)
RE_ARTID = re.compile(r"(?:idx|no|num|nid|aid|articleno|article_id|seq|id)\s*=\s*(\d{3,12})", re.I)
RE_ARTPATH = re.compile(r"/(?:news|article|view|read)[^/]*/(\d{4,12})")

MAX_CLASSES = 3000
MAX_TAGS = 400


def _attrs(tag: str) -> dict:
    return {k.lower(): v for k, v in RE_ATTR.findall(tag)}


def parse_html(html: str) -> dict:
    """렌더HTML → 계정토큰·구조·링크·메타."""
    out = {
        "ga": [], "gtm": [], "adsense": [], "naver_verify": [], "fb_app": "",
        "canonical": "", "og_url": "", "og_site_name": "", "generator": "",
        "favicon": "", "title": "", "classes": [], "tag_seq_hash": "", "links": {},
    }
    if not html:
        return out
    for tag in RE_META.findall(html):
        a = _attrs(tag)
        name = (a.get("name") or a.get("property") or "").lower()
        content = (a.get("content") or "").strip()
        if not content:
            continue
        if name == "naver-site-verification":
            out["naver_verify"].append(content)
        elif name == "og:url":
            out["og_url"] = content
        elif name == "og:site_name":
            out["og_site_name"] = content
        elif name == "generator":
            out["generator"] = content[:80]
        elif name == "fb:app_id":
            out["fb_app"] = content
    for tag in RE_LINK.findall(html):
        a = _attrs(tag)
        rel = (a.get("rel") or "").lower()
        href = (a.get("href") or "").strip()
        if not href:
            continue
        if "canonical" in rel and not out["canonical"]:
            out["canonical"] = href
        elif "icon" in rel and not out["favicon"]:
            out["favicon"] = href[:200]
    out["ga"] = sorted(set(RE_GA.findall(html)))[:6]
    out["gtm"] = sorted(set(RE_GTM.findall(html)))[:6]
    out["adsense"] = sorted(set(RE_ADS.findall(html)))[:6]
    out["naver_verify"] = sorted(set(out["naver_verify"]))[:6]
    m = RE_TITLE.search(html)
    if m:
        out["title"] = re.sub(r"\s+", " ", m.group(1)).strip()[:120]
    cls = set()
    for c in RE_CLASS.findall(html):
        for w in c.split():
            if 1 < len(w) <= 40:
                cls.add(w)
        if len(cls) > MAX_CLASSES:
            break
    out["classes"] = sorted(cls)
    tags = RE_TAG.findall(html)[:MAX_TAGS]
    if tags:
        import hashlib
        out["tag_seq_hash"] = hashlib.blake2b(
            ",".join(t.lower() for t in tags).encode(), digest_size=8).hexdigest()
    links = Counter()
    for h in RE_HREF.findall(html):
        links[I.host_of(h)] += 1
    out["links"] = dict(links)
    return out


def article_info(html: str) -> dict:
    """기사 페이지 → canonical·기사ID·제목해시. 같은 기사를 두 도메인이 내면 미러다."""
    out = {"canonical": "", "og_url": "", "기사ID": "", "제목": "", "제목해시": ""}
    if not html:
        return out
    p = parse_html(html)
    out["canonical"] = p["canonical"]
    out["og_url"] = p["og_url"]
    out["제목"] = p["title"]
    if p["title"]:
        import hashlib
        core = re.sub(r"[\s\W_]+", "", p["title"])[:60]
        if len(core) >= 8:
            out["제목해시"] = hashlib.blake2b(core.encode(), digest_size=8).hexdigest()
    url = p["canonical"] or p["og_url"]
    m = RE_ARTID.search(url) or RE_ARTPATH.search(url)
    if m:
        out["기사ID"] = m.group(1)
    return out


# ── 워커 전역 (ProcessPool initializer 로 한 번만 넘긴다) ───────────────────
_G = {}


def _init(workdir: str, registry: dict, pop_hosts: list):
    _G["work"] = Path(workdir) / "work"
    _G["shots"] = Path(workdir) / "shots"
    _G["reg"] = registry
    _G["pop"] = set(pop_hosts)


def _read(p: Path, limit: int = 6_000_000) -> str:
    try:
        with open(p, encoding="utf-8", errors="ignore") as f:
            return f.read(limit)
    except OSError:
        return ""


def fingerprint(rid: str) -> dict:
    work, shots = _G["work"], _G["shots"]
    try:
        d = json.loads((work / f"{rid}.json").read_text(encoding="utf-8"))
    except Exception as e:                                       # noqa: BLE001
        return {"언론사_id": rid, "오류": f"work 읽기 실패: {e}"}

    row = d.get("row") or {}
    findings = d.get("findings") or {}

    def fv(k: str) -> str:
        return ((findings.get(k) or {}).get("값") or "").strip()

    top, foot = d.get("상단_텍스트") or "", d.get("하단_텍스트") or ""
    text = top + "\n" + foot
    제호등록 = (d.get("제호_등록") or "").strip()

    html = _read(shots / f"{rid}_rendered.html")
    P = parse_html(html)
    art = article_info(_read(shots / f"{rid}_article.html", 2_000_000))

    # ── 호스트 ────────────────────────────────────────────────────────────
    final_host = I.host_of(d.get("final_url") or row.get("최종URL") or "")
    reg_host = I.host_of(d.get("url") or row.get("메인_url") or "")
    canon_host = I.host_of(P["canonical"])
    og_host = I.host_of(P["og_url"])
    art_host = I.host_of(art["canonical"] or art["og_url"])
    제외 = ""
    if I.ERROR_HOSTS.search(final_host or ""):
        제외 = f"호스팅 만료·오류·파킹 안내 페이지({final_host})"
    elif (row.get("파킹의심") or "").strip() == "Y":
        제외 = "파킹의심=Y"
    elif not final_host:
        제외 = "최종 호스트 없음(접속 실패)"

    # ── 연락처 ────────────────────────────────────────────────────────────
    tels = {I.norm_phone(x) for x in I.TEL_LABEL_RE.findall(text)}
    tels.add(I.norm_phone(fv("발행소 전화번호")))
    faxes = {I.norm_phone(x) for x in I.FAX_LABEL_RE.findall(text)}
    tels.discard("")
    faxes.discard("")
    tels -= faxes
    emails = {e.lower() for e in I.EMAIL_RE.findall(text)[:60]}
    emails |= {e.lower() for e in I.EMAIL_RE.findall(html[:400_000])[:60]} if html else set()
    emails = {e for e in emails if not e.endswith((".png", ".jpg", ".gif", ".svg"))}
    mail_doms = {e.split("@")[-1] for e in emails}
    mail_doms = {m for m in mail_doms if m and m not in I.FREE_MAIL}
    bizno = {"-".join(m) for m in I.BIZNO_RE.findall(text)}

    # ── 인물 ──────────────────────────────────────────────────────────────
    youth = sorted({m for m in I.YOUTH_RE.findall(text)})[:4]
    privacy = sorted({m for m in I.PRIVACY_RE.findall(text)})[:4]
    copyr = ""
    m = I.COPYRIGHT_RE.search(text)
    if m:
        copyr = re.sub(r"\s+", " ", m.group(1)).strip(" .,-")[:40]

    # ── 고정블록 ──────────────────────────────────────────────────────────
    블록줄, 블록 = I.fixed_block(top, foot, 제호=제호등록 or fv("제호"),
                                등록번호=fv("등록번호"))
    블록_mh = I.minhash(블록줄) if len(블록줄) >= 3 else []
    class_mh = I.minhash(P["classes"]) if len(P["classes"]) >= 10 else []

    # ── 링크그래프 (모집단 안의 다른 호스트로 나가는 링크만) ──────────────
    own = I.reg_domain(final_host)
    out_links = {}
    for h, c in P["links"].items():
        if not h or I.reg_domain(h) == own or I.COMMON_LINK_HOSTS.search(h):
            continue
        if h in _G["pop"] or I.reg_domain(h) in _G["pop"]:
            out_links[h] = c
    out_links = dict(sorted(out_links.items(), key=lambda x: -x[1])[:40])

    if not 제외:
        제외 = I.vendor_page(copyr, 블록줄)

    R = _G["reg"].get(rid, {})
    return {
        "언론사_id": rid,
        "생성": datetime.now().isoformat(timespec="seconds"),
        "제호_등록": 제호등록,
        "제외사유": 제외,
        "호스트": {
            "최종": final_host, "등록": reg_host, "canonical": canon_host,
            "og_url": og_host, "기사": art_host,
            "등록도메인": own, "등록도메인_등록URL": I.reg_domain(reg_host),
        },
        "계정토큰": {
            "ga": P["ga"], "gtm": P["gtm"], "adsense": P["adsense"],
            "naver_verify": P["naver_verify"], "fb_app": P["fb_app"],
        },
        "연락처": {
            "전화": sorted(tels)[:6], "팩스": sorted(faxes)[:4],
            "이메일": sorted(emails)[:12], "이메일도메인": sorted(mail_doms)[:6],
            "사업자번호": sorted(bizno)[:3],
        },
        "등록표기": {
            "등록번호": I.norm_regno(fv("등록번호")),
            "등록번호_원": fv("등록번호"),
            "등록연월일": fv("등록연월일"),
            "제호_게재": fv("제호"),
        },
        "인물": {
            "발행인": fv("발행인"), "편집인": fv("편집인"),
            "청소년보호책임자": youth, "개인정보책임자": privacy,
        },
        "조직": {
            "저작권": copyr, "주소_게재": fv("발행소 주소"),
            "주소키": I.addr_key(fv("발행소 주소")),
            "og_site_name": P["og_site_name"] or (d.get("og_site_name") or ""),
            "title": P["title"] or (d.get("title") or ""),
        },
        "구조": {
            "class_수": len(P["classes"]), "class_minhash": class_mh,
            "tag_seq_hash": P["tag_seq_hash"], "favicon": P["favicon"],
            "generator": P["generator"], "렌더HTML_있음": bool(html),
        },
        # 줄 목록을 그대로 남긴다 — R5b 가 정확 Jaccard·포함율을 재계산하고,
        # 사람이 군집 근거를 눈으로 볼 때도 이 줄들이 증빙이 된다.
        "고정블록": {"줄수": len(블록줄), "줄": 블록줄, "minhash": 블록_mh},
        "링크": {"모집단_외부호스트": out_links},
        "기사": art,
        "등록원부": {
            "발행소명": R.get("발행소명", ""), "발행인": R.get("발행인", ""),
            "편집인": R.get("편집인", ""), "전화": I.norm_phone(R.get("전화", "")),
            "주소키": I.addr_key(R.get("주소", "")), "등록번호": R.get("등록번호", ""),
            "등록일자": R.get("등록일자", ""), "제호": R.get("제호", ""),
        },
        "운영현황": row.get("홈페이지_운영현황", ""),
    }


def _task(rid: str) -> tuple:
    try:
        fp = fingerprint(rid)
    except Exception as e:                                       # noqa: BLE001
        return rid, {"언론사_id": rid, "오류": f"{type(e).__name__}: {e}"}, False
    out = _G["work"] / f"{rid}.fp.json"
    out.write_text(json.dumps(fp, ensure_ascii=False, indent=1), encoding="utf-8")
    return rid, fp, True


# ── 등록원부 ───────────────────────────────────────────────────────────────
def load_registry(path: Path) -> dict:
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    it = ws.iter_rows(values_only=True)
    hdr = list(next(it))
    idx = {h: i for i, h in enumerate(hdr) if h}
    need = {"최종ID": "최종ID", "제호": "제호", "URL": "URL", "등록번호": "등록번호",
            "발행소명": "발행소명", "발행인": "발행인", "편집인": "편집인",
            "발행소 전화번호": "전화", "발행소 주소": "주소", "등록일자": "등록일자"}
    out = {}
    for r in it:
        if not r or "최종ID" not in idx or not r[idx["최종ID"]]:
            continue
        rid = str(r[idx["최종ID"]]).strip()
        rec = {}
        for col, key in need.items():
            if col in idx and col != "최종ID":
                v = r[idx[col]]
                rec[key] = "" if v is None else str(v).strip()
        out[rid] = rec
    wb.close()
    return out


# ── DNS ────────────────────────────────────────────────────────────────────
def resolve_hosts(hosts, cache_path: Path, workers: int = 32) -> dict:
    """호스트 → A레코드. 사이트 방문이 아니라 이름 조회다. 캐시에 조회일시를 남긴다."""
    cache = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:                                        # noqa: BLE001
            cache = {}
    todo = [h for h in hosts if h and h not in cache]
    print(f"[DNS] 대상 {len(hosts)}개 · 캐시 {len(cache)}개 · 조회 {len(todo)}개")

    def one(h):
        try:
            infos = socket.getaddrinfo(h, None, socket.AF_INET, socket.SOCK_STREAM)
            return h, sorted({i[4][0] for i in infos})
        except Exception:                                        # noqa: BLE001
            return h, []

    t0 = time.time()
    done = 0
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        for h, ips in ex.map(one, todo):
            cache[h] = {"A": ips, "조회": datetime.now().isoformat(timespec="seconds")}
            done += 1
            if done % 500 == 0:
                print(f"  {done}/{len(todo)} ({time.time() - t0:.0f}s)", flush=True)
    cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    print(f"[DNS] 완료 {done}건 · {time.time() - t0:.0f}s · 캐시 {cache_path}")
    return cache


def attach_dns(workdir: Path, cache: dict) -> int:
    n = 0
    for p in sorted((workdir / "work").glob("*.fp.json")):
        try:
            fp = json.loads(p.read_text(encoding="utf-8"))
        except Exception:                                        # noqa: BLE001
            continue
        h = (fp.get("호스트") or {}).get("최종") or ""
        rec = cache.get(h) or {}
        fp["dns"] = {"호스트": h, "A": rec.get("A", []), "조회": rec.get("조회", "")}
        p.write_text(json.dumps(fp, ensure_ascii=False, indent=1), encoding="utf-8")
        n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description="R5a 지문 추출 — 저장본만 읽는다")
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--input", help="등록원부 xlsx (없으면 등록원부 칸이 빈 채로 만든다)")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--ids", help="쉼표로 구분한 언론사_id (일부만)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--force", action="store_true", help="이미 있는 .fp.json 도 다시 만든다")
    ap.add_argument("--dns", action="store_true", help="지문 추출 뒤 DNS A레코드를 붙인다")
    ap.add_argument("--dns-only", action="store_true", help="지문은 그대로 두고 DNS 만 붙인다")
    a = ap.parse_args()

    W = Path(a.workdir)
    work = W / "work"
    if not work.is_dir():
        print(f"work 폴더가 없다: {work}", file=sys.stderr)
        return 2

    registry = load_registry(Path(a.input)) if a.input else {}
    pop_hosts = sorted({I.host_of(r.get("URL", "")) for r in registry.values()} |
                       {I.reg_domain(I.host_of(r.get("URL", ""))) for r in registry.values()})
    pop_hosts = [h for h in pop_hosts if h]
    print(f"등록원부 {len(registry)}건 · 모집단 호스트 {len(pop_hosts)}개")

    if a.dns_only:
        hosts = set()
        for p in work.glob("*.fp.json"):
            try:
                hosts.add((json.loads(p.read_text(encoding="utf-8")).get("호스트") or {}).get("최종", ""))
            except Exception:                                    # noqa: BLE001
                pass
        cache = resolve_hosts(sorted(h for h in hosts if h), work / "_dns_cache.json")
        print(f"[DNS] .fp.json {attach_dns(W, cache)}건에 붙였다")
        return 0

    if a.ids:
        ids = [s.strip() for s in a.ids.split(",") if s.strip()]
    else:
        ids = sorted(p.stem for p in work.glob("*.json")
                     if not audit.is_sidecar(p.name) and not p.name.startswith("_"))
    if not a.force:
        ids = [i for i in ids if not (work / f"{i}.fp.json").exists()]
    if a.limit:
        ids = ids[:a.limit]
    if not ids:
        print("대상이 없다 (--force 를 주면 다시 만든다)")
        return 0
    print(f"지문 추출 대상 {len(ids)}건 · workers={a.workers}")

    t0 = time.time()
    ok = err = 0
    df = Counter()
    제외 = Counter()
    with cf.ProcessPoolExecutor(max_workers=a.workers, initializer=_init,
                                initargs=(str(W), registry, pop_hosts)) as ex:
        for n, (rid, fp, saved) in enumerate(ex.map(_task, ids, chunksize=16), 1):
            if fp.get("오류"):
                err += 1
            else:
                ok += 1
                for t in fp["연락처"]["전화"] + fp["연락처"]["팩스"]:
                    df[f"전화:{t}"] += 1
                for t in fp["계정토큰"]["ga"] + fp["계정토큰"]["adsense"]:
                    df[f"계정:{t}"] += 1
                제외[fp["제외사유"] or "(정상)"] += 1
            if n % 500 == 0:
                print(f"  {n}/{len(ids)} ({time.time() - t0:.0f}s)", flush=True)
    print(f"완료 {ok}건 · 오류 {err}건 · {time.time() - t0:.0f}s")
    print("제외사유:", 제외.most_common(6))
    print("빈출 토큰(벤더 잡음 후보):", df.most_common(8))

    if a.dns:
        hosts = set()
        for p in work.glob("*.fp.json"):
            try:
                hosts.add((json.loads(p.read_text(encoding="utf-8")).get("호스트") or {}).get("최종", ""))
            except Exception:                                    # noqa: BLE001
                pass
        cache = resolve_hosts(sorted(h for h in hosts if h), work / "_dns_cache.json")
        print(f"[DNS] .fp.json {attach_dns(W, cache)}건에 붙였다")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
