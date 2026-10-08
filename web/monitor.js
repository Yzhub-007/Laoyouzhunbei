'use strict';
let monitorBusy = false, monitorSnapshot = null, monitorConfig = null, monitorRevision = null;
let monitorConfigDirty = false, monitorJsonDirty = false, monitorConfigBusy = false;
const monitorStateLabels = {unknown:'状态未知',clear:'可见范围未见积水',suspected:'疑似异常，待复核',pooling_suspected:'疑似可见积水',pooling:'检测到积水',high_water:'较深积水'};
const monitorSourceLabels = {fresh:'数据在时效内',ok:'连接正常',stale:'数据已过期',future:'数据时间异常',degraded:'获取失败',error:'获取失败',unavailable:'尚无有效数据',disabled:'未启用',pending:'等待首次采集',unknown:'状态未知',replay:'历史回放',missing:'尚无观测',poor:'质量或置信度不足',orphaned:'点位已移除'};
function monitorText(value) {
  if (value == null) return '';
  if (typeof value === 'string') return value;
  if (typeof value !== 'object') return String(value);
  return value.label || value.message || value.detail || value.reason || JSON.stringify(value);
}
function monitorTone(state) {
  return state === 'clear' ? 'fresh-tag' : ['pooling','high_water'].includes(state) ? 'monitor-alert' : ['suspected','pooling_suspected'].includes(state) ? 'amber' : 'neutral';
}
function monitorNotice(text, error = false) {
  $('monitor-message').hidden = !text;
  $('monitor-message').className = 'message space-top' + (error ? ' error' : '');
  $('monitor-message').textContent = text || '';
}
function monitorConfigNotice(text, error = false) {
  $('monitor-config-feedback').textContent = text;
  $('monitor-config-feedback').className = error ? 'form-error' : 'help';
}
function monitorBasis(container, items, fallback) {
  container.replaceChildren();
  const rows = Array.isArray(items) ? items : items ? [items] : [];
  for (const item of rows) container.append(node('p', monitorText(item), 'help'));
  if (!rows.length && fallback) container.append(node('p', fallback, 'help'));
}
async function monitorGet(path) {
  const response = await Security.fetch(path, {cache:'no-store',signal:AbortSignal.timeout(15000)});
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || '监测服务暂不可用。');
  return value;
}
function monitorPointOptions(points) {
  for (const id of ['monitor-source-point','monitor-correction-point']) {
    const select = $(id), previous = select.value;
    const fingerprint = JSON.stringify(points.map(p=>[p.point_id,p.point_name]));
    if (select.dataset.points === fingerprint) continue;
    select.dataset.points = fingerprint;
    select.replaceChildren();
    if (!points.length) {const option=node('option','暂无已保存的点位，请先在试点建档保存');option.value='';select.append(option);}
    for (const p of points) {const option=node('option',`${p.point_name} · ${p.point_id}`);option.value=p.point_id;select.append(option);}
    if (points.some(p=>p.point_id===previous)) select.value=previous;
  }
}
function renderMonitor(data) {
  monitorSnapshot = data;
  const service=data.service || {}, summary=data.summary || {}, weather=data.weather || {};
  $('monitor-service').textContent = service.running ? '后台监测运行中' : '后台监测未运行';
  $('monitor-service').className = 'tag '+(service.running && !service.last_error ? 'fresh-tag' : 'amber');
  $('monitor-checked').textContent = `最近采集 ${localTime(service.last_poll_at)} · 页面读取 ${localTime(data.served_at)}（北京） · 采集间隔 ${service.poll_seconds || '—'}秒`;
  $('monitor-source-count').textContent = `${summary.connected_sources ?? 0} / ${summary.fresh_sources ?? 0}`;
  $('monitor-attention-count').textContent = String((summary.suspected_points || 0)+(summary.alert_points || 0));
  $('monitor-unknown-count').textContent = String(summary.unknown_points ?? 0);
  $('monitor-task-count').textContent = String(summary.task_count ?? (data.tasks || []).length);
  $('monitor-weather-label').textContent = weather.label || '天气状态未知';
  $('monitor-weather-status').textContent = weather.state && weather.state !== 'unknown' ? '自动识别 · 区域参考' : '暂无足够有效数据';
  $('monitor-weather-status').className = 'tag '+(weather.state && weather.state!=='unknown' ? 'neutral' : 'amber');
  monitorBasis($('monitor-weather-basis'),weather.basis,'等待具有有效时间的气象数据，不据此判断晴天或无雨。');
  for(const provider of weather.providers || []) $('monitor-weather-basis').append(node('p',`${provider.name || provider.id} · ${monitorSourceLabels[provider.status] || '状态未知'} · 观测 / 有效时刻 ${localTime(provider.observed_at)}（北京）`,'help'));
  $('monitor-vision-status').textContent = service.vision_configured ? '视觉服务已配置' : '视觉服务未配置';
  $('monitor-vision-status').className = 'tag '+(service.vision_configured ? 'fresh-tag' : 'neutral');
  const sources=data.sources || [];
  $('monitor-sources').replaceChildren();
  for (const source of sources) {
    const row=node('div',undefined,'monitor-source-row'),head=node('div',undefined,'monitor-row-heading');
    head.append(node('strong',source.name || source.id),node('span',monitorSourceLabels[source.status] || source.status || '状态未知','tag '+(source.status==='fresh'?'fresh-tag':'amber')));
    row.append(head,node('p',`${source.point_id || '未关联点位'} · 最近成功 ${localTime(source.last_success_at)}（北京）`,'help'));
    if(source.observed_at) row.append(node('p',`画面 / 数据时间 ${localTime(source.observed_at)}`,'help'));
    if(source.error) row.append(node('p',monitorText(source.error),'source-error'));
    $('monitor-sources').append(row);
  }
  if(!sources.length) $('monitor-sources').append(node('p','尚未接入现场来源。添加摄像头后自动采集，当前点位均按实际数据判断。','monitor-empty'));
  const points=data.points || [];
  monitorPointOptions(points);
  $('monitor-correction-save').disabled = !points.length || !Security.can('can_operate');
  $('monitor-points').replaceChildren();
  for(const point of points) {
    const card=node('article',undefined,'surface monitor-point'),head=node('div',undefined,'monitor-row-heading');
    card.dataset.state=point.state || 'unknown';
    head.append(node('span',point.point_id,'point-id'),node('span',point.label || monitorStateLabels[point.state] || '状态未知','tag '+monitorTone(point.state)));
    card.append(head,node('h3',point.point_name || point.point_id));
    const basis=node('div',undefined,'monitor-evidence');monitorBasis(basis,point.basis,'尚无足够有效的现场证据。');card.append(basis);
    card.append(node('p',`观测时间 ${localTime(point.observed_at)}（北京）`,'help'));
    card.append(node('p',`来源：${(point.sources || []).map(id=>sources.find(s=>s.id===id)?.name || id).join('、') || '未接入 / 暂无有效来源'}`,'help'));
    if(point.correction?.active) {
      const correction=point.correction;
      card.append(node('p',`人工临时校正 · ${correction.actor} · 有效至 ${localTime(correction.expires_at)}`,'monitor-correction-note'),node('p',correction.note,'help'),node('p',`机器识别：${point.automatic_label || monitorStateLabels[point.automatic_state] || '未知'}`,'help'));
    }
    const button=node('button','现场复核 / 临时校正','button secondary');button.type='button';button.dataset.requires='can_operate';
    button.addEventListener('click',()=>{$('monitor-correction-point').value=point.point_id;$('monitor-correction-details').open=true;$('monitor-correction-details').scrollIntoView({block:'start'});$('monitor-correction-state').focus({preventScroll:true});});
    if(Security.can('can_operate'))card.append(button);$('monitor-points').append(card);
  }
  if(!points.length) $('monitor-points').append(node('div','暂无已保存点位，请先在“试点建档”保存现场点位。','surface monitor-empty'));
  $('monitor-tasks').replaceChildren();
  for(const task of data.tasks || []) {
    const row=node('article',undefined,'monitor-task');
    row.append(node('h3',`${task.point_name || task.point_id} · ${task.title || '现场核验'}`),node('span','待人工核验','tag amber'),node('p',task.action || '请核验现场情况。'),node('p',`责任角色：${task.role || '待指定'} · 条目 ${task.plan_id || '待核实'}`,'help'));
    row.append(node('p',`点位：${task.point_status==='verified'?'已建档核验':'待核实'} · 条目：${task.plan_status==='verified'?'已审核':'草稿 / 待核实'}`,'help'));
    const details=node('details',undefined,'space-top'),basis=node('div');details.append(node('summary','查看自动匹配依据'));
    monitorBasis(basis,task.basis,'暂无可用依据，先核对来源。');details.append(basis);row.append(details);$('monitor-tasks').append(row);
  }
  if(!(data.tasks || []).length) $('monitor-tasks').append(node('p','当前没有可展示的自动匹配任务。没有任务不代表现场安全；请先核对数据时效与点位接入。','monitor-empty'));
  $('monitor-events').replaceChildren();
  for(const event of (data.events || []).slice(0,12)) {
    const row=node('article',undefined,'monitor-event');
    row.append(node('span',`${localTime(event.at || event.created_at || event.timestamp)}（北京）`,'help'));
    const scope=event.point_name || event.point_id || (event.kind==='weather_state'?'区域天气':event.source_name || sources.find(s=>s.id===event.source_id)?.name || event.source_id || '监测服务');
    const current=event.kind==='source_status'?(monitorSourceLabels[event.state] || '状态变化'):event.to_label || event.label || monitorStateLabels[event.to_state || event.state] || '状态更新';
    row.append(node('p',event.message || event.description || `${scope} · ${current}`));
    if(event.note) row.append(node('p',event.note,'help'));
    $('monitor-events').append(row);
  }
  if(!(data.events || []).length) $('monitor-events').append(node('p','尚无状态变化记录。','monitor-empty'));
  if(service.last_error) monitorNotice(`后台采集异常：${service.last_error}`,true);
  else monitorNotice('');
  if(monitorConfig && data.config_revision !== undefined && data.config_revision !== monitorRevision) monitorConfigNotice('后台配置版本已变化。当前草稿已保留；保存冲突时请先下载草稿，再重新载入配置。',true);
  Security.apply();
}
function monitorUnavailable(error) {
  monitorSnapshot=null;
  $('monitor-service').textContent='监测连接中断';$('monitor-service').className='tag amber';
  $('monitor-checked').textContent='当前状态不可用 · 等待重新连接';
  for(const id of ['monitor-source-count','monitor-attention-count','monitor-unknown-count','monitor-task-count']) $(id).textContent='—';
  $('monitor-weather-label').textContent='天气状态未知';$('monitor-weather-status').textContent='连接中断';$('monitor-weather-status').className='tag amber';
  monitorBasis($('monitor-weather-basis'),[],'无法确认当前气象状态。');
  $('monitor-sources').replaceChildren(node('p','连接中断，暂不能确认设备状态。','monitor-empty'));
  $('monitor-points').replaceChildren(node('div','当前现场状态未知。重新连接成功后恢复展示。','surface monitor-empty'));
  $('monitor-tasks').replaceChildren(node('p','当前依据不可用，已暂停展示自动任务。','monitor-empty'));
  $('monitor-events').replaceChildren(node('p','连接中断，暂不能读取状态变化记录。','monitor-empty'));
  $('monitor-correction-save').disabled=true;
  monitorNotice(`监测读取失败：${error}。未沿用上次的肯定状态。`,true);
}
async function refreshMonitor() {
  if(monitorBusy || !Security.authenticated()) return;
  monitorBusy=true;
  try {renderMonitor(await monitorGet('/api/monitor'));}
  catch(e) {monitorUnavailable(e.message);}
  finally {monitorBusy=false;}
}
function markMonitorConfigDirty() {
  monitorConfigDirty=true;
  DraftGuard.render();
  monitorConfigNotice('配置草稿有未保存修改，后台仍使用已保存版本。');
}
function syncMonitorConfigEditor() {
  $('monitor-config-json').value=JSON.stringify(monitorConfig,null,2);
  $('monitor-poll-seconds').value=monitorConfig.poll_seconds;
  $('monitor-confirm-samples').value=monitorConfig.confirm_samples;
  monitorJsonDirty=false;
}
function parseMonitorConfigEditor() {
  if(!monitorJsonDirty) return;
  const draft=JSON.parse($('monitor-config-json').value);
  if(!draft || Array.isArray(draft) || !Array.isArray(draft.sources)) throw new Error('配置必须是含 sources 数组的JSON对象。');
  monitorConfig=draft;
  syncMonitorConfigEditor();
}
function renderMonitorConfigSources() {
  $('monitor-config-sources').replaceChildren();
  for(const source of monitorConfig?.sources || []) {
    const row=node('div',undefined,'monitor-config-source'),label=node('label',undefined,'check');
    const toggle=node('input');toggle.type='checkbox';toggle.checked=source.enabled!==false;toggle.setAttribute('aria-label',`启用 ${source.name || source.id}`);
    label.append(toggle,node('span',`${source.name || source.id} · ${source.point_id} · ${source.transport==='vision'?'画面识别':source.kind==='water_depth'?'水深数据':'分析结果'}`));
    toggle.addEventListener('change',()=>{try {parseMonitorConfigEditor();const item=monitorConfig.sources.find(s=>s.id===source.id);if(item)item.enabled=toggle.checked;syncMonitorConfigEditor();markMonitorConfigDirty();renderMonitorConfigSources();}catch(e){toggle.checked=!toggle.checked;monitorConfigNotice(e.message,true);}});
    const remove=node('button','移除','button secondary');remove.type='button';remove.setAttribute('aria-label',`从草稿移除 ${source.name || source.id}`);
    remove.addEventListener('click',()=>{try{parseMonitorConfigEditor();monitorConfig.sources=monitorConfig.sources.filter(s=>s.id!==source.id);syncMonitorConfigEditor();markMonitorConfigDirty();renderMonitorConfigSources();}catch(e){monitorConfigNotice(e.message,true);}});
    row.append(label,remove);$('monitor-config-sources').append(row);
  }
  if(!monitorConfig?.sources?.length)$('monitor-config-sources').append(node('p','尚无来源，使用上方表单添加。','help'));
}
async function loadMonitorConfig() {
  if(monitorConfigBusy || !Security.can('can_configure')) return;
  if(!DraftGuard.confirmDiscard(['monitor-config'],'重新载入监测配置'))return;
  monitorConfigBusy=true;
  const release=DraftGuard.lock('#monitor-config-details');
  try {
    const saved=await monitorGet('/api/monitor/config');
    monitorConfig=copy(saved.config);monitorRevision=saved.revision;monitorConfigDirty=false;
    syncMonitorConfigEditor();renderMonitorConfigSources();
    DraftGuard.saved('monitor-config');
    $('monitor-source-add').disabled=false;$('monitor-config-save').disabled=false;
    monitorConfigNotice(`已载入配置版本 ${monitorRevision}。修改后保存即可生效。`);
  } catch(e) {monitorConfigNotice(`配置读取失败：${e.message}`,true);}
  finally {monitorConfigBusy=false;release();$('monitor-source-add').disabled=!monitorConfig;$('monitor-config-save').disabled=!monitorConfig;Security.apply();}
}
function monitorSourceMode() {
  const mode=$('monitor-source-mode').value,isVision=['image_url','image_file'].includes(mode),push=mode.endsWith('_push'),file=mode==='image_file'||mode.endsWith('_file');
  $('monitor-source-location-field').hidden=push;$('monitor-source-location').required=!push;
  $('monitor-vision-consent-field').hidden=!isVision;$('monitor-vision-consent').required=isVision;
  $('monitor-source-location-label').textContent=file?'本地文件路径（相对 runtime/monitor-input）':mode==='image_url'?'本机 / 校内HTTPS抓拍地址':'本机 / 校内HTTPS JSON接口';
  $('monitor-source-location').placeholder=file?mode==='image_file'?'entrance.jpg':'entrance.json':mode==='image_url'?'http://127.0.0.1:8080/snapshot.jpg':'https://gateway.campus.example/observation';
  $('monitor-source-help').textContent=mode==='image_url'?'默认通过本机网关抓拍并在本机识别。远端抓拍仅允许管理员登记的校内HTTPS地址；HTTP仅限本机。服务需用 X-Captured-At（含时区）或 Last-Modified 返回真实拍摄时间。公网视觉服务与RTSP直连已禁用。':mode==='image_file'?'将最新截图持续更新到本机 runtime/monitor-input。文件修改时间须反映实际拍摄时间，旧图不能当作当前现场。默认本机识别；校内远端识别需白名单和ROI裁剪。':push?'保存配置后，使用来源编号与已授权会话向 /api/monitor/ingest 推送带 observed_at 的结果；格式见接入说明。':file?'由现有识别程序定期写入JSON。结果必须含真实 observed_at；水深使用相对地面的 depth_cm。':'仅允许本机或已登记校内HTTPS接口。JSON须含真实 observed_at；水深使用相对地面的 depth_cm。';
}
function addMonitorSource(event) {
  event.preventDefault();
  if(!Security.guard('can_configure'))return;
  try {
    if(!monitorConfig)throw new Error('请先成功载入监测配置。');
    parseMonitorConfigEditor();
    const mode=$('monitor-source-mode').value,vision=['image_url','image_file'].includes(mode),location=$('monitor-source-location').value.trim();
    if(mode==='rtsp')throw new Error('RTSP直连已禁用，请通过校内HTTPS抓拍网关接入。');
    if(vision&&!$('monitor-vision-consent').checked)throw new Error('启用识别前，请确认可在本机或已登记校内视觉服务处理画面。');
    const source={id:'source_'+Date.now().toString(36)+'_'+Math.random().toString(36).slice(2,6),name:$('monitor-source-name').value.trim(),point_id:$('monitor-source-point').value,kind:mode.startsWith('depth_')?'water_depth':'camera',transport:vision?'vision':mode.split('_')[1],enabled:true,max_age_seconds:300};
    if(!source.name||!source.point_id)throw new Error('请填写来源名称并选择已保存点位。');
    if(vision){source.input_type=mode;source.vision_enabled=true;}
    if(mode==='image_file'||mode.endsWith('_file'))source.path=location;
    else if(!mode.endsWith('_push'))source.url=location;
    monitorConfig.sources.push(source);syncMonitorConfigEditor();renderMonitorConfigSources();markMonitorConfigDirty();
    $('monitor-source-name').value='';$('monitor-source-location').value='';$('monitor-vision-consent').checked=false;
    DraftGuard.saved('monitor-source');
    monitorConfigNotice(`“${source.name}”已加入草稿。点击“保存并启用配置”开始接入。`);
  }catch(e){monitorConfigNotice(e.message,true);}
}
async function saveMonitorConfig() {
  if(monitorConfigBusy||!monitorConfig||!Security.guard('can_configure'))return;
  monitorConfigBusy=true;$('monitor-config-save').disabled=true;$('monitor-source-add').disabled=true;
  const release=DraftGuard.lock('#monitor-config-details');
  try {
    parseMonitorConfigEditor();
    const saved=await api('/api/monitor/config',{revision:monitorRevision,config:monitorConfig});
    monitorRevision=saved.revision ?? saved.config_revision ?? monitorRevision;
    if(saved.config)monitorConfig=copy(saved.config);
    monitorConfigDirty=false;syncMonitorConfigEditor();renderMonitorConfigSources();
    DraftGuard.saved('monitor-config');
    monitorConfigNotice(`配置已保存并启用（版本 ${monitorRevision}）。后台将在采集周期内自动更新。`);
    await refreshMonitor();
  }catch(e){monitorConfigNotice(`保存未完成：${e.message}。你的配置草稿仍保留。`,true);}
  finally{monitorConfigBusy=false;release();$('monitor-config-save').disabled=!Security.can('can_configure');$('monitor-source-add').disabled=!Security.can('can_configure');Security.apply();}
}
async function saveMonitorCorrection(event) {
  event.preventDefault();
  if(!Security.guard('can_operate'))return;
  if(!monitorSnapshot){$('monitor-correction-feedback').textContent='当前监测连接不可用，请恢复连接后再保存。';return;}
  $('monitor-correction-save').disabled=true;
  const release=DraftGuard.lock('#monitor-correction-form');
  try {
    await api('/api/monitor/correction',{point_id:$('monitor-correction-point').value,state:$('monitor-correction-state').value,actor:$('monitor-correction-actor').value.trim(),note:$('monitor-correction-note').value.trim(),ttl_minutes:Number($('monitor-correction-ttl').value)});
    $('monitor-correction-feedback').textContent='人工核验已记录，临时校正到期后自动恢复机器识别。';
    DraftGuard.saved('monitor-correction');
    await refreshMonitor();
  }catch(e){$('monitor-correction-feedback').textContent=`保存失败：${e.message}`;}
  finally{release();$('monitor-correction-save').disabled=!monitorSnapshot||!Security.can('can_operate');Security.apply();}
}
function setupMonitor() {
  monitorPointOptions((bootstrap.demo.points || []).map(p=>({point_id:p.id,point_name:p.name})));
  $('monitor-correction-actor').value=bootstrap.user?.username || '';$('monitor-correction-actor').readOnly=true;
  monitorSourceMode();if(Security.can('can_configure'))loadMonitorConfig();
  if(Security.can('can_configure')){DraftGuard.register('monitor-config','监测配置',()=>[$('monitor-config-json').value,$('monitor-poll-seconds').value,$('monitor-confirm-samples').value]);DraftGuard.register('monitor-source','待加入的监测来源',()=>DraftGuard.snapshot('#monitor-source-form'));}
  if(Security.can('can_operate'))DraftGuard.register('monitor-correction','现场复核记录',()=>DraftGuard.snapshot('#monitor-correction-form'));
  $('monitor-open-config').addEventListener('click',()=>{$('monitor-config-details').open=true;$('monitor-config-details').scrollIntoView({block:'start'});$('monitor-source-name').focus({preventScroll:true});});
  $('monitor-refresh').addEventListener('click',async()=>{
    if(!Security.guard('can_operate'))return;
    $('monitor-refresh').disabled=true;
    try {await api('/api/monitor/refresh');monitorNotice('已提交采集请求，后台完成后自动更新。');setTimeout(refreshMonitor,1500);}
    catch(e){monitorUnavailable(e.message);}
    finally{$('monitor-refresh').disabled=!Security.can('can_operate');Security.apply();}
  });
  $('monitor-source-mode').addEventListener('change',monitorSourceMode);
  $('monitor-source-form').addEventListener('submit',addMonitorSource);
  $('monitor-config-save').addEventListener('click',saveMonitorConfig);
  $('monitor-config-reload').addEventListener('click',loadMonitorConfig);
  $('monitor-config-download').addEventListener('click',()=>download('监测配置草稿.json',$('monitor-config-json').value,'application/json;charset=utf-8'));
  $('monitor-config-json').addEventListener('input',()=>{monitorJsonDirty=true;markMonitorConfigDirty();});
  for(const [id,key] of [['monitor-poll-seconds','poll_seconds'],['monitor-confirm-samples','confirm_samples']]) $(id).addEventListener('change',()=>{
    const value=Number($(id).value);
    try{if(!monitorConfig)return;parseMonitorConfigEditor();monitorConfig[key]=value;syncMonitorConfigEditor();markMonitorConfigDirty();}catch(e){monitorConfigNotice(e.message,true);}
  });
  $('monitor-correction-form').addEventListener('submit',saveMonitorCorrection);
  $('monitor-config-example').addEventListener('click',()=>{
    const point=$('monitor-source-point').value || 'replace_with_saved_point_id';
    const example={note:'默认本机处理；远端只允许已登记的校内HTTPS视觉与抓拍来源，公网视觉和RTSP直连禁用。远端视觉必须配置roi；privacy_masks为人工指定遮挡区域，不是自动人脸或车牌打码。比例均相对原图0..1；示例须按实际视野修改。观测必须有真实时间，推送需授权会话。',config:{poll_seconds:60,depth_watch_cm:2,depth_alert_cm:15,camera_confidence:0.8,confirm_samples:2,sources:[{id:'entrance-camera',name:'入口摄像头',point_id:point,kind:'camera',transport:'vision',input_type:'image_url',url:'http://127.0.0.1:8080/snapshot.jpg',enabled:false,vision_enabled:false,max_age_seconds:300,roi:[0.1,0.1,0.8,0.8],privacy_masks:[]}]},camera_push:{source_id:'your-source-id',observations:[{observed_at:'真实观测时间，ISO 8601且含时区',water_detected:true,confidence:0.9,quality:'good'}]},water_depth_push:{source_id:'your-source-id',observations:[{observed_at:'真实观测时间，ISO 8601且含时区',depth_cm:3,depth_reference:'ground'}]}};
    download('监测接入示例.json',JSON.stringify(example,null,2),'application/json;charset=utf-8');
  });
  setInterval(()=>{if(!document.hidden&&!$('monitor-panel').hidden)refreshMonitor();},20000);
  document.addEventListener('visibilitychange',()=>{if(!document.hidden&&!$('monitor-panel').hidden)refreshMonitor();});
}
