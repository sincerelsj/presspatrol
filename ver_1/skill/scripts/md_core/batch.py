"""Message Batches API 공용 헬퍼 — 제출·폴링·수집.

왜 배치인가 (2026-09-03 결정)
-----------------------------
102건 실측에서 API 단계가 순차 호출로 건당 40.8초였다. 13,000건 환산 **147시간 / $350**.
배치는 **모든 토큰 50% 할인**이고, 수천 건을 한 번에 던져 대부분 1시간 안에 받는다.
사용자 결정: **배치는 쓰고, 동시 호출(ThreadPool)은 쓰지 않는다.**

어느 단계가 배치에 맞는가 — 판단 근거
-------------------------------------
    R1  llm_extract    ✅ 사이트당 단발 호출. 근거수리 2차 호출만 드물게 순차
    R3  verify_pubdate ✅ 1차(메인) 배치 → 2차(기사상세)는 1차 결과에 의존해 별도 패스
    R2  diagnose_site  ⚠️ 스크린샷 base64 — **배치당 256MB 한도**에 걸린다(청크 필요). 보류
    R1b resolve_failed ❌ `web_search` 서버 도구의 배치 동작 미검증 + 호출 뒤 후보 fetch·
                          게재사항 대조라는 I/O 단계가 붙는다. 대상 8% 라 위험 대비 이득 없음

★ 게이트가 배치 사이에 있다. 게이트B 는 R1 결과를 봐야 R2 대상을 정하므로
  `R1 배치 → 수집 → 게이트 → R2` 로 **단계별 순차**다. 한 배치에 두 단계를 못 섞는다.

배치의 대가
-----------
  · 진행이 안 보인다 — 끝나야 결과가 나온다. 잘못된 프롬프트로 1만 건을 날릴 수 있으므로
    반드시 소규모 `--sync` 로 검증한 뒤 전수 배치를 던진다.
  · 결과가 **임의 순서**로 온다 → `custom_id` 로 키를 잡는다(위치로 매칭하면 깨진다).
  · 건별로 `errored`/`expired` 가 날 수 있다 → 재제출 패스가 필요하다.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

# custom_id 는 영숫자·언더바·하이픈만 허용된다. 언론사_id 를 그대로 쓰되 접두를 붙인다.
ID_PREFIX = "md"
POLL_SEC = 30
POLL_MAX_SEC = 24 * 3600          # 배치 SLA 상한과 같다


def batch_dir(workdir: Path | str) -> Path:
    p = Path(workdir) / "batch"
    p.mkdir(parents=True, exist_ok=True)
    return p


def meta_path(workdir: Path | str, stage: str, base_date: str) -> Path:
    return batch_dir(workdir) / f"{stage}_{base_date}.json"


def cid(rid: str) -> str:
    return f"{ID_PREFIX}-{rid}"


def rid_of(custom_id: str) -> str:
    return custom_id.split("-", 1)[1] if "-" in custom_id else custom_id


def submit(client, workdir: Path | str, stage: str, base_date: str,
           requests: list, *, note: str = "") -> dict:
    """배치 제출 → 메타 파일 기록. requests 는 SDK Request 객체 리스트."""
    if not requests:
        return {"error": "제출할 요청이 없다"}
    b = client.messages.batches.create(requests=requests)
    meta = {"stage": stage, "batch_id": b.id, "기준일": base_date,
            "제출건수": len(requests), "제출시각": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "processing_status": b.processing_status, "note": note}
    meta_path(workdir, stage, base_date).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


def load_meta(workdir: Path | str, stage: str, base_date: str) -> dict:
    p = meta_path(workdir, stage, base_date)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def wait_ended(client, batch_id: str, *, poll: int = POLL_SEC,
               timeout: int = POLL_MAX_SEC, on_tick=None) -> str:
    """`processing_status == "ended"` 까지 폴링. → 최종 상태."""
    waited = 0
    while True:
        b = client.messages.batches.retrieve(batch_id)
        st = b.processing_status
        if st == "ended":
            return st
        if waited >= timeout:
            return st
        if on_tick:
            rc = getattr(b, "request_counts", None)
            on_tick(waited, st, rc)
        time.sleep(poll)
        waited += poll


def results(client, batch_id: str, tool_name: str) -> tuple[dict, dict]:
    """→ ({rid: (tool_input, usage)}, {rid: 실패사유}).

    성공 건에서 `tool_use` 블록의 input 을 꺼낸다. usage 에 `batch=True` 를 실어
    비용 계산이 50% 할인을 반영하게 한다.
    """
    ok: dict = {}
    bad: dict = {}
    for r in client.messages.batches.results(batch_id):
        rid = rid_of(r.custom_id)
        rtype = r.result.type
        if rtype != "succeeded":
            err = getattr(getattr(r.result, "error", None), "type", "") or ""
            bad[rid] = f"{rtype}{(': ' + err) if err else ''}"
            continue
        msg = r.result.message
        tool_input = {}
        for blk in msg.content:
            if getattr(blk, "type", "") == "tool_use" and blk.name == tool_name:
                tool_input = blk.input
                break
        u = msg.usage
        ok[rid] = (tool_input, {
            "input_tokens": getattr(u, "input_tokens", 0),
            "output_tokens": getattr(u, "output_tokens", 0),
            "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
            "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
            "stop_reason": getattr(msg, "stop_reason", ""),
            "batch": True,
        })
    return ok, bad


__all__ = ["batch_dir", "meta_path", "cid", "rid_of", "submit", "load_meta",
           "wait_ended", "results", "POLL_SEC"]
