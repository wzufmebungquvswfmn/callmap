// Real Chromium checks against a running preview; only task queries are mocked.
const assert = require('node:assert/strict');
const {test, before, after} = require('node:test');
const {chromium} = require(process.argv[2] || 'playwright');
const baseURL = process.env.FLUX_BROWSER_URL || 'http://127.0.0.1:18081';
let browser;
before(async () => { browser = await chromium.launch({headless: true}); });
after(async () => { await browser?.close(); });
async function auxiliary(route) {
  const path = new URL(route.request().url()).pathname;
  if (path === '/v1/tasks/summaries') { await route.fulfill({json: {tasks: {}, missing: route.request().postDataJSON().task_ids}}); return true; }
  if (path === '/v1/exports' && route.request().method() === 'GET') { await route.fulfill({json: {jobs: [], total: 0}}); return true; }
  return false;
}
async function setup(viewport) {
  const page = await browser.newPage({viewport});
  const calls = [], errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.route('**/v1/**', async (route) => {
    if (await auxiliary(route)) return;
    calls.push({url: route.request().url(), body: route.request().postDataJSON()});
    await route.fulfill({json: {task_ids: ['111', '222'], count: 2, scanned_count: 2, has_more: false, next_cursor: null, excluded_count: 0}});
  });
  await page.goto(baseURL);
  return {page, calls, errors};
}
async function add(page, field) {
  await page.locator('#add-condition').click();
  const row = page.locator('.condition-row').last();
  await row.locator('.field-select').selectOption(field);
  return page.locator(`[data-condition-id="${await row.getAttribute("data-condition-id")}"]`);
}
async function select(row, labels) {
  await row.locator('summary').click();
  for (const label of labels) await row.getByRole('checkbox', {name: label, exact: true}).check();
  await row.locator('summary').click();
}
test('desktop: fixed multiselects, manual owners, placeholder, submit and stale results', async () => {
  const {page, calls, errors} = await setup({width: 1440, height: 1100});
  try {
    const status = await add(page, 'task_status');
    await select(status, ['等待输入', '已回退']);
    const mode = await add(page, 'work_mode'); await select(mode, ['默认模式', '代码编写']);
    const owner = await add(page, 'created_by'); await owner.locator('textarea').fill('alice，bob\nalice');
    const category = await add(page, 'level_2_category'); await select(category, ['code review', 'GUI 测试']);
    await category.locator('.mode-select').selectOption('exclude');
    await add(page, 'source_platform');
    assert.equal(calls.length, 0);
    await page.locator('#query').click();
    await page.waitForFunction(() => document.querySelector('#result-count').textContent === '2');
    assert.equal(calls.length, 1);
    assert.ok(calls[0].url.endsWith('/query'));
    const filters = calls[0].body.filters;
    assert.deepEqual(filters.filter_fields.find((f) => f.field_name === 'task_status').values, ['input-required', 'rolled-back']);
    assert.deepEqual(filters.filter_fields.find((f) => f.field_name === 'work_mode').values, ['general', 'coding']);
    assert.deepEqual(filters.filter_fields.find((f) => f.field_name === 'created_by').values, ['alice', 'bob']);
    assert.deepEqual(filters.exclude_fields[0].values, ['code review', 'GUI 测试']);
    assert.equal(JSON.stringify(filters).includes('source_platform'), false);
    await page.screenshot({path: process.env.FLUX_BROWSER_SCREENSHOT || '/tmp/flux-fixed-filters-desktop.png', fullPage: true});
    await owner.getByRole('button', {name: '移除bob', exact: true}).click();
    assert.equal(await page.locator('#copy').isDisabled(), true);
    assert.deepEqual(errors, []);
  } finally { await page.close(); }
});
test('mobile: all category levels remain available, search, clear, no horizontal overflow', async () => {
  const {page, calls, errors} = await setup({width: 390, height: 844});
  try {
    for (const [field, label] of [['level_1_category', '代码工程'], ['level_2_category', '代码开发'], ['level_3_category', '需求开发']]) {
      const row = await add(page, field); await select(row, [label]);
    }
    const row = page.locator('.condition-row').last();
    await row.locator('summary').click();
    await row.locator('input[type=search]').fill('性能');
    await row.getByRole('checkbox', {name: '性能优化', exact: true}).check();
    assert.equal(await row.locator('.value-tag').count(), 2);
    await row.getByRole('button', {name: '清空', exact: true}).click();
    assert.equal(await row.locator('.value-tag').count(), 0);
    await page.screenshot({path: '/tmp/flux-fixed-filters-mobile.png', fullPage: true});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    assert.equal(calls.length, 0); assert.deepEqual(errors, []);
  } finally { await page.close(); }
});

test('pagination: first page, empty page, retry, cached navigation and export of loaded IDs', async () => {
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
  const calls = [], errors = [];
  let failed = false;
  page.on('pageerror', (error) => errors.push(error.message));
  await page.route('**/v1/**', async (route) => {
    if (await auxiliary(route)) return;
    assert.ok(route.request().url().endsWith('/query'));
    const body = route.request().postDataJSON(); calls.push(body);
    if (body.cursor === 'c2' && !failed) {
      failed = true; await route.fulfill({status: 503, json: {error: {code: 'UPSTREAM_UNAVAILABLE'}}}); return;
    }
    const ids = body.cursor === null ? ['111'] : body.cursor === 'c1' ? [] : ['111', '222'];
    await route.fulfill({json: {task_ids: ids, count: ids.length, scanned_count: 200,
      has_more: body.cursor !== 'c2', next_cursor: body.cursor === null ? 'c1' : body.cursor === 'c1' ? 'c2' : null}});
  });
  try {
    await page.goto(baseURL);
    await page.locator('#query').click();
    await page.locator('#page-info').filter({hasText: '第 1 页'}).waitFor();
    assert.equal(await page.locator('#result-badge').textContent(), '已加载部分');
    await page.locator('#export-format').selectOption('json');
    const partialEvent = page.waitForEvent('download');
    await page.locator('#download').click();
    const partial = await partialEvent;
    assert.match(partial.suggestedFilename(), /_partial_/);
    assert.deepEqual(JSON.parse(require('node:fs').readFileSync(await partial.path(), 'utf8')), ['111']);
    await page.locator('#next-page').click();
    await page.locator('#page-info').filter({hasText: '第 2 页'}).waitFor();
    assert.equal(await page.locator('#next-page').isEnabled(), true);
    assert.equal(await page.locator('#empty-state').isVisible(), true);
    await page.locator('#next-page').click();
    await page.locator('#query-error').waitFor({state: 'visible'});
    assert.match(await page.locator('#page-info').textContent(), /第 2 页/);
    await page.locator('#next-page').click();
    await page.locator('#page-info').filter({hasText: '第 3 页'}).waitFor();
    assert.equal(await page.locator('#result-count').textContent(), '2');
    assert.equal(await page.locator('#next-page').isDisabled(), true);
    await page.locator('#previous-page').click();
    await page.locator('#next-page').click();
    assert.equal(calls.length, 4);
    assert.deepEqual(calls.map((c) => c.cursor), [null, 'c1', 'c2', 'c2']);
    await page.locator('#export-format').selectOption('json');
    const downloaded = page.waitForEvent('download');
    await page.locator('#download').click();
    const download = await downloaded;
    assert.deepEqual(JSON.parse(require('node:fs').readFileSync(await download.path(), 'utf8')), ['111', '222']);
    assert.match(download.suggestedFilename(), /_all_/);
    await page.screenshot({path: '/tmp/flux-pagination-desktop.png', fullPage: true});
    await page.setViewportSize({width: 390, height: 844});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.screenshot({path: '/tmp/flux-pagination-mobile.png', fullPage: true});
    await page.locator('.query-preferences > summary').click();
    await page.locator('#page-size').selectOption('50');
    assert.equal(await page.locator('#previous-page').isDisabled(), true);
    assert.equal(await page.locator('#download').isDisabled(), true);
    assert.deepEqual(errors, []);
  } finally { await page.close(); }
});

test('data export: loaded IDs, full filter scope, progress, ZIP and server cancellation', async () => {
  const page = await browser.newPage({viewport: {width: 1100, height: 1000}});
  const bodies = [], errors = [];
  let jobNumber = 0, cancelled = false;
  page.on('pageerror', (error) => errors.push(error.message));
  await page.route('**/v1/**', async (route) => {
    if (await auxiliary(route)) return;
    const url = new URL(route.request().url());
    if (url.pathname.endsWith('/query')) {
      await route.fulfill({json: {task_ids: ['111', '222'], count: 2, scanned_count: 2, has_more: false, next_cursor: null}}); return;
    }
    if (url.pathname === '/v1/exports') {
      bodies.push(route.request().postDataJSON()); jobNumber += 1;
      await route.fulfill({status: 202, json: {job_id: `job${jobNumber}`, status: 'queued', task_count: 2, completed_count: 0, tasks: {}, archive_ready: false}}); return;
    }
    if (url.pathname.endsWith('/cancel')) {
      cancelled = true; await route.fulfill({json: {status: 'cancelled'}}); return;
    }
    if (url.pathname.endsWith('/download')) {
      await route.fulfill({contentType: 'application/zip', body: Buffer.from('PK-test')}); return;
    }
    await route.fulfill({json: {
      job_id: `job${jobNumber}`, status: jobNumber === 1 ? 'partial' : cancelled ? 'cancelled' : 'running',
      task_count: 2, completed_count: jobNumber === 1 ? 2 : 0, archive_ready: jobNumber === 1 || cancelled,
      tasks: {'111': {status: 'partial', components: {trace: {status: 'partial', source: 'flux_insight_openapi', warnings: ['原始 spans 不可用']}, artifacts: {status: 'ok'}}}},
    }});
  });
  try {
    await page.goto(baseURL);
    await page.locator('[data-view="exports"]').click(); await page.locator('[data-preset="all"]').click(); await page.locator('[data-view="tasks"]').click();
    assert.equal(await page.locator('#data-export-start').isDisabled(), true);
    await page.locator('#query').click();
    await page.waitForFunction(() => !document.querySelector('#data-export-start').disabled);
    await page.locator('[data-view="exports"]').click();
    await page.locator('#data-export-start').click();
    await page.waitForFunction(() => !document.querySelector('#data-export-download').disabled);
    assert.deepEqual(bodies[0].task_ids, ['111', '222']);
    assert.ok(bodies[0].components.includes('artifacts'));
    assert.match(await page.locator('#data-export-results').textContent(), /原始 spans 不可用/);
    const downloadEvent = page.waitForEvent('download');
    await page.locator('#data-export-download').click();
    assert.equal((await downloadEvent).suggestedFilename(), 'flux_export_job1.zip');
    await page.getByRole('button', {name: '新建批次', exact: true}).click();
    await page.locator('#data-export-scope').selectOption('all');
    await page.locator('#data-export-start').click();
    await page.locator('#data-export-cancel').waitFor({state: 'visible'});
    assert.ok(bodies[1].filters.filter_fields.length);
    assert.equal('task_ids' in bodies[1], false);
    await page.locator('#data-export-cancel').click();
    await page.waitForFunction(() => document.querySelector('#data-export-status').textContent.includes('已取消'));
    assert.equal(cancelled, true);
    assert.deepEqual(errors, []);
    await page.screenshot({path: '/tmp/flux-data-export-desktop.png', fullPage: true});
  } finally { await page.close(); }
});

test('export table: per-task progress, retained details, warnings, search and pagination', async () => {
  const page = await browser.newPage({viewport: {width: 1280, height: 1000}});
  const ids = Array.from({length: 62}, (_, i) => String(1000 + i));
  const errors = [];
  let phase = 'running', polls = 0;
  const task = () => ({status: phase === 'running' ? 'running' : 'partial', components: {
    task: {status: 'ok'},
    trace: {status: 'partial', source: 'fornax', warnings: ['原始 spans 不完整'], errors: {raw: {code: 'TRACE_UNAVAILABLE', message: '<img src=x onerror=alert(1)> Trace 不可用'}}},
  }});
  const job = () => ({job_id: 'table-job', status: phase, task_count: ids.length,
    completed_count: phase === 'running' ? 0 : 2, archive_ready: phase !== 'running',
    tasks: {'1000': task(), ...(phase === 'running' ? {} : {'1001': {status: 'error', components: {task: {status: 'error', error: {code: 'DENIED', message: '无权访问任务'}}}}})}});
  page.on('pageerror', (error) => errors.push(error.message));
  await page.route('**/v1/**', async (route) => {
    if (await auxiliary(route)) return;
    if (route.request().url().endsWith('/query')) {
      await route.fulfill({json: {task_ids: ids, count: ids.length, scanned_count: ids.length, has_more: false}}); return;
    }
    if (route.request().method() === 'GET') polls += 1;
    await route.fulfill({json: job()});
  });
  try {
    await page.goto(baseURL); await page.locator('#query').click();
    await page.locator('[data-view="exports"]').click();
    await page.locator('#data-export-start').waitFor({state: 'visible'});
    await page.waitForFunction(() => !document.querySelector('#data-export-start').disabled);
    await page.locator('#data-export-start').click();
    await page.waitForFunction(() => document.querySelectorAll('#export-task-rows tr[data-task-id]').length === 50);
    assert.equal(await page.locator('tr[data-task-id="1000"] > td .export-status').first().textContent(), '正在拉取');
    assert.equal(await page.locator('tr[data-task-id="1001"] > td .export-status').first().textContent(), '等待开始');
    assert.equal(await page.locator('#export-progress').getAttribute('value'), '0');
    assert.match(await page.locator('tr[data-task-id="1000"] .export-component-count').textContent(), /2 \/ 4/);
    const details = page.locator('tr[data-task-id="1000"] details');
    await details.locator('summary').click();
    const oldPolls = polls;
    await page.waitForFunction(() => document.querySelector('tr[data-task-id="1000"] details').open);
    phase = 'partial';
    await page.waitForFunction(() => document.querySelector('#data-export-status').textContent.includes('部分信息可用'));
    assert.ok(polls > oldPolls);
    assert.equal(await details.getAttribute('open'), '');
    assert.match(await details.textContent(), /TRACE_UNAVAILABLE/);
    assert.match(await details.textContent(), /来源：Fornax/);
    assert.equal(await page.locator('#data-export-results img').count(), 0);
    assert.equal(await page.locator('#export-progress').getAttribute('value'), '2');
    await page.locator('#export-task-filter').selectOption('issues');
    assert.match(await page.locator('#export-visible-count').textContent(), /62/);
    await page.locator('#export-task-search').fill('1001');
    assert.equal(await page.locator('#export-task-rows tr[data-task-id]').count(), 1);
    assert.equal(await page.locator('#export-task-rows tr').getAttribute('data-task-id'), '1001');
    await page.locator('#export-task-search').fill('does-not-exist');
    assert.match(await page.locator('#export-task-rows').textContent(), /没有符合条件/);
    await page.locator('#export-task-search').fill('');
    await page.locator('#export-next').click();
    assert.equal(await page.locator('#export-task-rows tr[data-task-id]').count(), 12);
    await page.locator('#export-previous').click();
    assert.equal(await details.getAttribute('open'), '');
    await page.locator('#export-task-filter').selectOption('all');
    await page.locator('#data-export-results').scrollIntoViewIfNeeded();
    await page.screenshot({path: '/tmp/flux-export-table-desktop.png', fullPage: true});
    await page.setViewportSize({width: 390, height: 844});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    assert.equal(await page.evaluate(() => {const table = document.querySelector('.export-table-scroll'); return table.scrollWidth > table.clientWidth;}), true);
    await page.screenshot({path: '/tmp/flux-export-table-mobile.png', fullPage: true});
    assert.deepEqual(errors, []);
  } finally { await page.close(); }
});

test('export polling: connection failure retains results and cancellation, retry restores progress', async () => {
  const page = await browser.newPage({viewport: {width: 1100, height: 1000}});
  let fail = true, cancelled = false;
  await page.route('**/v1/**', async (route) => {
    if (await auxiliary(route)) return;
    const url = new URL(route.request().url()).pathname;
    if (url.endsWith('/query')) {
      await route.fulfill({json: {task_ids: ['111', '222'], count: 2, scanned_count: 2, has_more: false}}); return;
    }
    if (url.endsWith('/cancel')) {
      cancelled = true; await route.fulfill({json: {status: 'cancelled'}}); return;
    }
    if (route.request().method() === 'GET' && fail) { await route.abort('failed'); return; }
    await route.fulfill({json: {job_id: 'retry-job', status: cancelled ? 'cancelled' : 'running',
      task_count: 2, completed_count: 0, archive_ready: cancelled, tasks: {'111': {status: cancelled ? 'cancelled' : 'running', components: {task: {status: 'ok'}}}}}});
  });
  try {
    await page.goto(baseURL); await page.locator('#query').click();
    await page.waitForFunction(() => !document.querySelector('#data-export-start').disabled);
    await page.locator('[data-view="exports"]').click();
    await page.locator('#data-export-start').click();
    await page.locator('#data-export-retry').waitFor({state: 'visible'});
    assert.equal(await page.locator('#export-task-rows tr[data-task-id]').count(), 2);
    assert.equal(await page.locator('#data-export-start').isDisabled(), true);
    assert.equal(await page.locator('#data-export-cancel').isVisible(), true);
    fail = false;
    await page.locator('#data-export-retry').click();
    await page.waitForFunction(() => document.querySelector('#data-export-status').textContent === '正在拉取');
    await page.locator('#data-export-cancel').click();
    await page.waitForFunction(() => document.querySelector('#data-export-status').textContent === '已取消');
    assert.equal(await page.locator('tr[data-task-id="222"] > td .export-status').first().textContent(), '已取消');
    assert.equal(await page.locator('#data-export-download').isEnabled(), true);
    assert.equal(await page.locator('#data-export-start').isEnabled(), true);
    assert.equal(await page.locator('#data-export-cancel').isHidden(), true);
    assert.equal(await page.locator('#export-progress').getAttribute('value'), '0');
  } finally { await page.close(); }
});

test('export failures: individual reasons, fallback sources and file paths remain visible', async () => {
  const page = await browser.newPage({viewport: {width: 1280, height: 1000}});
  await page.route('**/v1/**', async (route) => {
    if (await auxiliary(route)) return;
    if (route.request().url().endsWith('/query')) {
      await route.fulfill({json: {task_ids: ['111', '222'], count: 2, scanned_count: 2, has_more: false}}); return;
    }
    await route.fulfill({json: {job_id: 'failure-details', status: 'partial', completed_count: 2, task_count: 2, archive_ready: true, tasks: {
      '111': {status: 'partial', components: {
        task: {status: 'ok'},
        trace: {status: 'partial', source: 'fornax', fallback_errors: {openapi: {code: 'UPSTREAM_FORBIDDEN', message: '无权读取 Trace'}}},
        metrics: {status: 'error', error: {code: 'QUERY_TIMEOUT', message: '指标请求超时'}},
        artifacts: {status: 'partial', file_count: 3, downloaded_count: 1, errors: {
          'artifacts/a.md': {code: 'UPSTREAM_NOT_FOUND', message: '文件不存在'},
          'artifacts/b.bin': {code: 'ARTIFACT_INTEGRITY_ERROR', message: '文件校验失败'},
        }},
        versions: {status: 'error'},
      }},
      '222': {status: 'error', components: {'artifacts/manifest': {status: 'error', error: {code: 'UPSTREAM_FORBIDDEN', message: '无权读取产物清单'}}}},
    }}});
  });
  try {
    await page.goto(baseURL); await page.locator('#query').click();
    await page.waitForFunction(() => !document.querySelector('#data-export-start').disabled);
    await page.locator('[data-view="exports"]').click();
    await page.locator('[data-preset="all"]').click();
    await page.locator('#data-export-start').click();
    await page.waitForFunction(() => !document.querySelector('#data-export-download').disabled);
    const row = page.locator('tr[data-task-id="111"]');
    const previews = row.locator('.export-issue-previews');
    assert.equal(await previews.isVisible(), false);
    assert.match(await previews.textContent(), /来源 openapi 获取失败.*无权读取 Trace/);
    assert.match(await previews.textContent(), /上游未返回具体原因/);
    assert.match(await previews.textContent(), /另有 1 条原因/);
    await row.locator('details summary').click();
    assert.match(await row.locator('details').textContent(), /artifacts\/b.bin：文件校验失败 \(ARTIFACT_INTEGRITY_ERROR\)/);
    assert.match(await row.locator('details').textContent(), /已下载 1 \/ 3 个/);
    const manifestRow = page.locator('tr[data-task-id="222"]');
    assert.match(await manifestRow.locator('.export-component-chips').textContent(), /产物文件 · 获取失败/);
    await manifestRow.locator('details summary').click();
    assert.match(await manifestRow.locator('details').textContent(), /无权读取产物清单/);
    await page.locator('#data-export-results').screenshot({path: '/tmp/flux-export-failure-details.png'});
  } finally { await page.close(); }
});

test('time axis: automatic ruler, map zoom and pan, shortcuts and precise selection', async () => {
  const page = await browser.newPage({viewport: {width: 1280, height: 1000}, timezoneId: 'America/Los_Angeles'});
  const calls = [], errors = [];
  await page.clock.install({time: new Date('2026-03-01T00:10:00+08:00')});
  page.on('pageerror', (error) => errors.push(error.message));
  await page.route('**/v1/**', async (route) => {
    if (await auxiliary(route)) return;
    calls.push(route.request().postDataJSON());
    await route.fulfill({json: {task_ids: ['111'], count: 1, scanned_count: 1, has_more: false}});
  });
  try {
    await page.goto(baseURL);
    const row = page.locator('.condition-row').first();
    assert.match(await page.locator('.scope-note').textContent(), /北京时间.*昨天/);
    const start = row.locator('input[type="datetime-local"]').first();
    const end = row.locator('input[type="datetime-local"]').last();
    const values = async () => [await start.inputValue(), await end.inputValue()];
    assert.deepEqual(await values(), ['2026-02-28T00:00', '2026-02-28T23:59:59']);
    await page.locator('#query').click();
    await page.waitForFunction(() => !document.querySelector('#copy').disabled);
    assert.deepEqual(calls[0].filters.filter_fields[0].values, ['2026-02-28 00:00:00', '2026-02-28 23:59:59']);
    const selected = await values();
    assert.equal(await row.locator('.time-scale,.time-presets').count(), 0);
    const view = row.locator('.time-axis');
    const viewData = () => view.evaluate((node) => ({start: Number(node.dataset.viewStart), end: Number(node.dataset.viewEnd), tick: Number(node.dataset.tickInterval)}));
    const original = await viewData();
    let axis = await view.boundingBox();
    // Wheel zoom changes the ruler around the cursor without changing filters.
    await view.evaluate((node) => node.addEventListener('wheel', (event) => { node.dataset.wheelX = event.clientX; }, {once: true}));
    await page.mouse.move(axis.x + axis.width / 2, axis.y + 70);
    await page.mouse.wheel(0, -500);
    await page.waitForFunction((span) => { const node = document.querySelector('.time-axis'); return Number(node.dataset.viewEnd) - Number(node.dataset.viewStart) < span; }, original.end - original.start);
    const zoomed = await viewData();
    assert.ok(zoomed.tick < original.tick);
    const fraction = (Number(await view.getAttribute('data-wheel-x')) - axis.x) / axis.width;
    const anchor = original.start + (original.end - original.start) * fraction;
    assert.ok(Math.abs(zoomed.start + (zoomed.end - zoomed.start) * fraction - anchor) < 1000);
    assert.deepEqual(await values(), selected);
    assert.equal(await page.locator('#copy').isEnabled(), true);
    await view.focus(); await view.press('Home');
    let before = await viewData();
    // Dragging blank space pans the timeline; it never moves the selection.
    await page.mouse.move(axis.x + axis.width * .4, axis.y + 75);
    await page.mouse.down();
    await page.mouse.move(axis.x + axis.width * .65, axis.y + 75, {steps: 5});
    await page.mouse.up();
    assert.ok((await viewData()).start < before.start);
    assert.deepEqual(await values(), selected);
    assert.equal(await page.locator('#copy').isEnabled(), true);
    await view.focus(); await view.press('Home');
    for (let i = 0; i < 20; i++) await view.press('-');
    assert.match(await row.locator('.time-ruler-label').textContent(), /个月/);
    const monthTicks = await row.locator('.time-tick').allTextContents();
    assert.ok(monthTicks.every((label) => /^\d{4}\/\d{2}$/.test(label)));
    assert.deepEqual(await values(), selected);
    await view.press('F');
    const fitted = await viewData();
    assert.ok(fitted.start <= Date.parse(`${selected[0]}+08:00`) && fitted.end >= Date.parse(`${selected[1]}+08:00`));
    await view.press('Home');
    await view.press('?');
    assert.equal(await row.locator('.time-help').isVisible(), true);
    await view.press('Escape');
    assert.equal(await row.locator('.time-help').isHidden(), true);
    const begin = row.getByRole('slider', {name: '拖动开始时间'});
    await begin.focus(); await begin.press('ArrowLeft');
    assert.equal(await start.inputValue(), '2026-02-27T21:00');
    assert.equal(await page.locator('#copy').isDisabled(), true);
    axis = await row.locator('.time-axis').boundingBox();
    let handle = await begin.boundingBox();
    await page.mouse.move(handle.x + handle.width / 2, handle.y + handle.height / 2);
    await page.mouse.down();
    await page.mouse.move(axis.x + axis.width * 0.25, handle.y + handle.height / 2, {steps: 5});
    await page.mouse.up();
    assert.ok((await start.inputValue()) < '2026-02-27T21:00');
    const finish = row.getByRole('slider', {name: '拖动结束时间'});
    handle = await finish.boundingBox();
    await page.mouse.move(handle.x + handle.width / 2, handle.y + handle.height / 2);
    await page.mouse.down();
    await page.mouse.move(axis.x + axis.width * 0.75, handle.y + handle.height / 2, {steps: 5});
    await page.mouse.up();
    assert.ok((await end.inputValue()) < '2026-02-28T23:59:59');
    assert.ok((await start.inputValue()) <= (await end.inputValue()));
    const beforeFine = Date.parse(`${await end.inputValue()}+08:00`);
    await finish.press('Alt+ArrowLeft');
    assert.equal(Date.parse(`${await end.inputValue()}+08:00`), beforeFine - 1000);
    await view.focus(); await view.press('r');
    assert.deepEqual(await values(), selected);
    await row.locator('.time-precise summary').click();
    await start.fill('2026-01-02T12:34:56'); await start.dispatchEvent('change');
    await end.fill('2026-01-03T18:01:02'); await end.dispatchEvent('change');
    assert.match(await row.locator('.time-selected').textContent(), /2026-01-02 12:34:56/);
    await page.locator('#query').click();
    await page.waitForFunction(() => !document.querySelector('#copy').disabled);
    assert.deepEqual(calls.at(-1).filters.filter_fields[0].values, ['2026-01-02 12:34:56', '2026-01-03 18:01:02']);
    await row.locator('.time-precise summary').click();
    await end.fill(''); await end.dispatchEvent('change');
    await page.locator('#query').click();
    await page.waitForFunction(() => !document.querySelector('#copy').disabled);
    assert.equal(calls.at(-1).filters.filter_fields[0].values[1], '');
    const beforeFuture = calls.length;
    await row.locator('.time-precise summary').click();
    await end.fill('2026-03-01T00:00'); await end.dispatchEvent('change');
    await page.locator('#filter-form').evaluate((form) => form.dispatchEvent(new Event('submit', {cancelable: true, bubbles: true})));
    await page.locator('#query-error').waitFor({state: 'visible'});
    assert.match(await page.locator('#query-error').textContent(), /T\+1/);
    assert.equal(calls.length, beforeFuture);
    await page.locator('#reset').click();
    assert.deepEqual(await values(), ['2026-02-28T00:00', '2026-02-28T23:59:59']);
    await row.locator('.condition-value').screenshot({path: '/tmp/flux-time-axis-compact.png'});
    await page.screenshot({path: '/tmp/flux-time-axis-desktop.png', fullPage: true});
    await page.setViewportSize({width: 390, height: 844});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.screenshot({path: '/tmp/flux-time-axis-mobile.png', fullPage: true});
    const touch = await page.context().newCDPSession(page);
    const mobileAxis = await row.locator('.time-axis').boundingBox();
    const mobileHandle = await begin.boundingBox();
    const touchY = mobileHandle.y + mobileHandle.height / 2;
    const oldStart = await start.inputValue();
    await touch.send('Input.dispatchTouchEvent', {type: 'touchStart', touchPoints: [{x: mobileHandle.x + mobileHandle.width / 2, y: touchY}]});
    await touch.send('Input.dispatchTouchEvent', {type: 'touchMove', touchPoints: [{x: mobileAxis.x + mobileAxis.width * 0.35, y: touchY}]});
    await touch.send('Input.dispatchTouchEvent', {type: 'touchEnd', touchPoints: []});
    assert.ok((await start.inputValue()) < oldStart);
    const mobileSelection = await values();
    before = await viewData();
    const panY = mobileAxis.y + 75;
    await touch.send('Input.dispatchTouchEvent', {type: 'touchStart', touchPoints: [{id: 1, x: mobileAxis.x + mobileAxis.width * .3, y: panY}]});
    await touch.send('Input.dispatchTouchEvent', {type: 'touchMove', touchPoints: [{id: 1, x: mobileAxis.x + mobileAxis.width * .5, y: panY}]});
    await touch.send('Input.dispatchTouchEvent', {type: 'touchEnd', touchPoints: []});
    assert.ok((await viewData()).start < before.start);
    assert.deepEqual(await values(), mobileSelection);
    before = await viewData();
    await touch.send('Input.dispatchTouchEvent', {type: 'touchStart', touchPoints: [{id: 1, x: mobileAxis.x + mobileAxis.width * .3, y: panY}, {id: 2, x: mobileAxis.x + mobileAxis.width * .7, y: panY}]});
    await touch.send('Input.dispatchTouchEvent', {type: 'touchMove', touchPoints: [{id: 1, x: mobileAxis.x + mobileAxis.width * .15, y: panY}, {id: 2, x: mobileAxis.x + mobileAxis.width * .85, y: panY}]});
    await touch.send('Input.dispatchTouchEvent', {type: 'touchEnd', touchPoints: []});
    const afterPinch = await viewData();
    assert.ok(afterPinch.end - afterPinch.start < before.end - before.start);
    assert.deepEqual(await values(), mobileSelection);
    await touch.detach();
    assert.deepEqual(errors, []);
  } finally { await page.close(); }
});

test('time axis: one-second ruler and double-clicking either offscreen endpoint', async () => {
  const {page, errors} = await setup({width: 1440, height: 1000});
  const view = page.locator('.time-axis');
  const start = page.locator('input[aria-label="开始时间（北京时间）"]');
  const end = page.locator('input[aria-label="结束时间（北京时间）"]');
  const values = async () => [await start.inputValue(), await end.inputValue()];
  const viewData = () => view.evaluate((node) => ({start: Number(node.dataset.viewStart), end: Number(node.dataset.viewEnd), tick: Number(node.dataset.tickInterval)}));
  try {
    await page.locator('#query').click();
    await page.waitForFunction(() => !document.querySelector('#copy').disabled);
    const selected = await values();
    await view.focus();
    for (let i = 0; i < 75; i++) await view.press('+');
    const narrow = await viewData();
    assert.equal(narrow.tick, 1000);
    assert.equal(await page.locator('.time-ruler-label').textContent(), '每格 1 秒');
    assert.ok((await page.locator('.time-tick').allTextContents()).every((label) => /^\d{2}:\d{2}:\d{2}$/.test(label)));
    assert.deepEqual(await values(), selected);
    const begin = page.getByRole('slider', {name: '拖动开始时间'});
    const finish = page.getByRole('slider', {name: '拖动结束时间'});
    assert.equal(await begin.evaluate((node) => node.classList.contains('time-handle-outside')), true);
    await begin.dblclick();
    let current = await viewData();
    const startTime = Date.parse(`${selected[0]}+08:00`);
    assert.ok(current.start <= startTime && current.end >= startTime);
    assert.ok(Math.abs(current.end - current.start - (narrow.end - narrow.start)) < 1);
    assert.deepEqual(await values(), selected);
    assert.equal(await finish.evaluate((node) => node.classList.contains('time-handle-outside')), true);
    await finish.dblclick();
    current = await viewData();
    const endTime = Date.parse(`${selected[1]}+08:00`);
    assert.ok(current.start <= endTime && current.end >= endTime);
    assert.ok(Math.abs(current.end - current.start - (narrow.end - narrow.start)) < 1);
    assert.deepEqual(await values(), selected);
    assert.equal(await page.locator('#copy').isEnabled(), true);
    await finish.focus(); await finish.press('ArrowLeft');
    assert.equal(Date.parse(`${await end.inputValue()}+08:00`), endTime - 1000);
    assert.equal(await page.locator('#copy').isDisabled(), true);
    await page.locator('.condition-value').screenshot({path: '/tmp/flux-time-axis-seconds.png'});
    await begin.focus(); await begin.press('Enter');
    current = await viewData();
    assert.ok(current.start <= startTime && current.end >= startTime);
    assert.deepEqual(errors, []);
  } finally { await page.close(); }
});

test('time axis modal: wider view, shared edits, focus trap, form submission and close', async () => {
  const {page, errors} = await setup({width: 1440, height: 1000});
  const view = page.locator('.time-axis');
  const start = page.locator('input[aria-label="开始时间（北京时间）"]');
  const end = page.locator('input[aria-label="结束时间（北京时间）"]');
  const values = async () => [await start.inputValue(), await end.inputValue()];
  const viewData = () => view.evaluate((node) => ({width: node.clientWidth, span: Number(node.dataset.viewEnd) - Number(node.dataset.viewStart)}));
  try {
    await page.locator('#query').click();
    await page.waitForFunction(() => !document.querySelector('#copy').disabled);
    const selected = await values(), original = await viewData();
    await page.getByRole('button', {name: '放大时间轴', exact: true}).click();
    const dialog = page.getByRole('dialog', {name: '选择时间范围'});
    assert.equal(await dialog.isVisible(), true);
    const wide = await viewData();
    assert.ok(wide.width > original.width);
    assert.ok(wide.span > original.span);
    assert.deepEqual(await values(), selected);
    for (let i = 0; i < 9; i++) {
      await page.keyboard.press('Tab');
      assert.equal(await dialog.evaluate((node) => node.contains(document.activeElement)), true, await page.evaluate(() => document.activeElement?.outerHTML.slice(0, 200)));
    }
    await view.focus(); await view.press('-');
    assert.deepEqual(await values(), selected);
    const finish = dialog.getByRole('slider', {name: '拖动结束时间'});
    const rect = await view.boundingBox(), knob = await finish.boundingBox();
    await page.mouse.move(knob.x + knob.width / 2, knob.y + knob.height / 2);
    await page.mouse.down();
    await page.mouse.move(rect.x + rect.width * .9, knob.y + knob.height / 2, {steps: 5});
    await page.mouse.up();
    const edited = await values();
    assert.notDeepEqual(edited, selected);
    assert.equal(await page.locator('#copy').isDisabled(), true);
    await dialog.screenshot({path: '/tmp/flux-time-axis-modal.png'});
    await view.focus(); await view.press('Escape');
    assert.equal(await dialog.count(), 0);
    assert.deepEqual(await values(), edited);
    assert.equal(await page.evaluate(() => document.activeElement?.classList.contains('time-expand')), true);
    assert.equal(await page.evaluate(() => document.body.classList.contains('time-modal-open')), false);
    await page.getByRole('button', {name: '放大时间轴', exact: true}).click();
    await dialog.locator('.time-selected').click();
    assert.equal(await start.evaluate((input) => input.form?.id), 'filter-form');
    // Submitting from the relocated input still uses the real filter form,
    // and disables timeline editing while the request is in flight.
    await page.route('**/v1/task-ids/query', async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 400));
      await route.fulfill({json: {task_ids: ['111'], count: 1, scanned_count: 1, has_more: false}});
    });
    await end.press('Enter');
    await page.waitForFunction(() => document.querySelector('#query').disabled);
    assert.equal(await start.isDisabled(), true);
    assert.equal(await end.isDisabled(), true);
    assert.equal(await finish.isDisabled(), true);
    await page.waitForFunction(() => !document.querySelector('#query').disabled);
    assert.equal(await start.isEnabled(), true);
    await dialog.getByRole('button', {name: '关闭放大时间轴'}).click();
    assert.deepEqual(await values(), edited);
    await page.setViewportSize({width: 390, height: 844});
    await page.getByRole('button', {name: '放大时间轴', exact: true}).click();
    assert.equal(await dialog.isVisible(), true);
    assert.equal(await dialog.evaluate((node) => node.scrollWidth <= node.clientWidth), true);
    await dialog.screenshot({path: '/tmp/flux-time-axis-modal-mobile.png'});
    await page.mouse.click(2, 2);
    assert.equal(await dialog.count(), 0);
    assert.deepEqual(await values(), edited);
    assert.deepEqual(errors, []);
  } finally { await page.close(); }
});
