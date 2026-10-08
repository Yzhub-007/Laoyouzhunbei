// Deterministic UI checks: isolated static server and mocked APIs, no real accounts.
const http=require('node:http'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root=path.resolve(__dirname,'..'),web=path.join(root,'web');
const dataset=JSON.parse(fs.readFileSync(path.join(root,'data/zju_pilot.json'),'utf8'));
const checks=[],errors=[];
const check=(name,condition)=>{assert.ok(condition,name);checks.push(name);};
const server=http.createServer((request,response)=>{
  const name=new URL(request.url,'http://localhost').pathname;
  const target=path.join(web,name==='/'?'index.html':name.slice(1));
  if(!target.startsWith(web+path.sep)){response.writeHead(404);return response.end();}
  fs.readFile(target,(error,body)=>{if(error){response.writeHead(404);return response.end();}response.setHeader('Content-Type',target.endsWith('.js')?'text/javascript':target.endsWith('.css')?'text/css':'text/html; charset=utf-8');response.end(body);});
});
async function liveCheck(){
  const baseURL=process.env.SECURITY_LIVE_URL;
  assert.ok(process.env.TEST_USERNAME&&process.env.TEST_PASSWORD,'Set credentials for an isolated live test account.');
  const output=process.env.EVIDENCE_DIR||path.join(root,'evidence/security/auth');fs.mkdirSync(output,{recursive:true});
  const browser=await chromium.launch({headless:true,channel:'msedge'});
  try{
    const page=await browser.newPage({baseURL,viewport:{width:1440,height:1000}});
    page.on('pageerror',error=>errors.push(error.message));
    await page.goto('/');await page.waitForSelector('#auth-form:not([hidden])');
    check('真实服务未登录工作台隐藏',await page.locator('.workspace').isHidden());
    check('真实服务拒绝未授权读取资料',(await page.request.get('/api/bootstrap')).status()===401);
    await page.screenshot({path:path.join(output,'01_账户登录_桌面.png'),fullPage:true});
    await page.locator('#auth-username').fill(process.env.TEST_USERNAME);await page.locator('#auth-password').fill(process.env.TEST_PASSWORD);await page.locator('#auth-submit').click();
    await page.waitForSelector('#monitor-points .monitor-point');
    check('真实账户可登录并进入自动监测',await page.locator('#account-name').textContent().then(v=>v.includes(process.env.TEST_USERNAME)));
    check('真实会话cookie为HttpOnly与SameSiteStrict',(await page.context().cookies()).some(cookie=>cookie.httpOnly&&cookie.sameSite==='Strict'));
    check('真实管理员可读取监测配置',await page.locator('#monitor-open-config').isVisible());
    await page.screenshot({path:path.join(output,'02_安全工作台_桌面.png'),fullPage:true});
    await page.locator('#account-security').click();await page.waitForSelector('.security-user-row');
    check('真实管理员可读取账户列表',await page.locator('#security-users-list').textContent().then(v=>v.includes(process.env.TEST_USERNAME)));
    await page.setViewportSize({width:390,height:844});
    check('真实账户安全页手机无横向溢出',await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await page.screenshot({path:path.join(output,'03_账户安全_手机.png'),fullPage:true});
    check('真实登录仅在同源sessionStorage保存CSRF',await page.evaluate(()=>localStorage.length===0&&sessionStorage.length===1&&Boolean(sessionStorage.getItem('laoyou.csrf'))));
    await page.locator('#account-logout').click();await page.waitForSelector('#auth-form:not([hidden])');
    check('真实退出后服务端撤销会话',(await page.request.get('/api/bootstrap')).status()===401);
    check('真实登录全链无浏览器脚本异常',errors.length===0);
    const report={passed:checks.length,checks,errors,mode:'isolated real authentication service'};
    fs.writeFileSync(path.join(output,'browser-checks.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report,null,2));
  }finally{await browser.close();}
}
if(process.env.SECURITY_LIVE_URL)liveCheck().catch(error=>{console.error(error);process.exitCode=1;});
else (async()=>{
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const browser=await chromium.launch({headless:true,channel:'msedge'});
  try{
    const page=await browser.newPage({baseURL:`http://127.0.0.1:${server.address().port}`,viewport:{width:1440,height:1000}});
    page.on('pageerror',error=>errors.push(error.message));
    const requests=[];let authenticated=false,setup=false,role='viewer',badPassword=false,failMonitor=false,forbidRefresh=false;
    const json=(route,value,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(value)});
    const now=new Date().toISOString();
    const snapshot={served_at:now,service:{running:true,last_poll_at:now,poll_seconds:60,vision_configured:false},weather:{state:'unknown',label:'天气状态未知',basis:[],providers:[]},points:[{point_id:'ZJG-P01',point_name:'SENSITIVE_POINT_UNIQUE',state:'unknown',label:'状态未知',basis:['无有效观测'],sources:[]}],sources:[],tasks:[],events:[],summary:{total_points:1,unknown_points:1},config_revision:0};
    const config={poll_seconds:60,depth_watch_cm:2,depth_alert_cm:15,camera_confidence:0.8,confirm_samples:2,sources:[]};
    const user=()=>({username:role+'_account',role});
    await page.route('**/api/**',route=>{
      const request=route.request(),url=new URL(request.url()),body=request.postData()?request.postDataJSON():null;
      requests.push({path:url.pathname,method:request.method(),body,token:request.headers()['x-app-token']});
      if(url.pathname==='/api/auth/status'){
        const matched=authenticated&&request.headers()['x-app-token']==='session-csrf';
        return json(route,{setup_required:setup,authenticated:matched,user:matched?user():null,...(matched?{csrf_token:'session-csrf'}:{})});
      }
      if(url.pathname==='/api/auth/login'){
        if(badPassword)return json(route,{error:'用户名或密码不正确。'},401);
        authenticated=true;return json(route,{user:user(),csrf_token:'session-csrf'});
      }
      if(url.pathname==='/api/auth/setup'){authenticated=true;setup=false;role='admin';return json(route,{user:user(),csrf_token:'session-csrf'});}
      if(!authenticated)return json(route,{error:'请先登录。'},401);
      if(request.headers()['x-app-token']!=='session-csrf')return json(route,{error:'缺少同源会话校验。'},401);
      if(url.pathname==='/api/auth/logout'||url.pathname==='/api/auth/password'){authenticated=false;return json(route,{ok:true});}
      if(url.pathname==='/api/auth/users')return json(route,request.method()==='GET'?{users:[user(),{username:'reader',role:'viewer',disabled:false}]}:{ok:true});
      if(url.pathname==='/api/bootstrap')return json(route,{user:user(),token:'session-csrf',permissions:{can_operate:role!=='viewer',can_configure:role==='admin',can_manage_users:role==='admin'},demo:dataset,pilot_template:dataset,pilot_revision:0,ai_configured:true});
      if(url.pathname==='/api/monitor'){
        if(failMonitor){failMonitor=false;authenticated=false;return json(route,{error:'会话已过期。'},401);}
        return json(route,snapshot);
      }
      if(url.pathname==='/api/monitor/config')return json(route,{revision:0,config});
      if(url.pathname==='/api/monitor/refresh')return forbidRefresh?json(route,{error:'无权限执行采集。'},403):json(route,{queued:true});
      if(url.pathname==='/api/live')return json(route,{providers:{model:{status:'unavailable',usable:false},station:{status:'unavailable',usable:false}},served_at:now,poll_seconds:600});
      if(url.pathname==='/api/agent/inspect')return json(route,{readiness:{passed:0,total:1,checks:[],shadow_ready:false},outbound_preview:{},input_fingerprint:'fixture-fingerprint'});
      if(url.pathname==='/api/trials')return json(route,{trials:[{id:'read-only-trial',status:'active',condition:'rules',scenario_id:'历史情景',duration_seconds:null}]});
      if(url.pathname==='/api/runs')return json(route,{runs:[{site:'脱敏历史',created_at:now,mode:'rules',count:1,run_id:'fixture-run'}]});
      if(url.pathname==='/api/run')return json(route,{run_id:'fixture-run',label:'只读历史',elapsed_ms:1,mode:'rules',weather:dataset.weather,site:{name:'脱敏历史'},warnings:[],cards:[{card_id:'task-1',point_name:'历史点位',point_id:'P1',plan_id:'R1',title:'核对',action:'查看结果',role:'管理人员',workflow_state:'confirmed',field_status:'unknown',status:'已确认'}],notices:[{id:'notice-1',text:'只读演练提示',created_at:now,source:'公开来源',feedback:[{at:now,status:'received'}]}]});
      return json(route,{ok:true});
    });
    const waitWorkspace=()=>page.waitForSelector('#monitor-points .monitor-point');
    await page.goto('/');await page.waitForSelector('#auth-form:not([hidden])');
    check('未登录只请求身份状态，不加载资料',requests.every(r=>r.path==='/api/auth/status'));
    check('未登录工作台完全隐藏',await page.locator('.workspace').isHidden());
    badPassword=true;await page.locator('#auth-username').fill('viewer_account');await page.locator('#auth-password').fill('wrong-password-value');await page.locator('#auth-submit').click();
    await page.waitForFunction(()=>document.querySelector('#auth-feedback').textContent.includes('不正确'));
    check('错误密码显示提示且不会泄露工作台',await page.locator('.workspace').isHidden() && await page.locator('#auth-password').inputValue()==='');
    badPassword=false;await page.locator('#auth-password').fill('example long passphrase');await page.locator('#auth-submit').click();await waitWorkspace();
    check('登录后展示角色与监测',await page.locator('#account-name').textContent().then(v=>v.includes('只读用户')) && await page.locator('.workspace').isVisible());
    check('viewer不请求管理员配置接口',!requests.some(r=>r.path==='/api/monitor/config'));
    check('viewer不显示采集、来源配置及现场校正',await page.locator('#monitor-refresh').isHidden() && await page.locator('#monitor-open-config').isHidden() && await page.locator('#monitor-points .button').count()===0);
    await page.locator('[data-panel=pilot-panel]').click();
    check('viewer建档字段只读且不能保存',await page.locator('#pilot-site-name').isDisabled() && await page.locator('#save-pilot').isHidden());
    await page.locator('[data-panel=experiment-panel]').click();await page.waitForTimeout(1100);
    check('试验定时render不能重新开放viewer写操作',await page.locator('#trial-start').isHidden() && await page.locator('#start-trial').isDisabled());
    check('viewer试验身份缺失不显示undefined且无恢复按钮',await page.locator('#trial-records').textContent().then(v=>!v.includes('undefined'))&&await page.locator('#trial-records button').count()===0);
    await page.locator('[data-panel=history-panel]').click();await page.locator('.history-row button').click();await page.waitForSelector('.action-card');
    check('viewer可打开无input_snapshot的历史记录',await page.locator('#message').textContent().then(v=>v.includes('只读打开历史结果')));
    await page.locator('[data-panel=public-panel]').click();
    check('viewer提示与反馈缺失隐私字段不显示undefined',await page.locator('.notice-card').textContent().then(v=>!v.includes('undefined')&&v.includes('仅管理人员可见')));
    await page.evaluate(()=>{document.querySelector('#generate-rules').disabled=false;document.querySelector('#monitor-correction-save').disabled=false;});
    await page.waitForFunction(()=>document.querySelector('#generate-rules').disabled&&document.querySelector('#monitor-correction-save').disabled);
    check('异步重绘后仍保持角色权限',true);
    check('浏览器storage仅含同源CSRF，不保存密码或sessionID',await page.evaluate(()=>localStorage.length===0&&sessionStorage.length===1&&sessionStorage.getItem('laoyou.csrf')==='session-csrf'));
    await page.locator('#account-logout').click();await page.waitForSelector('#auth-form:not([hidden])');
    check('退出发送当前会话CSRF并返回登录',requests.some(r=>r.path==='/api/auth/logout'&&r.token==='session-csrf') && await page.locator('.workspace').isHidden());
    check('退出立即清除同源CSRF',await page.evaluate(()=>sessionStorage.length===0));

    authenticated=true;role='operator';await page.evaluate(()=>sessionStorage.setItem('laoyou.csrf','session-csrf'));await page.reload();await waitWorkspace();
    check('operator可校正与采集但不能配置来源',await page.locator('#monitor-refresh').isVisible() && await page.locator('#monitor-open-config').isHidden() && await page.locator('#monitor-points .button').count()===1);
    forbidRefresh=true;await page.locator('#monitor-refresh').click();await page.waitForFunction(()=>document.querySelector('#message').textContent.includes('没有此项操作权限'));
    check('403给出无权限提示且保留登录',await page.locator('.workspace').isVisible());forbidRefresh=false;
    failMonitor=true;await page.evaluate(()=>refreshMonitor());await page.waitForSelector('#auth-form:not([hidden])');
    check('401立即返回登录并清除旧监测页面',await page.locator('.workspace').isHidden() && !(await page.locator('body').textContent()).includes('SENSITIVE_POINT_UNIQUE'));

    authenticated=true;role='admin';await page.evaluate(()=>sessionStorage.setItem('laoyou.csrf','session-csrf'));await page.reload();await waitWorkspace();await page.waitForFunction(()=>!document.querySelector('#monitor-config-save').disabled);
    check('admin可以管理来源且RTSP不再提供',await page.locator('#monitor-open-config').isVisible() && await page.locator('#monitor-source-mode option[value=rtsp]').count()===0);
    await page.locator('#account-security').click();await page.waitForSelector('.security-user-row');
    check('管理员可读取用户管理',await page.locator('#security-users').isVisible());
    await page.locator('#security-user-name').fill('new_reader');await page.locator('#security-user-password').fill('reader long secret passphrase');await page.locator('#security-user-role').selectOption('viewer');await page.locator('#security-user-save').click();
    await page.waitForFunction(()=>document.querySelector('#security-feedback').textContent.includes('已保存'));
    check('账户管理发送角色及CSRF并清空密码',requests.some(r=>r.path==='/api/auth/users'&&r.body?.username==='new_reader'&&r.body.role==='viewer'&&r.token==='session-csrf') && await page.locator('#security-user-password').inputValue()==='');
    await page.locator('#security-password-current').fill('old long secret passphrase');await page.locator('#security-password-new').fill('new long secret passphrase');await page.locator('#security-password-confirm').fill('new long secret passphrase');await page.locator('#security-password-save').click();await page.waitForSelector('#auth-form:not([hidden])');
    check('修改密码后强制重新登录',requests.some(r=>r.path==='/api/auth/password'&&r.token==='session-csrf') && await page.locator('.workspace').isHidden());

    setup=true;await page.reload();await page.waitForSelector('#auth-form:not([hidden])');
    check('首次设置提示本机20分钟设置码',await page.locator('#auth-setup-fields').isVisible() && await page.locator('#auth-setup-fields').textContent().then(v=>v.includes('20分钟')&&v.includes('runtime/setup-code.txt')));
    await page.locator('#auth-username').fill('first_admin');await page.locator('#auth-setup-code').fill('synthetic-setup-code');await page.locator('#auth-password').fill('short');await page.locator('#auth-submit').click();
    check('新密码至少15字符，短密码不发送',await page.locator('#auth-password').getAttribute('minlength')==='15'&&!requests.some(r=>r.path==='/api/auth/setup'));
    await page.locator('#auth-password').fill('a long initial passphrase');await page.locator('#auth-submit').click();await waitWorkspace();
    check('首次设置发送用户提供的本机码',requests.some(r=>r.path==='/api/auth/setup'&&r.body.setup_code==='synthetic-setup-code'));
    await page.setViewportSize({width:390,height:844});
    check('手机账户顶栏无横向溢出',await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    check('安全设置说明真实ROI和人工遮挡',await page.locator('#monitor-config-details').textContent().then(v=>v.includes('roi:')&&v.includes('不是自动人脸')&&v.includes('禁止公网视觉')));
    check('保护接口GET与POST均发送同源CSRF',requests.filter(r=>r.path.startsWith('/api/')&&!r.path.startsWith('/api/auth/')).every(r=>r.token==='session-csrf'));
    check('浏览器无脚本异常',errors.length===0);
    const report={passed:checks.length,checks,errors,mode:'isolated mock API; synthetic identity and token only'};
    const evidence=path.join(root,'evidence/security');fs.mkdirSync(evidence,{recursive:true});
    fs.writeFileSync(path.join(evidence,'mock-auth-checks.json'),JSON.stringify(report,null,2));
    console.log(JSON.stringify(report,null,2));
  }finally{await browser.close();await new Promise(resolve=>server.close(resolve));}
})().catch(error=>{console.error(error);server.close();process.exitCode=1;});
