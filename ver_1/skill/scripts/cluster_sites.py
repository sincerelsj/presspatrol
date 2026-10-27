#!/usr/bin/env python3
"""cluster_sites — R5b. 지문(.fp.json)을 비교해 '같은 사이트/같은 운영주체' 군집을 만든다.

판정하지 않는다. **등급을 붙이고 근거를 남긴다.** 최종 확정은 사람이 한다(2026-09-11 결정).

세 등급
  A 동일 사이트      같은 호스트·같은 등록도메인·canonical 교차·같은 기사
  B 동일 운영주체    도메인은 달라도 식별 토큰이 여러 개 겹친다
  C 솔루션 공용      DOM 구조만 같다 — 같은 CMS 템플릿일 뿐, 운영주체 근거가 아니다

왜 C 를 따로 빼는가 (실측 2026-09-11)
  렌더HTML 353건 전 쌍 비교에서 class 집합 Jaccard = 1.00 인데 전화·이메일·저작권이 전부
  다른 쌍이 쏟아졌다(어반포스트 ↔ 위클리유연 ↔ CKL뉴스). 랜덤 쌍의 4.2% 가 J>0.3 이다.
  구조 유사도를 A·B 근거로 쓰면 오탐이 수천 건 나온다.

빈출 토큰 처리
  호스팅사의 공통 고객센터 번호가 여러 매체에 반복되기도 한다. 그래서 df(등장 매체 수)
  가 큰 토큰은 가중치를 깎는다. 다만 **0 으로 만들지는 않는다** — 투데이미디어그룹(주)처럼
  94개 제호를 한 사업자가 돌리면 그 사업자의 전화번호도 df 가 크기 때문이다. 대신
  **B 등급은 서로 다른 증거가 2종 이상**일 것을 요구해 벤더 잡음 단독으로는 묶이지 않게 한다.

사용
  python cluster_sites.py --workdir ver_1/run_full
  python cluster_sites.py --workdir ver_1/run_full --min-score 50 --vendor-df 40
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from md_core import identity as I                                # noqa: E402

# 한 토큰이 이만큼 많은 매체에 걸리면 그 토큰만으로는 후보쌍을 만들지 않는다.
# (같은 IP 500개 → 12만 쌍. 다른 근거로 이미 만들어진 쌍에서는 여전히 증거로 센다.)
PAIR_BLOCK_MAX = 120

# 고정블록의 한 줄이 이만큼 많은 매체에 나오면 '범용 문구' 다 — 식별 근거로 세지 않는다.
RARE_LINE_DF = 20

# 증거 종류 → (기본가중, 계열).
#   Z = URL 동일성      호스트·도메인·canonical·기사URL 이 같다. 판단할 게 없다 — 따로 뺀다
#   A = 고유 계정·등록 식별자   광고·분석 계정, 사업자번호, 게재 등록번호
#   B = 정황            연락처·주소·인물·고정블록·링크
#   C = 구조            DOM 템플릿 — 운영주체 근거가 아니다
#
# Z 를 A·B 와 섞지 않는 이유(2026-09-15 지시): 같은 URL 은 근거의 강약 문제가 아니라
# 애초에 같은 사이트다. 섞어 놓으면 "URL 이 같아서 A" 인 건과 "내용 증거로 A" 인 건이
# 한 칸에 들어가 검토 우선순위를 못 세운다. Z 를 빼고 나서도 묶이는지가 진짜 질문이다.
W = {
    "호스트동일":      (100, "Z"),
    "등록도메인동일":   (95, "Z"),
    "canonical교차":   (100, "Z"),
    "기사호스트교차":    (90, "Z"),
    "adsense":         (90, "A"),
    "naver_verify":    (85, "A"),
    "사업자번호":       (85, "A"),
    "ga":              (70, "A"),
    "등록번호게재":      (70, "A"),
    # 기사 제목 동일은 식별자가 아니다 — 같은 보도자료·통신사 기사를 여러 매체가 그대로 싣는다.
    "기사제목동일":      (60, "B"),
    "고정블록_희귀3+":   (60, "B"),
    "고정블록_희귀2":    (40, "B"),
    "고정블록_희귀1":    (15, "B"),
    "팩스":            (50, "B"),
    "이메일주소":       (45, "B"),
    "전화":            (45, "B"),
    "이메일도메인":      (40, "B"),
    "주소키":           (35, "B"),
    "저작권조직":       (35, "B"),
    "상호링크":         (40, "B"),
    "단방향링크":       (25, "B"),
    "동일IP":          (35, "B"),
    "공용도메인":       (30, "B"),   # 서브도메인 대여 플랫폼 — 같은 운영주체 근거가 아니다
    "인물동일":         (25, "B"),
    "class≥0.9":       (10, "C"),
    "태그구조동일":      (10, "C"),
}

# 저작권 문구가 이러면 조직명이 아니다 — 식별력 0.
GENERIC_COPYRIGHT = {
    "all rights reserved", "copyright all right reserved", "all right reserved",
    "copyright", "reserved", "rights reserved", "무단전재", "재배포 금지",
}


def damp(df: int) -> float:
    """df(그 토큰을 가진 매체 수) → 가중 계수. 벤더 잡음을 누르되 0 으로 만들지 않는다.

    **A 증거(호스트·등록도메인 동일)에는 적용하지 않는다.** 같은 도메인은 우연이 아니라
    범주적 동일성이다. 감쇠를 먹였더니 투데이미디어그룹(주)의 `*.today.kr` 94개가
    df 때문에 47점으로 깎여 '보류' 로 떨어졌다(2026-09-11). 플랫폼 도메인
    (tistory.com 등)은 PLATFORM_DOMAINS_RE 로 이미 토큰에서 빠진다.
    """
    if df <= 5:
        return 1.0
    if df <= 15:
        return 0.8
    if df <= 40:
        return 0.5
    if df <= 120:
        return 0.3
    return 0.15


# ── 제호 지역 변형 계열 ────────────────────────────────────────────────────
# 세계타임즈 18건은 `{지역}세계타임즈` 로 `*.thesegye.com` 서브도메인을 쓴다.
# 민정일보 5건은 `{지역}민정일보` 로 `*.kngd.kr` 을 쓴다. 등록원부상 각각 한 주체지만
# 실제로는 지역별로 따로 운영되는 지사·지역판이다(2026-09-15 사용자 확인).
# "한 사람이 서로 다른 제호를 여럿 돌리는 것" 과는 성격이 달라 Y 로 따로 라벨링한다.
_REGION: set = set()                       # main() 에서 시도명 + 주소에서 캔 시군구명으로 채운다
_SIDO = {"서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종", "경기", "강원",
         "충북", "충남", "전북", "전남", "경북", "경남", "제주", "한국", "대한민국",
         "전국", "수도권", "영남", "호남", "충청", "강원도", "제주도"}


def strip_region(제호: str) -> tuple:
    """제호 → (어간, 지역토큰을 떼어냈는가). '강원세계타임즈' → ('세계타임즈', True)

    괄호 뒤 별칭은 버린다('영주민정일보(영주민정방송,…)' → '영주민정일보').
    떼고 남는 어간이 3자 미만이면 떼지 않는다 — '서울신문'→'신문' 같은 과잉 제거 방지.
    """
    t = re.split(r"[(（]", str(제호 or ""))[0]
    t = re.sub(r"[\s·・\-_]+", "", t).strip()
    떼어냄 = False
    for _ in range(2):                     # '경기하남' 처럼 두 겹인 경우까지
        for r in sorted(_REGION, key=len, reverse=True):
            if t.startswith(r) and len(t) - len(r) >= 3:
                t, 떼어냄 = t[len(r):], True
                break
            if t.endswith(r) and len(t) - len(r) >= 3:
                t, 떼어냄 = t[:-len(r)], True
                break
        else:
            break
    return t, 떼어냄


def family_type(발행소명종수: int, 도메인종수: int, 매체수: int) -> str:
    """지역 변형 계열의 성격.

    `{지역}세계타임즈` 18건은 발행소명 1종·도메인 1종 → 한 브랜드의 지역판이다.
    `{지역}인터넷뉴스` 60건은 발행소명도 도메인도 제각각 → 그냥 흔한 작명이다.
    후자를 지역판으로 세면 '따로 기록' 의 의미가 없어진다.
    """
    if 발행소명종수 <= 1 or 도메인종수 <= 1:
        return "브랜드 지역판"
    if 발행소명종수 <= max(2, 0.3 * 매체수) or 도메인종수 <= max(2, 0.3 * 매체수):
        return "브랜드 지역판(일부 분리)"
    return "작명 유사"


class UF:
    def __init__(self):
        self.p = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def load_fps(work: Path) -> dict:
    fps = {}
    for p in sorted(work.glob("*.fp.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:                                        # noqa: BLE001
            continue
        if d.get("오류"):
            continue
        fps[d["언론사_id"]] = d
    return fps


def tokens_of(fp: dict) -> dict:
    """지문 → {증거종류: set(토큰)}. 여기 담긴 것만 블로킹·점수에 쓴다."""
    h = fp.get("호스트") or {}
    c = fp.get("연락처") or {}
    a = fp.get("계정토큰") or {}
    o = fp.get("조직") or {}
    p = fp.get("인물") or {}
    art = fp.get("기사") or {}
    # 플랫폼에 올린 페이지(blog.naver.com·v.daum.net 등)는 호스트가 같아도 같은 사이트가 아니다.
    _plat = bool(I.PLATFORM_DOMAINS_RE.search(I.reg_domain(h.get("최종", "")) or "x"))
    t = {
        "호스트동일": set() if _plat else ({h.get("최종", "")} - {""}),
        "등록도메인동일": ({h.get("등록도메인", "")} - {""}) if not I.PLATFORM_DOMAINS_RE.search(
            h.get("등록도메인", "") or "x") else set(),
        "adsense": set(a.get("adsense") or []),
        "naver_verify": set(a.get("naver_verify") or []),
        "ga": set(a.get("ga") or []),
        "사업자번호": set(c.get("사업자번호") or []),
        "전화": set(c.get("전화") or []),
        "팩스": set(c.get("팩스") or []),
        "이메일주소": set(c.get("이메일") or []),
        "이메일도메인": set(c.get("이메일도메인") or []),
        "주소키": {o.get("주소키", "")} - {""},
        "등록번호게재": {(fp.get("등록표기") or {}).get("등록번호", "")} - {""},
        "기사제목동일": {art.get("제목해시", "")} - {""},
        "태그구조동일": {(fp.get("구조") or {}).get("tag_seq_hash", "")} - {""},
        "동일IP": set((fp.get("dns") or {}).get("A") or []),
    }
    # 저작권 표기에서 연도·정형문구를 걷어내고 남는 '조직명' 이 3자 이상일 때만 근거로 쓴다.
    # ('Copyright 2020' 두 개가 겹쳤다고 같은 운영주체라 할 수 없다 — G0226 오탐, 2026-09-11)
    cr = (o.get("저작권") or "").strip().lower()
    core = re.sub(r"(?:19|20)\d\d|all\s*rights?\s*reserved|copyright|©|ⓒ|\(c\)|[\s.,\-–—]", "", cr)
    t["저작권조직"] = {cr} if cr not in GENERIC_COPYRIGHT and len(core) >= 3 else set()
    names = {v for v in (p.get("발행인", ""), p.get("편집인", "")) if len(v) >= 2}
    names |= {v for v in (p.get("청소년보호책임자") or []) + (p.get("개인정보책임자") or []) if len(v) >= 2}
    t["인물동일"] = names
    return {k: v for k, v in t.items() if v}


def main() -> int:
    ap = argparse.ArgumentParser(description="R5b 동일 사이트·동일 운영주체 군집화")
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--min-score", type=int, default=40, help="쌍 기록 하한")
    ap.add_argument("--a-score", type=int, default=90)
    ap.add_argument("--b-score", type=int, default=60)
    ap.add_argument("--vendor-df", type=int, default=40, help="이 이상이면 벤더 잡음 후보로 따로 보고")
    ap.add_argument("--out", default="", help="군집 JSON 경로 (기본 work/_clusters.json)")
    a = ap.parse_args()

    W_ = Path(a.workdir)
    work = W_ / "work"
    t0 = time.time()
    fps = load_fps(work)
    print(f"지문 {len(fps)}건 로드 ({time.time() - t0:.0f}s)")
    if not fps:
        print("지문이 없다 — 먼저 fingerprint_sites.py 를 돌려라", file=sys.stderr)
        return 2

    쓸수있음 = {i: f for i, f in fps.items() if not f.get("제외사유")}
    print(f"군집 대상 {len(쓸수있음)}건 · 제외 {len(fps) - len(쓸수있음)}건 "
          f"(오류·파킹 안내 페이지 — 같은 안내문을 같은 사이트로 보면 안 된다)")

    # ── 지역명 사전 ───────────────────────────────────────────────────────
    # 시도명 17개 + 게재된 발행소 주소에서 캔 시·군·구 이름. 고정 목록을 손으로 들고 있지
    # 않고 데이터에서 만든다. '영주민정일보' 의 '영주' 는 여기서 나온다.
    global _REGION
    _REGION = set(_SIDO)
    for f in 쓸수있음.values():
        addr = (f.get("조직") or {}).get("주소_게재", "")
        for m in re.finditer(r"([가-힣]{2,4})(?:시|군|구)\b", addr):
            if len(m.group(1)) >= 2:
                _REGION.add(m.group(1))
    print(f"  지역명 사전 {len(_REGION)}개 (시도 {len(_SIDO)} + 주소에서 캔 시군구)")

    # ── 토큰 색인 ─────────────────────────────────────────────────────────
    TOK = {i: tokens_of(f) for i, f in 쓸수있음.items()}
    index = defaultdict(set)                                     # (종류,값) → ids
    for i, tk in TOK.items():
        for kind, vals in tk.items():
            for v in vals:
                index[(kind, v)].add(i)
    df = {k: len(v) for k, v in index.items()}

    # ── 고정블록 줄 df ────────────────────────────────────────────────────
    # '청소년보호정책' 같은 줄은 수천 개 사이트에 있다. 희귀한 줄(주소·전화·조직명)만 근거다.
    line_df = Counter()
    for f in 쓸수있음.values():
        for ln in set(f.get("고정블록", {}).get("줄") or []):
            line_df[ln] += 1
    흔한줄 = [(ln, n) for ln, n in line_df.most_common(5) ]
    print(f"  고정블록 줄 {len(line_df):,}종 · 가장 흔한 줄: "
          + ", ".join(f"{ln[:18]}({n})" for ln, n in 흔한줄))

    # ── 공용도메인 판별 ───────────────────────────────────────────────────
    # `*.today.kr` 80건은 전부 투데이미디어그룹(주) 하나다 → 같은 사이트로 본다.
    # `*.newspro.kr` 65건은 발행소명이 제각각이다 → 서브도메인을 빌려주는 호스팅 플랫폼이다.
    # 둘의 차이는 **그 도메인 안에 등록원부 발행소명이 몇 종 있는가** 하나로 갈린다.
    # 손으로 도메인 블랙리스트를 관리하지 않고 데이터로 판별한다.
    공용도메인 = {}
    for (kind, dom), ids in list(index.items()):
        if kind != "등록도메인동일" or len(ids) < 5:
            continue
        주체 = {((쓸수있음[i].get("등록원부") or {}).get("발행소명") or
                (쓸수있음[i].get("등록원부") or {}).get("발행인") or "") for i in ids} - {""}
        if len(주체) >= max(3, 0.6 * len(ids)):
            공용도메인[dom] = {"매체수": len(ids), "발행소명종수": len(주체)}
    if 공용도메인:
        print(f"  공용도메인(서브도메인 대여) {len(공용도메인)}개 — A증거에서 강등: "
              f"{sorted(공용도메인.items(), key=lambda x: -x[1]['매체수'])[:4]}")

    # 고정블록·class 는 MinHash LSH 밴드로 후보를 만든다
    for i, f in 쓸수있음.items():
        for b in I.bands(f.get("고정블록", {}).get("minhash") or []):
            index[("_밴드_블록", b)].add(i)
        for b in I.bands(f.get("구조", {}).get("class_minhash") or []):
            index[("_밴드_class", b)].add(i)

    # ── 후보쌍 ────────────────────────────────────────────────────────────
    cand = set()
    skipped = []
    for (kind, val), ids in index.items():
        n = len(ids)
        if n < 2:
            continue
        if n > PAIR_BLOCK_MAX:
            if not kind.startswith("_밴드_"):
                skipped.append((kind, val, n))
            continue
        for x, y in combinations(sorted(ids), 2):
            cand.add((x, y))
    print(f"후보쌍 {len(cand):,}개 (전 쌍 {len(쓸수있음) * (len(쓸수있음) - 1) // 2:,} 대비 "
          f"{len(cand) / max(1, len(쓸수있음) * (len(쓸수있음) - 1) // 2) * 100:.3f}%)")
    if skipped:
        print(f"  블록 과대로 쌍 생성 제외 {len(skipped)}개 토큰 "
              f"(예: {sorted(skipped, key=lambda x: -x[2])[:3]})")

    # ── 채점 ──────────────────────────────────────────────────────────────
    pairs = []
    for x, y in cand:
        A, B = 쓸수있음[x], 쓸수있음[y]
        ev, z_ev = [], []          # 내용 증거 / URL 동일성 증거 — 절대 합치지 않는다
        score = z점수 = 0.0
        for kind in W:
            if kind in ("고정블록_희귀3+", "고정블록_희귀2", "고정블록_희귀1", "class≥0.9",
                        "canonical교차", "기사호스트교차", "상호링크", "단방향링크"):
                continue
            shared = TOK[x].get(kind, set()) & TOK[y].get(kind, set())
            for v in shared:
                d = df.get((kind, v), 2)
                label, base, typ = kind, W[kind][0], W[kind][1]
                if kind == "등록도메인동일" and v in 공용도메인:
                    # 서브도메인 대여 플랫폼은 URL 이 같아도 같은 사이트가 아니다 → Z 에서 뺀다
                    label, base, typ = "공용도메인", 30, "B"
                w = base * (1.0 if typ == "Z" else damp(d))
                if typ == "Z":
                    z점수 += w
                    z_ev.append({"종류": label, "값": v, "df": d, "점수": round(w, 1)})
                else:
                    score += w
                    ev.append({"종류": label, "값": v, "df": d, "점수": round(w, 1)})

        # 호스트 교차 — canonical·og:url·기사 URL 이 상대 호스트를 가리키면 같은 사이트다
        hx, hy = A["호스트"], B["호스트"]
        def _own(hosts):
            """플랫폼에 올린 페이지는 URL 동일성 근거가 못 된다.
            등록 URL 이 v.daum.net·blog.naver.com 인 매체끼리 '같은 사이트' 가 되면 안 된다."""
            return {v for v in hosts
                    if v and not I.PLATFORM_DOMAINS_RE.search(I.reg_domain(v) or "x")}

        cross = _own({hx.get("canonical"), hx.get("og_url")} & {hy.get("최종")}) or \
                _own({hy.get("canonical"), hy.get("og_url")} & {hx.get("최종")})
        cross.discard("")
        if cross:
            z점수 += W["canonical교차"][0]
            z_ev.append({"종류": "canonical교차", "값": sorted(cross)[0], "df": 2,
                         "점수": W["canonical교차"][0]})
        artcross = _own({hx.get("기사")} & {hy.get("최종")}) or _own({hy.get("기사")} & {hx.get("최종")})
        artcross.discard("")
        if artcross:
            z점수 += W["기사호스트교차"][0]
            z_ev.append({"종류": "기사호스트교차", "값": sorted(artcross)[0], "df": 2,
                         "점수": W["기사호스트교차"][0]})

        # 링크그래프 — 상대 호스트를 실제로 걸고 있는가
        lx = set((A.get("링크") or {}).get("모집단_외부호스트") or {})
        ly = set((B.get("링크") or {}).get("모집단_외부호스트") or {})
        a2b = bool({hy.get("최종")} & lx or {hy.get("등록도메인")} & {I.reg_domain(h) for h in lx})
        b2a = bool({hx.get("최종")} & ly or {hx.get("등록도메인")} & {I.reg_domain(h) for h in ly})
        if a2b and b2a:
            score += W["상호링크"][0]
            ev.append({"종류": "상호링크", "값": f"{hx.get('최종')}↔{hy.get('최종')}", "df": 2,
                       "점수": W["상호링크"][0]})
        elif a2b or b2a:
            score += W["단방향링크"][0]
            ev.append({"종류": "단방향링크", "값": f"{hx.get('최종')}→{hy.get('최종')}" if a2b
                       else f"{hy.get('최종')}→{hx.get('최종')}", "df": 2, "점수": W["단방향링크"][0]})

        # 고정블록 — 줄 집합 Jaccard 에 **희귀줄 요건**을 건다.
        # Jaccard 만 쓰면 '청소년보호정책 / 이메일무단수집거부 / 기사제보' 같은 범용 푸터
        # 메뉴 3~4줄짜리 사이트끼리 J=1.0 이 되어 1,646건짜리 거대 군집이 생겼다(2026-09-11).
        # 전 매체에서 세어 df 가 작은 줄(주소·전화·조직명)이 몇 개나 겹치는지가 진짜 근거다.
        LA, LB = set(A["고정블록"].get("줄") or []), set(B["고정블록"].get("줄") or [])
        공통 = LA & LB
        bj = len(공통) / len(LA | LB) if (LA and LB) else 0.0
        희귀 = [ln for ln in 공통 if line_df.get(ln, 0) <= RARE_LINE_DF]
        if len(LA) >= 3 and len(LB) >= 3 and bj >= 0.3 and 희귀:
            kind = ("고정블록_희귀3+" if len(희귀) >= 3 else
                    "고정블록_희귀2" if len(희귀) == 2 else "고정블록_희귀1")
            w = W[kind][0]
            score += w
            ev.append({"종류": kind, "df": 2, "점수": w,
                       "값": f"J={bj:.2f} 공통 {len(공통)}줄 중 희귀 {len(희귀)}줄: "
                             + " | ".join(sorted(희귀, key=len, reverse=True)[:2])[:120]})

        cj = I.mh_jaccard(A["구조"].get("class_minhash") or [], B["구조"].get("class_minhash") or [])
        if cj >= 0.9:
            score += W["class≥0.9"][0]
            ev.append({"종류": "class≥0.9", "값": f"J={cj:.2f}", "df": 2, "점수": W["class≥0.9"][0]})

        kinds = {e["종류"] for e in ev}
        A급 = {k for k in kinds if W.get(k, (0, ""))[1] == "A"}
        식별종수 = len({k for k in kinds if W.get(k, (0, ""))[1] in ("A", "B")})
        if z_ev:
            등급 = "Z"                       # URL 이 같다 — 내용 증거와 무관하게 Z
        elif not ev or score < a.min_score:
            continue
        elif A급 and score >= a.a_score:
            등급 = "A"
        elif score >= a.b_score and 식별종수 >= 2:
            등급 = "B"
        elif kinds <= {"class≥0.9", "태그구조동일"}:
            등급 = "C"
        else:
            등급 = "보류"
        # Z 안에서도 성격이 갈린다.
        #   URL동일        호스트·등록도메인이 같다 — 볼 것도 없이 한 사이트
        #   실체동일(URL다름) 도메인은 다른데 canonical·기사 URL 이 상대를 가리킨다
        #                  (13264 크리스천21세기 cin21.net ↔ 7714 C뉴스 c-news.net —
        #                   로고만 바꾸고 같은 기사를 같은 배치로 낸다)
        z세부 = ("" if not z_ev else
                "URL동일" if any(e["종류"] in ("호스트동일", "등록도메인동일") for e in z_ev)
                else "실체동일(URL다름)")
        pairs.append({
            "A_id": x, "B_id": y, "등급": 등급, "Z세부": z세부,
            "점수": round(score, 1), "URL점수": round(z점수, 1),
            "증거종수": 식별종수, "블록J": round(bj, 2), "classJ": round(cj, 2),
            "증거": sorted(ev, key=lambda e: -e["점수"])[:12],
            "URL증거": sorted(z_ev, key=lambda e: -e["점수"])[:6],
            "A_제호": 쓸수있음[x].get("제호_등록", ""), "B_제호": 쓸수있음[y].get("제호_등록", ""),
            "A_호스트": hx.get("최종", ""), "B_호스트": hy.get("최종", ""),
        })
    print(f"채점 완료 · 기록 대상 쌍 {len(pairs):,}개 ({time.time() - t0:.0f}s)")
    print("  등급 분포:", Counter(p["등급"] for p in pairs).most_common())

    # ── 제호 지역 변형 계열 (전 모집단 대상 — 군집과 무관하게 먼저 만든다) ──
    stem_of = {i: strip_region(f.get("제호_등록", "")) for i, f in 쓸수있음.items()}
    fam = defaultdict(list)
    for i, (s, cut) in stem_of.items():
        if cut and len(s) >= 3:
            fam[s].append(i)
    families = {s for s, ids in fam.items() if len(ids) >= 2}
    계열원 = defaultdict(list)
    for i, (s, _cut) in stem_of.items():
        if s in families:
            계열원[s].append(i)
    제호계열군, id2fam = [], {}
    for n, (s, ids) in enumerate(sorted(계열원.items(), key=lambda kv: -len(kv[1])), 1):
        ids = sorted(ids, key=int)
        발행소명 = sorted({(쓸수있음[i].get("등록원부") or {}).get("발행소명", "") for i in ids} - {""})
        도메인 = sorted({쓸수있음[i]["호스트"].get("등록도메인", "") for i in ids} - {""})
        유형 = family_type(len(발행소명), len(도메인), len(ids))
        제호 = [쓸수있음[i].get("제호_등록", "") for i in ids]
        rec = {
            "계열ID": f"Y{n:04d}", "어간": s, "계열유형": 유형, "매체수": len(ids),
            "매체": ids, "제호": 제호,
            "지역토큰": [re.split(r"[(（]", t)[0].replace(s, "").strip() or "(어간)" for t in 제호][:40],
            "호스트": sorted({쓸수있음[i]["호스트"].get("최종", "") for i in ids} - {""}),
            "등록도메인": 도메인, "등록원부_발행소명": 발행소명,
            "군집ID": [], "Z군집ID": [],
        }
        제호계열군.append(rec)
        for i in ids:
            id2fam[i] = (s, 유형)

    def build(등급들, prefix):
        """등급 집합으로 union-find 군집을 만든다. Z 군집과 내용(A·B) 군집을 따로 만든다."""
        uf = UF()
        for p in pairs:
            if p["등급"] in 등급들:
                uf.union(p["A_id"], p["B_id"])
        members = defaultdict(set)
        for p in pairs:
            if p["등급"] in 등급들:
                members[uf.find(p["A_id"])] |= {p["A_id"], p["B_id"]}
        out = []
        for n, (root, mem) in enumerate(sorted(members.items(), key=lambda kv: -len(kv[1])), 1):
            mp = [p for p in pairs if p["등급"] in 등급들
                  and p["A_id"] in mem and p["B_id"] in mem]
            evc = Counter()
            for p in mp:
                for e in (p["URL증거"] if prefix == "Z" else p["증거"]):
                    evc[e["종류"]] += 1
            제호 = [쓸수있음[i].get("제호_등록", "") for i in sorted(mem, key=int)]
            fams = Counter(id2fam[i][0] for i in mem
                           if i in id2fam and id2fam[i][1].startswith("브랜드"))
            어간 = ""
            if fams:
                s_, n_ = fams.most_common(1)[0]
                if n_ >= max(2, 0.6 * len(mem)):
                    어간 = s_
            등급 = ("Z" if prefix == "Z"
                   else "A" if any(p["등급"] == "A" for p in mp) else "B")
            z세부 = Counter(p["Z세부"] for p in mp if p.get("Z세부"))
            out.append({
                "군집ID": f"{prefix}{n:04d}",
                "등급": 등급,
                "Z세부": ("URL동일" if z세부.get("URL동일") else
                        "실체동일(URL다름)" if z세부 else ""),
                "Z세부_쌍": dict(z세부),
                # 제호가 한 어간의 지역·분야 변형이면 '지역판(Y)' 으로 따로 세운다.
                # 세계타임즈(강원·경기·경남·경북…)처럼 화면은 같아도 지역별로 따로 운영하는
                # 브랜드 계열이 있다 — 한 사람이 여러 제호를 돌리는 것과 구분해야 한다.
                "유형": "Z" if prefix == "Z" else ("Y" if 어간 else 등급),
                "제호계열": 어간,
                "매체수": len(mem),
                "A쌍수": sum(1 for p in mp if p["등급"] == "A"),
                "B쌍수": sum(1 for p in mp if p["등급"] == "B"),
                "매체": sorted(mem, key=int),
                "제호": 제호,
                "호스트": sorted({쓸수있음[i]["호스트"].get("최종", "") for i in mem}),
                "등록원부_발행소명": sorted({(쓸수있음[i].get("등록원부") or {}).get("발행소명", "")
                                       for i in mem} - {""}),
                "핵심근거": [f"{k}×{v}" for k, v in evc.most_common(6)],
                "쌍수": len(mp),
                "최고점": max(((p["URL점수"] if prefix == "Z" else p["점수"]) for p in mp), default=0),
            })
        return out

    Z군집 = build({"Z"}, "Z")
    clusters = build({"A", "B"}, "G")

    # ── C: 템플릿군 (구조 해시로만 묶는다 — 쌍 비교 없이) ─────────────────
    tpl = defaultdict(list)
    for i, f in 쓸수있음.items():
        th = (f.get("구조") or {}).get("tag_seq_hash", "")
        if th:
            tpl[th].append(i)
    in_cluster = {i for c in clusters for i in c["매체"]}
    템플릿군 = []
    for n, (th, ids) in enumerate(sorted(tpl.items(), key=lambda kv: -len(kv[1])), 1):
        if len(ids) < 2:
            continue
        템플릿군.append({
            "템플릿ID": f"T{n:04d}", "해시": th, "매체수": len(ids),
            "매체": sorted(ids, key=int)[:200],
            "제호": [쓸수있음[i].get("제호_등록", "") for i in sorted(ids, key=int)][:200],
            "generator": sorted({(쓸수있음[i].get("구조") or {}).get("generator", "")
                                 for i in ids} - {""})[:3],
            "AB군집에도_속함": len([i for i in ids if i in in_cluster]),
        })

    벤더토큰 = [{"종류": k, "값": v, "매체수": n}
              for k, v, n in sorted(skipped, key=lambda x: -x[2])]
    벤더토큰 += [{"종류": k, "값": v, "매체수": d}
               for (k, v), d in sorted(df.items(), key=lambda kv: -kv[1])
               if d >= a.vendor_df and not k.startswith("_밴드_")][:200]

    # 계열마다 어느 군집에 걸쳐 있는지 채운다
    id2cluster = {i: c["군집ID"] for c in clusters for i in c["매체"]}
    id2z = {i: c["군집ID"] for c in Z군집 for i in c["매체"]}
    for rec in 제호계열군:
        rec["군집ID"] = sorted({id2cluster[i] for i in rec["매체"] if i in id2cluster})
        rec["Z군집ID"] = sorted({id2z[i] for i in rec["매체"] if i in id2z})

    out = {
        "생성": datetime.now().isoformat(timespec="seconds"),
        "설정": {"min_score": a.min_score, "a_score": a.a_score, "b_score": a.b_score,
                "pair_block_max": PAIR_BLOCK_MAX, "rare_line_df": RARE_LINE_DF,
                "가중표": {k: f"{v[0]}({v[1]})" for k, v in W.items()}},
        "매체수": len(fps), "군집대상": len(쓸수있음),
        "Z군집": Z군집, "군집": clusters, "제호계열": 제호계열군,
        "쌍": pairs, "템플릿군": 템플릿군, "벤더토큰": 벤더토큰[:300],
        "공용도메인": [{"도메인": k, **v} for k, v in sorted(공용도메인.items(), key=lambda x: -x[1]["매체수"])],
    }
    dest = Path(a.out) if a.out else work / "_clusters.json"
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    zurl = sum(1 for c in Z군집 if c["Z세부"] == "URL동일")
    print(f"\nZ 동일 사이트  {len(Z군집)}군집 / {sum(c['매체수'] for c in Z군집)}건 "
          f"(URL동일 {zurl} · 실체동일(URL다름) {len(Z군집) - zurl})")
    print(f"내용 군집      {len(clusters)}개 / {sum(c['매체수'] for c in clusters)}건 "
          f"— A {sum(1 for c in clusters if c['유형'] == 'A')} · "
          f"B {sum(1 for c in clusters if c['유형'] == 'B')} · "
          f"Y(지역판) {sum(1 for c in clusters if c['유형'] == 'Y')}")
    브랜드 = [c for c in 제호계열군 if c["계열유형"].startswith("브랜드")]
    print(f"제호 지역변형  {len(제호계열군)}계열 / {sum(c['매체수'] for c in 제호계열군)}건 "
          f"— 브랜드 지역판 {len(브랜드)}계열 / {sum(c['매체수'] for c in 브랜드)}건")
    for c in 브랜드[:6]:
        print(f"    {c['계열ID']} {c['어간']} {c['매체수']}건 · 발행소명 {len(c['등록원부_발행소명'])}종 "
              f"· 도메인 {c['등록도메인'][:2]}")
    print(f"템플릿군 {len(템플릿군)}개 (최대 {템플릿군[0]['매체수'] if 템플릿군 else 0}건)")
    print(f"→ {dest}  ({time.time() - t0:.0f}s)")
    for c in Z군집[:5]:
        print(f"  {c['군집ID']} [Z·{c['Z세부']}] {c['매체수']}건 {c['제호'][:3]} {c['호스트'][:3]}")
    for c in clusters[:8]:
        print(f"  {c['군집ID']} [{c['유형']}{'·' + c['제호계열'] if c['제호계열'] else ''}] "
              f"{c['매체수']}건 {c['제호'][:4]} … {c['핵심근거'][:3]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
