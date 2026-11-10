// DOM checks: fixed choices must work without any option API requests.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {test} = require('node:test');
const {JSDOM} = require(process.argv[2] || 'jsdom');
const web = path.join(__dirname, '../src/flux_task_service/web');
function setup(handler = async () => ({task_ids: ['111'], count: 1, scanned_count: 1, has_more: false, next_cursor: null})) {
  const html = fs.readFileSync(path.join(web, 'index.html'), 'utf8').replace('<!--UI_CONFIG-->', '<script id="ui-config" type="application/json">{}</script>');
  const dom = new JSDOM(html, {url: 'http://localhost/', runScripts: 'outside-only'});
  const calls = [];
  dom.window.fetch = async (url, options = {}) => {
    if (url === 'health/ready') return {ok: true};
    assert.equal(url, 'v1/task-ids/query', 'fixed choices must not request options');
    const body = JSON.parse(options.body); calls.push(body);
    const data = await handler(body);
    return {ok: !data.error, status: data.error ? 422 : 200, json: async () => data};
  };
  dom.window.eval(fs.readFileSync(path.join(web, 'ui.js'), 'utf8'));
  return {dom, doc: dom.window.document, win: dom.window, calls};
}
function add(ui, field) {
  ui.doc.getElementById('add-condition').click();
  const row = Array.from(ui.doc.querySelectorAll('.condition-row')).at(-1);
  const select = row.querySelector('.field-select'); select.value = field;
  select.dispatchEvent(new ui.win.Event('change'));
  return row;
}
function check(row, value) {
  Array.from(row.querySelectorAll('input[type=checkbox]')).find((input) => input.value === value).click();
}
test('user shortcut focuses one inclusion row and submits deduplicated accounts', async () => {
  const ui = setup();
  try {
    const shortcut = ui.doc.getElementById('add-user-condition');
    shortcut.click();
    const row = Array.from(ui.doc.querySelectorAll('.condition-row')).at(-1);
    assert.equal(row.querySelector('.field-select').value, 'created_by');
    const input = row.querySelector('textarea');
    assert.equal(ui.doc.activeElement, input);
    input.value = 'alice，bob\nalice;carol';
    input.dispatchEvent(new ui.win.Event('input'));
    const filters = await submit(ui);
    assert.deepEqual(filters.filter_fields.find((field) => field.field_name === 'created_by').values, ['alice', 'bob', 'carol']);
    assert.ok(filters.filter_fields.some((field) => field.field_name === 'task_created_at'));
    shortcut.click();
    assert.equal(ui.doc.querySelectorAll('.condition-row').length, 2);
    assert.equal(ui.doc.activeElement, input);
    assert.equal(ui.doc.getElementById('copy').disabled, false, 'focusing an existing row does not invalidate results');
  } finally { ui.dom.window.close(); }
});

test('file progress remains visible before any task finishes and clears when stopped', async () => {
  const ui = setup();
  const job = {job_id: 'a'.repeat(32), status: 'running', task_count: 1,
    completed_count: 0, archive_ready: false, tasks: {'111': {status: 'running',
      components: {}, progress: {stage: 'artifacts', file_count: 795,
        completed_count: 721, downloaded_count: 720, failed_count: 1}}}};
  let release;
  const waitFor = async (condition) => {
    for (let i = 0; i < 100 && !condition(); i++) await new Promise((resolve) => setTimeout(resolve, 5));
    assert.ok(condition());
  };
  ui.win.fetch = async (url, options = {}) => {
    let data;
    if (url.endsWith('/query')) {
      ui.calls.push(JSON.parse(options.body));
      data = {task_ids: ['111'], count: 1, scanned_count: 1, has_more: false};
    } else if (url === 'v1/exports') data = job;
    else data = await new Promise((resolve) => { release = resolve; });
    return {ok: true, status: 200, json: async () => data};
  };
  try {
    await submit(ui);
    ui.doc.getElementById('data-export-start').click();
    await waitFor(() => release);
    assert.match(ui.doc.getElementById('data-export-status').textContent, /正在下载文件 721 \/ 795/);
    assert.equal(ui.doc.querySelector('.export-file-progress').textContent, '文件 721 / 795 · 1 个失败');
    assert.equal(ui.doc.getElementById('export-progress').value, 0);
    release({...job, status: 'cancelled', archive_ready: true});
    await waitFor(() => !ui.doc.getElementById('data-export-download').disabled);
    assert.equal(ui.doc.querySelector('.export-file-progress'), null);
    assert.equal(ui.doc.getElementById('data-export-status').textContent, '已取消');
  } finally { ui.dom.window.close(); }
});

test('request timeout is retryable, cancellation stays explicit and mutations are not repeated', async () => {
  const ui = setup(); let calls = 0;
  try {
    ui.win.eval(fs.readFileSync(path.join(web, 'api.js'), 'utf8'));
    ui.win.fetch = (url, options) => new Promise((resolve, reject) => {
      calls += 1;
      options.signal.addEventListener('abort', () => reject(new ui.win.DOMException('Aborted', 'AbortError')), {once: true});
    });
    await assert.rejects(ui.win.FluxAPI.request('v1/exports', {task_ids: ['111']}, {timeout: 5}), (error) => error.retryable === true && /超时/.test(error.message));
    assert.equal(calls, 1);
    const controller = new ui.win.AbortController();
    const pending = ui.win.FluxAPI.request('v1/exports/a/overview', undefined, {signal: controller.signal});
    controller.abort();
    await assert.rejects(pending, (error) => error.name === 'AbortError' && !error.retryable);
    assert.equal(calls, 2);
  } finally { ui.dom.window.close(); }
});

for (const scope of ['task', 'batch']) {
  test(`export ${scope} refetch updates the same job and prevents duplicate submissions`, async () => {
    const ui = setup();
    const sourceId = 'a'.repeat(32), childId = sourceId, retries = [];
    let release;
    const job = (id, ids) => ({job_id: id, status: 'partial', task_count: ids.length,
      completed_count: ids.length, archive_ready: true, components: ['task'],
      tasks: Object.fromEntries(ids.map((id) => [id, {status: 'partial', components: {task: {status: 'partial', warnings: ['test warning']}}}]))});
    const source = job(sourceId, ['111', '222']);
    const child = {...job(childId, ['111', '222']),
      request: {task_ids: ['111', '222']},
      retry_of: {job_id: sourceId, scope}, warnings: ['历史参数使用默认值']};
    const waitFor = async (condition) => {
      for (let i = 0; i < 100 && !condition(); i++) await new Promise((resolve) => setTimeout(resolve, 5));
      assert.ok(condition());
    };
    ui.win.fetch = async (url, options = {}) => {
      let data;
      if (url.endsWith('/query')) {
        ui.calls.push(JSON.parse(options.body));
        data = {task_ids: ['111', '222'], count: 2, scanned_count: 2, has_more: false, next_cursor: null};
      }
      else if (url.endsWith('/retry')) {
        retries.push({url, body: JSON.parse(options.body)});
        data = await new Promise((resolve) => { release = () => resolve(child); });
      } else data = retries.length ? child : source;
      return {ok: true, status: 200, json: async () => data};
    };
    try {
      await submit(ui);
      ui.doc.getElementById('data-export-start').click();
      await waitFor(() => !ui.doc.getElementById('data-export-refetch').disabled);
      const button = scope === 'task' ? ui.doc.querySelector('[aria-label="重新获取任务 111"]') : ui.doc.getElementById('data-export-refetch');
      assert.equal(button.hidden, false);
      button.click(); button.click();
      await waitFor(() => retries.length === 1);
      assert.equal(button.disabled, true);
      assert.equal(retries[0].url, `v1/exports/${sourceId}/${scope === 'task' ? 'tasks/111/retry' : 'retry'}`);
      assert.deepEqual(retries[0].body, {});
      release();
      await waitFor(() => ui.doc.getElementById('export-job-id').textContent.includes(childId) && !ui.doc.getElementById('data-export-refetch').disabled);
      assert.equal(ui.doc.getElementById('export-task-rows').children.length, 2);
      assert.match(ui.doc.getElementById('data-export-status').textContent, /历史参数使用默认值/);
      assert.equal(ui.doc.getElementById('data-export-download').disabled, false);
      assert.equal(ui.doc.getElementById('data-export-retry').hidden, true);
      ui.win.fetch = async () => ({ok: false, status: 429, json: async () => ({error: {message: 'busy'}})});
      ui.doc.getElementById('data-export-refetch').click();
      await waitFor(() => !ui.doc.getElementById('data-export-refetch').disabled);
      assert.ok(ui.doc.getElementById('export-job-id').textContent.includes(childId));
      assert.equal(ui.doc.getElementById('data-export-download').disabled, false);
      assert.match(ui.doc.getElementById('toast').textContent, /重新获取失败/);
    } finally { ui.dom.window.close(); }
  });
}

test('user shortcut preserves exclusions and user-only filtering works without a date row', async () => {
  const ui = setup();
  try {
    ui.doc.querySelector('[aria-label="删除这行筛选条件"]').click();
    const excluded = add(ui, 'created_by');
    const mode = excluded.querySelector('.mode-select');
    mode.value = 'exclude'; mode.dispatchEvent(new ui.win.Event('change'));
    const excludedInput = excluded.querySelector('textarea');
    excludedInput.value = 'bob'; excludedInput.dispatchEvent(new ui.win.Event('input'));
    ui.doc.getElementById('add-user-condition').click();
    const included = Array.from(ui.doc.querySelectorAll('.condition-row')).at(-1);
    const includedInput = included.querySelector('textarea');
    includedInput.value = 'alice carol'; includedInput.dispatchEvent(new ui.win.Event('input'));
    const filters = await submit(ui);
    assert.deepEqual(filters.filter_fields, [{field_name: 'created_by', query_type: 'in', values: ['alice', 'carol']}]);
    assert.deepEqual(filters.exclude_fields, [{field_name: 'created_by', query_type: 'in', values: ['bob']}]);
    ui.doc.getElementById('add-user-condition').click();
    assert.equal(ui.doc.querySelectorAll('.condition-row').length, 2);
    assert.equal(excluded.querySelector('.mode-select').value, 'exclude');
    ui.doc.getElementById('reset').click();
    assert.equal(ui.doc.querySelectorAll('.condition-row').length, 1);
    assert.equal(ui.doc.querySelector('.field-select').value, 'task_created_at');
  } finally { ui.dom.window.close(); }
});
function clear(row) {
  Array.from(row.querySelectorAll('button')).find((button) => button.textContent === '清空').click();
}
async function submit(ui) {
  ui.doc.getElementById('filter-form').dispatchEvent(new ui.win.Event('submit', {cancelable: true}));
  for (let i = 0; i < 50 && ui.doc.getElementById('query').disabled; i++) await new Promise((r) => setTimeout(r, 2));
  assert.equal(ui.doc.getElementById('query').disabled, false);
  return ui.calls.at(-1).filters;
}
test('fixed options are complete and independent of task data; multiselect submits mapped values', async () => {
  const ui = setup();
  try {
    for (const [field, count] of [['task_status', 8], ['work_mode', 4], ['task_is_completed', 2], ['level_1_category', 9], ['level_2_category', 24], ['level_3_category', 6]]) {
      const row = add(ui, field);
      assert.equal(row.querySelectorAll('input[type=checkbox]').length, count);
      assert.equal(row.querySelector('textarea'), null);
      const boxes = row.querySelectorAll('input[type=checkbox]');
      check(row, boxes[0].value); check(row, boxes[1].value);
    }
    assert.equal(ui.calls.length, 0);
    const filters = await submit(ui);
    const values = (field) => filters.filter_fields.find((f) => f.field_name === field).values;
    assert.deepEqual(values('task_status'), ['queueing', 'running']);
    assert.deepEqual(values('work_mode'), ['general', 'coding']);
    assert.deepEqual(values('level_1_category'), ['快速问答', '调研']);
    assert.deepEqual(values('level_2_category'), ['Flux 能力问答&操作', '信息查询']);
    assert.deepEqual(values('level_3_category'), ['需求开发', '代码重构']);
    assert.equal(ui.doc.getElementById('copy').disabled, false);
  } finally { ui.dom.window.close(); }
});
test('manual owners deduplicate immediately, exclusions preserve multi-values, placeholder is omitted', async () => {
  const ui = setup();
  try {
    const owner = add(ui, 'created_by');
    const input = owner.querySelector('textarea'); input.value = 'alice，bob\nalice;carol'; input.dispatchEvent(new ui.win.Event('input'));
    const status = add(ui, 'task_status'); check(status, 'input-required'); check(status, 'rolled-back');
    const mode = status.querySelector('.mode-select'); mode.value = 'exclude'; mode.dispatchEvent(new ui.win.Event('change'));
    add(ui, 'source_platform');
    const filters = await submit(ui);
    assert.deepEqual(filters.filter_fields.find((f) => f.field_name === 'created_by').values, ['alice', 'bob', 'carol']);
    assert.deepEqual(filters.exclude_fields[0].values, ['input-required', 'rolled-back']);
    assert.equal(JSON.stringify(filters).includes('source_platform'), false);
    owner.querySelector('[aria-label="移除bob"]').click();
    assert.equal(input.value, 'alice\ncarol');
    assert.equal(ui.doc.getElementById('copy').disabled, true);
    clear(owner); assert.equal(input.value, '');
  } finally { ui.dom.window.close(); }
});
test('search preserves selections, clear and switching field remove stale values, reset restores date only', async () => {
  const ui = setup();
  try {
    const row = add(ui, 'level_2_category'); check(row, 'code review'); check(row, '代码理解');
    const search = row.querySelector('input[type=search]'); search.value = 'GUI'; search.dispatchEvent(new ui.win.Event('input'));
    assert.equal(row.querySelectorAll('input[type=checkbox]').length, 1);
    let filters = await submit(ui);
    assert.deepEqual(filters.filter_fields.at(-1).values, ['code review', '代码理解']);
    clear(row); filters = await submit(ui); assert.equal(filters.filter_fields.length, 1);
    const field = row.querySelector('.field-select'); field.value = 'task_status'; field.dispatchEvent(new ui.win.Event('change'));
    assert.equal(row.querySelectorAll('input[type=checkbox]').length, 8);
    assert.equal(row.querySelectorAll('input:checked').length, 0);
    ui.doc.getElementById('reset').click();
    assert.equal(ui.doc.querySelectorAll('.condition-row').length, 1);
    assert.equal(ui.doc.getElementById('copy').disabled, true);
  } finally { ui.dom.window.close(); }
});
test('query failures remain visible and allow retry', async () => {
  let fail = true;
  const ui = setup(async () => fail ? {error: {code: 'FILTER_FIELD_UNAVAILABLE'}} : {task_ids: ['111'], count: 1, scanned_count: 1, has_more: false, next_cursor: null});
  try {
    await submit(ui); assert.equal(ui.doc.getElementById('query-error').hidden, false);
    assert.match(ui.doc.getElementById('query-error').textContent, /缺少工作模式/);
    fail = false; await submit(ui);
    assert.equal(ui.doc.getElementById('query-error').hidden, true);
    assert.equal(ui.doc.getElementById('result-count').textContent, '1');
  } finally { ui.dom.window.close(); }
});

test('pagination keeps empty pages navigable, caches previous pages and deduplicates loaded IDs', async () => {
  const ui = setup(async ({cursor}) => ({
    task_ids: cursor === null ? ['111', '111'] : cursor === 'c1' ? [] : ['111', '222'],
    count: cursor === 'c1' ? 0 : 2, scanned_count: 200,
    has_more: cursor !== 'c2', next_cursor: cursor === null ? 'c1' : cursor === 'c1' ? 'c2' : null,
  }));
  const clickNext = async () => {
    ui.doc.getElementById('next-page').click();
    for (let i = 0; i < 50 && ui.doc.getElementById('query').disabled; i++) await new Promise((r) => setTimeout(r, 2));
  };
  try {
    await submit(ui);
    assert.equal(ui.doc.getElementById('task-ids').value, '111');
    assert.match(ui.doc.getElementById('result-message').textContent, /仅复制或下载已加载/);
    await clickNext();
    assert.equal(ui.doc.getElementById('next-page').disabled, false);
    assert.equal(ui.doc.getElementById('empty-state').hidden, false);
    assert.equal(ui.doc.getElementById('result-count').textContent, '1');
    await clickNext();
    assert.equal(ui.doc.getElementById('next-page').disabled, true);
    assert.equal(ui.doc.getElementById('result-count').textContent, '2');
    assert.equal(ui.doc.getElementById('task-ids').value, '111\n222');
    assert.equal(ui.doc.getElementById('result-badge').textContent, '已加载全部');
    ui.doc.getElementById('previous-page').click();
    assert.match(ui.doc.getElementById('page-info').textContent, /第 2 页/);
    await clickNext();
    assert.equal(ui.calls.length, 3);
    assert.deepEqual(ui.calls.map((c) => c.cursor), [null, 'c1', 'c2']);
    assert.ok(ui.calls.every((c) => c.limit === 200));
    assert.deepEqual(ui.calls[0].filters, ui.calls[2].filters);
    const size = ui.doc.getElementById('page-size'); size.value = '50'; size.dispatchEvent(new ui.win.Event('change'));
    assert.equal(ui.doc.getElementById('previous-page').disabled, true);
    assert.equal(ui.doc.getElementById('copy').disabled, true);
    await submit(ui);
    assert.equal(ui.calls.at(-1).limit, 50); assert.equal(ui.calls.at(-1).cursor, null);
    assert.match(ui.doc.getElementById('page-info').textContent, /已加载 1 页/);
  } finally { ui.dom.window.close(); }
});

test('failed, repeated-cursor and cancelled pages preserve results and retry the same cursor', async () => {
  let behavior = 'failure';
  const ui = setup(async ({cursor}) => {
    if (cursor === null) return {task_ids: ['111'], scanned_count: 1, has_more: true, next_cursor: 'c1'};
    if (behavior === 'failure') return {error: {code: 'UPSTREAM_UNAVAILABLE'}};
    if (behavior === 'cycle') return {task_ids: ['222'], scanned_count: 1, has_more: true, next_cursor: 'c1'};
    if (behavior === 'cancel') { await new Promise((r) => setTimeout(r, 10)); }
    return {task_ids: ['222'], scanned_count: 1, has_more: false, next_cursor: null};
  });
  const next = async (cancel = false) => {
    ui.doc.getElementById('next-page').click();
    if (cancel) ui.doc.getElementById('cancel').click();
    for (let i = 0; i < 50 && ui.doc.getElementById('query').disabled; i++) await new Promise((r) => setTimeout(r, 2));
  };
  try {
    await submit(ui);
    for (const scenario of ['failure', 'cycle', 'cancel']) {
      behavior = scenario; await next(scenario === 'cancel');
      assert.equal(ui.doc.getElementById('task-ids').value, '111');
      assert.equal(ui.doc.getElementById('next-page').disabled, false);
      assert.match(ui.doc.getElementById('page-info').textContent, /已加载 1 页/);
    }
    behavior = 'success'; await next();
    assert.equal(ui.doc.getElementById('result-count').textContent, '2');
    assert.ok(ui.calls.slice(1).every((c) => c.cursor === 'c1'));
  } finally { ui.dom.window.close(); }
});
