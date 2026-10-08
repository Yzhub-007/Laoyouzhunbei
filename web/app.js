'use strict';
const $ = id => document.getElementById(id);
let dataset, bootstrap, latestResult = null;
let liveData = null, liveBusy = false, datasetBusy = false;
const copy = value => JSON.parse(JSON.stringify(value));

function node(tag, text, className) {
  const el = document.createElement(tag);
  if (text !== undefined) el.textContent = text;
  if (className) el.className = className;
  return el;
}
function safeUrl(value) {
  try { const u = new URL(value); return ['http:', 'https:'].includes(u.protocol) && !u.username && !u.password ? u.href : null; } catch { return null; }
}
function message(text, error = false) {
  $('message').hidden = false;
  $('message').className = error ? 'message error' : 'message';
  $('message').textContent = text;
}
function allowDatasetReplace(action,keys=['pilot','dataset-json']) {
  if(pilotBusy||datasetBusy){message('试点建档正在保存，请完成后再载入其他资料。',true);return false;}
  return DraftGuard.confirmDiscard(keys,action);
}
function panel(id) {
  if(id === 'response-panel' && !Security.guard('can_operate'))return;
  document.querySelectorAll('.panel').forEach(p => p.hidden = p.id !== id);
  document.querySelectorAll('.step').forEach(b => {
    const active = b.dataset.panel === id;
    b.classList.toggle('active', active);
    if (active) b.setAttribute('aria-current', 'step'); else b.removeAttribute('aria-current');
  });
  const liveView = id === 'live-panel';
  document.querySelector('.overview').hidden = ['monitor-panel','response-panel'].includes(id);
  $('reset-demo').hidden = ['monitor-panel','response-panel'].includes(id);
  const reference = dataset?.weather?.mode === 'live_reference';
  $('page-title').textContent = liveView ? '杭州气象参考' : '把气象信息整理成待办清单';
  $('page-subtitle').textContent = liveView ? '看清数据的时间、位置与来源，再决定如何使用。' : '先核对来源与适用范围，再查看每项任务的依据。';
  $('banner-title').textContent = liveView ? '最新气象参考' : reference ? '实时快照演练，非官方预警' : '历史演练，非当前预警';
  $('banner-description').textContent = liveView ? '城区模型与机场实测分别展示。校园点位仍为示范，官方预警接口尚未接入。' : '示范点位与预案草稿不代表真实校园状态；结果仅供核验，不对外发布。';
  const titles = {'input-panel':['核对本次气象资料','保留来源与资料时间，确认本次演练的适用范围。'],'site-panel':['让每项行动都有依据','核对点位、预案与责任角色，再开始一次匹配。'],'results-panel':['行动清单，从核验到完成','逐项审核、记录处理，所有修改都可以回查。'],'public-panel':['把确认的信息说清楚','从人工确认的现场状态整理提示，并记录演练反馈。'],'history-panel':['每次运行，都有迹可循','恢复资料快照与工作进度，回看每一次人工确认。']};
  if (titles[id]) { $('page-title').textContent=titles[id][0];$('page-subtitle').textContent=titles[id][1]; }
  if (!liveView) {
    $('banner-title').textContent = reference ? '实时快照演练，非官方预警' : dataset?.weather?.mode === 'simulation' ? '模拟演练，非当前预警' : '历史演练，非当前预警';
    $('banner-description').textContent = dataset?.site?.status !== 'verified' ? '未核验点位与草稿不代表真实校园状态；结果供演练核验，未对外发布。' : '场地由资料提供者标记已核验；请逐项人工确认，公众提示仍为本地演练草稿。';
  }
  if(id === 'history-panel') loadHistory();
  if(id === 'response-panel') {
    $('page-title').textContent='让监测依据进入可复核的预案';
    $('page-subtitle').textContent='选择已建档点位，补齐管理资料，保存每一版依据和内部复核记录。';
    $('banner-title').textContent='预案草案 · 需管理方核验';
    $('banner-description').textContent='编制依据为已保存建档与监测快照；不会自动启动处置或对外发布。';
    ResponsePlans.open();
  }
  if(id === 'public-panel') renderNotices();
  if(id === 'pilot-panel') {$('page-title').textContent='紫金港防汛行动试点';$('page-subtitle').textContent='一个小范围、可追溯、由管理人员确认的行动助手。';$('banner-title').textContent='团队试点原型';$('banner-description').textContent='校区已确认；具体楼宇、管理授权及真实点位仍以建档资料为准。本页面不代表校方公告。';}
  if(id === 'experiment-panel') {$('page-title').textContent='用真实记录验证是否有用';$('page-subtitle').textContent='先冻结资料与评分标准，再开始对照试验。';loadTrials().catch(e=>message(e.message,true));}
  if(id === 'monitor-panel') {$('page-title').textContent='天气与现场，持续自动监测';$('page-subtitle').textContent='接入摄像头后自动采集与识别，保留来源、时间和人工核验依据。';$('banner-title').textContent='自动监测 · 人工核验';$('banner-description').textContent='城市天气与现场画面分别判断；数据缺失保持未知，自动任务等待管理人员核验。';refreshMonitor();}
  Security.apply();
}
function invalidate() {
  latestResult = null;
  $('results-content').hidden = true;
  $('empty-state').hidden = false;
  $('result-count').textContent = '0';
  $('scope-confirmed').checked = false;
  $('external-confirmed').checked = false;
  overview();renderNotices();
}
function beijingInput(iso) {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? '' : new Date(date.getTime() + 8 * 3600 * 1000).toISOString().slice(0,16);
}
function syncWeather() {
  const w = dataset.weather;
  if (w.mode === 'live_reference') {
    w.hazards = ['rain','wind'].filter(h => $('hazard-' + h).checked);
    updateSource();
    return;
  }
  for (const [field,id] of Object.entries({title:'weather-title', publisher:'publisher',region:'region',source_url:'source-url',level:'level',text:'weather-text'})) w[field] = $(id).value.trim();
  w.published_at = $('published-at').value ? $('published-at').value + ':00+08:00' : '';
  w.replay_at = $('replay-at').value ? $('replay-at').value + ':00+08:00' : '';
  w.hazards = ['rain','wind'].filter(h => $('hazard-' + h).checked);
  updateSource();
}
function updateSource() {
  const url = safeUrl($('source-url').value);
  const link = $('source-link');
  if (url) { link.href = url; link.removeAttribute('aria-disabled'); }
  else { link.removeAttribute('href'); link.setAttribute('aria-disabled','true'); }
}
function fill() {
  const w = dataset.weather;
  const reference = w.mode === 'live_reference';
  for (const [field,id] of Object.entries({title:'weather-title',publisher:'publisher',region:'region',source_url:'source-url',level:'level',text:'weather-text'})) $(id).value = w[field] || '';
  $('published-at').value = beijingInput(w.published_at);
  $('replay-at').value = beijingInput(w.replay_at);
  for (const h of ['rain','wind']) $('hazard-' + h).checked = (w.hazards || []).includes(h);
  $('text-kind').textContent = w.text_kind || '内容性质需核实';
  for (const id of ['weather-title','publisher','region','source-url','published-at','replay-at','weather-text']) $(id).readOnly = reference;
  $('level').disabled = reference;
  $('weather-mode').textContent = reference ? '实时快照 · 演练参考' : '历史 / 模拟';
  $('published-label').textContent = reference ? '快照生成时间（北京时间）' : '发布时间（北京时间）';
  $('hazard-label').textContent = reference ? '手动选择演练灾种（非预警判断）' : '人工核对的相关灾种';
  $('input-kind-note').textContent = reference ? '气象资料：最新模型/站点快照，非校园实测' : '气象资料：历史或模拟内容，请核对来源';
  updateSource();
  $('site-name').textContent = dataset.site.name;
  $('site-note').textContent = dataset.site.note || '资料的真实性与使用授权由提供者核实。';
  $('point-list').replaceChildren();
  for (const p of dataset.points) {
    const card = node('article', undefined, 'point-card');
    card.append(node('span', p.id, 'point-id'), node('strong', p.name), node('p', (p.features || []).join(' · ')),node('p', `${p.region} / ${p.status === 'verified' ? '用户标记已核验' : '采集槽位 / 待核实'}`));
    $('point-list').append(card);
  }
  $('plan-count').textContent = dataset.plans.length + ' 条资料';
  $('plan-list').replaceChildren();
  for (const r of dataset.plans) {
    const row = node('article', undefined, 'plan-row');
    row.append(node('strong', `${r.id} · ${r.title}`),node('p',r.action),node('p',`${r.role} · ${r.source} · ${r.status === 'draft' ? '草稿待审核' : '用户标记已核验'}`));
    $('plan-list').append(row);
  }
  $('dataset-editor').value = JSON.stringify(dataset, null, 2);
  DraftGuard.saved('dataset-json');
  overview();
  renderPilot();
  $('shadow-option').disabled=true;$('operation-mode').value='tabletop';
  Security.apply();
}
function importData(value) {
  if (!value || typeof value !== 'object' || !value.site || !value.weather || !Array.isArray(value.points) || !Array.isArray(value.plans)) throw new Error('资料需包含site、weather、points、plans。');
  if (!value.points.every(p => p && typeof p === 'object') || !value.plans.every(r => r && typeof r === 'object')) throw new Error('点位与条目必须为对象。');
  if(!allowDatasetReplace('载入新资料',['pilot']))return false;
  const previous = dataset;
  dataset = copy(value);
  try { fill(); } catch { dataset = previous; fill(); throw new Error('资料字段不完整，请参考示例结构。'); }
  invalidate();
  return true;
}
function download(name, content, type) {
  const url = URL.createObjectURL(new Blob([content], {type}));
  const a = node('a'); a.href = url; a.download = name; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function renderResult(result) {
  latestResult = result;
  $('result-count').textContent = String(result.cards.length);
  $('result-title').textContent = `${result.cards.length} 项行动任务 · ${result.label}`;
  $('run-meta').textContent = `记录 ${result.run_id} · ${result.elapsed_ms} ms · ${result.weather.mode === 'live_reference' ? '快照生成时间' : '演练资料时间'} ${result.weather.published_at}`;
  $('warnings').replaceChildren(...result.warnings.map(w => node('p', w)));
  renderWorkflow();
  $('empty-state').hidden = true;
  $('results-content').hidden = false;
  panel('results-panel');
}
async function generate(mode) {
  if(!Security.guard('can_operate'))return;
  syncWeather();
  if(activeTrial && activeTrial.condition!==mode)return message('本次计时条件与生成方式不同，请按分配条件执行。',true);
  if (!$('scope-confirmed').checked) return message('请先确认资料区域与本次演练场景的适用关系。',true);
  if (mode === 'ai' && !$('external-confirmed').checked) return message('使用AI前，请确认资料可发送给配置的模型服务。',true);
  const request = {data:copy(dataset),mode,scope_confirmed:true,external_data_confirmed:$('external-confirmed').checked,operation_mode:$('operation-mode').value,trial_id:activeTrial?.id};
  latestResult = null; $('results-content').hidden = true; $('empty-state').hidden = false; $('result-count').textContent = '0';
  overview();renderNotices();$('task-search').value='';$('task-filter').value='all';
  const controls = [...document.querySelectorAll('button,input,textarea,select')];
  const disabled = controls.map(el => el.disabled);
  controls.forEach(el => el.disabled = true);
  message(mode === 'ai' ? '正在请求配置的模型服务并校验引用…' : '正在按资料条件匹配条目…');
  try {
    const response = await Security.fetch('/api/generate',{method:'POST',headers:{'Content-Type':'application/json','X-App-Token':Security.csrf()},body:JSON.stringify(request),signal:AbortSignal.timeout(60000)});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || '生成未完成。');
    renderResult(result);
    if(activeTrial)activeTrial.run_id=result.run_id;
    message(result.mode === 'rules' ? '规则演练已完成并保存本机记录。本次没有调用AI。' : 'AI匹配已通过编号与适用条件校验，任务内容来自输入条目；仍需人工审核。');
  } catch (error) { message(error.name === 'TimeoutError' ? '请求超时，请稍后检查本地服务。' : error.message,true); }
  finally { controls.forEach((el,i) => el.disabled = disabled[i]);Security.apply(); }
}
function exportMarkdown() {
  if (!latestResult) return;
  const r = latestResult;
  const clean = value => String(value).replace(/[<>]/g,'').replace(/\r/g,'');
  const lines = ['# 涝有准备 · 演练行动清单','','> 演练参考，非官方预警。审核状态按各任务记录；未对外发布。','',`- 场地：${clean(r.site.name)}`,`- 资料类型：${clean(r.weather.text_kind)}`,`- 生成方式：${r.label}`,`- 运行编号：${r.run_id}`,`- 资料/快照时间：${clean(r.weather.published_at)}`,`- 来源：${clean(r.weather.source_url)}`,'','## 资料限制','',...r.warnings.map(w => '- ' + clean(w))];
  if (r.weather.live_sources) {
    lines.push('','## 实时来源快照','',clean(r.weather.text));
    for (const p of Object.values(r.weather.live_sources)) lines.push('',`- ${clean(p.provider)}：数据时刻 ${clean(p.data_time)}；获取时刻 ${clean(p.fetched_at)}；来源 ${clean(p.source_url)}`);
  }
  r.cards.forEach(c => lines.push('',`## ${clean(c.point_id)} · ${clean(c.point_name)}`,'',`- 任务：${clean(c.action)}`,`- 责任角色：${clean(c.role)}`,`- 状态：${c.status}`,`- 条目：${clean(c.plan_id)} / ${clean(c.plan_source)}`,`- 点位来源：${clean(c.point_source)}`));
  lines.push('','## 人工操作记录','',...(r.audit || []).map(e=>`- ${clean(e.at)} · ${clean(e.actor)} · ${e.operation} · ${clean(e.detail.note || '')}`));
  download(`演练清单_${r.run_id}.md`,lines.join('\n'),'text/markdown;charset=utf-8');
}
async function boot() {
  try {
    if(!await Security.prepare())return;
    const response = await Security.fetch('/api/bootstrap');
    if (!response.ok) throw new Error('本地服务初始化失败。');
    bootstrap = await response.json();Security.configure(bootstrap);if(!Security.authenticated())return;dataset = copy(bootstrap.demo);fill();DraftGuard.register('dataset-json','完整JSON资料',()=>$('dataset-editor').value);setupPilot();setupMonitor();ResponsePlans.setup();panel('monitor-panel');
    $('ai-status').textContent = bootstrap.ai_configured ? '检测到模型配置；实际可用性需运行验证。密钥不发送给浏览器。' : 'AI尚未配置。可先使用规则演练；按本机加密配置说明接入本机或已登记的校内模型。';
    $('generate-ai').disabled = !bootstrap.ai_configured;
    Security.apply();
    document.querySelectorAll('[data-panel]').forEach(b => b.addEventListener('click',() => {syncWeather();if(!DraftGuard.dirty('dataset-json')){$('dataset-editor').value=JSON.stringify(dataset,null,2);DraftGuard.saved('dataset-json');}panel(b.dataset.panel);}));
    document.querySelectorAll('[data-go]').forEach(b => b.addEventListener('click',() => {syncWeather();if(!DraftGuard.dirty('dataset-json')){$('dataset-editor').value=JSON.stringify(dataset,null,2);DraftGuard.saved('dataset-json');}panel(b.dataset.go);}));
    document.querySelectorAll('#input-panel input,#input-panel select,#input-panel textarea').forEach(el => el.addEventListener('input',() => {invalidate();syncWeather();}));
    $('reset-demo').addEventListener('click',() => {if(activeTrial)return message('计时试验尚未结束，请先完成或中止。',true);if(!allowDatasetReplace('恢复已保存资料'))return;dataset=copy(bootstrap.demo);fill();invalidate();panel('input-panel');message('已恢复本机保存的紫金港试点资料，请核对真实／模拟标识。');});
    $('apply-json').addEventListener('click',() => {try{if(!importData(JSON.parse($('dataset-editor').value)))return;message('资料已应用。生成时将继续检查必填字段与引用条件。');}catch(e){message(e.message,true);}});
    $('import-file').addEventListener('change',async e => {try{const f=e.target.files[0];if(!f)return;if(f.size>512000)throw new Error('文件超过500KB。');if(!allowDatasetReplace('导入文件',['dataset-json']))return;if(!importData(JSON.parse(await f.text())))return;message('资料已导入，请核对后生成。');}catch(err){message(err.message,true);}finally{e.target.value='';}});
    $('download-data').addEventListener('click',() => {syncWeather();download('演练资料.json',JSON.stringify(dataset,null,2),'application/json;charset=utf-8');});
    $('generate-rules').addEventListener('click',() => generate('rules'));
    $('generate-ai').addEventListener('click',() => generate('ai'));
    $('export-md').addEventListener('click',exportMarkdown);
    $('export-json').addEventListener('click',() => latestResult && download(`运行记录_${latestResult.run_id}.json`,JSON.stringify(latestResult,null,2),'application/json;charset=utf-8'));
    $('refresh-live').addEventListener('click',refreshLive);
    $('export-live').addEventListener('click',() => liveData && download(`最新气象快照_${liveData.snapshot_id}.json`,JSON.stringify(liveData,null,2),'application/json;charset=utf-8'));
    $('use-live').addEventListener('click',useLiveReference);
    $('task-search').addEventListener('input',renderCards);
    $('task-filter').addEventListener('change',renderCards);
    $('refresh-history').addEventListener('click',loadHistory);
    refreshLive();
    setInterval(() => { if ($('auto-refresh').checked && !document.hidden && !$('live-panel').hidden) refreshLive(); },60000);
    document.addEventListener('visibilitychange',() => { if (!document.hidden && $('auto-refresh').checked && !$('live-panel').hidden) refreshLive(); });
  } catch(e) {if(Security.authenticated())Security.initializationFailed(e.message);}
}

function localTime(value) {
  if (!value) return '未提供';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '未提供' : new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(date);
}
function metric(value, digits=1) {
  return typeof value === 'number' && Number.isFinite(value) ? value.toFixed(digits) : '未提供';
}
function providerState(prefix, provider) {
  const labels = {fresh:'数据在时效内',stale:'已过期 · 仅供回看',future:'数据时间异常',degraded:'缓存 · 获取失败',unavailable:'暂无数据'};
  $(prefix+'-status').textContent = labels[provider.status] || '状态未知';
  $(prefix+'-status').className = 'tag ' + (provider.status === 'fresh' ? 'fresh-tag' : 'amber');
  $(prefix+'-error').hidden = !provider.error;
  $(prefix+'-error').textContent = provider.error || '';
  $(prefix+'-time').textContent = `${prefix === 'model' ? '模型有效时刻' : '站点观测时刻'}：${localTime(provider.data_time)}（北京）${typeof provider.age_minutes === 'number' ? ` · 相距 ${provider.age_minutes.toFixed(0)} 分钟` : ''}`;
  $(prefix+'-fetch').textContent = `成功获取：${localTime(provider.fetched_at)} · 最近检查：${localTime(provider.last_checked_at)}`;
}
function renderLive(data) {
  liveData = data;
  const model = data.providers.model, station = data.providers.station;
  providerState('model',model);providerState('station',station);
  $('model-rain').textContent = metric(model.precipitation_mm,2);
  $('model-interval').textContent = model.interval_seconds ? `毫米 / ${model.interval_seconds/60}分钟时段` : '时段未提供';
  $('model-wind').textContent = metric(model.wind_speed_ms,2);
  $('model-temp').textContent = metric(model.temperature_c);
  $('model-humidity').textContent = metric(model.humidity_percent,0);
  $('model-place').textContent = model.location ? `请求 ${model.location.requested.latitude}°N，${model.location.requested.longitude}°E；返回网格 ${model.location.grid_latitude.toFixed(4)}°N，${model.location.grid_longitude.toFixed(4)}°E。非校园测站。` : '杭州城市参考坐标，非校园点位。';
  $('station-wind').textContent = metric(station.wind_speed_ms,2);
  $('station-temp').textContent = metric(station.temperature_c);
  $('station-weather').textContent = station.weather_code || '未提供';
  $('metar-raw').textContent = station.raw_report || '未取得报文。';
  $('station-place').textContent = typeof station.latitude === 'number' && typeof station.longitude === 'number' ? `站点 ${station.latitude.toFixed(3)}°N，${station.longitude.toFixed(3)}°E · 经 NOAA AWC 获取。` : 'NOAA Aviation Weather Center提供，非紫金港校区测站。';
  $('live-checked').textContent = `本机检查 ${localTime(data.served_at)}（北京）· 上游请求间隔 ${data.poll_seconds/60} 分钟`;
  $('forecast-rows').replaceChildren();
  const rows = model.hourly || [];
  for (const r of rows) {
    const tr = node('tr');
    tr.append(node('td',`${localTime(r.window_start)} — ${localTime(r.time)}`),node('td',metric(r.precipitation_mm,2)),node('td',metric(r.wind_speed_ms,2)));
    $('forecast-rows').append(tr);
  }
  if (!rows.length) {const tr=node('tr'),td=node('td','未取得可显示的后续小时窗，不能据此判断无降水。');td.colSpan=3;tr.append(td);$('forecast-rows').append(tr);}
  $('forecast-status').textContent = model.status === 'fresh' ? '模型预报，非实测' : '上游异常或过期，勿作当前依据';
  $('forecast-status').className = 'tag ' + (model.status === 'fresh' ? 'neutral' : 'amber');
  $('use-live').disabled = !(model.usable || station.usable);
  $('export-live').disabled = false;
  const failures = Object.entries(data.providers).filter(([,p]) => p.status !== 'fresh');
  $('live-message').hidden = !failures.length;
  $('live-message').className = 'message error';
  $('live-message').textContent = failures.map(([n,p])=>`${n === 'model' ? '城区模型' : '机场观测'}：${p.error || (p.status==='stale' ? '资料已过期' : p.status==='future' ? '时刻超前，请核实时间' : '未取得有效资料')}`).join('；');
  Security.apply();
}
async function refreshLive() {
  if (liveBusy || !Security.authenticated()) return;
  liveBusy = true; $('refresh-live').disabled = true;
  try {
    const response = await Security.fetch('/api/live',{signal:AbortSignal.timeout(25000)});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '本地数据连接未完成。');
    renderLive(data);
  } catch(e) {
    const error = '未连接到最新数据，请检查本地服务或网络；没有以演练数据替代。';
    renderLive({providers:{model:{status:'unavailable',error},station:{status:'unavailable',error}},served_at:null,poll_seconds:600});
    liveData = null;$('use-live').disabled=true;$('export-live').disabled=true;
    $('live-checked').textContent = '获取失败 · 更新时间未知';
  } finally { liveBusy=false;$('refresh-live').disabled=false;Security.apply(); }
}
async function useLiveReference() {
  if(!Security.guard('can_operate'))return;
  if(!allowDatasetReplace('载入天气参考并刷新资料表单'))return;
  datasetBusy=true;const release=DraftGuard.lock('#pilot-form,#site-panel');
  $('use-live').disabled=true;
  try {
    const response=await Security.fetch('/api/live/reference',{method:'POST',headers:{'Content-Type':'application/json','X-App-Token':Security.csrf()},body:'{}',signal:AbortSignal.timeout(25000)});
    const data=await response.json();if(!response.ok)throw new Error(data.error || '快照载入失败。');
    dataset.weather = data.weather;
    fill();invalidate();panel('input-panel');
    message('已载入实时来源快照，内容锁定以保持可追溯。请选择要演练的灾种；此选择不会生成官方预警级别。');
  } catch(e){message(e.message,true);}
  finally{datasetBusy=false;release();$('use-live').disabled=!(liveData && Object.values(liveData.providers).some(p=>p.usable));Security.apply();}
}
boot();
