'use strict';
let pilotRevision = 0, activeTrial = null, pilotBusy=false;
function draftField(parent, id, label, value, options=null, multiline=false) {
  const caption=node('label',label);caption.htmlFor=id;
  const input=node(options?'select':multiline?'textarea':'input');input.id=id;
  if(options) for(const [key,label] of Object.entries(options)) {const o=node('option',label);o.value=key;input.append(o);}
  else {input.maxLength=multiline?2000:500;if(multiline) input.rows=3;}
  input.value=value || '';parent.append(caption,input);return input;
}
function renderPilot() {
  const site=dataset.site, auth=site.authorization || {};
  $('pilot-site-name').value=site.name;$('pilot-scope').value=site.scope || '';
  $('pilot-site-status').value=site.status==='verified'?'verified':'pending';
  $('pilot-auth-status').value=auth.status || 'pending';$('pilot-approver').value=auth.approver_role || '';
  $('pilot-auth-ref').value=auth.reference || '';$('pilot-auth-time').value=beijingInput(auth.confirmed_at);
  $('pilot-point-editor').replaceChildren();$('pilot-plan-editor').replaceChildren();
  for(const [index,p] of dataset.points.entries()) {
    const row=node('details',undefined,'editor-row');row.append(node('summary',`${p.id} · ${p.name}`));const grid=node('div',undefined,'field-grid');
    draftField(grid,`point-${index}-name`,'现场名称',p.name);
    draftField(grid,`point-${index}-type`,'核实后的点位类型',p.type,{underground:'地下入口',entrance:'楼宇入口',low_path:'通道（低洼性质另核实）',outdoor:'室外设施',service:'信息联络点'});
    draftField(grid,`point-${index}-source`,'资料来源 / 台账编号',p.source);
    draftField(grid,`point-${index}-status`,'核验状态',p.status==='verified'?'verified':'pending',{pending:'待核实',verified:'用户标记已核验'});
    draftField(grid,`point-${index}-by`,'核验人角色',p.verified_by);
    const time=draftField(grid,`point-${index}-at`,'核验时间（北京）',beijingInput(p.verified_at));time.type='datetime-local';
    row.append(grid);draftField(row,`point-${index}-features`,'已确认特征，每行一项；未知请写明',(p.features || []).join('\n'),null,true);
    $('pilot-point-editor').append(row);
  }
  for(const [index,p] of dataset.plans.entries()) {
    const row=node('details',undefined,'editor-row');row.append(node('summary',`${p.id} · ${p.title}`));
    draftField(row,`plan-${index}-action`,'经审核的任务内容',p.action,null,true);
    const grid=node('div',undefined,'field-grid');draftField(grid,`plan-${index}-role`,'责任角色',p.role);draftField(grid,`plan-${index}-source`,'预案条款 / 授权清单来源',p.source);
    draftField(grid,`plan-${index}-status`,'审核状态',p.status,{draft:'团队草稿',verified:'管理方已审核'});
    draftField(grid,`plan-${index}-by`,'审核人角色',p.verified_by);const time=draftField(grid,`plan-${index}-at`,'审核时间（北京）',beijingInput(p.verified_at));time.type='datetime-local';
    row.append(grid);$('pilot-plan-editor').append(row);
  }
  DraftGuard.render();
}
function collectPilot() {
  const next=copy(dataset), s=next.site;
  s.name=$('pilot-site-name').value.trim();s.scope=$('pilot-scope').value.trim();s.status=$('pilot-site-status').value;
  s.campus='紫金港校区';s.region='杭州市';
  s.authorization={status:$('pilot-auth-status').value,approver_role:$('pilot-approver').value.trim(),reference:$('pilot-auth-ref').value.trim(),confirmed_at:$('pilot-auth-time').value?$('pilot-auth-time').value+':00+08:00':''};
  next.points.forEach((p,i)=>{for(const key of ['name','type','source','status'])p[key]=$(`point-${i}-${key}`).value.trim();p.verified_by=$(`point-${i}-by`).value.trim();p.verified_at=$(`point-${i}-at`).value?$(`point-${i}-at`).value+':00+08:00':'';p.features=$(`point-${i}-features`).value.split('\n').map(v=>v.trim()).filter(Boolean);p.campus=s.campus;p.region=s.region;});
  next.plans.forEach((p,i)=>{for(const key of ['action','role','source','status'])p[key]=$(`plan-${i}-${key}`).value.trim();p.verified_by=$(`plan-${i}-by`).value.trim();p.verified_at=$(`plan-${i}-at`).value?$(`plan-${i}-at`).value+':00+08:00':'';});
  return next;
}
function renderReadiness(report) {
  $('readiness-count').textContent=`${report.passed} / ${report.total} 项已就绪`;
  $('readiness-list').replaceChildren();
  for(const check of report.checks) {const row=node('div',undefined,'readiness-row');row.append(node('span',check.passed?'已记录':'待补齐','tag '+(check.passed?'fresh-tag':'amber')),node('div',check.label),node('p',check.next_action,'help'));$('readiness-list').append(row);}
  $('shadow-option').disabled=!report.shadow_ready;
  if(!report.shadow_ready)$('operation-mode').value='tabletop';
}
async function inspectAgent(data=dataset) {
  try {const result=await api('/api/agent/inspect',{data});renderReadiness(result.readiness);$('outbound-preview').textContent=JSON.stringify(result.outbound_preview,null,2);$('input-hash').textContent='资料指纹 '+result.input_fingerprint.slice(0,16);return result;}
  catch(e){message(e.message,true);throw e;}
}
async function savePilot(e) {
  e.preventDefault();if(activeTrial)return message('请先完成或中止计时试验，再更改试点资料。',true);
  if(pilotBusy)return;if(!DraftGuard.confirmDiscard(['dataset-json'],'用建档表单保存并刷新完整JSON'))return;
  pilotBusy=true;const release=DraftGuard.lock('#pilot-form');const button=$('save-pilot');button.disabled=true;
  try{const next=collectPilot();const result=await api('/api/pilot',{action:'save',revision:pilotRevision,data:next});pilotRevision=result.revision;dataset=result.data;bootstrap.demo=copy(dataset);fill();DraftGuard.saved('pilot');invalidate();renderReadiness(result.readiness);message('试点资料已保存到本机。真实核验与授权由资料提供者负责；旧运行记录保留。');}
  catch(e){message(e.message,true);}finally{pilotBusy=false;release();button.disabled=false;Security.apply();}
}
function renderTrial() {
  $('trial-start').hidden=Boolean(activeTrial);$('trial-finish').hidden=!activeTrial;
  $('trial-status').textContent=activeTrial?`${activeTrial.status==='active'?'计时中':'已停止计时 · 等待评分'} · ${activeTrial.id} · ${activeTrial.participant} · ${{manual:'原始资料',rules:'规则模板',ai:'AI辅助'}[activeTrial.condition]} · 开始 ${localTime(activeTrial.started_at)}`:'尚未开始计时试验';
  $('trial-clock').textContent=activeTrial?`${activeTrial.status==='active'?Math.max(0,Math.floor((Date.now()-new Date(activeTrial.started_at))/1000)):activeTrial.duration_seconds} 秒`:'—';
  $('stop-trial').hidden=!activeTrial || activeTrial.status!=='active';
  Security.apply();
}
async function loadTrials() {
  const result=await api('/api/trials');
  const open=result.trials.filter(t=>['active','awaiting_score'].includes(t.status));
  $('trial-records').replaceChildren();
  for(const t of result.trials) {
      const row=node('article',undefined,'surface history-row');const details=node('div');details.append(node('strong',`${t.participant || '参与身份仅管理人员可见'} / ${t.scenario_id} / ${{manual:'原始资料',rules:'规则模板',ai:'AI辅助'}[t.condition]}`),node('p',`${{active:'计时中',awaiting_score:'等待评分',completed:'已完成',aborted:'已中止'}[t.status]} · ${t.duration_seconds===null?'尚未结束':t.duration_seconds+'秒'} · ${t.id}`,'help'));
    if(t.scoring)details.append(node('p',`人工评分：关键错误${t.scoring.critical_errors} · 遗漏${t.scoring.omissions} · 无依据${t.scoring.unsupported} · 求助${t.scoring.help_count}`,'help'));
    row.append(details);
    if(Security.can('can_operate') && t.input_snapshot && ['active','awaiting_score'].includes(t.status)){const resume=node('button','恢复试验','button secondary');resume.addEventListener('click',async()=>{if(activeTrial&&activeTrial.id!==t.id)return message('请先结束当前计时试验。',true);if(!allowDatasetReplace('恢复试验资料'))return;dataset=copy(t.input_snapshot);fill();invalidate();activeTrial=t;renderTrial();if(t.run_id){const run=t.result_at_finish || await api('/api/run',{run_id:t.run_id});renderResult(run);}panel('experiment-panel');message('已恢复试验记录；运行中的计时包含离开页面期间的时间。');});row.append(resume);}
    $('trial-records').append(row);
  }
  if(!result.trials.length)$('trial-records').append(node('p','暂无试验记录。空白不代表零错误或通过。','help'));
  $('active-trial-note').textContent=open.length?`本机有${open.length}项未结束试验，可在下方恢复。`:'使用匿名编号；原始资料、规则和AI条件分别记录，不合并为AI效果。';
  return result;
}
function setupPilot() {
  pilotRevision=bootstrap.pilot_revision;renderPilot();inspectAgent().catch(()=>{});renderTrial();
  DraftGuard.register('pilot','试点建档',()=>DraftGuard.snapshot('#pilot-form'));
  $('pilot-form').addEventListener('submit',savePilot);
  $('trial-outcome').addEventListener('change',()=>{for(const id of ['critical-errors','omissions','unsupported','help-count']){$(id).required=$('trial-outcome').value==='completed';$(id).disabled=$('trial-outcome').value==='aborted';}});
  $('stop-trial').addEventListener('click',async()=>{if(!activeTrial)return;const button=$('stop-trial');button.disabled=true;try{activeTrial=await api('/api/trials/stop',{trial_id:activeTrial.id});renderTrial();message('任务计时已停止，输出快照已保存。现在填写评分，评分时间不计入任务耗时。');}catch(e){message(e.message,true);}finally{button.disabled=false;}});
  $('inspect-agent').addEventListener('click',()=>{syncWeather();inspectAgent().catch(()=>{});});
  $('download-pilot-template').addEventListener('click',()=>download('紫金港点位与预案采集模板.json',JSON.stringify(bootstrap.pilot_template,null,2),'application/json;charset=utf-8'));
  $('trial-start').addEventListener('submit',async e=>{e.preventDefault();syncWeather();const button=$('start-trial');button.disabled=true;try{const values=Object.fromEntries(new FormData(e.target));if(values.condition==='ai'&&!bootstrap.ai_configured)throw new Error('AI尚未配置，请先选择原始资料或规则条件。');activeTrial=await api('/api/trials/start',{...values,data:copy(dataset)});invalidate();renderTrial();message('计时已开始。按分配条件完成任务，结束时由观察员评分；输入变化会阻止绑定。');loadTrials().catch(()=>{});}catch(err){message(err.message,true);}finally{button.disabled=false;}});
  $('trial-finish').addEventListener('submit',async e=>{e.preventDefault();if(!activeTrial)return;const values=Object.fromEntries(new FormData(e.target));for(const k of ['critical_errors','omissions','unsupported','help_count'])values[k]=Number(values[k]);const button=$('finish-trial');button.disabled=true;try{await api('/api/trials/finish',{trial_id:activeTrial.id,...values});activeTrial=null;renderTrial();e.target.reset();for(const id of ['critical-errors','omissions','unsupported','help-count']){$(id).required=true;$(id).disabled=false;}await loadTrials();message('试验已封存。耗时与评分保留，后续任务修改不会覆盖本次完成时快照。');}catch(err){message(err.message,true);}finally{button.disabled=false;}});
  $('refresh-trials').addEventListener('click',()=>loadTrials().catch(e=>message(e.message,true)));
  $('export-trials').addEventListener('click',async()=>{try{const result=await loadTrials();download('紫金港试验记录.json',JSON.stringify(result,null,2),'application/json;charset=utf-8');}catch(e){message(e.message,true);}});
  setInterval(renderTrial,1000);
}

