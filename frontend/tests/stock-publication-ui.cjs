/* Synthetic publication API only. Never contacts shops or publishes real stock. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'https://hub.example.test/stock/publication' });
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
const { StockPublicationPage } = require('../src/pages/StockPublicationPage.tsx');
const { unlockHub } = require('../src/api/access.ts');
const root = createRoot(document.getElementById('root'));
const tick = () => new Promise(resolve => setTimeout(resolve, 5));
const t = key => i18n.t(`stockPublication.${key}`);
const element = id => document.querySelector(`[data-testid="${id}"]`);
const required = id => { const value = element(id); assert(value, `${id} exists`); return value; };
const cannotUse = id => !element(id) || element(id).disabled;
async function click(id) { await act(async () => { required(id).click(); await tick(); }); }
async function input(id, value) {
  await act(async () => {
    const target = required(id);
    const prototype = target.tagName === 'SELECT' ? dom.window.HTMLSelectElement.prototype : target.tagName === 'TEXTAREA' ? dom.window.HTMLTextAreaElement.prototype : dom.window.HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(prototype, 'value').set.call(target, value);
    target.dispatchEvent(new dom.window.Event(target.tagName === 'SELECT' ? 'change' : 'input', { bubbles: true }));
    await tick();
  });
}
const clone = value => JSON.parse(JSON.stringify(value));
const reply = value => ({ ok: true, json: async () => clone(value) });
const warehouse = { id: 7, code: 'main', name: 'Main fixture warehouse' };
let policy = { revision: 0, enabled: false, target_fingerprint: null };
let hold = null, serverWriteEnabled = false;
const options = shop => ({ shop: { code: shop, name: shop === 'xtrek' ? 'xTrek' : 'BIKETREK' }, warehouse,
  policy, hold, server_write_enabled: serverWriteEnabled, external_write_enabled: serverWriteEnabled && policy.enabled,
  values: { publication_batch_size: 20, publication_preview_minutes: 10 } });
const makeBatch = body => {
  const rows = body.skus.map((sku, index) => {
    const unknown = sku === 'UNKNOWN-BALANCE';
    const target = { code: sku, parent_code: 'PARENT-FIXTURE', variant_code: sku };
    return { sku, product_id: index + 1, target, quantity_known: !unknown,
      qty_on_hand: unknown ? null : index === 0 ? '3' : '120000000001', qty_reserved: unknown ? null : index === 0 ? '3' : '0',
      qty_available: unknown ? null : index === 0 ? '0' : '120000000001',
      errors: unknown ? ['stock_projection_balance_missing'] : [],
      remote: unknown ? null : { identity: { ...target, product_id: 11, variant_id: index + 20 }, quantity: index === 0 ? '7' : '0' } };
  });
  return { id: body.request_id, shop_code: body.shop_code, hold_id: hold.id, status: rows.some(row => !row.quantity_known) ? 'blocked' : 'prepared',
    preview_hash: 'a'.repeat(64), created_at: '2026-09-23T12:00:00Z', expires_at: '2100-01-01T00:00:00Z',
    queued_at: null, started_at: null, completed_at: null, error: null, result: null,
    preview_data: { shop_code: body.shop_code, warehouse, hold_id: hold.id, policy_revision: policy.revision,
      order_policy_revision: 7, configuration_hash: 'b'.repeat(64), target_fingerprint: policy.target_fingerprint,
      local_hash: 'c'.repeat(64), rows, created_at: '2026-09-23T12:00:00Z', expires_at: '2100-01-01T00:00:00Z' },
    items: rows.filter(row => row.quantity_known).map((row, position) => ({ id: position + 1, position, sku: row.sku,
      target: row.remote.identity, quantity: row.qty_available, before_quantity: row.remote.quantity, after_quantity: null,
      status: 'prepared', attempt_id: null, attempt_started_at: null, attempt_completed_at: null, verified_at: null,
      error: null, observation: null, acknowledgement: null, resolution: null })),
  };
};
const calls = [], batches = new Map(), previewRequests = new Map();
let holdSequence = 0;
let optionsMode = 'valid', previewMode = 'valid', submitMode = 'valid';
let resolveOptions, resolvePreview, rejectSubmit;
const posts = suffix => calls.filter(call => call.method === 'POST' && (!suffix || call.path.endsWith(suffix)));
global.fetch = async (path, init = {}) => {
  const body = init.body ? JSON.parse(init.body) : undefined;
  calls.push({ path, ...init, body });
  assert(path.startsWith('/api/stock-publication/'), 'The page uses only protected publication endpoints');
  assert.equal(init.cache, 'no-store'); assert(init.headers.Authorization.startsWith('Bearer synthetic-'));
  assert(!path.includes('synthetic-'), 'Credentials never enter request URLs');
  const url = new URL(path, dom.window.location.origin);
  if (url.pathname.endsWith('/options')) {
    assert.equal(init.method, 'GET');
    const snapshot = clone(options(url.searchParams.get('shop_code')));
    if (optionsMode === 'pending') return new Promise(resolve => { resolveOptions = () => resolve(reply(snapshot)); });
    return reply(snapshot);
  }
  if (url.pathname.endsWith('/configure')) {
    assert.deepEqual(body, { shop_code: 'xtrek', expected_revision: policy.revision, enabled: true, confirmed: true });
    policy = { revision: policy.revision + 1, enabled: body.enabled, target_fingerprint: 'synthetic-target-fingerprint' };
    return reply(options(body.shop_code));
  }
  if (url.pathname.endsWith('/holds')) {
    assert.deepEqual(body, { shop_code: 'xtrek', confirmed: true, external_writers_paused: true, orders_reconciled: true });
    holdSequence += 1;
    hold = { id: `00000000-0000-4000-8000-${String(holdSequence).padStart(12, '0')}`, warehouse_id: warehouse.id, shop_code: body.shop_code, active: true,
      assertions: { external_writers_paused: true, orders_reconciled: true }, created_at: '2026-09-23T12:00:00Z', closed_at: null };
    return reply(hold);
  }
  if (url.pathname.endsWith('/release')) {
    assert.equal(url.pathname, `/api/stock-publication/holds/${hold.id}/release`);
    assert.deepEqual(body, { confirmed: true, maintenance_completed: true });
    hold = { ...hold, active: false, closed_at: '2026-09-23T12:30:00Z' };
    return reply(hold);
  }
  if (url.pathname.endsWith('/preview')) {
    assert.equal(init.method, 'POST');
    assert.deepEqual(Object.keys(body).sort(), ['request_id', 'shop_code', 'skus'], 'Browser sends no invented quantity, remote identity or sibling list');
    assert.match(body.request_id, /^[0-9a-f]{8}-[0-9a-f-]{27}$/i);
    if (previewRequests.has(body.request_id)) assert.deepEqual(body, previewRequests.get(body.request_id), 'A request ID cannot change its exact SKU selection');
    else { previewRequests.set(body.request_id, clone(body)); batches.set(body.request_id, makeBatch(body)); }
    const batch = batches.get(body.request_id);
    if (previewMode === 'pending') return new Promise(resolve => { resolvePreview = () => resolve(reply(batch)); });
    return reply(batch);
  }
  if (url.pathname.endsWith('/batches')) {
    assert.equal(init.method, 'GET');
    const values = [...batches.values()].filter(value => value.shop_code === url.searchParams.get('shop_code'));
    return reply({ batches: values.map(({ items, preview_data, ...summary }) => summary), total: values.length, limit: 50, offset: 0 });
  }
  const match = url.pathname.match(/^\/api\/stock-publication\/batches\/([^/]+)(?:\/(submit|verify|resolve|cancel))?$/);
  if (match) {
    const batch = batches.get(match[1]); assert(batch, 'Only an existing synthetic batch can be accessed');
    const action = match[2];
    if (!action) { assert.equal(init.method, 'GET'); return reply(batch); }
    assert.equal(init.method, 'POST');
    if (action === 'submit') {
      assert.deepEqual(body, { preview_hash: batch.preview_hash, confirmed: true });
      assert(serverWriteEnabled && policy.enabled && hold.active);
      batches.set(batch.id, { ...batch, status: 'queued', queued_at: '2026-09-23T12:05:00Z' });
      if (submitMode === 'pending') return new Promise((resolve, reject) => { rejectSubmit = () => reject(new TypeError('Synthetic connection lost')); });
    } else if (action === 'verify') {
      assert.deepEqual(body, { confirmed: true });
      batches.set(batch.id, { ...batch, items: batch.items.map(item => ({ ...item, after_quantity: item.quantity,
        verified_at: '2026-09-23T12:10:00Z', observation: { quantity: item.quantity } })) });
    } else if (action === 'resolve') {
      assert.deepEqual(body, { confirmed: true, external_requests_finished: true });
      batches.set(batch.id, { ...batch, status: 'completed', completed_at: '2026-09-23T12:11:00Z', error: null,
        items: batch.items.map(item => ({ ...item, status: 'verified', after_quantity: item.quantity, error: null,
          resolution: { external_requests_finished: true } })) });
    } else {
      assert.deepEqual(body, { confirmed: true });
      batches.set(batch.id, { ...batch, status: 'cancelled', completed_at: '2026-09-23T12:04:00Z',
        items: batch.items.map(item => ({ ...item, status: 'cancelled' })) });
    }
    return reply(batches.get(batch.id));
  }
  throw new Error('Unexpected synthetic endpoint ' + path);
};

(async () => {
  unlockHub('');
  await act(async () => { root.render(React.createElement(MemoryRouter, { initialEntries: ['/stock/publication?shop=xtrek'] }, React.createElement(StockPublicationPage))); await tick(); });
  assert.equal(calls.length, 0, 'Mount never fetches or starts maintenance');
  await input('unlock-token', 'synthetic-publication-token'); await click('unlock');
  assert.equal(calls.length, 0, 'Unlocking never reads or publishes automatically');
  assert.equal(required('shop').value, 'xtrek');
  await click('load-options');
  assert.equal(required('load-options').closest('.action-control').querySelector('[data-action-effects]').dataset.actionEffects, 'hub-read');
  assert.equal(calls.length, 1); assert.equal(calls[0].path, '/api/stock-publication/options?shop_code=xtrek');
  assert.equal(posts().length, 0); assert(!required('policy-enabled').checked);
  assert(cannotUse('save-policy'));
  await click('policy-confirm'); await click('policy-enabled');
  assert(!required('policy-confirm').checked && cannotUse('save-policy'), 'Policy edits clear their previous confirmation');
  await click('policy-confirm'); await click('save-policy');
  assert.equal(posts('/configure').length, 1); assert.equal(policy.revision, 1);
  assert.equal(options('xtrek').external_write_enabled, false, 'A shop policy never overrides the server master flag');
  assert(cannotUse('create-hold'));
  await click('writers-paused'); assert(cannotUse('create-hold'), 'Pausing external writers alone does not authorize a maintenance hold');
  await click('orders-reconciled'); assert(!cannotUse('create-hold'));
  await click('create-hold'); assert.equal(posts('/holds').length, 1);
  assert(cannotUse('release-hold'), 'Maintenance needs its own closing confirmation');
  const beforeInvalid = calls.length;
  await input('skus', 'DUPLICATE\nDUPLICATE'); await click('preview');
  await input('skus', Array.from({ length: 101 }, (_, index) => `SKU-${index}`).join('\n')); await click('preview');
  assert.equal(calls.length, beforeInvalid, 'Invalid or duplicate SKU selection is rejected before any API call');
  await input('skus', 'KNOWN-ZERO\nUNKNOWN-BALANCE'); await click('preview');
  assert(cannotUse('submit'), 'One unknown row blocks publication of the entire selection');
  const unknownRow = [...document.querySelectorAll('tr')].find(row => row.textContent.includes('UNKNOWN-BALANCE'));
  assert(unknownRow && unknownRow.textContent.includes(t('unknown')), 'An unknown balance remains visible in the blocked snapshot');
  assert(!unknownRow.textContent.includes('stock_projection_balance_missing'), 'Missing balance is explained with translated text');
  await input('skus', 'KNOWN-ZERO\nSELECTED-VARIANT'); await click('preview');
  assert.deepEqual(posts('/preview').at(-1).body.skus, ['KNOWN-ZERO', 'SELECTED-VARIANT'], 'Only exact selected SKU leaves enter the preview');
  const itemTable = [...document.querySelectorAll('table')].find(table => table.querySelector('thead')?.textContent.includes(t('itemStatus')));
  assert(itemTable); const itemRows = [...itemTable.querySelectorAll('tbody tr')];
  assert.equal(itemRows.length, 2, 'No unselected variant or parent is added to the item list');
  assert.equal(itemRows[0].querySelectorAll('td')[3].textContent, '0', 'Known available zero stays an explicit quantity');
  assert.equal(itemRows[1].querySelectorAll('td')[3].textContent, '120000000001', 'Quantity strings remain exact and unformatted');
  assert.equal(itemRows[1].querySelectorAll('td')[2].textContent, '0', 'Known remote zero is distinct from an unknown observation');
  assert.equal(itemRows[0].querySelectorAll('td')[4].textContent, t('unknown'));
  await click('submit-confirm'); await click('submit');
  assert(cannotUse('submit') && posts('/submit').length === 0, 'The server master flag blocks publication even with an enabled shop policy');
  assert(document.body.textContent.includes(t('serverDisabled')));

  serverWriteEnabled = true; await click('load-options');
  assert(cannotUse('submit'), 'Reloading options never authorizes a previous draft');
  await click('cancel-confirm'); await click('cancel');
  assert.equal(posts('/cancel').length, 1); assert(document.body.textContent.includes(t('states.cancelled')));
  assert(hold.active, 'Cancelling a batch does not silently end maintenance');
  const cancelledId = required('batch-id').value; await click('preview');
  assert.notEqual(required('batch-id').value, cancelledId, 'An explicit new comparison gets a fresh request ID even when exact SKUs are unchanged');
  await click('submit-confirm'); assert(!cannotUse('submit')); submitMode = 'pending';
  await act(async () => { required('submit').click(); required('submit').click(); await tick(); });
  assert.equal(posts('/submit').length, 1, 'Immediate repeated clicks enqueue only one publication');
  const submittedId = required('batch-id').value;
  assert(required('shop').disabled && required('skus').disabled);
  await act(async () => { rejectSubmit(); await tick(); });
  assert(cannotUse('submit') && cannotUse('release-hold'), 'Lost queue response blocks another write and maintenance release');
  assert(document.body.textContent.includes(t('batchUncertain')));
  const afterLoss = posts().length; await tick(); assert.equal(posts().length, afterLoss, 'A lost submit response never resends automatically');
  await click('load-options');
  assert(cannotUse('submit') && cannotUse('release-hold'), 'Reloading policy options cannot resolve an unknown batch submission');
  let stored = batches.get(submittedId);
  batches.set(submittedId, { ...stored, status: 'running', started_at: '2026-09-23T12:06:00Z',
    items: stored.items.map(item => ({ ...item, status: 'sending', attempt_id: '00000000-0000-4000-8000-000000000002' })) });
  await click('recover-batch');
  assert.equal(calls.at(-1).method, 'GET'); assert.equal(posts().length, afterLoss, 'Lost submission recovers through a batch GET only');
  assert(cannotUse('release-hold') && cannotUse('verify'), 'An in-flight write cannot be verified or released prematurely');
  stored = batches.get(submittedId);
  batches.set(submittedId, { ...stored, status: 'uncertain', error: 'stock_publication_write_unconfirmed',
    items: stored.items.map(item => ({ ...item, status: 'uncertain', error: 'stock_publication_write_unconfirmed' })) });
  await click('recover-batch');
  assert(cannotUse('submit') && cannotUse('release-hold'));
  assert(cannotUse('resolve'), 'Observed stock alone does not confirm that external requests have finished');
  await input('batch-id', cancelledId); await click('recover-batch');
  assert(cannotUse('release-hold'), 'Opening an older terminal batch cannot hide a known unresolved batch in the same maintenance hold');
  await input('batch-id', submittedId); await click('recover-batch');
  await click('verify');
  assert.equal(posts('/verify').length, 1); assert.equal(posts('/submit').length, 1, 'Verification never invokes a second stock submission');
  assert.equal(batches.get(submittedId).status, 'uncertain');
  assert(cannotUse('resolve') && cannotUse('release-hold'), 'Matching readback keeps ambiguity until outstanding requests are explicitly closed');
  await click('requests-finished'); await click('resolve');
  assert.equal(posts('/resolve').length, 1); assert.equal(posts('/submit').length, 1);
  assert(document.body.textContent.includes(t('states.completed')));
  assert(cannotUse('release-hold'), 'Resolving a batch still requires the separate maintenance completion confirmation');
  await click('maintenance-completed'); await click('release-hold');
  assert.equal(posts('/release').length, 1); assert.equal(hold.active, false);

  await click('writers-paused'); await click('orders-reconciled'); await click('create-hold');
  previewMode = 'pending'; await input('skus', 'OLD-SKU'); await click('preview');
  const stalePreview = resolvePreview, stalePreviewRequest = calls.at(-1);
  await input('skus', 'NEW-SKU'); assert(stalePreviewRequest.signal.aborted);
  await act(async () => { stalePreview(); await tick(); });
  assert(cannotUse('submit') && !document.querySelector('tbody tr'), 'Changing exact SKU input discards delayed publication drafts');
  previewMode = 'valid'; await click('preview'); await click('submit-confirm');
  await input('skus', 'ANOTHER-SKU');
  assert(cannotUse('submit') && !element('submit-confirm'), 'SKU edits invalidate both the preview and its confirmation');

  previewMode = 'pending'; await click('preview');
  const oldShopPreview = resolvePreview, oldShopRequest = calls.at(-1), beforeShop = calls.length;
  await input('shop', 'biketrek'); assert.equal(calls.length, beforeShop); assert(oldShopRequest.signal.aborted);
  await act(async () => { oldShopPreview(); await tick(); });
  assert(cannotUse('submit') && !element('skus'), 'A delayed preview cannot reappear after switching shops');
  await input('shop', 'xtrek'); await click('load-options');
  previewMode = 'pending'; await input('skus', 'TOKEN-OLD-SKU'); await click('preview');
  const oldTokenPreview = resolvePreview, oldTokenRequest = calls.at(-1), beforeToken = calls.length;
  await act(async () => { unlockHub('synthetic-replacement-token'); await tick(); oldTokenPreview(); await tick(); });
  assert(oldTokenRequest.signal.aborted && calls.length === beforeToken);
  assert(cannotUse('submit') && !element('skus') && required('batch-id').value === '', 'Changing shared credentials clears drafts, identities and confirmations');
  const beforeRecent = calls.length; await click('load-batches');
  assert.equal(calls.length, beforeRecent + 1); assert.equal(calls.at(-1).method, 'GET');
  assert.equal(posts('/submit').length, 1, 'Loading persisted history never publishes again');
  assert.equal(dom.window.localStorage.length, 0); assert.equal(dom.window.sessionStorage.length, 0);
  assert(!dom.window.location.href.includes('synthetic-'));
  const beforeIdle = calls.length; await tick(); assert.equal(calls.length, beforeIdle, 'The page never polls or writes in the background');
  await act(async () => { root.unmount(); await tick(); }); unlockHub('');
  console.log('Stock publication UI behavior checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
