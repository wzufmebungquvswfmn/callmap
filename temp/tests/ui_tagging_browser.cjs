// End-to-end browser checks against a local service with a seeded export batch.
const assert = require('node:assert/strict');
const {test, before, after} = require('node:test');
const {chromium} = require(process.argv[2] || 'playwright');
const base = process.env.FLUX_BROWSER_URL || 'http://127.0.0.1:18086';
const exportId = process.env.FLUX_TAGGING_EXPORT_ID || 'a'.repeat(32);
let browser;
before(async () => { browser = await chromium.launch({headless: true}); });
after(async () => { await browser?.close(); });
test('service: create, filter, inspect evidence, download and rerun priority policy', async () => {
  const page = await browser.newPage({viewport: {width: 1440, height: 1100}});
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  try {
    await page.goto(base);
    await page.locator('[data-view="tagging"]').click();
    await page.getByText('指定批次编号', {exact: true}).click();
    await page.locator('#tagging-export-id').fill(exportId);
    await page.locator('#tagging-start').click();
    await page.locator('#tagging-status').filter({hasText: '打标完成'}).waitFor();
    assert.equal(await page.locator('#tagging-rows tr').count(), 2);
    await page.locator('#tagging-filter').selectOption('priority_candidate');
    await page.waitForFunction(() => document.querySelectorAll('#tagging-rows tr').length === 1 && document.querySelector('#tagging-rows').textContent.includes('111'));
    await page.locator('#tagging-rows summary').click();
    await page.locator('#tagging-rows .tagging-detail').filter({hasText: '可进入缺陷提取'}).waitFor();
    const downloaded = page.waitForEvent('download');
    await page.locator('#tagging-selected').click();
    const download = await downloaded;
    assert.ok(download.suggestedFilename().endsWith('.json'));
    const stream = await download.createReadStream();
    const chunks = [];
    for await (const chunk of stream) chunks.push(chunk);
    assert.deepEqual(JSON.parse(Buffer.concat(chunks).toString()).map((r) => r.task_id), ['111', '222']);
    await page.screenshot({path: '/private/tmp/flux-tagging-service-desktop.png', fullPage: true});
    const previousId = await page.locator('#tagging-job-id').inputValue();
    await page.locator('#tagging-priority').check();
    await page.locator('#tagging-start').click();
    await page.waitForFunction((id) => document.querySelector('#tagging-job-id').value !== id, previousId);
    await page.locator('#tagging-status').filter({hasText: '打标完成'}).waitFor();
    assert.ok((await page.locator('#tagging-status').textContent()).includes('仅问题线索候选'));
    await page.locator('#tagging-filter').selectOption('ordinary_candidate');
    await page.waitForFunction(() => document.querySelector('#tagging-rows').textContent.includes('222'));
    assert.ok((await page.locator('#tagging-rows').textContent()).includes('排除'));
    assert.deepEqual(errors, []);
  } finally { await page.close(); }
});
test('mobile: service results remain usable without document horizontal overflow', async () => {
  const page = await browser.newPage({viewport: {width: 390, height: 844}});
  try {
    await page.goto(base);
    await page.locator('[data-view="tagging"]').click();
    await page.getByText('指定批次编号', {exact: true}).click();
    await page.locator('#tagging-export-id').fill(exportId);
    await page.locator('#tagging-start').click();
    await page.locator('#tagging-status').filter({hasText: '打标完成'}).waitFor();
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.locator('#tagging-title').scrollIntoViewIfNeeded();
    await page.screenshot({path: '/private/tmp/flux-tagging-service-mobile.png', fullPage: true});
  } finally { await page.close(); }
});
