/* Synthetic Sloger preview only: credentials, bounded submission and read-only recovery. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'https://hub.example.test/suppliers/sloger/b2b-test' });
global.window = dom.window; global.document = dom.window.document;
Object.defineProperty(global, 'navigator', { value: dom.window.navigator, configurable: true });
global.HTMLElement = dom.window.HTMLElement; global.sessionStorage = dom.window.sessionStorage;
global.localStorage = dom.window.localStorage; global.IS_REACT_ACT_ENVIRONMENT = true;
for (const ext of ['.ts', '.tsx']) require.extensions[ext] = (module, filename) => module._compile(ts.transpileModule(
  fs.readFileSync(filename, 'utf8').replace(/import\.meta\.env/g, '{}'),
  { compilerOptions: { jsx: ts.JsxEmit.React, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, esModuleInterop: true } }).outputText, filename);
require.extensions['.css'] = () => {};
const React = require('react'); const { act } = React; const { createRoot } = require('react-dom/client');
const { MemoryRouter, useLocation } = require('react-router-dom');
const i18n = require('../src/i18n/index.ts').default;
const { SlogerProbePage } = require('../src/pages/SlogerProbePage.tsx');
const { unlockHub } = require('../src/api/access.ts');
const root = createRoot(document.getElementById('root'));
const tick = () => new Promise(resolve => setTimeout(resolve, 8));
const find = id => document.querySelector(`[data-testid="${id}"]`);
const required = id => { const result = find(id); assert(result, `${id} exists`); return result; };
async function click(id) { await act(async () => { required(id).click(); await tick(); }); }
async function input(id, value) { await act(async () => {
  const element = required(id);
  Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, 'value').set.call(element, value);
  element.dispatchEvent(new dom.window.Event('input', { bubbles: true })); await tick();
}); }
const reply = (value, status = 200) => ({ ok: status < 400, status, statusText: 'Fixture',
  headers: { get: () => 'application/json' }, text: async () => JSON.stringify(value), json: async () => value });
const secret = 'synthetic-sloger-password';
const facts = {
  sku: '7700860', product_name: 'Synthetic supplier product', manufacturer_code: 'MANUFACTURER-001',
  eans: ['8581234567890'], purchase_price: '10', retail_price: '12.3', currency: 'EUR',
  price_basis: 'net', retail_price_basis: 'gross', availability: 'Skladom: 0', available: false,
  quantity: '0', quantity_kind: 'exact', quantity_relation: 'equal',
  availability_tiers: [
    { label: 'Skladom', quantity: '0', quantity_kind: 'exact', quantity_relation: 'equal', lead_days: 0 },
    { label: 'Do 5 dní', quantity: '99', quantity_kind: 'minimum', quantity_relation: 'greater_than', lead_days: 5 },
    { label: 'Do 10 dní', quantity: '10', quantity_kind: 'minimum', quantity_relation: 'at_least', lead_days: 10 },
  ],
  source_url: 'https://www.sloger.sk/synthetic-product/', observed_at: '2026-10-12T12:00:00Z',
};
let savedId = '', route = '', resolvePost, holdPost = true, failPost = false, readStatus = 'lookup_running';
const calls = [];
const result = status => ({ id: savedId, status, model: 'gpt-6-luna', result: status === 'lookup_completed' ? facts : null,
  usage: { input_tokens: 2000, output_tokens: 200 }, actual_usd: '0.000300', error: status === 'lookup_uncertain' ? 'ai_outcome_unknown' : null });
global.fetch = async (path, options = {}) => {
  const url = new URL(path, dom.window.location.origin); calls.push({ url, options });
  assert.equal(options.credentials, 'same-origin'); assert.equal(options.cache, 'no-store');
  assert.equal(options.headers['X-Hub-Request'], '1');
  assert.equal(options.headers.Authorization, 'Bearer synthetic-hub-token');
  assert(!url.href.includes(secret), 'Credentials never enter request URLs');
  if (options.method === 'POST') {
    assert.equal(url.pathname, '/api/suppliers/sloger/lookup/preview', 'Only the read-only preview can receive a POST');
    const body = JSON.parse(options.body);
    savedId = body.request_id.replace(/-/g, '');
    assert.match(savedId, /^[0-9a-f]{32}$/);
    assert.equal(body.password, secret); assert.equal(body.username, 'info@xtrek.sk'); assert.equal(body.sku, facts.sku);
    if (failPost) throw new TypeError(secret);
    if (holdPost) return new Promise(resolve => { resolvePost = status => resolve(reply(result(status))); });
    return reply({ detail: { code: 'ai_monthly_budget', message: secret } }, 422);
  }
  assert.equal(options.method, 'GET');
  assert.equal(url.pathname, `/api/suppliers/sloger/lookup/${savedId}`, 'Recovery reads the original request');
  assert.equal(options.body, undefined, 'Recovery never resends B2B credentials');
  if (readStatus === 'not_found') return reply({ detail: { code: 'sloger_probe_not_found', message: secret } }, 404);
  return reply(result(readStatus));
};
function Location() { const value = useLocation(); route = value.pathname + value.search; return null; }
async function mount(path = '/suppliers/sloger/b2b-test') {
  await act(async () => { root.render(null); await tick(); });
  await act(async () => {
    root.render(React.createElement(MemoryRouter, { initialEntries: [path] },
      React.createElement(React.Fragment, null, React.createElement(SlogerProbePage), React.createElement(Location))));
    await tick();
  });
}
const posts = () => calls.filter(call => call.options.method === 'POST');
const assertPrivate = () => {
  assert(!document.body.textContent.includes(secret));
  assert(!route.includes(secret));
  assert(!JSON.stringify({ ...sessionStorage, ...localStorage }).includes(secret));
};

(async () => {
  unlockHub(''); await mount();
  assert.equal(calls.length, 0); assert(!find('sloger-start'), 'Locked page has no supplier credential form');
  await act(async () => { unlockHub('synthetic-hub-token'); await tick(); });
  assert.equal(required('sloger-username').value, 'info@xtrek.sk');
  assert.equal(required('sloger-sku').value, '7700860');
  assert(required('sloger-start').disabled);
  await input('sloger-password', secret);
  await act(async () => { const start = required('sloger-start'); start.click(); start.click(); await tick(); });
  assert.equal(posts().length, 1, 'Rapid duplicate clicks start one paid request');
  assert(!find('sloger-password'), 'Credentials disappear immediately while the request is pending');
  assert(required('sloger-refresh').disabled); assert(!find('sloger-new'));
  assert.equal(required('sloger-request-id').textContent, savedId);
  assert.equal(route, `/suppliers/sloger/b2b-test?request=${savedId}`);
  assertPrivate();
  await act(async () => { resolvePost('lookup_uncertain'); await tick(); });
  assert(!find('sloger-new'), 'Unknown paid outcome does not expose a fresh request');
  assert.equal(calls.length, 1, 'No polling or implicit retry follows an uncertain result');
  assertPrivate();

  await mount(route);
  assert.equal(posts().length, 1, 'Reloading recovery link never repeats POST');
  assert.equal(calls.length, 2, 'Recovery link performs one protected GET');
  assert(!find('sloger-new')); assert(!find('sloger-password'));
  readStatus = 'lookup_completed'; await click('sloger-refresh');
  assert.equal(posts().length, 1);
  assert.equal(required('sloger-own-quantity').textContent, '0', 'External availability never replaces own warehouse quantity');
  const tiers = required('sloger-tiers').textContent;
  assert(tiers.includes('> 99') && tiers.includes('≥ 10'), 'Strict and inclusive lower bounds remain distinct');
  assert(required('sloger-result').textContent.includes('MANUFACTURER-001'));
  assert(required('sloger-result').textContent.includes('8581234567890'));
  assert(required('sloger-result').textContent.includes(i18n.t('slogerProbe.basis.net')));
  assert(required('sloger-result').textContent.includes(i18n.t('slogerProbe.basis.gross')));
  assert.equal(required('sloger-result').querySelector('a').href, facts.source_url);
  assertPrivate();

  await click('sloger-new');
  assert.equal(required('sloger-password').value, '', 'A new request never reuses the previous password');
  assert(required('sloger-start').disabled);
  const completedId = savedId;
  holdPost = false; await input('sloger-password', secret); await click('sloger-start');
  assert.notEqual(savedId, completedId, 'A new UUID is created only after an explicit new lookup');
  assert(find('sloger-new'), 'A definite pre-request budget rejection allows a fresh form');
  assertPrivate();

  await click('sloger-new'); failPost = true;
  await input('sloger-password', secret); await click('sloger-start');
  assert(!find('sloger-new'), 'Network failure keeps the existing request ID');
  readStatus = 'not_found'; await click('sloger-refresh');
  assert(!find('sloger-new'), 'A missing audit read does not implicitly restart a possibly pending request');
  assert.equal(posts().length, 3); assertPrivate();
  await act(async () => { unlockHub(''); await tick(); });
  assert(!find('sloger-result')); assert(!find('sloger-password'));
  await act(async () => { root.unmount(); await tick(); });
  console.log('Sloger probe UI: duplicate submit, password clearing, protected GET recovery, budgets and separate stock tiers passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
