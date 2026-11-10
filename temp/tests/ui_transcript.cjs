const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {test} = require('node:test');
const {JSDOM} = require(process.argv[2] || 'jsdom');
const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
test('transcript modes ignore stale responses, escape content, preserve edits and export the displayed mode', async () => {
  const dom = new JSDOM('<input id="review" value="unsaved review">', {url: 'http://localhost', runScripts: 'outside-only'});
  const {window: win} = dom, calls = [], downloads = [];
  win.FluxAPI = {request: (url, body, options) => new Promise((resolve, reject) => calls.push({url, options, resolve, reject})), save: (blob, name) => downloads.push(name)};
  win.eval(fs.readFileSync(path.join(__dirname, '../src/flux_task_service/web/transcript.js'), 'utf8'));
  const preview = win.FluxTranscript.create('a'.repeat(32), '111'); win.document.body.append(preview.element);
  const select = preview.element.querySelector('select'), text = preview.element.querySelector('pre');
  const button = (label) => [...preview.element.querySelectorAll('button')].find((button) => button.textContent === label);
  try {
    const first = preview.load(); assert.match(calls[0].url, /mode=assistant$/);
    select.value = 'user'; select.dispatchEvent(new win.Event('change'));
    assert.equal(calls[0].options.signal.aborted, true);
    calls[1].resolve('<script>window.injected=true</script>user'); await tick();
    calls[0].resolve('old assistant'); await first;
    assert.match(text.textContent, /user$/); assert.equal(text.querySelector('script'), null); assert.equal(win.injected, undefined);
    assert.equal(win.document.getElementById('review').value, 'unsaved review');
    button('下载 TXT').click(); assert.equal(downloads[0], 'task_111_user.txt');
    select.value = 'all'; select.dispatchEvent(new win.Event('change'));
    assert.equal(button('下载 TXT').disabled, true); calls[2].reject(new Error('not available')); await tick();
    assert.equal(button('重试').hidden, false); assert.equal(text.textContent, '');
    button('重试').click(); calls[3].resolve(''); await tick();
    assert.match(preview.element.textContent, /没有可显示/); assert.equal(button('下载 TXT').disabled, true);
    const last = preview.load(); preview.close(); calls[4].resolve('must not appear'); await last;
    assert.equal(text.textContent, '');
  } finally { preview.close(); dom.window.close(); }
});
