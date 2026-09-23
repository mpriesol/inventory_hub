/* Behavioral checks use synthetic orders and never contact Upgates. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'https://hub.example.test/orders' });
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
const { OrdersAuditPage } = require('../src/pages/OrdersAuditPage.tsx');
const { unlockHub, hubUnlocked } = require('../src/api/access.ts');
const { unlockAi, aiUnlocked, aiRequest } = require('../src/api/aiContent.ts');
const root = createRoot(document.getElementById('root'));
const tick = () => new Promise(resolve => setTimeout(resolve, 5));
const t = key => i18n.t(`orderAudit.${key}`);
const button = key => [...document.querySelectorAll('button')].find(element => element.textContent === t(key));
const select = key => [...document.querySelectorAll('label')].find(element => element.textContent.startsWith(t(key))).querySelector('select');
async function click(element) { assert(element, 'Element exists'); await act(async () => { element.click(); await tick(); }); }
async function input(element, value) {
  await act(async () => {
    const prototype = element.tagName === 'SELECT' ? dom.window.HTMLSelectElement.prototype : dom.window.HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(prototype, 'value').set.call(element, value);
    element.dispatchEvent(new dom.window.Event(element.tagName === 'SELECT' ? 'change' : 'input', { bubbles: true }));
    await tick();
  });
}
const fixture = (shop = 'biketrek', orderNumber = 'TEST-BT-01', page = 1) => ({
  shop, fetched_at: '2026-09-23T10:30:00Z', read_only: true, page, number_of_pages: 2, number_of_items: 101, has_more: page < 2,
  summary: { orders: 1, lines: 6, mapped: 1, identified: 1, manual: 1, non_stock: 1, unresolved: 1, conflict: 1 }, warnings: [],
  orders: [{ order_number: orderNumber, origin: 'frontend', created_at: '2026-09-23T10:00:00Z', updated_at: null,
    status_id: 8, status_name: 'Odoslaná', status_type: 'dispatched', paid: true, resolved: true, delivered: true,
    candidate: 'review', candidate_reason: 'identity_review_required', warnings: [],
    lines: ['mapped', 'identified', 'manual', 'non_stock', 'unresolved', 'conflict'].map((classification, index) => ({
      line_key: `line-${index}`, code: classification === 'manual' ? '' : classification === 'identified' ? `HUB-${index}` : `SHOP-${index}`, title: `Fixture ${classification}`,
      ean: '', quantity: index === 0 ? '1.250' : '1', unit: index === 0 ? 'm' : 'ks', classification,
      product_id: index < 2 ? index + 1 : null, sku: index < 2 ? `HUB-${index}` : null,
      matched_by: classification === 'identified' ? 'shared_sku' : classification === 'mapped' ? 'shop_mapping' : null, reasons: [],
    })),
  }],
});
const calls = [];
let pending = null;
let defer = false;
let rejectAccess = false;
global.fetch = async (path, init = {}) => {
  calls.push({ path, ...init });
  if (path === '/api/ai-content/status') return { ok: true, json: async () => ({ enabled: true }) };
  assert(/^\/api\/shops\/(biketrek|xtrek)\/upgates\/orders\/audit\?days=(7|30|90)&page=\d+$/.test(path), 'Only the bounded audit endpoint is requested');
  assert.equal(init.method, 'GET', 'Audit never sends a write request');
  assert.equal(init.body, undefined);
  assert.equal(init.cache, 'no-store', 'Authenticated order payload is excluded from the browser HTTP cache');
  const url = new URL(path, dom.window.location.origin);
  const shop = url.pathname.split('/')[3];
  if (rejectAccess) return { ok: false, json: async () => ({ detail: { code: 'hub_access_required' } }) };
  if (defer) return new Promise(resolve => { pending = value => resolve({ ok: true, json: async () => value }); });
  return { ok: true, json: async () => fixture(shop, shop === 'biketrek' ? 'TEST-BT-01' : 'TEST-XT-01', Number(url.searchParams.get('page'))) };
};

(async () => {
  unlockHub('');
  await act(async () => { root.render(React.createElement(MemoryRouter, null, React.createElement(OrdersAuditPage))); await tick(); });
  assert.equal(calls.length, 0, 'Mounting the locked audit never loads orders');
  assert(button('unlock').disabled);
  await input(document.querySelector('input[type="password"]'), 'synthetic-audit-token');
  await click(button('unlock'));
  assert.equal(calls.length, 0, 'Entering credentials alone does not fetch orders');
  assert(aiUnlocked(), 'AI and audit share memory-only Hub credentials');
  assert.equal(document.querySelector('input[type="password"]'), null);
  assert(!document.body.innerHTML.includes('synthetic-audit-token'));
  await click(button('load'));
  assert.equal(calls.length, 1, 'One action requests one upstream page');
  assert.equal(calls[0].headers.Authorization, 'Bearer synthetic-audit-token');
  assert.equal(calls[0].path, '/api/shops/biketrek/upgates/orders/audit?days=30&page=1');
  assert(document.body.textContent.includes('TEST-BT-01'));
  assert(document.body.textContent.includes(t('readOnly')));
  assert(document.body.textContent.includes(t('manualHelp')));
  assert.equal(document.querySelectorAll('tbody tr').length, 6, 'All six outcomes, including discount lines, remain visible');
  const sharedSkuRow = [...document.querySelectorAll('tbody tr')].find(row => row.textContent.includes('Fixture identified'));
  assert(sharedSkuRow.textContent.includes(t('classification.identified')), 'A shared SKU without EAN is shown as identified');
  assert.equal(sharedSkuRow.querySelectorAll('code')[0].textContent, 'HUB-1');
  assert.equal(sharedSkuRow.querySelectorAll('code')[1].textContent, 'HUB-1', 'The shop leaf and canonical SKU stay the same');
  assert(!sharedSkuRow.textContent.includes('EAN:'));
  assert(document.body.textContent.includes('1.250 m'), 'Fractional quantity stays decimal text');
  assert(button('previous').disabled);
  await tick(); assert.equal(calls.length, 1, 'There is no automatic polling or eager next-page fetching');
  await click(button('next'));
  assert(calls.at(-1).path.endsWith('page=2'));
  assert(button('next').disabled, 'Last page cannot advance');
  await click(button('previous')); assert(calls.at(-1).path.endsWith('page=1'));

  const beforeShop = calls.length;
  await input(select('shop'), 'xtrek');
  assert(!document.body.textContent.includes('TEST-BT-01'), 'Changing shops removes the previous result immediately');
  assert.equal(calls.length, beforeShop, 'Shop change waits for explicit fetch');
  await click(button('load')); assert(document.body.textContent.includes('TEST-XT-01'));
  await input(select('period'), '7');
  assert(!document.body.textContent.includes('TEST-XT-01'), 'Changing the audit window clears prior rows');
  await click(button('load')); assert(calls.at(-1).path.includes('days=7&page=1'));

  defer = true;
  await click(button('load'));
  const staleShop = pending;
  const staleRequest = calls.at(-1);
  await input(select('shop'), 'biketrek');
  assert(staleRequest.signal.aborted, 'Changing shops aborts the outstanding request');
  await act(async () => { staleShop(fixture('xtrek', 'STALE-SHOP')); await tick(); });
  assert(!document.body.textContent.includes('STALE-SHOP'), 'Even a response ignoring abort cannot render for another shop');

  await click(button('load'));
  const staleCredential = pending;
  await act(async () => { unlockAi('replacement-fixture-token'); await tick(); });
  await act(async () => { staleCredential(fixture('biketrek', 'STALE-CREDENTIAL')); await tick(); });
  assert(!document.body.textContent.includes('STALE-CREDENTIAL'), 'AI-side credential change invalidates the audit response');
  defer = false;
  await click(button('load'));
  assert.equal(calls.at(-1).headers.Authorization, 'Bearer replacement-fixture-token');

  await click(button('lock'));
  assert(!aiUnlocked(), 'Locking the audit also clears AI credentials');
  assert(!document.body.textContent.includes('TEST-BT-01'), 'Lock removes fetched order data');
  await input(document.querySelector('input[type="password"]'), 'invalid-fixture-token');
  await click(button('unlock'));
  rejectAccess = true;
  await click(button('load'));
  assert(!hubUnlocked(), 'Rejected access removes the token from memory');
  assert(document.querySelector('[role="alert"]'), 'Rejected access is visible');
  assert(document.querySelector('input[type="password"]'));
  assert(!document.body.textContent.includes('TEST-BT-01'));
  assert.equal(dom.window.localStorage.length, 0, 'Neither token nor order payload persists in local storage');
  assert.equal(dom.window.sessionStorage.length, 0, 'Neither token nor order payload persists in session storage');
  assert(!dom.window.location.href.includes('token'));

  await act(async () => { unlockAi('ai-compatibility-fixture'); await tick(); });
  await aiRequest('/status');
  assert.equal(calls.at(-1).headers.Authorization, 'Bearer ai-compatibility-fixture', 'Existing AI request contract is preserved');
  await act(async () => { root.unmount(); await tick(); });
  unlockHub('');
  console.log('Order audit UI behavior checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
