'use strict';
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const names = ['등록정보·URL 확인','홈페이지 화면 확인','홈페이지 유무','포털 검색·URL 확인','필요적 게재사항','등록정보 일치 여부','특이사항 확인','최종 확인·승인'];
const prompts = ['등록된 제호명과 홈페이지 주소(URL)를 확인하고, 홈페이지의 제호명·URL 변경 여부를 확인하세요.', '메인 화면의 상단·하단과 접속 결과를 확인하세요. 오류 페이지나 타 사이트로 연결되는 경우 사유를 남기세요.', '등록된 홈페이지 주소에서 제호명과 뉴스 기사 제공 여부를 확인하고, 홈페이지 있음·없음을 구분하세요.', '홈페이지가 없거나 등록 URL로 접속할 수 없는 경우 제호명 검색 결과와 추가 발견한 URL을 확인하세요.', '메인 화면 상·하단에서 8개 항목의 게재 여부를 확인하세요. 제호명은 하단을 먼저 확인하고 없으면 상단을 확인하세요.', '등록번호·발행인·편집인·발행소 주소·전화번호의 실제 게재 내용을 등록정보와 대조하세요.', '제호명·URL 변경, 홈페이지 운영 현황과 필요적 게재사항의 특이사항 및 추가 확인 사유를 확인하세요.', '홈페이지 유무, 필요적 게재사항, 등록정보 일치 여부와 특이사항을 확인한 뒤 점검을 완료하세요.'];
const trainingPages = [2,3,1,6,7,10,10,7];
const fieldNames = ['제호','등록번호','등록연월일','발행인','편집인','발행연월일','발행소 주소','발행소 전화번호'];
const comparisonNames = ['등록번호','발행인','편집인','발행소 주소','발행소 전화번호'];
const stateNames = {pending:'미점검',reviewing:'점검 중',held:'보류',reprocess:'추가 확인 대기',completed:'완료'};
const assetNames = {'top.png':'상단 원본','footer.png':'하단 원본','article.png':'기사 원본','top_annot.png':'상단 박스 표시','footer_annot.png':'하단 박스 표시'};
let assignment, detail, identity, gate = 1, page = 1, selectedAsset, dirty = false, busy = false;
let selectionSerial = 0, evidenceVariant = 'annotated', evidenceRegion = 'footer.png';

async function api(path, body) {
  const r = await fetch(path, body === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':identity?.csrf||''},body:JSON.stringify(body)});
  const data = await r.json();
  if(r.status===401){location.href='/login';throw new Error('로그인이 필요합니다.');}
  if (!r.ok) throw new Error(data.error || '요청에 실패했습니다.');
  return data;
}
let toastTimer;
function toast(message, error=false) { const t=$('#toast');t.textContent=message;t.className=error?'error':'';t.hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>t.hidden=true,6000); }
function safe(fn) { return async (...args) => {try {await fn(...args);} catch(e){toast(e.message,true);}}; }
function allowLeave(){if(busy){toast('저장이 끝난 뒤 이동해 주세요.');return false;}return !dirty || confirm('저장하지 않은 내용이 있습니다. 이동하면 입력 내용이 사라집니다. 이동할까요?');}
function badge(value) {const tone=value==='준수'||value==='completed'?'good':value==='미준수'?'bad':value==='held'||value==='reprocess'?'warn':'';return `<span class="badge ${tone}">${esc(stateNames[value]||({'홈페이지없음':'홈페이지 없음'}[value])||value)}</span>`;}
function options(values,current,blank='선택하세요') {return `<option value="">${esc(blank)}</option>`+values.map(v=>{const [key,label]=Array.isArray(v)?v:[v,v];return `<option value="${esc(key)}" ${key===current?'selected':''}>${esc(label)}</option>`;}).join('');}
function fact(label,value){return `<dt>${esc(label)}</dt><dd>${esc(value||'—')}</dd>`;}
function openModal(content){$('#modal-content').innerHTML=`<div class="modal-inner">${content}</div>`;if(!$('#modal').open)$('#modal').showModal();}
$('#close-modal').onclick=()=>$('#modal').close();

async function refresh(selectFirst=false) {
  assignment=await api('/api/assignment?view=prototype');
  const rows=assignment.businesses;
  $('.demo').textContent='유형별 15건 프로토타입';
  $('#total').textContent=rows.length.toLocaleString();$('#reviewer').textContent=assignment.meta.reviewer;
  $('#source-date').textContent=`자료 기준 ${assignment.meta.base_date}`;
  const done=rows.filter(x=>x.state==='completed').length;
  $('#completed').textContent=done;$('#held').textContent=rows.filter(x=>x.state==='held').length;
  $('#queued').textContent=rows.filter(x=>x.state==='reprocess').length;
  $('#progress').max=rows.length;$('#progress').value=done;$('#progress-label').textContent=`${done.toLocaleString()} / ${rows.length.toLocaleString()}`;
  $('#setup-banner').hidden=!!assignment.meta.setup?.confirmed;
  renderList();
  if(selectFirst && rows.length)await selectBusiness(rows[0].id);
  if(!rows.length)$('#detail').innerHTML='<div class="loading">아직 배정된 매체가 없습니다.<br>관리자에게 배정을 요청하세요.</div>';
}
function filtered(){const query=$('#search').value.trim().toLowerCase(),state=$('#state-filter').value,issue=$('#issue-filter').value;
  return assignment.businesses.filter(r=>(!query||`${r.name} ${r.id} ${r.url}`.toLowerCase().includes(query))&&(!state||r.state===state)&&(!issue||(issue==='flagged'?r.flagged:issue==='evidence'?!!r.missing_evidence:r.decision===issue)));}
function renderList(){if(!assignment)return;const rows=filtered(),max=Math.max(1,Math.ceil(rows.length/25));page=Math.min(page,max);
  $('#filtered-count').textContent=`${rows.length.toLocaleString()}건`;
  $('#business-list').innerHTML=rows.slice((page-1)*25,page*25).map(r=>`<button class="business-row ${detail?.id===r.id?'selected':''}" data-id="${esc(r.id)}"><span class="row-top"><b>${esc(r.name)}</b>${badge(r.state)}</span><span class="row-bottom"><span>#${esc(r.id)}</span>${badge(r.decision)}${r.flagged?'<span>확인필요</span>':''}<span>${r.approved_gates}/8</span></span><small class="case-label">${esc(r.case_label||'')}</small></button>`).join('')||'<div class="empty-list">조건에 맞는 매체가 없습니다.</div>';
  $('#page-label').textContent=`${page} / ${max} 페이지`;
  $('#prev-page').disabled=page===1;$('#next-page').disabled=page===max;
  document.querySelectorAll('.business-row').forEach(el=>el.onclick=safe(async()=>{if(allowLeave())await selectBusiness(el.dataset.id);}));
}
async function selectBusiness(id){const serial=++selectionSerial;const loaded=await api('/api/business/'+encodeURIComponent(id));if(serial!==selectionSerial)return;detail=loaded;dirty=false;
  gate=nextGate();evidenceVariant='annotated';focusEvidence();renderList();renderDetail();}
function nextGate(){for(let n=1;n<=8;n++)if(detail.review.gates?.[n]?.status!=='approved')return n;return 8;}
function currentData(){return detail.review.gates?.[gate]?.data||{};}
function operation(){return detail.review.gates?.[3]?.data?.operation||detail.source.operation;}
function formContent(){const s=detail.source,d=currentData();
  if(gate===1)return `<dl class="facts">${fact('등록 제호명',s.name)}${fact('매체 ID',s.id)}<dt>등록 URL</dt><dd>${urlLink(s.url)}</dd><dt>수집 URL</dt><dd>${urlLink(s.final_url)}</dd></dl><label class="form-label" for="review-url">점검 대상 홈페이지 주소(URL)</label><input id="review-url" value="${esc(s.url)}" readonly><p class="image-note">URL이 변경된 경우 메인 화면의 주소를 그대로 복사해 추가 확인 요청에 기록하세요. 제호명은 하단을 우선 확인하고 띄어쓰기에 유의하세요.</p>`;
  if(gate===2)return `<dl class="facts">${fact('접속 결과',s.connection)}${fact('증빙 파일',s.assets.length+'개')}${fact('최종 URL',s.final_url)}</dl>${s.missing_evidence?`<div class="callout">${esc(s.missing_evidence)}</div>`:''}<label class="form-label" for="quality">홈페이지 화면 확인</label><select id="quality">${options([['usable','점검할 홈페이지 화면 확인'],['failure','접속 불가·오류 페이지 확인']],d.quality)}</select><p class="image-note">캡처 시각은 이미지와 아래 자동 수집·판단 이력에서 확인하세요. 자료 기준일과 실제 캡처 시각은 다를 수 있습니다.</p>`;
  if(gate===3)return `<dl class="facts">${fact('AI 판단',s.operation+' / '+s.logic)}${fact('판단 근거',s.operation_reason)}</dl><label class="form-label" for="operation">홈페이지 유무</label><select id="operation">${options([['있음','홈페이지 있음'],['없음','홈페이지 없음']],d.operation)}</select><div class="source-note"><b>현재 적용 기준 · 260813 로직</b><br>제호명 동일 → 뉴스 기사 제공 여부와 관계없이 홈페이지 있음.<br>제호명 다름 + 뉴스 기사 있음 → 홈페이지 있음.<br>제호명 다름 + 뉴스 기사 없음 또는 오류 페이지 → 홈페이지 없음 후 포털 검색.<br><small>학습자료의 운영·미운영 설명과 구분해 적용합니다.</small></div>`;
  if(gate===4)return operation()==='있음'?'<div class="source-note">홈페이지 있음으로 분류된 매체입니다. 제호명과 URL 변경 여부를 확인하고 필요적 게재사항 점검으로 진행합니다.</div>':`<dl class="facts">${fact('검색 판정',s.search_decision||'검색 결과 미기록')}${fact('추가 발견한 URL',s.candidate_url)}</dl><div class="source-note">포털에서 찾은 홈페이지의 등록번호·발행인·편집인·발행소 주소·전화번호 중 등록정보와 같은 항목이 1개 이상인지 확인하세요. 일치 근거와 URL을 기록한 뒤 추가 확인을 요청하세요.</div>${!s.search_decision?'<div class="callout">포털 검색 결과가 기록되지 않았습니다. 네이버·구글 제호명 검색을 요청하거나 보류하세요. 검색 전에는 홈페이지를 찾지 못한 것으로 확정하지 않습니다.</div>':''}<details><summary>저장된 검색 근거 보기</summary><pre class="raw">${esc(JSON.stringify(s.search_result||{status:'검색 저장본 없음'},null,2))}</pre></details>`;
  if(gate===5){if(operation()==='없음')return '<div class="source-note">홈페이지 없음이 확인된 건은 필요적 게재사항 점검 대상이 아닙니다. 미준수로 집계하지 않습니다.</div>';
    return `<details class="criteria-note"><summary>현재 적용 기준 · 학습자료와 다른 날짜·성명 인정 방식</summary><p>260813 로직에 따라 창간일·등록일 등 명칭과 관계없이 날짜 형태를 인정하며 실시간 현재날짜 표시는 제외합니다. 기사 업로드 날짜는 발행연월일에 인정하지만 등록연월일로 전이하지 않습니다. 발행인·편집인은 이름 기재 항목의 대체·교차 인정을 적용합니다.</p></details><div class="comparison-head"><span>항목 / 등록정보</span><span>AI 수집</span><span>조사자 확인</span></div>`+fieldNames.map((name,i)=>{const f=s.fields[name],v=d.fields?.[name]||{};return `<section class="field-card compare-row" data-field="${i}"><div><button class="field-focus" data-focus="${esc(name)}">${i+1}. ${name} ↗</button><p>${esc(f.registered||(name==='제호'?s.name:'등록값 없음'))}</p></div><div class="machine"><b>${esc(f.value||'확인되지 않음')}</b><small>${esc(f.location||'위치 미확인')} · ${esc(f.method||'방법 미기록')}</small><details><summary>수집 근거</summary>${esc(f.evidence||'자동 근거 없음')}</details><button class="locate" data-focus="${esc(name)}">화면 위치 보기</button></div><div class="review-cell"><div class="field-controls"><select aria-label="${name} 게재 여부" class="decision">${options([['present','게재됨'],['absent','게재되지 않음']],v.decision)}</select><select aria-label="${name} 인정 방식" class="method">${options([['direct','직접 표기'],['substitute','대체 인정'],['cross','교차 인정'],['article','기사 날짜'],['absent','게재되지 않음']],v.method,'인정 방식')}</select></div><input class="value" aria-label="${name} 점검값" value="${esc(v.value??f.value)}" placeholder="화면 확인값"><textarea class="evidence" rows="2" aria-label="${name} 근거" placeholder="문구·위치·인정 이유">${esc(v.evidence??'')}</textarea></div></section>`;}).join('');}
  if(gate===6){if(operation()==='없음')return '<div class="source-note">홈페이지 없음 확인 건입니다. 등록정보 대조 대상 아님을 확인합니다.</div>';return `<div class="comparison-head"><span>항목 / 등록정보</span><span>AI 수집</span><span>조사자 확인</span></div>`+comparisonNames.map((name,i)=>{const f=s.fields[name],reviewed=detail.review.gates?.[5]?.data?.fields?.[name];return `<section class="field-card compare-row"><div><button class="field-focus" data-focus="${esc(name)}">${name} ↗</button><p>${esc(f.registered||'등록값 없음')}</p></div><div class="machine"><b>${esc(f.value||'확인되지 않음')}</b><small>AI 일치 여부: ${esc(f.comparison||'없음')}</small><button class="locate" data-focus="${esc(name)}">화면 위치 보기</button></div><div class="review-cell"><p>${esc(reviewed?.value||'조사자 확인값 없음')}</p><label class="form-label" for="comparison-${i}">대조 결과</label><select id="comparison-${i}">${options(['일치','부분일치','불일치',['미검출','확인되지 않음'],['등록값없음','등록정보 없음']],d.comparisons?.[name])}</select></div></section>`;}).join('');}
  if(gate===7)return `<dl class="facts">${fact('확인필요 사유',s.reason)}${fact('증빙 누락 표시',s.missing_evidence||'없음')}${fact('자동 점검 결과 간 차이',s.difference||'없음')}</dl><div class="source-note">조사자가 확인한 게재 내용과 홈페이지 화면이 일치하는지 확인하세요. 제호명·URL 변경 및 기타 특이사항을 기록하고, 화면 표시가 다르면 추가 확인을 요청하세요.</div><details><summary>근거 위치 정보</summary><pre class="raw">${esc(JSON.stringify(s.evidence_map||{},null,2))}</pre></details>`;
  return `<div class="source-note"><b>이 매체의 점검를 완료합니다.</b><br>AI 점검 결과은 보존되고 조사자 점검 결과가 별도로 저장됩니다.</div><dl class="facts">${fact('AI 점검 결과',s.decision)}${fact('조사자 확인 홈페이지 유무',operation())}${fact('승인된 단계',Object.values(detail.review.gates||{}).filter(x=>x.status==='approved').length+' / 8')}</dl><p class="image-note">이전 단계의 값이나 증거를 변경하면 최종 승인은 해제됩니다.</p>`;
}
function renderDetail(){const s=detail.source,v=detail.review.gates?.[gate];
  $('#detail').innerHTML=`<div class="review-sticky-header" role="region" aria-label="현재 매체와 점검 단계"><div class="detail-head"><div class="detail-title"><h2>${esc(s.name)}</h2><span class="id">#${esc(detail.id)}</span>${badge(detail.state)}</div><div class="detail-meta"><span>등록 URL ${urlLink(s.url)}</span><span>AI 점검 결과 ${badge(s.decision)}</span></div></div><nav class="gate-nav" aria-label="점검 단계">${names.map((n,i)=>`<button data-gate="${i+1}" class="${gate===i+1?'current':''} ${detail.review.gates?.[i+1]?.status==='approved'?'done':''}">${detail.review.gates?.[i+1]?.status==='approved'?'✓':i+1} ${n}</button>`).join('')}</nav><div class="stage-intro"><h3>${String(gate).padStart(2,'0')} · ${names[gate-1]}</h3><p>${prompts[gate-1]}</p><button class="training-link" id="stage-training">학습자료 ${trainingPages[gate-1]+13}쪽 보기 ↗</button></div></div><div class="detail-body"><section class="evidence-panel"><div class="section-label"><b>저장된 화면 증빙</b><button id="fullscreen-image" class="icon" title="증빙 크게 보기">⛶</button></div><div id="evidence-controls"></div><div id="evidence-version-note" class="image-note" aria-live="polite"></div><div class="image-toolbar"><button id="zoom-out" aria-label="축소">−</button><output id="zoom-label">맞춤</output><button id="zoom-in" aria-label="확대">＋</button><button id="zoom-fit">전체 맞춤</button><button id="zoom-actual">100%</button></div><div class="image-area" id="image-area"></div><div id="evidence-location" class="evidence-location"></div><button id="reprocess" class="secondary">↻ 추가 확인 요청</button></section><section class="form-panel" id="review-form">${formContent()}<label class="form-label" for="review-note">특이사항 / 수정·보류 사유</label><textarea id="review-note" placeholder="홈페이지 운영 현황·필요적 게재사항의 특이사항을 기록하세요. 수정·보류 시 사유를 남겨 주세요.">${esc(v?.note||'')}</textarea><label class="checkline"><input id="checked" type="checkbox" ${v?.data?.checked?'checked':''}><span>이 단계의 자료와 적용 기준을 확인했습니다.</span></label></section></div><div class="detail-footer"><span class="save-hint">${dirty?'저장하지 않은 변경 있음':v?'서버에 저장된 기록':'아직 저장된 점검 기록 없음'}</span><button id="previous" ${gate===1?'disabled':''}>← 이전</button><button id="hold">보류</button><button id="draft">임시 저장</button><button id="approve" class="primary">${gate===8?'점검 완료':'승인하고 다음 →'}</button></div><details class="history"><summary>점검 이력 ${detail.events.length}건 · 자동 수집·판단 이력 보기</summary><ul>${detail.events.map(e=>`<li>${esc(new Date(e.created_at).toLocaleString('ko-KR'))} · ${esc(e.actor)} · ${esc(e.action)}</li>`).join('')||'<li>아직 조사자 점검 이력이 없습니다.</li>'}</ul><details><summary>AI 판단 기록 (원본)</summary><pre class="raw">${esc(JSON.stringify(s.audit||{},null,2))}</pre></details></details>`;
  if(s.raw){const rawPanel=document.createElement('details');rawPanel.className='history';const summary=document.createElement('summary');summary.textContent='연결된 원본 조사자료 원본 컬럼 · '+Object.keys(s.raw).length+'개';rawPanel.append(summary);const pre=document.createElement('pre');pre.className='raw';pre.textContent=JSON.stringify({source_file:s.source_file,source_sha256:s.source_sha256,raw:s.raw},null,2);rawPanel.append(pre);$('#detail').append(rawPanel);}
  renderImage();
  document.querySelectorAll('[data-gate]').forEach(b=>b.onclick=()=>{if(allowLeave()){gate=Number(b.dataset.gate);dirty=false;focusEvidence();renderDetail();}});
  $('#review-form').oninput=()=>{dirty=true;$('.save-hint').textContent='저장하지 않은 변경 있음';};
  $('#draft').onclick=safe(()=>save('draft'));$('#hold').onclick=safe(()=>save('hold'));$('#approve').onclick=safe(()=>save('approve'));
  $('#reprocess').onclick=jobModal;
  $('#stage-training').onclick=safe(()=>setupModal(trainingPages[gate-1]));
  $('#previous').onclick=()=>{if(gate>1&&allowLeave()){gate--;dirty=false;focusEvidence();renderDetail();}};
  document.querySelectorAll('[data-focus]').forEach(b=>b.onclick=()=>{focusEvidence(b.dataset.focus);renderImage();document.querySelectorAll('.field-card').forEach(x=>x.classList.toggle('focused',x.contains(b)));});
  $('#zoom-in').onclick=()=>changeZoom(1.25);$('#zoom-out').onclick=()=>changeZoom(.8);
  $('#zoom-fit').onclick=()=>{zoom=0;applyZoom();};$('#zoom-actual').onclick=()=>{zoom=1;applyZoom();};
  $('#fullscreen-image').onclick=()=>{if(!selectedAsset)return;$('#fullscreen-image').disabled=true;const panel=$('.evidence-panel');const marker=document.createElement('div');marker.id='evidence-marker';panel.before(marker);openModal('<div id="expanded-evidence"></div>');$('#modal').classList.add('image-modal');$('#expanded-evidence').append(panel);applyZoom();$('#modal').addEventListener('close',()=>{marker.replaceWith(panel);$('#fullscreen-image').disabled=false;$('#modal').classList.remove('image-modal');applyZoom();},{once:true});};
}
let zoom=0, evidenceText='', imageResizeObserver;
function urlLink(value){try{const u=new URL(/^https?:\/\//i.test(value)?value:'https://'+value);if(!value||!['http:','https:'].includes(u.protocol))throw 0;return `<a href="${esc(u.href)}" target="_blank" rel="noopener noreferrer">${esc(value)} ↗</a>`;}catch{return esc(value||'없음');}}
function assetRegion(asset){return asset?.replace('_annot','');}
function annotatedAsset(region){return region?.replace('.png','_annot.png');}
function chooseEvidence(region){
  const assets=detail.source.assets;
  evidenceRegion=assets.some(a=>assetRegion(a)===region)?region:assetRegion(assets[0]);
  const preferred=evidenceVariant==='annotated'?annotatedAsset(evidenceRegion):evidenceRegion;
  const fallback=evidenceVariant==='annotated'?evidenceRegion:annotatedAsset(evidenceRegion);
  selectedAsset=assets.includes(preferred)?preferred:assets.includes(fallback)?fallback:undefined;
}
function renderEvidenceControls(){
  const assets=detail.source.assets,marked=selectedAsset?.includes('_annot');
  $('#evidence-controls').innerHTML=`<div class="evidence-selectors"><div class="asset-tabs" role="group" aria-label="증빙 위치">${[['top.png','상단'],['footer.png','하단'],['article.png','기사']].filter(([r])=>assets.some(a=>assetRegion(a)===r)).map(([r,n])=>`<button data-region="${r}" class="${r===evidenceRegion?'active':''}" aria-pressed="${r===evidenceRegion}">${n}</button>`).join('')}</div><div class="asset-tabs evidence-variants" role="group" aria-label="증빙 표시 방식"><button data-variant="annotated" class="${marked?'active':''}" aria-pressed="${!!marked}" ${assets.includes(annotatedAsset(evidenceRegion))?'':'disabled'}>박스 표시</button><button data-variant="original" class="${selectedAsset&&!marked?'active':''}" aria-pressed="${!!selectedAsset&&!marked}" ${assets.includes(evidenceRegion)?'':'disabled'}>원본 사진</button></div></div>`;
  $('#evidence-version-note').textContent=!selectedAsset?'':marked?'발견 위치에 박스가 표시된 저장본입니다. 원본 사진으로 전환할 수 있습니다.':assets.includes(annotatedAsset(evidenceRegion))?'박스 없는 원본 사진입니다.':'이 위치는 박스 표시본이 없어 원본 사진을 보여줍니다.';
  document.querySelectorAll('[data-region]').forEach(b=>b.onclick=()=>{chooseEvidence(b.dataset.region);zoom=0;renderImage();});
  document.querySelectorAll('[data-variant]').forEach(b=>b.onclick=()=>{evidenceVariant=b.dataset.variant;chooseEvidence(evidenceRegion);zoom=0;renderImage();});
}
function focusEvidence(field){
  const s=detail.source,items=s.evidence_map?.['항목']||{};
  let name=field||([1,3,4].includes(gate)?'제호':gate===6?'등록번호':gate===5?'제호':Object.keys(items).find(k=>items[k]['찾음'])||'제호');
  const loc=items[name]||{},f=s.fields[name]||{},location=loc['영역']||f.location||'';
  const article=/기사/.test(location+' '+f.method), zone=article?'article.png':/상단/.test(location)?'top.png':/하단/.test(location)?'footer.png':[1,2,3,4].includes(gate)?'top.png':'footer.png';
  chooseEvidence(zone);zoom=0;
  const context=loc['일치문구']||loc['주변문맥']||f.evidence||'';
  evidenceText=`${names[gate-1]} · ${name} — ${location||'정확한 발견 위치 미기록'}${context?'\n'+context:''}${loc['라벨근접']==='N'?'\n다른 문맥의 값일 수 있어 확인이 필요합니다.':''}`;
  if(!s.assets.some(a=>assetRegion(a)===zone))evidenceText+='\n해당 위치의 캡처가 없어 다른 저장 화면을 표시합니다.';
  if(!location)evidenceText+='\n단계에 맞는 참고 화면입니다. 발견 위치가 확인된 것은 아닙니다.';
}
function imageScale(){const img=$('#image-area img'),box=$('#image-area');return zoom||(img?.naturalWidth?Math.min(box.clientWidth/img.naturalWidth,box.clientHeight/img.naturalHeight):1);}
function applyZoom(){const img=$('#image-area img'),box=$('#image-area');if(!img?.naturalWidth)return;img.style.width=Math.floor(img.naturalWidth*imageScale())+'px';$('#zoom-label').textContent=zoom?Math.round(zoom*100)+'%':'전체 맞춤';if(!zoom)box.scrollTo(0,0);}
function changeZoom(factor){const box=$('#image-area'),before=imageScale(),x=(box.scrollLeft+box.clientWidth/2)/before,y=(box.scrollTop+box.clientHeight/2)/before;zoom=Math.min(4,Math.max(.1,before*factor));applyZoom();box.scrollLeft=x*zoom-box.clientWidth/2;box.scrollTop=y*zoom-box.clientHeight/2;}
function renderImage(){imageResizeObserver?.disconnect();const box=$('#image-area');renderEvidenceControls();$('#evidence-location').textContent=evidenceText;if(!selectedAsset){box.innerHTML='<div class="missing">저장된 화면 증빙이 없습니다.<br>자동 수집·판단 기록을 확인하거나 재수집을 요청하세요.</div>';return;}
  box.innerHTML=`<img draggable="false" alt="${esc(detail.source.name)} ${assetNames[selectedAsset]}" src="/evidence/${encodeURIComponent(detail.id)}/${selectedAsset}">`;
  const img=box.querySelector('img');img.onload=()=>{box.style.aspectRatio=img.naturalWidth+' / '+img.naturalHeight;applyZoom();imageResizeObserver=new ResizeObserver(()=>applyZoom());imageResizeObserver.observe(box);};img.onerror=()=>{box.innerHTML='<div class="missing">이미지를 불러올 수 없습니다. 증빙 경로를 확인하세요.</div>';};
  let drag;box.onpointerdown=e=>{if(e.button!==0)return;drag={x:e.clientX,y:e.clientY,l:box.scrollLeft,t:box.scrollTop};box.setPointerCapture(e.pointerId);};box.onpointermove=e=>{if(drag){box.scrollLeft=drag.l+drag.x-e.clientX;box.scrollTop=drag.t+drag.y-e.clientY;}};box.onpointerup=box.onpointercancel=()=>drag=null;
  box.onwheel=e=>{if(e.ctrlKey){e.preventDefault();changeZoom(e.deltaY<0?1.15:1/1.15);}};
}
function gather(){let d={checked:$('#checked').checked};if(gate===1)d.url=$('#review-url').value;if(gate===2)d.quality=$('#quality').value;if(gate===3)d.operation=$('#operation').value;
  if(gate===5&&operation()==='있음'){d.fields={};document.querySelectorAll('[data-field]').forEach(card=>d.fields[fieldNames[Number(card.dataset.field)]]={decision:card.querySelector('.decision').value,method:card.querySelector('.method').value,value:card.querySelector('.value').value,evidence:card.querySelector('.evidence').value});}
  if(gate===6&&operation()==='있음'){d.comparisons={};comparisonNames.forEach((n,i)=>d.comparisons[n]=$('#comparison-'+i).value);}
  return d;
}
async function save(action){if(busy)return;busy=true;const buttons=['#draft','#hold','#approve'];buttons.forEach(s=>$(s).disabled=true);
  try{detail=await api('/api/business/'+encodeURIComponent(detail.id)+'/review',{version:detail.version,gate,action,data:gather(),note:$('#review-note').value});dirty=false;
    if(action==='approve'&&gate<8){gate++;focusEvidence();}await refresh();renderDetail();toast(action==='approve'?'승인 내용이 서버에 저장되었습니다.':action==='hold'?'보류 사유를 저장했습니다.':'임시 저장했습니다.');
  }finally{busy=false;buttons.forEach(s=>{if($(s))$(s).disabled=false;});}}
async function setupModal(startPage=1){
  if(!assignment)return;
  const training=await api('/api/training'),m=assignment.meta;
  let current=Number.isInteger(startPage)?Math.min(training.pages.length,Math.max(1,startPage)):1,scale=1;
  openModal(`<h2>점검 준비 · 학습자료</h2><p>홈페이지 운영 현황 → 필요적 게재사항 순으로 학습자료를 확인해 주세요.</p><div class="training-layout"><section class="training-reader"><div class="training-toolbar"><button id="training-prev">← 이전 쪽</button><select id="training-select" aria-label="학습자료 페이지">${training.pages.map(p=>`<option value="${p.page}">${p.printed_page}쪽 · ${esc(p.title)}</option>`).join('')}</select><button id="training-next">다음 쪽 →</button><button id="training-minus" aria-label="학습자료 축소">−</button><button id="training-plus" aria-label="학습자료 확대">＋</button><button id="training-fit">전체 보기</button></div><div id="training-caption" aria-live="polite"></div><div class="training-view"><img id="training-image" alt="학습자료 페이지"><p id="training-error" hidden>미리보기를 불러오지 못했습니다. 아래 원본 PDF 열기를 이용해 주세요.</p></div><div class="training-links"><a href="/api/training/pdf" target="_blank" rel="noopener">원본 PDF 열기 ↗</a><a href="/api/training/pdf" download="학습용 자료.pdf">PDF 내려받기</a></div></section><aside class="training-notes"><h3>점검 순서</h3><ol><li>등록 제호명과 홈페이지 주소(URL) 확인</li><li>홈페이지 유무와 제호명·URL 변경 여부 확인</li><li>홈페이지가 없으면 포털 검색 결과 재확인</li><li>필요적 게재사항 8개 항목의 게재 여부 확인</li><li>등록정보 5개 항목의 일치 여부와 특이사항 확인</li></ol><details class="criteria-note" open><summary>학습자료와 현재 적용 기준</summary><p>화면 용어·점검 안내는 이 PDF를 참고합니다. 판정은 기존 260813 로직 엑셀 기준을 적용합니다.</p><p>홈페이지 점검 대상, 날짜·성명의 대체 인정은 PDF와 다를 수 있습니다. 실제 게재 내용은 원문대로 보존하고 등록정보와의 일치 여부를 별도로 기록합니다.</p></details><dl class="facts">${fact('조사자',m.reviewer)}${fact('화면 대상',m.count+'건')}${fact('자료 기준일',m.base_date)}</dl><label class="checkline"><input id="setup-check" type="checkbox" ${m.setup?.confirmed?'checked':''}>학습자료와 현재 적용 기준, 점검 대상을 확인했습니다.</label><button id="confirm-setup" class="primary">확인하고 점검 시작</button></aside></div>`);
  $('#modal').classList.add('training-modal');$('#modal').addEventListener('close',()=>$('#modal').classList.remove('training-modal'),{once:true});
  const fit=()=>{$('#training-image').style.width=(scale*100)+'%';};
  const render=()=>{const p=training.pages[current-1];$('#training-select').value=String(current);$('#training-caption').textContent=`자료 ${p.printed_page}쪽 · PDF ${current} / ${training.pages.length} · ${p.title}`;$('#training-image').alt=`학습용 자료 ${p.printed_page}쪽: ${p.title}`;$('#training-error').hidden=true;$('#training-image').src=p.image;$('#training-prev').disabled=current===1;$('#training-next').disabled=current===training.pages.length;$('.training-view').scrollTo(0,0);scale=1;fit();};
  $('#training-image').onerror=()=>$('#training-error').hidden=false;
  $('#training-select').onchange=()=>{current=Number($('#training-select').value);render();};$('#training-prev').onclick=()=>{if(current>1){current--;render();}};$('#training-next').onclick=()=>{if(current<training.pages.length){current++;render();}};
  $('#training-plus').onclick=()=>{scale=Math.min(3,scale+.25);fit();};$('#training-minus').onclick=()=>{scale=Math.max(.5,scale-.25);fit();};$('#training-fit').onclick=()=>{scale=1;fit();};render();
  $('#confirm-setup').onclick=safe(async()=>{if(!$('#setup-check').checked){toast('학습자료와 적용 기준을 확인한 뒤 확인란에 체크해 주세요.',true);return;}await api('/api/setup',{confirmed:true});await refresh();$('#modal').close();toast('점검 준비 확인을 저장했습니다.');});
}
function jobModal(){if(!allowLeave())return;const id=detail.id,version=detail.version;openModal(`<h2>추가 확인 요청</h2><p>${esc(detail.source.name)} · #${esc(id)}<br>요청을 저장하면 관련 승인과 최종 승인이 해제됩니다. 요청은 대기열에 저장되며 자동 실행되지 않습니다.</p><select id="job-stage" aria-label="추가 확인 종류">${options([['capture','홈페이지 화면 다시 수집'],['search','포털 검색'],['review','필요적 게재사항 다시 점검'],['pubdate','발행연월일 다시 확인'],['annotate','화면 표시 다시 생성']],'')}</select><label class="form-label" for="job-reason">요청 이유 / 후보 URL</label><textarea id="job-reason" placeholder="부족한 증거, 수정할 항목, 새 주소 등을 기록하세요."></textarea><div class="actions"><button id="send-job" class="primary">추가 확인 요청 저장</button></div>`);
  $('#send-job').onclick=safe(async()=>{detail=await api('/api/business/'+encodeURIComponent(id)+'/jobs',{version,stage:$('#job-stage').value,reason:$('#job-reason').value});dirty=false;await refresh();gate=nextGate();renderDetail();$('#modal').close();toast('추가 확인 대기열에 저장했습니다.');});}
function jobsModal(){openModal(`<h2>추가 확인 요청 대기열</h2><p>최근 100개 요청입니다. 요청 파일을 내려받아 점검 담당자에게 전달할 수 있습니다. 요청은 저장만 되며, 실제 추가 점검은 아직 자동으로 실행되지 않습니다.</p>${assignment.jobs.map(j=>`<div class="job-row"><b>#${esc(j.media_id)}</b><span class="badge">${esc(j.stage)} · 대기</span><br>${esc(j.reason)}<small>${esc(new Date(j.created_at).toLocaleString('ko-KR'))}</small></div>`).join('')||'<p>저장된 추가 확인 요청이 없습니다.</p>'}<div class="actions"><button id="download-jobs">요청 JSON 내려받기</button></div>`);$('#download-jobs').onclick=()=>location.href='/api/jobs/export';}
async function download(mode){const r=await fetch('/api/export?mode='+mode);if(!r.ok){const d=await r.json();throw new Error(d.error);}const blob=await r.blob();const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download=mode==='complete'?'점검완료본.csv':'점검중_초안.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
$('#export').onclick=()=>{openModal('<h2>점검 결과 내보내기</h2><p>초안에는 현재 계정의 조회 범위의 AI 점검 결과과 점검 상태·항목별 기록이 함께 포함됩니다. 완료본은 배정된 모든 매체의 최종 승인이 끝나야 출력됩니다.</p><div class="actions"><button id="export-draft">전체 담당 범위 초안 CSV</button><button id="export-complete" class="primary">전체 담당 범위 완료본 CSV</button></div>');$('#export-draft').onclick=safe(()=>download('draft'));$('#export-complete').onclick=safe(()=>download('complete'));};
$('#nav-setup').onclick=safe(()=>setupModal());$('#start-setup').onclick=safe(()=>setupModal());$('#nav-jobs').onclick=()=>assignment&&jobsModal();$('#nav-work').onclick=()=>$('#workspace').scrollIntoView({behavior:'smooth'});
$('#search').oninput=()=>{page=1;renderList();};$('#state-filter').onchange=$('#issue-filter').onchange=()=>{page=1;renderList();};
$('#prev-page').onclick=()=>{page--;renderList();};$('#next-page').onclick=()=>{page++;renderList();};
$('#refresh').onclick=safe(async()=>{if(!allowLeave())return;const id=detail?.id;await refresh();if(id)await selectBusiness(id);});
window.addEventListener('beforeunload',e=>{if(dirty){e.preventDefault();e.returnValue='';}});
safe(async()=>{
  identity=await api('/api/me');
  const tools=document.createElement('div');tools.className='account-actions';
  tools.innerHTML=(identity.role==='admin'?'<button id="go-admin">관리자</button>':'')+'<button id="my-password">비밀번호 변경</button><button id="my-logout">로그아웃</button>';
  document.querySelector('header').append(tools);
  if($('#go-admin'))$('#go-admin').onclick=()=>{if(allowLeave())location.href='/admin';};
  $('#my-logout').onclick=safe(async()=>{if(!allowLeave())return;await api('/api/logout',{});location.href='/login';});
  $('#my-password').onclick=()=>{openModal('<h2>비밀번호 변경</h2><label class="form-label" for="current-password">현재 비밀번호</label><input id="current-password" type="password" autocomplete="current-password"><label class="form-label" for="new-own-password">새 비밀번호 (12자 이상)</label><input id="new-own-password" type="password" autocomplete="new-password"><div class="actions"><button id="change-own-password" class="primary">변경 후 다시 로그인</button></div>');$('#change-own-password').onclick=safe(async()=>{await api('/api/password',{current_password:$('#current-password').value,new_password:$('#new-own-password').value});location.href='/login';});};
  await refresh(true);
})().catch(()=>{});
