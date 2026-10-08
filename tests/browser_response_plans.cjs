// Real UI + authenticated API acceptance. Run only against an isolated local DB.
// Required: RESPONSE_PLANS_LIVE_URL, TEST_USERNAME, TEST_PASSWORD.
// The production port 8765 is deliberately refused. No runtime files are read.
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {randomBytes} = require('node:crypto');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

const root = path.resolve(__dirname, '..');
const checks = [], errors = [], cleanupErrors = [];
const check = (name, condition) => { assert.ok(condition, name); checks.push(name); };

async function run() {
  assert.ok(process.env.RESPONSE_PLANS_LIVE_URL, 'Set RESPONSE_PLANS_LIVE_URL to an isolated service.');
  assert.ok(process.env.TEST_USERNAME && process.env.TEST_PASSWORD, 'Set isolated administrator credentials.');
  const target = new URL(process.env.RESPONSE_PLANS_LIVE_URL);
  assert.ok(target.protocol === 'http:' && ['127.0.0.1', 'localhost'].includes(target.hostname), 'Only local test servers are allowed.');
  assert.ok(target.port && target.port !== '8765', 'Refusing the production port 8765 or an implicit port.');
  assert.ok(!target.username && !target.password && !target.search && !target.hash && target.pathname === '/', 'Use an origin URL without credentials or parameters.');
  const baseURL = target.origin;
  const output = path.resolve(process.env.EVIDENCE_DIR || path.join(root, 'evidence/v07/response-plans'));
  fs.mkdirSync(output, {recursive: true});
  const browser = await chromium.launch({headless: true, channel: 'msedge'});
  const contexts = [], createdUsers = [];
  let adminPage, success = false;

  async function newPage(role) {
    const context = await browser.newContext({baseURL, viewport: {width: 1440, height: 1000}, acceptDownloads: true});
    contexts.push(context);
    const page = await context.newPage();
    page.on('pageerror', error => errors.push({role, message: error.message}));
    return page;
  }

  async function login(page, username, password) {
    await page.goto('/');
    await page.waitForSelector('#auth-form:not([hidden])');
    assert.ok(await page.locator('#auth-setup-fields').isHidden(), 'The isolated service must already have a test administrator.');
    await page.locator('#auth-username').fill(username);
    await page.locator('#auth-password').fill(password);
    await page.locator('#auth-submit').click();
    await page.waitForSelector('#monitor-points .monitor-point');
  }

  async function api(page, route, payload) {
    const proof = await page.evaluate(() => sessionStorage.getItem('laoyou.csrf'));
    const headers = {'Origin': baseURL, 'X-App-Token': proof || ''};
    return payload === undefined ? page.request.get(route, {headers}) : page.request.post(route, {headers, data: payload});
  }

  async function read(page, route) {
    const response = await api(page, route);
    assert.ok(response.ok(), `Read ${route} failed with ${response.status()}.`);
    return response.json();
  }

  async function posted(page, route, action) {
    const [response] = await Promise.all([
      page.waitForResponse(response => new URL(response.url()).pathname === route && response.request().method() === 'POST'),
      action(),
    ]);
    assert.ok(response.ok(), `${route} failed with ${response.status()}.`);
    return response.json();
  }

  async function openPlans(page) {
    await page.locator('#response-nav').click();
    await page.waitForSelector('#response-points input');
    await page.waitForFunction(() => !document.querySelector('#response-save').disabled);
  }

  async function chooseOnlyFirst(page) {
    const boxes = page.locator('#response-points input');
    const firstId = await boxes.first().inputValue();
    for (const box of await boxes.all()) await box.uncheck();
    await boxes.first().check();
    return firstId;
  }

  async function download(page, format) {
    const [file] = await Promise.all([page.waitForEvent('download'), page.locator('#response-export-' + format).click()]);
    assert.ok(file.suggestedFilename().endsWith('.' + format), 'Download has the requested extension.');
    const destination = path.join(output, 'response-plan-export.' + format);
    await file.saveAs(destination);
    return fs.readFileSync(destination, 'utf8');
  }

  try {
    adminPage = await newPage('admin');
    check('隔离服务未登录不能读取预案', (await adminPage.request.get('/api/response-plans')).status() === 401);
    await login(adminPage, process.env.TEST_USERNAME, process.env.TEST_PASSWORD);
    const bootstrap = await read(adminPage, '/api/bootstrap');
    check('通过真实管理员登录进行验收', bootstrap.user.role === 'admin');
    const baseline = await read(adminPage, '/api/response-plans');
    await openPlans(adminPage);
    check('预案编制读取已保存点位', await adminPage.locator('#response-points input').count() > 0);
    check('未生成预览时禁止导出', await adminPage.locator('#response-export-html').isDisabled() && await adminPage.locator('#response-export-json').isDisabled());

    for (const box of await adminPage.locator('#response-points input').all()) await box.uncheck();
    let emptySelectionPosts = 0;
    const countPreview = request => { if (new URL(request.url()).pathname === '/api/response-plans/preview') emptySelectionPosts += 1; };
    adminPage.on('request', countPreview);
    await adminPage.locator('#response-form button[type=submit]').click();
    await adminPage.waitForFunction(() => document.querySelector('#response-feedback').textContent.includes('至少选择'));
    adminPage.off('request', countPreview);
    check('未选点位时本地阻止预览并显示原因', emptySelectionPosts === 0);

    const pointId = await chooseOnlyFirst(adminPage);
    const injection = '<img src=x onerror="window.__planInjected=1"><script>window.__planInjected=1</script>隔离预案';
    await adminPage.locator('#response-title').fill(injection);
    const preview = await posted(adminPage, '/api/response-plans/preview', () => adminPage.locator('#response-form button[type=submit]').click());
    await adminPage.waitForSelector('#response-preview:not([hidden])');
    check('预览只包含所选择点位', JSON.stringify(preview.document.scope.point_ids) === JSON.stringify([pointId]) && preview.document.observations.points.length === 1);
    check('预览尚无持久化版本编号', preview.document.id === null && preview.document.revision === null);
    check('未接入点位监测仍明确标记未知', preview.document.observations.points[0].state === 'unknown' && (await adminPage.locator('#response-document').textContent()).includes('现状未知'));
    check('缺少联络及疏散资料列入待补项', preview.document.missing_items.some(item => item.includes('联系方式')) && preview.document.missing_items.some(item => item.includes('疏散路线')));
    check('天气参考只能生成待人工核验任务，不能授权执行', preview.document.publishable === false && preview.document.tasks.every(task => task.status === 'pending_review'));
    check('预览不增加已保存版本数量', (await read(adminPage, '/api/response-plans')).revision === baseline.revision);
    check('恶意标题按原文字显示', await adminPage.locator('#response-document-title').textContent() === injection);
    check('恶意标题未生成图片或脚本节点', await adminPage.locator('#response-preview img,#response-preview script').count() === 0 && await adminPage.evaluate(() => window.__planInjected === undefined));

    const first = (await posted(adminPage, '/api/response-plans/save', () => adminPage.locator('#response-save').click())).document;
    await adminPage.waitForSelector(`#response-history [data-version="${first.id}"]`);
    check('首次保存生成独立 V1 相对版本', first.revision === baseline.revision + 1 && Boolean(first.id));
    check('保存后的导出按钮可用', await adminPage.locator('#response-export-html').isEnabled() && await adminPage.locator('#response-export-json').isEnabled());
    check('历史标题也只呈现文本', await adminPage.locator('#response-history img,#response-history script').count() === 0);

    const titleV2 = injection + ' V2';
    await adminPage.locator('#response-title').fill(titleV2);
    await adminPage.locator('#response-resources').fill('仅用于隔离浏览器验收：物资资料尚待管理方核验。');
    const second = (await posted(adminPage, '/api/response-plans/save', () => adminPage.locator('#response-save').click())).document;
    await adminPage.waitForSelector(`#response-history [data-version="${second.id}"]`);
    const firstLoaded = (await read(adminPage, '/api/response-plans/' + first.id)).document;
    check('修改保存另建 V2，不复用原版本编号', second.revision === first.revision + 1 && second.id !== first.id);
    check('V1 标题、资料和观测保持原样', firstLoaded.title === injection && firstLoaded.details.resources === '' && JSON.stringify(firstLoaded.observations) === JSON.stringify(first.observations));

    const html = await download(adminPage, 'html');
    check('可下载独立可打印 HTML 草案', html.includes('<!doctype html>') && html.includes('不是正式发布文件'));
    check('HTML 导出转义恶意标签并限制外部资源', html.includes('&lt;img') && !/<(?:script|img)\b/i.test(html) && html.includes("default-src 'none'"));
    const exported = JSON.parse(await download(adminPage, 'json'));
    check('JSON 导出保持所选版本与完整依据', exported.id === second.id && exported.revision === second.revision && exported.title === titleV2 && exported.scope.point_ids[0] === pointId);
    check('导出持续标记草案且未获得执行授权', exported.publishable === false && exported.status === 'draft' && exported.missing_items.length > 0);
    await adminPage.screenshot({path: path.join(output, '01_预案预览与版本_桌面.png'), fullPage: true});

    await adminPage.locator('#response-review-details > summary').click();
    await adminPage.locator('#response-review-note').fill('隔离验收复核：只核对编制和留痕流程，现场联络、资源和疏散资料仍待核验。');
    const reviewed = (await posted(adminPage, '/api/response-plans/review', () => adminPage.locator('#response-review-save').click())).document;
    await adminPage.waitForSelector(`#response-history [data-version="${reviewed.id}"]`);
    check('管理员内部复核生成新的 V3 版本', reviewed.revision === second.revision + 1 && reviewed.parent_id === second.id && reviewed.id !== second.id);
    check('复核记录绑定实际管理员且仍为草案', reviewed.review.by === process.env.TEST_USERNAME && reviewed.status === 'reviewed_draft' && reviewed.publishable === false);
    check('内部复核不修改原 V2', (await read(adminPage, '/api/response-plans/' + second.id)).document.review === null);
    check('页面显示内部复核及未正式发布说明', (await adminPage.locator('#response-document').textContent()).includes('内部复核不等于正式发布'));

    await adminPage.locator(`#response-history [data-version="${first.id}"]`).click();
    await adminPage.waitForFunction(revision => document.querySelector('#response-document-meta').textContent.includes('版本 V' + revision), first.revision);
    check('历史版本可回读并恢复原补充资料', await adminPage.locator('#response-resources').inputValue() === '' && await adminPage.locator('#response-title').inputValue() === injection);
    await adminPage.setViewportSize({width: 390, height: 844});
    await adminPage.screenshot({path: path.join(output, '02_预案草案_390手机.png'), fullPage: true});
    check('390 像素手机预案页没有横向溢出', await adminPage.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    check('手机仍可使用版本和导出控件', await adminPage.locator('#response-history').isVisible() && await adminPage.locator('#response-export-json').isEnabled());

    for (const role of ['operator', 'viewer']) {
      const username = 'plan_' + role + '_' + randomBytes(5).toString('hex');
      const response = await api(adminPage, '/api/auth/users', {username, role, password: process.env.TEST_PASSWORD});
      assert.ok(response.ok(), 'Could not create an isolated role-test user.');
      createdUsers.push({username, role});
      const page = await newPage(role);
      await login(page, username, process.env.TEST_PASSWORD);
      if (role === 'operator') {
        await openPlans(page);
        check('操作员可读取预案与版本记录', await page.locator(`#response-history [data-version="${first.id}"]`).isVisible());
        check('操作员看不到管理员内部复核', await page.locator('#response-review-details').isHidden());
        await chooseOnlyFirst(page);
        await page.locator('#response-title').fill('仅供隔离验收的操作员预案草案');
        const saved = (await posted(page, '/api/response-plans/save', () => page.locator('#response-save').click())).document;
        check('操作员可保存草案并绑定实际身份', saved.created_by === username && saved.publishable === false);
        const refused = await api(page, '/api/response-plans/review', {id: saved.id, expected_revision: saved.revision, review_note: '越权尝试'});
        check('服务端拒绝操作员越权内部复核', refused.status() === 403);
      } else {
        check('只读用户不显示含内部资料的预案入口', await page.locator('#response-nav').isHidden() && await page.locator('#response-panel').isHidden());
        for (const route of ['/api/response-plans', '/api/response-plans/' + first.id, '/api/response-plans/' + first.id + '/export?format=json']) {
          check('只读用户无法读取预案 ' + route.replace(first.id, '<version>'), (await api(page, route)).status() === 403);
        }
        for (const operation of ['preview', 'save', 'review']) {
          check('服务端拒绝只读用户 ' + operation, (await api(page, '/api/response-plans/' + operation, {point_ids: [pointId], details: {title: '越权尝试'}})).status() === 403);
        }
        await page.setViewportSize({width: 390, height: 844});
        check('只读用户手机页没有横向溢出', await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
        await page.screenshot({path: path.join(output, '03_只读角色边界_手机.png'), fullPage: true});
      }
    }
    check('预案全流程无浏览器脚本异常', errors.length === 0);
    success = true;
  } finally {
    if (!success && adminPage && !adminPage.isClosed()) {
      await adminPage.screenshot({path: path.join(output, 'failure.png'), fullPage: true}).catch(() => {});
    }
    // These are disposable test identities, disabled after verification without deleting evidence.
    for (const account of createdUsers) {
      try {
        const response = await api(adminPage, '/api/auth/users', {...account, disabled: true});
        if (!response.ok()) cleanupErrors.push('A disposable test account could not be disabled.');
      } catch { cleanupErrors.push('A disposable test account could not be disabled.'); }
    }
    const report = {passed: checks.length, completed: success, checks, errors, cleanupErrors, mode: 'isolated real response-plan service; synthetic account and document inputs'};
    fs.writeFileSync(path.join(output, 'browser-checks.json'), JSON.stringify(report, null, 2));
    console.log(JSON.stringify(report, null, 2));
    await Promise.all(contexts.map(context => context.close()));
    await browser.close();
  }
}

run().catch(error => { console.error(error); process.exitCode = 1; });
