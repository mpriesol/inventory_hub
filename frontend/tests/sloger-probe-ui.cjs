/* Synthetic Sloger preview only: reusable server credentials, bounded submission and recovery. */
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
let credentials = { configured: false, username: null }, failCredentialsGet = true;
let holdSave = true, resolveSave, saveMode = 'success';
let requestedSku = facts.sku;
const calls = [];
const result = status => ({ id: savedId, status, model: 'gpt-6-luna', result: status === 'lookup_completed' ? facts : null,
  usage: { input_tokens: 2000, output_tokens: 200 }, actual_usd: '0.000300', error: status === 'lookup_uncertain' ? 'ai_outcome_unknown' : null });
global.fetch = async (path, options = {}) => {
  const url = new URL(path, dom.window.location.origin); calls.push({ url, options });
  assert.equal(options.credentials, 'same-origin'); assert.equal(options.cache, 'no-store');
  assert.equal(options.headers['X-Hub-Request'], '1');
  assert.equal(options.headers.Authorization, 'Bearer synthetic-hub-token');
  assert(!url.href.includes(secret), 'Credentials never enter request URLs');
  if (url.pathname === '/api/suppliers/sloger/lookup/credentials') {
    if (options.method === 'GET') {
      assert.equal(options.body, undefined, 'Reading login status never submits a password');
      if (failCredentialsGet) throw new TypeError(secret);
      return reply(credentials);
    }
    assert.equal(options.method, 'PUT', 'Login save is explicit and separate from paid lookup');
    const body = JSON.parse(options.body);
    assert.deepEqual(body, { username: 'info@xtrek.sk', password: secret });
    if (saveMode === 'reject') return reply({ detail: { code: 'sloger_credentials_unavailable', message: secret } }, 503);
    credentials = { configured: true, username: body.username };
    if (saveMode === 'uncertain') throw new TypeError(secret);
    if (holdSave) return new Promise(resolve => { resolveSave = () => resolve(reply(credentials)); });
    return reply(credentials);
  }
  if (options.method === 'POST') {
    assert.equal(url.pathname, '/api/suppliers/sloger/lookup/preview', 'Only the read-only preview can receive a POST');
    const body = JSON.parse(options.body);
    savedId = body.request_id.replace(/-/g, '');
    assert.match(savedId, /^[0-9a-f]{32}$/);
    assert.deepEqual(Object.keys(body).sort(), ['request_id', 'sku'], 'A lookup reuses saved credentials without sending login fields');
    assert.equal(body.sku, requestedSku);
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
const saves = () => calls.filter(call => call.options.method === 'PUT');
const reads = () => calls.filter(call => call.options.method === 'GET' && /\/lookup\/[0-9a-f]{32}$/.test(call.url.pathname));
const assertPrivate = () => {
  assert(!document.body.textContent.includes(secret));
  assert(!route.includes(secret));
  assert(!JSON.stringify({ ...sessionStorage, ...localStorage }).includes(secret));
};

(async () => {
  unlockHub(''); await mount();
  assert.equal(calls.length, 0); assert(!find('sloger-start'), 'Locked page has no supplier credential form');
  await act(async () => { unlockHub('synthetic-hub-token'); await tick(); });
  assert(find('sloger-credentials-error'), 'Unknown stored state has an explicit error');
  assert(!find('sloger-password'), 'A status error is not treated as an unconfigured login');
  assert(required('sloger-start').disabled); assertPrivate();
  failCredentialsGet = false; await click('sloger-credentials-reload');
  assert.equal(required('sloger-username').value, 'info@xtrek.sk');
  assert.equal(required('sloger-sku').value, '7700860');
  assert(required('sloger-start').disabled);
  assert(required('sloger-credentials-save').disabled);
  await input('sloger-password', secret);
  await act(async () => { const save = required('sloger-credentials-save'); save.click(); save.click(); await tick(); });
  assert.equal(saves().length, 1, 'Rapid duplicate clicks save once');
  assert(!find('sloger-password'), 'Password clears immediately while saving');
  assert(required('sloger-start').disabled); assertPrivate();
  await act(async () => { resolveSave(); await tick(); });
  assert.equal(posts().length, 0, 'Saving credentials never starts a paid lookup');
  assert(required('sloger-credentials-status').textContent.includes('info@xtrek.sk'));
  assert(!find('sloger-password')); assert(!required('sloger-start').disabled);
  await act(async () => { const start = required('sloger-start'); start.click(); start.click(); await tick(); });
  assert.equal(posts().length, 1, 'Rapid duplicate clicks start one paid request');
  assert(!find('sloger-password'), 'Credentials disappear immediately while the request is pending');
  assert(required('sloger-refresh').disabled); assert(!find('sloger-new'));
  assert.equal(required('sloger-request-id').textContent, savedId);
  assert.equal(route, `/suppliers/sloger/b2b-test?request=${savedId}`);
  assertPrivate();
  await act(async () => { resolvePost('lookup_uncertain'); await tick(); });
  assert(!find('sloger-new'), 'Unknown paid outcome does not expose a fresh request');
  assert.equal(posts().length, 1, 'No polling or implicit retry follows an uncertain result');
  assertPrivate();

  await mount(route);
  assert.equal(posts().length, 1, 'Reloading recovery link never repeats POST');
  assert.equal(reads().length, 1, 'Recovery link performs one protected result GET');
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
  assert(!find('sloger-password'), 'The next product reuses the saved login without asking for a password');
  assert(!required('sloger-start').disabled);
  await click('sloger-credentials-change');
  assert.equal(required('sloger-password').value, '', 'Changing a login never retrieves the saved password');
  assert(required('sloger-start').disabled, 'Finish or cancel a credential edit before starting a lookup');
  await input('sloger-password', secret); await click('sloger-credentials-cancel');
  assert(!find('sloger-password')); assert(!required('sloger-start').disabled);
  await mount();
  assert(!find('sloger-password'), 'Reloading the page uses the server status');
  assert(!required('sloger-start').disabled);
  requestedSku = '7700861'; await input('sloger-sku', requestedSku);
  const completedId = savedId;
  holdPost = false; await click('sloger-start');
  assert.notEqual(savedId, completedId, 'A new UUID is created only after an explicit new lookup');
  assert(find('sloger-new'), 'A definite pre-request budget rejection allows a fresh form');
  assertPrivate();

  await click('sloger-new');
  await click('sloger-credentials-change'); await input('sloger-password', secret);
  holdSave = false; saveMode = 'uncertain'; await click('sloger-credentials-save');
  assert(find('sloger-credentials-error')); assert(!find('sloger-password')); assert(required('sloger-start').disabled);
  assert.equal(saves().length, 2, 'An uncertain save is never repeated automatically');
  assert.equal(posts().length, 2, 'An uncertain save starts no paid lookup'); assertPrivate();
  await click('sloger-credentials-reload');
  assert(!find('sloger-password')); assert(!required('sloger-start').disabled);
  assert.equal(saves().length, 2, 'Reloading status does not repeat a save');

  await click('sloger-credentials-change'); await input('sloger-password', secret);
  saveMode = 'reject'; await click('sloger-credentials-save');
  assert(find('sloger-credentials-error')); assert(!find('sloger-password')); assert(required('sloger-start').disabled);
  assertPrivate();
  failCredentialsGet = true; await click('sloger-credentials-reload');
  assert(!find('sloger-password'), 'Failed recovery of saved status does not offer a blank setup form');
  assert(required('sloger-start').disabled); assertPrivate();
  failCredentialsGet = false; await click('sloger-credentials-reload');
  assert(!required('sloger-start').disabled);

  failPost = true; await click('sloger-start');
  assert(!find('sloger-new'), 'Network failure keeps the existing request ID');
  readStatus = 'not_found'; await click('sloger-refresh');
  assert(!find('sloger-new'), 'A missing audit read does not implicitly restart a possibly pending request');
  assert.equal(posts().length, 3); assertPrivate();
  await act(async () => { unlockHub(''); await tick(); });
  assert(!find('sloger-result')); assert(!find('sloger-password'));
  await act(async () => { root.unmount(); await tick(); });
  console.log('Sloger probe UI: saved login, secret-free product requests, uncertain save recovery, duplicate submit, protected GET recovery and stock tiers passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
