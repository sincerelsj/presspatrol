#!/usr/bin/env python3
"""inspect_run — 실행 산출물을 차원별로 점검한다.

설계 원칙 (CLAUDE.md)
---------------------
**점검도 스크립트가 Claude API 를 호출해서 한다.** 대화 에이전트나 Claude Code 서브에이전트로
점검하면 세 가지가 깨진다:
  1. 세션 한도에 걸려 중단된다 (2026-08-04 에 실제로 두 번 전멸했다)
  2. 8,000건에 그대로 적용할 수 없다
  3. 감사 추적이 산출물에 남지 않는다 — 행정 근거가 되지 못한다

파이프라인과 같은 패턴을 쓴다: **결정적 지표는 스크립트가 계산**하고, 그 결과를 함께 넣어
**판단이 필요한 부분만** LLM 에 묻는다. 방문 횟수·주석 오염·날짜 포맷·컬럼 공백은 계산이
정답이므로 LLM 에 묻지 않는다. 기사 선정 타당성·규칙기각 정당성·근거 위치 문맥은 판단이므로
LLM 에 묻는다.

사용
----
  python inspect_run.py --workdir ver_1/run_pilot4 --기준일 2026-08-04
  python inspect_run.py --workdir ver_1/run_pilot5 --기준일 2026-08-04 --dims capture,dates
  python inspect_run.py --workdir ver_1/run_pilot4 --기준일 2026-08-04 --no-llm   # 지표만 (무료)
  python inspect_run.py --workdir ver_1/run_pilot4 --기준일 2026-08-04 --dry-run  # 비용 추정

산출
----
  {workdir}/outputs/점검보고_{기준일}.md     사람이 읽는 보고서
  {workdir}/outputs/점검보고_{기준일}.json   지표 + 판정 원본
  {workdir}/audit/_inspect.json              점검 이력
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from llm_extract import MODEL, PRICE_IN, PRICE_OUT, cost_of, load_env_key  # noqa: E402
from md_core import audit  # noqa: E402
from md_core.fields import REQUIRED_FIELDS  # noqa: E402
from md_core.registry import COMPARE_FIELDS  # noqa: E402

MAX_TOKENS = 3000
SAMPLE = 8          # LLM 에 상세를 보내는 표본 매체 수 (전체 지표는 항상 전건 계산)

SYSTEM = """너는 인터넷신문 필요적 게재사항(신문법 제21조) 실태점검 파이프라인의 **점검자**다.
파이프라인이 낸 산출물을 받아 결함을 찾는다.

지켜야 할 것
- 주어진 지표와 표본만 근거로 삼아라. 없는 파일을 상상하지 마라.
- 지표가 정상이면 결함을 만들어내지 마라. defects 는 비어도 된다.
- 심각도를 부풀리지 마라. 전수 8,000건에서 실제로 판정을 틀리게 하는 것만 high 다.
- 설계 의도를 결함으로 보지 마라:
  * 접속 불가·도메인 파킹은 `홈페이지없음`(지침 I-4) 이다 — 수집 실패 ≠ 미게재.
    미준수(과태료 대상)로 집계하면 안 되고 준수율 분모에서도 빠진다
  * `홈페이지없음` 에는 지침 I-3(매체명 상이 + 뉴스 없음)도 포함된다 — 접속은 됐지만
    등록 매체의 사이트가 아니라는 판정이다
  * 지침 R7-2/R10-2 로 창간일·등록연월일을 발행연월일 근거로 쓴 것, R13-2/R15-2 로
    청소년보호책임자를 발행인으로 인정한 것은 **결함이 아니라 지침 그대로다**
  * 등록원부 대조 불일치는 제21조 위반이 아니다 (제9조 변경등록 사안, 별도 컬럼·시트로 분리)
  * `라벨근접=N` 은 결함이 아니라 사람 확인을 요청하는 플래그다
  * `확인필요=Y` 는 AI 초안이 확정이 아님을 뜻하는 정상 표시다
- 한국어로 답하라. 수치 없는 문장을 쓰지 마라."""

TOOL = {
    "name": "report_inspection",
    "description": "점검 결과를 구조화해 보고한다",
    "input_schema": {
        "type": "object",
        "properties": {
            "verdict": {
                "type": "string", "enum": ["ok", "warn", "fail"],
                "description": "이 차원의 종합 판정. ok=설계대로 동작 / warn=개선 필요 / fail=전수 전에 반드시 수정",
            },
            "summary": {
                "type": "string",
                "description": "결론 3~6문장. 반드시 수치를 포함한다.",
            },
            "defects": {
                "type": "array",
                "description": "발견한 결함. 없으면 빈 배열.",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "결함 제목 한 줄"},
                        "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                        "evidence": {"type": "string",
                                     "description": "근거. 매체 id·컬럼명·실제 값을 인용한다."},
                        "why_wrong": {"type": "string", "description": "무엇이 어긋나는가"},
                        "impact_full": {"type": "string",
                                        "description": "전수 8,000건에서 어떤 결과가 되는가"},
                        "fix": {"type": "string", "description": "수정 방향"},
                    },
                    "required": ["title", "severity", "evidence", "why_wrong"],
                    "additionalProperties": False,
                },
            },
            "ok_points": {
                "type": "array",
                "description": "설계 의도대로 확인된 것",
                "items": {"type": "string"},
            },
        },
        "required": ["verdict", "summary", "defects"],
        "additionalProperties": False,
    },
}


# ────────────────────────────── 공통 로더 ──────────────────────────────

def load_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def load_rows(W: Path, base: date) -> list[dict]:
    for name in (f"verify_{base.isoformat()}.csv", f"extract_{base.isoformat()}.csv"):
        p = W / "outputs" / name
        if p.exists():
            return list(csv.DictReader(p.open(encoding="utf-8-sig")))
    for pat in ("verify_*.csv", "extract_*.csv"):
        c = sorted((W / "outputs").glob(pat))
        if c:
            return list(csv.DictReader(c[-1].open(encoding="utf-8-sig")))
    return []


def work_ids(W: Path) -> list[str]:
    return sorted(p.stem for p in (W / "work").glob("*.json")
                  if not audit.is_sidecar(p.name))


def stage_detail(W: Path, rid: str, 단계: str) -> dict:
    for s in audit.load(W, rid).get("단계", []):
        if s.get("단계") == 단계:
            return s.get("상세") or {}
    return {}


def pct(a: int, b: int) -> str:
    return f"{a}/{b} ({a / b:.0%})" if b else f"{a}/0 (-)"


def run_mode(W: Path) -> str:
    """이 실행에서 **실제로 수행된 단계**를 감지해 프롬프트에 넣는다.

    왜 필요한가: `--skip-llm` 로 규칙만 돌린 산출물을 점검자에게 그냥 주면, 애초에 실행되지
    않은 단계(발행일 검증·LLM 검토)의 결과가 없는 것을 '전건 누락' 이라는 결함으로 보고한다
    (2026-08-05 pilot4 점검에서 실제로 fail/high 오탐 4건이 났다). 무엇을 안 돌렸는지
    알려주지 않으면 점검자는 안 돌린 것과 실패한 것을 구분할 수 없다.
    """
    ids = work_ids(W)
    n = len(ids)
    있음: Counter = Counter()
    for rid in ids:
        for s in audit.load(W, rid).get("단계", []):
            있음[str(s.get("단계"))] += 1
    파일 = {
        "5_LLM검토": sum(1 for r in ids if (W / "work" / f"{r}.llm.json").exists()),
        "5b_비전진단": sum(1 for r in ids if (W / "work" / f"{r}.diag.json").exists()),
        "5c_발행일검증": sum(1 for r in ids if (W / "work" / f"{r}.pubdate.json").exists()),
        "1b_실패해소": sum(1 for r in ids if (W / "work" / f"{r}.resolved.json").exists()),
    }
    수행, 미수행 = [], []
    for st in audit.STAGES:
        c = 있음.get(st, 0) or 파일.get(st, 0)
        (수행 if c else 미수행).append(f"{st}({c}건)" if c else st)
    llm단계 = [k for k, v in 파일.items() if v]
    모드 = ("규칙 전용(--skip-llm) — API 판단 단계를 돌리지 않았다"
            if not llm단계 else f"전 과정 — LLM 단계 {', '.join(llm단계)} 수행")
    return (
        f"[이 실행의 모드] {모드}\n"
        f"  대상 매체 {n}건\n"
        f"  수행된 단계: {', '.join(수행) or '(없음)'}\n"
        f"  **수행되지 않은 단계**: {', '.join(미수행) or '(없음)'}\n"
        f"  ※ 수행되지 않은 단계의 산출물이 비어 있는 것은 **결함이 아니다.** "
        f"그 차원은 '판정 보류' 로 다루고, 결함으로 보고하지 마라. "
        f"수행된 단계의 산출물만 결함 판단 대상이다.\n"
    )


# ────────────────────────────── 차원별 지표 ──────────────────────────────

def dim_capture(W: Path, base: date, rows: list[dict]) -> tuple[dict, str]:
    """방문 횟수·캡처 완비·주석 오염 — 전부 결정적."""
    ids = work_ids(W)
    n = len(ids)
    shots = W / "shots"
    방문합 = 0
    상세 = []
    for rid in ids:
        d = load_json(W / "work" / f"{rid}.json")
        req = len(d.get("fetch_시도") or []) or (1 if d.get("접속결과") else 0)
        cap = stage_detail(W, rid, "4_증빙캡처")
        pw = 1 if cap else 0
        pubd = load_json(W / "work" / f"{rid}.pubdate.json")
        재fetch = 0
        for k in ("메인본문_출처", "기사본문_출처"):
            if "직접 fetch" in (pubd.get(k) or ""):
                재fetch += 1
        방문 = req + pw + 재fetch
        방문합 += 방문
        상세.append({"id": rid, "requests": req, "playwright": pw, "재fetch": 재fetch,
                     "총방문": 방문, "캡처결과": cap.get("결과", "-"),
                     "기사확보": bool((cap.get("파일") or {}).get("기사HTML"))})
    rend = sorted(shots.glob("*_rendered.html"))
    art = sorted(shots.glob("*_article.html"))
    오염 = [p.name for p in rend
            if "__md_annot__" in p.read_text(encoding="utf-8", errors="replace")]
    오염_기사 = [p.name for p in art
                if "__md_annot__" in p.read_text(encoding="utf-8", errors="replace")]
    빈렌더 = [p.name for p in rend if p.stat().st_size < 500]

    m = {
        "대상_매체": n,
        "총_네트워크_방문": 방문합,
        "건당_방문": round(방문합 / n, 2) if n else 0,
        "8000건_환산_방문": int(방문합 / n * 8000) if n else 0,
        "직전_pilot3_건당_방문": 2.1,
        "렌더HTML_확보": pct(len(rend), n),
        "기사페이지_확보": pct(len(art), n),
        "주석_오염_렌더HTML": len(오염),
        "주석_오염_기사HTML": len(오염_기사),
        "빈_렌더HTML_500B미만": len(빈렌더),
        "캡처_결과분포": dict(Counter(x["캡처결과"] for x in 상세)),
        "하류_재fetch_발생": sum(x["재fetch"] for x in 상세),
    }
    실패 = [x for x in 상세 if x["캡처결과"] != "성공" or not x["기사확보"]]
    dig = (
        f"[캡처 실패·기사 미확보 매체 {len(실패)}건]\n"
        + "\n".join(f"  {x['id']} 캡처={x['캡처결과']} 기사확보={x['기사확보']} "
                    f"방문={x['총방문']}" for x in 실패[:20])
        + (f"\n\n[주석 오염 파일] {오염[:10]}" if 오염 else "\n\n[주석 오염] 없음")
        + (f"\n[빈 렌더HTML] {빈렌더[:10]}" if 빈렌더 else "")
    )
    q = ("캡처 단계 점검이다. 설계 목표는 (a) 사이트를 1회만 방문 (b) 렌더HTML 은 주석 주입 전에 "
         "저장해 오염 0 (c) 최신 기사 페이지를 같은 세션에서 확보 다. "
         "지표를 보고 목표 달성 여부를 판정하라. 캡처 실패 건은 접속 불가(정상적 홈페이지없음)와 "
         "캡처 로직 결함을 구분하라 — CSV 의 접속결과를 함께 보라.")
    return m, dig + "\n\n" + _접속요약(rows) + "\n\n" + q


def _접속요약(rows: list[dict]) -> str:
    c = Counter(r.get("접속결과") for r in rows)
    불가 = [r["언론사_id"] for r in rows if r.get("접속결과") not in ("가능", "", None)]
    return f"[CSV 접속결과] {dict(c)}\n  접속불가 id: {불가[:25]}"


def dim_dates(W: Path, base: date, rows: list[dict]) -> tuple[dict, str]:
    """날짜 포맷 통일 — 결정적."""
    COLS = ["등록연월일_게재값", "등록연월일_등록값", "발행연월일_게재값",
            "창간일_게재값", "발행연월일_실제"]

    def shape(v: str) -> str | None:
        v = (v or "").strip()
        if not v:
            return None
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            return "YYYY-MM-DD"
        if re.fullmatch(r"\d{4}-\d{2}", v):
            return "YYYY-MM(연월만)"
        if re.fullmatch(r"\d{4}", v):
            return "YYYY(연도만)"
        return f"비정규:{v[:24]}"

    m: dict = {}
    이상 = []
    for c in COLS:
        if not rows or c not in rows[0]:
            continue
        cnt = Counter(x for x in (shape(r.get(c)) for r in rows) if x)
        m[c] = dict(cnt)
        for r in rows:
            s = shape(r.get(c))
            if s and s.startswith("비정규"):
                이상.append(f"  {r['언론사_id']} {c} = {r.get(c)!r}")
    m["날짜_원표기_기록"] = pct(sum(1 for r in rows
                                if (r.get("날짜_원표기") or "").strip()), len(rows))
    m["비정규_포맷_건수"] = len(이상)
    dig = ("[비정규 포맷] 없음" if not 이상
           else "[비정규 포맷 값]\n" + "\n".join(이상[:25]))
    q = ("최종 산출물의 날짜 컬럼은 전부 YYYY-MM-DD 여야 한다(정렬·기간집계·8000건 통계). "
         "연월만·연도만 있는 값은 없는 '일' 을 만들지 않기로 한 설계이므로 그 자체는 결함이 아니다. "
         "비정규 포맷이 남아 있으면 어떤 표기를 정규화가 놓쳤는지 지적하라.")
    return m, dig + "\n\n" + q


def dim_evidence(W: Path, base: date, rows: list[dict]) -> tuple[dict, str]:
    """근거 DOM 위치 — 지표는 결정적, 문맥 타당성은 LLM 판단."""
    ids = work_ids(W)
    evs = {rid: load_json(W / "evidence" / f"{rid}.json") for rid in ids}
    있음 = {k: v for k, v in evs.items() if v}
    tot = 찾음 = 근접 = css = 0
    zone: Counter = Counter()
    의심 = []
    미검출 = []
    for rid, d in 있음.items():
        for f, v in (d.get("항목") or {}).items():
            tot += 1
            if not v.get("찾음"):
                미검출.append(f"  {rid} {f} = {(v.get('값') or '')[:24]!r} — {(v.get('사유') or '')[:60]}")
                continue
            찾음 += 1
            zone[v.get("영역") or "?"] += 1
            if (v.get("css") or "").strip():
                css += 1
            if v.get("라벨근접") == "Y":
                근접 += 1
            else:
                의심.append({"id": rid, "항목": f, "값": (v.get("값") or "")[:26],
                            "css": (v.get("css") or "")[:70],
                            "문맥": (v.get("주변문맥") or "")[:120]})
    m = {
        "evidence_파일": pct(len(있음), len(ids)),
        "항목_총계": tot,
        "위치확정": pct(찾음, tot),
        "css셀렉터_보유": pct(css, max(찾음, 1)),
        "라벨근접_Y": pct(근접, max(찾음, 1)),
        "화면영역": dict(zone),
        "위치_미확정": len(미검출),
    }
    dig = ("[라벨 미근접 항목 — 같은 값이 다른 문맥에서 잡혔을 수 있다]\n"
           + "\n".join(f"  {x['id']} {x['항목']} = {x['값']}\n"
                       f"    css: {x['css']}\n    문맥: {x['문맥']}"
                       for x in 의심[:SAMPLE * 2])
           + ("\n\n[위치 미확정]\n" + "\n".join(미검출[:15]) if 미검출 else ""))
    q = ("근거 위치 기록 점검이다. 목적은 (a) 다음 회차에 이 셀렉터만 보고 재확인 (b) 행정 근거 "
         "(c) 사이트 개편 감지 다. **핵심 판단**: 위 '라벨 미근접' 항목들의 `문맥` 을 읽고, "
         "그 문맥이 해당 항목의 게재 위치로 타당한지 판정하라. 예를 들어 발행인 값이 "
         "'청소년보호책임자 : 홍길동' 문맥에서 잡혔다면 값은 맞아도 위치가 틀린 것이므로 "
         "그 셀렉터는 재사용할 수 없다. 그런 사례를 매체 id 와 함께 지적하라. "
         "화면영역이 '중간' 인 건이 많으면 무엇을 뜻하는지도 해석하라 — 신문법은 상·하단 게재를 요구한다.")
    return m, dig + "\n\n" + q


def dim_pubdate(W: Path, base: date, rows: list[dict]) -> tuple[dict, str]:
    """발행연월일 — 근거 존재는 결정적 검증, 기사 선정 타당성은 LLM 판단."""
    ids = work_ids(W)
    pds = {rid: load_json(W / "work" / f"{rid}.pubdate.json") for rid in ids}
    있음 = {k: v for k, v in pds.items() if v}
    경로: Counter = Counter()
    출처: Counter = Counter()
    근거없음 = []
    기준일같음 = []
    경과_180 = []
    표본 = []
    for rid, p in 있음.items():
        경로[p.get("확인경로") or "-"] += 1
        출처[p.get("기사본문_출처") or "-"] += 1
        근거 = (p.get("게재_근거") or "").strip()
        # 근거가 저장본에 실제로 있는가 — 환각 차단 (결정적 검증)
        if 근거:
            hay = ""
            for fn in (f"{rid}_rendered.html", f"{rid}_article.html"):
                fp = W / "shots" / fn
                if fp.exists():
                    hay += fp.read_text(encoding="utf-8", errors="replace")
            key = re.sub(r"\s+", "", 근거)[:20]
            if key and key not in re.sub(r"\s+", "", hay):
                근거없음.append(f"  {rid} 게재_근거={근거[:60]!r} — 저장본에 없음")
        if (p.get("발행연월일_게재") or "") == base.isoformat():
            기준일같음.append(rid)
        try:
            g = int(p.get("경과일") or 0)
        except (TypeError, ValueError):
            g = 0
        if g >= 180:
            경과_180.append(f"{rid}({g}일)")
        cap = stage_detail(W, rid, "4_증빙캡처")
        표본.append({"id": rid, "게재": p.get("발행연월일_게재") or "-",
                    "실제": p.get("발행연월일_실제") or "-", "경과일": g,
                    "확인경로": p.get("확인경로") or "-",
                    "기사제목": (cap.get("기사제목_링크") or "")[:60],
                    "기사선정근거": cap.get("기사선정_근거") or "-"})
    m = {
        "pubdate_파일": pct(len(있음), len(ids)),
        "확인경로_분포": dict(경로),
        "기사본문_출처": dict(출처),
        "근거가_저장본에_없음": len(근거없음),
        "게재값이_기준일과_동일": len(기준일같음),
        "경과180일_이상": len(경과_180),
    }
    dig = (
        (f"[근거 미확인 — 환각 의심]\n" + "\n".join(근거없음[:12]) if 근거없음
         else "[근거 검증] 전건 저장본에서 확인됨")
        + f"\n\n[게재값이 기준일({base})과 동일한 매체] {기준일같음[:20]}"
        + (f"\n[180일 경과] {경과_180[:20]}" if 경과_180 else "")
        + "\n\n[표본 — 캡처가 고른 기사]\n"
        + "\n".join(f"  {x['id']} 게재={x['게재']} 실제={x['실제']} 경과={x['경과일']}일 "
                    f"경로={x['확인경로']}\n    기사: {x['기사제목']}\n    선정: {x['기사선정근거']}"
                    for x in 표본[:SAMPLE])
    )
    q = ("발행연월일 = 최신 기사 업데이트 날짜다. 점검할 것: "
         "(1) 캡처가 고른 기사가 '가장 지배적인 최신 기사' 로 타당한가 — 기사 제목과 선정 근거"
         "(글자 크기·굵기·위치·URL 패턴 점수)를 보고 판단하라. 공지·이벤트·고정 배너를 기사로 "
         "골랐으면 결함이다. "
         "(2) 게재값이 기준일과 같은 건 — 사이트가 오늘 날짜를 실제로 표기했을 수도 있고, "
         "잘못 읽었을 수도 있다. 표본의 근거를 보고 판단하라. "
         "(3) 180일 경과 건은 운영 중단 의심이며 제21조 게재 위반이 아니다 — 그렇게 다뤄졌는가.")
    return m, dig + "\n\n" + q


def dim_compare(W: Path, base: date, rows: list[dict]) -> tuple[dict, str]:
    """등록원부 대조 — 지표는 결정적, 오탐 판단은 LLM."""
    m: dict = {}
    표본 = []
    for f in COMPARE_FIELDS:
        col = f"{f}_대조"
        if not rows or col not in rows[0]:
            continue
        m[f] = dict(Counter(r.get(col) or "-" for r in rows))
    m["등록정보_상이_Y"] = pct(sum(1 for r in rows if r.get("등록정보_상이") == "Y"), len(rows))
    섞임 = [r["언론사_id"] for r in rows if "[대조]" in (r.get("확인필요_사유") or "")]
    m["확인필요_사유에_대조가_섞인_건"] = len(섞임)
    for r in rows:
        불 = [f for f in COMPARE_FIELDS
              if (r.get(f"{f}_대조") or "") in ("불일치", "부분일치")]
        if 불:
            표본.append({"id": r["언론사_id"], "제호": r.get("제호_등록", ""),
                        "항목": [{"f": f, "게재": (r.get(f"{f}_게재값") or "")[:40],
                                "등록": (r.get(f"{f}_등록값") or "")[:40],
                                "대조": r.get(f"{f}_대조")} for f in 불]})
    dig = "[불일치·부분일치 표본]\n" + "\n".join(
        f"  {x['id']} {x['제호']}\n" + "\n".join(
            f"    {i['f']}({i['대조']})  게재={i['게재']!r}  등록={i['등록']!r}"
            for i in x["항목"]) for x in 표본[:SAMPLE])
    q = ("등록원부 대조 점검이다. 대조 불일치는 제21조 게재의무 위반이 **아니다** — 사이트 표기가 "
         "정확한데 등록원부가 낡은 것이므로 제9조 변경등록 사안이고, 그래서 확인필요가 아니라 "
         "등록정보_상이 컬럼과 별도 시트로 분리했다. "
         "**핵심 판단**: 위 표본에서 **같은 값인데 표기 차이로 불일치가 난 오탐**을 찾아라. "
         "예: 등록번호 '아00073' vs '세종 아00073'(시도 접두 유무), 주소의 우편번호·층수 표기, "
         "이름의 직함. 오탐이면 정규화 규칙을 어떻게 고칠지 제시하라. "
         "입력 xlsx 에 해당 컬럼이 없어 '등록값없음' 인 것은 정상이다.")
    return m, dig + "\n\n" + q


def dim_rule_llm(W: Path, base: date, rows: list[dict]) -> tuple[dict, str]:
    """규칙 ↔ LLM 판정 차이 — 각 변경의 타당성은 LLM 판단."""
    ids = work_ids(W)
    변경 = []
    for rid in ids:
        d = load_json(W / "work" / f"{rid}.json")
        l = load_json(W / "work" / f"{rid}.llm.json")
        if not l:
            continue
        r_f = d.get("findings") or {}
        l_f = l.get("검증후_findings") or {}
        diffs = []
        for f in REQUIRED_FIELDS:
            a = ((r_f.get(f) or {}).get("값") or "").strip()
            b = ((l_f.get(f) or {}).get("값") or "").strip()
            if a != b:
                diffs.append({"항목": f, "규칙": a or "(없음)", "LLM": b or "(없음)",
                             "근거": ((l_f.get(f) or {}).get("근거") or "")[:110]})
        if diffs:
            변경.append({"id": rid, "제호": d.get("제호_등록", ""), "차이": diffs,
                        "수리이력": len(l.get("근거수리") or l.get("repair") or [])})
    동결깨짐 = [r["언론사_id"] for r in rows
              if (r.get("준수여부_규칙") or "") and (r.get("준수여부_LLM") or "")
              and r.get("필수8_게재수_규칙") == r.get("필수8_게재수_LLM")
              and (r.get("규칙_LLM_차이") or "").strip()]
    m = {
        "llm_검토_건수": sum(1 for rid in ids if (W / "work" / f"{rid}.llm.json").exists()),
        "값이_달라진_건": len(변경),
        "총_변경_항목수": sum(len(x["차이"]) for x in 변경),
        "준수여부_규칙_분포": dict(Counter(r.get("준수여부_규칙") or "-" for r in rows)),
        "준수여부_LLM_분포": dict(Counter(r.get("준수여부_LLM") or "-" for r in rows)),
        "동결_의심(게재수는_같은데_차이표기_있음)": len(동결깨짐),
    }
    dig = "[규칙 → LLM 값 변경]\n" + "\n".join(
        f"  {x['id']} {x['제호']} (근거수리 {x['수리이력']}회)\n" + "\n".join(
            f"    {c['항목']}: {c['규칙']!r} → {c['LLM']!r}\n      근거: {c['근거']}"
            for c in x["차이"]) for x in 변경[:SAMPLE])
    q = ("규칙 1차결과를 LLM 에 넣고 받은 최종 판정이 옳은 방향인지 점검한다. "
         "**핵심 판단**: 위 각 변경이 타당한가? "
         "(a) LLM 이 규칙을 기각한 건(LLM 이 '(없음)') — 규칙이 근거 없이 낸 값을 버린 것이면 옳다. "
         "근거를 보고 판정하라. "
         "(b) LLM 이 값을 추가한 건 — 근거 문구가 그 값을 실제로 뒷받침하는가? "
         "(c) 값이 바뀐 건 — 표기 차이인가, 다른 값으로 바뀐 것인가? 다른 값이면 어느 쪽이 옳은가. "
         "행정 처분 근거로 쓸 수 있는 수준의 근거가 남았는지도 판정하라.")
    return m, dig + "\n\n" + q


def dim_flags(W: Path, base: date, rows: list[dict]) -> tuple[dict, str]:
    """플래그·컬럼 정합성 — 지표는 결정적, 사유 타당성은 LLM."""
    n = len(rows)
    빈컬럼 = [c for c in (rows[0] if rows else {})
            if all(not (r.get(c) or "").strip() for r in rows)]
    사유: Counter = Counter()
    for r in rows:
        for s in (r.get("확인필요_사유") or "").split(" / "):
            s = s.strip()
            if s:
                사유[re.sub(r"\s*\d+\s*항목", "", s.split(":")[0].split("—")[0]).strip()[:44]] += 1
    미등록단계 = []
    for rid in work_ids(W):
        for s in audit.load(W, rid).get("단계", []):
            if str(s.get("단계", "")).startswith("미등록:"):
                미등록단계.append(f"{rid}:{s['단계']}")
    증빙 = sum(1 for r in rows if (r.get("증빙_경로") or "").strip())
    shots존재 = sum(1 for r in rows
                  if (W / "shots" / f"{r['언론사_id']}_footer.png").exists())
    m = {
        "대상": n,
        "확인필요_Y": pct(sum(1 for r in rows if r.get("확인필요") == "Y"), n),
        "준수여부_LLM": dict(Counter(r.get("준수여부_LLM") or "-" for r in rows)),
        "확인필요_사유_분포": dict(사유.most_common()),
        "증빙_경로_채워진_건": pct(증빙, n),
        "실제_스크린샷_존재": pct(shots존재, n),
        "전건_빈_컬럼": 빈컬럼,
        "audit_미등록_단계": 미등록단계[:10],
    }
    표본 = [{"id": r["언론사_id"], "제호": r.get("제호_등록", ""),
            "판정": r.get("준수여부_LLM"), "게재수": r.get("필수8_게재수_LLM"),
            "미게재": r.get("미게재_항목_LLM") or r.get("미게재_항목"),
            "사유": (r.get("확인필요_사유") or "")[:300]}
           for r in rows if r.get("확인필요") == "Y"]
    dig = ("[확인필요 표본]\n" + "\n".join(
        f"  {x['id']} {x['제호']} 판정={x['판정']} 게재={x['게재수']} 미게재={x['미게재']}\n"
        f"    사유: {x['사유']}" for x in 표본[:SAMPLE])
        + f"\n\n[전건 빈 컬럼] {빈컬럼}")
    q = ("검수자가 이 산출물로 일할 수 있는지 점검한다. "
         "(1) **증빙_경로**: 스크린샷은 실제로 존재하는데 컬럼이 비어 있으면 검수자가 증빙을 "
         "찾을 수 없다 — 행정 처분 근거 확보 실패다. 지표를 보고 판정하라. "
         "(2) **전건 빈 컬럼**: 채워져야 하는데 빈 컬럼을 골라내라. 해당 입력에 원래 없는 정보"
         "(예: 입력 xlsx 에 등록연월일 컬럼이 없음)나 그 단계를 안 돌린 경우는 정상이다. "
         "(3) **확인필요 사유**: 표본의 사유가 검수자에게 '무엇을 확인해야 하는지' 를 알려주는가? "
         "모호하거나 중복된 사유를 지적하라. "
         "(4) audit 미등록 단계가 있으면 STAGES 등록 누락이다.")
    return m, dig + "\n\n" + q


DIMENSIONS = {
    "capture": ("캡처·방문 횟수·주석 오염", dim_capture),
    "dates": ("날짜 포맷 통일", dim_dates),
    "evidence": ("근거 DOM 위치 기록", dim_evidence),
    "pubdate": ("발행연월일 확정 근거", dim_pubdate),
    "compare": ("등록원부 대조", dim_compare),
    "rule_llm": ("규칙 ↔ LLM 판정 차이", dim_rule_llm),
    "flags": ("플래그·컬럼 정합성", dim_flags),
}


# ────────────────────────────── API 호출 ──────────────────────────────

def ask(client, 차원명: str, metrics: dict, digest: str, base: date,
        mode: str) -> tuple[dict, dict]:
    user = (
        f"[점검 차원] {차원명}\n"
        f"[기준일] {base.isoformat()}\n\n"
        f"{mode}\n"
        f"[스크립트가 계산한 지표 — 이 수치가 정답이다]\n"
        f"{json.dumps(metrics, ensure_ascii=False, indent=2)}\n\n"
        f"[상세·표본]\n{digest}\n"
    )
    resp = client.messages.create(
        model=MODEL, max_tokens=MAX_TOKENS,
        system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
        tools=[TOOL], tool_choice={"type": "tool", "name": TOOL["name"]},
        messages=[{"role": "user", "content": user}],
    )
    out = {}
    for b in resp.content:
        if getattr(b, "type", "") == "tool_use":
            out = b.input
            break
    # 드물게 모델이 tool_use 대신 텍스트로 답하거나 요약에 태그를 흘린다(실측 1/7).
    # verdict 가 없으면 그 응답은 쓰지 않는다 — 호출자가 재시도한다.
    if not out.get("verdict"):
        out.setdefault("_불완전", True)
    u = resp.usage
    # cost_of 는 Anthropic usage 의 키 이름을 그대로 받는다(캐시 읽기·쓰기 단가가 다르다)
    raw = {"input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
           "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
           "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0}
    usage = {"in": raw["input_tokens"], "out": raw["output_tokens"],
             "cache_read": raw["cache_read_input_tokens"],
             "cache_write": raw["cache_creation_input_tokens"],
             "cost": cost_of(raw)}
    return out, usage


def write_report(W: Path, base: date, 결과: list[dict], 비용: float) -> Path:
    L = [f"# 실행 점검 보고 — {W.name}", "",
         f"- 기준일: {base.isoformat()}",
         f"- 점검 시각: {datetime.now().isoformat(timespec='seconds')}",
         f"- 모델: {MODEL} (API 호출 {sum(1 for r in 결과 if r.get('판정'))}회, ${비용:.4f})",
         "", "> 결정적 지표는 스크립트가 계산했다. 판단이 필요한 부분만 API 에 물었다.", ""]
    등급 = {"fail": "🔴 전수 전 수정 필요", "warn": "🟡 개선 필요", "ok": "🟢 정상"}
    L.append("## 차원별 판정")
    L.append("")
    L.append("| 차원 | 판정 | 결함 |")
    L.append("|---|---|---|")
    for r in 결과:
        j = (r.get("판정") or {})
        L.append(f"| {r['차원명']} | {등급.get(j.get('verdict'), '- (미실행)')} | "
                 f"{len(j.get('defects') or [])} |")
    L.append("")
    확정 = []
    for r in 결과:
        j = r.get("판정") or {}
        L.append(f"## {r['차원명']}")
        L.append("")
        L.append("```json")
        L.append(json.dumps(r["지표"], ensure_ascii=False, indent=2))
        L.append("```")
        L.append("")
        if not j:
            L.append("_판정 미실행 (--no-llm 또는 호출 실패)_")
            L.append("")
            continue
        L.append(j.get("summary", ""))
        L.append("")
        for d in (j.get("defects") or []):
            확정.append({**d, "차원": r["차원명"]})
            L.append(f"### [{d.get('severity', '?').upper()}] {d.get('title', '')}")
            L.append("")
            L.append(f"- **근거**: {d.get('evidence', '')}")
            L.append(f"- **왜 잘못인가**: {d.get('why_wrong', '')}")
            if d.get("impact_full"):
                L.append(f"- **전수 영향**: {d['impact_full']}")
            if d.get("fix"):
                L.append(f"- **수정 방향**: {d['fix']}")
            L.append("")
        for o in (j.get("ok_points") or []):
            L.append(f"- ✅ {o}")
        L.append("")
    순 = {"high": 0, "medium": 1, "low": 2}
    확정.sort(key=lambda d: 순.get(d.get("severity"), 9))
    L.insert(6, "")
    L.insert(6, " / ".join(f"{k} {v}" for k, v in
                           Counter(d.get("severity") for d in 확정).items()) or "결함 없음")
    L.insert(6, f"**확정 결함 {len(확정)}건** — ")
    p = W / "outputs" / f"점검보고_{base.isoformat()}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(L), encoding="utf-8")
    return p


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--base-date", "--기준일", dest="base_date", default="")
    ap.add_argument("--dims", default="", help="쉼표로 구분. 기본 전체: " + ",".join(DIMENSIONS))
    ap.add_argument("--no-llm", action="store_true", help="지표만 계산 (API 호출 0)")
    ap.add_argument("--dry-run", action="store_true", help="비용 추정만")
    args = ap.parse_args()

    base = (datetime.strptime(args.base_date, "%Y-%m-%d").date()
            if args.base_date else date.today())
    W = Path(args.workdir)
    rows = load_rows(W, base)
    if not rows:
        print(json.dumps({"error": f"{W}/outputs 에 verify/extract CSV 가 없다"},
                         ensure_ascii=False)); sys.exit(1)

    want = [d.strip() for d in args.dims.split(",") if d.strip()] or list(DIMENSIONS)
    나쁜 = [d for d in want if d not in DIMENSIONS]
    if 나쁜:
        print(json.dumps({"error": f"모르는 차원 {나쁜}", "가능": list(DIMENSIONS)},
                         ensure_ascii=False)); sys.exit(1)

    mode = run_mode(W)
    print(mode, file=sys.stderr)

    결과, usages = [], []
    client = None
    if not args.no_llm and not args.dry_run:
        import anthropic
        load_env_key()
        client = anthropic.Anthropic()

    for i, key in enumerate(want, 1):
        차원명, fn = DIMENSIONS[key]
        try:
            metrics, digest = fn(W, base, rows)
        except Exception as exc:  # noqa: BLE001
            print(f"[{i}/{len(want)}] {key} 지표 계산 실패: "
                  f"{exc.__class__.__name__}: {exc}", file=sys.stderr)
            결과.append({"차원": key, "차원명": 차원명,
                       "지표": {"오류": f"{exc.__class__.__name__}: {exc}"}, "판정": {}})
            continue
        rec = {"차원": key, "차원명": 차원명, "지표": metrics, "판정": {}}
        if args.dry_run:
            rec["예상_입력자수"] = len(digest) + len(json.dumps(metrics, ensure_ascii=False))
            print(f"[{i}/{len(want)}] {key} 지표 OK (입력 약 {rec['예상_입력자수']}자)",
                  file=sys.stderr)
        elif client is not None:
            판정: dict = {}
            for 시도 in (1, 2):
                try:
                    판정, u = ask(client, 차원명, metrics, digest, base, mode)
                    usages.append(u)
                    if 판정.get("verdict"):
                        rec["판정"], rec["usage"] = 판정, u
                        print(f"[{i}/{len(want)}] {key} → {판정['verdict']} "
                              f"(결함 {len(판정.get('defects') or [])}, ${u['cost']:.4f})",
                              file=sys.stderr)
                        break
                    print(f"[{i}/{len(want)}] {key} 응답 불완전 — 재시도 {시도}",
                          file=sys.stderr)
                except Exception as exc:  # noqa: BLE001
                    rec["판정_실패"] = f"{exc.__class__.__name__}: {str(exc)[:200]}"
                    print(f"[{i}/{len(want)}] {key} 판정 실패: {rec['판정_실패']}",
                          file=sys.stderr)
                    break
            else:
                rec["판정_실패"] = "verdict 없는 응답 2회 — 판정 보류"
        else:
            print(f"[{i}/{len(want)}] {key} 지표만 계산 (--no-llm)", file=sys.stderr)
        결과.append(rec)

    비용 = sum(u["cost"] for u in usages)
    js = W / "outputs" / f"점검보고_{base.isoformat()}.json"
    js.parent.mkdir(parents=True, exist_ok=True)
    js.write_text(json.dumps({"workdir": str(W), "기준일": base.isoformat(),
                              "모델": MODEL, "비용_USD": round(비용, 5),
                              "차원": 결과}, ensure_ascii=False, indent=2),
                  encoding="utf-8")
    md = write_report(W, base, 결과, 비용)
    audit.record(W, "_inspect", "6_대조", "성공",
                 {"점검차원": want, "비용_USD": round(비용, 5), "보고서": md.name})

    확정: list[dict] = []
    for r in 결과:
        for d in ((r.get("판정") or {}).get("defects") or []):
            확정.append({"차원": r["차원명"], "심각도": d.get("severity"),
                        "제목": d.get("title")})
    print(json.dumps({
        "보고서_md": str(md), "보고서_json": str(js),
        "모델": MODEL, "API_호출": len(usages), "총비용_USD": round(비용, 5),
        "차원별_판정": {r["차원"]: (r.get("판정") or {}).get("verdict") or "-" for r in 결과},
        "확정_결함": 확정,
        "심각도_분포": dict(Counter(d["심각도"] for d in 확정)),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
