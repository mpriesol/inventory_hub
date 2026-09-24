/* Local stock-history and dashboard behavior with synthetic APIs only. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'https://hub.example.test/stock/history' });
global.window = dom.window; global.document = dom.window.document;
Object.defineProperty(global, 'navigator', { value: dom.window.navigator, configurable: true });
global.HTMLElement = dom.window.HTMLElement; global.sessionStorage = dom.window.sessionStorage;
global.IS_REACT_ACT_ENVIRONMENT = true;
for (const ext of ['.ts', '.tsx']) require.extensions[ext] = (module, filename) => module._compile(ts.transpileModule(
  fs.readFileSync(filename, 'utf8').replace(/import\.meta\.env/g, '{}'),
  { compilerOptions: { jsx: ts.JsxEmit.React, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, esModuleInterop: true } }).outputText, filename);
require.extensions['.css'] = () => {};
const React = require('react'); const { act } = React; const { createRoot } = require('react-dom/client');
const { MemoryRouter, useLocation } = require('react-router-dom');
const i18n = require('../src/i18n/index.ts').default;
const { StockHistoryPage } = require('../src/pages/StockHistoryPage.tsx');
const { unlockHub, hubUnlocked } = require('../src/api/access.ts');
const root = createRoot(document.getElementById('root'));
const tick = () => new Promise(resolve => setTimeout(resolve, 8));
const find = id => document.querySelector(`[data-testid="${id}"]`);
const required = id => { const element = find(id); assert(element, `${id} exists`); return element; };
async function click(id) { await act(async () => { required(id).click(); await tick(); }); }
async function input(id, value) { await act(async () => {
  const element = typeof id === 'string' ? required(id) : id; const proto = element.tagName === 'SELECT' ? dom.window.HTMLSelectElement.prototype : dom.window.HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, 'value').set.call(element, value);
  element.dispatchEvent(new dom.window.Event(element.tagName === 'SELECT' ? 'change' : 'input', { bubbles: true })); await tick();
}); }
const reply = (value, status = 200) => ({ ok: status < 400, status, statusText: 'Fixture',
  headers: { get: () => 'application/json' }, text: async () => JSON.stringify(value), json: async () => value });
const oddSku = 'ABC/1? farba=červená';
const movement = (id, changes = {}) => ({ id, product_id: id, sku: oddSku, product_name: 'Synthetic item', warehouse_code: 'main', warehouse_name: 'Main warehouse',
  movement_type: 'SALE_OUT', quantity: '-1.000', balance_before: '2.000', balance_after: '1.000', unit_cost: null, total_cost: null, cost_basis: 'unknown',
  reason: 'Physical handover', reference_type: 'order', reference_id: 'order-A', reference_source: 'order', reference_label: 'BT-100', document: null,
  shop_code: 'biketrek', supplier_code: null, created_by: 'fixture-operator', created_at: '2026-09-24T10:00:00Z', ...changes });
const calls = []; let hold = false, resolveHeld, fail = false, rejectAccess = false, summaryFail = false, empty = false;
const reads = () => calls.filter(call => call.url.pathname.endsWith('/movements'));
const dataFor = params => ({ items: empty ? [] : Number(params.get('page')) === 2 ? [movement(49, { reference_label: 'PAGE-TWO' })] : [movement(100), movement(99, { sku: 'KNOWN-ZERO', unit_cost: '0.0000', total_cost: '0.0000' })],
  total: empty ? 0 : 51, page: Number(params.get('page')), page_size: Number(params.get('page_size')), snapshot_id: Number(params.get('snapshot_id') || 100), source: 'hub', upgates_calls: 0 });
global.fetch = async (path, options = {}) => {
  const url = new URL(path, dom.window.location.origin); calls.push({ path, url, ...options });
  assert.equal(options.method || 'GET', 'GET', 'History and dashboard never write data');
  assert(['/api/stock-history/options', '/api/stock-history/movements', '/api/stock/summary'].includes(url.pathname), 'Only local endpoints are used');
  if (url.pathname === '/api/stock/summary') {
    if (summaryFail) return reply({ detail: 'Unavailable' }, 500);
    return reply({ products_total: 3, inventory_value: null, low_stock_count: 1, open_managed_orders: 2,
      on_hand_total: 10, reserved_total: 2, available_total: 7, quarantined_total: 1 });
  }
  assert.equal(options.cache, 'no-store'); assert.equal(options.headers.Authorization, 'Bearer synthetic-history-token');
  if (rejectAccess) return reply({ detail: { code: 'hub_access_required' } }, 401);
  if (url.pathname.endsWith('/options')) return reply({ warehouses: [{ code: 'main', name: 'Main warehouse' }], movement_types: ['SALE_OUT', 'PURCHASE_IN'], date_timezone: 'Europe/Bratislava' });
  if (fail) throw new TypeError('Synthetic request failed');
  if (hold) return new Promise(resolve => { resolveHeld = data => resolve(reply(data || dataFor(url.searchParams))); });
  return reply(dataFor(url.searchParams));
};
async function mountHistory(path = '/stock/history') {
  await act(async () => { root.render(null); await tick(); });
  await act(async () => { root.render(React.createElement(MemoryRouter, { initialEntries: [path] }, React.createElement(StockHistoryPage))); await tick(); });
}
(async () => {
  unlockHub(''); await mountHistory(`/stock/history?sku=${encodeURIComponent(oddSku)}`);
  assert.equal(calls.length, 0, 'Locked history never starts a read');
  assert(required('history-unlock').disabled);
  await input('history-token', 'synthetic-history-token'); await click('history-unlock');
  assert.equal(calls.length, 2, 'Unlock loads options and one bounded history page');
  assert.equal(reads()[0].url.searchParams.get('sku'), oddSku, 'SKU query preserves slash, query characters and Unicode exactly');
  assert.equal(reads()[0].url.searchParams.get('page_size'), '50');
  assert.equal(required('history-sku').value, oddSku);
  assert.equal(document.querySelector('[data-action-effects]').dataset.actionEffects, 'hub-read');
  const rows = [...required('history-table').querySelectorAll('tbody tr')];
  assert.equal(rows[0].querySelectorAll('td')[10].textContent, i18n.t('stockHistory.unknown'), 'Unknown purchase cost remains visibly unknown');
  assert.equal(rows[1].querySelectorAll('td')[10].textContent, '0', 'Known zero cost remains distinct from unknown');
  assert.equal(rows[0].querySelector('a').getAttribute('href'), `/products/${encodeURIComponent(oddSku)}`);
  assert(rows[0].textContent.includes('BT-100') && rows[0].textContent.includes('fixture-operator'));
  assert(required('history-previous').disabled);

  await click('history-next');
  assert.equal(reads().at(-1).url.searchParams.get('page'), '2');
  assert.equal(reads().at(-1).url.searchParams.get('snapshot_id'), '100', 'Next page preserves the captured movement bound');
  assert(required('history-next').disabled);
  assert(required('history-table').textContent.includes('PAGE-TWO'));
  await input('history-query', 'NEW DOC');
  const beforeDraft = calls.length;
  await click('history-previous');
  assert.equal(reads().at(-1).url.searchParams.get('q'), null, 'Paging uses applied filters, not an unsubmitted draft');
  assert.equal(calls.length, beforeDraft + 1);
  await click('history-load');
  assert.equal(reads().at(-1).url.searchParams.get('q'), 'NEW DOC');
  assert.equal(reads().at(-1).url.searchParams.get('page'), '1');
  assert.equal(reads().at(-1).url.searchParams.get('snapshot_id'), null, 'Applying filters resets the captured movement bound');
  await input('history-page-size', '25');
  assert.equal(reads().at(-1).url.searchParams.get('page_size'), '25');
  assert.equal(reads().at(-1).url.searchParams.get('snapshot_id'), null);
  assert.equal(reads().at(-1).url.searchParams.get('q'), 'NEW DOC');
  await input(document.querySelector('select:not([data-testid])'), 'main');
  const dates = document.querySelectorAll('input[type="date"]');
  await input(dates[0], '2026-09-01'); await input(dates[1], '2026-09-24');
  await input('history-type', 'PURCHASE_IN'); await click('history-load');
  assert.equal(reads().at(-1).url.searchParams.get('movement_type'), 'PURCHASE_IN');
  assert.equal(reads().at(-1).url.searchParams.get('warehouse_code'), 'main');
  assert.equal(reads().at(-1).url.searchParams.get('date_from'), '2026-09-01');
  assert.equal(reads().at(-1).url.searchParams.get('date_to'), '2026-09-24');

  hold = true; const beforeDouble = reads().length;
  await act(async () => { const load = required('history-load'); load.click(); load.click(); await tick(); });
  assert.equal(reads().length, beforeDouble + 1, 'Double submit starts one request');
  const delayed = resolveHeld; const request = reads().at(-1);
  await act(async () => { unlockHub(''); await tick(); });
  assert(request.signal.aborted, 'Locking access aborts the pending history read');
  await act(async () => { delayed({ ...dataFor(request.url.searchParams), items: [movement(200, { reference_label: 'STALE-LOCKED' })] }); await tick(); });
  assert(!document.body.textContent.includes('STALE-LOCKED') && !find('history-table'), 'Late responses cannot reveal history after locking');
  hold = false; fail = true;
  await input('history-token', 'synthetic-history-token'); await click('history-unlock');
  assert(document.querySelector('[role="alert"]'), 'Request failure is shown as an error');
  assert(!document.body.textContent.includes(i18n.t('stockHistory.empty')), 'Failed load is not reported as an empty ledger');
  fail = false; empty = true; await click('history-load');
  assert(document.body.textContent.includes(i18n.t('stockHistory.empty')));
  assert(!document.querySelector('[role="alert"]'));
  empty = false; rejectAccess = true;
  await click('history-load');
  assert(!hubUnlocked(), 'Rejected access clears the invalid credential');
  assert(find('history-token') && document.querySelector('[role="alert"]'), 'Access failure remains explained above the unlock form');
  assert(!find('history-table'), 'Rejected access removes previously loaded history');
  rejectAccess = false; await input('history-token', 'synthetic-history-token'); await click('history-unlock');
  assert(find('history-table') && !document.querySelector('[role="alert"]'), 'Successful unlock clears the previous access error');

  await act(async () => { root.render(null); unlockHub(''); await tick(); });
  const { getRecentActivity, getDashboardStats } = require('../src/api/dashboard.ts');
  const beforeLockedActivity = calls.length;
  assert.deepEqual(await getRecentActivity(), [], 'Locked dashboard API does not invent recent events');
  assert.equal(calls.length, beforeLockedActivity, 'Locked dashboard activity makes no protected request');
  unlockHub('synthetic-history-token');
  const activity = await getRecentActivity();
  assert.equal(activity.length, 2);
  assert.equal(activity[0].id, '100'); assert.equal(activity[0].sku, oddSku); assert.equal(activity[0].reference, 'BT-100');
  assert.equal(activity[0].quantity, '-1.000');
  empty = true; assert.deepEqual(await getRecentActivity(), [], 'Empty ledger stays empty; no fake recent receipt or sync is synthesized');
  empty = false;
  const stats = await getDashboardStats();
  assert.equal(stats.totalProducts, 3); assert.equal(stats.openOrders, 2); assert.equal(stats.inventoryValue, null);
  assert.equal(stats.available, 7); assert.equal(stats.quarantined, 1);
  summaryFail = true; await assert.rejects(getDashboardStats(), /500/, 'Summary failure is not converted to zero stats'); summaryFail = false;

  const { DashboardPage } = require('../src/pages/DashboardPage.tsx');
  function LocationProbe() { return React.createElement('output', { 'data-testid': 'current-route' }, useLocation().pathname); }
  async function mountDashboard() {
    await act(async () => { root.render(null); await tick(); });
    await act(async () => { root.render(React.createElement(MemoryRouter, null,
      React.createElement(React.Fragment, null, React.createElement(DashboardPage), React.createElement(LocationProbe)))); await tick(); });
  }
  const statsCard = key => [...document.querySelectorAll('.text-sm')].find(el => el.textContent === i18n.t(`dashboard.${key}`))?.parentElement;
  await act(async () => { unlockHub(''); await tick(); });
  const beforeLockedDashboard = reads().length;
  await mountDashboard();
  assert.equal(reads().length, beforeLockedDashboard, 'Locked dashboard renders without reading protected movements');
  assert(document.body.textContent.includes(i18n.t('dashboard.unlockHistory')));
  assert(!document.body.textContent.includes(i18n.t('dashboard.noActivity')), 'Locked data is not presented as an empty ledger');
  assert(statsCard('inventoryValue').textContent.includes(i18n.t('stock.unknownValue')), 'Incomplete inventory value is not shown as zero');
  assert(!document.body.textContent.includes('+12') && !document.body.textContent.includes('NaN'));
  await act(async () => { unlockHub('synthetic-history-token'); await tick(); });
  assert.equal(reads().length, beforeLockedDashboard + 1);
  assert(document.body.textContent.includes('BT-100'), 'Dashboard renders the actual ledger reference');
  const activityLink = [...document.querySelectorAll('a')].find(el => el.textContent === oddSku);
  assert.equal(activityLink.getAttribute('href'), `/stock/movements?sku=${encodeURIComponent(oddSku)}`);
  assert.equal(statsCard('products').querySelector('.text-2xl').textContent, '3');
  assert.equal(statsCard('available').querySelector('.text-2xl').textContent, '7');
  const beforeNavigation = calls.length;
  await act(async () => { [...document.querySelectorAll('button')].find(el => el.textContent.includes(i18n.t('dashboard.settings'))).click(); await tick(); });
  assert.equal(required('current-route').textContent, '/settings/stock', 'Settings action leads to a real settings page');
  assert.equal(calls.length, beforeNavigation, 'Quick-action navigation performs no hidden stock upload');

  empty = true; await mountDashboard();
  assert(document.body.textContent.includes(i18n.t('dashboard.noActivity')));
  assert.equal(document.querySelectorAll('ul li').length, 0, 'Empty ledger renders no fabricated recent activity');
  empty = false; summaryFail = true; await mountDashboard();
  assert(document.querySelector('[role="alert"]'));
  assert.equal(statsCard('products'), undefined, 'Failed summary renders no zero-filled statistics');
  assert(!document.body.textContent.includes(i18n.t('dashboard.noActivity')), 'Failed dashboard is not reported as having no activity');
  summaryFail = false;

  hold = true; await mountDashboard();
  const delayedDashboard = resolveHeld;
  await act(async () => { unlockHub(''); await tick(); });
  await act(async () => { delayedDashboard({ ...dataFor(new URLSearchParams({ page: '1', page_size: '25' })), items: [movement(300, { reference_label: 'STALE-DASHBOARD' })] }); await tick(); });
  assert(!document.body.textContent.includes('STALE-DASHBOARD'), 'Late protected activity is discarded after locking');
  hold = false;
  await act(async () => { root.unmount(); });
  console.log('Stock history UI: access, encoded SKU, stable paging/filter bounds, unknown costs, duplicate submit and authentic dashboard data passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
