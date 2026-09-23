/* Synthetic API interactions only. No real configuration or stock writes. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'https://hub.example.test/settings/stock' });
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
const { StockSettingsPage } = require('../src/pages/StockSettingsPage.tsx');
const { unlockHub } = require('../src/api/access.ts');
const root = createRoot(document.getElementById('root'));
const tick = () => new Promise(resolve => setTimeout(resolve, 5));
const element = id => document.querySelector(`[data-testid="${id}"]`);
const required = id => { const value = element(id); assert(value, `${id} exists`); return value; };
const cannotSave = id => !element(id) || element(id).disabled;
async function click(id) { await act(async () => { required(id).click(); await tick(); }); }
async function input(id, value) {
  await act(async () => {
    const target = required(id);
    const prototype = target.tagName === 'SELECT' ? dom.window.HTMLSelectElement.prototype : dom.window.HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(prototype, 'value').set.call(target, value);
    target.dispatchEvent(new dom.window.Event(target.tagName === 'SELECT' ? 'change' : 'input', { bubbles: true }));
    await tick();
  });
}
const defaults = {
  poll_interval_seconds: 300, reconcile_interval_hours: 24, full_order_check_hours: 24,
  overlap_minutes: 10, reconcile_window_days: 7, max_pages_per_pass: 100,
  run_timeout_seconds: 180, retry_base_seconds: 300, retry_max_seconds: 3600,
  processing_batch_size: 20, processing_retry_minutes: 5,
  publication_batch_size: 20, publication_preview_minutes: 15,
};
const warehouses = {
  main: { warehouse_code: 'main', revision: 4, values: { ...defaults }, processing_paused: false },
  auxiliary: { warehouse_code: 'auxiliary', revision: 0, values: { ...defaults }, processing_paused: false },
};
const shopSettings = { xtrek: null, biketrek: null };
const policy = { warehouse_id: 1, warehouse_code: 'main', starts_at: '2026-09-23T10:00:00Z', revision: 7 };
const options = shop => {
  const settings = shopSettings[shop];
  return { shop: { code: shop, name: shop === 'xtrek' ? 'xTrek fixture' : 'BIKETREK fixture' },
    warehouses: [{ id: 1, code: 'main', name: 'Main fixture' }, { id: 2, code: 'auxiliary', name: 'Auxiliary fixture' }],
    policy, warehouse: warehouses.main, shop_settings: settings,
    effective: { values: { ...warehouses.main.values, ...settings?.overrides },
      sources: Object.fromEntries(Object.keys(defaults).map(key => [key, Object.hasOwn(settings?.overrides || {}, key) ? 'shop' : 'warehouse'])),
      configuration_hash: `synthetic-settings-${warehouses.main.revision}-${settings?.revision || 0}`,
      mode: settings?.mode || 'manual', processing_paused: warehouses.main.processing_paused,
      automation_starts_at: settings?.automation_starts_at || null, issue_starts_at: settings?.issue_starts_at || null },
  };
};
const clone = value => JSON.parse(JSON.stringify(value));
const reply = value => ({ ok: true, json: async () => clone(value) });
const calls = [];
let optionsMode = 'valid', warehouseMode = 'valid', warehouseSaveMode = 'valid', shopSaveMode = 'valid';
let resolveOptions, resolveWarehouse, rejectWarehouseSave, rejectShopSave;
global.fetch = async (path, init = {}) => {
  const body = init.body ? JSON.parse(init.body) : undefined;
  calls.push({ path, ...init, body });
  assert(path.startsWith('/api/stock-settings/'), 'This page only accesses its protected settings API');
  assert.equal(init.cache, 'no-store');
  assert(init.headers.Authorization.startsWith('Bearer synthetic-'));
  assert(!path.includes('synthetic-'), 'Credentials never enter request URLs');
  const url = new URL(path, dom.window.location.origin);
  if (url.pathname.endsWith('/options')) {
    assert.equal(init.method, 'GET');
    const snapshot = clone(options(url.searchParams.get('shop_code')));
    if (optionsMode === 'pending') return new Promise(resolve => { resolveOptions = () => resolve(reply(snapshot)); });
    return reply(snapshot);
  }
  if (url.pathname.endsWith('/warehouse') && init.method === 'GET') {
    const snapshot = clone(warehouses[url.searchParams.get('warehouse_code')]);
    if (warehouseMode === 'pending') return new Promise(resolve => { resolveWarehouse = () => resolve(reply(snapshot)); });
    return reply(snapshot);
  }
  if (url.pathname.endsWith('/warehouse') && init.method === 'POST') {
    const previous = warehouses[body.warehouse_code];
    assert.equal(body.expected_revision, previous.revision, 'Warehouse save uses the revision actually loaded');
    assert.equal(body.confirmed, true);
    assert.deepEqual(Object.keys(body.values).sort(), Object.keys(defaults).sort(), 'Warehouse save sends the complete configuration');
    assert(Object.values(body.values).every(value => typeof value === 'number' && Number.isInteger(value)));
    warehouses[body.warehouse_code] = { warehouse_code: body.warehouse_code, revision: previous.revision + 1,
      values: clone(body.values), processing_paused: body.processing_paused };
    if (warehouseSaveMode === 'pending') return new Promise((resolve, reject) => { rejectWarehouseSave = () => reject(new TypeError('Synthetic connection lost')); });
    return reply(warehouses[body.warehouse_code]);
  }
  if (url.pathname.endsWith('/shop') && init.method === 'POST') {
    const previous = shopSettings[body.shop_code];
    assert.equal(body.expected_revision, previous?.revision || 0, 'An unconfigured shop starts at expected revision zero');
    assert.equal(body.expected_warehouse_revision, warehouses.main.revision, 'Shop overrides are bound to loaded warehouse settings');
    assert.equal(body.confirmed, true);
    assert.equal(body.fulfillment_confirmed, body.mode === 'fulfill');
    shopSettings[body.shop_code] = { revision: (previous?.revision || 0) + 1, overrides: clone(body.overrides), mode: body.mode,
      automation_starts_at: '2026-09-23T13:00:00Z', issue_starts_at: body.mode === 'fulfill' ? '2026-09-23T13:00:00Z' : null,
      authorized_policy_revision: policy.revision, target_fingerprint: 'synthetic-target-fingerprint' };
    if (shopSaveMode === 'pending') return new Promise((resolve, reject) => { rejectShopSave = () => reject(new TypeError('Synthetic connection lost')); });
    return reply(options(body.shop_code));
  }
  throw new Error('Unexpected synthetic endpoint ' + path);
};
const posts = () => calls.filter(call => call.method === 'POST');
const warehousePosts = () => posts().filter(call => call.path.endsWith('/warehouse'));
const shopPosts = () => posts().filter(call => call.path.endsWith('/shop'));

(async () => {
  unlockHub('');
  await act(async () => { root.render(React.createElement(MemoryRouter, { initialEntries: ['/settings/stock?shop=xtrek'] }, React.createElement(StockSettingsPage))); await tick(); });
  assert.equal(calls.length, 0, 'Mount never fetches settings');
  await input('unlock-token', 'synthetic-settings-token'); await click('unlock');
  assert.equal(calls.length, 0, 'Unlocking does not automatically read or change settings');
  assert.equal(required('shop').value, 'xtrek');
  await click('load-options');
  assert.equal(calls.length, 1); assert.equal(calls[0].path, '/api/stock-settings/options?shop_code=xtrek');
  assert.equal(posts().length, 0);
  await input('warehouse', 'main'); await click('load-warehouse');
  assert.equal(calls.at(-1).path, '/api/stock-settings/warehouse?warehouse_code=main');
  assert(cannotSave('save-warehouse'), 'Warehouse changes require an explicit confirmation');
  const beforeInvalidWarehouse = calls.length;
  await input('warehouse-poll_interval_seconds', '59'); await click('warehouse-confirm'); await click('save-warehouse');
  assert(cannotSave('save-warehouse'), 'Intervals below the supported minimum cannot be saved');
  await input('warehouse-poll_interval_seconds', '300.5'); await click('warehouse-confirm'); await click('save-warehouse');
  assert(cannotSave('save-warehouse'), 'Fractional scheduling intervals cannot be saved');
  await input('warehouse-poll_interval_seconds', '600');
  await input('warehouse-retry_base_seconds', '3600'); await input('warehouse-retry_max_seconds', '300');
  await click('warehouse-confirm'); await click('save-warehouse');
  assert(cannotSave('save-warehouse'), 'The retry maximum must cover the retry base');
  assert.equal(calls.length, beforeInvalidWarehouse, 'Invalid settings are rejected before any request');
  await input('warehouse-retry_base_seconds', '300'); await input('warehouse-retry_max_seconds', '3600');
  await input('warehouse-poll_interval_seconds', '600');
  await click('warehouse-confirm'); assert(!required('save-warehouse').disabled);
  await click('warehouse-paused');
  assert(!required('warehouse-confirm').checked && cannotSave('save-warehouse'), 'Pause changes invalidate confirmation');
  await click('warehouse-confirm'); await input('warehouse-reconcile_interval_hours', '48');
  assert(!required('warehouse-confirm').checked, 'Numeric changes invalidate confirmation');
  await click('warehouse-confirm'); warehouseSaveMode = 'pending';
  await act(async () => { required('save-warehouse').click(); required('save-warehouse').click(); await tick(); });
  assert.equal(warehousePosts().length, 1, 'Double clicks cannot duplicate configuration writes');
  assert(required('shop').disabled, 'The shop cannot be switched while a save is in flight');
  assert.deepEqual(warehousePosts()[0].body, { warehouse_code: 'main', expected_revision: 4,
    values: { ...defaults, poll_interval_seconds: 600, reconcile_interval_hours: 48 }, processing_paused: true, confirmed: true });
  await act(async () => { rejectWarehouseSave(); await tick(); });
  const afterWarehouseLoss = posts().length;
  if (element('warehouse-confirm') && !element('warehouse-confirm').disabled) await click('warehouse-confirm');
  assert(cannotSave('save-warehouse'), 'Lost warehouse response blocks another save until an explicit read');
  await tick(); assert.equal(posts().length, afterWarehouseLoss, 'A lost response never causes an automatic retry');
  await click('load-warehouse');
  assert.equal(posts().length, afterWarehouseLoss, 'Recovery reads the applied warehouse result without a write');
  assert.equal(required('warehouse-poll_interval_seconds').value, '600');
  assert(required('warehouse-paused').checked);
  assert(!required('warehouse-confirm').checked);
  warehouseSaveMode = 'valid'; await input('warehouse-poll_interval_seconds', '900');
  await click('warehouse-confirm'); await click('save-warehouse');
  assert.equal(warehousePosts().at(-1).body.expected_revision, 5, 'Recovered revision replaces the stale revision');

  const beforeSelection = calls.length; await input('warehouse', 'auxiliary');
  assert.equal(calls.length, beforeSelection, 'Changing warehouse selection does not fetch automatically');
  assert(cannotSave('save-warehouse'), 'Selecting another warehouse clears the previous form authorization');
  await click('load-warehouse'); await input('warehouse-poll_interval_seconds', '420');
  await click('warehouse-confirm'); await click('save-warehouse');
  assert.equal(warehousePosts().at(-1).body.expected_revision, 0, 'An unconfigured warehouse uses revision zero');

  await click('load-options');
  assert.equal(required('mode').value, 'manual');
  assert(required('inherit-poll_interval_seconds').checked);
  assert(required('override-poll_interval_seconds').disabled, 'Inherited values cannot accidentally become overrides');
  assert(cannotSave('save-shop'));
  await click('inherit-poll_interval_seconds');
  assert.equal(required('override-poll_interval_seconds').value, '900', 'A new override starts from the effective warehouse value');
  const beforeInvalidOverride = calls.length;
  await input('override-poll_interval_seconds', '86401'); await click('shop-confirm'); await click('save-shop');
  assert(cannotSave('save-shop') && calls.length === beforeInvalidOverride, 'Out-of-range shop overrides cannot be submitted');
  await input('override-poll_interval_seconds', '1200'); await input('mode', 'fulfill');
  await click('shop-confirm');
  assert(cannotSave('save-shop'), 'Fulfillment needs its separate physical-issue authorization');
  await click('fulfillment-confirm'); assert(!required('save-shop').disabled);
  await input('mode', 'reserve');
  assert(!required('shop-confirm').checked, 'Changing automation mode invalidates authorization');
  await input('mode', 'fulfill'); await click('shop-confirm'); await click('fulfillment-confirm');
  await input('override-poll_interval_seconds', '1500');
  assert(!required('shop-confirm').checked && !required('fulfillment-confirm').checked, 'Override edits invalidate both confirmations');
  await click('shop-confirm'); await click('fulfillment-confirm'); shopSaveMode = 'pending';
  await act(async () => { required('save-shop').click(); required('save-shop').click(); await tick(); });
  assert.equal(shopPosts().length, 1, 'Double clicks cannot duplicate shop authorization');
  assert.deepEqual(shopPosts()[0].body, { shop_code: 'xtrek', expected_revision: 0, expected_warehouse_revision: 6,
    overrides: { poll_interval_seconds: 1500 }, mode: 'fulfill', confirmed: true, fulfillment_confirmed: true });
  await act(async () => { rejectShopSave(); await tick(); });
  const afterShopLoss = posts().length;
  if (element('shop-confirm') && !element('shop-confirm').disabled) await click('shop-confirm');
  if (element('fulfillment-confirm') && !element('fulfillment-confirm').disabled) await click('fulfillment-confirm');
  assert(cannotSave('save-shop'), 'Lost shop response requires a new options read');
  await click('load-options'); assert.equal(posts().length, afterShopLoss);
  assert.equal(required('mode').value, 'fulfill');
  assert.equal(required('override-poll_interval_seconds').value, '1500');
  assert(!required('shop-confirm').checked && !required('fulfillment-confirm').checked);
  shopSaveMode = 'valid'; await click('inherit-reconcile_interval_hours');
  await input('override-reconcile_interval_hours', '72'); await click('shop-confirm'); await click('fulfillment-confirm');
  await click('inherit-poll_interval_seconds');
  assert(!required('shop-confirm').checked && !required('fulfillment-confirm').checked, 'Returning to inheritance clears both confirmations');
  await input('mode', 'reserve'); await click('shop-confirm'); await click('save-shop');
  assert.deepEqual(shopPosts().at(-1).body, { shop_code: 'xtrek', expected_revision: 1, expected_warehouse_revision: 6,
    overrides: { reconcile_interval_hours: 72 }, mode: 'reserve', confirmed: true, fulfillment_confirmed: false },
  'Inherited settings are omitted, never saved as copied overrides');

  optionsMode = 'pending'; await click('load-options');
  const staleOptions = resolveOptions, staleOptionsRequest = calls.at(-1);
  const beforeShop = calls.length; await input('shop', 'biketrek');
  assert.equal(calls.length, beforeShop, 'Switching shops does not automatically load settings');
  assert(staleOptionsRequest.signal.aborted);
  await act(async () => { staleOptions(); await tick(); });
  assert(!element('mode'), 'Delayed options for the previous shop cannot restore editable configuration');
  optionsMode = 'valid'; await click('load-options');
  assert.equal(required('mode').value, 'manual', 'The other shop retains its independent mode');
  assert(required('inherit-reconcile_interval_hours').checked, 'The other shop does not inherit the previous shop draft');

  warehouseMode = 'pending'; await input('warehouse', 'main'); await click('load-warehouse');
  const staleWarehouse = resolveWarehouse, staleWarehouseRequest = calls.at(-1);
  const beforeWarehouseChange = calls.length; await input('warehouse', 'auxiliary');
  assert.equal(calls.length, beforeWarehouseChange); assert(staleWarehouseRequest.signal.aborted);
  await act(async () => { staleWarehouse(); await tick(); });
  assert(!element('warehouse-poll_interval_seconds'), 'Delayed warehouse response cannot populate a different selection');
  warehouseMode = 'valid'; await click('load-warehouse');
  assert.equal(required('warehouse-poll_interval_seconds').value, '420');

  optionsMode = 'pending'; await click('load-options');
  const oldCredentialOptions = resolveOptions, oldCredentialRequest = calls.at(-1);
  const beforeToken = calls.length;
  await act(async () => { unlockHub('synthetic-replacement-token'); await tick(); oldCredentialOptions(); await tick(); });
  assert(oldCredentialRequest.signal.aborted);
  assert.equal(calls.length, beforeToken, 'Changing shared credentials never refetches automatically');
  assert(!element('mode') && !element('warehouse-poll_interval_seconds'), 'Credential changes discard configuration and confirmations');
  assert.equal(dom.window.localStorage.length, 0); assert.equal(dom.window.sessionStorage.length, 0);
  assert(!dom.window.location.href.includes('synthetic-'));
  const beforeIdle = calls.length; await tick(); assert.equal(calls.length, beforeIdle, 'Settings never poll or save in the background');
  await act(async () => { root.unmount(); await tick(); }); unlockHub('');
  console.log('Stock settings UI behavior checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
