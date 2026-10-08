'use strict';
const stateLabels = {pending:'待人工核验',confirmed:'已确认',processing:'处理中',completed:'已完成',rejected:'已排除'};
const nextStates = {pending:['pending','confirmed','rejected'],confirmed:['confirmed','processing','pending','rejected'],processing:['processing','completed','pending'],completed:['completed','pending'],rejected:['rejected','pending']};
let workflowBusy = false;

async function api(path, payload = {}) {
  const permission=path==='/api/monitor/config'||(path==='/api/pilot'&&payload.action==='save')?'can_configure':
    ['/api/workflow','/api/generate','/api/live/reference','/api/monitor/refresh','/api/monitor/correction','/api/trials/start','/api/trials/stop','/api/trials/finish'].includes(path)?'can_operate':null;
  if(permission&&!Security.guard(permission))throw new Error('当前账户没有此项操作权限。');
  const response = await Security.fetch(path, {method:'POST',headers:{'Content-Type':'application/json','X-App-Token':Security.csrf()},body:JSON.stringify(payload),signal:AbortSignal.timeout(60000)});
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || '操作未完成。');
  return value;
}
function overview() {
  const cards = latestResult?.cards || [];
  $('overview-site').textContent = dataset?.site?.campus ? `${dataset.site.campus} · ${dataset.site.status==='verified'?'已建档':'待核实'}` : dataset?.site?.name || '待载入';
  for (const [id,states] of Object.entries({pending:['pending'],active:['confirmed','processing'],done:['completed']})) {
    $('overview-'+id).textContent = latestResult ? String(cards.filter(c=>states.includes(c.workflow_state || 'pending')).length) : '—';
  }
}
function field(form, name, label, value, options = null) {
  const id = form.id + '-' + name;
  const caption = node('label',name==='actor'?'记录账户（当前登录）':label); caption.htmlFor = id;
  const input = node(options ? 'select' : name === 'action' || name === 'note' ? 'textarea' : 'input');
  input.id=id; input.name=name; input.required=true;
  if (options) for (const [key,text] of Object.entries(options)) {const option=node('option',text);option.value=key;input.append(option);}
  else {input.maxLength = name === 'actor' ? 100 : name === 'role' ? 200 : 2000;if(input.tagName==='TEXTAREA') input.rows=3;}
  input.value=name==='actor'?(bootstrap.user?.username || ''):(value || '');
  if(name==='actor')input.readOnly=true;
  form.append(caption,input);return input;
}
async function saveWorkflow(payload, form, target = 'results-panel') {
  if (workflowBusy || !Security.guard('can_operate')) return;
  workflowBusy = true;
  const controls = [...document.querySelectorAll('button,input,textarea,select')];
  const disabled = controls.map(el=>el.disabled);
  controls.forEach(el=>el.disabled=true);
  try {
    const result = await api('/api/workflow',{run_id:latestResult.run_id,revision:latestResult.revision,...payload});
    renderResult(result);panel(target);message('已保存至本机记录，操作与修改内容可回查。');
  } catch(e) {message(e.message,true);form.querySelector('.form-error')?.remove();const error=node('p',e.message,'form-error');error.setAttribute('role','alert');form.append(error);}
  finally {workflowBusy=false;controls.forEach((el,index)=>el.disabled=disabled[index]);Security.apply();}
}
function renderCards() {
  if (!latestResult) return;
  const query = $('task-search').value.trim().toLowerCase(), state=$('task-filter').value;
  const cards=latestResult.cards.filter(c=>(state==='all' || (c.workflow_state || 'pending')===state) && `${c.point_name} ${c.action} ${c.role} ${c.point_id} ${c.plan_id}`.toLowerCase().includes(query));
  $('filter-count').textContent=`显示 ${cards.length} / ${latestResult.cards.length} 项任务`;
  $('cards').replaceChildren();
  for (const c of cards) {
    const article=node('article',undefined,'action-card');article.dataset.state=c.workflow_state || 'pending';
    const head=node('div',undefined,'card-head');head.append(node('h3',c.point_name),node('span',c.status,'card-status'));
    const body=node('div',undefined,'card-body');
    body.append(node('span',`${c.point_id} / ${c.plan_id} · ${c.title}`,'card-label'),node('p',c.action),node('span','责任角色','card-label'),node('p',c.role));
    const evidence=node('details',undefined,'evidence');evidence.append(node('summary','查看匹配依据与来源'),node('p',c.reason),node('div',`${c.point_id} · ${c.point_source}`),node('div',`${c.plan_id} · ${c.plan_source}`),node('div',c.data_status));body.append(evidence);
    if(c.reviewed_by) body.append(node('p',`${c.reviewed_by} · ${localTime(c.reviewed_at)} · ${c.review_note}`,'help'));
    const details=node('details',undefined,'review-details');details.append(node('summary',c.workflow_state==='pending'?'审核与修改任务':'更新处理状态'));
    const form=node('form');form.id='review-'+c.card_id;
    field(form,'action','任务内容',c.action);field(form,'role','责任角色',c.role);
    field(form,'state','保存后的状态',c.workflow_state,Object.fromEntries((nextStates[c.workflow_state] || nextStates.pending).map(s=>[s,stateLabels[s]])));
    field(form,'field_status','人工核验的现场状态',c.field_status,{unknown:'未知 / 尚未核验',closed:'演练确认：暂时关闭',restricted:'演练确认：限制使用'});
    field(form,'actor','本次操作人 / 角色',c.reviewed_by || '');
    field(form,'note','本次核验依据或处理说明','');
    form.append(node('p','现场状态与任务完成状态分别记录。修改已确认内容时，选择“待人工核验”后重新审核。','help'));
    const save=node('button','保存审核','button primary');save.type='submit';form.append(save);
    form.addEventListener('submit',e=>{e.preventDefault();saveWorkflow({operation:'review',card_id:c.card_id,...Object.fromEntries(new FormData(form))},form);});details.append(form);body.append(details);
    if (['confirmed','processing','completed'].includes(c.workflow_state)) {
      if(c.field_status==='unknown') body.append(node('p','现场状态仍未知，暂不能生成设施状态提示。','help'));
      else {
        const noticeForm=node('form');noticeForm.id='notice-'+c.card_id;
        field(noticeForm,'actor','草稿整理人 / 角色','');
        const button=node('button','生成公众提示草稿','button secondary');button.type='submit';noticeForm.append(button);
        noticeForm.addEventListener('submit',e=>{e.preventDefault();saveWorkflow({operation:'notice',card_id:c.card_id,actor:new FormData(noticeForm).get('actor')},noticeForm,'public-panel');});body.append(noticeForm);
      }
    }
    article.append(head,body);$('cards').append(article);
  }
  if(!cards.length) $('cards').append(node('div',latestResult.cards.length?'没有符合筛选条件的任务。':'没有匹配任务，请检查资料与适用条件；这不代表场地安全。','surface'));
  Security.apply();
}
function renderWorkflow() {
  overview();
  const agent=latestResult.agent;
  $('agent-trace').replaceChildren(node('p','AGENT · 可回查的匹配过程','eyebrow'));
  const steps=node('ol',undefined,'trace-steps');
  for (const step of agent?.steps || [{title:'历史版本记录',detail:'该次运行未记录逐阶段信息，可重新生成以查看。'}]) {const li=node('li');li.append(node('strong',step.title),node('p',step.detail));steps.append(li);}
  $('agent-trace').append(steps);
  if(agent?.omitted_candidates?.length) $('agent-trace').append(node('p','需复核的未选候选：'+agent.omitted_candidates.map(c=>`${c.point_id}/${c.plan_id}`).join('、'),'help'));
  $('audit-list').replaceChildren();
  for(const event of [...(latestResult.audit || [])].reverse()) $('audit-list').append(node('p',`#${event.revision} · ${localTime(event.at)} · ${event.actor} · ${{review:'审核任务',notice:'生成提示草稿',feedback:'记录反馈'}[event.operation]}${event.detail.note ? ' · '+event.detail.note : ''}`,'audit-row'));
  if(!latestResult.audit?.length) $('audit-list').append(node('p','尚无人工操作记录。','help'));
  renderCards();renderNotices();
}
function renderNotices() {
  $('notice-list').replaceChildren();
  const notices=latestResult?.notices || [];
  if(!notices.length) {const empty=node('div',undefined,'surface empty-state');empty.append(node('p','还没有公众提示草稿。'),node('p','先审核行动清单，并记录有依据的现场状态。'));$('notice-list').append(empty);return;}
  for(const n of [...notices].reverse()) {
    const card=node('article',undefined,'surface notice-card');
    card.append(node('span',n.revoked?'已失效 · 任务有更新':'演练草稿 · 未发布','tag '+(n.revoked?'amber':'fresh-tag')),node('h3',n.text),node('p',`确认人：${n.confirmed_by || '仅管理人员可见'} · ${localTime(n.confirmed_at)}`,'help'),node('p',`人工依据：${n.confirmation_note || '仅管理人员可见'}`,'help'),node('p',`资料引用：${n.source || '未提供'}`,'help'),node('p',`整理人：${n.actor || '仅管理人员可见'} · 更新于 ${localTime(n.created_at)}`,'help'));
    if(!n.revoked) {
      const exportButton=node('button','下载提示草稿','button secondary');exportButton.addEventListener('click',()=>download(`演练提示_${latestResult.run_id}_${n.id}.txt`,`${n.text}\n仅供本地演练，未对外发布。\n来源：${n.source || '未提供'}\n确认：${n.confirmed_by || '仅管理人员可见'} / ${n.confirmed_at || '未提供'}\n依据：${n.confirmation_note || '仅管理人员可见'}\n更新时间：${n.created_at}`,'text/plain;charset=utf-8'));card.append(exportButton);
      const details=node('details',undefined,'review-details');details.append(node('summary','记录演练反馈'));
      const form=node('form');form.id='feedback-'+n.id;field(form,'actor','反馈人 / 角色','');field(form,'status','反馈类型','received',{received:'已接收',question:'需要解释',handled:'已处理'});field(form,'note','反馈说明','');const save=node('button','保存反馈','button primary');save.type='submit';form.append(save);
      form.addEventListener('submit',e=>{e.preventDefault();saveWorkflow({operation:'feedback',notice_id:n.id,...Object.fromEntries(new FormData(form))},form,'public-panel');});details.append(form);card.append(details);
    }
    for(const f of n.feedback || []) card.append(node('p',`${localTime(f.at)} · ${f.actor || '仅管理人员可见'} · ${{received:'已接收',question:'需要解释',handled:'已处理'}[f.status]}：${f.note || '反馈详情仅管理人员可见'}`,'audit-row'));
    $('notice-list').append(card);
  }
  Security.apply();
}
async function loadHistory() {
  $('history-list').replaceChildren(node('p','正在读取本机记录…','help'));
  try {
    const {runs}=await api('/api/runs');$('history-list').replaceChildren();
    for(const run of runs) {
      const row=node('article',undefined,'surface history-row'),info=node('div');info.append(node('strong',run.site),node('p',`${localTime(run.created_at)} · ${run.mode==='ai'?'AI辅助':'规则演练'} · ${run.count}项任务 · ${run.run_id}`,'help'));
      const button=node('button','打开记录','button secondary');button.addEventListener('click',async()=>{button.disabled=true;try{const result=await api('/api/run',{run_id:run.run_id});if(result.input_snapshot){if(!allowDatasetReplace('恢复历史运行资料'))return;dataset=copy(result.input_snapshot);fill();invalidate();}$('task-search').value='';$('task-filter').value='all';renderResult(result);message(result.input_snapshot?'已恢复该次输入与审核记录。气象信息仍保留原始时间，不代表当前状态。':'已只读打开历史结果。原始输入与私密核验记录仅管理人员可见；历史气象不代表当前状态。');}catch(e){message(e.message,true);}finally{button.disabled=false;Security.apply();}});row.append(info,button);$('history-list').append(row);
    }
    if(!runs.length) $('history-list').append(node('div','尚无运行记录。完成一次匹配后将自动保存。','surface empty-state'));
  }catch(e){$('history-list').replaceChildren(node('p',e.message,'message error'));}
}
