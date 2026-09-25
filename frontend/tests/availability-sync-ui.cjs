/* Synthetic protected API interactions. No supplier downloads or shop writes. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'https://hub.example.test/settings/availability' });
global.window = dom.window; global.document = dom.window.document;
Object.defineProperty(global, 'navigator', { value: dom.window.navigator, configurable: true });
global.HTMLElement = dom.window.HTMLElement; global.IS_REACT_ACT_ENVIRONMENT = true;
for (const ext of ['.ts', '.tsx']) require.extensions[ext] = (module, filename) => {
  module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8').replace(/import\.meta\.env/g, '{}'), {
    compilerOptions: { jsx: ts.JsxEmit.React, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, esModuleInterop: true },
  }).outputText, filename);
};
require.extensions['.css'] = () => {};
require('../src/i18n/index.ts');
const React = require('react'); const { act } = React;
const { createRoot } = require('react-dom/client');
const { MemoryRouter } = require('react-router-dom');
const { AvailabilitySyncPage } = require('../src/pages/AvailabilitySyncPage.tsx');
const { unlockHub } = require('../src/api/access.ts');
const root = createRoot(document.getElementById('root'));
const tick = () => new Promise(resolve => setTimeout(resolve, 5));
const element = id => document.querySelector(`[data-testid="${id}"]`);
const required = id => { const result = element(id); assert(result, `${id} exists`); return result; };
async function click(id) { await act(async () => { required(id).click(); await tick(); }); }
async function input(id, value) {
  await act(async () => {
    const target = required(id), proto = target.tagName === 'SELECT' ? dom.window.HTMLSelectElement.prototype : dom.window.HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, 'value').set.call(target, value);
    target.dispatchEvent(new dom.window.Event(target.tagName === 'SELECT' ? 'change' : 'input', { bubbles: true })); await tick();
  });
}
const clone = value => JSON.parse(JSON.stringify(value));
const reply = value => ({ ok: true, json: async () => clone(value) });
let supplier = { supplier: 'fixture', name: 'Fixture supplier', feed_keys: ['stock', 'products'], feed_key: 'stock', revision: 0, enabled: false,
  interval_seconds: 3600, freshness_seconds: 21600, min_coverage_percent: 100, last_started_at: null, last_success_at: null,
  next_run_at: null, last_error: null, manual_requested_at: null, last_item_count: null, running: false,
  availability: { orderable: 'do 7 dní', unknown: 'overíme' } };
let defaults = { revision: 2, warehouse_code: 'main', interval_seconds: 300, batch_size: 20, max_order_age_seconds: 900 };
const emptySettings = () => ({ revision: 0, enabled: false, authorized: false, interval_seconds: null, batch_size: null, max_order_age_seconds: null,
  authority_confirmed_at: null, next_run_at: null, last_started_at: null, last_completed_at: null, last_error: null });
let shops = { xtrek: emptySettings(), biketrek: emptySettings() }, authorityChanged = false;
let run = { id: '22222222-2222-4222-8222-222222222222', shop_code: 'xtrek', trigger: 'manual', status: 'queued', started_at: null,
  completed_at: null, error: null, counts: { verified: 0, skipped: 0, failed: 0, uncertain: 0, pending: 0 }, items: [], more_pending: true };
let hasRun = false, supplierSaveMode = 'ok', stockSaveMode = 'ok', optionsMode = 'ok', runMode = 'ok';
let rejectSupplier, rejectStock, resolveOptions, rejectRun;
let repairMode = 'ok', finishRepair;
const options = shop => ({ shop: { code: shop, name: shop }, warehouse: { code: 'main', name: 'Main warehouse' }, warehouse_settings: clone(defaults),
  settings: clone(shops[shop]), effective: Object.fromEntries(['interval_seconds','batch_size','max_order_age_seconds'].map(key => [key, shops[shop][key] ?? defaults[key]])),
  blockers: !shops[shop].authorized ? ['stock_sync_authority_required'] : authorityChanged ? ['stock_sync_authority_changed'] : [],
  runs: hasRun ? [clone(run)] : [], server_write_enabled: true });
const calls = [];
global.fetch = async (path, init = {}) => {
  const body = init.body ? JSON.parse(init.body) : undefined; calls.push({ path, ...init, body });
  assert.equal(init.cache, 'no-store'); assert(init.headers.Authorization.startsWith('Bearer synthetic-')); assert(!path.includes('synthetic-'));
  const url = new URL(path, dom.window.location.origin);
  if (url.pathname === '/api/supplier-availability') { assert.equal(init.method, 'GET'); return reply({ suppliers: [supplier] }); }
  if (url.pathname === '/api/supplier-availability/fixture') {
    assert.equal(init.method, 'PUT'); assert.equal(body.expected_revision, supplier.revision);
    supplier = { ...supplier, ...body, revision: supplier.revision + 1 };
    if (supplierSaveMode === 'pending') return new Promise((resolve, reject) => { rejectSupplier = () => reject(new TypeError('Synthetic lost response')); });
    return reply(supplier);
  }
  if (url.pathname === '/api/supplier-availability/fixture/run') { assert.equal(body.expected_revision, supplier.revision); supplier.manual_requested_at = '2026-09-24T10:00:00Z'; return reply({ supplier: 'fixture', queued: true }); }
  if (url.pathname === '/api/supplier-availability/fixture/links/reconcile') {
    assert.equal(init.method, 'POST'); assert.equal(body.limit, 500);
    const result = body.after_product_id === 0 ? { supplier: 'fixture', scanned: 500, linked: 20, existing: 475, skipped: 4, conflicts: [{ product_id: 18, sku: 'TEST-18', supplier_sku: '18', reason: 'supplier_link_conflict' }], skipped_details: [], next_after_product_id: 700 }
      : { supplier: 'fixture', scanned: 40, linked: 10, existing: 30, skipped: 0, conflicts: [], skipped_details: [], next_after_product_id: null };
    assert([0, 700].includes(body.after_product_id), 'Reconciliation follows the server cursor');
    if (repairMode === 'pending' && body.after_product_id === 0) return new Promise(resolve => { finishRepair = () => resolve(reply(result)); });
    if (repairMode === 'lost' && body.after_product_id === 700) throw new TypeError('Synthetic lost link result');
    return reply(result);
  }
  if (url.pathname === '/api/stock-sync/options') {
    assert.equal(init.method, 'GET'); const value = options(url.searchParams.get('shop_code'));
    if (optionsMode === 'pending') return new Promise(resolve => { resolveOptions = () => resolve(reply(value)); });
    return reply(value);
  }
  if (url.pathname === '/api/stock-sync/warehouse') { assert.equal(body.expected_revision, defaults.revision); defaults = { ...body, revision: defaults.revision + 1 }; return reply(defaults); }
  if (url.pathname === '/api/stock-sync/configure') {
    const previous = shops[body.shop_code]; assert.equal(body.expected_revision, previous.revision); assert.equal(body.confirmed, true);
    if (body.authorized && (!previous.authorized || authorityChanged)) assert(body.hub_is_stock_authority && body.external_stock_writers_disabled && body.orders_reconciled);
    shops[body.shop_code] = { ...previous, ...body, revision: previous.revision + 1 }; authorityChanged = false;
    if (stockSaveMode === 'pending') return new Promise((resolve, reject) => { rejectStock = () => reject(new TypeError('Synthetic lost response')); });
    return reply(options(body.shop_code));
  }
  if (url.pathname === '/api/stock-sync/run') {
    assert.deepEqual(body, { shop_code: 'xtrek', confirmed: true }); hasRun = true;
    if (runMode === 'pending') return new Promise((resolve, reject) => { rejectRun = () => reject(new TypeError('Synthetic lost enqueue response')); });
    return reply(run);
  }
  if (url.pathname === `/api/stock-sync/runs/${run.id}`) { assert.equal(init.method, 'GET'); const offset = Number(url.searchParams.get('offset') || 0), limit = Number(url.searchParams.get('limit') || 1000); return reply({ ...run, items: run.items.slice(offset, offset + limit), items_offset: offset, items_limit: limit, items_total: run.items.length, items_truncated: offset + limit < run.items.length }); }
  if (url.pathname === '/api/stock-sync/items/77/resolve') {
    assert.deepEqual(body, { confirmed: true, external_requests_finished: true });
    run.items[0].status = 'verified'; run.counts.verified = 1; run.counts.uncertain = 0; return reply(run);
  }
  throw new Error('Unexpected endpoint ' + path);
};
const writes = () => calls.filter(call => call.method !== 'GET');
(async () => {
  unlockHub('');
  await act(async () => { root.render(React.createElement(MemoryRouter, { initialEntries: ['/settings/availability?shop=xtrek'] }, React.createElement(AvailabilitySyncPage))); await tick(); });
  assert.equal(calls.length, 0);
  await input('sync-token', 'synthetic-sync-token'); await click('unlock-sync'); assert.equal(calls.length, 0, 'Unlock never starts external work');
  assert.equal(required('sync-shop').value, 'xtrek');
  await click('load-suppliers'); assert.equal(writes().length, 0); assert(required('supplier-fixture-run').disabled, 'Unconfigured supplier requires saved settings');
  await input('supplier-fixture-interval', '299'); assert(required('supplier-fixture-save').disabled);
  await input('supplier-fixture-interval', '300.5'); assert(required('supplier-fixture-save').disabled);
  await input('supplier-fixture-interval', '7200'); await input('supplier-fixture-freshness', '3600'); assert(required('supplier-fixture-save').disabled, 'Freshness cannot expire before refresh interval');
  await input('supplier-fixture-freshness', '14400'); await input('supplier-fixture-coverage', '0'); assert(required('supplier-fixture-save').disabled);
  await input('supplier-fixture-coverage', '95'); await click('supplier-fixture-enabled');
  supplierSaveMode = 'pending';
  await act(async () => { required('supplier-fixture-save').click(); required('supplier-fixture-save').click(); await tick(); });
  assert.equal(writes().length, 1, 'Supplier double click sends one PUT'); assert(required('load-suppliers').disabled);
  assert.deepEqual(writes()[0].body, { expected_revision: 0, enabled: true, feed_key: 'stock', interval_seconds: 7200, freshness_seconds: 14400, min_coverage_percent: 95 });
  await act(async () => { rejectSupplier(); await tick(); }); assert(required('supplier-fixture-save').disabled && required('supplier-fixture-run').disabled);
  await tick(); assert.equal(writes().length, 1, 'Lost supplier save is never retried');
  supplierSaveMode = 'ok'; await click('load-suppliers'); assert.equal(required('supplier-fixture-interval').value, '7200');
  await click('supplier-fixture-run'); assert.equal(writes().length, 2); assert(required('supplier-fixture-run').disabled);
  assert(document.querySelector('[data-action-effects="hub-write queued-supplier"]'), 'Supplier work is not labelled as Upgates work');
  supplier.manual_requested_at = null; await click('load-suppliers'); await input('supplier-fixture-interval', '3600'); assert(required('supplier-fixture-run').disabled, 'Manual run cannot silently use unsaved settings');
  await click('supplier-fixture-save'); assert.equal(writes().at(-1).body.expected_revision, 1);

  const beforeRepair = writes().length;
  repairMode = 'pending';
  await act(async () => { required('supplier-fixture-reconcile').click(); required('supplier-fixture-reconcile').click(); await tick(); });
  assert.equal(writes().length, beforeRepair + 1, 'Double click starts only one local reconciliation');
  assert(required('load-suppliers').disabled && required('supplier-fixture-save').disabled && required('supplier-fixture-run').disabled);
  assert.equal(required('supplier-fixture-reconcile').parentElement.querySelector('[data-action-effects]').getAttribute('data-action-effects'), 'hub-write', 'Link repair has no supplier or shop API effect');
  await act(async () => { finishRepair(); await tick(); });
  assert.equal(writes().length, beforeRepair + 2);
  assert.equal(writes().at(-1).body.after_product_id, 700);
  assert(required('supplier-fixture-link-result').textContent.includes('540'));
  assert(required('supplier-fixture-link-result').textContent.includes('Nové väzby: 30'));
  assert(required('supplier-fixture-link-result').textContent.includes('TEST-18'), 'Unresolved identities remain visible');

  repairMode = 'lost'; await click('supplier-fixture-reconcile');
  const afterLostRepair = writes().length;
  assert(required('supplier-fixture-reconcile').disabled, 'A failed batch requires operator recovery');
  assert(required('supplier-fixture-link-result').textContent.includes('500'), 'Completed batches remain visible after a later failure');
  await tick(); assert.equal(writes().length, afterLostRepair, 'A lost local repair result never starts automatic retries');
  repairMode = 'ok'; await click('load-suppliers');
  assert(!required('supplier-fixture-reconcile').disabled);

  await click('load-stock-sync'); assert(required('run-stock-sync').disabled); assert(required('sync-inherit-interval_seconds').checked);
  assert(required('sync-shop-interval_seconds').disabled, 'Inherited value stays an inheritance');
  await input('sync-warehouse-interval_seconds', '59'); assert(required('save-sync-warehouse').disabled);
  await input('sync-warehouse-interval_seconds', '600'); await click('save-sync-warehouse');
  assert.equal(writes().at(-1).body.expected_revision, 2); assert(required('save-stock-sync').disabled && required('run-stock-sync').disabled, 'Warehouse save requires fresh effective shop read');
  await click('load-stock-sync'); assert.equal(required('sync-shop-interval_seconds').value, '600');
  await click('sync-authorized'); assert(required('save-stock-sync').disabled);
  await click('sync-authority'); await click('sync-writers-disabled'); assert(required('save-stock-sync').disabled, 'All three authority assertions are required');
  await click('sync-orders-reconciled'); assert(!required('save-stock-sync').disabled);
  await click('sync-enabled'); assert(!required('sync-authority').checked, 'Editing configuration clears authority assertions');
  await click('sync-inherit-interval_seconds'); await input('sync-shop-interval_seconds', '900');
  await click('sync-authority'); await click('sync-writers-disabled'); await click('sync-orders-reconciled');
  stockSaveMode = 'pending';
  const beforeStock = writes().length;
  await act(async () => { required('save-stock-sync').click(); required('save-stock-sync').click(); await tick(); });
  assert.equal(writes().length, beforeStock + 1); assert(required('sync-shop').disabled, 'Cannot switch target while saving');
  assert.deepEqual(writes().at(-1).body, { shop_code: 'xtrek', expected_revision: 0, enabled: true, authorized: true, interval_seconds: 900, batch_size: null, max_order_age_seconds: null, hub_is_stock_authority: true, external_stock_writers_disabled: true, orders_reconciled: true, confirmed: true });
  await act(async () => { rejectStock(); await tick(); }); assert(required('save-stock-sync').disabled && required('run-stock-sync').disabled);
  stockSaveMode = 'ok'; await click('load-stock-sync'); assert(!required('run-stock-sync').disabled); assert(!element('sync-authority'));
  await click('sync-enabled'); await click('save-stock-sync'); assert.equal(writes().at(-1).body.hub_is_stock_authority, false, 'Existing authorization need not be asserted for an interval-only save');
  assert(!required('run-stock-sync').disabled, 'Manual transfer is available with automatic scheduling disabled');
  runMode = 'pending'; const beforeRun = writes().length;
  await act(async () => { required('run-stock-sync').click(); required('run-stock-sync').click(); await tick(); }); assert.equal(writes().length, beforeRun + 1);
  await act(async () => { rejectRun(); await tick(); }); assert(required('run-stock-sync').disabled); await tick(); assert.equal(writes().length, beforeRun + 1, 'Lost enqueue never retries automatically');
  await click('load-stock-sync'); runMode = 'ok'; await click(`sync-run-${run.id}`); assert(element('sync-run-detail'));
  run.status = 'completed'; run.more_pending = false; run.counts.uncertain = 1; run.items = [{ id: 77, sku: 'EXACT-LEAF', status: 'uncertain', error: 'timeout', desired: { stock: 2, availability: 'SKLADOM', can_add_to_basket_yn: true }, before: null, after: null, attempt_started_at: '2026-09-24T10:00:00Z', verified_at: null }];
  await click('refresh-sync-run'); assert(required('resolve-sync-77').disabled); const beforeResolve = writes().length;
  await click('resolve-sync-77'); assert.equal(writes().length, beforeResolve);
  await click('sync-finished-77'); await click('resolve-sync-77'); assert(!element('resolve-sync-77')); assert.equal(writes().length, beforeResolve + 1);
  run.items = Array.from({ length: 101 }, (_, index) => ({ id: 100 + index, sku: `FIXTURE-${index}`, status: 'verified', error: null, desired: { stock: 1, availability: 'SKLADOM', can_add_to_basket_yn: true } }));
  await click('refresh-sync-run'); assert(!required('sync-items-next').disabled && required('sync-items-previous').disabled);
  await click('sync-items-next'); assert(calls.at(-1).path.includes('offset=100&limit=100')); assert(required('sync-items-next').disabled && !required('sync-items-previous').disabled);
  assert(required('sync-run-detail').textContent.includes('FIXTURE-100') && !required('sync-run-detail').textContent.includes('FIXTURE-99'), 'Pagination can inspect all transfer items');
  await click('sync-items-previous'); assert(calls.at(-1).path.includes('offset=0&limit=100'));
  authorityChanged = true; await click('load-stock-sync'); assert(element('sync-authority'), 'Changed shop topology offers explicit reauthorization');
  await click('sync-authority'); await click('sync-writers-disabled'); await click('sync-orders-reconciled'); assert(!required('save-stock-sync').disabled); await click('save-stock-sync'); assert.equal(authorityChanged, false);

  optionsMode = 'pending'; await click('load-stock-sync'); const late = resolveOptions, pendingRequest = calls.at(-1);
  await act(async () => { unlockHub('synthetic-new-token'); await tick(); }); assert(pendingRequest.signal.aborted);
  await act(async () => { late(); await tick(); }); assert(!element('sync-enabled'), 'An old credential response cannot repopulate editable settings');
  optionsMode = 'ok'; await input('sync-shop', 'biketrek'); await click('load-stock-sync'); assert(!required('sync-authorized').checked);
  assert(calls.at(-1).path.endsWith('shop_code=biketrek')); assert(calls.at(-1).headers.Authorization === 'Bearer synthetic-new-token');
  await click('load-suppliers'); repairMode = 'pending'; await click('supplier-fixture-reconcile');
  const repairRequest = calls.at(-1), finishOldRepair = finishRepair, countBeforeLock = calls.length;
  await click('lock-sync'); assert(!element('load-suppliers') && element('sync-token')); assert.equal(required('sync-token').value, '');
  assert(repairRequest.signal.aborted);
  await act(async () => { finishOldRepair(); await tick(); });
  assert.equal(calls.length, countBeforeLock, 'Locking access stops cursor continuation after the current batch');
  assert.equal(dom.window.localStorage.length, 0); assert.equal(dom.window.sessionStorage.length, 0);
  await act(async () => { root.unmount(); await tick(); });
  console.log('availability-sync-ui: supplier validation, CAS, inheritance, central authority, manual jobs, recovery, credentials and stale responses passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
