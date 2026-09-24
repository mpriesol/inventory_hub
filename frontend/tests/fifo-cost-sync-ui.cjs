/* Synthetic purchase-cost controls. No live shop calls. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'https://hub.example.test/settings/purchase-costs' });
global.window = dom.window; global.document = dom.window.document;
Object.defineProperty(global, 'navigator', { value: dom.window.navigator, configurable: true });
global.HTMLElement = dom.window.HTMLElement; global.IS_REACT_ACT_ENVIRONMENT = true;
for (const ext of ['.ts', '.tsx']) require.extensions[ext] = (module, filename) => module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8').replace(/import\.meta\.env/g, '{}'), { compilerOptions: { jsx: ts.JsxEmit.React, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, esModuleInterop: true } }).outputText, filename);
require.extensions['.css'] = () => {};
require('../src/i18n/index.ts');
const React = require('react'), { act } = React;
const { createRoot } = require('react-dom/client');
const { MemoryRouter } = require('react-router-dom');
const { FifoCostSyncPage } = require('../src/pages/FifoCostSyncPage.tsx');
const { unlockHub } = require('../src/api/access.ts');
const root = createRoot(document.getElementById('root'));
const tick = () => new Promise(resolve => setTimeout(resolve, 3));
const element = id => document.querySelector(`[data-testid="${id}"]`);
const required = id => { const value = element(id); assert(value, `${id} exists`); return value; };
async function click(id) { await act(async () => { required(id).click(); await tick(); }); }
async function input(id, value) { await act(async () => { const target = required(id); const proto = target.tagName === 'SELECT' ? dom.window.HTMLSelectElement.prototype : dom.window.HTMLInputElement.prototype; Object.getOwnPropertyDescriptor(proto, 'value').set.call(target, value); target.dispatchEvent(new dom.window.Event(target.tagName === 'SELECT' ? 'change' : 'input', { bubbles: true })); await tick(); }); }
const clone = value => JSON.parse(JSON.stringify(value));
const reply = value => ({ ok: true, json: async () => clone(value) });
const base = '/api/fifo-cost-sync';
let warehouse = { warehouse_code: 'main', revision: 1, interval_seconds: 300, batch_size: 20 };
const blank = () => ({ revision: 0, warehouse_code: 'main', enabled: false, product_cost_enabled: false, order_cost_enabled: false, interval_seconds: null, batch_size: null, orders_since: null, next_run_at: null, retry_after_at: null, last_completed_at: null, last_error: null, scan_active: false });
const shops = { biketrek: blank(), xtrek: blank() };
let serverWrite = false, records = [], optionsMode = 'ok', saveMode = 'ok', previewMode = 'ok', sendMode = 'ok';
let resolveOptions, rejectSave, rejectSend;
const options = shop => ({ shop: { code: shop, name: shop }, warehouse: { code: 'main', name: 'Main warehouse' }, warehouses: [{ code: 'main', name: 'Main warehouse' }, { code: 'secondary', name: 'Other warehouse' }], warehouse_settings: warehouse, settings: shops[shop], effective: { interval_seconds: shops[shop].interval_seconds ?? warehouse.interval_seconds, batch_size: shops[shop].batch_size ?? warehouse.batch_size }, blockers: [], server_write_enabled: serverWrite, publications: records.slice(0, 10) });
const preview = () => ({ id: '33333333-3333-4333-8333-333333333333', kind: 'order', status: 'prepared', subject: 'OLD-3', created_at: new Date().toISOString(), expires_at: new Date(Date.now() + 1800000).toISOString(), attempt_started_at: null, verified_at: null, resolution: null, before: { values: { 'line-1': '10' } }, after: null, error: null,
  remote_costs: { prices_with_vat_yn: true, lines: [{ line_key: 'line-1', code: 'FIFO-SKU', before: '12.3', desired: '24.6' }] },
  source: { kind: 'order', order_number: 'OLD-3', currency: 'EUR', vat_included: false, total_cost: '60', lines: [{ line_key: 'line-1', product_id: 42, sku: 'FIFO-SKU', quantity: '3', unit_cost: '20', total_cost: '60', allocations: [10, 20, 30].map((cost, i) => ({ id: i + 1, layer_id: i + 1, quantity: '1', unit_cost_current: String(cost), total_cost_current: String(cost), unit_cost_at_issue: String(cost), total_cost_at_issue: String(cost) })) }] } });
const calls = [];
global.fetch = async (path, init = {}) => {
  const body = init.body ? JSON.parse(init.body) : undefined; calls.push({ path, ...init, body });
  assert.equal(init.cache, 'no-store'); assert(init.headers.Authorization.startsWith('Bearer synthetic-')); assert(!path.includes('synthetic-'));
  const url = new URL(path, dom.window.location.origin);
  if (url.pathname === `${base}/options`) {
    assert.equal(init.method, 'GET'); const value = clone(options(url.searchParams.get('shop_code')));
    if (optionsMode === 'pending') return new Promise(resolve => { resolveOptions = () => resolve(reply(value)); });
    return reply(value);
  }
  if (url.pathname === `${base}/warehouse`) { assert.equal(body.expected_revision, warehouse.revision); assert(body.confirmed); warehouse = { ...body, revision: warehouse.revision + 1 }; return reply(warehouse); }
  if (url.pathname === `${base}/configure`) {
    assert.equal(body.expected_revision, shops[body.shop_code].revision); assert.equal(body.confirmed, true); assert.equal(body.orders_since, undefined, 'UI cannot authorize a historical bulk window');
    shops[body.shop_code] = { ...shops[body.shop_code], ...body, revision: body.expected_revision + 1, orders_since: body.order_cost_enabled ? new Date().toISOString() : null };
    if (saveMode === 'pending') return new Promise((resolve, reject) => { rejectSave = () => reject(new TypeError('Synthetic lost settings response')); });
    return reply(options(body.shop_code));
  }
  if (url.pathname === `${base}/run`) { assert.deepEqual(body, { shop_code: 'xtrek', confirmed: true }); shops.xtrek.scan_active = true; return reply(options('xtrek')); }
  if (url.pathname === `${base}/orders/preview`) { assert.match(body.request_id, /^[0-9a-f-]{36}$/); assert.deepEqual({ ...body, request_id: undefined }, { shop_code: 'xtrek', order_number: 'OLD-3', confirmed: true, request_id: undefined }); if (previewMode === 'malformed') return reply({}); const value = { ...preview(), id: body.request_id }; if (previewMode === 'expired') value.expires_at = new Date(Date.now() - 60000).toISOString(); records = previewMode === 'lost-missing' ? [] : [value]; if (previewMode.startsWith('lost')) throw new TypeError('Synthetic lost preview response'); return reply(value); }
  if (url.pathname === `${base}/history`) { const offset = Number(url.searchParams.get('offset') || 0), limit = Number(url.searchParams.get('limit') || 50); return reply({ items: records.slice(offset, offset + limit), total: records.length, offset, limit }); }
  const found = url.pathname.match(/\/publications\/([^/]+)(?:\/(send|resolve))?$/);
  if (found) {
    const value = records.find(item => item.id === found[1]); if (!value) return { ok: false, json: async () => ({ detail: { code: 'fifo_cost_publication_not_found' } }) };
    if (!found[2]) { assert.equal(init.method, 'GET'); return reply(value); }
    if (found[2] === 'send') { assert.deepEqual(body, { confirmed: true }); value.status = 'queued'; if (sendMode === 'pending') return new Promise((resolve, reject) => { rejectSend = () => reject(new TypeError('Synthetic lost send response')); }); return reply(value); }
    if (found[2] === 'resolve') { assert.equal(body.confirmed, true); assert.equal(body.original_request_settled, true); assert.equal(body.note, 'Confirmed request completion'); value.status = 'verified'; value.verified_at = new Date().toISOString(); return reply(value); }
  }
  throw new Error('Unexpected endpoint: ' + path);
};
const writes = () => calls.filter(call => call.method !== 'GET');
(async () => {
  try {
    unlockHub('');
    await act(async () => { root.render(React.createElement(MemoryRouter, { initialEntries: ['/settings/purchase-costs?shop=xtrek'] }, React.createElement(FifoCostSyncPage))); await tick(); });
    assert.equal(calls.length, 0);
    await input('cost-token', 'synthetic-cost-token'); await click('unlock-cost-sync'); assert.equal(calls.length, 0, 'Unlock does not start external work');
    await click('load-cost-settings'); assert.equal(required('cost-shop').value, 'xtrek'); assert(required('run-cost-sync').disabled); assert(!required('cost-products').checked && !required('cost-orders').checked && !required('cost-enabled').checked);
    assert(required('cost-shop-interval_seconds').disabled && required('cost-inherit-interval_seconds').checked);
    await input('cost-warehouse-interval_seconds', '59'); assert(required('save-cost-warehouse').disabled);
    await input('cost-warehouse-interval_seconds', '600'); await click('save-cost-warehouse'); assert.equal(writes().at(-1).body.expected_revision, 1); assert(required('save-cost-settings').disabled);
    await click('load-cost-settings'); assert.equal(required('cost-shop-interval_seconds').value, '600');
    await click('cost-products'); await click('cost-orders'); await click('cost-inherit-interval_seconds'); await input('cost-shop-interval_seconds', '900');
    saveMode = 'pending'; const beforeSave = writes().length;
    await act(async () => { required('save-cost-settings').click(); required('save-cost-settings').click(); await tick(); }); assert.equal(writes().length, beforeSave + 1); assert(required('cost-shop').disabled);
    assert.deepEqual(writes().at(-1).body, { shop_code: 'xtrek', warehouse_code: 'main', expected_revision: 0, enabled: false, product_cost_enabled: true, order_cost_enabled: true, interval_seconds: 900, batch_size: null, confirmed: true });
    await act(async () => { rejectSave(); await tick(); }); assert(required('run-cost-sync').disabled && required('save-cost-settings').disabled);
    await click('load-cost-history'); assert(required('save-cost-settings').disabled, 'Unrelated history read cannot recover a lost settings save');
    saveMode = 'ok'; await click('load-cost-settings'); assert(required('cost-products').checked && required('cost-orders').checked); assert(required('run-cost-sync').disabled, 'Server gate applies even with saved flags');
    shops.xtrek.retry_after_at = new Date(Date.now() + 60000).toISOString(); await click('load-cost-settings'); assert(element('cost-retry-after'), 'A future server cooldown is visible');
    shops.xtrek.retry_after_at = new Date(Date.now() - 1000).toISOString(); await click('load-cost-settings'); assert(!element('cost-retry-after'), 'Expired cooldown is not shown as active');
    shops.xtrek.retry_after_at = null;
    serverWrite = true; await click('load-cost-settings'); assert(!required('run-cost-sync').disabled, 'Manual run works without automatic scheduling');
    await click('run-cost-sync'); assert(required('run-cost-sync').disabled); assert(shops.xtrek.scan_active);
    shops.xtrek.scan_active = false; await click('load-cost-settings');
    await input('cost-order-number', 'OLD-3'); previewMode = 'lost'; const beforePreview = writes().length;
    await act(async () => { required('preview-order-cost').click(); required('preview-order-cost').click(); await tick(); }); assert.equal(writes().length, beforePreview + 1, 'Double-click retains the single original preview request');
    const lostPreviewId = writes().at(-1).body.request_id, afterPreview = writes().length;
    assert(required('preview-order-cost').disabled && element('cost-preview-recovery'));
    await click('recover-cost-preview'); assert(calls.at(-1).path.endsWith(`/publications/${lostPreviewId}`));
    assert.equal(writes().length, afterPreview, 'Recovery reads the exact retained UUID without replaying preview');
    assert(!element('cost-preview-recovery') && !required('preview-order-cost').disabled);
    previewMode = 'lost-missing'; await click('preview-order-cost'); const absentId = writes().at(-1).body.request_id;
    await click('recover-cost-preview'); assert(calls.at(-1).path.endsWith(`/publications/${absentId}`)); assert(element('cost-preview-recovery') && required('preview-order-cost').disabled, '404 retains the request ID and never silently creates a second preview');
    previewMode = 'ok'; await click('retry-cost-preview'); assert.equal(writes().at(-1).body.request_id, absentId, 'Explicit preview retry reuses the original UUID'); assert(!element('cost-preview-recovery'));
    assert(required('send-order-cost').disabled);
    assert(required('cost-publication-detail').textContent.includes('FIFO-SKU')); assert.equal(required('cost-publication-detail').querySelectorAll('tbody tr').length, 2); assert(required('cost-remote-values').textContent.includes('24.6'), 'Remote VAT-inclusive value shown separately from net FIFO cost');
    const cells = required('cost-publication-detail').querySelectorAll('tbody tr td'); assert.equal(cells[1].textContent, '3'); assert.equal(cells[2].textContent, '20'); assert.equal(cells[3].textContent, '60'); assert(cells[4].textContent.includes('1 × 10 = 10'));
    await click('confirm-order-cost'); assert(!required('send-order-cost').disabled);
    await input('cost-order-number', 'DIFFERENT'); assert(!element('cost-publication-detail'), 'Changing order clears frozen preview and confirmation');
    await input('cost-order-number', 'OLD-3'); await click('preview-order-cost'); await click('confirm-order-cost');
    sendMode = 'pending'; const beforeSend = writes().length;
    await act(async () => { required('send-order-cost').click(); required('send-order-cost').click(); await tick(); }); assert.equal(writes().length, beforeSend + 1);
    await act(async () => { rejectSend(); await tick(); }); assert(required('send-order-cost').disabled); await tick(); assert.equal(writes().length, beforeSend + 1, 'Lost response does not resend');
    records[0].status = 'uncertain'; await click('refresh-cost-publication'); assert(required('resolve-cost-publication').disabled, 'Exact refresh still requires explicit request-settled confirmation');
    assert(!element('send-order-cost'));
    await click('cost-request-settled'); assert(required('resolve-cost-publication').disabled, 'Recovery requires evidence note');
    await input('cost-recovery-note', 'Confirmed request completion'); assert(!required('resolve-cost-publication').disabled, 'Successful exact publication GET recovers the lost send without reloading unrelated settings'); await click('resolve-cost-publication'); assert(!element('resolve-cost-publication')); assert(records[0].verified_at);
    assert(document.querySelector('[data-action-effects="hub-write queued-upgates"]'));
    previewMode = 'expired'; await click('preview-order-cost'); assert(required('confirm-order-cost').disabled && required('send-order-cost').disabled);
    previewMode = 'malformed'; await click('preview-order-cost'); assert(required('preview-order-cost').disabled && required('send-order-cost').disabled, 'Malformed successful response is not accepted as safe preview');
    previewMode = 'ok'; await click('retry-cost-preview'); assert(!element('cost-preview-recovery'));
    await click('load-cost-settings');
    records = Array.from({ length: 51 }, (_, i) => ({ ...preview(), id: `record-${i}`, subject: `ORDER-${i}`, status: 'verified' }));
    await click('load-cost-history'); assert(required('cost-history-previous').disabled && !required('cost-history-next').disabled); await click('cost-history-next'); assert(calls.at(-1).path.includes('offset=50')); assert(element('cost-publication-record-50') && !element('cost-publication-record-49'));
    optionsMode = 'pending'; await click('load-cost-settings'); const old = resolveOptions, oldCall = calls.at(-1);
    await act(async () => { unlockHub('synthetic-new-token'); await tick(); }); assert(oldCall.signal.aborted);
    await act(async () => { old(); await tick(); }); assert(!element('cost-products'), 'An old credential response cannot reopen settings');
    optionsMode = 'ok'; await input('cost-shop', 'biketrek'); await click('load-cost-settings'); assert(!required('cost-products').checked); assert.equal(calls.at(-1).headers.Authorization, 'Bearer synthetic-new-token');
    await click('lock-cost-sync'); assert(element('cost-token') && !element('load-cost-settings')); assert.equal(required('cost-token').value, '');
    assert.equal(dom.window.localStorage.length, 0); assert.equal(dom.window.sessionStorage.length, 0);
    console.log('fifo-cost-sync-ui: defaults, CAS, inheritance, server gates, weighted preview, explicit send, lost responses, recovery, paging and credential invalidation passed');
  } finally { await act(async () => { root.unmount(); await tick(); }); }
})().catch(error => { console.error(error); process.exitCode = 1; });
