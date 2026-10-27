#!/usr/bin/env python3
"""capture_shots — Playwright 로 증빙 스크린샷 + 렌더링 HTML 저장.

왜 필요한가
-----------
1. **검수 증빙** — 판정 결과를 사람이 확정하려면 "그때 그 화면" 이 있어야 한다.
   신문법은 '메인화면 **상·하단**' 게재를 요구하므로 상단·하단을 각각 남긴다.
2. **JS 렌더링 사이트 구제** — requests 로는 본문 0자인 사이트(예: 30876 세종타임즈)가 있다.
   렌더링된 HTML 을 저장해 두면 기존 `file://` 판독 경로가 그대로 받는다(ISS-002 에서 수정 완료).

산출
----
  W/shots/{id}_top.png        상단 뷰포트
  W/shots/{id}_footer.png     하단으로 스크롤한 뷰포트
  W/shots/{id}_rendered.html  렌더링 후 DOM (재판독용)
  W/audit/{id}.json           4_증빙캡처 단계 기록

사용
----
  python capture_shots.py --workdir ver_1/run2 --input ver_1/input/pilot2.xlsx
  python capture_shots.py --workdir ver_1/run2 --input ... --only-flagged   # 확인필요 건만
  python capture_shots.py --workdir ver_1/run2 --input ... --ids 30876

렌더링 HTML 을 판독에 쓰려면:
  overrides/{도메인}.json 에  {"rendered_html": "file:///.../shots/30876_rendered.html"}
  를 넣고 run_extract.py 를 재실행한다. --emit-override 로 자동 생성할 수 있다.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import threading
import time
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from md_core import audit  # noqa: E402
from md_core.http import normalize_url  # noqa: E402
from md_core.registry import load_registry  # noqa: E402

VIEWPORT = {"width": 1440, "height": 900}
NAV_TIMEOUT = 45_000

# 정착(settle) 대기 — 고정 시간이 아니라 **본문 텍스트가 자라기를 멈출 때까지** 기다린다.
#
# 왜 바꿨나: 예전에는 domcontentloaded 뒤 1.8초 고정이었다. domcontentloaded 는 이미지·지연
# 스크립트·XHR 을 기다리지 않으므로, 기사 목록을 JS 로 채우는 사이트에서 덜 그려진 화면을 찍었다.
# pilot4 실측에서 Playwright 렌더 본문이 requests 원수집보다 **짧은** 건이 6건 나왔다 —
# 30893 2,060자 vs 4,867자(42%), 30901 973자 vs 1,713자(57%), 30877 2,276자 vs 4,390자(52%).
# 브라우저가 JS 를 돌린 결과가 생 HTML 보다 적다는 건 로딩 미완의 증거다.
SETTLE_MIN_MS = 1_500      # 최소 이만큼은 기다린다
SETTLE_MAX_MS = 20_000     # 이 이상은 기다리지 않는다 (8,000건 전수에서 시간이 문제가 된다)
SETTLE_STEP_MS = 500       # 정착 신호를 이 간격으로 재본다
SETTLE_STABLE = 2          # 같은 신호가 연속 이만큼 나오면 정착으로 본다
SETTLE_IMG_GRACE = 4       # 텍스트·높이는 멈췄는데 이미지가 안 끝났을 때 더 주는 폴링 횟수

# 지연 로딩(lazy) 유발 — 스크롤해야 뜨는 하단 로고·게재사항 이미지가 흔하다.
LAZY_SCROLL_STEPS = 8
IMG_WAIT_MS = 5_000        # 이미지·웹폰트 로딩 최대 대기
IMG_WAIT_SHOT_MS = 2_500   # 촬영 직전 재확인용(짧게)

# 리다이렉트 경쟁으로 goto 가 중단됐을 때의 오류 표지.
# 사이트가 http→https 또는 JS 로 리다이렉트하는 중이면 Playwright 가 "interrupted by another
# navigation" 예외를 던진다. 이건 **사이트 실패가 아니다** — 다시 시도하면 대개 성공한다.
# pilot4 에서 탐색실패 9건 중 4건(30874·30875·30878·30879)이 이것이었고, 30879 는
# pilot3 에서 정상 캡처됐던 사이트다. 재시도 없이 접속 불가로 찍으면 오판이다.
NAV_RETRY_MARKS = ("interrupted by another navigation", "ERR_ABORTED",
                   "Navigation failed because page was closed")
NAV_RETRIES = 2

# 렌더러 크래시. **사이트 실패가 아니라 우리 브라우저가 죽은 것**이다.
#
# 2026-09-03 실측: 99건 실행에서 14건이 탐색실패였는데 12건이 `Page crashed` 였고,
# 전부 접속은 가능한 매체였다. 연속된 id(1170~1173…)에서 몰려 난 것을 보면 한 페이지를
# 수십 건 재사용하며 메모리가 새는 것이다. 13,574건에서는 훨씬 심해진다.
# 대응 두 가지: ① 일정 건수마다 컨텍스트를 새로 만든다 ② 크래시가 나면 새 페이지로 1회 재시도.
CRASH_MARKS = ("Page crashed", "Target crashed", "Target closed",
               "Browser closed", "has been closed")
RECYCLE_EVERY = 25          # 이 건수마다 컨텍스트·페이지를 새로 만든다

# 느린 사이트: domcontentloaded 가 제한 시간 안에 오지 않는다.
#
# 이건 **사이트 실패가 아니다.** 무거운 이미지·광고 스크립트가 DOMContentLoaded 를 붙잡고
# 있을 뿐이고, 그 시점에 DOM 은 대개 이미 그려져 있다. 2026-09-02 실측에서 횡성TV(1009)가
# requests 수집은 성공했는데 Playwright goto 만 25초 타임아웃으로 죽어 **증빙 스크린샷 없이
# 준수 8/8 이 확정**됐다. 행정 처분은 증빙으로 다투므로 이건 그냥 넘길 수 없다.
#
# 3단 폴백으로 되살린다:
#   ① domcontentloaded @ NAV_TIMEOUT        — 정상 경로
#   ② 타임아웃 시 **현재 DOM 을 먼저 본다**   — 이미 본문이 있으면 그대로 진행 (재방문 없음)
#   ③ 그래도 비었으면 commit @ 긴 타임아웃    — 응답 첫 바이트만 기다리고 정착 루프에 맡긴다
NAV_TIMEOUT_SLOW = 60_000
NAV_SALVAGE_MIN_CHARS = 200      # 이만큼 있으면 '그려진 화면' 으로 본다

# 항목별 박스 색 — 검수자가 색으로 구분할 수 있게 고정한다
FIELD_COLORS = {
    "제호": "#e6194b", "제호 영문": "#f58231", "등록번호": "#3cb44b",
    "등록연월일": "#4363d8", "발행인": "#911eb4", "편집인": "#46f0f0",
    "발행연월일": "#f032e6", "발행소 주소": "#008080", "발행소 전화번호": "#9a6324",
    "창간일": "#808000",
}

# 항목별 화면 표기 라벨 — 같은 값이 여러 곳에 나올 때 '게재사항 표기' 쪽을 고르는 데 쓴다.
# 실제 사례(30895): 발행인 '송태은' 이 "발행인 : 송태은" 과 "청소년보호책임자 : 송태은" 두 곳에
# 있었고 라벨을 안 보면 뒤쪽을 잡는다. 값은 같아도 기록된 위치가 틀리면 재사용이 안 된다.
FIELD_LABELS = {
    "제호": ["제호", "신문명", "매체명"],
    "제호 영문": ["제호", "영문", "English"],
    "등록번호": ["등록번호", "등록 번호", "신문등록번호"],
    "등록연월일": ["등록일", "등록연월일", "등록 연월일", "등록년월일"],
    # '발행·편집인 : 홍길동' 처럼 두 항목을 한 라벨로 묶어 적는 사이트가 많다.
    # '발행인' 은 그 문자열의 부분문자열이 아니므로(발행·편집인) 라벨 근접 판정이 실패하고,
    # 같은 값이 '청소년보호책임자 : 홍길동' 에도 있으면 그쪽 위치를 기록했다(pilot4 30863·30865).
    # 값 자체는 맞았지만 셀렉터를 다음 회차에 재사용할 수 없다.
    "발행인": ["발행인", "발행·편집인", "발행/편집인", "발행,편집인", "발행 편집인", "발행겸편집인"],
    "편집인": ["편집인", "편집국장", "발행·편집인", "발행/편집인", "발행,편집인", "발행 편집인"],
    "발행연월일": ["발행일", "발행연월일", "발행 연월일", "최종수정", "업데이트"],
    "발행소 주소": ["주소", "발행소", "소재지", "본사"],
    "발행소 전화번호": ["전화", "Tel", "TEL", "대표전화", "연락처"],
    "창간일": ["창간", "창간일"],
}

# 페이지에 주석 오버레이를 주입한다. 값 텍스트를 Range 로 정확히 찾아 박스를 씌우고
# 항목명 라벨을 붙인다. 검수자가 "이 값이 화면 어디의 무엇을 근거로 잡혔는지" 를 바로 본다.
ANNOTATE_JS = r"""
(items) => {
  if (!document.body) return [];        // 파싱이 덜 된 저장본 — 호출자가 '열기실패' 로 다룬다
  const OLD = document.getElementById('__md_annot__');
  if (OLD) OLD.remove();
  const layer = document.createElement('div');
  layer.id = '__md_annot__';
  layer.style.cssText = 'position:absolute;left:0;top:0;width:0;height:0;z-index:2147483600;pointer-events:none;';
  document.body.appendChild(layer);

  // 값의 표기 변형 후보. 규칙은 날짜를 2014-03-05 로 정규화하지만 화면은
  // '2014년 3월 5일' / '2014.03.05' 로 적혀 있다 — 변형을 만들어 함께 찾는다.
  const variants = (v) => {
    const out = [v];
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec((v || '').trim());
    if (m) {
      const [_, y, mo, d] = m;
      const M = String(parseInt(mo, 10)), D = String(parseInt(d, 10));
      out.push(`${y}년 ${M}월 ${D}일`, `${y}년${M}월${D}일`,
               `${y}.${mo}.${d}`, `${y}.${M}.${D}`, `${y}-${mo}-${d}`,
               `${y}/${mo}/${d}`, `${y}년 ${mo}월 ${d}일`);
    }
    return out;
  };

  // 근거가 DOM 어디에 있었는지를 되짚을 수 있는 셀렉터를 만든다.
  // id 를 만나면 거기서 끊는다(가장 짧고 안정적). 없으면 태그+클래스+nth-of-type 으로 내려간다.
  const cssPath = (el) => {
    if (!el || el.nodeType !== 1) return '';
    const parts = []; let e = el, hitId = false;
    while (e && e.nodeType === 1 && e !== document.body && parts.length < 12) {
      if (e.id && /^[A-Za-z][\w:.-]*$/.test(e.id)) {
        parts.unshift('#' + e.id); hitId = true; break;
      }
      let s = e.tagName.toLowerCase();
      const cls = (typeof e.className === 'string' ? e.className : '').trim()
        .split(/\s+/).filter(c => /^[A-Za-z][\w-]*$/.test(c)).slice(0, 2);
      if (cls.length) s += '.' + cls.join('.');
      const par = e.parentElement;
      if (par) {
        const same = Array.from(par.children).filter(c => c.tagName === e.tagName);
        if (same.length > 1) s += ':nth-of-type(' + (same.indexOf(e) + 1) + ')';
      }
      parts.unshift(s); e = e.parentElement;
    }
    if (!hitId) parts.unshift('body');
    return parts.join(' > ');
  };

  // 페이지 어느 영역인가 — 신문법은 '메인화면 상·하단' 게재를 요구하므로 이 구분이 판정에 쓰인다
  const zoneOf = (top) => {
    const H = document.documentElement.scrollHeight || document.body.scrollHeight || 0;
    const vh = window.innerHeight || 900;
    if (top <= vh) return '상단';
    if (H && top >= H - vh * 1.5) return '하단';
    return '중간';
  };

  // labels: 이 항목의 표기 라벨들(예: 발행인 → ['발행인']). 같은 값이 화면에 여러 번 나올 때
  // **라벨 바로 뒤에 있는 것**을 고른다.
  //
  // 왜 필요한가: 시사픽(30895)에서 발행인 '송태은' 이 하단에 두 번 나온다 —
  //   p:nth-of-type(2) "… | 발행인 : 송태은 | …"        ← 이게 게재사항이다
  //   p:nth-of-type(3) "… | 청소년보호책임자 : 송태은 | …"  ← 아래쪽이라 예전에 이걸 잡았다
  // 값은 같아서 판정에는 영향이 없지만, 위치 기록이 틀리면 다음 회차에 그 셀렉터를 못 쓴다.
  // 공백을 접어서 비교한다. 화면 DOM 과 추출값은 공백이 자주 어긋난다 —
  // `이화1길 54.&nbsp; 주식회사 닥터유 ` (두 칸 + nbsp + 끝 공백) vs 추출값 `이화1길 54. 주식회사 닥터유`.
  // 예전에는 nodeValue.indexOf() 로 **정확 일치**만 봐서 1001 뉴스넷코리아의 주소·발행인이
  // 화면에 멀쩡히 있는데도 `css=None` 으로 남았다(2026-09-03 실측). 값 판정은 맞고 위치만
  // 못 찾은 것이라 위치지도·주석본에서만 조용히 비었다.
  //
  // 정규화 문자열과 원본 오프셋 대응표를 함께 만들어, 찾은 뒤 원본 좌표로 되돌린다.
  // 공백 판정은 **문자코드**로 한다. 이스케이프 문자열을 쓰면 이 JS 가 파이썬 문자열 안에
  // 들어 있어 이스케이프가 한 번 더 풀리고, JS 리터럴 안에 진짜 탭·개행이 박혀
  // `SyntaxError: Invalid or unexpected token` 이 난다(2026-09-03 실측 — 주석본 0/75 전멸).
  //   32=space  9~13=탭·개행류  160=nbsp  8203=zero-width  12288=전각공백
  const isSpCode = (cc) => (cc === 32 || (cc >= 9 && cc <= 13)
                            || cc === 160 || cc === 8203 || cc === 12288);
  const foldWS = (v) => {
    let text = '', map = [], prevSp = false;
    for (let i = 0; i < v.length; i++) {
      if (isSpCode(v.charCodeAt(i))) {
        if (prevSp) continue;
        text += ' '; map.push(i); prevSp = true;
      } else {
        text += v[i]; map.push(i); prevSp = false;
      }
    }
    return {text: text, map: map};
  };

  // 공백을 **전부 제거**한 비교. 값에서만 공백이 빠진 경우를 잡는다 —
  // 등록번호는 추출 단계에서 `re.sub(r"\s+","")` 로 공백을 지우므로 값은 `강원아00253` 인데
  // 화면은 `등록번호 : 강원 아 00253` 이다. 접기(foldWS)만으로는 안 맞는다(1106 실측).
  const stripWS = (v) => {
    let text = '', map = [];
    for (let i = 0; i < v.length; i++) {
      if (!isSpCode(v.charCodeAt(i))) { text += v[i]; map.push(i); }
    }
    return {text: text, map: map};
  };

  const findRange = (needle, labels) => {
    if (!needle || needle.length < 2) return null;
    // ① 공백 접기로 먼저 찾고 ② 못 찾으면 공백을 완전히 빼고 다시 찾는다.
    for (const norm of [foldWS, stripWS]) {
      const nd = norm(needle).text.trim();
      if (nd.length < 2) continue;
      const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
        acceptNode: (n) => {
          if (!n.nodeValue) return NodeFilter.FILTER_REJECT;
          if (norm(n.nodeValue).text.indexOf(nd) === -1) return NodeFilter.FILTER_REJECT;
          const p = n.parentElement;
          if (!p) return NodeFilter.FILTER_REJECT;
          if (p.closest('#__md_annot__')) return NodeFilter.FILTER_REJECT;
          const st = getComputedStyle(p);
          if (st.display === 'none' || st.visibility === 'hidden') return NodeFilter.FILTER_REJECT;
          return NodeFilter.FILTER_ACCEPT;
        }
      });
      let best = null, bestScore = -1, bestNear = false;
      let n;
      while ((n = w.nextNode())) {
        // 같은 텍스트노드 안의 모든 출현을 본다 — 하단 게재사항은 한 노드에 몰려 있다
        const f = norm(n.nodeValue), fv = f.text;
        for (let i = fv.indexOf(nd); i !== -1; i = fv.indexOf(nd, i + 1)) {
          const s0 = f.map[i];                       // 정규화 위치 → 원본 오프셋
          const e0 = f.map[i + nd.length - 1] + 1;
          const r = document.createRange();
          try { r.setStart(n, s0); r.setEnd(n, e0); } catch (e) { continue; }
          const rect = r.getBoundingClientRect();
          if (rect.width < 2 || rect.height < 2) continue;
          const top = rect.top + window.scrollY;
          // 값 앞쪽 40자 안에 라벨이 있으면 그게 게재사항 표기다 (라벨도 같은 방식으로 정규화)
          const before = fv.slice(Math.max(0, i - 40), i);
          const near = (labels || []).some(L => {
            if (!L) return false;
            return before.indexOf(norm(L).text.trim()) !== -1;
          });
          // 라벨 근접이 최우선, 그 다음 아래쪽(하단 게재사항 우선)
          const score = (near ? 1e9 : 0) + top;
          if (score > bestScore) { bestScore = score; best = r; bestNear = near; }
        }
      }
      if (best) return {range: best, near: bestNear};
    }
    return null;
  };

  // 라벨 충돌 회피 — 하단 게재사항은 한 줄에 밀집해서 그냥 그리면 라벨이 서로 덮는다.
  const placed = [];
  const LH = 16;
  const hits = (a, b) => !(a.r <= b.l || a.l >= b.r || a.b <= b.t || a.t >= b.b);
  const freeSpot = (l, w, baseTop) => {
    for (let k = 1; k <= 14; k++) {
      const t = baseTop - k * LH;                 // 위로 쌓는다
      const cand = {l: l, r: l + w, t: t, b: t + LH};
      if (t > 0 && !placed.some(p => hits(cand, p))) return cand;
    }
    for (let k = 1; k <= 14; k++) {
      const t = baseTop + k * LH;                 // 위가 막히면 아래로
      const cand = {l: l, r: l + w, t: t, b: t + LH};
      if (!placed.some(p => hits(cand, p))) return cand;
    }
    return {l: l, r: l + w, t: baseTop - LH, b: baseTop};
  };

  const out = [];
  for (const it of items) {
    let hit = null, via = 'value', matched = '';
    for (const cand of variants(it.value)) {      // 값(및 표기 변형)으로 먼저
      hit = findRange(cand, it.labels);
      if (hit) { matched = cand; break; }
    }
    if (!hit && it.evidence) {                    // 없으면 근거 조각으로
      matched = it.evidence.trim().slice(0, 40);
      hit = findRange(matched, it.labels);
      via = 'evidence';
    }
    const r = hit ? hit.range : null;
    if (!r) {
      out.push({field: it.field, ok: false,
                value: it.value, tried: variants(it.value).length,
                reason: '화면 텍스트에서 값·근거를 찾지 못했다 (이미지 표기이거나 표기 변형 미대응)'});
      continue;
    }
    const rect = r.getBoundingClientRect();
    const L = rect.left + window.scrollX, T = rect.top + window.scrollY;

    // ── 위치 기록 ── 나중에 이 셀렉터만 보고 바로 다시 꺼낼 수 있어야 한다
    const host = r.startContainer.parentElement;
    const hostText = ((host && host.innerText) || '').replace(/\s+/g, ' ').trim();
    const loc = {
      css: cssPath(host),
      tag: host ? host.tagName.toLowerCase() : '',
      id: (host && host.id) || '',
      cls: (host && typeof host.className === 'string' ? host.className.trim() : '').slice(0, 120),
      영역: zoneOf(T),
      좌표: {x: Math.round(L), y: Math.round(T),
            w: Math.round(rect.width), h: Math.round(rect.height)},
      matched: matched,
      // 텍스트노드 안에서 값이 시작하는 위치 — 같은 값이 여러 번 나올 때 어느 것인지 구분한다
      offset: r.startOffset,
      문맥: hostText.slice(0, 220),
      via: via,
      // 라벨(예: '발행인 :') 바로 뒤에서 찾았는가. false 면 같은 값이 다른 문맥에서 잡혔을
      // 수 있다 — 셀렉터를 다음 회차에 재사용하기 전에 사람이 봐야 한다.
      라벨근접: !!(hit && hit.near),
    };

    const box = document.createElement('div');
    box.style.cssText =
      `position:absolute;left:${L - 3}px;top:${T - 2}px;width:${rect.width + 6}px;` +
      `height:${rect.height + 4}px;border:2px solid ${it.color};` +
      `background:${it.color}22;border-radius:3px;box-sizing:border-box;`;
    layer.appendChild(box);

    // 인라인 라벨 대신 **번호 배지**를 쓴다. 하단 게재사항은 한 줄에 밀집해서
    // 텍스트 라벨을 붙이면 반드시 본문을 가린다. 번호↔항목명은 범례가 알려준다.
    const n = out.filter(o => o.ok).length + 1;
    const badge = document.createElement('div');
    badge.textContent = String(n);
    const bT = T - 9 > 0 ? T - 9 : T + rect.height + 1;
    badge.style.cssText =
      `position:absolute;left:${L - 11}px;top:${bT}px;width:15px;height:15px;` +
      `background:${it.color};color:#fff;border:1px solid #fff;border-radius:50%;` +
      `font:800 10px/15px "Malgun Gothic",sans-serif;text-align:center;` +
      `box-shadow:0 1px 3px rgba(0,0,0,.45);`;
    layer.appendChild(badge);
    out.push({field: it.field, ok: true, via: via, top: Math.round(T), no: n,
              value: it.value, 위치: loc});
  }

  // 범례 — position:fixed 로 두면 상단·하단 캡처 모두에 들어온다
  const done = out.filter(o => o.ok);
  if (done.length) {
    const lg = document.createElement('div');
    lg.style.cssText =
      'position:fixed;right:10px;bottom:10px;max-width:430px;background:rgba(255,255,255,.97);' +
      'border:2px solid #222;border-radius:6px;padding:8px 10px;z-index:2147483647;' +
      'font:600 11px/1.65 "Malgun Gothic",sans-serif;color:#111;' +
      'box-shadow:0 2px 10px rgba(0,0,0,.3);';
    let html = '<div style="font-size:12px;font-weight:800;margin-bottom:4px;' +
               'border-bottom:1px solid #ccc;padding-bottom:3px">' +
               '필요적 게재사항 — 추출 근거 표시</div>';
    for (const o of done) {
      const it = items.find(x => x.field === o.field);
      html += `<div><span style="display:inline-block;width:15px;height:15px;background:${it.color};` +
              `color:#fff;border-radius:50%;font:800 10px/15px sans-serif;text-align:center;` +
              `margin-right:6px;vertical-align:middle">${o.no}</span>${it.label}` +
              (o.via === 'evidence' ? ' <span style="color:#888">(근거문구 기준)</span>' : '') + '</div>';
    }
    const miss = out.filter(o => !o.ok).map(o => o.field);
    if (miss.length) {
      html += `<div style="margin-top:4px;color:#a00;border-top:1px solid #ddd;padding-top:3px">` +
              `화면에서 위치 못 찾음: ${miss.join(', ')}</div>`;
    }
    lg.innerHTML = html;
    layer.appendChild(lg);
  }
  return out;
}
"""


# 최신 기사(가장 지배적인 기사) 링크를 라이브 DOM 에서 고른다.
#
# 왜 여기서 하는가: 발행연월일 = 최신 기사 업데이트 날짜이고, 첫 화면에 날짜가 없으면 기사
# 상세로 들어가 확인해야 한다(사용자 명세). 그 기사 페이지를 verify_pubdate 가 따로 fetch 하면
# 같은 매체를 또 방문한다 — pilot3 에서 건당 2.1회(8천건 16,615회) 방문했고 그 중 7건이
# 이 재fetch 였다. 캡처 세션에서 미리 받아두면 하류는 디스크만 읽는다.
#
# 왜 정규식이 아니라 in-page JS 인가: 렌더된 화면의 글자 크기·굵기·위치를 봐야 '지배적'을
# 판단할 수 있다. HTML 문자열에는 그 정보가 없다.
ARTICLE_PICK_JS = r"""
(opt) => {
  const hostHint = (opt && opt.hostHint) || '';
  const relaxed  = !!(opt && opt.relaxed);
  // 기사 상세 URL 패턴 (국내 언론사 CMS 관행: 인터넷신문 CMS·자체 구축 모두 포함).
  // `?p=1234` 는 워드프레스 기본 퍼머링크다 — newspro.kr 등 호스팅형 매체가 그대로 쓴다.
  const PAT = /(articleView|article_view|view\.html|\/article\/|\/news\/|idxno=|wr_id=|[?&]no=|\/view\/|contentid=|aid=|[?&]p=\d{2,}|\/\d{4}\/\d{2}\/)/i;
  // 기사가 아닌 워드프레스 링크 — 작성자·태그·분류 목록을 기사로 잡으면 발행일이 엉뚱해진다
  const WP_NOT_POST = /[?&](author|cat|tag|s|paged|m)=/i;
  // 기사가 아닌 링크 — 이런 걸 최신 기사로 잡으면 발행일이 엉뚱해진다
  const SKIP = /(login|join|member|privacy|agreement|rss|sitemap|search|about|company|advertis|mailto:|javascript:|\.pdf$|\.jpg$|\.png$)/i;

  // 등록가능 도메인 기준으로 같은 매체인지 본다.
  // iframe 안에서는 location.hostname 이 iframe 의 호스트라, 본 사이트 링크가 '외부 링크' 로
  // 걸러진다. www/m 서브도메인 차이도 같은 이유로 놓친다. co.kr·or.kr 같은 2단계 SLD 를
  // 고려해 마지막 2~3 라벨로 비교한다.
  const baseOf = (h) => {
    const p = String(h || '').toLowerCase().split('.').filter(Boolean);
    if (p.length <= 2) return p.join('.');
    const two = p.slice(-2).join('.');
    if (/^(co|or|go|ne|re|pe|ac|hs|ms|es|sc|kg)\.(kr)$/.test(two)) return p.slice(-3).join('.');
    return two;
  };
  const want = baseOf(hostHint || location.hostname);
  // 호스팅형 언론사 CMS 도메인. 매체 도메인과 기사 도메인이 다른 구조를 허용한다.
  const HOSTED = /(newspro\.kr|tistory\.com|blog\.naver\.com|wordpress\.com|wixsite\.com|modoo\.at|cafe24\.com|imweb\.me|creatorlink\.net|godohosting\.com)$/i;

  const out = [];
  for (const a of document.querySelectorAll('a[href]')) {
    const href = a.href || '';
    if (!/^https?:/i.test(href)) continue;
    // 같은 매체인가. 도메인이 달라도 **제호가 호스트명에 들어 있으면** 같은 매체로 본다 —
    // 호스팅형 매체는 기사가 다른 도메인에 있다(실측: riumnews.com → reyoomnews.newspro.kr,
    // piladay.co.kr → piladay.newspro.kr). 도메인 일치만 보면 기사를 하나도 못 찾는다.
    const 같은도메인 = !want || baseOf(a.hostname) === want;
    const 호스팅형 = !같은도메인 && HOSTED.test(a.hostname) && PAT.test(href);
    if (!같은도메인 && !호스팅형) continue;
    if (SKIP.test(href)) continue;
    if (WP_NOT_POST.test(href)) continue;      // 작성자·태그·분류 목록은 기사가 아니다
    // 자기 자신 링크 제외. ★ 쿼리스트링은 남긴다 — 예전에는 `?` 이후를 잘라 비교해서,
    // 워드프레스 기본 퍼머링크(`/?p=2168`)를 쓰는 사이트의 **모든 기사가 홈페이지와 같아져**
    // 전부 걸러졌다. 실측: reyoomnews.newspro.kr·piladay.newspro.kr 에서 후보 0개.
    const norm = (u) => String(u || '').replace(/#.*$/, '').replace(/\/$/, '');
    if (norm(href) === norm(location.href)) continue;
    // 제목 출처: 링크 텍스트 → title → aria-label → 안쪽 이미지 alt.
    // 썸네일만 있는 카드형 목록은 innerText 가 비어 있어 예전에는 전부 걸러졌다.
    let txt = (a.innerText || '').trim().replace(/\s+/g, ' ');
    let via = 'text';
    if (!txt) {
      const img = a.querySelector('img[alt]');
      txt = (a.getAttribute('title') || a.getAttribute('aria-label')
             || (img && img.getAttribute('alt')) || '').trim().replace(/\s+/g, ' ');
      via = txt ? 'attr' : '';
    }
    const minLen = relaxed ? 4 : 8;
    if (txt.length < minLen) continue;
    const r = a.getBoundingClientRect();
    const minW = relaxed ? 10 : 40, minH = relaxed ? 4 : 8;
    if (r.width < minW || r.height < minH) continue;
    const cs = getComputedStyle(a);
    if (cs.visibility === 'hidden' || cs.display === 'none') continue;
    const fs = parseFloat(cs.fontSize) || 12;
    const fw = parseInt(cs.fontWeight) || 400;
    // 지배성 점수: 글자가 크고 굵고 제목이 길고 위쪽에 있고 기사 URL 패턴이면 높다
    let score = fs * 2.0
              + (fw >= 600 ? 8 : 0)
              + Math.min(txt.length, 60) * 0.15
              + (PAT.test(href) ? 25 : 0)
              + (r.top + window.scrollY < 1400 ? 6 : 0)
              - (via === 'attr' ? 3 : 0);          // 텍스트 제목을 약간 우대
    out.push({href: href, text: txt.slice(0, 140),
              score: Math.round(score * 10) / 10,
              fontSize: fs, weight: fw, titleVia: via,
              top: Math.round(r.top + window.scrollY),
              patterned: PAT.test(href)});
  }
  out.sort((a, b) => b.score - a.score);
  // 같은 URL 중복 제거
  const seen = new Set(), uniq = [];
  for (const o of out) { if (seen.has(o.href)) continue; seen.add(o.href); uniq.push(o); }
  return uniq.slice(0, 5);
}
"""


# ── 건별 자체 시간 상한 (ISS-070) ───────────────────────────────────────────
# `about:blank` 필터로 알려진 원인은 막았지만, 실제 URL 을 가진 광고 iframe 도 실행
# 컨텍스트가 안 생길 수 있고 `frame.evaluate`·`page.content` 는 상한을 받지 못한다.
# 파이썬은 블록된 greenlet 을 깨우지 못하므로 **프로세스를 스스로 끝내는** 수밖에 없다.
#
# 부모(run_capture_parallel)의 워치독은 8분 무진행을 봐야 개입하는데, 그동안 그 샤드는
# 통째로 놀고 멈춘 매체는 기사 페이지 없이 넘어간다. 여기서 90초에 끊으면 손실이 1/5 로
# 줄고, 어느 건에서 끊겼는지 자식이 직접 기록해 부모가 정확히 그 건만 제외하고 재기동한다.
ITEM_DEADLINE = int(os.environ.get("MD_ITEM_DEADLINE", "90"))
_현재건 = {"id": "", "i": 0, "n": 0, "t": 0.0}


def 건별_시작(rid: str, i: int, n: int) -> None:
    _현재건.update({"id": rid, "i": i, "n": n, "t": time.time()})


def _건별_감시() -> None:
    while True:
        time.sleep(5)
        t = _현재건["t"]
        if not t or time.time() - t < ITEM_DEADLINE:
            continue
        경과 = time.time() - t
        print(f"[{_현재건['i']}/{_현재건['n']}] {_현재건['id']} 건별상한초과 "
              f"({경과:.0f}s > {ITEM_DEADLINE}s) — 프로세스를 끝낸다. "
              f"부모가 이 건을 빼고 재기동한다", file=sys.stderr, flush=True)
        os._exit(EXIT_ITEM_STALLED)


EXIT_ITEM_STALLED = 42


def 평가가능(fr, page) -> bool:
    """이 프레임에 evaluate 를 걸어도 되는가.

    ★ 무한 대기의 실제 원인이다 (ISS-070). `frame.evaluate()` 에는 **timeout 인자가 없고**
    `set_default_timeout()` 도 적용되지 않는다. 광고·트래킹 스크립트가 만드는
    `about:blank` iframe 은 문서가 실제로 로드되지 않아 **JS 실행 컨텍스트가 영영 생기지
    않는데**, Playwright 는 그 컨텍스트를 무한히 기다린다.

    실측(6203 luxuryedu.kr): 메인 프레임 evaluate 0.0초, `about:blank` 프레임 110초 뒤에도
    반환 없음 — 브라우저를 닫아야만 끝났다. 같은 매체가 r1·r3·r4 세 번 모두 이 지점에서
    걸렸다. 사이트는 정상이다(2초에 로드, 본문 455자). 문제는 우리 쪽이었다.

    기사 링크가 `about:blank` 안에 있을 수는 없으므로(문서 자체가 없다) 걸러도 잃는 게 없다.
    detached 프레임도 같은 이유로 뺀다.
    """
    if fr is page.main_frame:
        return True
    u = (fr.url or "").strip().lower()
    if not u or u in ("about:blank", "about:srcdoc") or u.startswith("javascript:"):
        return False
    try:
        if fr.is_detached():
            return False
    except Exception:  # noqa: BLE001
        return False
    return True


def pick_article(page, host: str) -> tuple[list[dict], dict]:
    """메인 프레임 + 모든 iframe 에서 기사 링크 후보를 모은다.

    왜 iframe 을 봐야 하는가: pilot4 에서 5건(30877·30878·30901·30904·30910)이
    '기사 링크 후보 없음' 이었다. 정착 대기를 9초로 늘려도 안 잡혔다 — 기사 목록이 iframe
    안에 있거나 썸네일만 있는 카드형이어서, 메인 프레임의 innerText 기준 탐색으로는 보이지
    않았다. 발행연월일(= 최신 기사 업데이트 날짜)의 근거가 여기서 끊긴다.

    2단계로 찾는다: 엄격 기준으로 먼저 훑고, 한 건도 없으면 완화 기준으로 다시 훑는다.
    완화를 처음부터 쓰면 네비게이션 링크가 기사로 잡힌다.
    """
    진단: dict = {"프레임수": 0, "프레임별": []}
    for relaxed in (False, True):
        모음: list[dict] = []
        frames = [f for f in page.frames if 평가가능(f, page)]
        진단["프레임수"] = len(page.frames)
        진단["평가대상"] = len(frames)
        for fr in frames:
            메인 = fr is page.main_frame
            try:
                cands = fr.evaluate(ARTICLE_PICK_JS,
                                    {"hostHint": host, "relaxed": relaxed}) or []
            except Exception as exc:  # noqa: BLE001
                if not relaxed:
                    진단["프레임별"].append(
                        {"frame": "(main)" if 메인 else (fr.url or "")[:80],
                         "오류": f"{exc.__class__.__name__}: {str(exc)[:70]}"})
                continue
            라벨 = "(main)" if 메인 else (fr.url or "about:blank")[:80]
            if not relaxed:
                진단["프레임별"].append({"frame": 라벨, "후보": len(cands)})
            for c in cands:
                c["frame"] = 라벨
                모음.append(c)
        모음.sort(key=lambda c: -(c.get("score") or 0))
        본, uniq = set(), []
        for c in 모음:
            if c["href"] in 본:
                continue
            본.add(c["href"])
            uniq.append(c)
        if uniq:
            진단["완화기준_사용"] = relaxed
            return uniq[:5], 진단
    진단["완화기준_사용"] = True
    return [], 진단


# 캡처 시각 스탬프 — 화면 오른쪽 위에 고정으로 얹는다.
#
# 왜 필요한가: 스크린샷은 행정 처분의 증빙이다. "언제 찍은 화면인가" 가 화면 자체에 없으면
# 파일 수정시각(복사·이동으로 바뀐다)에 의존해야 하고, 매체가 나중에 표기를 고쳤을 때
# 점검 시점을 다투게 된다. 화면에 박아두면 그 다툼이 사라진다.
#
# position:fixed 라서 상단·하단 캡처 모두에 들어온다.
# ★ 반드시 렌더HTML 을 저장한 **뒤에** 주입한다 — 순서를 어기면 ISS-031(주석 오염)이 재발한다.
STAMP_JS = r"""
(lines) => {
  if (!document.body) return false;
  const OLD = document.getElementById('__md_stamp__');
  if (OLD) OLD.remove();
  const el = document.createElement('div');
  el.id = '__md_stamp__';
  el.style.cssText =
    'position:fixed;top:8px;right:8px;z-index:2147483647;' +
    'background:rgba(17,17,17,.93);color:#fff;padding:5px 9px;border-radius:5px;' +
    'font:700 12px/1.55 "Malgun Gothic",Consolas,monospace;letter-spacing:.2px;' +
    'box-shadow:0 2px 9px rgba(0,0,0,.5);pointer-events:none;text-align:right;' +
    'border:1px solid rgba(255,255,255,.4);white-space:pre;';
  el.textContent = lines.join('\n');
  document.body.appendChild(el);
  return true;
}
"""


# 화면(뷰포트) 근처에서 **실제로 뜰 수 있는** 이미지만 세는 식.
#
# 전체 document.images 를 기다리면 절대 끝나지 않는다 — 2026-09-02 실측에서 횡성TV 가
# 미완 222개였는데, 전부 화면 밖 지연 로딩 자리표시(src 없음)라 영원히 complete 가 되지
# 않는다. 증빙에 필요한 건 **지금 찍을 화면에 들어오는 이미지**뿐이다.
_PENDING_IMGS_JS = """() => {
    const vh = window.innerHeight || 900, margin = vh;
    let pending = 0;
    try {
        for (const i of document.images) {
            if (!i.currentSrc && !i.getAttribute('src')) continue;   // lazy 자리표시
            const r = i.getBoundingClientRect();
            if (r.width === 0 || r.height === 0) continue;           // 숨김·0크기
            if (r.bottom < -margin || r.top > vh + margin) continue; // 화면에서 먼 것
            if (!i.complete) pending++;
        }
    } catch (e) {}
    return pending;
}"""


# 저장본의 charset 선언을 실제 바이트와 맞춘다.
#
# 왜 필요한가 (2026-09-03 실측 — 1103 강원매일)
# `page.content()` 는 이미 디코딩된 str 이라 우리는 **UTF-8 바이트**로 저장한다. 그런데
# 원문서의 `<meta ... charset=euc-kr>` 선언이 그대로 남아 있어, 저장본을 `file://` 로 다시
# 열면 브라우저가 그 선언을 믿고 UTF-8 바이트를 EUC-KR 로 해석한다 → 한글이 전부 깨진다.
#   title `강원매일` → `媛뺤썝留ㅼ씪`
# 결과: ① 주석 스크린샷의 한글이 깨져 증빙 구실을 못 하고 ② DOM 값 탐색이 실패해
# 한글 항목(제호·발행인·주소)만 `css=None` 이 된다. 숫자 항목은 ASCII 라 살아남아
# "일부만 되는" 증상으로 보인다.
#
# ISS-031(주석 주입 전 저장) 원칙과 충돌하지 않는다 — 내용을 더하는 게 아니라 **틀린
# 선언을 사실에 맞추는** 것이고, 저장 직전 한 번만 한다.
_CHARSET_META = re.compile(
    r"<meta[^>]*?charset\s*=\s*[\"']?\s*([\w-]+)[^>]*>", re.I)
_HEAD_OPEN = re.compile(r"<head[^>]*>", re.I)


def force_utf8_meta(html: str) -> tuple[str, str]:
    """저장본의 charset 선언을 utf-8 로 맞춘다 → (html, 바꾼 원래 선언값)."""
    m = _CHARSET_META.search(html or "")
    if m:
        원 = (m.group(1) or "").lower()
        if 원 in ("utf-8", "utf8"):
            return html, ""
        return html[:m.start()] + '<meta charset="utf-8">' + html[m.end():], 원
    h = _HEAD_OPEN.search(html or "")
    if h:
        return html[:h.end()] + '<meta charset="utf-8">' + html[h.end():], "(선언없음)"
    return html, ""


def wait_images(page, timeout_ms: int = IMG_WAIT_MS) -> dict:
    """지금 찍을 화면의 이미지가 다 뜰 때까지 기다린다.

    제호 로고·게재사항이 이미지인 매체가 많다. 덜 뜬 화면을 찍으면 증빙 구실을 못 한다.
    웹폰트는 **막지 않는다** — `document.fonts.status` 가 'loading' 에 머무는 사이트가 있어
    이걸 조건에 넣으면 미완 이미지 0개인데도 매번 타임아웃한다(1004 실측). 짧게만 기다린다.
    """
    out: dict = {}
    try:
        page.wait_for_function(f"() => ({_PENDING_IMGS_JS})() === 0", timeout=timeout_ms)
        out["이미지대기"] = "완료"
    except Exception:  # noqa: BLE001
        try:
            남음 = page.evaluate(_PENDING_IMGS_JS)
        except Exception:  # noqa: BLE001
            남음 = "?"
        out["이미지대기"] = f"미완({timeout_ms}ms 초과, 화면 내 미완 이미지 {남음}개)"
    # 웹폰트는 있으면 좋고 없어도 진행 — 늦게 뜨면 글자가 밀려 주석 좌표가 어긋난다
    try:
        page.wait_for_function(
            "() => !document.fonts || document.fonts.status === 'loaded'", timeout=1_500)
    except Exception:  # noqa: BLE001
        out["폰트대기"] = "미완(1.5초 초과) — 진행"
    return out


def prime_lazy_load(page) -> dict:
    """페이지를 한 번 훑어 내려 지연 로딩 이미지를 끌어낸 뒤 맨 위로 돌아온다."""
    try:
        page.evaluate("""async (steps) => {
            const h = document.body ? document.body.scrollHeight : 0;
            for (let i = 1; i <= steps; i++) {
                window.scrollTo(0, Math.round(h * i / steps));
                await new Promise(r => setTimeout(r, 120));
            }
            window.scrollTo(0, 0);
            await new Promise(r => setTimeout(r, 150));
        }""", LAZY_SCROLL_STEPS)
        return {"지연로딩_유발": f"{LAZY_SCROLL_STEPS}단계 스크롤"}
    except Exception as exc:  # noqa: BLE001
        return {"지연로딩_유발": f"실패 {exc.__class__.__name__}"}


def goto_settled(page, url: str) -> dict:
    """리다이렉트 경쟁을 재시도하며 이동하고, 본문이 자라기를 멈출 때까지 기다린다.

    돌려주는 dict 는 그대로 audit 상세에 들어간다 — "무엇을 얼마나 기다렸는가" 가 남아야
    나중에 '덜 로딩된 화면으로 판정했다' 는 의심을 데이터로 답할 수 있다.
    """
    def _본문길이() -> int:
        try:
            return page.evaluate(
                "document.body ? (document.body.innerText || '').length : 0") or 0
        except Exception:  # noqa: BLE001
            return 0

    info: dict = {"이동_시도": []}
    resp = None
    성공 = False
    for 시도 in range(1, NAV_RETRIES + 2):
        try:
            resp = page.goto(url, timeout=NAV_TIMEOUT, wait_until="domcontentloaded")
            info["이동_시도"].append(f"{시도}회: 성공")
            성공 = True
            break
        except Exception as exc:  # noqa: BLE001
            msg = f"{exc.__class__.__name__}: {str(exc)[:160]}"
            info["이동_시도"].append(f"{시도}회: {msg[:110]}")
            느림 = "Timeout" in str(exc) or "timeout" in str(exc)
            if 느림:
                # ② 타임아웃이어도 DOM 은 이미 찼을 수 있다. 재방문 없이 그것부터 본다.
                n = _본문길이()
                if n >= NAV_SALVAGE_MIN_CHARS:
                    info["이동_시도"].append(f"{시도}회b: 타임아웃이나 DOM 본문 {n}자 확보 — 그대로 진행")
                    info["느린사이트_구제"] = f"domcontentloaded 미도달, DOM {n}자로 캡처"
                    성공 = True
                    break
                # ③ commit 은 응답 첫 바이트만 기다린다. 이후는 정착 루프가 담당한다.
                try:
                    resp = page.goto(url, timeout=NAV_TIMEOUT_SLOW, wait_until="commit")
                    info["이동_시도"].append(f"{시도}회c: commit 성공(느린 사이트)")
                    info["느린사이트_구제"] = "commit 폴백"
                    성공 = True
                    break
                except Exception as e2:  # noqa: BLE001
                    info["이동_시도"].append(
                        f"{시도}회c: commit 실패 {e2.__class__.__name__}: {str(e2)[:70]}")
            if 시도 > NAV_RETRIES or not (느림 or any(m in str(exc) for m in NAV_RETRY_MARKS)):
                info["이동_실패"] = msg
                return info
            # 리다이렉트가 정착할 틈을 준다
            try:
                page.wait_for_timeout(1_200)
            except Exception:  # noqa: BLE001
                pass
    if not 성공:
        n = _본문길이()
        if n < NAV_SALVAGE_MIN_CHARS:
            info.setdefault("이동_실패", f"재시도 {NAV_RETRIES + 1}회 모두 실패 (DOM 본문 {n}자)")
            return info
        info["느린사이트_구제"] = f"goto 실패했으나 DOM 본문 {n}자 존재 — 캡처 계속"
    info["HTTP상태"] = resp.status if resp else None

    # 네트워크가 조용해지길 기다린다(있으면 좋고, 없어도 계속 간다).
    # 광고·트래킹 소켓이 계속 열려 있는 사이트에서는 networkidle 이 절대 오지 않으므로
    # 짧게 끊고 텍스트 안정화 판정으로 넘어간다.
    try:
        page.wait_for_load_state("networkidle", timeout=4_000)
        info["networkidle"] = "도달"
    except Exception:  # noqa: BLE001
        info["networkidle"] = "미도달(4초 초과) — 텍스트 안정화로 판단"

    # ── 렌더링 완료 판정 ────────────────────────────────────────────────────
    #
    # 예전에는 innerText 길이 하나만 봤다. 그것만으로는 **그림이 덜 뜬 화면**을 정착으로
    # 오판한다 — 제호 로고나 게재사항이 이미지인 매체가 많고, 그 화면이 곧 증빙이다.
    # 그래서 네 신호를 함께 본다: 본문 길이 · 문서 높이 · 미완 이미지 수 · readyState.
    # 텍스트·높이가 멈췄는데 이미지만 남았으면 조금 더 기다렸다가(SETTLE_IMG_GRACE) 넘어간다.
    이전, 안정, 누적, 유예 = None, 0, 0, 0
    추이, 마지막 = [], {}
    try:
        page.wait_for_timeout(SETTLE_MIN_MS)
        누적 += SETTLE_MIN_MS
        while 누적 < SETTLE_MAX_MS:
            sig = page.evaluate(
                "() => { const b = document.body; return {"
                "t: b ? (b.innerText || '').length : 0,"
                "h: b ? b.scrollHeight : 0,"
                f"img: ({_PENDING_IMGS_JS})(),"
                "ready: document.readyState}; }") or {}
            마지막 = sig
            추이.append(f"{sig.get('t', 0)}자/{sig.get('h', 0)}px/img{sig.get('img', 0)}")
            현재 = (sig.get("t"), sig.get("h"))
            멈춤 = (현재 == 이전) and (sig.get("t") or 0) > 0
            # readyState 는 'complete' 를 요구하지 않는다 — 지연 로딩 사이트는 계속
            # 'interactive' 에 머문다. 화면 내 이미지가 다 떴으면 찍어도 된다.
            그림완료 = (sig.get("img") or 0) == 0 and sig.get("ready") in ("complete", "interactive")
            if 멈춤:
                안정 += 1
                if 안정 >= SETTLE_STABLE:
                    if 그림완료:
                        break
                    유예 += 1
                    if 유예 >= SETTLE_IMG_GRACE:
                        break
            else:
                안정 = 0
            이전 = 현재
            page.wait_for_timeout(SETTLE_STEP_MS)
            누적 += SETTLE_STEP_MS
    except Exception as exc:  # noqa: BLE001
        info["정착_예외"] = f"{exc.__class__.__name__}: {str(exc)[:90]}"
    info["정착_대기_ms"] = 누적
    info["정착_추이"] = 추이[:20]
    info["정착_완료"] = 안정 >= SETTLE_STABLE
    info["정착_readyState"] = 마지막.get("ready")
    info["정착_미완이미지"] = 마지막.get("img")

    # 지연 로딩을 끌어내고 이미지·웹폰트가 다 뜰 때까지 기다린 뒤에 넘긴다.
    info.update(prime_lazy_load(page))
    info.update(wait_images(page, IMG_WAIT_MS))
    if not info["정착_완료"]:
        info["정착_경고"] = (f"{SETTLE_MAX_MS}ms 안에 본문이 안정되지 않았다 "
                          f"(추이 {추이[-4:]}) — 화면이 덜 그려진 상태일 수 있다")
    return info


def stamp_lines(rec: dict, rid: str, *, 기준일: str = "", 주석: bool = False) -> list[str]:
    """스탬프에 넣을 줄. 시각이 첫 줄이고, 어느 매체의 증빙인지 함께 남긴다."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    제호 = str(rec.get("제호") or "").strip()
    라벨 = f"{rid} {제호}".strip()
    lines = [f"캡처 {now}"]
    if 라벨:
        lines.append(라벨[:28])
    if 기준일:
        lines.append(f"기준일 {기준일}")
    if 주석:
        lines.append("근거 표시본")
    return lines


def domain_of(url: str) -> str:
    host = urlparse(normalize_url(url)).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def flagged_ids(workdir: Path, base: date) -> set[str]:
    """extract CSV 에서 확인필요=Y 인 id 만 추린다."""
    out: set[str] = set()
    for name in (f"verify_{base.isoformat()}.csv", f"extract_{base.isoformat()}.csv"):
        p = workdir / "outputs" / name
        if not p.exists():
            continue
        with p.open(encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                if (row.get("확인필요") or "").strip().upper() == "Y":
                    out.add(str(row.get("언론사_id")))
        if out:
            break
    return out


def annot_items(workdir: Path, rid: str) -> list[dict]:
    """추출 결과 → 주석 대상 목록. 값이 있는 항목만.

    LLM 검토 결과가 있으면 그것을 쓴다 — 규칙이 근거 없이 낸 값(규칙기각된 항목)을
    화면에 표시하면 검수자를 오도한다.
    """
    def _load(p: Path) -> dict:
        if not p.exists():
            return {}
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    llm = _load(workdir / "work" / f"{rid}.llm.json").get("검증후_findings") or {}
    d = _load(workdir / "work" / f"{rid}.json")
    findings = llm or (d.get("findings") or {})
    items = []
    for f, v in findings.items():
        val = (v.get("값") or "").strip()
        if not val or val == "해당사항 없음":
            continue
        items.append({
            "field": f,
            "label": f"{f}: {val[:26]}" + ("…" if len(val) > 26 else ""),
            "value": val,
            "evidence": (v.get("근거") or "").strip(),
            "color": FIELD_COLORS.get(f, "#666666"),
            "labels": FIELD_LABELS.get(f, [f]),
        })
    return items


def capture_one(page, rec: dict, shots: Path, *, workdir: Path | None = None,
                article: bool = True, 기준일: str = "") -> dict:
    """한 사이트 캡처. 실패해도 예외를 올리지 않고 상세를 돌려준다."""
    rid = str(rec.get("id") or "")
    url = normalize_url(rec.get("url") or "")
    info: dict = {"url": url, "파일": {}}
    if not url:
        info["결과"] = "URL없음"
        return info

    nav = goto_settled(page, url)
    info.update(nav)
    if nav.get("이동_실패"):
        info["결과"] = "탐색실패"
        info["예외"] = nav["이동_실패"]
        # DNS 실패와 그 밖의 실패를 구분해 남긴다 — 전자는 폐업·도메인 소멸이고
        # 후자는 재시도 대상이다. 이 구분이 없으면 검수자가 둘을 같이 다룬다.
        info["실패유형"] = ("DNS해석실패" if "ERR_NAME_NOT_RESOLVED" in nav["이동_실패"]
                        else "접속거부" if "ERR_CONNECTION" in nav["이동_실패"]
                        else "시간초과" if "Timeout" in nav["이동_실패"]
                        else "기타")
        return info
    info["최종URL"] = page.url
    info["리다이렉트"] = "Y" if domain_of(page.url) != domain_of(url) else ""

    def shoot(suffix: str, key: str) -> None:
        """상단·하단 뷰포트 캡처. 전체 페이지 캡처는 뉴스 사이트에서 과도하게 커진다."""
        try:
            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(300)
            wait_images(page, IMG_WAIT_SHOT_MS)      # 스크롤로 새로 드러난 이미지
            p = shots / f"{rid}_top{suffix}.png"
            page.screenshot(path=str(p))
            info["파일"][f"상단{key}"] = p.name
        except Exception as exc:  # noqa: BLE001
            info[f"상단{key}_실패"] = f"{exc.__class__.__name__}: {str(exc)[:100]}"
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(600)
            wait_images(page, IMG_WAIT_SHOT_MS)      # 하단 로고는 대개 지연 로딩이다
            p = shots / f"{rid}_footer{suffix}.png"
            page.screenshot(path=str(p))
            info["파일"][f"하단{key}"] = p.name
        except Exception as exc:  # noqa: BLE001
            info[f"하단{key}_실패"] = f"{exc.__class__.__name__}: {str(exc)[:100]}"

    # ① 렌더링 HTML — JS 사이트 재판독용. **가장 먼저** 저장한다.
    #
    # ★ 어떤 오버레이도 주입하기 전에 저장해야 한다 (ISS-031). 주석 레이어(__md_annot__)나
    #   시각 스탬프(__md_stamp__)가 DOM 에 들어간 뒤 page.content() 를 저장하면, 하류 단계가
    #   우리가 얹은 텍스트를 페이지 내용으로 읽는 자기확인 루프가 된다. pilot3 에서 실제로
    #   16/26 파일이 오염됐고, 30884 는 주석에 `발행연월일: 2026-07-31`(= 기준일, 페이지 값이
    #   아님)이 박힌 채로 R3 발행일 검증이 그 HTML 을 읽었다.
    try:
        html, 원charset = force_utf8_meta(page.content())
        if 원charset:
            info["charset_정규화"] = f"{원charset} → utf-8 (저장본은 UTF-8 바이트다)"
        p_html = shots / f"{rid}_rendered.html"
        p_html.write_text(html, encoding="utf-8")
        info["파일"]["렌더HTML"] = p_html.name
        text_len = len(page.evaluate("document.body ? document.body.innerText : ''") or "")
        info["렌더_본문길이"] = text_len
        info["html_길이"] = len(html)
        info["렌더HTML_오버레이오염"] = "N"
    except Exception as exc:  # noqa: BLE001
        info["렌더_실패"] = f"{exc.__class__.__name__}: {str(exc)[:100]}"

    # ② 캡처 시각 스탬프 — 증빙 화면에 "언제 찍었는가" 를 박는다.
    lines = stamp_lines(rec, rid, 기준일=기준일)
    try:
        page.evaluate(STAMP_JS, lines)
        info["스탬프"] = " | ".join(lines)
    except Exception as exc:  # noqa: BLE001
        info["스탬프_실패"] = f"{exc.__class__.__name__}: {str(exc)[:100]}"

    # ③ 원본 화면 — 행정 증빙. 추출 근거 주석은 넣지 않는다(그건 R4 의 주석본이 한다).
    shoot("", "")

    # ③ 최신 기사 페이지 — 발행연월일 검증(R3)이 **이 파일만** 읽는다.
    #
    # 같은 브라우저 세션에서 지금 받아둔다. verify_pubdate 가 따로 fetch 하면 같은 매체를
    # 또 방문하게 된다. 메인 페이지 산출물(스크린샷·렌더HTML)은 위에서 이미 확보했으므로
    # 여기서 페이지를 이동해도 잃는 것이 없다. 주석본(④)은 저장된 렌더HTML 로 따로 만든다.
    if article:
        try:
            cands, 진단 = pick_article(page, domain_of(page.url) or domain_of(url))
            info["기사탐색"] = 진단
            info["기사후보"] = [{"url": c["href"], "제목": c["text"],
                             "점수": c["score"], "패턴일치": c["patterned"],
                             "프레임": c.get("frame"), "제목출처": c.get("titleVia")}
                            for c in cands[:3]]
            if cands:
                top = cands[0]
                anav = goto_settled(page, top["href"])
                info["기사_이동"] = {k: v for k, v in anav.items()
                                 if k in ("이동_시도", "정착_대기_ms", "정착_완료",
                                          "정착_경고", "HTTP상태")}
                if anav.get("이동_실패"):
                    info["기사_실패"] = anav["이동_실패"]
                    info["결과"] = "성공" if info["파일"] else "실패"
                    return info
                # 기사 HTML 도 스탬프 주입 전에 저장한다 (발행일 검증이 이 파일을 읽는다)
                ahtml, _ = force_utf8_meta(page.content())
                pa = shots / f"{rid}_article.html"
                pa.write_text(ahtml, encoding="utf-8")
                info["파일"]["기사HTML"] = pa.name
                try:
                    page.evaluate(STAMP_JS, stamp_lines(rec, rid, 기준일=기준일))
                except Exception:  # noqa: BLE001
                    pass
                try:
                    page.evaluate("window.scrollTo(0, 0)")
                    page.wait_for_timeout(250)
                    ps = shots / f"{rid}_article.png"
                    page.screenshot(path=str(ps))
                    info["파일"]["기사스크린샷"] = ps.name
                except Exception as exc:  # noqa: BLE001
                    info["기사스크린샷_실패"] = f"{exc.__class__.__name__}: {str(exc)[:100]}"
                info["기사url"] = page.url
                info["기사제목_링크"] = top["text"]
                info["기사_HTTP상태"] = anav.get("HTTP상태")
                info["기사선정_근거"] = (
                    f"점수 {top['score']} (글자 {top['fontSize']}px, 굵기 {top['weight']}, "
                    f"위치 {top['top']}px, 기사URL패턴 {'Y' if top['patterned'] else 'N'}, "
                    f"제목출처 {top.get('titleVia') or '-'}, 프레임 {top.get('frame') or '-'}"
                    + (", 완화기준" if 진단.get("완화기준_사용") else "") + ")")
            else:
                info["기사_비고"] = (
                    f"기사 링크 후보 없음 (프레임 {진단.get('프레임수')}개 탐색, "
                    f"완화기준까지 적용) — 프레임별 결과 {진단.get('프레임별')}")
        except Exception as exc:  # noqa: BLE001
            info["기사_실패"] = f"{exc.__class__.__name__}: {str(exc)[:140]}"

    info["결과"] = "성공" if info["파일"] else "실패"
    return info


def save_evidence_map(workdir: Path, rid: str, res: list[dict], shots: Path) -> str:
    """항목별 근거의 DOM 위치를 `W/evidence/{id}.json` 에 남긴다.

    왜 필요한가
    -----------
    1. **재점검 비용** — 다음 회차에 같은 매체를 다시 볼 때, 이 셀렉터만 확인하면 된다.
       전수 8,000건을 매번 처음부터 탐색하는 것과 셀렉터를 검증하는 것은 비용이 다르다.
    2. **행정 근거** — "이 값을 화면 어디서 봤는가" 를 좌표·셀렉터·주변문맥으로 특정할 수
       있어야 처분 근거가 된다. 스크린샷만으로는 나중에 지목이 안 된다.
    3. **회귀 감지** — 사이트가 개편되면 셀렉터가 깨진다. 그 자체가 '표기가 바뀌었다' 는 신호다.

    주석 표시에 실패한 항목도 남긴다 — 실패는 이미지 표기이거나 표기 변형 미대응이고,
    둘 다 사람이 봐야 하는 정보다.
    """
    ev = {
        "언론사_id": rid,
        "생성": datetime.now().isoformat(timespec="seconds"),
        "렌더HTML": f"{rid}_rendered.html",
        "스크린샷": {"상단": f"{rid}_top.png", "하단": f"{rid}_footer.png",
                  "상단_주석": f"{rid}_top_annot.png", "하단_주석": f"{rid}_footer_annot.png"},
        "주의": ("좌표는 캡처 당시 뷰포트 1440x900 기준 문서 좌표다. css 셀렉터는 저장된 "
               "렌더HTML 에서 검증됐다 — 라이브 사이트는 개편으로 달라질 수 있다."),
        "항목": {},
    }
    for r in res:
        f = r.get("field") or ""
        if not f:
            continue
        if r.get("ok"):
            loc = r.get("위치") or {}
            ev["항목"][f] = {
                "값": r.get("value") or "",
                "찾음": True,
                "css": loc.get("css") or "",
                "태그": loc.get("tag") or "",
                "id": loc.get("id") or "",
                "class": loc.get("cls") or "",
                "영역": loc.get("영역") or "",
                "좌표": loc.get("좌표") or {},
                "일치문구": loc.get("matched") or "",
                "텍스트오프셋": loc.get("offset"),
                "주변문맥": loc.get("문맥") or "",
                "탐색기준": ("값 문자열" if loc.get("via") == "value" else "근거 문구"),
                # 라벨('발행인 :' 등) 바로 뒤에서 찾았는가. N 이면 같은 값이 다른 문맥에서
                # 잡혔을 수 있으니 셀렉터 재사용 전에 사람이 확인해야 한다.
                "라벨근접": "Y" if loc.get("라벨근접") else "N",
                "주석번호": r.get("no"),
            }
        else:
            ev["항목"][f] = {
                "값": r.get("value") or "",
                "찾음": False,
                "사유": r.get("reason") or "화면에서 위치를 찾지 못했다",
                "표기변형_시도수": r.get("tried"),
            }
    p = Path(workdir) / "evidence" / f"{rid}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(ev, ensure_ascii=False, indent=2), encoding="utf-8")
    return p.name


def annotate_from_saved(page, rid: str, shots: Path, workdir: Path,
                        *, rec: dict | None = None, 기준일: str = "") -> dict:
    """저장된 렌더 HTML 을 file:// 로 열어 주석본을 만든다 — 네트워크 재방문 없음.

    주석은 LLM 최종값을 반영해야 검수에 쓸모가 있으므로 판단 단계 **뒤**에 만들어야 한다.
    반면 캡처(원본 화면·렌더HTML)는 판단 단계 **앞**에 있어야 LLM 이 그것을 보고 판단한다.
    두 요구를 동시에 만족시키는 방법이 이것이다: 캡처는 먼저, 주석은 저장본으로 나중에.
    """
    info: dict = {"파일": {}}
    p_html = shots / f"{rid}_rendered.html"
    if not p_html.exists():
        info["결과"] = "렌더HTML없음"
        return info
    items = annot_items(workdir, rid)
    if not items:
        info["결과"] = "추출값없음"
        return info
    # 저장본에는 원 사이트의 외부 스크립트·이미지 URL 이 그대로 남아 있다. domcontentloaded 를
    # 기다리면 head 의 외부 스크립트를 받으려다 타임아웃한다(실측: 26건 대부분 25초 초과).
    # 외부 요청은 main 에서 차단하고, 여기서는 commit 까지만 기다린 뒤 고정 시간 정착한다.
    # 저장본은 원 사이트의 인라인·외부 스크립트를 그대로 갖고 있다. 그 중 하나가 파싱을
    # 붙잡으면 domcontentloaded 도, wait_for_function 폴링도 타임아웃한다(실측 6건).
    # 우리에게 필요한 건 body 텍스트뿐이므로 **타임아웃을 허용하고 파싱된 만큼으로 진행**한다.
    nav = ""
    try:
        page.goto(p_html.resolve().as_uri(), timeout=NAV_TIMEOUT,
                  wait_until="domcontentloaded")
    except Exception as exc:  # noqa: BLE001
        nav = f"{exc.__class__.__name__}: {str(exc)[:80]}"
    try:
        page.wait_for_timeout(900)
        if not page.evaluate("() => !!document.body"):
            info["결과"] = "열기실패"
            info["예외"] = f"document.body 없음 (navigation: {nav or 'ok'})"
            return info
    except Exception as exc:  # noqa: BLE001
        info["결과"] = "열기실패"
        info["예외"] = f"{exc.__class__.__name__}: {str(exc)[:140]}"
        return info
    if nav:
        info["navigation_경고"] = nav + " — 파싱된 DOM 으로 진행했다"
    try:
        res = page.evaluate(ANNOTATE_JS, items)
        info["주석_표시"] = [r["field"] for r in res if r.get("ok")]
        info["주석_미표시"] = [r["field"] for r in res if not r.get("ok")]
        info["주석_근거문구기준"] = [r["field"] for r in res
                              if r.get("ok") and r.get("via") == "evidence"]
        # ★ 위치 JSON 은 스크린샷보다 먼저 쓴다 — 스크린샷이 실패해도 위치 기록은 남는다
        info["근거위치_파일"] = save_evidence_map(workdir, rid, res, shots)
        # 주석본에도 시각 스탬프를 얹는다. 이 파일은 저장본을 재렌더한 것이므로
        # 여기서 무엇을 주입해도 원본 렌더HTML 은 영향받지 않는다.
        try:
            page.evaluate(STAMP_JS,
                          stamp_lines(rec or {}, rid, 기준일=기준일, 주석=True))
        except Exception:  # noqa: BLE001
            pass
        info["결과"] = "성공"
    except Exception as exc:  # noqa: BLE001
        info["결과"] = "주석실패"
        info["예외"] = f"{exc.__class__.__name__}: {str(exc)[:140]}"
        return info
    # 스크린샷은 개별로 감싼다 — 한 장이 실패해도 나머지와 위치 기록을 잃지 않는다
    for suffix, key, js in (("top", "상단", "0"),
                            ("footer", "하단", "document.body.scrollHeight")):
        try:
            page.evaluate(f"window.scrollTo(0, {js})")
            page.wait_for_timeout(400)
            p = shots / f"{rid}_{suffix}_annot.png"
            page.screenshot(path=str(p), timeout=15_000)
            info["파일"][f"{key}_주석"] = p.name
        except Exception as exc:  # noqa: BLE001
            info[f"{key}_주석_실패"] = f"{exc.__class__.__name__}: {str(exc)[:100]}"
    if not info["파일"]:
        info["결과"] = "주석표시성공_캡처실패"
    return info


def main() -> None:
    # 건별 상한 감시. 데몬이라 정상 종료를 막지 않는다.
    threading.Thread(target=_건별_감시, daemon=True).start()
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--input", required=True)
    ap.add_argument("--sheet")
    ap.add_argument("--map", dest="map_json", default="")
    ap.add_argument("--base-date", "--기준일", dest="base_date", default="")
    ap.add_argument("--ids", default="")
    # 워치독이 멈춘 샤드를 죽이고 남은 몫으로 재기동할 때 목록이 길어진다.
    # 윈도 명령줄 상한(32,767자)에 걸리지 않도록 파일로도 받는다.
    ap.add_argument("--ids-file", dest="ids_file", default="",
                    help="id 목록 파일 (쉼표·줄바꿈 구분)")
    ap.add_argument("--limit", type=int, default=0)
    # 샤딩 — 캡처는 Playwright 동기 API 라 스레드로 못 나눈다. 프로세스 N 개로 쪼갠다.
    # 분할 기준은 **도메인 해시** 다: 같은 호스트는 항상 같은 샤드로 가므로 서로 다른
    # 프로세스가 한 사이트를 동시에 때리는 일이 원천적으로 없다(밴·부하 회피).
    ap.add_argument("--shard", type=int, default=0, help="이 프로세스가 맡을 샤드 번호 (0부터)")
    ap.add_argument("--shards", type=int, default=1, help="전체 샤드 수")
    ap.add_argument("--only-flagged", action="store_true",
                    help="확인필요=Y 인 건만 캡처 (extract/verify CSV 필요)")
    ap.add_argument("--emit-override", action="store_true",
                    help="렌더 본문이 원 수집보다 길면 overrides 에 rendered_html 을 써준다")
    ap.add_argument("--no-annotate", action="store_true",
                    help="(구 플래그, 무효) 주석본은 --annotate-only 로 따로 만든다")
    ap.add_argument("--annotate-only", action="store_true",
                    help="캡처하지 않고, 저장된 렌더HTML 로 주석본만 만든다 (네트워크 방문 없음). "
                         "LLM 판단 뒤에 실행해야 주석이 최종값을 반영한다")
    ap.add_argument("--no-article", action="store_true",
                    help="최신 기사 페이지를 받아오지 않는다 (발행연월일 검증이 근거를 잃는다)")
    ap.add_argument("--op-col", "--운영컬럼", dest="op_col", default="")
    ap.add_argument("--op-val", "--운영값", dest="op_val", default="운영")
    args = ap.parse_args()

    base = (datetime.strptime(args.base_date, "%Y-%m-%d").date()
            if args.base_date else date.today())
    workdir = Path(args.workdir)
    shots = workdir / "shots"
    shots.mkdir(parents=True, exist_ok=True)
    (workdir / "audit").mkdir(parents=True, exist_ok=True)

    override_map = json.loads(args.map_json) if args.map_json else None
    rows, mapping, _ = load_registry(args.input, override_map=override_map, sheet=args.sheet)
    if "url" not in mapping:
        print(json.dumps({"error": "URL 컬럼 없음 — inspect_registry.py 로 확인"},
                         ensure_ascii=False)); sys.exit(1)

    if args.op_col:
        rows = [r for r in rows if args.op_val in str(r.get("운영상태", ""))]
    id원문 = args.ids
    if args.ids_file:
        id원문 += "," + Path(args.ids_file).read_text(encoding="utf-8")
    if id원문.strip(", \n\r\t"):
        want = {s for s in re.split(r"[,\s]+", id원문) if s}
        rows = [r for r in rows if str(r.get("id")) in want]
    if args.only_flagged:
        want = flagged_ids(workdir, base)
        if not want:
            print(json.dumps({"error": "확인필요 건을 찾지 못했다 — extract/verify CSV 확인"},
                             ensure_ascii=False)); sys.exit(1)
        rows = [r for r in rows if str(r.get("id")) in want]
    if args.shards > 1:
        import hashlib
        def _dom(u: str) -> str:
            h = urlparse(normalize_url(u or "")).netloc.lower()
            return h[4:] if h.startswith("www.") else h
        rows = [r for r in rows
                if int(hashlib.md5(_dom(r.get("url") or str(r.get("id"))).encode()).hexdigest(), 16)
                % args.shards == args.shard]
    if args.limit:
        rows = rows[: args.limit]
    if not rows:
        print(json.dumps({"error": "대상 없음"}, ensure_ascii=False)); sys.exit(1)

    from playwright.sync_api import sync_playwright

    결과: list[dict] = []
    override_written: list[str] = []
    처리수 = 재활용 = 크래시재시도 = 0

    def 새_페이지(browser, old_ctx, old_page):
        """컨텍스트·페이지를 새로 만든다 (크래시 복구 / 선제 재활용)."""
        for obj in (old_page, old_ctx):
            try:
                obj.close()
            except Exception:  # noqa: BLE001
                pass
        c = browser.new_context(viewport=VIEWPORT, ignore_https_errors=True,
                                locale="ko-KR")
        return c.new_page(), c

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        # 인증서 오류를 무시한다 — ISS-001 과 같은 정책(인증서는 점검 항목이 아니다)
        ctx = browser.new_context(viewport=VIEWPORT, ignore_https_errors=True,
                                  locale="ko-KR")
        page = ctx.new_page()
        if args.annotate_only:
            # 저장본 재렌더 시 외부 네트워크를 완전히 차단한다. 두 가지 이유:
            #  1) 원 사이트 스크립트를 받으려다 페이지 로드가 타임아웃한다
            #  2) 이 라운드는 '방문 없음' 이 설계 전제다 — 몰래 원 사이트를 때리면 안 된다
            # abort 가 아니라 **빈 응답으로 채운다**. abort 하면 폰트 요청이 미결로 남아
            # document.fonts.ready 가 풀리지 않고, Playwright 의 screenshot 이
            # "waiting for fonts to load" 에서 30초 타임아웃한다(실측 30893).
            # 204 를 주면 폰트 로드가 즉시 실패로 확정되고 화면은 대체 글꼴로 그려진다.
            def _block(route):
                try:
                    if route.request.url.startswith("file:"):
                        route.continue_()
                    else:
                        route.fulfill(status=204, body="")
                except Exception:  # noqa: BLE001
                    try:
                        route.abort()
                    except Exception:  # noqa: BLE001
                        pass
            page.route("**/*", _block)
        for i, rec in enumerate(rows, 1):
            rid = str(rec.get("id") or "")
            건별_시작(rid, i, len(rows))
            if args.annotate_only:
                # 판단 단계 뒤에 주석본만 다시 만든다 — 저장된 렌더HTML 사용, 방문 없음
                info = annotate_from_saved(page, rid, shots, workdir,
                                           rec=rec, 기준일=base.isoformat())
                결과.append({"언론사_id": rid, **info})
                audit.record(workdir, rid, "4b_주석본", info.get("결과", "실패"), info,
                             제호=rec.get("제호"), 기준일=base.isoformat())
                print(f"[{i}/{len(rows)}] {rid} {rec.get('제호','')} 주석본 "
                      f"{info.get('결과')} ({len(info.get('주석_표시') or [])}항목)",
                      file=sys.stderr)
                continue
            # ── 페이지 재활용 ──────────────────────────────────────────────
            # 한 페이지를 계속 쓰면 렌더러가 메모리를 물고 늘어지다 `Page crashed` 로 죽는다.
            # 일정 건수마다 선제적으로 새로 만든다(비용은 1초 남짓).
            if 처리수 and 처리수 % RECYCLE_EVERY == 0:
                page, ctx = 새_페이지(browser, ctx, page)
                재활용 += 1
            처리수 += 1

            info = capture_one(page, rec, shots, workdir=workdir,
                               article=not args.no_article, 기준일=base.isoformat())
            # 크래시는 사이트 실패가 아니다 — 새 페이지로 한 번 더 해본다
            if any(m in str(info.get("이동_실패") or "") for m in CRASH_MARKS):
                page, ctx = 새_페이지(browser, ctx, page)
                재활용 += 1
                크래시재시도 += 1
                info = capture_one(page, rec, shots, workdir=workdir,
                                   article=not args.no_article, 기준일=base.isoformat())
                info["크래시_재시도"] = "Y"
            # ★ dict 를 **참조로** 담는다. 예전에는 {"언론사_id": rid, **info} 로 복사본을
            #   만들어 넣었고, 그 뒤에 계산하는 렌더링_개선·원수집_본문길이 가 요약 JSON 에
            #   영원히 반영되지 않았다. 오케스트레이터는 렌더링_개선_id 로 R0c 재추출 대상을
            #   정하므로, 이 버그 때문에 **R0c 가 한 번도 실행되지 않았다** — 30876 처럼
            #   requests 본문 0자인데 렌더로 2,615자를 얻은 JS 사이트가 반영되지 못했다.
            info["언론사_id"] = rid
            결과.append(info)

            # 원 수집 본문 길이와 비교 — 렌더링이 더 많이 얻었으면 override 후보다
            원길이 = None
            wp = workdir / "work" / f"{rid}.json"
            if wp.exists():
                try:
                    원길이 = (json.loads(wp.read_text(encoding="utf-8"))
                            .get("텍스트_길이", {}).get("전체"))
                except Exception:  # noqa: BLE001
                    pass
            info["원수집_본문길이"] = 원길이

            # ★ 길이 비교는 **같은 추출기로** 해야 한다.
            # `렌더_본문길이` 는 Playwright innerText(보이는 것만)이고 `원수집_본문길이` 는
            # BeautifulSoup get_text(숨은 요소 포함)이다. 둘을 비교하면 정상 사이트도
            # 렌더가 42~57% 로 나와 오탐만 낸다(실측 9/9 오탐). 저장된 렌더HTML 을
            # 하류가 실제로 쓰는 extract_zones 로 다시 재서 비교한다.
            렌더zones = None
            hp = shots / f"{rid}_rendered.html"
            if hp.exists():
                try:
                    from md_core.zones import extract_zones as _ez
                    렌더zones = len((_ez(hp.read_text(encoding="utf-8", errors="replace"))
                                    .get("전체") or ""))
                except Exception:  # noqa: BLE001
                    렌더zones = None
            info["렌더HTML_zones길이"] = 렌더zones
            # 렌더링 개선 = 하류가 쓸 텍스트가 실제로 늘었는가. innerText 로 재면
            # 30876(원수집 0자 → 렌더 2,615자) 같은 건만 잡히고 부분 개선은 놓친다.
            개선 = (렌더zones is not None
                  and 렌더zones > max(200, (원길이 or 0) + 300))
            info["렌더링_개선"] = "Y" if 개선 else ""
            if (원길이 or 0) > 300 and 렌더zones is not None and 렌더zones < (원길이 or 0) * 0.9:
                info["로딩미완_의심"] = (
                    f"렌더HTML {렌더zones}자 < 원수집 {원길이}자 "
                    f"({렌더zones / 원길이:.0%}, 같은 추출기 기준) — 화면이 덜 그려진 상태로 "
                    f"찍혔을 수 있다. 정착대기 {info.get('정착_대기_ms')}ms, "
                    f"완료={info.get('정착_완료')}")

            if 개선 and args.emit_override:
                dom = domain_of(rec.get("url") or "")
                if dom:
                    op = workdir / "overrides" / f"{dom}.json"
                    op.parent.mkdir(parents=True, exist_ok=True)
                    prev = {}
                    if op.exists():
                        try:
                            prev = json.loads(op.read_text(encoding="utf-8"))
                        except Exception:  # noqa: BLE001
                            prev = {}
                    prev.update({
                        "domain": dom,
                        "rendered_html": (shots / f"{rid}_rendered.html").resolve().as_uri(),
                        "notes": (prev.get("notes", "") + " / " if prev.get("notes") else "")
                                 + f"JS 렌더링 필요 — capture_shots.py 가 저장한 DOM 사용"
                                   f"(원 본문 {원길이}자 → 렌더HTML {렌더zones}자, "
                                   f"같은 추출기 기준)",
                    })
                    op.write_text(json.dumps(prev, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
                    override_written.append(rid)

            audit.record(workdir, rid, "4_증빙캡처",
                         info.get("결과", "실패"), info,
                         제호=rec.get("제호"), 기준일=base.isoformat())
            표시 = info.get("주석_표시")
            print(f"[{i}/{len(rows)}] {rid} {rec.get('제호','')} "
                  f"{info.get('결과')} "
                  f"(렌더본문 {info.get('렌더_본문길이','-')}자"
                  + (f", 원수집 {원길이}자" if 원길이 is not None else "")
                  + (", 렌더링개선" if 개선 else "")
                  + (f", 주석 {len(표시)}항목" if 표시 is not None else "") + ")",
                  file=sys.stderr)
        ctx.close()
        browser.close()

    성공 = sum(1 for r in 결과 if r.get("결과") == "성공")
    print(json.dumps({
        "기준일": base.isoformat(), "대상": len(결과), "성공": 성공,
        "실패": len(결과) - 성공,
        "렌더링_개선_id": [r["언론사_id"] for r in 결과 if r.get("렌더링_개선") == "Y"],
        "override_생성": override_written,
        "주석_표시_합계": sum(len(r.get("주석_표시") or []) for r in 결과),
        "주석_미표시": {r["언론사_id"]: r["주석_미표시"] for r in 결과
                   if r.get("주석_미표시")},
        # 발행연월일 검증(R3)이 쓸 기사 페이지를 몇 건 확보했는가.
        # 이 수가 낮으면 R3 가 근거 없이 판정하게 된다 — 캡처 단계에서 잡아야 한다.
        "기사페이지_확보": sum(1 for r in 결과 if (r.get("파일") or {}).get("기사HTML")),
        "기사페이지_실패": {r["언론사_id"]: (r.get("기사_실패") or r.get("기사_비고"))
                     for r in 결과
                     if not (r.get("파일") or {}).get("기사HTML")
                     and (r.get("기사_실패") or r.get("기사_비고"))},
        # 로딩 상태 — 이 두 수치가 0 이 아니면 캡처 시점이 이르다
        "로딩미완_의심": {r["언론사_id"]: r["로딩미완_의심"] for r in 결과
                    if r.get("로딩미완_의심")},
        "정착_미완": {r["언론사_id"]: r.get("정착_경고") for r in 결과
                  if r.get("정착_완료") is False},
        "재시도로_살린_건": {r["언론사_id"]: r["이동_시도"] for r in 결과
                     if len(r.get("이동_시도") or []) > 1 and not r.get("이동_실패")},
        "실패유형": {r["언론사_id"]: r["실패유형"] for r in 결과 if r.get("실패유형")},
        "평균_정착대기_ms": (round(sum(r.get("정착_대기_ms") or 0 for r in 결과)
                                / max(1, sum(1 for r in 결과 if r.get("정착_대기_ms")))) ),
        "shots_경로": str(shots),
        "샤드": f"{args.shard}/{args.shards}" if args.shards > 1 else "전체",
        "페이지_재활용": 재활용,
        "크래시_재시도": 크래시재시도,
        "실패상세": [{k: v for k, v in r.items() if k in
                  ("언론사_id", "결과", "예외", "url")}
                 for r in 결과 if r.get("결과") != "성공"],
        "다음": ("렌더링 개선 건은 --emit-override 로 override 를 만든 뒤 "
               "run_extract.py 를 --ids 로 재실행해라"),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
