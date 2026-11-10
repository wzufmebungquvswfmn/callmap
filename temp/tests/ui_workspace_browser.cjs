// Browser integration against the served app, with deterministic API contracts.
const assert = require('node:assert/strict');
const {test, before, after} = require('node:test');
const {chromium} = require(process.argv[2] || 'playwright');
const base = process.env.FLUX_BROWSER_URL || 'http://127.0.0.1:18082';
const batch = 'a'.repeat(32), tagging = 'b'.repeat(32);
let browser;
before(async () => { browser = await chromium.launch({headless: true}); });
after(async () => { await browser?.close(); });
async function setup(viewport = {width: 1440, height: 1000}) {
  const page = await browser.newPage({viewport}), calls = [], errors = [];
  let tags = {repository: {source: 'rule', repositories: [{name: 'team/project', categories: ['user_specified'], purposes: ['target_code'], states: ['mentioned']}]}, task_classification: {source: 'rule', category: '测试'}};
  let conflicts = false;
  page.on('pageerror', (error) => errors.push(error.message));
  await page.route('**/v1/**', async (route) => {
    const req = route.request(), url = new URL(req.url()), path = url.pathname;
    const body = req.method() === 'POST' ? req.postDataJSON() : undefined;
    calls.push({path, url: req.url(), body, headers: req.headers()});
    const job = {job_id: batch, status: 'completed', archive_ready: true, task_count: 2, completed_count: 2, components: ['task', 'trace', 'metrics', 'artifacts', 'versions', 'gui', 'conversation'], request: {task_ids: ['111', '222']}, tasks: {'111': {status: 'ok', components: {task: {status: 'ok'}}}, '222': {status: 'partial', components: {task: {status: 'partial', warnings: ['部分资料未采集']}}}}};
    let data;
    if (path.startsWith('/v1/chatml/')) { await route.fulfill({body: `<|im_start|>${url.searchParams.get('mode')}\n<script>window.transcriptUnsafe=true</script>对话预览\n<|im_end|>`, contentType: 'text/plain'}); return; }
    else if (path === '/v1/exports' && req.method() === 'GET') data = {jobs: [job], total: 1};
    else if (path.endsWith('/overview')) data = {...job, tasks: undefined, task_counts: {ok: 1, partial: 1}, stages: {}};
    else if (path === `/v1/exports/${batch}/tasks`) data = {tasks: job.tasks, total: 2, offset: 0, limit: 50};
    else if (path === '/v1/tasks/summaries') data = {tasks: Object.fromEntries(body.task_ids.map((id) => [id, {title: id === '111' ? '支付服务 · 补充退款接口测试' : '用户中心 · 登录流程回归', status: 'finished', owner: '测试用户'}])), missing: []};
    else if (path.endsWith('/task-ids/query')) data = {task_ids: ['111', '222'], scanned_count: 2, has_more: true, next_cursor: 'next'};
    else if (path.endsWith('/task-ids/collect')) data = {task_ids: ['111', '222', '333'], scanned_count: 3};
    else if (path.endsWith('/filter-options')) { assert.equal(body.field, 'category'); data = {options: [{value: '测试', label: '测试'}]}; }
    else if (path.endsWith('/task-data/detail')) data = {items: [{task_id: body.task_id, task: {title: body.task_id === '111' ? '支付服务 · 补充退款接口测试' : '用户中心 · 登录流程回归', task_status: 'finished', created_by: '测试用户', task_created_at: '2026-11-09 10:30:00', level_1_category: '测试'}}]};
    else if (path.endsWith('/metrics')) data = {complete: false, values: {total_tokens: 12000, input_tokens: 10000, output_tokens: 2000, total_latency_ms: 62000, latency_ms_first_resp: 0, model_call_count: 4}};
    else if (path.endsWith('/trace')) data = {execution_complete: true, spans: [{span_id: 'one', name: '读取项目配置', status: 'finished'}, {span_id: 'two', name: '<img src=x onerror=alert(1)>', status: 'finished'}]};
    else if (path.endsWith('/conversation')) {
      if (body.action === 'inventory') data = {sessions: [{ticket: 'session-ticket'}]};
      else { assert.equal(body.action, 'messages'); assert.equal(body.ticket, 'session-ticket'); data = {data: {messages: [{role: body.cursor ? 'assistant' : 'user', content: body.cursor ? '已补充退款用例，并通过检查。' : '请为退款接口增加异常场景测试。'}]}, meta: {has_more: !body.cursor, next_cursor: body.cursor ? null : 'page-2', complete: true}}; }
    }
    else if (path.endsWith('/artifacts/list')) data = {worker_session_id: '555', files: [{path: 'src/refund.test.ts', size_bytes: 2048}]};
    else if (path.endsWith('/artifacts/raw')) { assert.equal(body.worker_session_id, '555'); await route.fulfill({body: 'test file', contentType: 'application/octet-stream'}); return; }
    else if (path.endsWith('/artifacts/versions')) data = {versions: [{version_id: 0}, {version_id: 2}], comparisons: [{from_version_id: 0, to_version_id: 2}]};
    else if (path.endsWith('/version-diff')) { assert.equal(body.from_version_id, 0); data = {files: [{path: 'src/refund.test.ts', patch: '+ test("refund", () => {})'}]}; }
    else if (path.endsWith('/case-result')) data = {items: [{case_result: {name: '退款成功', status: 'passed', screenshot: 'https://invalid/case.png'}}]};
    else if (path.endsWith('/screenshots')) { await route.fulfill({body: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jL1sAAAAASUVORK5CYII=', 'base64'), contentType: 'image/png'}); return; }
    else if (path.endsWith('/fornax/auth')) data = {status: 'loaded', upstream_verified: false};
    else if (path.endsWith('/tag-schema')) data = {schema: {properties: {review: {}, repository: {}, task_classification: {}}}};
    else if (path.endsWith('/task/111')) {
      if (req.method() === 'POST') {
        assert.equal(req.headers()['if-match'], '"rev-1"');
        if (conflicts) { await route.fulfill({status: 412, json: {error: {code: 'STALE'}}}); return; }
        tags = body;
      }
      await route.fulfill({json: tags, headers: {ETag: '"rev-1"'}}); return;
    }
    else if (path.endsWith('/tasks/111')) data = {core: {test_evidence_level: 'result_observed', runs: []}, dynamic_tags: tags};
    else if (path.endsWith('/results')) data = {total: 1, records: [{task_id: '111', candidate_level: 'priority_candidate', decision: 'keep', dynamic_tags: tags, test_evidence_level: 'result_observed', test_types: ['api'], reason_codes: ['TEST_EXECUTION_RESULT_OBSERVED']}]};
    else if (path.endsWith('/selected')) data = [{task_id: '111'}];
    else if (path.endsWith('/download')) { await route.fulfill({body: 'zip', contentType: 'application/zip'}); return; }
    else if (path.includes('/tagging')) data = {tagging_id: tagging, export_job_id: batch, status: 'completed', task_count: 2, completed_count: 2, archive_ready: true, decision_counts: {keep: 1, exclude: 1}, policy: {}};
    else if (path.startsWith('/v1/exports')) data = job;
    else throw new Error(`Unhandled request ${path}`);
    await route.fulfill({json: data});
  });
  await page.goto(base);
  return {page, calls, errors, setConflict: () => { conflicts = true; }};
}
test('task workflow: collect, select, inspect content and download prepared data', async () => {
  const {page, calls, errors} = await setup();
  try {
    assert.equal(await page.locator('[data-workspace="exports"]').isVisible(), false);
    await page.locator('#query').click(); await page.locator('.task-list-row').first().waitFor();
    await page.getByRole('button', {name: '支付服务 · 补充退款接口测试'}).waitFor();
    assert.equal(calls.filter((call) => call.path.endsWith('/summaries')).length, 1);
    assert.equal(calls.filter((call) => call.path.endsWith('/detail')).length, 0);
    await page.screenshot({path: '/private/tmp/flux-workspace-desktop.png', fullPage: true});
    await page.locator('#collect-all').click(); await page.waitForFunction(() => document.querySelectorAll('.task-list-row').length === 3);
    await page.getByLabel('选择任务 111').check();
    await page.getByRole('button', {name: '支付服务 · 补充退款接口测试'}).click();
    await page.locator('.detail-metrics').waitFor();
    assert.match(await page.locator('#task-detail-content').textContent(), /0 毫秒/);
    assert.match(await page.locator('#task-detail-content').textContent(), /统计可能不完整/);
    await page.screenshot({path: '/private/tmp/flux-workspace-detail.png', fullPage: true});
    await page.locator('[data-detail="conversation"]').click();
    await page.getByText('请为退款接口增加异常场景测试。', {exact: true}).waitFor();
    await page.getByRole('button', {name: '继续读取', exact: true}).click();
    await page.getByText('已补充退款用例，并通过检查。', {exact: true}).waitFor();
    assert.equal(await page.getByRole('button', {name: '继续读取', exact: true}).isVisible(), false);
    await page.locator('[data-detail="trace"]').click(); await page.getByText('读取项目配置', {exact: true}).waitFor();
    assert.equal(await page.locator('#task-detail-content img').count(), 0);
    await page.locator('[data-detail="artifacts"]').click(); await page.getByRole('button', {name: '▸ src · 1 个文件'}).click(); await page.locator('.detail-file > span').filter({hasText: 'refund.test.ts'}).waitFor();
    const download = page.waitForEvent('download'); await page.locator('#task-detail-content').getByRole('button', {name: '下载', exact: true}).click();
    assert.equal((await download).suggestedFilename(), 'refund.test.ts');
    await page.locator('[data-detail="versions"]').click(); await page.getByRole('button', {name: '版本 0 → 2'}).click();
    await page.getByText('+ test("refund", () => {})', {exact: true}).waitFor();
    await page.locator('[data-detail="gui"]').click(); await page.getByRole('button', {name: '查看截图'}).click();
    await page.locator('#task-detail-content img').waitFor();
    assert.ok(await page.locator('#task-detail-content img').evaluate((img) => img.complete && img.naturalWidth > 0));
    await page.getByRole('button', {name: '关闭任务详情'}).click();
    await page.locator('#export-selection').click();
    assert.equal(await page.locator('#data-export-scope').inputValue(), 'selected');
    await page.locator('[data-preset="files"]').click(); await page.locator('#data-export-start').click();
    await page.locator('#data-export-status').filter({hasText: '拉取完成'}).waitFor();
    const submitted = calls.find((call) => call.path === '/v1/exports' && call.body);
    assert.deepEqual(submitted.body.task_ids, ['111']); assert.deepEqual(submitted.body.components, ['task', 'artifacts', 'versions']);
    await page.screenshot({path: '/private/tmp/flux-workspace-exports.png', fullPage: true});
    await page.locator('#data-export-refetch').click();
    await page.waitForFunction(() => !document.querySelector('#data-export-refetch').disabled);
    assert.ok(calls.some((call) => call.path.endsWith('/retry')));
    const zip = page.waitForEvent('download'); await page.locator('#data-export-download').click(); assert.match((await zip).suggestedFilename(), /\.zip$/);
    assert.deepEqual(errors, []);
  } finally { await page.close(); }
});
test('candidate workflow: history, editable tags, preservation and conflict handling', async () => {
  const {page, calls, errors, setConflict} = await setup();
  try {
    await page.locator('[data-view="exports"]').click();
    await page.getByText('打开其他批次', {exact: true}).click(); await page.locator('#resume-export-id').fill(batch); await page.locator('#resume-export').click();
    await page.waitForFunction(() => !document.querySelector('#go-candidates').disabled);
    await page.locator('#go-candidates').click(); await page.locator('#tagging-start').click();
    await page.locator('#tagging-rows').getByText('查看证据与其它标签').click();
    await page.getByText('人工复核与标签', {exact: true}).click();
    await page.getByLabel('复核结论', {exact: true}).selectOption('keep');
    await page.getByLabel('复核原因', {exact: true}).fill('已核对测试证据');
    await page.locator('.transcript-text').filter({hasText: '对话预览'}).waitFor();
    assert.ok(calls.some((call) => call.url.includes(`/v1/chatml/${batch}/111?mode=assistant`)));
    await page.getByLabel('对话预览模式', {exact: true}).selectOption('user');
    await page.locator('.transcript-text').filter({hasText: '<|im_start|>user'}).waitFor();
    assert.equal(await page.getByLabel('复核原因', {exact: true}).inputValue(), '已核对测试证据');
    await page.getByLabel('对话预览模式', {exact: true}).selectOption('all');
    await page.locator('.transcript-text').filter({hasText: '<|im_start|>all'}).waitFor();
    assert.equal(await page.evaluate(() => window.transcriptUnsafe), undefined);
    await page.getByRole('button', {name: '保存复核'}).click();
    await page.getByText('复核已保存。自动筛选结论保持独立。', {exact: true}).waitFor();
    const saved = calls.find((call) => call.path.endsWith('/task/111') && call.body);
    assert.equal(saved.body.review.source, 'human'); assert.equal(saved.body.repository.repositories[0].name, 'team/project');
    assert.equal(saved.body.repository.source, 'rule'); assert.equal(saved.body.task_classification.source, 'rule');
    setConflict(); await page.getByRole('button', {name: '保存复核'}).click(); await page.getByRole('button', {name: '重新读取标签'}).waitFor();
    await page.getByRole('button', {name: '重新读取标签'}).click();
    await page.getByRole('button', {name: '保存复核'}).waitFor();
    await page.screenshot({path: '/private/tmp/flux-workspace-review.png', fullPage: true});
    await page.getByRole('button', {name: '关闭人工复核'}).click();
    await page.locator('.review-decision').filter({hasText: '复核：保留'}).waitFor();
    await page.locator('#tagging-select-kept').click(); await page.locator('#tagging-selection-count').filter({hasText: '已选 1'}).waitFor();
    for (const [button, endpoint] of [['tagging-download-source', `/v1/exports/${batch}/download`], ['tagging-download-subset', `/v1/tagging/${tagging}/download`]]) {
      const event = page.waitForEvent('download'); await page.locator(`#${button}`).click(); assert.match((await event).suggestedFilename(), /selected_1\.zip$/);
      const url = new URL(calls.findLast((call) => call.path === endpoint).url); assert.deepEqual(url.searchParams.getAll('task_id'), ['111']);
    }
    const download = page.waitForEvent('download'); await page.locator('#tagging-selected').click(); assert.match((await download).suggestedFilename(), /\.json$/);
    await page.screenshot({path: '/private/tmp/flux-workspace-candidates.png', fullPage: true});
    assert.deepEqual(errors, []);
  } finally { await page.close(); }
});
test('mobile navigation, direct identities, settings and failed reads remain usable', async () => {
  const {page, calls, errors} = await setup({width: 390, height: 844});
  try {
    await page.locator('#query').click(); await page.locator('.task-list-row').first().waitFor();
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.screenshot({path: '/private/tmp/flux-workspace-mobile.png', fullPage: true});
    await page.locator('#open-task').click(); await page.locator('#identity-kind').selectOption('worker_session_id'); await page.locator('#task-open-id').fill('555');
    await page.locator('#task-open-form').getByRole('button', {name: '查看'}).click();
    await page.getByRole('button', {name: '▸ src · 1 个文件'}).click(); await page.locator('.detail-file > span').filter({hasText: 'refund.test.ts'}).waitFor();
    assert.equal(await page.locator('[data-detail="overview"]').isVisible(), false);
    assert.equal(await page.locator('#task-dialog').evaluate((dialog) => dialog.scrollWidth <= dialog.clientWidth), true);
    await page.locator('#identity-kind').selectOption('session_id'); await page.locator('#task-open-id').fill('666'); await page.locator('#task-open-form').getByRole('button', {name: '查看'}).click();
    await page.getByText('请为退款接口增加异常场景测试。', {exact: true}).waitFor();
    assert.ok(calls.some((call) => call.body?.session_id === '666' && call.body.action === 'inventory'));
    await page.getByRole('button', {name: '关闭任务详情'}).click();
    await page.locator('#open-settings').click(); await page.locator('#api-key').fill('private-test-key');
    await page.locator('#fornax-token').fill('private-test-token'); await page.getByRole('button', {name: '更新凭据', exact: true}).click();
    await page.getByText('凭据已加载，读取资料时会验证可用性。', {exact: true}).waitFor();
    assert.equal(await page.locator('#fornax-token').inputValue(), '');
    assert.equal(await page.evaluate(() => JSON.stringify(localStorage).includes('private-test')), false);
    await page.getByRole('button', {name: '关闭连接设置'}).click();
    await page.route('**/v1/task-data/detail', (route) => route.fulfill({status: 403, json: {error: {code: 'UPSTREAM_FORBIDDEN'}}}));
    await page.locator('#open-task').click(); await page.locator('#task-open-id').fill('111'); await page.locator('#task-open-form').getByRole('button', {name: '查看'}).click();
    await page.getByText('当前账号没有读取这份资料的权限。', {exact: true}).waitFor();
    assert.equal(await page.locator('.detail-metric').count(), 6);
    assert.deepEqual(errors, []);
  } finally { await page.close(); }
});

test('slow metrics do not block task content, and default download avoids bulk files', async () => {
  const {page, errors} = await setup(); let release;
  await page.route('**/v1/task-data/metrics', async (route) => {
    await new Promise((resolve) => { release = resolve; });
    await route.fulfill({json: {complete: true, values: {total_tokens: 7}}});
  });
  try {
    assert.deepEqual(await page.locator('#data-export-components input:checked').evaluateAll((nodes) => nodes.map((node) => node.value)), ['task', 'trace', 'metrics', 'conversation']);
    await page.locator('#open-task').click(); await page.locator('#task-open-id').fill('111');
    await page.locator('#task-open-form').getByRole('button', {name: '查看'}).click();
    await page.getByRole('heading', {name: '支付服务 · 补充退款接口测试'}).waitFor({timeout: 3000});
    assert.equal(await page.locator('.detail-metric').count(), 0);
    release(); await page.locator('.detail-metric').first().waitFor();
    assert.deepEqual(errors, []);
  } finally { release?.(); await page.close(); }
});

test('switch active batches, page server results and read snapshots without upstream', async () => {
  const {page, errors} = await setup(); const other = 'c'.repeat(32), requests = []; let release;
  page.on('request', (request) => requests.push(new URL(request.url()).pathname));
  const ids = ['111', ...Array.from({length: 124}, (_, i) => String(200 + i))];
  const complete = {job_id: other, status: 'completed', created_at: 1794000000000, task_count: 125, completed_count: 125, archive_ready: true, components: ['task', 'metrics', 'artifacts'], task_counts: {ok: 125}, stages: {}};
  await page.route('**/v1/exports?*', (route) => route.fulfill({json: {jobs: [{...complete, job_id: batch, status: 'running', archive_ready: false}, complete], total: 2}}));
  await page.route(`**/v1/exports/${batch}/overview`, async (route) => {
    await new Promise((resolve) => { release = resolve; });
    await route.fulfill({json: {...complete, job_id: batch, status: 'running', archive_ready: false}}).catch(() => {});
  });
  await page.route(`**/v1/exports/${other}/overview`, (route) => route.fulfill({json: complete}));
  await page.route(`**/v1/exports/${other}/tasks?*`, (route) => {
    const query = new URL(route.request().url()).searchParams;
    assert.equal(query.get('limit'), '50');
    const matched = ids.filter((id) => id.includes(query.get('search') || ''));
    const offset = Number(query.get('offset'));
    return route.fulfill({json: {total: matched.length, tasks: Object.fromEntries(matched.slice(offset, offset + 50).map((id) => [id, {status: 'ok', components: {task: {status: 'ok'}}}]))}});
  });
  await page.route(`**/v1/exports/${other}/tasks/111/datasets/*`, (route) => {
    const name = new URL(route.request().url()).pathname.split('/').at(-1);
    const contents = {task: {items: [{task_id: '111', task: {title: '已保存的任务标题'}}]}, metrics: {complete: true, values: {total_tokens: 42}}, artifacts: {files: Array.from({length: 795}, (_, i) => ({path: `src/f${i}.txt`, size: 30}))}};
    const files = name === 'artifacts' ? [{path: 'artifacts/manifest.json', role: 'initial-manifest', format: 'json-object.v1'}, ...Array.from({length: 795}, (_, i) => ({path: `artifacts/files/src/f${i}.txt`, role: 'artifact', format: 'bytes.v1'}))] : [{path: `${name}.json`, role: 'data', format: 'json-object.v1'}];
    return route.fulfill({json: {status: 'available', complete: true, files, documents: [{reference: files[0], content: contents[name]}], total: 1}});
  });
  await page.route(`**/v1/exports/${other}/tasks/111/file?*`, (route) => route.fulfill({body: '<script>window.unsafePreview=true</script>saved content', contentType: 'application/octet-stream'}));
  try {
    await page.locator('[data-view="exports"]').click();
    await page.waitForFunction((id) => [...document.querySelector('#export-history').options].some((o) => o.value === id), other);
    await page.locator('#export-history').selectOption(batch);
    for (let i = 0; i < 50 && !release; i++) await new Promise((resolve) => setTimeout(resolve, 10));
    assert.ok(release);
    await page.locator('#export-history').selectOption(other);
    await page.locator('#data-export-status').filter({hasText: '拉取完成'}).waitFor(); release();
    await page.waitForFunction(() => document.querySelectorAll('#export-task-rows tr[data-task-id]').length === 50);
    await page.locator('#export-next').click(); await page.locator('#export-page-info').filter({hasText: '第 2 / 3 页'}).waitFor();
    assert.equal(await page.locator('tr[data-task-id="111"]').count(), 0);
    await page.locator('#export-task-search').fill('111'); await page.waitForFunction(() => document.querySelectorAll('#export-task-rows tr[data-task-id]').length === 1);
    await page.locator('tr[data-task-id="111"] button').first().click();
    await page.getByRole('heading', {name: '已保存的任务标题'}).waitFor();
    assert.equal(await page.locator('.detail-source').textContent(), '已保存资料查看实时资料');
    assert.equal(requests.some((path) => path.startsWith('/v1/task-data/')), false);
    await page.locator('[data-detail="artifacts"]').click(); await page.getByRole('button', {name: '▸ src · 795 个文件'}).click();
    assert.equal(await page.locator('.detail-file').count(), 100);
    await page.getByLabel('搜索文件', {exact: true}).fill('f794.txt'); await page.getByRole('button', {name: '预览', exact: true}).click();
    await page.locator('.file-preview pre').filter({hasText: 'saved content'}).waitFor();
    assert.equal(await page.evaluate(() => window.unsafePreview), undefined);
    assert.equal(requests.some((path) => path.startsWith('/v1/task-data/')), false);
    await page.getByRole('button', {name: '关闭任务详情'}).click();
    await page.screenshot({path: '/private/tmp/task-reflux-paged-batch.png', fullPage: true});
    await page.reload(); await page.locator('#data-export-status').filter({hasText: '拉取完成'}).waitFor();
    assert.equal(await page.locator('#export-history').inputValue(), other);
    assert.equal(requests.some((path) => path.endsWith('/cancel')), false);
    assert.deepEqual(errors, []);
  } finally { release?.(); await page.close(); }
});
