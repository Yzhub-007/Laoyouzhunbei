// Optional: start server.py with an isolated --db before running.
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const output=process.env.EVIDENCE_DIR || path.resolve(__dirname,'../evidence/security/monitor');fs.mkdirSync(output,{recursive:true});
(async()=>{
  const browser=await chromium.launch({headless:true,channel:'msedge'});
  try {
    const baseURL=process.env.TEST_URL || 'http://127.0.0.1:8766';
    const page=await browser.newPage({baseURL,viewport:{width:1440,height:1080}});
    assert.ok(process.env.TEST_USERNAME&&process.env.TEST_PASSWORD,'Set TEST_USERNAME and TEST_PASSWORD for an isolated test account.');
    const login=await page.request.post('/api/auth/login',{data:{username:process.env.TEST_USERNAME,password:process.env.TEST_PASSWORD},headers:{Origin:baseURL}});
    assert.ok(login.ok(),'测试账户登录成功');
    const session=await login.json();await page.addInitScript(token=>sessionStorage.setItem('laoyou.csrf',token),session.csrf_token);
    const checks=[],errors=[];page.on('pageerror',e=>errors.push(e.message));
    const check=(name,ok)=>{assert.ok(ok,name);checks.push(name);};
    const now=new Date().toISOString();
    let revision=0,config={poll_seconds:60,depth_watch_cm:2,depth_alert_cm:15,camera_confidence:0.8,confirm_samples:2,sources:[]},savedPayload=null,correctionPayload=null,outage=false,conflict=false;
    let snapshot={served_at:now,service:{running:true,last_poll_at:now,last_error:null,poll_seconds:60,vision_configured:false},weather:{state:'unknown',label:'天气状态未知',hazards:[],basis:['无有效天气来源，不判断晴天。'],providers:[]},points:[{point_id:'P01',point_name:'地下入口',state:'unknown',label:'状态未知',automatic_state:'unknown',basis:['尚未接入'],observed_at:null,sources:[]}],tasks:[],events:[],sources:[],summary:{total_points:1,unknown_points:1,suspected_points:0,alert_points:0,clear_points:0,active_corrections:0,task_count:0,connected_sources:0,fresh_sources:0},config_revision:0};
    const json=(route,value,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(value)});
    await page.route('**/api/live',r=>json(r,{error:'deterministic upstream outage'},503));
    await page.route('**/api/monitor',r=>outage?json(r,{error:'test disconnection'},503):json(r,snapshot));
    await page.route('**/api/monitor/config',r=>{
      if(r.request().method()==='POST') {
        if(conflict)return json(r,{error:'配置版本冲突'},409);
        savedPayload=r.request().postDataJSON();config=savedPayload.config;revision++;snapshot.config_revision=revision;
      }
      return json(r,{revision,config});
    });
    await page.route('**/api/monitor/refresh',r=>json(r,{queued:true}));
    await page.route('**/api/monitor/correction',r=>{correctionPayload=r.request().postDataJSON();return json(r,{ok:true});});
    await page.goto('/');await page.waitForSelector('#monitor-points .monitor-point');
    check('默认进入监测首页',await page.locator('#monitor-panel').isVisible());
    check('无现场来源显示未知',await page.locator('#monitor-points').textContent().then(t=>t.includes('状态未知')&&!t.includes('现场确认')));
    check('气象缺测不判晴',await page.locator('#monitor-weather-label').textContent()==='天气状态未知');
    await page.locator('#monitor-open-config').click();
    check('HTTP抓拍为默认接入方式',await page.locator('#monitor-source-mode').inputValue()==='image_url');
    check('提示真实画面时间及校内HTTPS限制',await page.locator('#monitor-source-help').textContent().then(t=>t.includes('X-Captured-At')&&t.includes('Last-Modified')&&t.includes('校内HTTPS')));
    await page.locator('#monitor-source-name').fill('地下入口抓拍');await page.locator('#monitor-source-location').fill('https://camera.example/snapshot.jpg');
    await page.locator('#monitor-source-add').click();
    check('未确认发送画面不添加来源',await page.locator('#monitor-config-sources .monitor-config-source').count()===0);
    await page.locator('#monitor-vision-consent').check();await page.locator('#monitor-source-add').click();
    check('添加来源后仍待保存',await page.locator('#monitor-config-sources .monitor-config-source').count()===1 && savedPayload===null);
    const draft=await page.locator('#monitor-config-json').inputValue();
    await page.locator('[data-panel=live-panel]').click();await page.locator('[data-panel=monitor-panel]').click();await page.waitForTimeout(200);
    check('重新读取快照保留配置草稿',await page.locator('#monitor-config-json').inputValue()===draft);
    await page.locator('#monitor-config-save').click();await page.waitForFunction(()=>document.querySelector('#monitor-config-feedback').textContent.includes('已保存并启用'));
    check('保存发送版本及截图配置',savedPayload.revision===0&&savedPayload.config.sources[0].input_type==='image_url'&&savedPayload.config.sources[0].vision_enabled===true&&savedPayload.config.sources[0].point_id==='P01');
    snapshot={...snapshot,weather:{state:'rain',label:'有降水迹象',basis:['城区模型检测到降水，仅作区域参考。']},points:[{point_id:'P01',point_name:'地下入口',state:'pooling_suspected',label:'疑似可见积水',automatic_state:'pooling_suspected',basis:['连续两帧识别到可见积水；无水深标定'],observed_at:now,sources:['camera-1']}],sources:[{id:'camera-1',name:'地下入口抓拍',point_id:'P01',kind:'camera',transport:'vision',status:'fresh',last_success_at:now,observed_at:now}],summary:{...snapshot.summary,unknown_points:0,suspected_points:1,connected_sources:1,fresh_sources:1,task_count:1},tasks:[{id:'task-1',point_id:'P01',point_name:'地下入口',plan_id:'plan-1',title:'复核排水',action:'请管理人员到场核验并检查排水口。',role:'楼宇管理人员',basis:['摄像头显示疑似积水'],status:'pending_review',point_status:'pending',plan_status:'draft'}],events:[{at:now,point_id:'P01',message:'地下入口：未知 → 疑似可见积水'}]};
    await page.locator('[data-panel=live-panel]').click();await page.locator('[data-panel=monitor-panel]').click();await page.waitForSelector('.monitor-task');
    check('摄像头识别只显示疑似积水',await page.locator('#monitor-points .tag').textContent()==='疑似可见积水');
    check('任务标注待核验及草稿',await page.locator('#monitor-tasks').textContent().then(t=>t.includes('待人工核验')&&t.includes('草稿 / 待核实')));
    await page.locator('#monitor-points .button').click();await page.locator('#monitor-correction-state').selectOption('clear');await page.locator('#monitor-correction-note').fill('到场观察排水口周边无积水');await page.locator('#monitor-correction-ttl').selectOption('30');await page.locator('#monitor-correction-save').click();await page.waitForFunction(()=>document.querySelector('#monitor-correction-feedback').textContent.includes('已记录'));
    check('人工校正包含点位、登录身份、依据与时效',correctionPayload.point_id==='P01'&&correctionPayload.state==='clear'&&correctionPayload.actor===process.env.TEST_USERNAME&&correctionPayload.ttl_minutes===30);
    await page.locator('#monitor-config-details .editor-row summary').click();
    const amended={...config,poll_seconds:90};await page.locator('#monitor-config-json').fill(JSON.stringify(amended,null,2));conflict=true;
    await page.locator('#monitor-config-save').click();await page.waitForFunction(()=>document.querySelector('#monitor-config-feedback').textContent.includes('版本冲突'));
    check('配置冲突保留未保存内容',JSON.parse(await page.locator('#monitor-config-json').inputValue()).poll_seconds===90);
    await page.locator('#monitor-config-details summary').first().click();await page.locator('#monitor-correction-details summary').click();
    await page.screenshot({path:path.join(output,'01_自动监测_桌面.png'),fullPage:true});
    await page.setViewportSize({width:390,height:844});
    check('390px监测首页无水平溢出',await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await page.screenshot({path:path.join(output,'02_自动监测_手机.png'),fullPage:true});
    outage=true;await page.locator('[data-panel=live-panel]').click();await page.locator('[data-panel=monitor-panel]').click();await page.waitForFunction(()=>document.querySelector('#monitor-service').textContent.includes('中断'));
    check('断线清除旧肯定状态',await page.locator('#monitor-points').textContent().then(t=>t.includes('未知')&&!t.includes('疑似可见积水')));
    check('断线移除旧自动任务',await page.locator('#monitor-tasks .monitor-task').count()===0);
    check('断线禁用人工保存',await page.locator('#monitor-correction-save').isDisabled());
    check('无浏览器脚本异常',errors.length===0);
    fs.writeFileSync(path.join(output,'browser-checks.json'),JSON.stringify({checks,errors},null,2));console.log(JSON.stringify({passed:checks.length,checks},null,2));
  }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
