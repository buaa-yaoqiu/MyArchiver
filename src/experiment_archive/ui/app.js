const $ = (selector, parent = document) => parent.querySelector(selector);
const E = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt = number => Number(number || 0).toLocaleString('zh-CN');
const labels = {valid:'有效记录',unknown:'尚未复核',needs_review:'待复核',invalid:'无效记录',retracted:'已撤回',superseded:'已替代',current:'当前版本',development:'开发证据',frozen_config_evaluation:'冻结配置评估',exploratory_diagnostic:'探索诊断',paper_main:'论文主评测',extension:'扩展实验',reported_pass:'报告记载通过',pass:'通过',static:'静态预测',strict_causal_pre:'严格因果 · 更新前',same_frame_post:'同帧 · 更新后',frozen_future:'冻结后未来预测',domain:'数据域',sequence:'序列',paper_split:'官方测试清单',case_proxy_group:'病例代理分组',time_quarter:'时间分段',historical_note:'历史原文',paired_tta_screen:'配对实验',static_reproduction:'静态复现',dataset_identity_audit:'数据审计',hyperparameter_search:'参数搜索',classification:'分类实验',synthetic_demo:'接口示例'};
const L = value => labels[value] || value || '未记录';
Object.assign(labels,{cancelled:'已取消',completed:'已完成',completed_subset:'已完成子集',running:'运行中',failed:'失败',pending:'待处理',inconclusive:'结论未定',supported:'证据支持',not_supported:'证据不支持'});
const badge = (value, label) => `<span class="badge ${E(value)}">${E(label || L(value))}</span>`;
const date = value => { const parsed = new Date(value); return Number.isNaN(parsed.getTime()) ? String(value || '').slice(0,10) : parsed.toLocaleString('zh-CN',{hour12:false}); };
const revisionDate = value => String(value || '').slice(0,10);
const state = {page:'library',status:null,offset:0,library:{q:'',domain:'',validity:'',structured:false,sort:'time_desc'},detail:null,detailTab:'overview',tableKind:'metrics',tableOffset:0,tableFilters:{},searchQuery:'',searchSort:'relevance',searchResults:null,jobs:[],activeJob:false,request:0};
state.translation=null;state.translationPending=null;state.translationCompare=false;
let token = location.hash.slice(1) || sessionStorage.getItem('archive-token');
if (location.hash) { sessionStorage.setItem('archive-token',token); history.replaceState(null,'',location.pathname); }

async function api(path, body) {
  const response = await fetch(path,{method:body === undefined ? 'GET':'POST',headers:{Authorization:`Bearer ${token || ''}`,...(body === undefined ? {}:{'Content-Type':'application/json'})},body:body === undefined ? undefined:JSON.stringify(body)});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || '操作未完成');
  return result;
}
function toast(message) { const box = $('#toast'); box.textContent=message;box.classList.add('visible');clearTimeout(toast.timer);toast.timer=setTimeout(()=>box.classList.remove('visible'),5000); }
function errorPanel(error) { return `<div class="error-page"><h2>暂时无法读取</h2><div>${E(error.message || error)}</div><p>可以重新加载，或检查归档与模型状态。</p><button class="button small" data-action="retry">重新加载</button></div>`; }
function empty(title,message) { return `<div class="empty"><div class="empty-symbol">▤</div><h3>${E(title)}</h3><p>${E(message)}</p></div>`; }
function options(values, selected, all='全部') { return `<option value="">${E(all)}</option>`+values.map(v=>`<option value="${E(v)}" ${v===selected?'selected':''}>${E(L(v))}</option>`).join(''); }
function loading() { return '<div class="loading"><span class="spinner"></span>正在读取本地归档…</div>'; }
function sortOptions(selected,relevance) {
  return [...(relevance?[['relevance','按相关性排序']]:[]),['time_desc','按时间倒序 · 最新优先'],['time_asc','按时间正序 · 最早优先']].map(([value,label])=>`<option value="${value}" ${value===selected?'selected':''}>${label}</option>`).join('');
}
function timeBasis(value){return ({experiment_id:'日期来自实验编号',title:'日期来自记录标题',archive_revision:'未记录实验日期，使用归档版本时间',unknown:'时间未记录'})[value]||`日期字段：${value}`;}
function lifecycleHTML(value={}) {
  return [['run','运行'],['evaluation','评价'],['conclusion','结论']].map(([key,label])=>infoRow(`${label}状态`,!value[key]||value[key]==='unknown'?'未记录':L(value[key]))).join('');
}
function weightIdentity(evaluation){const current=evaluation.checkpoint_sha256||evaluation.weights_sha256||evaluation.weight_identity||evaluation.checkpoint_id||'未记录';return `当前：${current}；起始：${evaluation.source_checkpoint_sha256||'未记录'}`;}
function enhanceSearchResults(result){
  document.querySelectorAll('.result-card').forEach((card,i)=>{
    const hit=result.results[i];
    card.querySelector('.result-top').insertAdjacentHTML('afterend',`<div class="result-context"><div class="chips"><span class="chip" title="${E(timeBasis(hit.time_basis))}">${E(hit.time||'时间未记录')}${hit.time_basis==='archive_revision'?' · 归档时间':''}</span><span class="chip">相关性分数 ${Number(hit.score).toFixed(4)}</span><span class="chip">${E(L(hit.benchmark_scope))}</span></div>${lifecycleHTML(hit.lifecycle)}${(hit.comparison_context||[]).map(context=>`<div class="comparison-context"><strong>${E(context.domain||'未记录数据域')} · ${E(L(context.scope_type))} · ${E(context.scope_id||'未记录范围')}</strong><div>帧数：${E(context.n_frames??'未记录')} · 预测时机：${E(L(context.prediction_phase||context.candidate.prediction_phase))}</div><div>方法：${E(context.candidate.variant_id||'未记录')} · 基线：${E(context.baseline.variant_id||'未记录')}</div><div>权重身份：${E(weightIdentity(context.candidate))}</div></div>`).join('')}${(hit.limitations||[]).slice(0,2).map(x=>`<p class="provenance-note">限制：${E(x)}</p>`).join('')}<div class="provenance-note">比较条件最多展示 4 组；完整条件与核验依据请查看记录。</div></div>`);
  });
}
async function choosePath(button){
  button.disabled=true;
  try{
    const selected=await api('/api/paths/pick',{kind:button.dataset.kind,initial:button.dataset.initial||''});
    if(!selected.path)return;
    await api('/api/paths',{target:button.dataset.pick,id:button.dataset.id,path:selected.path});
    if(button.dataset.pick==='archive'){state.searchResults=null;state.searchQuery='';state.detail=null;state.offset=0;state.library={q:'',domain:'',validity:'',structured:false,sort:'time_desc'};}
    await refreshStatus();await renderSettings();toast('路径已保存');
  }catch(error){toast(error.message);}finally{button.disabled=false;}
}
function enhanceSyncPlans(){
  document.querySelectorAll('.job').forEach((element,i)=>{
    const result=state.jobs[i]?.result;
    if(state.jobs[i]?.kind==='accept'&&result){element.insertAdjacentHTML('beforeend',`<div class="notice ${result.status==='pass'?'':'warning'}">验收结果：${E({pass:'本地检查通过',needs_review:'需要复核',failed:'未通过'}[result.status]||result.status)} · 问题 ${result.issues?.length||0} · 待复核项 ${result.warnings||0}。该回执仅检查归档与声明，不代表整套实验通过。</div>`);}
    if(!result?.details)return;
    const names={new_record:'新增记录',metrics:'指标变化',body:'正文变化',metadata:'元数据变化',location_only:'仅位置或所在文件哈希变化',sources:'来源变化',unchanged:'无变化'};
    element.insertAdjacentHTML('beforeend',`<div class="sync-preview"><strong>预计新增 ${result.counts.new||0} · 更新 ${result.counts.update||0} · 复用 ${result.counts.reuse||0}</strong>${result.details.map(task=>`<details><summary>${E(task.id)} · 逐条变更原因</summary>${task.records.map(row=>`<p><code>${E(row.experiment_id)}</code> · ${row.action==='reuse'?'复用原版本':row.action==='new'?'新增':'更新'} · ${row.changes.map(x=>E(names[x]||x)).join('、')}</p>`).join('')}</details>`).join('')}</div>`);
  });
}

async function refreshStatus() {
  state.status = await api('/api/status');
  const s=state.status,index=s.index;
  $('#nav-count').textContent=fmt(s.experiment_count);
  $('#sidebar-state').textContent=`${fmt(s.record_count)} 条归档记录`;
  $('#index-state').classList.toggle('warn',!index.exists||index.stale);
  $('#index-state').innerHTML=`<span class="status-dot"></span>${index.exists ? (index.stale?'索引需要更新':'本地索引就绪'):'尚未建立索引'}`;
  $('#sync-button').disabled=!s.sync_available||state.activeJob;
  $('#verify-button').disabled=state.activeJob;
}

function setNavigation(page) {
  state.page=page;
  document.querySelectorAll('.nav-item').forEach(item=>item.classList.toggle('active',item.dataset.page===page));
  $('#breadcrumb').textContent={library:'实验归档',search:'语义检索',jobs:'运行任务',settings:'来源与接入',detail:'实验详情'}[page];
}
async function navigate(page) {
  setNavigation(page); ++state.request;
  try {
    if(page==='library') renderLibrary();
    else if(page==='search') renderSearch();
    else if(page==='jobs') await renderJobs();
    else if(page==='settings') await renderSettings();
  } catch(error) { $('#content').innerHTML=errorPanel(error); }
}

function stats() {
  const s=state.status;
  const entries=[['实验条目',s.experiment_count,'同一实验的关联记录已合并','▦'],['结构化指标',s.metric_count,'保留原报告的数值精度','⌁'],['基线比较',s.comparison_count,'差值、相对变化与置信区间','⇄'],['检索片段',s.index.chunks||0,s.index.stale?'来源已更新，请刷新索引':'本地向量与关键词混合检索','⌕']];
  return `<div class="stats">${entries.map(([title,value,note,symbol])=>`<div class="stat"><div class="stat-label">${title}</div><span class="stat-symbol">${symbol}</span><div class="stat-value">${fmt(value)}</div><div class="stat-note">${E(note)}</div></div>`).join('')}</div>`;
}
function renderLibrary() {
  const filter=state.library;
  $('#content').innerHTML=`<div class="heading-row"><div><div class="eyebrow">RESEARCH COLLECTION</div><h1>让每一次实验，都有迹可循。</h1><p class="subtitle">汇集实验记录、指标与证据，在这里找到下一步的依据。</p></div><button class="button" data-page="search">⌕ 自然语言检索</button></div>${stats()}
  <div class="section-heading"><h2>实验归档 <span class="count" id="library-count"></span></h2><span class="section-meta">本地文件 · 版本可追溯</span></div>
  <div class="library-toolbar"><div class="filter-search"><input id="library-query" value="${E(filter.q)}" placeholder="筛选标题、编号或摘要…" aria-label="筛选实验"></div><select id="library-domain" aria-label="数据域">${options(state.status.domains,filter.domain,'所有数据域')}</select><select id="library-validity" aria-label="记录有效性">${options(['valid','needs_review','unknown','invalid'],filter.validity,'所有状态')}</select><div class="toolbar-spacer"></div><label class="toggle"><input id="library-structured" type="checkbox" ${filter.structured?'checked':''}>仅含结构化指标</label></div><div id="library-results">${loading()}</div>`;
  let timer;
  $('#library-query').addEventListener('input',e=>{filter.q=e.target.value;state.offset=0;clearTimeout(timer);timer=setTimeout(loadLibrary,220);});
  $('#library-domain').addEventListener('change',e=>{filter.domain=e.target.value;state.offset=0;loadLibrary();});
  $('#library-validity').addEventListener('change',e=>{filter.validity=e.target.value;state.offset=0;loadLibrary();});
  $('#library-structured').addEventListener('change',e=>{filter.structured=e.target.checked;state.offset=0;loadLibrary();});
  $('.library-toolbar').insertAdjacentHTML('beforeend',`<select id="library-sort" aria-label="归档排序">${sortOptions(filter.sort,false)}</select>`);
  $('#library-sort').addEventListener('change',e=>{filter.sort=e.target.value;state.offset=0;loadLibrary();});
  loadLibrary();
}
async function loadLibrary() {
  if(state.page!=='library')return;
  const request=++state.request;
  try {
    const q=new URLSearchParams({...state.library,offset:state.offset,limit:12});
    const result=await api('/api/experiments?'+q);
    if(request!==state.request||state.page!=='library')return;
    $('#library-count').textContent=`${fmt(result.total)} 条`;
    $('#library-results').innerHTML=result.items.length ? `<div class="cards">${result.items.map(card=>`
      <button class="experiment-card" data-open="${E(card.id)}"><div class="card-top"><span class="card-icon">${card.has_metrics?'⌁':'▤'}</span>${badge(card.claim_status==='current'?card.validity:card.claim_status)}</div><h3>${E(card.title)}</h3><p class="card-excerpt">${E(card.excerpt||'记录方法、结果与适用边界，查看完整证据。')}</p><div class="chips">${card.domains.map(d=>`<span class="chip">${E(d)}</span>`).join('')||`<span class="chip">${E(L(card.kind))}</span>`}</div><div class="card-bottom"><span title="${E(timeBasis(card.time_basis))}">${E(revisionDate(card.time)||'时间未记录')}${card.time_basis==='archive_revision'?' · 归档时间':''}</span><strong>${card.metric_count?`${fmt(card.metric_count)} 项指标`:'原文记录'}</strong><span class="card-arrow">↗</span></div></button>`).join('')}</div>${paginationHTML(result,'library')}` : empty('没有匹配的实验','试试其他关键词或放宽筛选条件。');
  }catch(error){if(state.page==='library')$('#library-results').innerHTML=errorPanel(error);}
}
function paginationHTML(result,kind) { return `<div class="pagination"><span>显示 ${result.total?result.offset+1:0}–${Math.min(result.offset+result.limit,result.total)}，共 ${fmt(result.total)} 条</span><div class="pagination-controls"><button class="button small" data-paging="${kind}" data-delta="-1" ${result.offset===0?'disabled':''}>← 上一页</button><button class="button small" data-paging="${kind}" data-delta="1" ${result.offset+result.limit>=result.total?'disabled':''}>下一页 →</button></div></div>`; }

function renderSearch() {
  $('#content').innerHTML=`<div class="heading-row"><div><div class="eyebrow">FIND YOUR EVIDENCE</div><h1>用问题，找到实验。</h1><p class="subtitle">从方法、现象或失败原因出发，检索有来源的研究记录。</p></div></div><div class="search-hero"><h2>你想从历史实验中了解什么？</h2><p>支持中文语义检索，也可以精确搜索编号和关键词。</p><form class="semantic-form" id="search-form"><input id="semantic-query" value="${E(state.searchQuery)}" placeholder="例如：哪些方法出现了负迁移，原因是什么？" aria-label="自然语言检索问题" required><button class="button primary" id="search-submit">⌕ 检索归档</button></form><div class="search-options"><select id="search-mode" aria-label="检索模式"><option value="hybrid">语义 + 关键词</option><option value="semantic">语义检索</option><option value="lexical">仅关键词</option></select><select id="search-domain" aria-label="检索数据域">${options(state.status.domains,'','所有数据域')}</select><label class="toggle"><input type="checkbox" id="search-retracted">包含已撤回记录</label></div><div class="suggestions">${['哪些实验出现了负迁移？','基础模型复现结果','数据身份和配对更正'].map(text=>`<button class="suggestion" data-suggest="${E(text)}">${text}</button>`).join('')}</div></div><div id="search-results">${state.searchResults ? renderSearchResults(state.searchResults):empty('从一个研究问题开始','检索会返回相关记录与来源。相关性不等于科学有效性，请结合评价口径和复核状态解读。')}</div>`;
  $('#search-form').addEventListener('submit',e=>{e.preventDefault();runSearch();});
  $('.search-options').insertAdjacentHTML('beforeend',`<select id="search-sort" aria-label="检索结果排序">${sortOptions(state.searchSort,true)}</select>`);
  $('#search-sort').addEventListener('change',e=>{state.searchSort=e.target.value;if($('#semantic-query').value.trim())runSearch();});
  if(state.searchResults)enhanceSearchResults(state.searchResults);
}
async function runSearch() {
  const query=$('#semantic-query').value.trim();if(!query)return;
  state.searchQuery=query;
  const request=++state.request;
  const button=$('#search-submit');button.disabled=true;button.textContent='正在检索…';
  $('#search-results').innerHTML=loading();
  try{
    const result=await api('/api/search',{query,mode:$('#search-mode').value,domain:$('#search-domain').value,include_retracted:$('#search-retracted').checked,sort:state.searchSort});
    if(state.page!=='search'||request!==state.request)return;
    state.searchResults=result;
    $('#search-results').innerHTML=renderSearchResults(result);
    enhanceSearchResults(result);
  }catch(error){if(state.page==='search')$('#search-results').innerHTML=errorPanel(error);}
  finally{if(state.page==='search'){button.disabled=false;button.textContent='⌕ 检索归档';}}
}
function renderSearchResults(result) {
  return `<div class="section-heading"><h2>相关实验 <span class="count">${result.results.length} 条结果</span></h2><span class="section-meta">${Number(result.seconds||0).toFixed(2)} 秒 · ${result.mode==='lexical'?'关键词':result.mode==='semantic'?'语义检索':'混合检索'}</span></div><div class="notice">${result.sort&&result.sort!=='relevance'?'当前按时间排序，分数仍表示相关性。':'当前按相关性排序。'}历史原文可能尚未复核；判断收益前，请查看对应指标、基线和评价条件。${result.excluded.retracted_or_invalid?` 已过滤 ${result.excluded.retracted_or_invalid} 条撤回或无效候选。`:''}</div>${result.results.length?result.results.map(hit=>`<article class="result-card"><div class="result-top"><h3>${E(hit.title)}</h3>${badge(hit.claim_status==='current'?hit.evidence_validity:hit.claim_status)}</div><div class="result-snippet">${E(hit.snippet)}</div><div class="result-foot"><span>${hit.domains.map(E).join(' · ')} · ${E(L(hit.evidence_role))}</span><button class="text-button" data-open="${E(hit.experiment_id)}">查看完整记录 ↗</button></div>${hit.matched_record_id!==hit.experiment_id?'<div class="provenance-note">命中关联历史片段；下方入口指向同一实验的主记录，原片段的复核状态独立保留。</div>':''}</article>`).join(''):empty('没有找到相关记录','可以切换到关键词检索，或尝试更具体的方法名。')}`;
}

async function openDetail(identity,revision) {
  setNavigation('detail');const request=++state.request;
  $('#content').innerHTML=loading();
  try{
    const q=new URLSearchParams({id:identity,...(revision?{revision}:{})});
    const detail=await api('/api/experiment?'+q);
    if(request!==state.request)return;
    state.detail=detail;state.detailTab='overview';state.tableOffset=0;state.translation=null;state.translationPending=null;state.translationCompare=false;state.translationProvider=state.status.translation?.config?.provider||'local';
    const filters=detail.filters;
    state.tableFilters={domain:filters.domain[0]||'',scope_type:filters.scope_type.includes('domain')?'domain':filters.scope_type[0]||'',scope_id:'',metric:filters.metric[0]||'',variant:''};
    renderDetail();
  }catch(error){$('#content').innerHTML=errorPanel(error);}
}
function renderDetail() {
  const d=state.detail,m=d.metadata;
  $('#content').innerHTML=`<button class="back-button" data-page="library">← 返回实验归档</button><div class="heading-row"><div><div class="eyebrow">EXPERIMENT RECORD</div><h1 class="detail-title">${E(m.title)}</h1><div class="detail-meta">${badge(m.claim_status==='current'?m.evidence_validity:m.claim_status)}<span class="divider"></span><span>${E(L(m.kind))}</span><span class="divider"></span><span>${E(L(m.evidence_role))}</span><span class="divider"></span><span>${revisionDate(d.revision)}</span></div></div><button class="button small" data-copy="${E(d.id)}">复制编号</button></div><div class="tabs">${[['overview','实验概览'],['metrics','指标与比较'],['sources','证据来源'],['history','版本记录']].map(([key,label])=>`<button class="tab ${state.detailTab===key?'active':''}" data-tab="${key}">${label}${key==='history'?` · ${d.history.length}`:''}</button>`).join('')}</div><div id="detail-body"></div>`;
  renderDetailBody();
}
function renderDetailBody() {
  const d=state.detail,m=d.metadata,body=$('#detail-body');
  if(state.detailTab==='overview'){
    body.innerHTML=`${m.evidence_validity==='unknown'||m.evidence_validity==='needs_review'?'<div class="notice warning">本记录仍需复核。原文中的运行状态、数值或解释可能已有后续更正，请结合证据来源和版本记录阅读。</div>':''}${translationControls()}<div class="detail-grid"><article class="panel document">${translatedNote()}</article><aside><div class="info-card"><h3>记录信息</h3><div class="chips">${(m.domains||[]).map(x=>`<span class="chip">${E(x)}</span>`).join('')}</div>${infoRow('证据角色',L(m.evidence_role))}${infoRow('评价范围',L(m.benchmark_scope))}${infoRow('审计状态',L(m.audit_status))}${infoRow('结构化指标',`${fmt(d.metric_count)} 条`)}${infoRow('基线比较',`${fmt(d.comparison_count)} 条`)}</div><div class="info-card"><h3>解释边界</h3>${(m.limitations||['结论仅适用于记录中声明的配置与评价集合。']).map(x=>`<p class="provenance-note">${E(x)}</p>`).join('')}</div></aside></div>`;
    const info=$('.info-card',body);if(info)info.insertAdjacentHTML('beforeend',lifecycleHTML(m.lifecycle||{run:m.run_status,evaluation:m.evaluation_status,conclusion:m.conclusion_status}));
  }else if(state.detailTab==='metrics') renderMetrics();
  else if(state.detailTab==='sources'){
    body.innerHTML=`<div class="notice">下列来源是归档时保存的证据引用。归档完整性校验不会重新运行实验，也不代表服务器上的原始预测已重新核验。</div>${(m.source_refs||[]).map(source=>`<article class="source-card"><div class="section-heading"><h3>${E(source.id||'来源记录')}</h3><button class="text-button" data-copy="${E(source.path||source.description||'')}">复制路径</button></div><div class="source-path">${E(source.path||source.description||'未记录路径')}</div>${source.heading?`<p class="provenance-note">章节：${E(source.heading)}</p>`:''}${source.start_line?`<p class="provenance-note">原记录行号 ${source.start_line}–${source.end_line||'末尾'}（仅辅助定位）</p>`:''}<div class="source-sha">文件 SHA256<br>${E(source.sha256||'未记录')} ${source.section_sha256?`<br><br>章节 SHA256<br>${E(source.section_sha256)}`:''}</div></article>`).join('')}<details class="panel"><summary>配置与元数据</summary><pre class="json-block">${E(JSON.stringify(m,null,2))}</pre></details>`;
  }else{
    body.innerHTML=`<div class="notice">每次变更保留为独立版本；查看旧版本不会覆盖当前记录。</div>${[...d.history].reverse().map((revision,index)=>`<div class="timeline-item"><span class="timeline-dot"></span><div class="timeline-content"><div class="timeline-title"><strong>${date(revision.created_at)}</strong>${revision.revision===d.revision?badge('current','正在查看'):`<button class="text-button" data-revision="${E(revision.revision)}">查看此版本 →</button>`}</div><p>${E(revision.reason)}</p><code>${E(revision.revision)}</code></div></div>`).join('')}`;
  }
}
function infoRow(label,value) { return `<div class="info-row"><span>${E(label)}</span><strong>${E(value)}</strong></div>`; }

function providerOptions(selected) {
  return [['local','本地专用模型 · 离线'],['google','联网 Google · 发送本条文本'],['ollama','本地 Ollama']].map(([key,title])=>`<option value="${key}" ${key===selected?'selected':''}>${title}</option>`).join('');
}
function translationControls() {
  const config=state.status.translation?.config||{},t=state.translation;
  return `<section class="translation-bar"><div><strong>阅读语言</strong><span class="translation-caption">原文保留，译文单独缓存</span></div><select id="translation-provider" aria-label="翻译后端">${providerOptions(state.translationProvider||config.provider||'local')}</select><button class="button small" data-translate="original">原文</button><button class="button small" data-translate="zh-CN" ${state.translationPending?'disabled':''}>中文译文</button><button class="button small" data-translate="en" ${state.translationPending?'disabled':''}>English</button>${t?`<label class="toggle"><input id="translation-compare" type="checkbox" ${state.translationCompare?'checked':''}>对照原文</label>`:''}${state.translationPending?'<span class="translation-caption"><span class="spinner"></span>正在翻译，可切换到任务页查看</span>':''}</section>${t?`<div class="notice translation-notice">机器译文 · ${E(t.backend.provider==='local'?'本地专用模型':t.backend.provider==='ollama'?'本地 Ollama':'联网 Google')} · ${t.cached?'已复用缓存':'已生成并缓存'}。科研结论请核对原文，指标表和证据引用保持原样。${t.untranslated_blocks?` 有 ${t.untranslated_blocks} 个段落未通过保护校验，已保留原文。`:''}</div>`:''}`;
}
function translatedNote() {
  const t=state.translation,d=state.detail;
  if(!t||t.experiment_id!==d.id||t.revision!==d.revision)return markdown(d.note);
  const translated=`<div class="translated-title">${E(t.title)}</div>${markdown(t.note)}`;
  return state.translationCompare?`<div class="bilingual"><section><h3 class="bilingual-label">原文</h3>${markdown(d.note)}</section><section><h3 class="bilingual-label">${t.target_language==='en'?'English · 机器译文':'中文 · 机器译文'}</h3>${translated}</section></div>`:translated;
}
async function translateDetail(target) {
  if(target==='original'){state.translation=null;state.translationCompare=false;renderDetailBody();return;}
  try{
    const provider=state.translationProvider||$('#translation-provider')?.value||'local';
    const job=await api('/api/jobs',{kind:'translate',experiment_id:state.detail.id,revision:state.detail.revision,target,provider});
    state.translationPending=job.id;state.activeJob=true;renderDetailBody();await refreshStatus();pollJobs();
  }catch(error){toast(error.message);}
}
function translationSettingsPanel() {
  const config=state.status.translation?.config||{};
  return `<section class="panel translation-settings"><h2>翻译与阅读语言</h2><p class="setting-value">默认用本地中英专用模型离线翻译。只有选择联网后端并点击翻译时，才会发送所选记录的标题和正文；不会发送数据集、权重或整份CSV。英文是否节省token取决于具体模型，不自动替换原文。</p><form id="translation-settings-form"><div class="translation-settings-grid"><label>默认翻译后端<select id="translation-default-provider">${providerOptions(config.provider||'local')}</select></label><label>本地 Ollama 地址<input id="translation-ollama-url" value="${E(config.ollama_url||'http://127.0.0.1:11434')}"></label><label>本地 Ollama 模型<input id="translation-ollama-model" value="${E(config.ollama_model||'qwen2.5:3b')}"></label><button class="button primary small" type="submit">保存翻译设置</button></div></form><p class="provenance-note">本地专用模型首次需显式下载安装，之后无需联网。Ollama须预先启动并安装模型，App不会自动执行模型下载。公共联网接口可能限流；无法保留受保护数值或标识时会拒绝译文。</p></section>`;
}

function markdown(text) {
  const lines=text.split('\n');let result='',code=false,table=false,list=false;
  function closeTable(){if(table){result+='</tbody></table></div>';table=false;}}
  function closeList(){if(list){result+='</ul>';list=false;}}
  for(let i=0;i<lines.length;i++){
    const line=lines[i];
    if(line.startsWith('```')){closeTable();closeList();result+=code?'</code></pre>':'<pre><code>';code=!code;continue;}
    if(code){result+=E(line)+'\n';continue;}
    if(line.startsWith('|')){
      closeList();const cells=line.trim().replace(/^\||\|$/g,'').split('|').map(x=>x.trim());
      if(cells.every(x=>/^:?-+:?$/.test(x)))continue;
      if(!table){result+='<div class="md-table"><table><tbody>';table=true;}
      result+='<tr>'+cells.map(cell=>`<td>${inline(cell)}</td>`).join('')+'</tr>';continue;
    }
    closeTable();
    const heading=line.match(/^(#{1,3})\s+(.*)$/);
    if(heading){closeList();const level=heading[1].length;result+=`<h${level}>${inline(heading[2])}</h${level}>`;}
    else if(/^[-*]\s/.test(line)){if(!list){result+='<ul>';list=true;}result+=`<li>${inline(line.slice(2))}</li>`;}
    else{closeList();if(line.startsWith('>'))result+=`<blockquote>${inline(line.slice(1))}</blockquote>`;else if(line.trim())result+=`<p>${inline(line)}</p>`;}
  }
  closeTable();closeList();if(code)result+='</code></pre>';return result;
}
function inline(text) { return E(text).replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>').replace(/`([^`]+)`/g,'<code class="code-inline">$1</code>'); }

function renderMetrics() {
  const d=state.detail,f=state.tableFilters;
  if(!d.metric_count&&!d.comparison_count){$('#detail-body').innerHTML=empty('此记录暂未提取结构化指标','原文仍可在实验概览中阅读。只有已明确解析的指标才会进入表格，不会从文本中猜测分数。');return;}
  $('#detail-body').innerHTML=`<div class="table-toolbar"><select id="table-kind" aria-label="数值表类型"><option value="metrics" ${state.tableKind==='metrics'?'selected':''}>绝对指标</option><option value="comparisons" ${state.tableKind==='comparisons'?'selected':''}>基线比较</option></select>${[['domain','数据域',d.filters.domain],['scope_type','统计层级',d.filters.scope_type],['scope_id','统计范围',d.filters.scope_id],['metric','指标',d.filters.metric],['variant','方法',d.variants]].map(([key,label,values])=>`<select data-table-filter="${key}" aria-label="${label}">${options(values,f[key],`所有${label}`)}</select>`).join('')}<div class="toolbar-spacer"></div><button class="button small" data-action="export">↓ 导出 CSV</button></div><div id="table-content">${loading()}</div><div id="chart-content"></div>`;
  $('#table-kind').addEventListener('change',e=>{state.tableKind=e.target.value;state.tableOffset=0;loadTable();});
  document.querySelectorAll('[data-table-filter]').forEach(select=>select.addEventListener('change',e=>{state.tableFilters[e.target.dataset.tableFilter]=e.target.value;state.tableOffset=0;loadTable();}));
  loadTable();
}
function tableQuery() { return new URLSearchParams({id:state.detail.id,revision:state.detail.revision,kind:state.tableKind,...state.tableFilters,offset:state.tableOffset,limit:20}); }
async function loadTable() {
  const request=++state.request;
  try{
    const result=await api('/api/table?'+tableQuery());
    if(request!==state.request||state.page!=='detail'||state.detailTab!=='metrics')return;
    const comparisons=state.tableKind==='comparisons';
    const columns=comparisons?[['candidate_variant','方法'],['baseline_variant','基线'],['domain','数据域'],['scope_id','范围'],['metric','指标'],['delta','差值 Δ'],['relative_percent','相对变化 %'],['ci','95% CI Δ'],['direction','判断']]:[['variant_id','方法'],['domain','数据域'],['scope_id','范围'],['metric','指标'],['value','绝对值'],['unit','单位'],['n_frames','有效帧数']];
    $('#table-content').innerHTML=result.rows.length?`<div class="panel table-panel"><div class="table-wrap"><table class="data-table"><thead><tr>${columns.map(([,label])=>`<th>${label}</th>`).join('')}</tr></thead><tbody>${result.rows.map(row=>`<tr>${columns.map(([key])=>{
      const numeric=['delta','relative_percent','value'].includes(key);let value=row[key]||'—';
      if(key==='ci')value=row.ci_low!==''&&row.ci_high!==''?`[${row.ci_low}, ${row.ci_high}]`:'—';
      if(key==='direction')value={improved:'区间支持改善',worsened:'区间支持恶化',includes_zero_not_equivalence:'CI 包含 0',not_available:'未报告 CI',unknown_metric_direction:'未声明方向'}[row.ci_direction]||'未记录';
      const color=comparisons&&['delta','relative_percent','direction'].includes(key)?(key==='direction'?(row.ci_direction||''):(row.numeric_direction||'')):'';
      return `<td class="${numeric?'number ':''}${E(color)}">${E(value)}${['variant_id','candidate_variant'].includes(key)?`<span class="phase">${E(L(row.prediction_phase))}</span>`:''}</td>`;
    }).join('')}</tr>`).join('')}</tbody></table></div></div>${paginationHTML(result,'table')}`:empty('当前筛选下没有指标','可以放宽方法、统计范围或指标筛选。');
    $('#chart-content').innerHTML=!comparisons?chart(result):'<div class="notice">差值为候选方法减基线；方向依据项目声明的指标定义。CI 包含 0 不代表等效，数值改善也不自动构成独立泛化或因果证据。</div>';
  }catch(error){if(state.detailTab==='metrics')$('#table-content').innerHTML=errorPanel(error);}
}
function chart(result) {
  const rows=result.rows.filter(row=>row.value_status==='valid'&&Number.isFinite(Number(row.value)));
  if(!rows.length)return '';
  const groups=new Set(rows.map(r=>[r.domain,r.scope_type,r.scope_id,r.metric,r.unit].join('|')));
  if(groups.size!==1)return '<div class="notice">选择单一指标、数据域与统计范围，即可查看方法对比图；不同口径的数值不会合并绘制。</div>';
  const visible=rows.slice(0,12),min=Math.min(0,...visible.map(r=>Number(r.value))),max=Math.max(0,...visible.map(r=>Number(r.value))),span=max-min||1;
  const width=900,left=225,right=100,plot=width-left-right,zero=left+(0-min)/span*plot;
  const height=visible.length*43+20;
  const svg=`<svg class="chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="${E(visible[0].metric)}方法对比图"><line x1="${zero}" x2="${zero}" y1="0" y2="${height-10}" stroke="#dde8d7"/>${visible.map((row,index)=>{const value=Number(row.value),end=left+(value-min)/span*plot;return `<g><text x="0" y="${index*43+23}">${E(row.variant_id.length>30?row.variant_id.slice(0,28)+'…':row.variant_id)}</text><rect x="${Math.min(zero,end)}" y="${index*43+7}" width="${Math.max(Math.abs(end-zero),2)}" height="24" rx="3" fill="${index===0?'#99b8a5':'#318d73'}"><title>${E(row.variant_id)}: ${E(row.value)} · ${E(L(row.prediction_phase))}</title></rect><text class="value" x="${end+10}" y="${index*43+23}">${E(row.value)}</text></g>`;}).join('')}</svg>`;
  const definition=result.metric_definitions[visible[0].metric];
  const preference=definition?.direction==='minimize'?'越低越好':definition?.direction==='maximize'?'越高越好':'方向未声明';
  return `<section class="panel chart-panel"><div class="chart-header"><h2>方法对比 · ${E(visible[0].metric)}</h2><span>${E(visible[0].scope_id)} · ${preference}</span></div>${svg}<div class="chart-context">图中保留各方法的预测时机；完整精度以表格为准。${result.total>20?'图表仅展示当前页的前12项，请按方法进一步筛选。':''} ${definition?'':'项目未声明指标方向，图表仅作数值展示。'}</div></section>`;
}
async function exportTable() {
  try{
    const response=await fetch('/api/export?'+tableQuery(),{headers:{Authorization:`Bearer ${token}`}});
    if(!response.ok)throw new Error((await response.json()).error);
    const blob=await response.blob(),url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download=`${state.detail.id}_${state.tableKind}.csv`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);toast('CSV 已交给浏览器保存');
  }catch(error){toast(error.message);}
}

const jobLabels={sync:'同步项目归档',preview:'预览同步计划',verify:'校验本地归档',accept:'统一发布验收',index:'更新检索索引',translate:'生成阅读译文'};
async function startJob(kind) {
  try{const job=await api('/api/jobs',{kind});state.activeJob=true;await refreshStatus();toast(`${jobLabels[kind]}已启动`);if(state.page==='jobs')renderJobs();pollJobs();}
  catch(error){toast(error.message);}
}
async function renderJobs() {
  const result=await api('/api/jobs');state.jobs=result.items;
  state.activeJob=result.items.some(job=>['queued','running'].includes(job.status));
  if(state.page!=='jobs')return;
  $('#content').innerHTML=`<div class="heading-row"><div><div class="eyebrow">ARCHIVE OPERATIONS</div><h1>运行任务</h1><p class="subtitle">脚本负责导入、校验和索引。这里查看每一步是否完成。</p></div></div><div class="job-actions"><button class="button primary" data-job="sync" ${!state.status.sync_available||state.activeJob?'disabled':''}>↻ 同步归档</button><button class="button" data-job="preview" ${!state.status.sync_available||state.activeJob?'disabled':''}>预览同步计划</button><button class="button" data-job="verify" ${state.activeJob?'disabled':''}>校验完整性</button><button class="button" data-job="index" ${state.activeJob?'disabled':''}>更新检索索引</button></div><div class="notice">同步只读取已配置的项目来源，发布归档新版本并更新索引，不会触发实验训练或评分。未变更的文件与向量会被复用。</div>${result.items.length?result.items.map(job=>`<article class="job"><div class="job-header"><h3>${['queued','running'].includes(job.status)?'<span class="spinner"></span>':''}${jobLabels[job.kind]}</h3><span class="badge job-status ${job.status}">${{queued:'等待中',running:'运行中',completed:'已完成',failed:'失败'}[job.status]}</span></div><p>${date(job.created_at)}${job.seconds!==undefined?` · 耗时 ${job.seconds.toFixed(2)} 秒`:''}</p>${job.error?`<div class="error">${E(job.error)}</div>`:''}${job.result?`<details><summary class="text-button">查看任务结果</summary><pre class="json-block">${E(JSON.stringify(job.result,null,2))}</pre></details>`:''}</article>`).join(''):empty('还没有运行任务','开始一次校验，或先预览同步计划。任务记录显示当前App会话中的操作。')}`;
  enhanceSyncPlans();
  $('.job-actions').insertAdjacentHTML('beforeend',`<button class="button" data-job="accept" ${state.activeJob?'disabled':''}>统一发布验收</button>`);
}
async function pollJobs() {
  clearTimeout(pollJobs.timer);
  try{
    const result=await api('/api/jobs');const wasActive=state.activeJob;
    state.jobs=result.items;state.activeJob=result.items.some(job=>['queued','running'].includes(job.status));
    if(state.translationPending){
      const translatedJob=result.items.find(job=>job.id===state.translationPending);
      if(translatedJob&&!['queued','running'].includes(translatedJob.status)){
        state.translationPending=null;
        if(translatedJob.status==='completed'&&state.detail?.id===translatedJob.result.experiment_id&&state.detail?.revision===translatedJob.result.revision){state.translation=translatedJob.result;if(state.page==='detail'&&state.detailTab==='overview')renderDetailBody();}
        else if(translatedJob.status==='failed'){toast(translatedJob.error);if(state.page==='detail'&&state.detailTab==='overview')renderDetailBody();}
      }
    }
    $('#job-dot').textContent=state.activeJob?'·':'';
    if(state.page==='jobs')await renderJobs();
    if(wasActive&&!state.activeJob){await refreshStatus();const latest=result.items[0];toast(latest.status==='completed'?'归档任务已完成':`任务未完成：${latest.error}`);if(state.page==='library')renderLibrary();}
    if(state.activeJob)pollJobs.timer=setTimeout(pollJobs,1100);
  }catch(error){toast(error.message);}
}
async function renderSettings() {
  const sources=await api('/api/sources'),s=state.status;
  if(state.page!=='settings')return;
  $('#content').innerHTML=`<div class="heading-row"><div><div class="eyebrow">LOCAL & CONNECTED</div><h1>来源与项目接入</h1><p class="subtitle">归档核心独立运行，项目通过标准接口连接。</p></div></div><div class="settings-grid"><section class="panel"><h2>本地归档</h2><div class="setting-value"><strong>归档文件位置</strong>${E(s.archive_path)}</div><div class="setting-value"><strong>同步配置</strong>${E(s.config_path||'未配置，仅浏览模式')}</div><div class="setting-value"><strong>已安装来源适配器</strong>${s.adapters.map(x=>`<span class="chip">${E(x)}</span>`).join(' ')}</div><div class="setting-value"><strong>检索模型</strong>${E(s.index.model?.repository||'尚未建立索引')}<br>${s.index.model?`${s.index.model.dimension} 维 · 本地 CPU`:''}</div><div class="header-actions"><button class="button small" data-copy="${E(s.archive_path)}">复制归档路径</button><button class="button small" data-action="quit">退出并释放内存</button></div><p class="provenance-note">关闭窗口后服务可继续留在本地；退出会等待已有归档任务收尾后释放模型内存。</p></section><section class="panel"><h2>接入其他项目</h2><div class="setting-value"><strong>直接上报</strong>通过 ArchiveClient 发布标准实验记录，已有项目可在结果封存后调用。</div><div class="setting-value"><strong>文件交换</strong>导出说明、元数据和两份 CSV，使用 bundle 适配器导入。</div><div class="setting-value"><strong>既有格式</strong>单独实现来源适配器，声明输入文件与转换逻辑，无需修改归档核心。</div><div class="notice">配置文件修改后，可先预览同步计划。App 不会自动下载模型或扫描未配置的目录。</div></section></div><div class="section-heading"><h2>已配置来源</h2><span class="section-meta">${sources.sources.length} 个</span></div>${sources.sources.map(source=>`<article class="source-card"><div class="section-heading"><h3>${E(source.id)}</h3><span class="chip">${E(source.adapter||source.type)}</span></div><div class="source-path">${E(source.path)}</div><pre class="json-block">${E(JSON.stringify(source.options||{},null,2))}</pre></article>`).join('')||empty('尚未配置项目来源','当前可浏览归档。重新启动时指定同步配置，即可使用项目同步功能。')}`;
  $('#content').insertAdjacentHTML('beforeend',translationSettingsPanel());
  const local=$('.settings-grid .panel');
  local.insertAdjacentHTML('beforeend',`<div class="path-actions"><button class="button small" data-pick="archive" data-kind="directory" data-initial="${E(s.archive_path)}">选择归档文件夹…</button><button class="button small" data-pick="config" data-kind="file" data-initial="${E(s.config_path||'')}">选择同步配置…</button></div><p class="provenance-note">更换归档会切换当前浏览库，不搬移文件；路径设置下次启动仍有效。选择来源后请先预览再同步。</p>`);
  document.querySelectorAll('.source-card').forEach((card,i)=>{
    const source=sources.sources[i];if(!source)return;
    card.insertAdjacentHTML('beforeend',`<div class="path-actions"><button class="button small" data-pick="source" data-id="${E(source.id)}" data-kind="directory" data-initial="${E(source.path)}">选择文件夹…</button><button class="button small" data-pick="source" data-id="${E(source.id)}" data-kind="file" data-initial="${E(source.path)}">选择文件…</button></div>`);
  });
}

document.addEventListener('click',async event=>{
  const button=event.target.closest('button,a');if(!button)return;
  if(button.id==='brand'){event.preventDefault();navigate('library');return;}
  if(button.dataset.page){navigate(button.dataset.page);return;}
  if(button.dataset.open){openDetail(button.dataset.open);return;}
  if(button.dataset.tab){state.detailTab=button.dataset.tab;renderDetail();return;}
  if(button.dataset.translate){translateDetail(button.dataset.translate);return;}
  if(button.dataset.revision){openDetail(state.detail.id,button.dataset.revision);return;}
  if(button.dataset.copy!==undefined){try{await navigator.clipboard.writeText(button.dataset.copy);toast('已复制');}catch{toast('无法访问剪贴板，请手动复制');}return;}
  if(button.dataset.suggest){$('#semantic-query').value=button.dataset.suggest;runSearch();return;}
  if(button.dataset.job){startJob(button.dataset.job);return;}
  if(button.dataset.pick){choosePath(button);return;}
  if(button.dataset.paging){const delta=Number(button.dataset.delta);if(button.dataset.paging==='library'){state.offset=Math.max(0,state.offset+delta*12);loadLibrary();}else{state.tableOffset=Math.max(0,state.tableOffset+delta*20);loadTable();}return;}
  if(button.dataset.action==='export'){exportTable();return;}
  if(button.dataset.action==='quit'){
    try{await api('/api/quit',{});clearTimeout(pollJobs.timer);$('#content').innerHTML=empty('本地服务正在退出','等待已有归档任务收尾后即会释放内存。下次双击 ExperimentArchive.cmd 即可重新打开。');$('#sidebar-state').textContent='已退出';}
    catch(error){toast(error.message);}return;
  }
  if(button.dataset.action==='retry'){if(state.page==='detail'&&state.detail)openDetail(state.detail.id);else navigate(state.page);}
});
document.addEventListener('change',event=>{
  if(event.target.id==='translation-provider')state.translationProvider=event.target.value;
  if(event.target.id==='translation-compare'){state.translationCompare=event.target.checked;renderDetailBody();}
});
document.addEventListener('submit',async event=>{
  if(event.target.id!=='translation-settings-form')return;
  event.preventDefault();
  try{const result=await api('/api/translation/settings',{provider:$('#translation-default-provider').value,ollama_url:$('#translation-ollama-url').value,ollama_model:$('#translation-ollama-model').value});state.status.translation=result;toast('翻译设置已保存，不会自动发送文本');}
  catch(error){toast(error.message);}
});
$('#sync-button').addEventListener('click',()=>startJob('sync'));
$('#verify-button').addEventListener('click',()=>startJob('verify'));
(async()=>{try{await refreshStatus();renderLibrary();pollJobs();}catch(error){$('#sidebar-state').textContent='连接失败';$('#content').innerHTML=errorPanel(error);}})();
