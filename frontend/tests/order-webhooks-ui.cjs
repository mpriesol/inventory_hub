/* Synthetic webhook setup/recovery interactions. No live requests or stock writes. */
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
const { OrderWebhooksPanel } = require('../src/components/orders/OrderWebhooksPanel.tsx');
const { unlockHub, accessRevision, setHubUser } = require('../src/api/access.ts');
const root = createRoot(document.getElementById('root'));
const tick = () => new Promise(resolve => setTimeout(resolve, 5));
const text = key => i18n.t(`orderWebhooks.${key}`);
const element = suffix => document.querySelector(`[data-testid="order-webhooks-${suffix}"]`);
async function click(suffix) { assert(element(suffix), `Missing ${suffix}`); await act(async () => { element(suffix).click(); await tick(); }); }
async function input(suffix, value) {
  await act(async () => {
    Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, 'value').set.call(element(suffix), value);
    element(suffix).dispatchEvent(new dom.window.Event('input', { bubbles: true })); await tick();
  });
}
let shop = 'biketrek', collector = false, connection = true, webhook = null, reviewCount = 0;
let mode = 'normal', pending;
const calls = [], busy = [];
const snapshot = (code = shop) => ({ shop: { code, name: code }, webhook, collector_enabled: collector, connection_matches: connection,
  pending_count: 0, review_count: reviewCount, events: [], external_write_enabled: false });
const writes = () => calls.filter(call => call.method === 'POST');
global.fetch = async (path, init = {}) => {
  const body = init.body ? JSON.parse(init.body) : undefined;
  calls.push({ path, ...init, body });
  assert(path.startsWith('/api/order-webhooks/'), 'Only webhook administration endpoints are used');
  assert.equal(init.cache, 'no-store'); assert.equal(init.redirect, 'error');
  assert(init.headers.Authorization.startsWith('Bearer synthetic-'));
  if (path.includes('/status?')) {
    if (mode === 'pending-read') return new Promise(resolve => { pending = value => resolve({ ok: true, json: async () => value }); });
    if (mode === 'malformed') return { ok: true, json: async () => ({ shop: { code: shop }, webhook: {} }) };
    return { ok: true, json: async () => snapshot() };
  }
  assert.equal(body.shop_code, shop); assert.equal(body.confirmed, true);
  assert.equal(body.expected_revision, webhook?.revision ?? null);
  assert(!JSON.stringify(body).includes('secret'), 'The browser neither receives nor sends webhook secrets');
  if (path.endsWith('/configure')) {
    assert.deepEqual(Object.keys(body).sort(), ['catchup_interval_seconds', 'confirmed', 'enabled', 'expected_revision', 'public_base_url', 'shop_code']);
    webhook = { enabled: body.enabled, revision: (webhook?.revision || 0) + 1, public_base_url: body.public_base_url,
      catchup_interval_seconds: body.catchup_interval_seconds, registration_status: 'pending', registered_at: null,
      last_received_at: null, last_completed_at: null, last_error: null, next_registration_at: null,
      registration_attempts: 0, registered_events: {}, secret_configured: true };
    if (mode === 'lost-write') return new Promise((resolve, reject) => { pending = () => reject(new TypeError('Synthetic lost response')); });
  } else if (path.endsWith('/register')) webhook = { ...webhook, registration_status: 'pending' };
  else if (path.endsWith('/retry')) reviewCount = 0;
  else assert.fail('Unexpected endpoint');
  return { ok: true, json: async () => snapshot() };
};
async function render() { await act(async () => { root.render(React.createElement(OrderWebhooksPanel,
  { key: `${accessRevision()}:${shop}`, shop, blocked: false, onBusy: value => busy.push(value) })); await tick(); }); }

(async () => {
  await i18n.changeLanguage('sk'); unlockHub('synthetic-webhook-session'); await render();
  assert.equal(calls.length, 0, 'Opening the panel never calls Upgates or enables anything');
  await click('load'); assert.equal(writes().length, 0);
  assert(element('prerequisite').textContent.includes(text('collectionRequired')));
  assert(element('enabled').disabled, 'Disabled collection cannot be implicitly enabled through webhook setup');
  assert.equal(element('base-url').value, 'https://hub.example.test/api');
  assert.equal(element('interval').value, '3600');
  collector = true; connection = false; await click('load');
  assert(element('enabled').disabled && element('prerequisite').textContent.includes(text('connectionRequired')));
  connection = true; await click('load'); await click('enabled');
  await input('base-url', 'https://user:password@hub.example.test/api'); assert(element('save').disabled);
  await input('base-url', 'http://hub.example.test/api'); assert(element('save').disabled);
  await input('base-url', 'https://hub.example.test/api?secret=not-allowed'); assert(element('save').disabled);
  await input('base-url', 'https://hub.example.test/api');
  await input('interval', '299'); assert(element('save').disabled);
  await input('interval', '3600'); assert(!element('save').disabled);
  const scope = element('save').closest('.action-control').querySelector('[data-action-effects]');
  assert.equal(scope.dataset.actionEffects, 'hub-write queued-upgates upgates-read upgates-write');
  mode = 'lost-write';
  await act(async () => { element('save').click(); element('save').click(); await tick(); });
  assert.equal(writes().length, 1, 'Repeated clicks cannot duplicate a registration change');
  assert(busy.at(-1));
  await act(async () => { pending(); await tick(); });
  assert(element('save').disabled && document.body.textContent.includes(text('uncertain')));
  const beforeRecovery = writes().length; mode = 'normal'; await click('load');
  assert.equal(writes().length, beforeRecovery, 'An unknown write response is recovered with GET only');
  assert(element('enabled').checked && element('register').disabled, 'Pending registration cannot be submitted again');
  assert(!busy.at(-1));
  webhook = { ...webhook, registration_status: 'registered', registered_at: '2026-10-28T15:00:00Z', registered_events: { create: 1, update: 2, delete: 3 } };
  reviewCount = 2; await click('load');
  assert.equal(element('review').textContent, '2'); assert(element('retry'));
  await click('retry'); assert.equal(calls.at(-1).path, '/api/order-webhooks/retry'); assert(!element('retry'));
  await click('register'); assert.equal(calls.at(-1).path, '/api/order-webhooks/register'); assert(element('register').disabled);
  webhook = { ...webhook, enabled: false, registration_status: 'failed' }; collector = false;
  await click('load'); assert(!element('register').disabled, 'Failed remote deactivation can be retried with collection paused');
  await click('register'); assert.equal(calls.at(-1).path, '/api/order-webhooks/register');
  assert.equal(webhook.enabled, false, 'Retrying registration does not reactivate notification reception');
  const beforeWait = calls.length; await tick(); assert.equal(calls.length, beforeWait, 'UI never polls or registers automatically');
  mode = 'pending-read'; await click('load'); const stale = pending, staleRequest = calls.at(-1);
  shop = 'xtrek'; await render(); assert(staleRequest.signal.aborted);
  await act(async () => { stale(snapshot('biketrek')); await tick(); });
  assert(!element('save'), 'Late responses for the previous shop are discarded');
  mode = 'malformed'; await click('load');
  assert(!element('save') && document.body.textContent.includes(text('requestFailed')), 'Malformed successful responses cannot unlock writes');
  mode = 'normal'; webhook = null; await click('load');
  mode = 'pending-read'; await click('load'); const oldSession = pending;
  unlockHub('synthetic-webhook-replacement'); await render();
  await act(async () => { oldSession(snapshot()); await tick(); });
  assert(!element('save'), 'Changing credentials discards old private results');
  assert.equal(dom.window.localStorage.length, 0); assert.equal(dom.window.sessionStorage.length, 0);
  setHubUser({ id: 7, username: 'operator-fixture', display_name: 'Operator', role: 'operator', active: true }); await render();
  assert(element('load').disabled && document.body.textContent.includes(text('adminRequired')));
  await act(async () => { root.unmount(); await tick(); }); setHubUser(null);
  console.log('Order webhook UI validation, setup, queueing, read-only recovery and isolation checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
