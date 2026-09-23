/* Synthetic API interactions only. No live collection or external stock writes. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'https://hub.example.test/orders/inbox' });
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
const { OrdersInboxPage } = require('../src/pages/OrdersInboxPage.tsx');
const { unlockHub } = require('../src/api/access.ts');
const root = createRoot(document.getElementById('root'));
const tick = () => new Promise(resolve => setTimeout(resolve, 5));
const t = key => i18n.t(`orderCollection.${key}`);
const button = key => [...document.querySelectorAll('button')].find(element => element.textContent === t(key));
const field = key => [...document.querySelectorAll('label')].find(element => element.textContent.startsWith(t(key))).querySelector('input,select,textarea');
async function click(element) { assert(element, 'Element exists'); await act(async () => { element.click(); await tick(); }); }
async function input(element, value) {
  await act(async () => {
    const prototype = element.tagName === 'SELECT' ? dom.window.HTMLSelectElement.prototype : element.tagName === 'TEXTAREA' ? dom.window.HTMLTextAreaElement.prototype : dom.window.HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(prototype, 'value').set.call(element, value);
    element.dispatchEvent(new dom.window.Event(element.tagName === 'SELECT' ? 'change' : 'input', { bubbles: true }));
    await tick();
  });
}
let collector = null;
let connection = true;
let connectionMatches = true;
let hasPolicy = true;
const status = shop => ({ shop: { code: shop, name: shop }, policy: hasPolicy ? { starts_at: '2026-09-23T10:00:00Z', warehouse_code: 'main' } : null,
  collector, connection_configured: connection, connection_matches: connectionMatches, external_write_enabled: false });
const entry = (id, number, changes = {}) => ({ id, order_number: number, source_uuid: `00000000-0000-4000-8000-${String(id).padStart(12, '0')}`, created_at: '2026-09-23T11:00:00Z', updated_at: '2026-09-23T11:30:00Z', deleted: false, origin: 'eshop', status_id: 8,
  observed_at: '2026-09-23T12:00:00Z', last_seen_at: '2026-09-23T12:00:00Z', review_reason: null, stock_state: null, stock_issued_at: null, stock_updated_at: null, ...changes });
const inbox = offset => ({ entries: offset ? [entry(51, 'NEXT-PAGE')] : [entry(1, 'ACTIVE-1'), entry(2, 'DELETED-1', { deleted: true }), entry(3, 'CONFLICT-1', { review_reason: 'order_collection_identity_conflict' }), entry(4, 'NO-IDENTITY', { source_uuid: null }), entry(5, 'EMPTY-REVIEW', { review_reason: '' })], total: 51, limit: 50, offset });
const runs = { runs: [{ id: 1, mode: 'delta', status: 'failed', started_at: '2026-09-23T12:00:00Z', completed_at: '2026-09-23T12:00:01Z', from_at: '2026-09-23T11:00:00Z', until_at: '2026-09-23T12:00:00Z', pages: 1, observed_count: 0, error: 'order_collection_source_unavailable' }] };
const stock = (shop, sku = 'KNOWN-ZERO') => ({ shop_code: shop, warehouse: { id: 1, code: 'main', name: 'Main fixture' }, captured_at: '2026-09-23T12:05:00Z', external_write_enabled: false,
  rows: [{ sku, product_id: 1, target: { parent_code: 'PARENT', variant_code: sku, code: sku }, quantity_known: true, qty_on_hand: '3', qty_reserved: '3', qty_available: '0', errors: [] },
    { sku: 'UNKNOWN-BALANCE', product_id: 2, target: null, quantity_known: false, qty_on_hand: null, qty_reserved: null, qty_available: null, errors: ['stock_projection_balance_missing'] }] });
const calls = []; let pendingStatus; let pendingStock; let statusMode = 'valid'; let stockMode = 'valid'; let configMode = 'valid'; let rejectConfig;
global.fetch = async (path, init = {}) => {
  const body = init.body ? JSON.parse(init.body) : undefined;
  calls.push({ path, ...init, body });
  assert(path.startsWith('/api/order-collection/'), 'No stock apply, outbox or shop-write endpoint can be invoked');
  assert.equal(init.cache, 'no-store'); assert(init.headers.Authorization.startsWith('Bearer synthetic-'));
  const url = new URL(path, dom.window.location.origin); const shop = url.searchParams.get('shop_code') || body?.shop_code;
  if (url.pathname.endsWith('/status')) {
    if (statusMode === 'pending') return new Promise(resolve => { pendingStatus = value => resolve({ ok: true, json: async () => value }); });
    return { ok: true, json: async () => status(shop) };
  }
  if (url.pathname.endsWith('/inbox')) return { ok: true, json: async () => inbox(Number(url.searchParams.get('offset'))) };
  if (url.pathname.endsWith('/runs')) return { ok: true, json: async () => runs };
  if (url.pathname.endsWith('/configure')) {
    assert.equal(init.method, 'POST'); assert.equal(body.confirmed, true); assert.equal(body.expected_revision, collector?.revision ?? null);
    collector = { enabled: body.enabled, revision: (collector?.revision ?? 0) + 1, cursor_at: '2026-09-23T10:00:00Z', last_reconciled_at: null, next_poll_at: '2026-09-23T12:10:00Z', last_started_at: null, last_completed_at: null, last_error: null, entries_seen: 5 };
    if (configMode === 'pending') return new Promise((resolve, reject) => { rejectConfig = () => reject(new TypeError('Synthetic connection lost')); });
    return { ok: true, json: async () => status(shop) };
  }
  if (url.pathname.endsWith('/refresh')) {
    assert.equal(body.expected_revision, collector.revision); assert.equal(body.confirmed, true); assert(collector.enabled);
    return { ok: true, json: async () => status(shop) };
  }
  if (url.pathname.endsWith('/stock-preview')) {
    assert.equal(init.method, 'POST'); assert.deepEqual(Object.keys(body).sort(), ['shop_code', 'skus']);
    if (stockMode === 'pending') return new Promise(resolve => { pendingStock = value => resolve({ ok: true, json: async () => value }); });
    return { ok: true, json: async () => stock(shop) };
  }
  throw new Error('Unexpected synthetic endpoint ' + path);
};
const configurations = () => calls.filter(call => call.path.endsWith('/configure'));
const posts = () => calls.filter(call => call.method === 'POST');

(async () => {
  unlockHub('');
  await act(async () => { root.render(React.createElement(MemoryRouter, { initialEntries: ['/orders/inbox?shop=xtrek'] }, React.createElement(OrdersInboxPage))); await tick(); });
  assert.equal(calls.length, 0);
  await input(document.querySelector('input[type="password"]'), 'synthetic-collection-token'); await click(button('unlock'));
  assert.equal(calls.length, 0, 'Token entry never starts collection or loads records automatically');
  assert.equal(field('shop').value, 'xtrek');
  await click(button('load')); assert.equal(calls.length, 3, 'Explicit overview load reads status, one inbox page and runs');
  assert(calls.every(call => call.path.includes('shop_code=xtrek')));
  assert(button('enable').disabled && button('fetchNow').disabled);
  assert.equal(posts().length, 0);
  const orderTable = [...document.querySelectorAll('table')].find(table => table.textContent.includes('ACTIVE-1'));
  const links = [...orderTable.querySelectorAll('a')];
  assert.equal(links.length, 1, 'Deleted, missing identity, conflict and any non-null review reason suppress processing links');
  assert.equal(links[0].getAttribute('href'), '/orders/stock?shop=xtrek&order=ACTIVE-1');
  assert(orderTable.textContent.includes(t('deleted')) && orderTable.textContent.includes(t('stockStates.none')));
  assert(document.body.textContent.includes(t('runStates.failed')));
  await click(field('enableConfirm')); configMode = 'pending'; const enable = button('enable');
  await act(async () => { enable.click(); enable.click(); await tick(); });
  assert.equal(configurations().length, 1, 'A double click cannot duplicate enable requests');
  assert(field('shop').disabled);
  await act(async () => { rejectConfig(); await tick(); });
  assert(document.body.textContent.includes(t('uncertain')) && button('enable').disabled);
  const beforeRecovery = posts().length; await click(button('load'));
  assert.equal(posts().length, beforeRecovery, 'Lost configuration response recovers through reads only');
  assert(button('pause') && !button('fetchNow').disabled);
  configMode = 'valid'; await click(button('fetchNow'));
  assert(document.body.textContent.includes(t('refreshQueued')));
  assert.equal(calls.at(-1).body.expected_revision, 1);
  await click(button('pause')); assert.equal(configurations().at(-1).body.enabled, false);
  assert(button('fetchNow').disabled && !field('enableConfirm').checked, 'Pause prevents scheduled-refresh requests until explicitly re-enabled');
  await click(button('next')); assert(calls.at(-1).path.endsWith('limit=50&offset=50')); assert(document.body.textContent.includes('NEXT-PAGE'));
  await click(button('previous')); assert(calls.at(-1).path.endsWith('offset=0'));

  await input(field('skus'), 'KNOWN-ZERO\nUNKNOWN-BALANCE'); await click(button('previewStock'));
  assert.deepEqual(calls.at(-1).body, { shop_code: 'xtrek', skus: ['KNOWN-ZERO', 'UNKNOWN-BALANCE'] });
  const stockTable = [...document.querySelectorAll('table')].find(table => table.textContent.includes('KNOWN-ZERO'));
  const stockRows = [...stockTable.querySelectorAll('tbody tr')];
  assert.equal(stockRows[0].querySelectorAll('td')[4].textContent, '0', 'Known zero remains a numeric string');
  assert.equal(stockRows[1].querySelectorAll('td')[4].textContent, t('unknown'), 'Missing balance never silently becomes zero');
  assert(document.body.textContent.includes(t('draftOnly')));
  assert(![...document.querySelectorAll('button')].some(element => /odoslať|send stock|publish/i.test(element.textContent)), 'No outbound stock button exists');
  const beforeInvalidSkus = calls.length; await input(field('skus'), Array.from({ length: 101 }, (_, index) => 'SKU-' + index).join('\n')); await click(button('previewStock'));
  assert.equal(calls.length, beforeInvalidSkus, 'More than 100 SKUs is rejected before a request');
  await input(field('skus'), 'DUPLICATE\nDUPLICATE'); await click(button('previewStock'));
  assert.equal(calls.length, beforeInvalidSkus, 'Duplicate SKU selection is rejected without silently changing the selection');

  stockMode = 'pending'; await input(field('skus'), 'OLD-SKU'); await click(button('previewStock'));
  const oldStock = pendingStock, oldStockRequest = calls.at(-1);
  await input(field('skus'), 'NEW-SKU'); assert(oldStockRequest.signal.aborted);
  await act(async () => { oldStock(stock('xtrek', 'STALE-STOCK-ROW')); await tick(); });
  assert(!document.body.textContent.includes('STALE-STOCK-ROW'), 'Editing SKU input invalidates delayed draft results');

  statusMode = 'pending'; await click(button('load')); const oldStatus = pendingStatus; const staleRequest = calls.findLast(call => call.path.includes('/status?'));
  const beforeShop = calls.length; await input(field('shop'), 'biketrek'); assert.equal(calls.length, beforeShop);
  assert(staleRequest.signal.aborted);
  await act(async () => { oldStatus(status('xtrek')); await tick(); });
  assert(!document.body.textContent.includes('ACTIVE-1'), 'Shop changes discard pending inbox and status responses');
  statusMode = 'valid'; stockMode = 'valid'; connection = false; hasPolicy = false;
  await click(button('load')); await click(field('enableConfirm'));
  assert(button('enable').disabled, 'Missing connection or stock policy prevents enabling');
  assert(document.querySelector('a[href="/orders/stock?shop=biketrek"]'));
  connection = true; hasPolicy = true; connectionMatches = false; await click(button('load')); await click(field('enableConfirm'));
  assert(button('enable').disabled && document.body.textContent.includes(t('connectionChanged')), 'A different target connection stays visibly blocked');
  connectionMatches = true; await click(button('load'));
  statusMode = 'pending'; await click(button('load')); const oldTokenStatus = pendingStatus;
  await act(async () => { unlockHub('synthetic-replacement-token'); await tick(); oldTokenStatus(status('biketrek')); await tick(); });
  assert(!document.body.textContent.includes('ACTIVE-1'));
  assert.equal(field('skus').value, '', 'Shared token changes clear typed product data too');
  assert.equal(dom.window.localStorage.length, 0); assert.equal(dom.window.sessionStorage.length, 0);
  assert(!dom.window.location.href.includes('synthetic-'));
  const beforeWait = calls.length; await tick(); assert.equal(calls.length, beforeWait, 'The browser never polls or posts automatically');
  await act(async () => { root.unmount(); await tick(); }); unlockHub('');
  console.log('Order collection UI behavior checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
