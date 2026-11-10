// DOM integration checks for policy submission, filtering, evidence and downloads.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {test} = require('node:test');
const {JSDOM} = require(process.argv[2] || 'jsdom');
const web = path.join(__dirname, '../src/flux_task_service/web');
const exportId = 'a'.repeat(32), taggingId = 'b'.repeat(32);
const tick = () => new Promise((resolve) => setTimeout(resolve, 15));
function setup(handler) {
  const html = fs.readFileSync(path.join(web, 'index.html'), 'utf8').replace('<!--UI_CONFIG-->', '<script id="ui-config" type="application/json">{}</script>');
  const dom = new JSDOM(html, {url: 'http://localhost/', runScripts: 'outside-only'});
  const calls = [], downloaded = [];
  dom.window.URL.createObjectURL = () => 'blob:download';
  dom.window.URL.revokeObjectURL = () => {};
  dom.window.HTMLAnchorElement.prototype.click = function () { downloaded.push(this.download); };
  dom.window.fetch = async (url, options = {}) => {
    calls.push({url, options});
    const response = await handler(url, options);
    return {ok: !response.error, status: response.error ? 409 : 200,
      json: async () => response, blob: async () => new dom.window.Blob(['data'])};
  };
  dom.window.eval(fs.readFileSync(path.join(web, 'tagging.js'), 'utf8'));
  return {dom, doc: dom.window.document, win: dom.window, calls, downloaded};
}
const job = {tagging_id: taggingId, export_job_id: exportId, status: 'completed', completed_count: 2,
  task_count: 2, archive_ready: true, decision_counts: {keep: 1, hold: 1}, policy: {require_problem_signal: true}};
test('completed export fills scope, submits policy, filters, shows evidence, downloads with auth', async () => {
  const ui = setup(async (url) => {
    if (url.endsWith('/tagging')) return {tagging_id: taggingId};
    if (url === `v1/tagging/${taggingId}`) return job;
    if (url.includes('/results?')) return {total: 1, records: [{task_id: '111', candidate_level: 'priority_candidate', decision: 'keep', test_evidence_level: 'result_observed', test_types: ['unit'], reason_codes: ['TEST_RESULT_WITH_PROBLEM_SIGNAL']}]};
    if (url.endsWith('/tasks/111')) return {dynamic_tags: {review: {source: 'model', decision: 'hold', reason: '动态复核'}}, dynamic_tag_revision: 2, core: {runs: [{evidence: [{excerpt: '<script>window.injected=true</script>'}]}]}, admission: {selected_run_keys: [{run_id: 'r1'}]}, recorded_only: {metrics: [], repository: {repo_path: '', extraction: {
      category_labels: {user_specified: '用户指定'}, purpose_labels: {target_code: '目标代码'},
      repositories: [{name: 'org/repo', platform: 'github.com', categories: ['user_specified'], purposes: ['target_code'], evidence: [{category: 'user_specified', state: 'mentioned', path: 'conversation.json', pointer: '/steps/0', excerpt: '<img src=x onerror="window.injected=true">'}]}],
    }}}};
    return {};
  });
  try {
    ui.doc.getElementById('api-key').value = 'caller-key';
    assert.equal(ui.doc.getElementById('tagging-start').disabled, true);
    ui.doc.dispatchEvent(new ui.win.CustomEvent('flux-export-ready', {detail: {job_id: exportId}}));
    assert.equal(ui.doc.getElementById('tagging-start').disabled, false);
    ui.doc.getElementById('tagging-priority').checked = true;
    ui.doc.getElementById('tagging-start').click(); await tick();
    const post = ui.calls.find((call) => call.url.endsWith('/tagging'));
    assert.equal(JSON.parse(post.options.body).policy.require_problem_signal, true);
    assert.equal(post.options.headers.Authorization, 'Bearer caller-key');
    assert.equal(ui.doc.getElementById('tagging-results').hidden, false);
    const select = ui.doc.getElementById('tagging-filter'); select.value = 'priority_candidate';
    select.dispatchEvent(new ui.win.Event('change')); await tick();
    assert.ok(ui.calls.some((call) => call.url.includes('candidate_level=priority_candidate')));
    const detail = ui.doc.querySelector('#tagging-rows details'); detail.open = true;
    detail.dispatchEvent(new ui.win.Event('toggle')); await tick();
    assert.ok(detail.textContent.includes('<script>window.injected=true</script>'));
    assert.equal(ui.win.injected, undefined);
    assert.ok(detail.textContent.includes('关联仓库（来源与用途分类）'));
    assert.ok(detail.textContent.includes('org/repo · github.com · 用户指定'));
    assert.ok(detail.textContent.includes('用途：目标代码'));
    assert.ok(detail.textContent.includes('提及，待确认'));
    assert.ok(detail.textContent.includes('conversation.json/steps/0'));
    assert.equal(detail.querySelector('img'), null);
    assert.ok(detail.textContent.includes('人工复核：待复核'));
    assert.equal(detail.textContent.includes('查看原始标签 JSON'), false);
    assert.ok(detail.textContent.includes('动态复核'));
    ui.doc.getElementById('tagging-selected').click(); await tick();
    assert.ok(ui.downloaded[0].endsWith('.json'));
    assert.equal(ui.calls.at(-1).options.headers.Authorization, 'Bearer caller-key');
  } finally { ui.dom.window.close(); }
});
test('invalid config and invalid source do not create requests', async () => {
  const ui = setup(async () => ({}));
  try {
    ui.doc.getElementById('tagging-export-id').value = '../escape';
    ui.doc.getElementById('tagging-export-id').dispatchEvent(new ui.win.Event('input'));
    assert.equal(ui.doc.getElementById('tagging-start').disabled, true);
    ui.doc.dispatchEvent(new ui.win.CustomEvent('flux-export-ready', {detail: {job_id: exportId}}));
    ui.doc.getElementById('tagging-rules').value = '{invalid';
    ui.doc.getElementById('tagging-start').click(); await tick();
    assert.equal(ui.calls.length, 0);
    assert.ok(ui.doc.getElementById('tagging-status').textContent.includes('创建失败'));
  } finally { ui.dom.window.close(); }
});
test('history resume reports interrupted status without enabling downloads', async () => {
  const ui = setup(async () => ({...job, status: 'interrupted', archive_ready: false}));
  try {
    ui.doc.getElementById('tagging-job-id').value = taggingId;
    ui.doc.getElementById('tagging-resume').click(); await tick();
    assert.ok(ui.doc.getElementById('tagging-status').textContent.includes('服务中断'));
    assert.equal(ui.doc.getElementById('tagging-download').disabled, true);
  } finally { ui.dom.window.close(); }
});

test('cross-page output sends repeated task_id parameters to the original export and tagging batch', async () => {
  const records = Array.from({length: 51}, (_, index) => ({task_id: String(111 + index), candidate_level: 'ordinary_candidate', decision: index === 0 || index === 50 ? 'keep' : 'exclude', dynamic_tags: index === 0 ? {review: {decision: 'exclude'}} : {}}));
  const ui = setup(async (url) => {
    if (url === `v1/tagging/${taggingId}`) return job;
    if (url.includes('/results?')) { const params = new URL(url, 'http://localhost').searchParams, offset = Number(params.get('offset')), limit = Number(params.get('limit')); return {total: records.length, records: records.slice(offset, offset + limit)}; }
    return {};
  });
  try {
    ui.doc.getElementById('tagging-job-id').value = taggingId; ui.doc.getElementById('tagging-resume').click(); await tick();
    ui.doc.querySelector('input[aria-label="选择候选任务 111"]').click();
    ui.doc.getElementById('tagging-next').click(); await tick();
    ui.doc.querySelector('input[aria-label="选择候选任务 161"]').click();
    assert.match(ui.doc.getElementById('tagging-selection-count').textContent, /已选 2/);
    // Changing the source input must not change the original export of this result.
    ui.doc.getElementById('tagging-export-id').value = 'c'.repeat(32);
    ui.doc.getElementById('tagging-download-source').click(); await tick();
    ui.doc.getElementById('tagging-download-subset').click(); await tick();
    const urls = ui.calls.filter((call) => call.url.includes('/download?')).map((call) => new URL(call.url, 'http://localhost'));
    assert.deepEqual(urls.map((url) => url.searchParams.getAll('task_id')), [['111', '161'], ['111', '161']]);
    assert.equal(urls[0].pathname, `/v1/exports/${exportId}/download`);
    assert.equal(urls[1].pathname, `/v1/tagging/${taggingId}/download`);
    ui.doc.getElementById('tagging-select-kept').click(); await tick();
    assert.match(ui.doc.getElementById('tagging-selection-count').textContent, /已选 1/);
    ui.doc.getElementById('tagging-download-subset').click(); await tick();
    assert.deepEqual(new URL(ui.calls.at(-1).url, 'http://localhost').searchParams.getAll('task_id'), ['161']);
    ui.doc.getElementById('tagging-clear-selection').click();
    const before = ui.calls.length; ui.doc.getElementById('tagging-download-subset').click(); await tick();
    assert.equal(ui.calls.length, before, 'empty selection never becomes an unfiltered download');
    ui.doc.getElementById('tagging-job-id').value = 'd'.repeat(32); ui.doc.getElementById('tagging-resume').click(); await tick();
    assert.match(ui.doc.getElementById('tagging-selection-count').textContent, /已选 0/);
  } finally { ui.dom.window.close(); }
});
