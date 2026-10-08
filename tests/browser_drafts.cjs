// Run against an isolated local service only; never production port 8765.
'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {randomBytes}=require('node:crypto');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const checks=[],errors=[];
const check=(name,condition)=>{assert.ok(condition,name);checks.push(name);};

(async()=>{
  const target=new URL(process.env.DRAFTS_LIVE_URL||process.env.RESPONSE_PLANS_LIVE_URL||'');
  assert.ok(target.protocol==='http:'&&['127.0.0.1','localhost'].includes(target.hostname)&&target.port&&target.port!=='8765','Use an explicit isolated localhost port, never 8765.');
  assert.ok(target.pathname==='/'&&!target.username&&!target.password&&!target.search&&!target.hash,'Use a bare test origin.');
  assert.ok(process.env.TEST_USERNAME&&process.env.TEST_PASSWORD,'Provide isolated test credentials.');
  const output=path.resolve(process.env.EVIDENCE_DIR||path.join(__dirname,'../evidence/v08/drafts'));
  fs.mkdirSync(output,{recursive:true});
  const browser=await chromium.launch({channel:'msedge',headless:true});
  const context=await browser.newContext({baseURL:target.origin,viewport:{width:1440,height:1000}});
  const page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));
  let originalPilot=null,pilotChanged=false;
  async function api(route,payload){const proof=await page.evaluate(()=>sessionStorage.getItem('laoyou.csrf'));const headers={'Origin':target.origin,'X-App-Token':proof||''};const response=payload===undefined?await page.request.get(route,{headers}):await page.request.post(route,{headers,data:payload});assert.ok(response.ok(),route+' returned '+response.status());return response.json();}
  async function idle(){await page.waitForFunction(()=>!document.getElementById('response-save').disabled);}
  async function confirmClick(selector,accept=false){const dialog=page.waitForEvent('dialog');const click=page.locator(selector).click();const shown=await dialog;check('覆盖操作显示确认：'+selector,shown.type()==='confirm');await (accept?shown.accept():shown.dismiss());await click;}
  async function filter(){await page.locator('#response-history-search').click();await idle();}
  try{
    await page.goto('/');await page.locator('#auth-username').fill(process.env.TEST_USERNAME);await page.locator('#auth-password').fill(process.env.TEST_PASSWORD);await page.locator('#auth-submit').click();await page.waitForSelector('#monitor-points .monitor-point');await page.waitForFunction(()=>!document.getElementById('monitor-config-save').disabled);
    const bootstrap=await api('/api/bootstrap');check('隔离管理员登录',bootstrap.user.role==='admin');check('初次载入没有伪草稿标记',await page.evaluate(()=>['pilot','dataset-json','monitor-config','monitor-source','monitor-correction','response','response-review'].every(key=>!DraftGuard.dirty(key))));
    await page.locator('[data-panel="pilot-panel"]').click();const savedName=await page.locator('#pilot-site-name').inputValue();await page.locator('#pilot-site-name').fill('隔离验收未保存建档');await page.locator('[data-panel="monitor-panel"]').click();await page.locator('[data-panel="pilot-panel"]').click();check('切换页面保留建档编辑',await page.locator('#pilot-site-name').inputValue()==='隔离验收未保存建档');
    await confirmClick('#reset-demo');check('取消覆盖保留建档草稿',await page.locator('#pilot-site-name').inputValue()==='隔离验收未保存建档');
    await page.route('**/api/pilot',route=>route.request().postDataJSON()?.action==='save'?route.fulfill({status:409,contentType:'application/json',body:JSON.stringify({error:'隔离验收版本冲突'})}):route.continue());await page.locator('#save-pilot').click();await page.waitForFunction(()=>document.getElementById('message').textContent.includes('隔离验收版本冲突'));check('建档保存失败仍保留草稿',await page.evaluate(()=>DraftGuard.dirty('pilot'))&&await page.locator('#pilot-site-name').inputValue()==='隔离验收未保存建档');await page.unroute('**/api/pilot');await confirmClick('#reset-demo',true);check('确认恢复后建档返回已保存内容',await page.locator('#pilot-site-name').inputValue()===savedName);
    await page.locator('[data-panel="site-panel"]').click();await page.locator('#dataset-editor').evaluate(el=>el.closest('details').open=true);const json=await page.locator('#dataset-editor').inputValue();await page.locator('#dataset-editor').fill(json+'\n ');await page.locator('[data-panel="monitor-panel"]').click();await page.locator('[data-panel="site-panel"]').click();check('切换页面不覆盖未应用JSON',await page.locator('#dataset-editor').inputValue()===json+'\n ');await confirmClick('#reset-demo',true);
    await page.locator('[data-panel="monitor-panel"]').click();await page.locator('#monitor-config-details').evaluate(el=>el.open=true);const poll=await page.locator('#monitor-poll-seconds').inputValue();await page.locator('#monitor-poll-seconds').fill(poll==='61'?'62':'61');await page.locator('#monitor-poll-seconds').press('Tab');await confirmClick('#monitor-config-reload');check('取消监测配置重载保留草稿',await page.evaluate(()=>DraftGuard.dirty('monitor-config')));await confirmClick('#monitor-config-reload',true);await page.waitForFunction(()=>!document.getElementById('monitor-config-save').disabled);check('确认监测重载恢复保存值',await page.locator('#monitor-poll-seconds').inputValue()===poll);
    await page.locator('#response-nav').click();await idle();
    const marker='隔离草稿验收_'+randomBytes(4).toString('hex');const point=bootstrap.demo.points[0].id;let revision=(await api('/api/response-plans')).revision;let latest=null;
    for(let n=0;n<35;n++){const result=await api('/api/response-plans/save',{point_ids:[point],details:{title:marker+'_'+String(n).padStart(2,'0')},expected_revision:revision});latest=result.document;revision=result.revision;}
    await page.locator('#response-refresh').click();await idle();await page.locator('#response-history-q').fill(marker);await page.locator('#response-history-point').selectOption(point);await page.locator('#response-history-status').selectOption('draft');await filter();check('标题点位状态共同筛选且首批30条',await page.locator('#response-history [data-version]').count()===30);check('有更早版本入口',await page.locator('#response-history-more').isVisible());await page.locator('#response-history-more').click();await idle();check('加载更早版本完整显示35条',await page.locator('#response-history [data-version]').count()===35&&await page.locator('#response-history-more').isHidden());
    await page.locator('#response-history-q').fill(marker+'_不存在');await filter();check('无匹配筛选明确为空',await page.locator('#response-history [data-version]').count()===0&&(await page.locator('#response-history').textContent()).includes('没有符合'));await page.locator('#response-history-q').fill(marker);await filter();await page.locator('[data-version="'+latest.id+'"]').click();await idle();
    await page.locator('#response-title').fill('尚未保存的隔离草案编辑');await page.locator('#response-review-details').evaluate(el=>el.open=true);await page.locator('#response-review-note').fill('尚未提交的隔离复核意见');
    originalPilot=await api('/api/pilot',{action:'load'});const changed=structuredClone(originalPilot.data);changed.site.note='隔离验收：服务端资料已变化';await api('/api/pilot',{action:'save',revision:originalPilot.revision,data:changed});pilotChanged=true;
    await page.locator('#response-refresh').click();await idle();check('刷新时效保留预案编辑与复核意见',await page.locator('#response-title').inputValue()==='尚未保存的隔离草案编辑'&&await page.locator('#response-review-note').inputValue()==='尚未提交的隔离复核意见');check('服务端资料变化出现在更新提示',(await page.locator('#response-document').textContent()).includes('场地、点位或预案资料已变更'));
    await page.route('**/api/response-plans/'+latest.id,route=>route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({error:'隔离验收读取失败'})}));await page.locator('[data-panel="monitor-panel"]').click();await page.locator('#response-nav').click();await idle();check('重新进入页面核验失败明确提示',(await page.locator('#response-freshness').textContent()).includes('时效未能重新核验'));check('核验失败不清除复核意见',await page.locator('#response-review-note').inputValue()==='尚未提交的隔离复核意见');await page.unroute('**/api/response-plans/'+latest.id);
    await confirmClick('[data-version="'+latest.id+'"]');check('取消读取版本保留预案草稿',await page.locator('#response-title').inputValue()==='尚未保存的隔离草案编辑');await confirmClick('#account-logout');check('取消退出继续保留已登录草稿',await page.locator('.workspace').isVisible()&&await page.evaluate(()=>DraftGuard.dirty('response')));
    check('未保存内容不写浏览器存储',await page.evaluate(()=>localStorage.length===0&&Object.keys(sessionStorage).every(key=>key==='laoyou.csrf')));
    const dialogWait=page.waitForEvent('dialog');const reload=page.reload().catch(()=>null);const dialog=await dialogWait;check('刷新页面提示未保存资料',dialog.type()==='beforeunload');await dialog.dismiss();await reload;check('取消刷新保留编辑',await page.locator('#response-title').inputValue()==='尚未保存的隔离草案编辑');
    await page.locator('#response-review-note').fill('');
    await page.route('**/api/response-plans?*',route=>route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({error:'隔离验收列表失败'})}));
    let releaseSave;const saveHeld=new Promise(resolve=>{releaseSave=resolve;});let requestSeen;const saveSeen=new Promise(resolve=>{requestSeen=resolve;});await page.route('**/api/response-plans/save',async route=>{requestSeen();await saveHeld;await route.continue();});
    const saving=page.locator('#response-save').click();await saveSeen;check('保存期间锁定预案输入',await page.locator('#response-title').isDisabled()&&await page.locator('#response-review-note').isDisabled());releaseSave();await saving;await idle();check('保存成功后列表失败明确提示已保存',(await page.locator('#response-feedback').textContent()).includes('已保存，但版本列表刷新失败'));check('列表失败不把已保存内容恢复成未保存',!await page.evaluate(()=>DraftGuard.dirty('response'))&&await page.locator('#response-export-html').isEnabled());await page.unroute('**/api/response-plans/save');
    await page.locator('#response-review-note').fill('隔离复核成功与列表刷新失败验收');await page.locator('#response-review-save').click();await idle();check('复核成功后列表失败明确提示已保存',(await page.locator('#response-feedback').textContent()).includes('内部复核版本')&&(await page.locator('#response-feedback').textContent()).includes('已保存，但版本列表刷新失败'));check('已保存复核意见清除草稿标记',!await page.evaluate(()=>DraftGuard.dirty('response-review')));await page.unroute('**/api/response-plans?*');
    await page.screenshot({path:path.join(output,'drafts-desktop.png'),fullPage:true});await page.setViewportSize({width:390,height:844});check('移动端无横向溢出',await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));await page.screenshot({path:path.join(output,'drafts-mobile.png'),fullPage:true});check('浏览器无脚本异常',errors.length===0);
  }finally{
    if(pilotChanged&&originalPilot){const current=await api('/api/pilot',{action:'load'});await api('/api/pilot',{action:'save',revision:current.revision,data:originalPilot.data});}
    fs.writeFileSync(path.join(output,'checks.json'),JSON.stringify({checks,errors},null,2));await context.close();await browser.close();
  }
  console.log(JSON.stringify({passed:checks.length,checks,errors},null,2));
})().catch(error=>{console.error(error);process.exitCode=1;});
