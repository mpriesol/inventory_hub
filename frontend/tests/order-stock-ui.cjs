/* Operator interaction checks with synthetic APIs only; no live stock writes. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'https://hub.example.test/orders/stock' });
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
const { OrdersStockPage } = require('../src/pages/OrdersStockPage.tsx');
const { unlockHub } = require('../src/api/access.ts');
const root = createRoot(document.getElementById('root'));
const tick = () => new Promise(resolve => setTimeout(resolve, 5));
const t = key => i18n.t(`orderStock.${key}`);
const button = key => [...document.querySelectorAll('button')].find(element => element.textContent === t(key));
const field = key => [...document.querySelectorAll('label')].find(element => element.textContent.startsWith(t(key))).querySelector('input,select');
const actionSelect = id => document.querySelector(`select[aria-label^="${t('statusAction')} ${id} "]`);
async function click(element) { assert(element, 'Element exists'); await act(async () => { element.click(); await tick(); }); }
async function input(element, value) {
  await act(async () => {
    const prototype = element.tagName === 'SELECT' ? dom.window.HTMLSelectElement.prototype : dom.window.HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(prototype, 'value').set.call(element, value);
    element.dispatchEvent(new dom.window.Event(element.tagName === 'SELECT' ? 'change' : 'input', { bubbles: true }));
    await tick();
  });
}
const warehouse = { id: 7, code: 'main', name: 'Main fixture warehouse' };
let policy = null;
const options = shop => ({ shop: { id: shop === 'xtrek' ? 2 : 1, code: shop, name: shop }, warehouses: [warehouse],
  statuses: [{ id: 1, name: 'Prijatá', type: 'Received' }, { id: 8, name: 'Odoslaná', type: 'Sent' }, { id: 2, name: 'Storno', type: 'Canceled' }, { id: 12, name: 'V riešení', type: 'Custom' }],
  status_hash: 'a'.repeat(64), policy, suggested_actions: { '1': 'reserve', '8': 'issue', '2': 'cancel', '12': 'review' },
  allowed_actions: { '1': ['review', 'reserve'], '8': ['review', 'issue'], '2': ['review', 'cancel'], '12': ['review', 'reserve'] } });
let nextAction = 'reserve'; let planReady = true; let nextCount = 1;
const makePreview = body => ({ id: body.request_id, preview_hash: 'b'.repeat(64), status: 'prepared', created_at: '2026-09-23T12:00:00Z', expires_at: '2100-01-01T00:00:00Z',
  shop_code: body.shop_code, warehouse, action: nextAction, order_revision: 0, policy_revision: 1,
  source: { order_number: body.order_number, uuid: 'remote-order-id', created_at: '2026-09-23T11:00:00Z', updated_at: '2026-09-23T11:30:00Z', origin: 'eshop', status_id: 1, paid: false, resolved: false, lines: [{ line_key: 'broken-line', code: 'BROKEN-CODE', title: 'Problematic fixture product' }] },
  plan: { ready: planReady, errors: planReady ? [] : [{ code: 'order_stock_insufficient_stock', product_id: 1 }, { code: 'order_stock_line_identity', line_key: 'broken-line' }],
    lines: Array.from({ length: nextCount }, (_, index) => ({ line_key: `line-${index}`, product_id: index + 1, sku: `SKU-${index}`, quantity: '2', old_allocation: '0', allocation: planReady ? '2' : '1', shortage: planReady ? '0' : '1' })),
    effects: Array.from({ length: nextCount }, (_, index) => ({ product_id: index + 1, sku: `SKU-${index}`, qty_on_hand: '7', qty_reserved: '2', avg_cost: '3.0001', total_value: '21.0007', old_allocation: '0', allocation: '2', shortage: planReady ? '0' : '1', issue_quantity: nextAction === 'issue' ? '2' : '0', issue_cost: nextAction === 'issue' ? '6.0002' : '0.0000', qty_on_hand_after: nextAction === 'issue' ? '5' : '7', qty_reserved_after: nextAction === 'issue' ? '2' : '4', total_value_after: nextAction === 'issue' ? '15.0005' : '21.0007' })),
    excluded_lines: [{ line_key: 'manual-1', code: '', title: 'Synthetic manual service', classification: 'manual' }] }, result: null,
});
const complete = preview => ({ ...preview, status: 'completed', result: { order_id: 44, order_number: preview.source.order_number,
  action: preview.action, stock_state: preview.action === 'issue' ? 'issued' : 'reserved', revision: 1,
  movements_created: preview.action === 'issue' ? preview.plan.effects.length : 0, lines: preview.plan.lines, effects: preview.plan.effects } });
const calls = []; let stored; let pendingPreview; let previewMode = 'valid'; let applyMode = 'success'; let rejectApply; let configMode = 'success';
global.fetch = async (path, init = {}) => {
  const body = init.body ? JSON.parse(init.body) : undefined;
  calls.push({ path, ...init, body });
  assert.equal(init.cache, 'no-store'); assert(init.headers.Authorization.startsWith('Bearer synthetic-'));
  if (path.includes('/options?')) return { ok: true, json: async () => options(new URL(path, dom.window.location).searchParams.get('shop_code')) };
  if (path.endsWith('/configure')) {
    assert.equal(init.method, 'POST'); assert.equal(body.confirmed, true); assert(!('starts_at' in body), 'Activation cannot supply a historical cutover');
    policy = { warehouse_id: 7, warehouse_code: 'main', starts_at: '2026-09-23T10:00:00Z', revision: policy ? policy.revision + 1 : 1, status_actions: body.status_actions };
    if (configMode === 'fail') throw new TypeError('Synthetic connection lost after configuration');
    return { ok: true, json: async () => options(body.shop_code) };
  }
  if (path.endsWith('/preview')) {
    assert.deepEqual(Object.keys(body).sort(), ['order_number', 'request_id', 'shop_code'], 'No customer lines or stock quantities come from the browser');
    stored = makePreview(body);
    if (previewMode === 'pending') return new Promise(resolve => { pendingPreview = value => resolve({ ok: true, json: async () => ({ ready: value.plan.ready, errors: value.plan.errors, preview: value }) }); });
    return { ok: true, json: async () => ({ ready: stored.plan.ready, errors: stored.plan.errors, preview: stored }) };
  }
  if (path.endsWith('/apply')) {
    assert.equal(init.method, 'POST'); assert.equal(body.preview_hash, stored.preview_hash); assert.equal(body.confirmed, true);
    assert.equal(body.physical_confirmed, stored.action === 'issue');
    if (applyMode === 'pending') return new Promise((resolve, reject) => { rejectApply = () => reject(new TypeError('Synthetic connection lost')); });
    if (applyMode === 'fail') throw new TypeError('Synthetic connection lost');
    stored = complete(stored); return { ok: true, json: async () => stored.result };
  }
  if (path.includes('/previews?')) return { ok: true, json: async () => ({ previews: stored ? [{ ...stored, order_number: stored.source.order_number }] : [] }) };
  assert.equal(init.method, 'GET'); assert.equal(path, `/api/order-stock/previews/${stored.id}`);
  return { ok: true, json: async () => stored };
};
const applies = () => calls.filter(call => call.path.endsWith('/apply'));
const configurations = () => calls.filter(call => call.path.endsWith('/configure'));

(async () => {
  unlockHub('');
  await act(async () => { root.render(React.createElement(MemoryRouter, { initialEntries: ['/orders/stock?shop=xtrek&order=WEB-1'] }, React.createElement(OrdersStockPage))); await tick(); });
  assert.equal(calls.length, 0);
  await input(document.querySelector('input[type="password"]'), 'synthetic-stock-token'); await click(button('unlock'));
  assert.equal(calls.length, 0, 'Unlock or audit link never starts background processing');
  assert.equal(field('shop').value, 'xtrek'); assert.equal(field('orderNumber').value, 'WEB-1');
  assert(button('preview').disabled);
  await click(button('loadOptions'));
  assert(calls.at(-1).path.endsWith('shop_code=xtrek'));
  assert(['1', '8', '2', '12'].every(id => actionSelect(id).value === 'review'), 'Suggestions are visible but every new status defaults to review');
  assert.deepEqual([...actionSelect('1').options].map(option => option.value), ['review', 'reserve'], 'Web received status cannot be configured as issue');
  assert.deepEqual([...actionSelect('8').options].map(option => option.value), ['review', 'issue']);
  assert.deepEqual([...actionSelect('2').options].map(option => option.value), ['review', 'cancel']);
  assert.deepEqual([...actionSelect('12').options].map(option => option.value), ['review', 'reserve'], 'Custom working statuses can explicitly enable reservations');
  assert.equal(field('warehouse').value, '');
  await input(field('warehouse'), 'main'); await input(actionSelect('1'), 'reserve');
  await click(field('configConfirm')); await input(actionSelect('8'), 'issue');
  assert(!field('configConfirm').checked && button('activate').disabled, 'Any config edit clears its confirmation');
  await input(actionSelect('2'), 'cancel'); await input(actionSelect('12'), 'reserve'); await click(field('configConfirm')); await click(button('activate'));
  assert.equal(configurations().length, 1); assert(field('warehouse').disabled, 'Activated warehouse is fixed');
  assert.equal(actionSelect('12').value, 'reserve', 'Allowed custom reservation mapping persists despite default review suggestion');
  await click(button('preview'));
  assert.equal(applies().length, 0); assert(document.body.textContent.includes('Synthetic manual service'));
  assert(document.body.textContent.includes('3.0001 EUR') && document.body.textContent.includes('21.0007'));
  assert(button('apply').disabled);
  await click(field('applyConfirm')); await click(button('apply'));
  assert.equal(applies().length, 1); assert.equal(applies()[0].body.physical_confirmed, false);
  assert(document.body.textContent.includes(t('completed')));

  nextAction = 'issue'; await input(field('orderNumber'), 'ISSUE-1'); await click(button('preview'));
  await click(field('applyConfirm')); assert(button('apply').disabled, 'Issue requires separate physical confirmation');
  await click(field('physicalConfirm')); assert(!button('apply').disabled);
  await input(field('orderNumber'), 'ISSUE-2');
  assert.equal(button('apply'), undefined, 'Order changes invalidate the preview and both confirmations');
  await click(button('preview')); await click(field('applyConfirm')); await click(field('physicalConfirm'));
  applyMode = 'pending'; const applyButton = button('apply');
  await act(async () => { applyButton.click(); applyButton.click(); await tick(); });
  assert.equal(applies().length, 2, 'Repeated immediate clicks cause only one request');
  assert(field('orderNumber').disabled && field('shop').disabled);
  await act(async () => { rejectApply(); await tick(); });
  assert(document.body.textContent.includes(t('applyUncertain')) && !button('apply'));
  assert(button('preview').disabled, 'Uncertain posting cannot be replaced by a new preview');
  stored = complete(stored); await click(button('recover'));
  assert(document.body.textContent.includes(t('completed'))); assert.equal(applies().length, 2, 'GET confirms a completed operation without another POST');

  await input(field('orderNumber'), 'RETRY-1'); await click(button('preview')); await click(field('applyConfirm')); await click(field('physicalConfirm'));
  applyMode = 'fail'; await click(button('apply')); const retryId = stored.id;
  await click(button('recover')); assert(!field('applyConfirm').checked && !field('physicalConfirm').checked);
  assert(button('apply').disabled, 'Prepared recovery requires fresh operator confirmations');
  await click(field('applyConfirm')); await click(field('physicalConfirm')); applyMode = 'success'; await click(button('apply'));
  assert.equal(stored.id, retryId); assert.equal(applies().length, 4);

  planReady = false; await input(field('orderNumber'), 'SHORTAGE-1'); await click(button('preview'));
  assert(document.body.textContent.includes(t('blocked'))); assert(button('apply').disabled && field('applyConfirm').disabled);
  assert(document.querySelector('[role="alert"]'), 'Blocked plans keep shortage diagnostics visible');
  const diagnostics = document.querySelector('[role="alert"]').textContent;
  assert(diagnostics.includes('BROKEN-CODE') && diagnostics.includes('Problematic fixture product') && diagnostics.includes('SKU-0'), 'Diagnostics identify the actual source product, not only a UUID');
  planReady = true; await input(field('orderNumber'), 'STALE-FORM'); previewMode = 'pending'; await click(button('preview'));
  const stale = pendingPreview, old = stored, request = calls.at(-1);
  await input(actionSelect('1'), 'review'); assert(request.signal.aborted);
  await act(async () => { stale(old); await tick(); });
  assert.equal(button('apply'), undefined, 'Config edit cannot be undone by a late preview response');
  assert(button('preview').disabled, 'Unsaved local policy edits block new previews');
  await click(field('configConfirm')); configMode = 'fail'; await click(button('saveConfig'));
  assert(document.body.textContent.includes(t('configUncertain')) && button('saveConfig').disabled);
  const configCount = configurations().length; await click(button('loadOptions'));
  assert.equal(configurations().length, configCount, 'Lost config response is recovered by GET, never automatic POST');
  configMode = 'success'; previewMode = 'valid';

  await input(field('orderNumber'), 'STALE-SHOP'); previewMode = 'pending'; await click(button('preview'));
  const staleShop = pendingPreview, oldShopPreview = stored, oldShopRequest = calls.at(-1);
  const beforeShopChange = calls.length; await input(field('shop'), 'biketrek');
  assert.equal(calls.length, beforeShopChange, 'Changing shops does not fetch or write automatically');
  assert(oldShopRequest.signal.aborted);
  await act(async () => { staleShop(oldShopPreview); await tick(); });
  assert.equal(button('apply'), undefined, 'A previous shop response cannot restore its stock operation');
  await input(field('shop'), 'xtrek'); await click(button('loadOptions')); previewMode = 'valid';

  nextCount = 201; await input(field('orderNumber'), 'LONG-ORDER'); await click(button('preview'));
  const effectTable = [...document.querySelectorAll('table')].find(table => table.textContent.includes(t('onHand')));
  assert.equal(effectTable.querySelectorAll('tbody tr').length, 100);
  await click(button('next')); assert.equal(effectTable.querySelectorAll('tbody tr').length, 100);
  await click(button('next')); assert.equal(effectTable.querySelectorAll('tbody tr').length, 1);
  await input(field('orderNumber'), 'STALE-TOKEN'); previewMode = 'pending'; await click(button('preview'));
  const staleToken = pendingPreview, oldTokenPreview = stored;
  await act(async () => { unlockHub('synthetic-replacement-token'); await tick(); staleToken(oldTokenPreview); await tick(); });
  assert.equal(button('apply'), undefined, 'Changing the shared token clears outstanding confirmation and ignores old response');
  const beforeRecent = calls.length; await click(button('loadRecent')); assert.equal(calls.length, beforeRecent + 1);
  stored = complete(stored); await click(button('openPreview'));
  assert(document.body.textContent.includes(t('completed'))); assert.equal(applies().length, 4);
  policy.status_actions['1'] = 'issue';
  await click(button('loadOptions'));
  assert.equal(actionSelect('1').value, 'review', 'Unsupported persisted action is reset to review');
  assert(button('preview').disabled && !field('configConfirm').checked, 'Correcting persisted policy requires explicit confirmation before processing');
  assert.equal(dom.window.localStorage.length, 0); assert.equal(dom.window.sessionStorage.length, 0);
  assert(!dom.window.location.href.includes('synthetic-'));
  await act(async () => { root.unmount(); await tick(); }); unlockHub('');
  console.log('Order stock UI behavior checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
