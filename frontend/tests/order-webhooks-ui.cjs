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
const { unlockHub, accessRevision, setHubUser, hubUser } = require('../src/api/access.ts');
const { checkedNotificationOperation } = require('../src/api/orderWebhooks.ts');
const root = createRoot(document.getElementById('root'));
const tick = () => new Promise(resolve => setTimeout(resolve, 5));
const text = key => i18n.t(`orderWebhooks.${key}`);
const element = suffix => document.querySelector(`[data-testid="order-webhooks-${suffix}"]`);
async function click(suffix) { assert(element(suffix), `Missing ${suffix}`); await act(async () => { element(suffix).click(); await tick(); }); }
async function input(suffix, value) {
  await act(async () => {
    const prototype = element(suffix).tagName === 'TEXTAREA' ? dom.window.HTMLTextAreaElement.prototype : dom.window.HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(prototype, 'value').set.call(element(suffix), value);
    element(suffix).dispatchEvent(new dom.window.Event('input', { bubbles: true })); await tick();
  });
}
let shop = 'biketrek', collector = false, connection = true, webhook = null, reviewCount = 0;
let mode = 'normal', pending, events = [], notificationFetch = null, renderVersion = 0;
const calls = [], busy = [];
const snapshot = (code = shop) => ({ shop: { code, name: code }, webhook, collector_enabled: collector, connection_matches: connection,
  pending_count: 0, review_count: reviewCount, events, external_write_enabled: false });
const writes = () => calls.filter(call => call.method === 'POST');
global.fetch = async (path, init = {}) => {
  const body = init.body ? JSON.parse(init.body) : undefined;
  calls.push({ path, ...init, body });
  assert(path.startsWith('/api/order-webhooks/'), 'Only webhook administration endpoints are used');
  assert.equal(init.cache, 'no-store'); assert.equal(init.redirect, 'error');
  assert(init.headers.Authorization?.startsWith('Bearer synthetic-') || hubUser()?.role === 'admin');
  if (path.includes('/notifications/')) {
    assert(notificationFetch, 'Notification operations require an explicit test fixture');
    return notificationFetch(path, init, body);
  }
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
  } else if (path.endsWith('/resolve-registration')) {
    assert.deepEqual(Object.keys(body).sort(), ['confirmed', 'event', 'expected_revision', 'intent_id', 'note', 'remote_request_finished_confirmed', 'shop_code']);
    assert.equal(body.remote_request_finished_confirmed, true); assert(body.note.trim().length >= 10);
    assert(webhook.pending_mutations.some(item => item.event === body.event && item.intent_id === body.intent_id), 'Recovery uses the current exact pending request');
    webhook = { ...webhook, registration_status: 'pending', pending_mutations: webhook.pending_mutations.filter(item => item.intent_id !== body.intent_id) };
    if (mode === 'lost-resolution') return new Promise((resolve, reject) => { pending = () => reject(new TypeError('Synthetic lost resolution response')); });
  } else if (path.endsWith('/register')) webhook = { ...webhook, registration_status: 'pending' };
  else if (path.endsWith('/retry')) reviewCount = 0;
  else assert.fail('Unexpected endpoint');
  return { ok: true, json: async () => snapshot() };
};
async function render() { await act(async () => { root.render(React.createElement(OrderWebhooksPanel,
  { key: `${accessRevision()}:${shop}:${renderVersion}`, shop, blocked: false, onBusy: value => busy.push(value) })); await tick(); }); }

const notification = suffix => document.querySelector(`[data-testid="order-notification-${suffix}"]`);
async function notificationClick(suffix) { assert(notification(suffix), `Missing notification ${suffix}`); await act(async () => { notification(suffix).click(); await tick(); }); }
async function notificationNote(value) {
  await act(async () => {
    Object.getOwnPropertyDescriptor(dom.window.HTMLTextAreaElement.prototype, 'value').set.call(notification('note'), value);
    notification('note').dispatchEvent(new dom.window.Event('input', { bubbles: true })); await tick();
  });
}

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
  assert(!element('resolutions'), 'Routine setup has no exceptional recovery form');
  webhook = { ...webhook, registration_status: 'failed', pending_mutations: [
    { event: 'create', method: 'POST', intent_id: '00000000-0000-4000-8000-000000000001', started_at: '2026-10-28T15:01:00Z' },
    { event: 'update', method: 'PUT', intent_id: '00000000-0000-4000-8000-000000000002', started_at: '2026-10-28T15:02:00Z' },
  ] };
  await click('load'); assert(element('resolutions'));
  assert(element('resolution-create-submit').disabled);
  await click('resolution-create-finished');
  await input('resolution-create-note', 'short'); assert(element('resolution-create-submit').disabled, 'Confirmation without sufficient evidence cannot release a pending request');
  await input('resolution-create-note', '  Upgates support confirmed the original request has finished.  ');
  await click('resolution-create-finished'); assert(element('resolution-create-submit').disabled, 'Evidence without explicit finished confirmation cannot release a pending request');
  await click('resolution-create-finished'); assert(!element('resolution-create-submit').disabled);
  const beforeResolution = writes().length; mode = 'lost-resolution';
  await act(async () => { element('resolution-create-submit').click(); element('resolution-create-submit').click(); await tick(); });
  assert.equal(writes().length, beforeResolution + 1, 'Double recovery confirmation sends only one request');
  assert.equal(calls.at(-1).body.note, 'Upgates support confirmed the original request has finished.');
  await act(async () => { pending(); await tick(); });
  assert(element('resolution-create-submit').disabled && element('resolution-update-note').disabled, 'Lost recovery response blocks further mutations');
  const beforeResolutionRead = writes().length; mode = 'normal'; await click('load');
  assert.equal(writes().length, beforeResolutionRead, 'Recovery confirmation response is resolved by GET, never replayed');
  assert(!element('resolution-create-submit') && element('resolution-update-submit').disabled, 'A resolved POST cannot be confirmed again and remaining PUT requires its own evidence');
  await input('resolution-update-note', 'Upgates confirmed that the pending update completed.'); await click('resolution-update-finished');
  await click('resolution-update-submit');
  assert.equal(calls.at(-1).body.event, 'update'); assert.equal(calls.at(-1).body.intent_id, '00000000-0000-4000-8000-000000000002');
  assert(!element('resolutions'), 'The recovery panel disappears after all exact requests have been resolved');
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

  // A deleted, never-managed order is explicitly reviewed; the browser never writes stock.
  setHubUser(null); unlockHub('synthetic-notification-session'); shop = 'biketrek'; mode = 'normal'; collector = true;
  webhook = { enabled: true, revision: 3, public_base_url: 'https://hub.example.test/api', catchup_interval_seconds: 3600,
    registration_status: 'registered', registered_at: '2026-11-09T10:00:00Z', pending_mutations: [], registered_events: {} };
  const sourceEvent = () => ({ order_number: 'SYNTHETIC-DELETE-1', event: 'delete', generation: 4, notification_hash: 'd'.repeat(64),
    notification_at: '2026-11-09T09:55:00Z', last_received_at: '2026-11-09T10:00:00Z', last_completed_at: null,
    status: 'review', attempts: 1, last_error: 'order_webhook_order_not_found' });
  events = [sourceEvent()]; reviewCount = 1;
  const operations = new Map(); let notificationMode = 'normal', heldNotification;
  const retainedKey = () => `hub.order-webhook-notification.${shop}.${encodeURIComponent(events[0].order_number)}`;
  const retained = () => JSON.parse(dom.window.sessionStorage.getItem(retainedKey()) || 'null');
  const response = value => ({ ok: true, json: async () => structuredClone(value) });
  const notificationCalls = () => calls.filter(call => call.path.includes('/notifications/'));
  notificationFetch = async (path, init, body) => {
    if (path.includes('/history?')) {
      const value = { shop_code: shop, order_number: events[0].order_number, limit: 50,
        operations: [...operations.values()].filter(value => value.phase === 'completed').reverse() };
      if (notificationMode === 'wrong-history-scope') value.shop_code = 'xtrek';
      return response(value);
    }
    if (path.endsWith('/preview')) {
      assert.equal(init.method, 'POST');
      assert.deepEqual(Object.keys(body).sort(), ['action', 'note', 'order_number', 'request_id', 'shop_code']);
      assert.equal(retained().request_id, body.request_id, 'The UUID is durably retained before preview is sent');
      assert.equal(retained().preview_hash, null); assert.equal(retained().note, body.note);
      if (notificationMode === 'local-history') return { ok: false, status: 409, json: async () => ({ detail: { code: 'order_webhook_resolution_local_history' } }) };
      const value = { ...body, actor: hubUser()?.id != null ? `user:${hubUser().id}` : 'operator-token', phase: 'prepared',
        preview_hash: 'a'.repeat(64), created_at: '2026-11-09T10:01:00Z', expires_at: '2026-11-09T10:16:00Z',
        event: structuredClone(events[0]), after_status: body.action === 'reopen' ? 'review' : 'completed', stock_effect: 'none',
        evidence: { kind: body.action === 'reopen' ? 'reopen_manual_ack' : 'absent_delete', checked_at: '2026-11-09T10:01:00Z',
          active_absent: body.action === 'reopen' ? null : true, deleted_absent: body.action === 'reopen' ? null : true,
          no_local_history: body.action === 'reopen' ? null : true }, result: null };
      operations.set(body.request_id, structuredClone(value));
      if (notificationMode === 'lost-preview') throw new TypeError('Synthetic lost preview');
      if (notificationMode === 'missing-proof') value.evidence.deleted_absent = false;
      return response(value);
    }
    const id = path.match(/\/operations\/([^/?]+)/)?.[1], value = structuredClone(operations.get(id));
    assert(value, 'Recovery/apply uses an existing exact UUID');
    if (path.endsWith('/apply')) {
      assert.deepEqual(Object.keys(body).sort(), ['confirmed', 'preview_hash', 'shop_code']);
      assert.equal(body.confirmed, true); assert.equal(body.preview_hash, value.preview_hash);
      assert.equal(retained().request_id, id); assert.equal(retained().preview_hash, value.preview_hash, 'The hash is durably retained before apply');
      value.phase = 'completed'; value.result = { applied_at: '2026-11-09T10:02:00Z', event_status: value.after_status, event_generation: value.event.generation };
      operations.set(id, structuredClone(value));
      events = [{ ...events[0], status: value.after_status, last_error: value.action === 'reopen' ? 'order_webhook_order_not_found' : null,
        resolution: { request_id: id, action: value.action, note: value.note, actor: value.actor, applied_at: value.result.applied_at,
          event_generation: value.event.generation } }];
      reviewCount = value.after_status === 'review' ? 1 : 0;
      if (notificationMode === 'held-apply') return new Promise(resolve => { heldNotification = () => resolve(response(value)); });
      if (notificationMode === 'bad-ack') value.result.event_generation++;
      return response(value);
    }
    assert.equal(init.method, 'GET');
    if (notificationMode === 'bad-recovery') value.preview_hash = 'b'.repeat(64);
    return response(value);
  };
  await render(); const beforeNotificationLoad = notificationCalls().length; await click('load');
  assert.equal(notificationCalls().length, beforeNotificationLoad, 'Loading webhook state never performs an absence check');
  assert(notification('preview').disabled); await notificationNote('short'); assert(notification('preview').disabled);
  await notificationNote('  Order is absent and the historical delete needs closing.  ');
  assert(!notification('preview').disabled);
  assert.equal(notification('preview').closest('.action-control').querySelector('[data-action-effects]').dataset.actionEffects, 'hub-write upgates-read');
  const storagePrototype = dom.window.Storage.prototype, saveSetItem = storagePrototype.setItem;
  storagePrototype.setItem = () => { throw new Error('Synthetic denied storage'); };
  const beforeStorageFailure = notificationCalls().length; await notificationClick('preview');
  assert.equal(notificationCalls().length, beforeStorageFailure, 'No request is sent when durable storage fails');
  storagePrototype.setItem = saveSetItem; renderVersion++; await render(); await click('load');
  await notificationNote('Order is absent and the historical delete needs closing.');
  notificationMode = 'local-history'; await notificationClick('preview');
  assert(!retained() && !notification('preview-result'), 'Local-history conflict cannot be acknowledged');
  notificationMode = 'missing-proof'; await notificationClick('preview');
  assert(notification('pending') && !notification('confirm'), 'Malformed proof cannot unlock confirmation');
  const proofRequest = retained().request_id; notificationMode = 'normal'; await notificationClick('recover');
  assert(notification('preview-result') && notification('confirm').disabled); assert.equal(retained().request_id, proofRequest);
  await notificationClick('confirmation'); notificationMode = 'bad-ack';
  const beforeApply = notificationCalls().filter(call => call.path.endsWith('/apply')).length;
  await act(async () => { notification('confirm').click(); notification('confirm').click(); await tick(); });
  assert.equal(notificationCalls().filter(call => call.path.endsWith('/apply')).length, beforeApply + 1, 'Repeated confirmation sends the same operation only once');
  assert(notification('pending') && !notification('saved') && retained(), 'Malformed 200 cannot be treated as success or discard the UUID');
  notificationMode = 'bad-recovery'; await notificationClick('recover');
  assert(notification('pending') && retained(), 'A different recovered hash cannot resolve the pending operation');
  notificationMode = 'normal'; const beforeRecover = writes().length; await notificationClick('recover');
  assert.equal(writes().length, beforeRecover, 'Lost/malformed apply is recovered through GET only');
  assert(!retained() && notification('saved')); assert.equal(element('review').textContent, '0');
  assert(notification('panel').textContent.includes(text('notifications.actions.reopen')));
  await notificationClick('history'); assert(notification('history-result').textContent.includes('historical delete'));
  const originalHistory = notification('history-result').textContent;
  notificationMode = 'wrong-history-scope'; await notificationClick('history');
  assert.equal(notification('history-result').textContent, originalHistory, 'Foreign-shop history is rejected');
  notificationMode = 'normal'; await notificationNote('Reopening the manual acknowledgement for another review.'); await notificationClick('preview');
  assert.equal(notificationCalls().at(-1).body.action, 'reopen');
  assert.equal(notification('confirm').closest('.action-control').querySelector('[data-action-effects]').dataset.actionEffects, 'hub-write');
  await notificationClick('confirmation'); await notificationClick('confirm');
  assert.equal(events[0].status, 'review'); assert(!retained());
  assert(notification('panel').textContent.includes(text('notifications.actions.acknowledge_absent_delete')));

  // A lost preview is recovered after remount using its original UUID, then only explicit confirmation applies it.
  notificationMode = 'lost-preview'; await notificationNote('The same exact deleted order was checked for this decision.'); await notificationClick('preview');
  const lostPreviewId = retained().request_id; renderVersion++; await render(); await click('load');
  assert(notification('pending') && !notification('preview'));
  notificationMode = 'normal'; const writesBeforePreviewRecovery = writes().length; await notificationClick('recover');
  assert.equal(retained().request_id, lostPreviewId); assert.equal(writes().length, writesBeforePreviewRecovery);
  assert(notification('preview-result') && notification('confirm').disabled);
  await notificationClick('cancel'); assert(!retained());

  // A retained operation from a cookie login cannot be confirmed by another actor.
  setHubUser({ id: 11, username: 'admin-one', display_name: 'Admin One', role: 'admin', active: true }); await render(); await click('load');
  await notificationNote('The original administrator recorded this review reason.'); await notificationClick('preview');
  assert.equal(retained().actor, 'user:11');
  setHubUser({ id: 12, username: 'admin-two', display_name: 'Admin Two', role: 'admin', active: true }); await render(); await click('load');
  const beforeActorRecovery = notificationCalls().length;
  assert(notification('recover').disabled && notification('panel').textContent.includes(text('notifications.actorChanged')));
  await notificationClick('recover'); assert.equal(notificationCalls().length, beforeActorRecovery);
  setHubUser({ id: 11, username: 'admin-one', display_name: 'Admin One', role: 'admin', active: true }); await render(); await click('load');
  await notificationClick('recover'); await notificationClick('confirmation');
  notificationMode = 'held-apply'; await notificationClick('confirm');
  const heldId = retained().request_id;
  events = [{ ...sourceEvent(), event: 'update', generation: 5, notification_hash: 'e'.repeat(64), status: 'pending', last_error: null }]; reviewCount = 0;
  setHubUser({ id: 12, username: 'admin-two', display_name: 'Admin Two', role: 'admin', active: true }); await render(); await click('load');
  assert(notification('pending') && notification('recover').disabled, 'A newer update retains the original actor-bound delete recovery');
  await act(async () => { heldNotification(); await tick(); });
  assert(!notification('saved') && retained().request_id === heldId, 'An old session response cannot clear or display the new session request state');
  notificationMode = 'normal'; setHubUser({ id: 11, username: 'admin-one', display_name: 'Admin One', role: 'admin', active: true }); await render(); await click('load');
  assert(notification('recover') && !notification('preview'), 'Reload still offers recovery of the original DELETE UUID after a newer update');
  const beforeNewGenerationRecovery = writes().length; await notificationClick('recover'); assert(!retained());
  assert.equal(writes().length, beforeNewGenerationRecovery); assert.equal(events[0].generation, 5); assert.equal(events[0].event, 'update');
  assert(!notification('panel'), 'The current update remains unchanged after the older completed decision is recovered');

  // Binding checks cover all acknowledgement identities, including a same-generation event hash.
  const completed = [...operations.values()].find(value => value.phase === 'completed');
  const binding = { request_id: completed.request_id, shop_code: completed.shop_code, order_number: completed.order_number,
    action: completed.action, actor: completed.actor, note: completed.note, preview_hash: completed.preview_hash,
    event_generation: completed.event.generation, notification_hash: completed.event.notification_hash };
  for (const mutate of [value => { value.shop_code = 'xtrek'; }, value => { value.order_number += '-other'; },
    value => { value.request_id = '00000000-0000-4000-8000-000000000099'; }, value => { value.actor = 'user:99'; },
    value => { value.action = 'reopen'; }, value => { value.preview_hash = 'f'.repeat(64); },
    value => { value.event.generation++; }, value => { value.event.notification_hash = 'c'.repeat(64); },
    value => { value.after_status = 'review'; }, value => { value.stock_effect = 'issue'; }, value => { value.result = null; }]) {
    const malformed = structuredClone(completed); mutate(malformed);
    assert.throws(() => checkedNotificationOperation(malformed, binding, true), 'A malformed or foreign acknowledgement must remain unresolved');
  }
  events = [{ ...sourceEvent(), status: 'completed', generation: 5, resolution: { request_id: completed.request_id,
    action: 'acknowledge_absent_delete', note: completed.note, actor: completed.actor,
    applied_at: completed.result.applied_at, event_generation: 4 } }]; await click('load');
  assert(!notification('preview') && notification('history'), 'Reopen is unavailable for an acknowledgement of another generation');
  events = [{ ...events[0], event: 'update' }]; await click('load');
  assert(!notification('panel'), 'Ordinary updates have no delete recovery control');
  await act(async () => { root.unmount(); await tick(); }); setHubUser(null);
  console.log('Order webhook UI setup, delete review/reopen, durable UUID recovery, strict acknowledgements and session isolation checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
