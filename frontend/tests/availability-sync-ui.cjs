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
const i18n = require('../src/i18n/index.ts').default;
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
    const target = required(id), proto = target.tagName === 'SELECT' ? dom.window.HTMLSelectElement.prototype : target.tagName === 'TEXTAREA' ? dom.window.HTMLTextAreaElement.prototype : dom.window.HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, 'value').set.call(target, value);
    target.dispatchEvent(new dom.window.Event(target.tagName === 'SELECT' ? 'change' : 'input', { bubbles: true })); await tick();
  });
}
const polls = new Map(); let nextPoll = 10000;
const realTimeout = window.setTimeout.bind(window), realClear = window.clearTimeout.bind(window);
window.setTimeout = (fn, delay, ...args) => { if (delay === 5000) { polls.set(++nextPoll, fn); return nextPoll; } return realTimeout(fn, delay, ...args); };
window.clearTimeout = id => { if (!polls.delete(id)) realClear(id); };
async function poll() { assert(polls.size, 'Pending refresh has a read-only status poll'); const [id, fn] = polls.entries().next().value; polls.delete(id); await act(async () => { fn(); await tick(); await tick(); }); }
const clone = value => JSON.parse(JSON.stringify(value));
const reply = value => ({ ok: true, json: async () => clone(value) });
let supplier = { supplier: 'fixture', name: 'Fixture supplier', feed_keys: ['stock', 'products'], feed_key: 'stock', revision: 0, enabled: false,
  interval_seconds: 3600, freshness_seconds: 21600, min_coverage_percent: 100, last_started_at: null, last_success_at: null,
  next_run_at: null, last_error: null, manual_requested_at: null, last_item_count: null, running: false,
  availability: { orderable: 'do 7 dní', unknown: 'overíme' } };
let showSloger = false;
let sloger = { ...clone(supplier), supplier: 'sloger', name: 'Sloger', revision: 1, feed_keys: ['b2b'], feed_key: 'b2b',
  manual_only: true, source_kind: 'b2b', refresh_scope: 'hub_products', refresh_progress: null };
let defaults = { revision: 2, warehouse_code: 'main', interval_seconds: 300, batch_size: 20, max_order_age_seconds: 900 };
const emptySettings = () => ({ revision: 0, enabled: false, authorized: false, protocol: 'legacy_absolute', interval_seconds: null, batch_size: null, max_order_age_seconds: null,
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
  baseline_count: 2, runs: hasRun ? [clone(run)] : [], server_write_enabled: true });
const capacity = shop => ({ shop_code: shop, observed_at: '2026-10-14T19:00:00Z',
  probe: { ok: true, http_status: 200, error: null, requests_attempted: 1, latency_ms: 123,
    target_fingerprint: 'synthetic-fingerprint-not-for-display', permissions: { products: 'all', orders: 'read', order_statuses: 'read' }, write_tested: false },
  quota: { hour_limit: 1000, day_limit: 24000, hour_remaining: 0, day_remaining: 19000,
    total_remaining: 0, retry_after_seconds: null, theoretical_daily_max: 48000, headers_complete: true },
  projection: { captured_at: '2026-10-14T19:00:00Z', warehouse_code: 'main', listed_skus: 50, confirmed_skus: 15, eligible_skus: 30, unpublishable_skus: 20,
    eligibility_errors: [{ code: 'stock_sync_mapping_ambiguous', count: 20 }],
    stock: { configured: true, enabled: false, authorized: false, server_enabled: false, interval_seconds: 300,
      batch_size: 20, call_model: 'origin_aware_delta', audit_interval_seconds: 21600, audit_pass_calls: 2, initial_batch_calls: 2, singleton_pass_calls: 30,
      unchanged_pass_calls: 0, all_changed_pass_calls: 32, daily_unchanged_if_enabled: 8, daily_all_changed_if_enabled: 9216 },
    orders: { collector_enabled: false, processing_mode: 'manual', processing_ready: false, processing_error: 'order_processing_manual',
      poll_interval_seconds: 300, retry_minutes: 5, full_check_hours: 24, inbox_count: 5, jobs_by_status: { pending: 1, running: 0, completed: 3, retry: 1, review: 0 },
      unprocessed_count: 2, collector_empty_poll_calls: 2, collector_daily_if_enabled: 576, retry_daily_if_processing: 288, history_daily_if_processing: 6 },
    estimate_kind: 'theoretical_no_runtime_delay', not_included: ['other_integrations', 'manual_operations', 'extra_order_pages', 'new_orders', 'errors_and_retries', 'other_hub_sync'] } });
let capacityMode = 'ok', sampleErrorCode = 'stock_sync_untranslated_sample_error', finishCapacity;
const capacityText = key => i18n.t(`availabilitySync.capacity.${key}`);
const calls = [];
global.fetch = async (path, init = {}) => {
  const body = init.body ? JSON.parse(init.body) : undefined; calls.push({ path, ...init, body });
  assert.equal(init.cache, 'no-store'); assert(init.headers.Authorization.startsWith('Bearer synthetic-')); assert(!path.includes('synthetic-'));
  const url = new URL(path, dom.window.location.origin);
  if (/^\/api\/shops\/(biketrek|xtrek)\/upgates\/capacity$/.test(url.pathname)) {
    assert.equal(init.method, 'GET'); assert.equal(body, undefined);
    let result = capacity(url.pathname.split('/')[3]);
    if (url.searchParams.get('include_product_sample') === 'true') result.catalog_probe = { requested: true, ok: true, selected_products: 4, selected_variants: 4, planned_requests: 2, requests_attempted: 2, error: null };
    if (capacityMode === 'sample-failed' && result.catalog_probe) result.catalog_probe = { ...result.catalog_probe, ok: false, requests_attempted: null, error: sampleErrorCode };
    if (capacityMode === 'pending') return new Promise(resolve => { finishCapacity = () => resolve(reply(result)); });
    if (capacityMode === 'missing') result.quota = { ...result.quota, hour_limit: null, hour_remaining: null, day_limit: null, day_remaining: null, total_remaining: null, theoretical_daily_max: null, headers_complete: false };
    if (capacityMode === 'empty-catalog') result.projection.stock = { ...result.projection.stock, audit_pass_calls: 0, initial_batch_calls: 0, singleton_pass_calls: 0, unchanged_pass_calls: 0, all_changed_pass_calls: 0, daily_unchanged_if_enabled: 0, daily_all_changed_if_enabled: 0 };
    if (capacityMode === 'unknown-catalog') result.projection.stock = { ...result.projection.stock, audit_pass_calls: null, initial_batch_calls: null, singleton_pass_calls: null, unchanged_pass_calls: null, all_changed_pass_calls: null, daily_unchanged_if_enabled: null, daily_all_changed_if_enabled: null };
    if (capacityMode === 'limited') { result.probe = { ...result.probe, ok: false, http_status: 429, error: 'upgates_capacity_rate_limited' }; result.quota.retry_after_seconds = 60; }
    if (capacityMode === 'malformed') result = { shop_code: result.shop_code, probe: {}, quota: {} };
    return reply(result);
  }
  if (url.pathname === '/api/supplier-availability/fixture/runs/72/failures') {
    assert.equal(init.method, 'GET');
    const offset = Number(url.searchParams.get('offset'));
    return reply({ run_id: 72, total: 51, offset, limit: 50, items: Array.from({ length: offset ? 1 : 50 }, (_, index) => ({
      row: offset + index + 1, sku: `FEED-${offset + index + 1}`, stock: '-1', external_stock: null,
      kind: 'rejected', code: 'supplier_availability_invalid_quantity' })) });
  }
  if (url.pathname === '/api/supplier-availability') { assert.equal(init.method, 'GET'); return reply({ suppliers: showSloger ? [supplier, sloger] : [supplier] }); }
  if (url.pathname === '/api/supplier-availability/sloger') {
    assert.equal(init.method, 'PUT'); assert.equal(body.expected_revision, sloger.revision);
    assert.equal(body.enabled, false); assert.equal(body.feed_key, 'b2b');
    assert.equal(body.interval_seconds, Math.min(sloger.interval_seconds, body.freshness_seconds)); assert.equal(body.min_coverage_percent, sloger.min_coverage_percent);
    assert(body.freshness_seconds >= body.interval_seconds, 'Manual TTL satisfies the shared persistence constraint without enabling a schedule');
    sloger = { ...sloger, ...body, revision: sloger.revision + 1 }; return reply(sloger);
  }
  if (url.pathname === '/api/supplier-availability/sloger/run') {
    assert.equal(body.expected_revision, sloger.revision); assert.equal(init.method, 'POST');
    sloger.manual_requested_at = '2026-10-13T12:00:00Z'; return reply({ supplier: 'sloger', queued: true });
  }
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
    if (body.authorized && (!previous.authorized || previous.protocol !== 'delta_v1' || authorityChanged)) assert(body.hub_is_stock_authority && body.native_order_stock_enabled && body.external_stock_writers_disabled && body.orders_reconciled);
    shops[body.shop_code] = { ...previous, ...body, protocol: body.authorized ? 'delta_v1' : previous.protocol, revision: previous.revision + 1 }; authorityChanged = false;
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
    assert.deepEqual(body, run.protocol === 'delta_v1' ? { confirmed: true, external_requests_finished: true, outcome: 'not_applied', evidence: 'Upgates support confirmed the request was never applied.' } : { confirmed: true, external_requests_finished: true });
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
  const beforeCapacity = calls.length;
  const capacityScope = required('probe-upgates-capacity').closest('.action-control').querySelector('[data-action-effects]');
  assert.equal(capacityScope.dataset.actionEffects, 'hub-read upgates-read');
  assert(capacityScope.textContent.includes(i18n.t('actions.callsKnown', { count: 1 })));
  assert.equal(required('upgates-capacity-panel').querySelectorAll('input').length, 1, 'The only diagnostic option is a read-only product sample');
  assert.equal(required('capacity-product-sample').checked, false, 'Product reads are off by default');
  capacityMode = 'pending';
  await act(async () => { required('probe-upgates-capacity').click(); required('probe-upgates-capacity').click(); await tick(); });
  assert.equal(calls.length, beforeCapacity + 1, 'An explicit double click starts exactly one diagnostic GET');
  assert.equal(calls.at(-1).path, '/api/shops/xtrek/upgates/capacity');
  assert(required('capacity-product-sample').disabled, 'The scope cannot change during an in-flight probe');
  await act(async () => { finishCapacity(); await tick(); });
  assert.equal(required('upgates-capacity-remaining').textContent, '0', 'Observed zero quota remains a real zero');
  assert.equal(required('upgates-capacity-daily-max').textContent.replace(/[^0-9]/g, ''), '48000', 'The theoretical daily limit is separate from current remaining quota');
  assert(required('upgates-capacity-result').textContent.includes(capacityText('estimateHelp')));
  assert(required('upgates-capacity-result').textContent.includes(capacityText('writeUntested')));
  assert(!required('upgates-capacity-result').textContent.includes('synthetic-fingerprint'), 'Internal target fingerprints are not shown');
  assert(required('upgates-capacity-panel').textContent.includes(i18n.t('actions.callsActual', { count: 1 })));
  assert.equal(required('upgates-capacity-singleton-calls').textContent, '30');
  assert.equal(required('upgates-capacity-batch-calls').textContent, '2', 'Batched initial calls come from the backend grouping plan');
  assert.equal(required('upgates-capacity-read-savings').textContent, (1 - 2 / 30).toLocaleString(i18n.language, { style: 'percent', maximumFractionDigits: 1 }));
  const capacityRows = required('upgates-capacity-estimate').querySelectorAll('tbody tr');
  assert.equal(capacityRows[0].children[1].textContent, '0', 'Unchanged local counters and metadata use no API requests between audits');
  assert.equal(capacityRows[0].children[2].textContent, '8', 'The unchanged daily estimate includes four six-hour batch audits');
  assert(required('upgates-capacity-audits').textContent.includes(i18n.t('availabilitySync.capacity.auditHelp', { hours: '6', calls: '2' })), 'Audit request count and interval are explicit');
  assert.equal(capacityRows[1].children[1].textContent, '32', 'The all-changed estimate uses batch identity reads and one delta write per changed item');
  assert(required('upgates-capacity-result').textContent.includes(capacityText('changedReadsHelp')), 'Delta acknowledgement, uncertainty and retry limitations are disclosed');
  assert.equal(writes().length, 0, 'Capacity diagnostics never mutate Hub settings or stock');
  const beforeSampleOption = calls.length; await click('capacity-product-sample');
  assert.equal(calls.length, beforeSampleOption, 'Selecting the sample never starts requests by itself');
  assert(!element('upgates-capacity-result'), 'Changing the diagnostic scope clears the previous count');
  assert(required('upgates-capacity-panel').textContent.includes(i18n.t('actions.callsKnown', { count: 3 })));
  capacityMode = 'ok'; await click('probe-upgates-capacity');
  assert.equal(calls.at(-1).path, '/api/shops/xtrek/upgates/capacity?include_product_sample=true');
  assert(required('upgates-capacity-panel').textContent.includes(i18n.t('actions.callsActual', { count: 3 })), 'Actual calls include status and the confirmed sample request count');
  assert(required('upgates-capacity-sample').textContent.includes(capacityText('sampleOk')));
  assert(required('upgates-capacity-sample').textContent.includes(i18n.t('availabilitySync.capacity.sampleCounts', { products: '4', variants: '4' })));
  capacityMode = 'sample-failed'; await click('probe-upgates-capacity');
  assert(required('upgates-capacity-sample').textContent.includes(capacityText('sampleFailed')));
  const failedSampleScope = required('probe-upgates-capacity').closest('.action-control').querySelector('[data-action-effects]');
  assert(failedSampleScope.textContent.includes(i18n.t('actions.callsUnmeasured')), 'An unknown sample attempt count stays unknown, not zero or status-only');
  assert(!failedSampleScope.textContent.includes(i18n.t('actions.callsActual', { count: 0 })));
  assert.equal([...required('upgates-capacity-sample').querySelectorAll('p')].filter(p => p.textContent === capacityText('sampleFailed')).length, 1, 'An untranslated sample failure does not repeat the generic explanation');
  sampleErrorCode = 'stock_publication_stock_unknown'; await click('probe-upgates-capacity');
  assert(required('upgates-capacity-sample').textContent.includes(i18n.t('stockPublication.errors.stock_publication_stock_unknown')), 'Sample failures expose the existing specific stock-source explanation');
  assert(!required('upgates-capacity-sample').textContent.includes(sampleErrorCode), 'A translated reason is displayed without raw implementation codes');
  assert.equal(writes().length, 0, 'Sample diagnostics make no mutation requests');
  await click('capacity-product-sample');
  capacityMode = 'empty-catalog'; await click('probe-upgates-capacity');
  assert.equal(required('upgates-capacity-batch-calls').textContent, '0');
  assert.equal(required('upgates-capacity-read-savings').textContent, capacityText('unknown'), 'An empty baseline never manufactures a 100% saving');
  capacityMode = 'unknown-catalog'; await click('probe-upgates-capacity');
  assert.equal(required('upgates-capacity-singleton-calls').textContent, capacityText('unknown'));
  assert.equal(required('upgates-capacity-read-savings').textContent, capacityText('unknown'), 'Missing catalog eligibility never becomes a savings estimate');
  const unknownRows = required('upgates-capacity-estimate').querySelectorAll('tbody tr');
  assert.equal(unknownRows[0].children[1].textContent, capacityText('unknown'), 'Unknown eligible stock cannot be claimed as a zero-call scenario');
  assert.equal(unknownRows[0].children[2].textContent, capacityText('unknown'));
  assert(required('upgates-capacity-audits').textContent.includes(capacityText('unknown')), 'Unknown audit scope remains unknown');
  capacityMode = 'missing'; await click('probe-upgates-capacity');
  assert.equal(required('upgates-capacity-remaining').textContent, capacityText('unknown'));
  assert.equal(required('upgates-capacity-daily-max').textContent, capacityText('unknown'));
  assert(element('upgates-capacity-missing-quota'), 'Missing quota is explained instead of silently using zero');
  capacityMode = 'limited'; await click('probe-upgates-capacity');
  assert(required('upgates-capacity-panel').textContent.includes(capacityText('errors.upgates_capacity_rate_limited')));
  assert(required('upgates-capacity-result').textContent.includes(i18n.t('availabilitySync.capacity.retryAfter', { seconds: 60 })));
  const afterLimited = calls.length; await tick(); assert.equal(calls.length, afterLimited, 'Rate limits never trigger automatic diagnostic retries');
  capacityMode = 'pending'; await click('probe-upgates-capacity'); const oldCapacity = finishCapacity, oldCapacityRequest = calls.at(-1);
  await input('sync-shop', 'biketrek'); assert(oldCapacityRequest.signal.aborted);
  await act(async () => { oldCapacity(); await tick(); }); assert(!element('upgates-capacity-result'), 'Changing shop clears and rejects a delayed diagnostic for the previous target');
  assert.equal(calls.length, afterLimited + 1, 'Changing shop never starts a new external probe');
  capacityMode = 'ok'; await click('probe-upgates-capacity'); assert.equal(calls.at(-1).path, '/api/shops/biketrek/upgates/capacity');
  capacityMode = 'pending'; await click('probe-upgates-capacity'); const staleCapacity = finishCapacity, staleCapacityRequest = calls.at(-1);
  await act(async () => { unlockHub('synthetic-capacity-replacement'); await tick(); staleCapacity(); await tick(); });
  assert(staleCapacityRequest.signal.aborted && !element('upgates-capacity-result'), 'An old credential response cannot repopulate capacity results');
  capacityMode = 'malformed'; await click('probe-upgates-capacity');
  assert(!element('upgates-capacity-result') && required('upgates-capacity-panel').textContent.includes(capacityText('failed')), 'Incomplete successful responses fail visibly');
  capacityMode = 'ok'; await input('sync-shop', 'xtrek'); assert.equal(writes().length, 0);
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
  await click('sync-authority'); await click('sync-writers-disabled'); assert(required('save-stock-sync').disabled, 'Every authority assertion is required');
  await click('sync-orders-reconciled'); assert(required('save-stock-sync').disabled, 'Native order stock behavior needs explicit confirmation'); await click('sync-native-orders'); assert(!required('save-stock-sync').disabled);
  await click('sync-enabled'); assert(!required('sync-authority').checked, 'Editing configuration clears authority assertions');
  await click('sync-inherit-interval_seconds'); await input('sync-shop-interval_seconds', '900');
  await click('sync-authority'); await click('sync-native-orders'); await click('sync-writers-disabled'); await click('sync-orders-reconciled');
  stockSaveMode = 'pending';
  const beforeStock = writes().length;
  await act(async () => { required('save-stock-sync').click(); required('save-stock-sync').click(); await tick(); });
  assert.equal(writes().length, beforeStock + 1); assert(required('sync-shop').disabled, 'Cannot switch target while saving');
  assert.deepEqual(writes().at(-1).body, { shop_code: 'xtrek', expected_revision: 0, enabled: true, authorized: true, interval_seconds: 900, batch_size: null, max_order_age_seconds: null, hub_is_stock_authority: true, native_order_stock_enabled: true, external_stock_writers_disabled: true, orders_reconciled: true, confirmed: true });
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
  run.protocol = 'delta_v1'; run.items[0].status = 'uncertain'; run.items[0].desired.stock_increment = '-1'; delete run.items[0].desired.stock;
  await click('refresh-sync-run');
  assert(required('sync-run-detail').textContent.includes(i18n.t('availabilitySync.deltaRecoveryHelp')));
  assert.equal(required('resolve-sync-77').closest('.action-control').querySelector('[data-action-effects]').dataset.actionEffects, 'hub-write', 'Saving a verified delta outcome does not read or write Upgates');
  await click('sync-finished-77'); assert(required('resolve-sync-77').disabled, 'A finished request and numeric readback cannot resolve an uncertain delta');
  await input('sync-outcome-77', 'not_applied'); await input('sync-evidence-77', 'short');
  assert(required('resolve-sync-77').disabled, 'Outcome requires meaningful independent evidence');
  await input('sync-evidence-77', 'Upgates support confirmed the request was never applied.');
  const beforeDeltaResolve = writes().length;
  await act(async () => { required('resolve-sync-77').click(); required('resolve-sync-77').click(); await tick(); });
  assert.equal(writes().length, beforeDeltaResolve + 1, 'Delta resolution is sent once with the selected outcome and evidence');
  assert(!element('resolve-sync-77'));
  run.items = Array.from({ length: 101 }, (_, index) => ({ id: 100 + index, sku: `FIXTURE-${index}`, status: 'verified', error: null, desired: { stock: 1, availability: 'SKLADOM', can_add_to_basket_yn: true } }));
  await click('refresh-sync-run'); assert(!required('sync-items-next').disabled && required('sync-items-previous').disabled);
  await click('sync-items-next'); assert(calls.at(-1).path.includes('offset=100&limit=100')); assert(required('sync-items-next').disabled && !required('sync-items-previous').disabled);
  assert(required('sync-run-detail').textContent.includes('FIXTURE-100') && !required('sync-run-detail').textContent.includes('FIXTURE-99'), 'Pagination can inspect all transfer items');
  await click('sync-items-previous'); assert(calls.at(-1).path.includes('offset=0&limit=100'));
  authorityChanged = true; await click('load-stock-sync'); assert(element('sync-authority'), 'Changed shop topology offers explicit reauthorization');
  await click('sync-authority'); await click('sync-native-orders'); await click('sync-writers-disabled'); await click('sync-orders-reconciled'); assert(!required('save-stock-sync').disabled); await click('save-stock-sync'); assert.equal(authorityChanged, false);

  shops.xtrek.protocol = 'legacy_absolute'; await click('load-stock-sync');
  assert(required('run-stock-sync').disabled && element('sync-native-orders'), 'A previously authorized absolute writer cannot run without reconfirmation');
  await click('sync-authority'); await click('sync-native-orders'); await click('sync-writers-disabled'); await click('sync-orders-reconciled'); await click('save-stock-sync');
  assert.equal(shops.xtrek.protocol, 'delta_v1');
  assert(required('sync-baseline-count').textContent.includes('2'));

  showSloger = true; const beforeSloger = writes().length; await click('load-suppliers');
  assert(!element('supplier-sloger-enabled') && !element('supplier-sloger-interval') && !element('supplier-sloger-coverage') && !element('supplier-sloger-feed'), 'Sloger offers no feed or periodic scheduler controls');
  assert(required('supplier-sloger-manual-help').textContent.includes('iba ručne'));
  assert.equal(writes().length, beforeSloger, 'Loading manual availability performs no background refresh');
  await input('supplier-sloger-freshness', '300');
  assert(!required('supplier-sloger-save').disabled, 'Manual freshness need not exceed an unused scheduling interval');
  await click('supplier-sloger-save'); assert.equal(writes().length, beforeSloger + 1);
  assert.equal(writes().at(-1).body.interval_seconds, 300); assert.equal(writes().at(-1).body.freshness_seconds, 300); assert.equal(writes().at(-1).body.enabled, false);
  await act(async () => { required('supplier-sloger-run').click(); required('supplier-sloger-run').click(); await tick(); });
  assert.equal(writes().length, beforeSloger + 2, 'Manual refresh queues once on an explicit click');
  assert(required('supplier-sloger-run').disabled);
  sloger.manual_requested_at = null; sloger.refresh_progress = { total: 2, processed: 2, succeeded: 1, failed: 1, failures: [{ sku: 'SLG-2', code: 'sloger_product_not_found' }], status: 'completed' };
  await click('load-suppliers');
  assert(required('supplier-sloger-refresh-progress').textContent.includes('SLG-2'));
  assert(required('supplier-sloger-refresh-progress').textContent.includes('chyby: 1'));
  assert(!required('supplier-sloger-run').disabled); assert.equal(writes().length, beforeSloger + 2);

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
  const beforeFocused = writes().length;
  supplier = { ...supplier, revision: 1, enabled: false, manual_requested_at: null, last_success_at: null, status: 'not_loaded' };
  await act(async () => {
    unlockHub('synthetic-focused-token');
    root.render(React.createElement(MemoryRouter, { key: 'focused', initialEntries: ['/settings/availability?supplier=fixture&sku=103404-281-103'] }, React.createElement(AvailabilitySyncPage)));
    await tick(); await tick();
  });
  assert(required('supplier-fixture-card'));
  assert(!element('supplier-fixture-observation') && !element('selected-supplier-sku'));
  assert(!element('supplier-sloger-card') && !element('sync-shop') && !element('probe-upgates-capacity'), 'Supplier link focuses only the relevant workflow');
  assert(!calls.some(call => call.path.includes('/observation')), 'Supplier settings never fetch a single product');
  assert.equal(writes().length, beforeFocused, 'Supplier deep link only reads supplier-wide setup');
  assert.equal(required('supplier-fixture-enabled').checked, false);
  await input('supplier-fixture-freshness', '22000');
  assert(required('all-suppliers-blocked').disabled && !element('all-suppliers'), 'Changing focus cannot silently discard supplier edits');
  await click('supplier-fixture-discard');
  assert(required('all-suppliers') && !element('all-suppliers-blocked'));
  assert.equal(writes().length, beforeFocused, 'Discarding local edits makes no request');
  assert(!required('supplier-fixture-run').disabled, 'Explicit manual refresh remains available with automation off');
  await click('supplier-fixture-run');
  assert.equal(writes().length, beforeFocused + 1);
  assert.equal(writes().at(-1).path, '/api/supplier-availability/fixture/run');
  assert.equal(supplier.enabled, false, 'Manual refresh does not enable the scheduler');
  await input('supplier-fixture-interval', '4800');
  supplier = { ...supplier, manual_requested_at: null, last_success_at: '2026-10-15T12:00:00Z', status: 'fresh', latest_run: {
    id: 72, feed_key: 'stock', status: 'partial', started_at: '2026-10-15T11:59:00Z', finished_at: '2026-10-15T12:00:00Z', error: null,
    report: { total: 151, accepted: 100, rejected: 51, unknown: 0, reasons: { supplier_availability_invalid_quantity: 51 }, warnings: [] } } };
  const beforePoll = writes().length;
  await poll();
  assert.equal(required('supplier-fixture-interval').value, '4800', 'Background completion preserves unsaved settings');
  assert.equal(required('supplier-fixture-count-total').textContent, '151');
  assert.equal(required('supplier-fixture-count-accepted').textContent, '100');
  assert.equal(required('supplier-fixture-count-rejected').textContent, '51');
  await click('supplier-fixture-failures');
  assert(required('supplier-fixture-card').textContent.includes('FEED-1'));
  assert(required('supplier-fixture-card').textContent.includes(i18n.t('availabilitySync.errors.supplier_availability_invalid_quantity')));
  assert(required('supplier-fixture-failures-prev').disabled);
  await click('supplier-fixture-failures-next');
  assert(required('supplier-fixture-card').textContent.includes('FEED-51'));
  assert(required('supplier-fixture-failures-next').disabled);
  assert.equal(writes().length, beforePoll, 'Polling and paging the failures never mutate settings or retry the feed');
  await click('supplier-fixture-discard');
  supplier.manual_requested_at = '2026-10-15T12:00:00Z';
  await click('load-suppliers');
  await input('supplier-fixture-interval', '5400');
  supplierSaveMode = 'pending'; await click('supplier-fixture-save');
  await act(async () => { rejectSupplier(); await tick(); });
  await poll();
  assert(required('supplier-fixture-save').disabled && required('supplier-fixture-run').disabled, 'Status polling cannot resolve an uncertain write');
  assert.equal(required('supplier-fixture-interval').value, '5400');
  supplierSaveMode = 'ok'; supplier.manual_requested_at = null; await click('load-suppliers');
  await click('all-suppliers'); assert(element('sync-shop'), 'Operator can return to all settings');
  await act(async () => { root.unmount(); await tick(); });
  console.log('availability-sync-ui: supplier validation, CAS, inheritance, central authority, manual jobs, recovery, credentials and stale responses passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
