'use strict';
const ResponsePlans = (() => {
  const fields=['title','contact_roster','resources','activation','escalation','evacuation','recheck'];
  const labels={contact_roster:'责任岗位与联络',resources:'物资与设施',activation:'启动与停止条件',escalation:'报告与升级路径',evacuation:'经确认的疏散安排',recheck:'复查与交接安排'};
  let initialized=false, contextLoaded=false, busy=false, revision=null, current=null, points=[], history=[], total=0, nextBefore=null, unlock=null, loadedHistoryPath=null;
  const el=id=>document.getElementById(id);
  function feedback(text,error=false){const n=el('response-feedback');n.hidden=!text;n.textContent=text;n.className=error?'message error':'message';}
  function lock(value){if(value&&!busy)unlock=DraftGuard.lock('#response-panel');if(!value&&busy){unlock?.();unlock=null;}busy=value;if(!value)for(const box of el('response-points').querySelectorAll('input'))box.disabled=false;for(const button of el('response-panel').querySelectorAll('button'))button.disabled=value;el('response-save').disabled=value||revision===null;for(const name of ['html','json'])el('response-export-'+name).disabled=value||!current?.id;el('response-review-save').disabled=value||!current?.id;el('response-history-more').disabled=value||nextBefore===null;Security.apply();}
  async function request(path,payload){
    const options={signal:AbortSignal.timeout(20000)};
    if(payload!==undefined)Object.assign(options,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    const response=await Security.fetch(path,options);const data=await response.json();
    if(!response.ok)throw new Error(data.error||'预案操作未完成，请重试。');return data;
  }
  function selected(){return [...el('response-points').querySelectorAll('input:checked')].map(n=>n.value);}
  function input(){const point_ids=selected();if(!point_ids.length)throw new Error('请至少选择一个已建档点位。');return {point_ids,details:Object.fromEntries(fields.map(key=>[key,el('response-'+key).value.trim()]))};}
  function drawPoints(ids){const host=el('response-points');host.replaceChildren();for(const point of points){const label=node('label',undefined,'check response-point-choice');const box=document.createElement('input');box.type='checkbox';box.disabled=busy;box.value=point.id;box.checked=ids?ids.includes(point.id):true;label.append(box,node('span',`${point.name} · ${point.status==='verified'?'已登记核验':'待核验'}`));host.append(label);}if(!points.length)host.append(node('p','尚无已保存点位，请管理员先完成试点建档。','help'));}
  function historyPath(before=null){const query=new URLSearchParams({limit:'30'});for(const [key,id] of [['q','response-history-q'],['point_id','response-history-point'],['status','response-history-status']]){const value=el(id).value.trim();if(value)query.set(key,value);}if(before!==null)query.set('before',String(before));return '/api/response-plans?'+query;}
  function acceptHistory(list,append=false){if(!append)loadedHistoryPath=historyPath();revision=list.revision;total=list.total??list.records.length;nextBefore=list.next_before??null;history=append?[...history,...list.records.filter(row=>!history.some(old=>old.id===row.id))]:list.records;drawHistory(history);}
  async function fetchHistory(append=false){if(busy||!Security.can('can_operate')||(append&&nextBefore===null))return;if(append&&historyPath()!==loadedHistoryPath)append=false;lock(true);feedback('');try{acceptHistory(await request(historyPath(append?nextBefore:null)),append);}catch(error){feedback(error.message,true);}finally{lock(false);}}
  function filterPoints(items){const select=el('response-history-point'),previous=select.value;select.replaceChildren(new Option('全部点位',''));for(const point of items)select.append(new Option(point.name+' · '+point.id,point.id));if(previous&&!items.some(point=>point.id===previous))select.append(new Option(previous+'（历史点位）',previous));select.value=previous;}
  async function checkCurrent(){
    if(!current?.id){if(current)el('response-freshness').textContent='尚未保存的预览，请重新生成预览获取当前依据。';return;}
    try{const result=await request('/api/response-plans/'+encodeURIComponent(current.id));draw(result.document,true);}
    catch(error){el('response-freshness').textContent='时效未能重新核验：'+error.message+'。上次快照不代表当前现场。';el('response-freshness').className='message error';}
  }
  async function refreshContext(){
    if(!Security.can('can_operate')||busy)return;lock(true);feedback('');
    try{const [pilot,list]=await Promise.all([request('/api/pilot',{action:'load'}),request(historyPath())]);const first=!contextLoaded;const ids=first?null:selected();if(first||!DraftGuard.dirty('response')){points=pilot.data.points;drawPoints(ids);if(!el('response-title').value)el('response-title').value=`${pilot.data.site.name} · 防汛应急预案草案`;DraftGuard.saved('response');}else feedback('已刷新版本；未保存的编辑和点位选择已保留。保存时会再次验证点位。');filterPoints(pilot.data.points);contextLoaded=true;acceptHistory(list);el('response-site').textContent=`${pilot.data.site.name} · 已保存建档版本 ${pilot.revision}`;await checkCurrent();}
    catch(error){feedback(error.message,true);if(current){el('response-freshness').textContent='时效未能重新核验：刷新资料失败。上次快照不代表当前现场。';el('response-freshness').className='message error';}}finally{lock(false);}
  }
  function drawHistory(records){
    el('response-revision').textContent=`最新 V${revision} · 显示 ${records.length} / ${total} 条`;
    el('response-history-more').hidden=nextBefore===null;
    const host=el('response-history');host.replaceChildren();
    for(const row of records){const article=node('article',undefined,'response-history-row');const button=node('button',row.title,'button secondary');button.type='button';button.dataset.version=row.id;button.addEventListener('click',()=>load(row.id));article.append(node('p',`V${row.revision} · ${row.status==='reviewed_draft'?'内部复核草案':'待复核草案'}`,'eyebrow'),button,node('p',`${formatTime(row.created_at)} · ${row.created_by}`,'help'));host.append(article);}
    if(!records.length)host.append(node('p',revision?'没有符合筛选条件的版本。':'还没有保存的预案。完成左侧资料后，先预览再保存首个版本。','help'));
  }
  function formatTime(value){const date=new Date(value);return value&&!Number.isNaN(date.getTime())?date.toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}):'时间未提供';}
  function listSection(title,items,className=''){const section=node('section',undefined,`response-section ${className}`);section.append(node('h3',title));const list=node('ul',undefined,'response-list');for(const item of items)list.append(node('li',String(item)));section.append(list);return section;}
  function draw(doc,preserveReview=false){
    current=doc;const host=el('response-document');host.replaceChildren();el('response-preview').hidden=false;
    el('response-document-title').textContent=doc.title;
    el('response-document-status').textContent=doc.status_label||'应急预案草案';
    el('response-document-meta').textContent=`${doc.revision?`版本 V${doc.revision}`:'预览，尚未保存'} · 编制时间 ${formatTime(doc.created_at)} · 观测快照 ${formatTime(doc.observations.captured_at)}`;
    el('response-freshness').textContent=`时效检查 ${formatTime(doc.update_status?.checked_at)} · ${doc.update_status?.needs_refresh?'资料或监测已变化，请重新预览编制。':'执行前仍须重新核验当前现场。'}`;el('response-freshness').className='help';
    if(doc.update_status?.messages?.length)host.append(listSection('时效与更新提醒',doc.update_status.messages,'response-warning'));
    if(doc.missing_items.length)host.append(listSection(`待补充与待核验 · ${doc.missing_items.length}项`,doc.missing_items,'response-warning'));
    const scope=node('section',undefined,'response-section');scope.append(node('h3','适用场地与边界'),node('p',`${doc.scope.site_name} / ${doc.scope.region} / ${doc.scope.campus||'校区未填'}`),node('p',doc.scope.description||'适用边界待补充'));host.append(scope);
    const observed=node('section',undefined,'response-section');observed.append(node('h3','编制时的监测依据'),node('p',`天气参考：${doc.observations.weather.label}。现场情况以各点位来源为准。`));
    for(const point of doc.observations.points){const row=node('article',undefined,'response-observation');row.append(node('h4',`${point.name} · ${point.label}`),node('p',`观测时间：${formatTime(point.observed_at)}；点位${point.verified?'已登记核验':'待核验'}`,'help'));for(const basis of point.basis||[])row.append(node('p',basis));for(const source of point.sources||[])row.append(node('p',`${source.name||source.id} · ${source.status} · ${formatTime(source.observed_at)}`,'help'));observed.append(row);}host.append(observed);
    const tasks=node('section',undefined,'response-section');tasks.append(node('h3','匹配条目与待核验任务'));
    if(!doc.tasks.length)tasks.append(node('p','没有适用于所选点位的有效条目，请管理员补充来源与适用范围。'));
    for(const task of doc.tasks){const article=node('article',undefined,'response-task');article.append(node('h4',`${task.point_name} · ${task.title}`),node('p',task.action),node('p',`责任岗位：${task.role}；条目：${task.plan_id}；${task.trigger_state==='signal_present'?'有监测线索，待现场核验':'备查条目，未自动启动'}`,'help'),node('p',`来源状态：${task.source_status}；执行前须人工核验。`,'help'));tasks.append(article);}host.append(tasks);
    for(const [key,label] of Object.entries(labels)){const section=node('section',undefined,'response-section');section.append(node('h3',label),node('p',doc.details[key]||'待管理方补充并核验。','response-preserve'));host.append(section);}
    host.append(listSection('资料来源',doc.sources.length?doc.sources.map(s=>`${s.id} · ${s.title} · ${s.source} · ${s.status}`):['没有可引用的预案条目来源。']));
    if(doc.review)host.append(listSection('内部复核记录',[`${doc.review.by} · ${formatTime(doc.review.at)}`,doc.review.note,'内部复核不等于正式发布或授权执行。']));
    host.append(listSection('使用限制',doc.limitations));if(!preserveReview){el('response-review-note').value='';DraftGuard.saved('response-review');}lock(busy);
  }
  async function compose(save){
    if(busy||!Security.guard('can_operate'))return;
    if(!DraftGuard.confirmDiscard(['response-review'],'重新编制预案'))return;
    try{const payload=input();if(save){if(revision===null)throw new Error('请先刷新建档与版本。');payload.expected_revision=revision;}lock(true);feedback('');const result=await request('/api/response-plans/'+(save?'save':'preview'),payload);draw(result.document);let warning='';if(save){revision=result.revision;DraftGuard.saved('response');try{acceptHistory(await request(historyPath()));}catch(error){warning=`版本 V${result.document.revision} 已保存，但版本列表刷新失败：${error.message}。不必重复保存，可稍后刷新列表。`;}}feedback(warning||(save?'已保存独立版本，原版本保留。导出仍标注为草案。':'预览已生成，尚未保存。所用的是服务器已保存建档与本次监测快照，请核对待补项。'),Boolean(warning));el('response-preview').scrollIntoView({block:'start'});}
    catch(error){feedback(error.message,true);}finally{lock(false);}
  }
  async function load(id){if(busy||!Security.guard('can_operate')||!DraftGuard.confirmDiscard(['response','response-review'],'读取历史版本'))return;lock(true);feedback('');try{const result=await request('/api/response-plans/'+encodeURIComponent(id));draw(result.document);for(const key of fields)el('response-'+key).value=current.details[key]||'';drawPoints(current.scope.point_ids);DraftGuard.saved('response');feedback('已读取历史版本。修改后保存会生成新版本，历史观测不会自动更新。');el('response-preview').scrollIntoView({block:'start'});}catch(error){feedback(error.message,true);}finally{lock(false);}}
  async function review(event){event.preventDefault();if(busy||!current?.id||!Security.guard('can_configure'))return;lock(true);feedback('');try{const result=await request('/api/response-plans/review',{id:current.id,expected_revision:revision,review_note:el('response-review-note').value.trim()});draw(result.document);revision=result.revision;try{acceptHistory(await request(historyPath()));feedback('内部复核已保存为新版本；复核针对展示的已保存版本，不包含表单中尚未保存的编辑。仍为草案，尚未正式发布。');}catch(error){feedback(`内部复核版本 V${result.document.revision} 已保存，但版本列表刷新失败：${error.message}。不必重复提交，可稍后刷新列表。`,true);}}catch(error){feedback(error.message,true);}finally{lock(false);}}
  async function exportFile(format){if(busy||!current?.id||!Security.guard('can_operate'))return;lock(true);feedback('');try{const response=await Security.fetch(`/api/response-plans/${encodeURIComponent(current.id)}/export?format=${format}`,{signal:AbortSignal.timeout(20000)});if(!response.ok){const data=await response.json();throw new Error(data.error||'导出失败。');}const blob=await response.blob();const url=URL.createObjectURL(blob);const link=document.createElement('a');link.href=url;link.download=`应急预案草案_V${current.revision}.${format}`;document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);feedback('导出完成。文件含你录入的联络与资源资料，请按内部资料保管。');}catch(error){feedback(error.message,true);}finally{lock(false);}}
  function setup(){if(initialized)return;initialized=true;DraftGuard.register('response','预案编制',()=>({details:fields.map(key=>el('response-'+key).value),point_ids:selected()}));DraftGuard.register('response-review','内部复核意见',()=>el('response-review-note').value);el('response-refresh').addEventListener('click',refreshContext);el('response-history-filter').addEventListener('submit',event=>{event.preventDefault();fetchHistory();});el('response-history-more').addEventListener('click',()=>fetchHistory(true));el('response-form').addEventListener('submit',event=>{event.preventDefault();compose(false);});el('response-save').addEventListener('click',()=>compose(true));el('response-review-form').addEventListener('submit',review);for(const name of ['html','json'])el('response-export-'+name).addEventListener('click',()=>exportFile(name));}
  return {setup,open:async()=>{if(!Security.guard('can_operate')||busy)return;if(!contextLoaded)return refreshContext();lock(true);try{await checkCurrent();}finally{lock(false);}}};
})();
