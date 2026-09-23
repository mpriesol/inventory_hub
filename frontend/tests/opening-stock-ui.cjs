/* Synthetic APIs only: no physical stock or live Hub/Upgates writes. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'https://hub.example.test/stock/opening' });
global.window = dom.window; global.document = dom.window.document;
Object.defineProperty(global, 'navigator', { value: dom.window.navigator, configurable: true });
global.HTMLElement = dom.window.HTMLElement; global.IS_REACT_ACT_ENVIRONMENT = true;
for (const ext of ['.ts', '.tsx']) require.extensions[ext] = (module, filename) => {
  module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8').replace(/import\.meta\.env/g, '{}'), {
    compilerOptions: { jsx: ts.JsxEmit.React, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, esModuleInterop: true },
  }).outputText, filename);
};
require.extensions['.css'] = () => {};
const i18n = require('../src/i18n/index.ts').default;
const React = require('react'); const { act } = React;
const { createRoot } = require('react-dom/client');
const { MemoryRouter } = require('react-router-dom');
const { OpeningStockPage } = require('../src/pages/OpeningStockPage.tsx');
const { unlockHub } = require('../src/api/access.ts');
const root = createRoot(document.getElementById('root'));
const tick = () => new Promise(resolve => setTimeout(resolve, 5));
const t = key => i18n.t(`openingStock.${key}`);
const button = key => [...document.querySelectorAll('button')].find(element => element.textContent === t(key));
const field = key => [...document.querySelectorAll('label')].find(element => element.textContent.startsWith(t(key))).querySelector('input,select,textarea');
const checks = () => [...document.querySelectorAll('input[type="checkbox"]')];
async function click(element) { assert(element, 'Element exists'); await act(async () => { element.click(); await tick(); }); }
async function input(element, value) {
  await act(async () => {
    const prototype = element.tagName === 'SELECT' ? dom.window.HTMLSelectElement.prototype : element.tagName === 'TEXTAREA' ? dom.window.HTMLTextAreaElement.prototype : dom.window.HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(prototype, 'value').set.call(element, value);
    element.dispatchEvent(new dom.window.Event(element.tagName === 'SELECT' ? 'change' : 'input', { bubbles: true }));
    await tick();
  });
}
const options = { warehouses: [{ id: 7, code: 'count-room', name: 'Count room' }], limits: { max_bytes: 1048576, max_rows: 5000, max_quantity: '999999999', max_unit_cost: '99999999.9999' }, unit: 'ks', currency: 'EUR', price_basis: 'ex_vat', expires_minutes: 30 };
const makeBatch = (id, count = 1) => ({ id, status: 'prepared', preview_hash: 'f'.repeat(64), warehouse: options.warehouses[0], source_reference: 'COUNT-2026', operator_name: 'Test operator',
  counted_at: '2026-09-23T09:30:27Z', created_at: '2026-09-23T10:00:00Z', expires_at: '2100-01-01T00:00:00Z', completed_at: null,
  unit: 'ks', currency: 'EUR', price_basis: 'ex_vat', summary: { lines: count, quantity: count === 1 ? '3.000' : String(count), total_value: count === 1 ? '270002700.0003' : '0.0201' }, warnings: [], result: null,
  lines: Array.from({ length: count }, (_, index) => ({ line_number: index + 2, product_id: index + 1, sku: index === 0 ? 'TEST;"SKU' : `TEST-${index}`, name: `Fixture product ${index}`, quantity: count === 1 ? '3.000' : '1.000', unit_cost: count === 1 ? '90000900.0001' : '0.0001', value: count === 1 ? '270002700.0003' : '0.0001', unit: 'ks', warnings: [] })),
});
const resultFor = batch => ({ batch_id: batch.id, completed_at: '2026-09-23T10:05:00Z', movements_created: batch.lines.length, summary: batch.summary,
  lines: batch.lines.map(line => ({ ...line, movement_id: line.product_id + 100 })) });
const complete = batch => ({ ...batch, status: 'completed', completed_at: '2026-09-23T10:05:00Z', result: resultFor(batch) });
const calls = []; let stored; let previewMode = 'valid'; let previewCount = 1; let pendingPreview;
let finalizeMode = 'success'; let rejectFinalize;
global.fetch = async (path, init = {}) => {
  const body = init.body ? JSON.parse(init.body) : undefined;
  calls.push({ path, ...init, body });
  assert.equal(init.cache, 'no-store');
  assert(init.headers.Authorization.startsWith('Bearer synthetic-'));
  if (path.endsWith('/options')) return { ok: true, json: async () => options };
  if (path.endsWith('/batches')) return { ok: true, json: async () => ({ batches: stored ? [stored] : [], limit: 20 }) };
  if (path.endsWith('/preview')) {
    assert.equal(init.method, 'POST');
    assert.match(body.request_id, /^[a-f0-9-]{36}$/);
    assert.equal(body.warehouse_code, 'count-room');
    if (previewMode === 'invalid') return { ok: true, json: async () => ({ ready: false, errors: [{ row: 2, code: 'missing_unit_cost', field: 'unit_cost' }], warnings: [], batch: null }) };
    stored = makeBatch(body.request_id, previewCount);
    if (previewMode === 'pending') return new Promise(resolve => { pendingPreview = batch => resolve({ ok: true, json: async () => ({ ready: true, errors: [], warnings: [], batch }) }); });
    return { ok: true, json: async () => ({ ready: true, errors: [], warnings: [], batch: stored }) };
  }
  if (path.endsWith('/finalize')) {
    assert.equal(init.method, 'POST');
    assert.deepEqual(body, { preview_hash: stored.preview_hash, confirmed: true, receipts_reconciled: true });
    if (finalizeMode === 'pending') return new Promise((resolve, reject) => { rejectFinalize = () => reject(new TypeError('Synthetic connection lost')); });
    if (finalizeMode === 'fail') throw new TypeError('Synthetic connection lost');
    stored = complete(stored);
    return { ok: true, json: async () => stored.result };
  }
  assert.equal(init.method, 'GET', 'Recovery only reads a batch');
  assert.equal(path, `/api/stock/opening/${stored.id}`);
  return { ok: true, json: async () => stored };
};
const writes = () => calls.filter(call => call.path.endsWith('/finalize'));
async function fillForm() {
  await input(field('warehouse'), 'count-room'); await input(field('source'), 'COUNT-2026');
  await input(field('operator'), 'Test operator'); await input(field('countedAt'), '2026-09-23T09:30:27');
  await input(field('csv'), 'sku;quantity;unit_cost;unit\nTEST;3;90000900.0001;ks');
}
async function confirmBoth() { for (const checkbox of checks()) if (!checkbox.checked) await click(checkbox); }

(async () => {
  unlockHub('');
  await act(async () => { root.render(React.createElement(MemoryRouter, null, React.createElement(OpeningStockPage))); await tick(); });
  assert.equal(calls.length, 0);
  await input(document.querySelector('input[type="password"]'), 'synthetic-opening-token'); await click(button('unlock'));
  assert.equal(calls.length, 0, 'Unlock does not fetch or write');
  assert(button('preview').disabled);
  await click(button('loadWarehouses'));
  assert.equal(field('warehouse').value, '', 'Warehouse is explicitly selected, never invented');
  await fillForm();
  previewMode = 'invalid'; await click(button('preview'));
  assert(document.querySelector('[role="alert"]').textContent.includes('unit_cost'));
  assert.equal(button('finalize'), undefined, 'Invalid source cannot be posted');
  previewMode = 'valid'; await input(field('csv'), field('csv').value + '\n'); await click(button('preview'));
  assert.equal(writes().length, 0, 'Preview creates no stock posting');
  assert(document.body.textContent.includes('270002700.0003 EUR'), 'Server decimal amount is never converted to a JS number');
  assert(document.body.textContent.includes('90000900.0001 EUR'));
  assert(button('finalize').disabled);
  await click(checks()[0]); assert(button('finalize').disabled, 'Both confirmations are required');
  await click(checks()[1]); assert(!button('finalize').disabled);
  await input(field('source'), 'COUNT-CHANGED');
  assert.equal(button('finalize'), undefined, 'Changing any form field invalidates frozen preview and confirmations');
  assert.equal(document.querySelectorAll('tbody tr').length, 0);

  previewMode = 'pending'; await click(button('preview'));
  const stale = pendingPreview, oldBatch = stored, oldRequest = calls.at(-1);
  await input(field('csv'), 'sku;quantity;unit_cost;unit\nCHANGED;1;2;ks');
  assert(oldRequest.signal.aborted);
  await act(async () => { stale(oldBatch); await tick(); });
  assert.equal(button('finalize'), undefined, 'Late preview does not restore obsolete form data');

  let releaseFile;
  const fileBuffer = new Promise(resolve => { releaseFile = resolve; });
  const fileInput = document.querySelector('input[type="file"]');
  await act(async () => {
    Object.defineProperty(fileInput, 'files', { configurable: true, value: [{ size: 50, arrayBuffer: () => fileBuffer }] });
    fileInput.dispatchEvent(new dom.window.Event('change', { bubbles: true })); await tick();
  });
  assert(button('preview').disabled && button('recover').disabled, 'Pending UTF-8 file read prevents preview and recovery races');
  await act(async () => { releaseFile(new TextEncoder().encode('sku;quantity;unit_cost;unit\nFROM-FILE;1;2;ks').buffer); await tick(); });
  assert(field('csv').value.includes('FROM-FILE'));
  previewMode = 'valid'; await click(button('preview'));
  const originalId = stored.id;
  await click(button('preview'));
  assert.equal(stored.id, originalId, 'Unchanged preview retries preserve the same request UUID');
  await confirmBoth(); finalizeMode = 'pending';
  const finalizeButton = button('finalize');
  await act(async () => { finalizeButton.click(); finalizeButton.click(); await tick(); });
  assert.equal(writes().length, 1, 'Two immediate clicks produce one finalize request');
  assert(field('csv').matches(':disabled'), 'Source cannot change during posting');
  await act(async () => { rejectFinalize(); await tick(); });
  assert(document.body.textContent.includes(t('uncertain')));
  assert.equal(button('finalize'), undefined, 'Connection loss cannot trigger another posting');
  assert(button('newBatch').disabled);
  stored = complete(stored); await click(button('recover'));
  assert(document.body.textContent.includes(t('completedHelp')));
  assert.equal(writes().length, 1, 'GET recovery of completed batch never repeats its posting');
  assert.equal(checks().length, 0);

  await click(button('newBatch')); await fillForm(); await click(button('preview')); await confirmBoth();
  finalizeMode = 'fail'; await click(button('finalize'));
  const retryId = stored.id;
  await click(button('recover'));
  assert.equal(field('csv').value.split('\n')[1], '"TEST;""SKU";3.000;90000900.0001;ks', 'Recovered CSV quotes semicolons and quotes in canonical SKU');
  assert.equal(new Date(field('countedAt').value).getSeconds(), 27, 'Recovered physical count time preserves seconds');
  assert(checks().every(checkbox => !checkbox.checked), 'Recovery of prepared batch requires fresh confirmations');
  assert(button('finalize').disabled);
  await confirmBoth(); finalizeMode = 'success'; await click(button('finalize'));
  assert.equal(stored.id, retryId);
  assert.equal(writes().length, 3, 'Only an explicit retry after GET and renewed confirmation posts again');
  assert(document.body.textContent.includes(t('completedHelp')));

  await act(async () => { unlockHub('synthetic-new-token'); await tick(); });
  assert.equal(document.querySelectorAll('tbody tr').length, 0, 'Credential changes clear the batch and source data');
  assert.equal(field('csv').value, 'sku;quantity;unit_cost;unit\n');
  const beforeRecent = calls.length; await click(button('loadRecent'));
  assert.equal(calls.length, beforeRecent + 1);
  await click(button('openBatch')); assert(document.body.textContent.includes(t('completedHelp')));
  assert.equal(writes().length, 3, 'Recent batch recovery after credential change is read-only');

  await click(button('newBatch')); await click(button('loadWarehouses')); await fillForm();
  previewCount = 201; await click(button('preview'));
  assert.equal(document.querySelectorAll('tbody tr').length, 100, 'Large previews render at most 100 rows at once');
  await click(button('next')); assert.equal(document.querySelectorAll('tbody tr').length, 100);
  await click(button('next')); assert.equal(document.querySelectorAll('tbody tr').length, 1);
  assert(button('next').disabled);
  await input(field('source'), 'PENDING-TOKEN'); previewMode = 'pending'; await click(button('preview'));
  const staleToken = pendingPreview, tokenBatch = stored;
  await act(async () => { unlockHub(''); await tick(); staleToken(tokenBatch); await tick(); });
  assert(document.querySelector('input[type="password"]'));
  assert.equal(document.querySelectorAll('tbody tr').length, 0);
  assert(!document.body.innerHTML.includes('synthetic-opening-token'));
  assert.equal(dom.window.localStorage.length, 0); assert.equal(dom.window.sessionStorage.length, 0);
  assert.equal(writes().length, 3);
  await act(async () => { root.unmount(); await tick(); });
  console.log('Opening stock UI behavior checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
