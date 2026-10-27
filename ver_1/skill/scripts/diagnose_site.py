#!/usr/bin/env python3
"""diagnose_site — Tier 2. 스크린샷 + DOM 을 보내 규칙 실패 원인을 진단하고 override 를 만든다.

언제 쓰나
---------
게이트B(`md_core/gate.evaluate_after_review`)가 `진단필요` 로 판정한 건에만 쓴다.
텍스트 검토(Tier 1)로 결론이 난 건에 이미지를 보내는 것은 낭비다 — 파일럿 실측에서
미검출 4항목이던 사이트가 텍스트만으로 3항목을 해결했다.

이미지가 실제로 값을 더하는 경우는 정해져 있다:
  - 제호가 **로고 이미지** 로만 표기됨 (텍스트에 없어서 title 태그로 폴백된 건)
  - 하단 블록을 규칙이 잘못 잡아 영역 분리가 실패한 건 (DOM 을 보고 selector 를 정정)
  - JS 렌더링으로 requests 가 못 본 내용

산출
----
  W/overrides/{도메인}.json   footer/header selector + 항목별 정정값 (다음 실행이 자동 적용)
  W/work/{id}.diag.json       진단 원문 + usage/비용
  W/audit/{id}.json           5b_비전진단 단계

사용
----
  python diagnose_site.py --workdir W --ids 30876              # 특정 건
  python diagnose_site.py --workdir W --from-gate              # 게이트B가 진단필요로 본 건 전부
  python diagnose_site.py --workdir W --ids 30876 --dry-run    # 토큰·비용만 추정
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from md_core import audit, gate  # noqa: E402
from md_core.fields import FIELDS  # noqa: E402
from md_core.http import normalize_url  # noqa: E402
from llm_extract import (FIELD_KEY, MODEL, PRICE_IN, PRICE_OUT,  # noqa: E402
                         cost_of, load_env_key)

TOOL_NAME = "record_diagnosis"
MAX_TOKENS = 4000
DOM_BLOCK_CHARS = 1600
DOM_MAX_BLOCKS = 3


SYSTEM_PROMPT = """\
너는 한국 인터넷신문의 필요적 게재사항(신문법 제21조) 점검에서 **정규식 추출기가 실패한
원인을 진단하는 역할**이다. 텍스트만으로는 해결되지 않아 화면 이미지와 DOM 까지 받았다.

받는 자료
- [하단 텍스트] / [상단 텍스트] — 규칙이 본 것
- [규칙 결과] — 규칙이 뽑은 값과 미해결 항목
- [하단 DOM 후보] — 하단으로 추정된 블록들의 HTML
- 스크린샷 — 실제 렌더링된 화면 (하단, 필요시 상단)

## 할 일 세 가지

### 1. 원인 진단 (`cause`)
왜 규칙이 실패했는지 한두 문장으로 특정해라. 예:
- "제호가 로고 이미지(img)로만 표기돼 텍스트에 없다"
- "하단 정보가 #footer 가 아니라 .company_info 에 있어 영역 분리가 빗나갔다"
- "게재사항이 실제로 없다 — 규칙 실패가 아니라 매체 미게재다"

★ **매체가 정말 게재하지 않은 경우도 정당한 진단이다.** 없는 것을 억지로 찾지 마라.

### 2. selector 정정 (`footer_selector` / `header_selector`)
[하단 DOM 후보] 를 보고, 게재사항이 실제로 담긴 블록의 CSS selector 를 골라라.
- 이 selector 는 다음 실행에서 `soup.select(selector)` 로 그대로 쓰인다
- 지금 규칙이 잡은 영역이 이미 맞으면 빈 문자열로 둬라 (불필요한 override 는 위험하다)
- 존재를 확신할 수 없는 selector 를 추측해서 쓰지 마라 — DOM 에서 본 것만

### 3. 이미지 판독 (`image_only_fields`, `corrections`)
스크린샷에서 **텍스트에는 없지만 화면에는 보이는** 값을 읽어라. 로고 이미지의 제호가
대표적이다. 이미지에서 읽은 항목은 `image_only_fields` 에 넣고, `corrections` 의
`source` 를 `이미지판독` 으로 표시해라.

## corrections 규칙
- `source`: `이미지판독`(스크린샷에서 읽음) / `DOM`(HTML 속성·구조에서) / `텍스트`(텍스트 재확인)
- `evidence`: **`텍스트` 인 경우에만 원문을 그대로 복사**한다(프로그램이 부분문자열 검증한다).
  `이미지판독`·`DOM` 은 무엇을 보고 읽었는지 서술해라 — 예 "하단 좌측 로고 이미지의 글자"
- 해결하지 못한 항목은 값을 비우고 `source` 를 `미해결` 로 둬라
- 미해결 항목만 채워라. 규칙이 이미 맞게 잡은 항목은 건드리지 마라(값 비움 + `source=규칙유지`)

## 주의
- 신문법은 **메인화면 상·하단** 게재를 요구한다. 회사소개 같은 별도 페이지는 게재가 아니다.
- 이미지로만 표기된 항목은 실태점검에서 별도 판단이 필요하다 — 값을 읽더라도 반드시
  `image_only_fields` 에 넣어 사람이 알 수 있게 해라.
- 애매하면 매체에 유리하게 판단하고 `confidence` 를 낮춰라.

반드시 record_diagnosis 도구를 호출해 제출해라."""


def build_tool() -> dict:
    corr = {
        FIELD_KEY[f]: {
            "type": "object",
            "description": f"항목: {f}",
            "properties": {
                "value": {"type": "string", "description": "정정값. 해결 못했거나 규칙 유지면 빈 문자열"},
                "evidence": {"type": "string",
                             "description": "source=텍스트 면 원문 그대로 복사. 이미지판독/DOM 이면 무엇을 봤는지 서술"},
                "location": {"type": "string", "enum": ["하단", "상단", "기타", "없음"]},
                "confidence": {"type": "string", "enum": ["high", "mid", "low"]},
                "source": {"type": "string",
                           "enum": ["이미지판독", "DOM", "텍스트", "규칙유지", "미해결"]},
            },
            "required": ["value", "evidence", "location", "confidence", "source"],
            "additionalProperties": False,
        } for f in FIELDS
    }
    # strict: True 를 쓰지 않는다 — 10항목 × 5속성 × enum 조합이
    # "The compiled grammar is too large" 400 을 유발한다. tool_choice 강제 + 방어적 파싱
    # + verify_corrections() 로 대체한다.
    return {
        "name": TOOL_NAME,
        "description": "규칙 실패 원인 진단과 정정 사항을 제출한다. 키의 한글 항목명: "
                       + ", ".join(f"{v}={k}" for k, v in FIELD_KEY.items()),
        "input_schema": {
            "type": "object",
            "properties": {
                "cause": {"type": "string", "description": "규칙이 실패한 원인 진단 (한두 문장)"},
                "footer_selector": {"type": "string",
                                    "description": "하단 CSS selector. DOM 에서 확인한 것만. 정정 불필요면 빈 문자열"},
                "header_selector": {"type": "string", "description": "상단 CSS selector. 불필요면 빈 문자열"},
                "image_only_fields": {"type": "array",
                                      "items": {"type": "string",
                                                "enum": [FIELD_KEY[f] for f in FIELDS]},
                                      "description": "이미지로만 표기된 항목"},
                "corrections": {"type": "object", "properties": corr,
                                "required": [FIELD_KEY[f] for f in FIELDS],
                                "additionalProperties": False},
                "notes": {"type": "string"},
            },
            "required": ["cause", "footer_selector", "header_selector",
                         "image_only_fields", "corrections", "notes"],
            "additionalProperties": False,
        },
    }


def dom_candidates(html: str) -> list[tuple[str, str]]:
    """하단 후보 블록들의 (selector 힌트, outerHTML 발췌)."""
    from md_core.zones import FOOTER_HINT, soupify
    soup = soupify(html)
    out: list[tuple[str, str]] = []
    for el in soup.find_all(True):
        name = el.name or ""
        idv = str(el.get("id") or "")
        cls = " ".join(el.get("class") or [])
        attrs = f"{idv} {cls}"
        if name == "footer" or FOOTER_HINT.search(attrs):
            sel = name
            if idv:
                sel = f"#{idv}"
            elif cls:
                sel = "." + ".".join(cls.split()[:2])
            out.append((sel, str(el)[:DOM_BLOCK_CHARS]))
        if len(out) >= DOM_MAX_BLOCKS * 3:
            break
    # 중첩 제거 대신 가장 텍스트가 많은 블록 우선
    out.sort(key=lambda t: -len(t[1]))
    return out[:DOM_MAX_BLOCKS]


def img_block(path: Path) -> dict | None:
    if not path.exists():
        return None
    return {"type": "image",
            "source": {"type": "base64", "media_type": "image/png",
                       "data": base64.standard_b64encode(path.read_bytes()).decode()}}


def rendered_text(shots: Path, rid: str) -> str:
    """렌더 HTML 의 평문 — 근거 검증용 건초더미로 쓴다."""
    p = shots / f"{rid}_rendered.html"
    if not p.exists():
        return ""
    from md_core.zones import extract_zones
    z = extract_zones(p.read_text(encoding="utf-8", errors="replace"))
    return z.get("전체") or ""


def build_content(detail: dict, shots: Path, rid: str, 미해결: list[str],
                   사유: list[str]) -> tuple[list, dict]:
    """user content 블록 리스트 + 첨부 메타."""
    html = ""
    for cand in (shots / f"{rid}_rendered.html",):
        if cand.exists():
            html = cand.read_text(encoding="utf-8", errors="replace")
    doms = dom_candidates(html) if html else []

    규칙표 = "\n".join(
        f"  {f}: {(detail.get('findings', {}).get(f) or {}).get('값') or '(미검출)'}"
        f"  [{(detail.get('findings', {}).get(f) or {}).get('위치', '')}/"
        f"{(detail.get('findings', {}).get(f) or {}).get('확신도', '')}]"
        for f in FIELDS)

    text = (
        f"언론사_id: {rid}\n등록된 제호: {detail.get('제호_등록') or '(없음)'}\n"
        f"URL: {detail.get('url', '')}\ntitle 태그: {(detail.get('title') or '')[:80]}\n"
        f"\n[게이트 판정] 진단이 필요한 이유\n  " + "\n  ".join(사유) +
        f"\n\n[미해결 항목] {', '.join(미해결) or '(없음)'}\n"
        f"\n[하단 텍스트]\n{(detail.get('하단_텍스트') or '(없음)')[:2500]}\n"
        f"\n[상단 텍스트]\n{(detail.get('상단_텍스트') or '(없음)')[:800]}\n"
        f"\n[규칙 결과]\n{규칙표}\n"
    )
    if doms:
        text += "\n[하단 DOM 후보]\n" + "\n\n".join(
            f"--- selector 힌트: {sel} ---\n{h}" for sel, h in doms)
    else:
        text += "\n[하단 DOM 후보] 렌더 HTML 없음 — capture_shots.py 를 먼저 돌려라\n"

    content: list = [{"type": "text", "text": text}]
    첨부 = {"하단이미지": False, "상단이미지": False, "DOM블록": len(doms)}

    foot = img_block(shots / f"{rid}_footer.png")
    if foot:
        content.append({"type": "text", "text": "\n[하단 스크린샷]"})
        content.append(foot)
        첨부["하단이미지"] = True
    # 제호 관련이 미해결이면 상단도 보낸다 (로고가 상단에 있다)
    if any(f in 미해결 for f in ("제호", "제호 영문")):
        top = img_block(shots / f"{rid}_top.png")
        if top:
            content.append({"type": "text", "text": "\n[상단 스크린샷]"})
            content.append(top)
            첨부["상단이미지"] = True
    return content, 첨부


def verify_corrections(raw: dict, detail: dict, *, dom_text: str = "") -> tuple[dict, list[str]]:
    """source=텍스트 인 항목만 근거 verbatim 검증. 이미지판독은 검증 대상이 아니다.

    ★ 건초더미에 **렌더 HTML 텍스트도 넣어야 한다.** 모델에게 DOM 발췌를 보여줬으므로
    DOM 에서 인용한 근거는 정당하다. work JSON 의 텍스트만으로 검증하면, 본문 0자인
    사이트(진단이 가장 필요한 사이트)의 정당한 근거가 전부 폐기된다 — 실제로 30876 에서
    6항목이 그렇게 버려졌다.
    """
    hay = []
    for k in ("하단_텍스트", "상단_텍스트", "title"):
        v = detail.get(k) or ""
        if v:
            hay += [v, " ".join(v.split())]
    if dom_text:
        hay += [dom_text, " ".join(dom_text.split())]
    폐기: list[str] = []
    out: dict = {}
    corr = raw.get("corrections") or {}
    for f in FIELDS:
        item = corr.get(FIELD_KEY[f]) or {}
        val = str(item.get("value") or "").strip()
        src = str(item.get("source") or "미해결")
        ev = str(item.get("evidence") or "").strip()
        if not val:
            continue
        if src == "텍스트":
            evn = " ".join(ev.split())
            if not (ev and any(ev in h or evn in h for h in hay)):
                폐기.append(f)
                continue
        out[f] = {"값": val, "근거": ev[:180],
                  "위치": str(item.get("location") or "하단"),
                  "확신도": str(item.get("confidence") or "low"),
                  "source": src}
    return out, 폐기


def domain_of(url: str) -> str:
    host = urlparse(normalize_url(url or "")).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def targets(workdir: Path, ids: set[str] | None, from_gate: bool) -> list[tuple[dict, dict]]:
    """→ [(work detail, 게이트 판정), ...]"""
    out = []
    for p in sorted((workdir / "work").glob("*.json")):
        if audit.is_sidecar(p.name):
            continue
        d = json.loads(p.read_text(encoding="utf-8"))
        rid = str(d.get("언론사_id") or p.stem)
        if ids and rid not in ids:
            continue
        lp = workdir / "work" / f"{rid}.llm.json"
        if lp.exists():
            l = json.loads(lp.read_text(encoding="utf-8"))
            g = gate.evaluate_after_review(l.get("검증후_findings") or {},
                                           폐기항목=l.get("근거검증_폐기항목") or [],
                                           js_의심=bool(d.get("js_의심")),
                                           notes=l.get("notes", ""))
        else:
            g = gate.evaluate(d.get("findings") or {}, 접속결과=d.get("접속결과", "가능"),
                              js_의심=bool(d.get("js_의심")),
                              텍스트길이=d.get("텍스트_길이"))
        if from_gate and g["등급"] != gate.진단필요:
            continue
        out.append((d, g))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--ids", default="")
    ap.add_argument("--from-gate", action="store_true",
                    help="게이트가 진단필요로 판정한 건만")
    ap.add_argument("--base-date", "--기준일", dest="base_date", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    base = (datetime.strptime(args.base_date, "%Y-%m-%d").date()
            if args.base_date else date.today())
    workdir = Path(args.workdir)
    shots = workdir / "shots"
    ids = {s.strip() for s in args.ids.split(",") if s.strip()} or None
    tg = targets(workdir, ids, args.from_gate)
    if args.limit:
        tg = tg[: args.limit]
    if not tg:
        print(json.dumps({"error": "대상 없음 — --ids 또는 --from-gate 확인"},
                         ensure_ascii=False)); sys.exit(1)

    if args.dry_run:
        est = []
        for d, g in tg:
            rid = str(d["언론사_id"])
            c, 첨부 = build_content(d, shots, rid, g["미해결항목"], g["사유"])
            txt = sum(len(b.get("text", "")) for b in c if b.get("type") == "text")
            imgs = sum(1 for b in c if b.get("type") == "image")
            # 이미지 토큰 ≈ (w*h)/750, 1440x900 → 약 1728
            est.append({"id": rid, "텍스트문자": txt, "이미지수": imgs,
                        "추정입력토큰": int(txt / 1.7) + imgs * 1728, **첨부})
        tot = sum(e["추정입력토큰"] for e in est) + len(est) * 1200  # system
        out_est = len(est) * 1100
        print(json.dumps({"모드": "dry-run", "모델": MODEL, "대상": len(est), "건별": est,
                          "추정입력토큰": tot, "추정출력토큰": out_est,
                          "추정비용_USD": round(tot/1e6*PRICE_IN + out_est/1e6*PRICE_OUT, 4),
                          }, ensure_ascii=False, indent=2)); return

    load_env_key()
    import anthropic
    client = anthropic.Anthropic()
    tool = build_tool()

    결과, usages = [], []
    for i, (d, g) in enumerate(tg, 1):
        rid = str(d["언론사_id"])
        content, 첨부 = build_content(d, shots, rid, g["미해결항목"], g["사유"])
        try:
            resp = client.messages.create(
                model=MODEL, max_tokens=MAX_TOKENS,
                system=[{"type": "text", "text": SYSTEM_PROMPT,
                         "cache_control": {"type": "ephemeral"}}],
                tools=[tool], tool_choice={"type": "tool", "name": TOOL_NAME},
                messages=[{"role": "user", "content": content}],
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[{i}/{len(tg)}] {rid} 실패: {exc.__class__.__name__}: {exc}", file=sys.stderr)
            audit.record(workdir, rid, "5b_비전진단", "실패",
                         {"예외": f"{exc.__class__.__name__}: {exc}"[:300], "첨부": 첨부},
                         제호=d.get("제호_등록"), 기준일=base.isoformat())
            continue

        raw = next((b.input for b in resp.content
                    if getattr(b, "type", "") == "tool_use" and b.name == TOOL_NAME), {})
        u = resp.usage
        usage = {"input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
                 "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
                 "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
                 "stop_reason": resp.stop_reason}
        usages.append(usage)
        정정, 폐기 = verify_corrections(raw, d, dom_text=rendered_text(shots, rid))
        이미지항목 = [k for k in (raw.get("image_only_fields") or [])]
        이미지항목_한글 = [f for f in FIELDS if FIELD_KEY[f] in 이미지항목]

        # --- override 작성 ---
        dom = domain_of(d.get("url"))
        ov_path = workdir / "overrides" / f"{dom}.json" if dom else None
        ov_written = False
        if ov_path:
            prev = {}
            if ov_path.exists():
                try:
                    prev = json.loads(ov_path.read_text(encoding="utf-8"))
                except Exception:  # noqa: BLE001
                    prev = {}
            새 = dict(prev)
            새["domain"] = dom
            if raw.get("footer_selector"):
                새["footer_selector"] = raw["footer_selector"]
            if raw.get("header_selector"):
                새["header_selector"] = raw["header_selector"]
            if 정정:
                fields = dict(새.get("fields") or {})
                for f, v in 정정.items():
                    fields[f] = {"값": v["값"], "근거": v["근거"], "위치": v["위치"]}
                새["fields"] = fields
            메모 = f"비전진단({base.isoformat()}): {raw.get('cause','')[:140]}"
            if 이미지항목_한글:
                메모 += f" / 이미지 표기: {', '.join(이미지항목_한글)}"
            새["notes"] = ((prev.get("notes", "") + " / ") if prev.get("notes") else "") + 메모
            새["diagnosed_by"] = MODEL
            새["diagnosed_at"] = datetime.now().isoformat(timespec="seconds")
            ov_path.parent.mkdir(parents=True, exist_ok=True)
            ov_path.write_text(json.dumps(새, ensure_ascii=False, indent=2), encoding="utf-8")
            ov_written = True

        (workdir / "work" / f"{rid}.diag.json").write_text(json.dumps({
            "언론사_id": rid, "제호_등록": d.get("제호_등록", ""), "모델": MODEL,
            "기준일": base.isoformat(), "게이트판정": g, "첨부": 첨부,
            "usage": usage, "비용_USD": round(cost_of(usage), 5),
            "원응답": raw, "검증후_정정": 정정, "근거검증_폐기": 폐기,
            "이미지표기_항목": 이미지항목_한글, "override_경로": str(ov_path) if ov_written else "",
        }, ensure_ascii=False, indent=2), encoding="utf-8")

        audit.record(workdir, rid, "5b_비전진단", "성공", {
            "모델": MODEL, "첨부": 첨부, "게이트사유": g["사유"],
            "원인진단": raw.get("cause", ""),
            "footer_selector": raw.get("footer_selector", ""),
            "header_selector": raw.get("header_selector", ""),
            "이미지표기_항목": 이미지항목_한글,
            "정정항목": {f: {"값": v["값"], "source": v["source"], "근거": v["근거"]}
                     for f, v in 정정.items()},
            "근거검증_폐기": 폐기, "override_생성": ov_written,
            "usage": usage, "비용_USD": round(cost_of(usage), 5),
            "notes": raw.get("notes", ""),
        }, 제호=d.get("제호_등록"), 기준일=base.isoformat())

        결과.append({"언론사_id": rid, "제호": d.get("제호_등록", ""),
                   "원인": raw.get("cause", "")[:120],
                   "footer_selector": raw.get("footer_selector", ""),
                   "정정항목": list(정정), "이미지표기": 이미지항목_한글,
                   "폐기": 폐기, "비용": round(cost_of(usage), 5)})
        print(f"[{i}/{len(tg)}] {rid} {d.get('제호_등록','')} "
              f"정정 {len(정정)}항목 이미지표기 {len(이미지항목_한글)} "
              f"${round(cost_of(usage),4)} — {raw.get('cause','')[:60]}", file=sys.stderr)

    총 = sum(cost_of(u) for u in usages)
    print(json.dumps({
        "모델": MODEL, "대상": len(결과), "기준일": base.isoformat(),
        "총비용_USD": round(총, 5),
        "건당_평균_USD": round(총 / max(1, len(결과)), 5),
        "결과": 결과,
        "다음": "run_extract.py --ids <id> 로 재실행하면 생성된 override 가 적용된다",
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
