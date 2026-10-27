#!/usr/bin/env python3
"""orchestrate — 게이트 기반 루프로 전 과정을 관리한다.

흐름
----
    라운드 0  run_extract            규칙 추출 (전건)
              게이트A                통과 → 종료 / 검토필요 → R1 / 진단필요 → R2
    라운드 1  llm_extract            텍스트 + 규칙결과 검토 (Tier 1)
              게이트B                통과 → 종료 / 진단필요 → R2
    라운드 2  capture_shots          스크린샷 + 렌더 HTML (없으면)
              diagnose_site          이미지 + DOM 진단 → override 생성 (Tier 2)
              run_extract --ids      override 적용 재수집
              게이트A 재평가          통과 → 종료 / 실패 → 확인필요 확정
    마무리    compare_registry → finalize → location_map

`--max-rounds` 기본 2 — 인계 문서 원칙("한 사이트에 매몰되지 마라. 2회 시도 후 안 되면
확인필요로 두고 다음으로")을 코드로 강제한다.

사용
----
  python orchestrate.py --workdir W --input in.xlsx --기준일 2026-07-31
  python orchestrate.py --workdir W --input in.xlsx --no-vision      # Tier2 끔(비용 절약)
  python orchestrate.py --workdir W --input in.xlsx --skip-llm       # 규칙만 (API 호출 0)
  python orchestrate.py --workdir W --input in.xlsx --dry-run        # 계획만 출력
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from md_core import audit, gate  # noqa: E402


def run(cmd: list[str], *, label: str) -> tuple[int, str]:
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=env)
    if p.returncode != 0:
        print(f"  ! {label} 실패 (exit {p.returncode})", file=sys.stderr)
        print((p.stderr or p.stdout or "")[-600:], file=sys.stderr)
    return p.returncode, (p.stdout or "")


def tail_json(out: str) -> dict:
    """스크립트가 마지막에 출력하는 요약 JSON 을 뽑는다."""
    s = out.rfind("{")
    while s != -1:
        try:
            return json.loads(out[s:])
        except Exception:  # noqa: BLE001
            s = out.rfind("{", 0, s)
    return {}


def load_work(workdir: Path) -> dict[str, dict]:
    out = {}
    for p in sorted((workdir / "work").glob("*.json")):
        if audit.is_sidecar(p.name):
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        out[str(d.get("언론사_id") or p.stem)] = d
    return out


def failed_ids(workdir: Path, base_date: str) -> list[str]:
    """접속 실패·도메인 파킹 건의 id — extract CSV 에서 읽는다.

    work/{id}.json 에는 `접속결과` 가 없다(`status` 만 있다). 판정 문자열('가능'/'불가'/
    '내용없음'/'URL없음')은 run_extract 가 CSV 에 쓸 때 만든다. 그래서 여기서 CSV 를 본다.
    """
    import csv as _csv
    for name in (f"extract_{base_date}.csv", f"verify_{base_date}.csv"):
        p = workdir / "outputs" / name
        if not p.exists():
            continue
        out = []
        with p.open(encoding="utf-8-sig") as f:
            for r in _csv.DictReader(f):
                if (r.get("접속결과") or "가능") != "가능" or (r.get("파킹의심") or "") == "Y":
                    out.append(str(r.get("언론사_id") or ""))
        return [x for x in out if x]
    return []


def gateA(d: dict, 통과기준: str, 기준일=None) -> dict:
    return gate.evaluate(d.get("findings") or {}, 접속결과=d.get("접속결과", "가능"),
                         js_의심=bool(d.get("js_의심")), 텍스트길이=d.get("텍스트_길이"),
                         override_적용=bool(d.get("override")), 통과기준=통과기준,
                         기준일=기준일)


def gateB(workdir: Path, rid: str, d: dict) -> dict | None:
    p = workdir / "work" / f"{rid}.llm.json"
    if not p.exists():
        return None
    l = json.loads(p.read_text(encoding="utf-8"))
    return gate.evaluate_after_review(l.get("검증후_findings") or {},
                                      폐기항목=l.get("근거검증_폐기항목") or [],
                                      js_의심=bool(d.get("js_의심")),
                                      notes=l.get("notes", ""))


def 기록(workdir: Path, rid: str, 라운드: str, g: dict, d: dict) -> None:
    audit.record(workdir, rid, "3b_게이트", g["등급"],
                 {"라운드": 라운드, "등급": g["등급"], "사유": g["사유"],
                  "미해결항목": g["미해결항목"], "이미지필요": g["이미지필요"]},
                 제호=d.get("제호_등록"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--input", required=True)
    ap.add_argument("--sheet")
    ap.add_argument("--map", dest="map_json", default="")
    ap.add_argument("--base-date", "--기준일", dest="base_date", default="")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--op-col", "--운영컬럼", dest="op_col", default="")
    ap.add_argument("--op-val", "--운영값", dest="op_val", default="운영")
    ap.add_argument("--max-rounds", type=int, default=2)
    ap.add_argument("--shots", default="flagged",
                    choices=["flagged", "all", "diagnose", "none"],
                    help="증빙 스크린샷 범위. flagged=확인필요 건(기본) / all=전건 / "
                         "diagnose=비전진단 대상만 / none=안 찍음")
    ap.add_argument("--skip-llm", action="store_true", help="Tier1·2 모두 끔 (API 호출 0)")
    ap.add_argument("--skip-capture", action="store_true",
                    help="R0b 캡처를 건너뛰고 이미 저장된 shots/ 를 쓴다. "
                         "규칙만 먼저 돌린 작업폴더에 판단 단계를 이어붙일 때 쓴다 — "
                         "사이트를 두 번 방문하지 않는다(설계 원칙)")
    ap.add_argument("--skip-resolve", action="store_true",
                    help="접속 불가 건의 제호 검색(R1b)을 하지 않는다")
    ap.add_argument("--no-vision", action="store_true", help="Tier2(이미지) 끔")
    ap.add_argument("--pass-level", "--통과기준", dest="pass_level", default=gate.기본_통과기준,
                    choices=[gate.통과기준_엄격, gate.통과기준_중간, gate.통과기준_느슨])
    ap.add_argument("--batch", action="store_true",
                    help="LLM 단계를 Message Batches API 로 돌린다 (토큰 50%% 할인). "
                         "배치는 단계별로 제출→수집을 순차 수행한다 — 게이트가 배치 사이에 "
                         "있어 한 배치에 두 단계를 섞을 수 없다. "
                         "R2 비전진단(이미지 256MB 한도)·R1b 제호검색(web_search 미검증)은 제외")
    ap.add_argument("--phase", default="all", choices=["1", "2", "3", "all"],
                    help="1=수집·캡처·규칙(LLM 없음) / 2=LLM 판단·산출 / "
                         "3=웹검색(제호검색)만 / all=전부. "
                         "웹검색은 설계상 **맨 마지막**이다 — 새 URL 이 나오면 "
                         "그 건들만 다시 phase 1→2 를 돌린다")
    ap.add_argument("--capture-shards", type=int, default=0,
                    help="R0b 캡처를 N개 프로세스로 병렬 실행 (0=단일). "
                         "도메인 해시로 나눠 같은 호스트는 한 샤드에만 간다")
    ap.add_argument("--capture-retry-shards", type=int, default=2,
                    help="캡처 품질 불량 건 재시도 동시성")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    P1 = args.phase in ("1", "all")
    P2 = args.phase in ("2", "all")
    P3 = args.phase in ("3", "all")

    base = (datetime.strptime(args.base_date, "%Y-%m-%d").date()
            if args.base_date else date.today())
    D = base.isoformat()
    W = str(Path(args.workdir))
    PY = sys.executable
    공통 = ["--workdir", W, "--input", args.input, "--기준일", D]
    if args.sheet:
        공통 += ["--sheet", args.sheet]
    if args.map_json:
        공통 += ["--map", args.map_json]

    이력: list[dict] = []

    if args.dry_run:
        생략 = lambda c, f: f"  [{f} 으로 생략]" if c else ""  # noqa: E731
        print(json.dumps({
            "계획": [
                "R0   run_extract  requests 수집 + 규칙 추출 → 게이트A            무료",
                "R0b  capture_shots  단일 방문: 스크린샷·렌더HTML·기사페이지         무료"
                + 생략(args.skip_capture, "--skip-capture")
                + 생략(args.shots == "none", "--shots none"),
                "R0c  run_extract --ids  렌더HTML 반영 재추출 → 게이트A 재평가       무료",
                "R1b  resolve_failed  접속불가 제호 검색(web_search)                API"
                + 생략(args.skip_llm or args.skip_resolve,
                     "--skip-resolve" if args.skip_resolve else "--skip-llm"),
                "R1   llm_extract  텍스트 검토 → 게이트B                          API"
                + 생략(args.skip_llm, "--skip-llm"),
                "R2   diagnose_site  비전 진단 (진단필요 건만, 방문 없음)            API"
                + 생략(args.no_vision or args.skip_llm,
                     "--no-vision" if args.no_vision else "--skip-llm"),
                "R3   verify_pubdate  발행연월일 확정 (저장본만 읽음)                API"
                + 생략(args.skip_llm, "--skip-llm"),
                "R4   capture_shots --annotate-only  주석본 + evidence 위치        무료"
                + 생략(args.shots == "none", "--shots none"),
                "     merge_llm → compare_registry → finalize → location_map      무료",
                "점검  inspect_run  7차원 결과 점검 (지표는 계산, 판단만 API)         API"
                + 생략(args.skip_llm, "--skip-llm"),
            ],
            "기준일": D, "통과기준": args.pass_level, "max_rounds": args.max_rounds,
            "스크린샷_범위": args.shots,
            "원칙": "사이트는 R0b 에서 한 번만 본다. 하류는 shots/ 의 저장본만 읽는다.",
        }, ensure_ascii=False, indent=2)); return

    # ---------------- 라운드 0: 규칙 추출 ----------------
    print("== R0 규칙 추출 ==", file=sys.stderr)
    cmd = [PY, str(HERE / "run_extract.py"), *공통, "--workers", str(args.workers)]
    if args.limit:
        cmd += ["--limit", str(args.limit)]
    if args.op_col:
        cmd += ["--운영컬럼", args.op_col, "--운영값", args.op_val]
    rc, out = run(cmd, label="run_extract")
    if rc != 0:
        sys.exit(rc)
    r0 = tail_json(out)

    work = load_work(Path(W))
    등급 = {}
    for rid, d in work.items():
        g = gateA(d, args.pass_level, base)
        등급[rid] = g
        기록(Path(W), rid, "R0", g, d)
    분포0 = {}
    for g in 등급.values():
        분포0[g["등급"]] = 분포0.get(g["등급"], 0) + 1
    이력.append({"라운드": "R0 규칙추출", "게이트A_분포": 분포0,
                "대상": r0.get("대상_사이트"), "준수": r0.get("8항목_전부게재")})
    print(f"   게이트A: {분포0}", file=sys.stderr)

    검토대상 = [r for r, g in 등급.items() if g["등급"] == gate.검토필요]
    진단대상 = [r for r, g in 등급.items() if g["등급"] == gate.진단필요]

    # ---------------- 라운드 0b: 증빙 캡처 (판단 **앞**) ----------------
    #
    # 왜 판단 앞인가 — 캡처 한 번에 판단에 필요한 모든 것이 나온다:
    #   · 원본 스크린샷 (상단·하단)      → 증빙, 비전 진단 입력
    #   · 렌더 HTML                      → JS 사이트 재추출(R0c), 발행일 검증 입력
    #   · 최신 기사 페이지 HTML·스크린샷  → 발행연월일 확정 근거
    # 예전에는 캡처가 R1 뒤였다. 그래서 (1) R1 이 렌더 HTML 없이 판단했고,
    # (2) R2 가 캡처를 위해, R3 가 기사 페이지를 위해 같은 매체를 또 방문했다
    # (pilot3 실측 건당 2.1회 = 8천건 16,615회).
    # 주석본은 LLM 최종값을 반영해야 하므로 R3 뒤에 저장본으로 따로 만든다(R4).
    if args.skip_capture:
        보유 = len(list((Path(W) / "shots").glob("*_rendered.html")))
        기사 = len(list((Path(W) / "shots").glob("*_article.html")))
        print(f"== R0b 캡처 생략 (--skip-capture) — 저장본 재사용: "
              f"렌더HTML {보유}건, 기사페이지 {기사}건 ==", file=sys.stderr)
        이력.append({"라운드": "R0b 증빙캡처", "생략": "--skip-capture",
                    "재사용_렌더HTML": 보유, "재사용_기사페이지": 기사})
    elif args.shots != "none":
        cmd = [PY, str(HERE / "capture_shots.py"), *공통, "--emit-override"]
        if args.op_col:
            cmd += ["--운영컬럼", args.op_col, "--운영값", args.op_val]
        라벨 = args.shots
        if args.shots == "flagged":
            cmd += ["--only-flagged"]
        elif args.shots == "diagnose":
            # 판단 전이라 '진단필요' 는 게이트A 결과다. 비어 있으면 건너뛴다.
            if not 진단대상:
                라벨 = None
            else:
                cmd += ["--ids", ",".join(진단대상)]
        if 라벨:
            print(f"== R0b 증빙 캡처 (범위={라벨}) — 스크린샷·렌더HTML·기사페이지 ==",
                  file=sys.stderr)
            if args.capture_shards > 1:
                # 병렬 + 품질 재시도. Playwright 동기 API 는 스레드 안전하지 않아
                # **프로세스**로 쪼갠다. 도메인 해시 분할이라 같은 호스트는 한 샤드에만 간다.
                pcmd = [PY, str(HERE / "run_capture_parallel.py"), *공통,
                        "--emit-override",
                        "--shards", str(args.capture_shards),
                        "--retry-shards", str(args.capture_retry_shards)]
                if args.shots == "flagged":
                    pcmd += ["--only-flagged"]
                rc, out = run(pcmd, label="capture(parallel)")
            else:
                rc, out = run(cmd, label="capture_shots")
            cs = tail_json(out)
            이력.append({"라운드": "R0b 증빙캡처", "범위": 라벨, "비용_USD": 0,
                        "대상": cs.get("대상"), "성공": cs.get("성공"),
                        "기사페이지_확보": cs.get("기사페이지_확보"),
                        "렌더링_개선_id": cs.get("렌더링_개선_id"),
                        "override_생성": cs.get("override_생성")})
            print(f"   캡처 {cs.get('성공')}/{cs.get('대상')}, "
                  f"기사페이지 {cs.get('기사페이지_확보','-')}건", file=sys.stderr)

            # R0c 렌더링으로 본문이 살아난 건은 재수집해 반영한다.
            # 여기서 해소되면 LLM 을 안 쓴다 — 무료 단계에서 먼저 고치는 게 싸다.
            개선 = cs.get("렌더링_개선_id") or []
            if 개선:
                print(f"== R0c 렌더HTML 반영 재추출 {len(개선)}건 ==", file=sys.stderr)
                run([PY, str(HERE / "run_extract.py"), *공통,
                     "--ids", ",".join(개선)], label="run_extract(렌더적용)")
                work = load_work(Path(W))
                for rid in 개선:
                    g = gateA(work.get(rid, {}), args.pass_level, base)
                    등급[rid] = g
                    기록(Path(W), rid, "R0c", g, work.get(rid, {}))
                검토대상 = [r for r, g in 등급.items() if g["등급"] == gate.검토필요]
                진단대상 = [r for r, g in 등급.items() if g["등급"] == gate.진단필요]
                분포0c = {}
                for g in 등급.values():
                    분포0c[g["등급"]] = 분포0c.get(g["등급"], 0) + 1
                이력.append({"라운드": "R0c 렌더적용", "대상": len(개선),
                            "게이트A_분포": 분포0c})
                print(f"   게이트A 재평가: {분포0c} "
                      f"(검토 {len(검토대상)} / 진단 {len(진단대상)})", file=sys.stderr)

    # ---------------- 라운드 1b: 접속 불가 건 제호 검색 (ISS-011) ----------------
    # 접속 불가 = 홈페이지없음(지침 I-4) 이다. 지침은 여기서 포털검색을 지시한다.
    # 도메인이 바뀐 것인지 폐업인지 가려야
    # 검수자가 판단할 수 있다. API 의 web_search 서버 도구로 제호를 검색하고 등록번호로
    # 동일 매체를 확인한다 — 에이전트가 대신 검색하면 8,000건에 적용할 수 없다.
    # ★ 접속 실패는 **CSV** 에서 읽는다. work/{id}.json 에는 `접속결과` 키가 없고 `status`
    #   만 있다 — 처음엔 work 에서 읽어 조건이 항상 거짓이 되어 R1b 가 한 번도 실행되지
    #   않았다(pilot4 실측, 로그에 R1b 가 아예 안 찍혔다).
    #   대상 선정은 resolve_failed 가 CSV 를 보고 직접 한다(파킹 건까지 포함). 여기서는
    #   돌릴 가치가 있는지만 센다 — --ids 로 목록을 넘기면 선정 규칙이 두 곳으로 갈라진다.
    접속불가 = failed_ids(Path(W), D)
    if P3 and 접속불가 and not args.skip_llm and not args.skip_resolve:
        print(f"== R1b 접속 불가·파킹 제호 검색 {len(접속불가)}건 ==", file=sys.stderr)
        rc, out = run([PY, str(HERE / "resolve_failed.py"), "--workdir", W,
                       "--기준일", D, "--input", args.input], label="resolve_failed")
        rb = tail_json(out)
        이력.append({"라운드": "R1b 실패해소", "대상": len(접속불가),
                    "비용_USD": rb.get("총비용_USD"), "판정_분포": rb.get("판정_분포")})
        print(f"   검색 판정: {rb.get('판정_분포')}", file=sys.stderr)
        # 검색 결과를 CSV·플래그에 반영한다
        run([PY, str(HERE / "run_extract.py"), *공통,
             "--ids", ",".join(접속불가)], label="run_extract(검색반영)")
        work = load_work(Path(W))
    elif 접속불가:
        이력.append({"라운드": "R1b 실패해소",
                    "생략": "--skip-resolve" if args.skip_resolve else "--skip-llm",
                    "미처리_접속불가": len(접속불가)})
    else:
        이력.append({"라운드": "R1b 실패해소", "대상": 0, "비고": "접속 실패·파킹 건 없음"})

    # ---------------- 라운드 1: 텍스트 검토 (Tier 1) ----------------
    if P2 and 검토대상 and not args.skip_llm:
        print(f"== R1 텍스트 검토 {len(검토대상)}건 ==", file=sys.stderr)
        if args.batch:
            rc, _ = run([PY, str(HERE / "llm_extract.py"), "--workdir", W, "--기준일", D,
                         "--batch-submit", "--ids", ",".join(검토대상)],
                        label="llm_extract(batch-submit)")
            print("   배치 제출 완료 — 결과 대기(최대 24h)", file=sys.stderr)
            rc, out = run([PY, str(HERE / "llm_extract.py"), "--workdir", W, "--기준일", D,
                           "--batch-collect", "--ids", ",".join(검토대상)],
                          label="llm_extract(batch-collect)")
        else:
            rc, out = run([PY, str(HERE / "llm_extract.py"), "--workdir", W,
                           "--기준일", D, "--sync", "--ids", ",".join(검토대상)],
                          label="llm_extract")
        r1 = tail_json(out)
        추가진단 = []
        for rid in 검토대상:
            gb = gateB(Path(W), rid, work.get(rid, {}))
            if gb is None:
                continue
            등급[rid] = gb
            기록(Path(W), rid, "R1", gb, work.get(rid, {}))
            if gb["등급"] == gate.진단필요:
                추가진단.append(rid)
        진단대상 = sorted(set(진단대상) | set(추가진단))
        이력.append({"라운드": "R1 텍스트검토", "대상": len(검토대상),
                    "비용_USD": r1.get("총비용_USD"), "판단_분포": r1.get("판단_분포"),
                    "게이트B_진단필요": len(추가진단)})
        print(f"   게이트B → 진단필요 {len(추가진단)}건", file=sys.stderr)
    elif args.skip_llm:
        이력.append({"라운드": "R1 텍스트검토", "생략": "--skip-llm"})

    # ---------------- 라운드 2: 비전 진단 (Tier 2) ----------------
    if P2 and 진단대상 and not args.skip_llm and not args.no_vision and args.max_rounds >= 2:
        print(f"== R2 렌더링·비전 진단 {len(진단대상)}건 ==", file=sys.stderr)
        shots = Path(W) / "shots"

        # R2a 렌더 HTML override → 재수집.
        # 렌더링만으로 해결되면 비전 호출을 하지 않는다. 실측(30876)에서 본문 0자 → 렌더 2054자
        # → 7/8 항목까지 회복됐다.
        # 캡처는 R0b 에서 전건 끝냈으므로 보통 아래 재캡처는 돌지 않는다 — `--shots flagged`
        # 처럼 범위를 좁혀 실행했을 때만 빠진 건을 메운다(방문 중복 방지).
        미보유 = [r for r in 진단대상 if not (shots / f"{r}_footer.png").exists()]
        if 미보유:
            run([PY, str(HERE / "capture_shots.py"), *공통,
                 "--ids", ",".join(미보유), "--emit-override"], label="capture_shots")
        run([PY, str(HERE / "run_extract.py"), *공통,
             "--ids", ",".join(진단대상)], label="run_extract(렌더적용)")
        work = load_work(Path(W))
        렌더해소, 남음 = [], []
        for rid in 진단대상:
            g = gateA(work.get(rid, {}), args.pass_level, base)
            등급[rid] = g
            기록(Path(W), rid, "R2a", g, work.get(rid, {}))
            (렌더해소 if g["등급"] == gate.통과 else 남음).append(rid)
        이력.append({"라운드": "R2a 렌더링적용", "대상": len(진단대상),
                    "비용_USD": 0, "렌더링만으로_해소": 렌더해소, "비전진단_필요": 남음})
        print(f"   렌더링 적용: 해소 {len(렌더해소)} / 비전필요 {len(남음)}", file=sys.stderr)

        # R2b 남은 건만 비전 진단
        해소, 잔존 = list(렌더해소), []
        if 남음:
            rc, out = run([PY, str(HERE / "diagnose_site.py"), "--workdir", W,
                           "--기준일", D, "--ids", ",".join(남음)], label="diagnose_site")
            r2 = tail_json(out)
            run([PY, str(HERE / "run_extract.py"), *공통,
                 "--ids", ",".join(남음)], label="run_extract(진단적용)")
            work = load_work(Path(W))
            for rid in 남음:
                g = gateA(work.get(rid, {}), args.pass_level, base)
                등급[rid] = g
                기록(Path(W), rid, "R2b", g, work.get(rid, {}))
                (해소 if g["등급"] == gate.통과 else 잔존).append(rid)
            이력.append({"라운드": "R2b 비전진단", "대상": len(남음),
                        "비용_USD": r2.get("총비용_USD"),
                        "해소": [r for r in 해소 if r in 남음], "잔존(확인필요 확정)": 잔존})
            print(f"   비전 진단 후: 해소 {len([r for r in 해소 if r in 남음])} / 잔존 {len(잔존)}",
                  file=sys.stderr)
    elif 진단대상:
        이력.append({"라운드": "R2 비전진단",
                    "생략": "--no-vision" if args.no_vision else "--skip-llm",
                    "미처리_진단대상": 진단대상})

    # ---------------- 발행연월일 전용 검증 ----------------
    # 정의가 '최신 기사 업데이트 날짜' 라서 규칙으로 확정할 수 없다. 반드시 LLM 이 확인하고,
    # 메인에 없으면 가장 지배적인 기사 상세까지 들어가 확인한다.
    if P2 and not args.skip_llm:
        print("== R3 발행연월일 검증 (전건) ==", file=sys.stderr)
        if args.batch:
            rc, _ = run([PY, str(HERE / "verify_pubdate.py"), "--workdir", W, "--기준일", D,
                         "--batch-submit"], label="verify_pubdate(batch-submit)")
            print("   배치 제출 완료 — 결과 대기(최대 24h)", file=sys.stderr)
            rc, out = run([PY, str(HERE / "verify_pubdate.py"), "--workdir", W, "--기준일", D,
                           "--batch-collect"], label="verify_pubdate(batch-collect)")
        else:
            rc, out = run([PY, str(HERE / "verify_pubdate.py"), "--workdir", W, "--기준일", D],
                          label="verify_pubdate")
        r3 = tail_json(out)
        이력.append({"라운드": "R3 발행일검증", "대상": r3.get("대상"),
                    "비용_USD": r3.get("총비용_USD"),
                    "메인게재": r3.get("메인게재"),
                    "기사상세로_확인": r3.get("기사상세로_확인"),
                    "확인실패": r3.get("확인실패")})
        print(f"   메인게재 {r3.get('메인게재')} / 기사상세 {r3.get('기사상세로_확인')} "
              f"/ 실패 {r3.get('확인실패')}", file=sys.stderr)

    # ---------------- 라운드 4: 주석본 + 근거위치 ----------------
    # 저장된 렌더HTML 을 file:// 로 열어 표시한다 — 네트워크 방문 없음, API 호출 없음.
    # 판단이 끝난 뒤라야 주석이 최종값(LLM·비전·발행일 반영)을 가리킨다.
    #
    # ★ --skip-llm 에 묶지 않는다. 이 라운드는 무료이고, evidence/{id}.json(항목별 근거
    #   DOM 위치)을 만드는 유일한 지점이다. 규칙만 돌리는 실행에서도 위치지도가 나와야 한다.
    if P2 and args.shots != "none":
        print("== R4 주석본·근거위치 (저장본 재렌더, 방문·API 없음) ==", file=sys.stderr)
        cmd4 = [PY, str(HERE / "capture_shots.py"), *공통, "--annotate-only"]
        if args.op_col:
            cmd4 += ["--운영컬럼", args.op_col, "--운영값", args.op_val]
        if args.shots == "flagged":
            cmd4 += ["--only-flagged"]
        rc, out = run(cmd4, label="capture_shots(주석)")
        cs4 = tail_json(out)
        이력.append({"라운드": "R4 주석본·근거위치", "비용_USD": 0, "대상": cs4.get("대상"),
                    "성공": cs4.get("성공"),
                    "주석_표시_합계": cs4.get("주석_표시_합계")})
        print(f"   주석본 {cs4.get('성공')}/{cs4.get('대상')} "
              f"({cs4.get('주석_표시_합계')}항목)", file=sys.stderr)

    # ---------------- 마무리 ----------------
    if not P2:
        print(json.dumps({"phase": args.phase, "이력": 이력,
                          "다음": "phase 2 로 LLM 판단·산출을 이어서 돌려라"},
                         ensure_ascii=False, indent=2))
        return

    print("== 마무리 LLM판정병합·대조·최종·위치지도 ==", file=sys.stderr)
    # 준수여부_LLM 이 최종 판정이다. 대조 전에 병합해야 확인필요 사유가 함께 재도출된다.
    rc, out = run([PY, str(HERE / "merge_llm.py"), "--workdir", W, "--기준일", D],
                  label="merge_llm")
    ml = tail_json(out)
    이력.append({"라운드": "병합 LLM판정", "준수_규칙기준": ml.get("준수_규칙기준"),
                "준수_LLM기준": ml.get("준수_LLM기준"),
                "필수8_합계_규칙": ml.get("필수8_합계_규칙"),
                "필수8_합계_LLM": ml.get("필수8_합계_LLM"),
                "규칙과_달라진_건": ml.get("규칙과_달라진_건")})
    rc, out = run([PY, str(HERE / "compare_registry.py"), *공통], label="compare_registry")
    v = tail_json(out)
    rc, out = run([PY, str(HERE / "finalize.py"), *공통], label="finalize")
    fin = tail_json(out)
    rc, out = run([PY, str(HERE / "location_map.py"), *공통], label="location_map")
    lm = tail_json(out)

    # ---------------- 결과 점검 ----------------
    # 점검도 스크립트가 API 로 한다. 결정적 지표는 계산하고 판단만 모델에 묻는다.
    # 이 단계가 없으면 "잘 돌았는지" 를 사람이 매번 눈으로 훑어야 하고, 8,000건에서는 불가능하다.
    insp = {}
    if not args.skip_llm:
        print("== 점검 inspect_run ==", file=sys.stderr)
        rc, out = run([PY, str(HERE / "inspect_run.py"), "--workdir", W, "--기준일", D],
                      label="inspect_run")
        insp = tail_json(out)
        이력.append({"라운드": "점검 inspect_run", "비용_USD": insp.get("총비용_USD"),
                    "차원별_판정": insp.get("차원별_판정"),
                    "심각도_분포": insp.get("심각도_분포")})
        print(f"   점검 판정: {insp.get('차원별_판정')} "
              f"결함 {insp.get('심각도_분포')}", file=sys.stderr)

    # 게이트A 통과("규칙만으로 8항목 확보")와 게이트B 통과("이미지 진단 불필요")는 뜻이 다르다.
    # 한 칸에 섞으면 통과 건수가 부풀려져 최종 xlsx 의 준수 건수와 어긋난다 — 따로 보고한다.
    work = load_work(Path(W))
    A분포, B분포 = {}, {}
    for rid, d in work.items():
        ga = gateA(d, args.pass_level, base)
        A분포[ga["등급"]] = A분포.get(ga["등급"], 0) + 1
        gb = gateB(Path(W), rid, d)
        if gb:
            B분포[gb["등급"]] = B분포.get(gb["등급"], 0) + 1
    총비용 = sum(h.get("비용_USD") or 0 for h in 이력)
    미해결 = [rid for rid, d in work.items()
            if gateA(d, args.pass_level, base)["등급"] != gate.통과]

    print(json.dumps({
        "기준일": D, "통과기준": args.pass_level, "max_rounds": args.max_rounds,
        "라운드_이력": 이력,
        "점검": {"보고서": insp.get("보고서_md"),
               "차원별_판정": insp.get("차원별_판정"),
               "확정_결함": insp.get("확정_결함"),
               "심각도_분포": insp.get("심각도_분포")} if insp else "미실행",
        "게이트A_최종(규칙만으로 8항목 확보했나)": A분포,
        "게이트B_최종(이미지 진단이 더 필요한가)": B분포,
        "총_LLM_비용_USD": round(총비용, 5),
        "게이트A_미통과(사람 검수 필요)": 미해결,
        "최종산출": {
            "verify_csv": v.get("verify_csv") or f"{W}/outputs/verify_{D}.csv",
            "final_xlsx": fin.get("final_xlsx"),
            "위치지도_xlsx": lm.get("위치지도_xlsx"),
            "준수_규칙기준(대조용)": ml.get("준수_규칙기준"),
            "준수_LLM기준(최종 판정)": ml.get("준수_LLM기준"),
            "확인필요": fin.get("확인필요"),
        },
        "주의": [
            "AI 추출 초안이다. 확인필요 건은 사람 검증 전까지 확정이 아니다.",
            "최종 판정은 `준수여부_LLM` 이다 — LLM 이 규칙 1차결과를 함께 받아 판단한 종합 결과다. "
            "`준수여부_8항목`(규칙 기준)은 대조·감사용으로 남긴다.",
            "LLM 판정은 규칙보다 엄격할 수 있다 — `규칙_LLM_차이` 의 `-항목(규칙기각)` 은 "
            "규칙이 근거 없이 낸 값을 버린 것이다.",
        ],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
