#!/usr/bin/env python3
"""capture_shots 를 N개 프로세스로 병렬 실행하고, 잘 안 된 건을 한 번 더 본다.

왜 프로세스인가
---------------
Playwright 동기 API 는 스레드 안전하지 않다. 그래서 스레드로 못 나누고 **프로세스**로
쪼갠다. 분할은 `capture_shots --shard K --shards N` 이 맡고, 기준은 **도메인 해시**다 —
같은 호스트는 항상 같은 샤드로 가므로 서로 다른 프로세스가 한 사이트를 동시에 때리지 않는다.

동시 실행 수를 정할 때 (2026-09-03 판단)
----------------------------------------
원격 밴보다 **로컬 렌더링 용량**이 먼저 걸린다. 사이트당 방문은 2회(메인+기사 1편)뿐이고
대상이 13,574개 서로 다른 도메인이라 한 호스트에 부하가 몰리지 않는다. 반면 Chromium
컨텍스트는 하나에 150~300MB를 쓰고, 너무 많이 띄우면 렌더링이 느려져 정착 루프가
타임아웃하고 **덜 그려진 화면을 증빙으로 찍는다**. 느린 것보다 이게 나쁘다.

그래서 이 스크립트는 **2단 구조**다:
    1단  N개 병렬로 빠르게 훑는다 (기본 10)
    2단  품질이 나쁜 건만 골라 **낮은 동시성**으로 다시 본다 (기본 2)
2단이 있어야 1단을 공격적으로 잡을 수 있다.

재시도 대상 (`품질불량`)
-----------------------
  · 캡처 결과가 성공이 아님 (탐색실패 등)
  · 렌더HTML 이 없음
  · 정착 미완 — 본문이 계속 자라는 중에 찍었다
  · 화면 내 이미지 미완 — 로고·게재사항이 이미지인 매체에서 증빙이 못 된다

사용
----
  python run_capture_parallel.py --workdir W --input in.xlsx --기준일 D \
      [--shards 10] [--retry-shards 2] [--no-retry] [--only-flagged]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from md_core import audit  # noqa: E402


# ── 무한 대기 워치독 (ISS-066) ──────────────────────────────────────────────
# Playwright 의 page.evaluate() 에는 타임아웃 인자가 **아예 없다**. 사이트 JS 가 메인
# 스레드를 막으면 영원히 안 돌아오고, page.goto 의 NAV_TIMEOUT 은 이미 지난 뒤라 소용없다.
# 동기 API 는 다른 스레드에서 건드릴 수 없으므로 **프로세스 밖에서** 감시하는 수밖에 없다.
#
# 2026-09-04 전수에서 두 번 터졌다: 6203 luxuryedu.kr(78분), 11711 newsmon.co.kr(94분).
# 각각 샤드 하나를 통째로 묶어 전체 일정을 2시간 넘게 늘렸다. 13,574건 중 2건(0.015%)이다.
#
# 판정은 **로그 mtime** 으로 한다. 진행줄은 stderr(무버퍼)로 나가므로 버퍼링 지연이 없다.
STALL_SEC = 480          # 이 시간 진행이 없으면 멈춘 것으로 본다
WATCH_TICK = 30
MAX_REVIVE = 6           # 샤드당 재기동 상한 — 무한 반복 방지

# capture_shots 가 **자기 건별 상한**(기본 90초)을 넘겨 스스로 끝낼 때 쓰는 종료코드.
# 이걸 모르면 부모가 그냥 '실패한 샤드' 로 보고 남은 몫을 통째로 버린다.
EXIT_ITEM_STALLED = 42

_PROG = re.compile(r"^\[(\d+)/(\d+)\]\s+(\S+)")


def 등록부_순서(args) -> list[dict]:
    """capture_shots 가 도는 것과 **같은 순서**의 행 목록. 워치독이 '다음 차례' 를
    알아내려면 순서가 일치해야 한다."""
    from md_core.registry import load_registry
    override = json.loads(args.map_json) if args.map_json else None
    rows, _, _ = load_registry(args.input, override_map=override, sheet=args.sheet)
    return rows


def 해시분할(args, W: Path, n: int) -> dict[int, list[str]]:
    """도메인 md5 해시로 N등분. 같은 호스트는 항상 같은 샤드로 간다 —
    서로 다른 프로세스가 한 사이트를 동시에 때리지 않게 하려는 것이다.
    capture_shots 의 --shard/--shards 와 같은 식을 쓴다."""
    import hashlib
    from urllib.parse import urlparse
    from md_core.http import normalize_url

    def dom(u: str) -> str:
        h = urlparse(normalize_url(u or "")).netloc.lower()
        return h[4:] if h.startswith("www.") else h

    out: dict[int, list[str]] = {k: [] for k in range(n)}
    for r in 등록부_순서(args):
        key = dom(r.get("url") or "") or str(r.get("id"))
        out[int(hashlib.md5(key.encode()).hexdigest(), 16) % n].append(str(r.get("id")))
    return out


def 처리된_ids(log: Path) -> list[str]:
    out = []
    try:
        with log.open(encoding="utf-8", errors="replace") as f:
            for ln in f:
                m = _PROG.match(ln)
                if m:
                    out.append(m.group(3))
    except OSError:
        pass
    return out


def tail_json(path: Path) -> dict:
    """로그 끝에 붙은 JSON 요약을 되돌린다. 자식 stdout 이 로그로 리다이렉트돼 있다."""
    try:
        txt = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    i = txt.rfind("\n{")
    for start in ([i + 1] if i >= 0 else []) + [txt.find("{")]:
        if start < 0:
            continue
        try:
            return json.loads(txt[start:])
        except Exception:  # noqa: BLE001
            continue
    return {}


def spawn(cmd: list[str], log: Path):
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    f = log.open("w", encoding="utf-8")
    return subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT, env=env), f


# 화면 안에서 안 뜬 이미지가 몇 개부터 증빙 결함인가.
# 전수 4,323건 실측: '미완 1개' 357건, '2개' 126건, '3개 이상' 144건. 1~2개는 배너·아이콘
# 하나가 늦은 것이라 게재사항 판독에 영향이 없는데, 이걸 재시도에 넣으면 대상이 33% 로
# 부풀어 2단에만 12시간이 붙는다. 게다가 느리거나 깨진 이미지는 다시 봐도 또 미완이다.
IMG_BAD_MIN = 3

_IMGN = re.compile(r"미완 이미지 (\d+)개")


def 품질불량(workdir: Path, ids: list[str]) -> list[str]:
    """캡처가 잘 안 된 id — 낮은 동시성으로 다시 볼 대상.

    **죽은 사이트는 넣지 않는다.** requests 도 실패하고 브라우저도 실패한 건은 재시도해도
    같은 결과다(전수 실측: 탐색실패 654건 중 646건이 requests 도 불가·URL없음, 재시도 가치가
    있는 '브라우저만 실패' 는 8건뿐이었다). 이들은 지침 I-4 홈페이지없음 으로 이미 정확히
    분류돼 Phase 3 포털검색으로 간다. 재시도는 **덜 그려진 화면**을 다시 찍는 게 목적이지
    죽은 사이트를 되살리는 게 아니다.
    """
    bad = []
    shots = workdir / "shots"
    for rid in ids:
        det, 결과, 접속 = {}, None, ""
        for s in audit.load(workdir, rid).get("단계", []):
            if s.get("단계") == "1_수집":
                접속 = s.get("결과") or ""
            elif s.get("단계") == "4_증빙캡처":
                det = s.get("상세") or {}
                결과 = s.get("결과")
        if 결과 is None:
            bad.append(rid)          # 캡처 기록 자체가 없다
            continue
        if 결과 != "성공":
            # requests 로는 본문을 받았는데 브라우저만 실패 → 진짜 이상, 다시 본다
            if 접속 in ("성공", "내용없음"):
                bad.append(rid)
            continue
        if not (shots / f"{rid}_rendered.html").exists():
            bad.append(rid); continue
        if det.get("정착_완료") is False:
            bad.append(rid); continue
        m = _IMGN.search(str(det.get("이미지대기") or ""))
        if m and int(m.group(1)) >= IMG_BAD_MIN:
            bad.append(rid); continue
    return bad


def 대상_ids(workdir: Path, base: str, only_flagged: bool) -> list[str]:
    import csv as _csv
    for name in (f"verify_{base}.csv", f"extract_{base}.csv"):
        p = workdir / "outputs" / name
        if not p.exists():
            continue
        out = []
        with p.open(encoding="utf-8-sig") as f:
            for r in _csv.DictReader(f):
                if only_flagged and r.get("확인필요") != "Y":
                    continue
                rid = str(r.get("언론사_id") or "").strip()
                if rid:
                    out.append(rid)
        return out
    return []


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--input", required=True)
    ap.add_argument("--sheet")
    ap.add_argument("--map", dest="map_json", default="")
    ap.add_argument("--base-date", "--기준일", dest="base_date", required=True)
    ap.add_argument("--shards", type=int, default=10, help="1단 동시 프로세스 수")
    ap.add_argument("--retry-shards", type=int, default=2, help="2단(재시도) 동시 프로세스 수")
    ap.add_argument("--no-retry", action="store_true", help="2단 재시도를 하지 않는다")
    ap.add_argument("--only-flagged", action="store_true",
                    help="확인필요 건만 캡처 (기본은 전건)")
    ap.add_argument("--ids", default="",
                    help="쉼표 구분 id 만 캡처. 도메인 해시 대신 목록을 N등분한다 "
                         "(--shard/--shards 는 전수 기준이라 부분 목록에 못 쓴다)")
    ap.add_argument("--ids-file", dest="ids_file", default="",
                    help="id 목록 파일 (쉼표·줄바꿈 구분). 명령줄 32,767자 상한 회피용")
    # 로그 파일은 "w" 로 열어 매번 자른다. 부분 재실행을 기본 접두어로 돌리면 앞선
    # 전수 캡처의 capture_shard0~3.log 를 덮어써 감사 기록과 진행 집계가 사라진다.
    ap.add_argument("--log-prefix", dest="log_prefix", default="capture_shard",
                    help="샤드 로그 파일 접두어. 부분 재실행은 다른 값을 줘라")
    ap.add_argument("--no-article", action="store_true")
    # R4 주석본. 저장된 렌더HTML 을 file:// 로 다시 열 뿐 사이트를 방문하지 않는다.
    # 10,218건을 순차로 돌리면 30시간이라 여기서도 병렬이 필요하다.
    ap.add_argument("--annotate-only", action="store_true",
                    help="주석본만 만든다 (저장본 재렌더, 방문·API 없음)")
    # ★ 없으면 R0c 가 통째로 죽는다. capture_shots 는 이 플래그가 있을 때만
    #   overrides/{도메인}.json 에 rendered_html 을 써 준다. orchestrate 는 단일 프로세스
    #   경로에만 이걸 붙이고 있었고, 병렬 경로로 오면 override 가 0개라
    #   `렌더HTML 반영 재추출` 이 이름만 남는다 (ISS-064).
    ap.add_argument("--emit-override", action="store_true",
                    help="렌더 본문이 원 수집보다 길면 overrides 에 rendered_html 을 써준다")
    args = ap.parse_args()

    W = Path(args.workdir)
    (W / "logs").mkdir(parents=True, exist_ok=True)
    공통 = ["--workdir", str(W), "--input", args.input, "--기준일", args.base_date]
    if args.sheet:
        공통 += ["--sheet", args.sheet]
    if args.map_json:
        공통 += ["--map", args.map_json]
    if args.only_flagged:
        공통 += ["--only-flagged"]
    if args.no_article:
        공통 += ["--no-article"]
    if args.emit_override:
        공통 += ["--emit-override"]
    if args.annotate_only:
        공통 += ["--annotate-only"]

    # ── 1단: N개 병렬 ────────────────────────────────────────────────────────
    id원문 = args.ids
    if args.ids_file:
        id원문 += "," + Path(args.ids_file).read_text(encoding="utf-8")
    지정 = [s for s in re.split(r"[,\s]+", id원문) if s]
    t0 = time.time()

    # 샤드별 담당 목록을 **드라이버가** 정한다. 예전엔 --shard/--shards 를 넘겨
    # 자식이 도메인 해시로 스스로 골랐는데, 그러면 멈춘 샤드의 '남은 몫' 을 드라이버가
    # 알 수 없어 재기동을 못 한다. 해시 계산을 여기로 올리고 목록을 파일로 넘긴다
    # (같은 호스트가 같은 샤드로 가는 성질은 그대로 유지된다).
    if 지정:
        # 지정 목록도 **등록부 순서**로 세운다 — 자식이 그 순서로 돌기 때문에
        # 워치독이 '아직 안 찍힌 것 중 첫 번째 = 매달린 건' 이라고 볼 수 있다.
        순 = [str(r.get("id")) for r in 등록부_순서(args) if str(r.get("id")) in set(지정)]
        몫 = {k: 순[k::args.shards] for k in range(args.shards)}
    else:
        몫 = 해시분할(args, W, args.shards)
    hang_log = W / "logs" / "hang_ids.txt"

    def 감시_실행(할당: dict, 접두어: str, 라벨: str):
        """샤드를 띄우고 **무한 대기 워치독**을 물려 끝까지 돌린다.

        1단과 2단이 같은 함수를 쓴다. 2단만 p.wait() 로 두면 거기서 매달렸을 때
        아무도 깨우지 못한다 — r2 반복 실행이 재시도 샤드에서 10시간 정지했다(ISS-068).
        """
        def 띄우기(k: int, ids: list[str]):
            idf = W / "logs" / f"{접두어}{k}.ids"
            idf.write_text("\n".join(ids), encoding="utf-8")
            log = W / "logs" / f"{접두어}{k}.log"
            p, f = spawn([sys.executable, str(HERE / "capture_shots.py"), *공통,
                          "--ids-file", str(idf)], log)
            return {"k": k, "p": p, "f": f, "log": log, "ids": ids}

        사는중 = [띄우기(k, v) for k, v in 할당.items() if v]
        print(f"[{라벨}] {len(사는중)}개 샤드 병렬 시작"
              + (f" (지정 {len(지정)}건)" if 지정 and 라벨 == "1단" else ""), file=sys.stderr)

        실패샤드: list[int] = []
        부활 = {k: 0 for k in 할당}
        멈춘_ids: list[str] = []
        while 사는중:
            time.sleep(WATCH_TICK)
            남김 = []
            for s in 사는중:
                rc = s["p"].poll()
                if rc == EXIT_ITEM_STALLED:
                    # 자식이 **스스로** 건별 상한(기본 90초)을 넘겨 끝냈다. 부모의 8분
                    # mtime 워치독보다 훨씬 빠르다. 멈춘 건만 빼고 즉시 다시 띄운다.
                    s["f"].close()
                    처리 = set(처리된_ids(s["log"]))
                    남은 = [i for i in s["ids"] if i not in 처리]
                    멈춤 = 남은[0] if 남은 else None
                    print(f"  샤드 {s['k']} 자체상한 종료 — 처리 {len(처리)}, "
                          f"남은 {len(남은)}, 제외 {멈춤}", file=sys.stderr)
                    if 멈춤:
                        멈춘_ids.append(멈춤)
                        남은 = [i for i in 남은 if i != 멈춤]
                        with hang_log.open("a", encoding="utf-8") as hf:
                            hf.write(f"{멈춤}\n")
                    부활[s["k"]] += 1
                    if 남은 and 부활[s["k"]] <= MAX_REVIVE:
                        남김.append(띄우기(s["k"], 남은))
                    elif 남은:
                        실패샤드.append(s["k"])
                    continue
                if rc is not None:
                    s["f"].close()
                    if rc != 0:
                        실패샤드.append(s["k"])
                    print(f"  샤드 {s['k']} 종료 rc={rc}  ({s['log']})", file=sys.stderr)
                    continue
                # 살아 있다 — 진행이 멈췄는지 본다
                try:
                    정체 = time.time() - s["log"].stat().st_mtime
                except OSError:
                    정체 = 0
                if 정체 < STALL_SEC:
                    남김.append(s); continue

                # ── 무한 대기로 판정: 죽이고 남은 몫으로 다시 띄운다 ──────────────
                # s["ids"] 는 **등록부 행 순서**다(자식이 도는 순서와 같다). 따라서
                # 아직 안 찍힌 것 중 첫 번째가 곧 매달려 있는 건이다.
                처리 = set(처리된_ids(s["log"]))
                done = len(처리)
                남은 = [i for i in s["ids"] if i not in 처리]
                멈춤 = 남은[0] if 남은 else None
                print(f"  샤드 {s['k']} {정체/60:.0f}분 무진행 — 죽이고 재기동 "
                      f"(처리 {done}, 남은 {len(남은)}, 제외 {멈춤})", file=sys.stderr)
                try:
                    s["p"].kill()
                except Exception:  # noqa: BLE001
                    pass
                s["f"].close()
                if 멈춤:
                    멈춘_ids.append(멈춤)
                    남은 = [i for i in 남은 if i != 멈춤]
                    with hang_log.open("a", encoding="utf-8") as hf:
                        hf.write(f"{멈춤}\n")
                부활[s["k"]] += 1
                if 남은 and 부활[s["k"]] <= MAX_REVIVE:
                    남김.append(띄우기(s["k"], 남은))
                else:
                    실패샤드.append(s["k"])
            사는중 = 남김
        return 실패샤드, 멈춘_ids

    실패샤드, 멈춘_ids = 감시_실행(몫, args.log_prefix, "1단")
    t1 = time.time()

    ids = 지정 or 대상_ids(W, args.base_date, args.only_flagged)
    나쁨 = 품질불량(W, ids)
    요약 = {"1단_샤드": args.shards, "1단_초": round(t1 - t0),
           "1단_실패샤드": 실패샤드, "대상": len(ids), "품질불량": len(나쁨)}

    # ── 2단: 품질 불량만 낮은 동시성으로 ─────────────────────────────────────
    # 주석본은 저장본을 다시 그릴 뿐이라 '품질불량 재시도' 개념이 없다.
    # 그대로 두면 캡처 품질 기준으로 엉뚱한 건을 다시 그린다.
    if 나쁨 and not args.no_retry and not args.annotate_only:
        print(f"[2단] 품질불량 {len(나쁨)}건을 {args.retry_shards}개로 재시도", file=sys.stderr)
        # 나쁨 은 CSV(=등록부) 순서라 k::n 슬라이스도 그 순서를 지킨다. 워치독의
        # '아직 안 찍힌 것 중 첫 번째가 매달린 건' 추론이 성립하려면 이 순서가 필요하다.
        재몫 = {k: 나쁨[k::args.retry_shards] for k in range(args.retry_shards)}
        실패2, 멈춤2 = 감시_실행(재몫, f"{args.log_prefix}_retry", "2단")
        멈춘_ids += 멈춤2
        남음 = 품질불량(W, 나쁨)
        요약.update({"2단_초": round(time.time() - t1), "2단_실패샤드": 실패2,
                    "재시도_해소": len(나쁨) - len(남음), "재시도_잔존": len(남음),
                    "잔존_id": 남음[:40]})
    요약["총_초"] = round(time.time() - t0)
    요약["렌더HTML_보유"] = len(list((W / "shots").glob("*_rendered.html")))
    # orchestrate 가 읽는 키와 맞춘다 (단일 실행 요약과 같은 이름).
    # ★ 렌더링_개선_id 를 안 넘기면 orchestrate 가 R0c 대상을 빈 목록으로 보고 단계를
    #   통째로 건너뛴다. 샤드마다 stdout 끝에 JSON 요약을 찍으므로 그걸 합친다 (ISS-064).
    요약["대상"] = len(ids)
    요약["성공"] = len(ids) - len(품질불량(W, ids))
    요약["기사페이지_확보"] = len(list((W / "shots").glob("*_article.html")))
    # capture_shots 는 `override_생성` 을 **개수가 아니라 id 리스트**로 준다.
    # int() 로 받으면 TypeError 로 드라이버가 죽고, 요약이 안 나오면 orchestrate 가
    # 렌더링_개선_id 를 빈 목록으로 보고 R0c 를 통째로 건너뛴다 (ISS-064 재발 경로).
    def 목록(j, key):
        v = j.get(key)
        if isinstance(v, list):
            return [str(x) for x in v]
        return []

    개선: list[str] = []
    ovids: list[str] = []
    for log in sorted((W / "logs").glob(f"{args.log_prefix}*.log")):
        j = tail_json(log)
        개선 += 목록(j, "렌더링_개선_id")
        ovids += 목록(j, "override_생성")
    def _키(x):
        return (0, int(x)) if x.isdigit() else (1, 0)
    요약["렌더링_개선_id"] = sorted(set(개선), key=_키)
    요약["override_생성"] = len(set(ovids)) or len(list((W / "overrides").glob("*.json")))
    print(json.dumps(요약, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
