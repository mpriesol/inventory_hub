/* Real receiving page, synthetic local API. No supplier or shop connections. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'https://hub.example.test/receiving/test-invoice' });
global.window = dom.window; global.document = dom.window.document;
Object.defineProperty(global, 'navigator', { value: dom.window.navigator, configurable: true });
global.HTMLElement = dom.window.HTMLElement; global.sessionStorage = dom.window.sessionStorage;
global.IS_REACT_ACT_ENVIRONMENT = true;
for (const ext of ['.ts', '.tsx']) require.extensions[ext] = (module, filename) => module._compile(ts.transpileModule(
  fs.readFileSync(filename, 'utf8').replace(/import\.meta\.env/g, '{}'),
  { compilerOptions: { jsx: ts.JsxEmit.React, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, esModuleInterop: true } }).outputText, filename);
require.extensions['.css'] = () => {};
const React = require('react'); const { act } = React; const { createRoot } = require('react-dom/client');
const { MemoryRouter, Routes, Route, useNavigate, useLocation } = require('react-router-dom');
const i18n = require('../src/i18n/index.ts').default;
const { ReceivingSessionPage } = require('../src/pages/ReceivingSessionPage.tsx');
const root = createRoot(document.getElementById('root'));
const tick = () => new Promise(resolve => setTimeout(resolve, 8));
const find = id => document.querySelector(`[data-testid="${id}"]`);
const button = text => [...document.querySelectorAll('button')].find(el => el.textContent.includes(text));
async function input(code) { await act(async () => {
  const el = find('receiving-scan-code'); assert(el && !el.disabled, 'Scanner remains available');
  Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, 'value').set.call(el, code);
  el.dispatchEvent(new dom.window.Event('input', { bubbles: true })); await tick();
}); }
async function scan(code) { await input(code); await act(async () => {
  find('receiving-scan-code').dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true })); await tick();
}); }
const response = (value, status = 200) => ({ ok: status < 400, status, statusText: status === 200 ? 'OK' : 'Conflict',
  headers: { get: () => 'application/json' }, text: async () => JSON.stringify(value), json: async () => value });
const calls = [], scans = [], scanSessions = [], quantityEdits = [], finalizations = [], applied = new Map();
let navigation, currentLocation;
function RouteControl() { navigation = useNavigate(); currentLocation = useLocation(); return null; }
let received = 0, hold = true, resolveHeld, lose = false, ambiguous = false, chooseAmbiguous = false, failSummary = false, invalidResponse = null, holdSummary = false, resolveSummary;
let holdQuantity = false, resolveQuantity, loseQuantity = false, invalidQuantityResponse = null;
let sessionStatus = 'in_progress', pauseMode = 'ok', extraLines = [], failProducts = false, failPrepare = false, failNote = false;
const preparations = [], productRequests = [];
let matches = [{ line_id: 41, status: 'feed_only', product_id: null, product_code: null, product_name: null, catalog_product_id: 77, display_ean: '0123456789012', own_stock_qty: null, stock_confirmed: false, can_prepare: true, draft_id: null, warnings: [] }];
const productResult = () => ({ session_id: 'session-A', invoice_no: 'test-invoice', warehouse_id: 1, feed_verified: true, items: matches, counts: Object.fromEntries(['matched','basic_card','feed_only','not_found','ambiguous','feed_unverified'].map(status => [status, matches.filter(item => item.status === status).length])) });
const line = () => ({ id: 41, scm: 'PART-A', product_code: 'PART-A', ean: '', display_ean: '0123456789012', ean_source: 'supplier_catalog', title: 'Synthetic part', ordered_qty: 20, received_qty: received, status: received >= 20 ? 'matched' : 'partial' });
const summary = () => ({ matched: received >= 20 ? 1 : 0, partial: received > 0 && received < 20 ? 1 : 0, pending: received ? 0 : 1, overage: 0, unexpected: 0 });
global.fetch = async (path, options = {}) => {
  calls.push({ path, method: options.method || 'GET' });
  assert(path.startsWith('/api/suppliers/fixture/'), 'Only local receiving APIs are used');
  if (/\/products(?:\/(?:reconcile|prepare))?$/.test(path)) {
    productRequests.push({ path, method: options.method, headers: options.headers, credentials: options.credentials });
    // Mirror the cookie-authenticated API boundary: a missing CSRF header
    // produces a real rejected response, not an assertion swallowed by the UI.
    if (!path.endsWith('/products') && options.headers?.['X-Hub-Request'] !== '1') {
      return response({ detail: { code: 'csrf_failed', message: 'Missing same-origin request marker' } }, 403);
    }
    if (options.credentials !== 'same-origin') return response({ detail: { code: 'hub_access_required' } }, 401);
  }
  if (path.endsWith('/products') || path.endsWith('/products/reconcile')) {
    if (failProducts) throw new Error('Synthetic feed matching unavailable');
    return response(productResult());
  }
  if (path.endsWith('/products/prepare')) {
    if (failPrepare) return response({ detail: { code: 'receiving_drafts_mixed', message: 'Časť výberu už má rozpracovanú prípravu.' } }, 409);
    preparations.push(JSON.parse(options.body));
    return response({ draft_id: 'invoice-draft', created: preparations.length === 1 });
  }
  if (path.endsWith('/resume')) { sessionStatus = 'in_progress'; return response({ session_id: 'session-A' }); }
  if (path.endsWith('/summary')) {
    if (failSummary) { failSummary = false; throw new TypeError('Synthetic local refresh lost'); }
    const snapshot = { invoice_no: 'test-invoice', status: sessionStatus, lines: [line(), ...extraLines], summary: summary() };
    if (holdSummary) { holdSummary = false; return new Promise(resolve => { resolveSummary = () => resolve(response(snapshot)); }); }
    return response(snapshot);
  }
  if (path.endsWith('/note')) return failNote && options.method === 'POST' ? response({ detail: 'Synthetic note save failure' }, 500) : response({ note: '' });
  if (path.endsWith('/received-items')) return response({ success: true });
  if (path.endsWith('/set-qty')) {
    const body = JSON.parse(options.body); quantityEdits.push(body);
    assert.equal(body.line_index, 0, 'Manual quantity targets the chosen invoice row');
    assert(Number.isFinite(body.received_qty) && body.received_qty >= 0, 'Manual quantity cannot become negative');
    received = body.received_qty;
    const result = { success: true, line: line(), summary: summary() };
    if (loseQuantity) { loseQuantity = false; throw new TypeError('Synthetic quantity acknowledgement lost after commit'); }
    if (invalidQuantityResponse) {
      const mode = invalidQuantityResponse; invalidQuantityResponse = null;
      if (mode === 'empty') return response({});
      if (mode === 'wrong-quantity') return response({ ...result, line: { ...result.line, received_qty: received + 10 } });
      if (mode === 'wrong-line') return response({ ...result, line: { ...result.line, id: 999 } });
    }
    if (holdQuantity) { holdQuantity = false; return new Promise(resolve => { resolveQuantity = () => resolve(response(result)); }); }
    return response(result);
  }
  if (path.endsWith('/pause')) {
    if (pauseMode === 'failed') return response({ detail: 'Synthetic pause failure' }, 500);
    sessionStatus = pauseMode === 'completed' ? 'completed' : 'paused';
    if (pauseMode !== 'ok') throw new TypeError('Synthetic lost pause acknowledgement');
    return response({ success: true });
  }
  if (path.endsWith('/finalize')) {
    const body = JSON.parse(options.body); finalizations.push({ ...body, received });
    if (received < 20) assert.equal(body.force, true, 'Incomplete receipt requires explicit forced closure');
    sessionStatus = 'completed';
    return response({ success: true, invoice_no: 'test-invoice', session_id: 'session-A', completed_at: '2026-09-24T10:00:00Z',
      stats: { total_lines: 1, received_complete: received >= 20 ? 1 : 0, received_partial: received > 0 && received < 20 ? 1 : 0, received_overage: 0, not_received: received === 0 ? 1 : 0, total_scans: received, unexpected_scans: 0 },
      total_ordered: 20, total_received: received, received_items_count: received > 0 ? 1 : 0, message: 'Completed' });
  }
  assert(path.endsWith('/scan'), 'No stock mutation or finalization is triggered by scanning');
  const body = JSON.parse(options.body); scans.push(body); scanSessions.push(path.match(/sessions\/([^/]+)\/scan/)[1]);
  assert.match(body.request_id, /^[0-9a-f-]{36}$/i);
  if (chooseAmbiguous && body.line_id === undefined) return response({ detail: { code: 'scan_line_ambiguous', line_ids: [41, 42] } }, 409);
  if (ambiguous) { ambiguous = false; return response({ detail: { code: 'scan_line_ambiguous' } }, 409); }
  let result = applied.get(body.request_id);
  if (!result) { received += body.qty; result = { request_id: body.request_id, replayed: false, line: line(), status: line().status, summary: summary() }; applied.set(body.request_id, result); }
  else result = { ...result, replayed: true };
  if (lose) { lose = false; throw new TypeError('Synthetic response lost after commit'); }
  if (invalidResponse) {
    const mode = invalidResponse; invalidResponse = null;
    if (mode === 'empty') return response({});
    if (mode === 'body-failure') return { ...response({}), text: async () => { throw new TypeError('Response body truncated'); } };
    if (mode === 'wrong-id') return response({ ...result, request_id: crypto.randomUUID() });
    if (mode === 'invalid-summary') return response({ ...result, summary: { ...result.summary, partial: 'not-a-number' } });
  }
  if (hold) { hold = false; return new Promise(resolve => { resolveHeld = () => resolve(response(result)); }); }
  return response(result);
};
function page() { return React.createElement(MemoryRouter, { initialEntries: [{ pathname: '/receiving/test-invoice', state: { sessionId: 'session-A', supplier: 'fixture', lines: [line()] } }] },
  React.createElement(React.Fragment, null, React.createElement(RouteControl), React.createElement(Routes, null,
    React.createElement(Route, { path: '/product-import', element: React.createElement('div', { 'data-testid': 'product-import-draft' }, 'Existing preparation editor') }),
    React.createElement(Route, { path: '/receiving', element: React.createElement('div', { 'data-testid': 'receiving-list' }, 'Receiving list') }),
    React.createElement(Route, { path: '/receiving/:invoiceId', element: React.createElement(ReceivingSessionPage) })))); }
async function mountReceipt(status = 'in_progress') {
  await act(async () => { root.render(null); await tick(); });
  sessionStatus = status;
  await act(async () => { root.render(page()); await tick(); });
}
(async () => {
  await act(async () => { root.render(page()); await tick(); });
  assert(document.querySelector('[data-action-effects="hub-write"]'), 'Scan is visibly a local change');
  assert(!document.querySelector('[data-action-effects*="upgates"]'), 'No remote operation is implied for scans');
  assert(find('receiving-lines'), 'Invoice items are visible on opening the receipt');
  assert.equal(find('receiving-line-ean-0').firstElementChild.textContent, '0123456789012', 'Matched supplier EAN is visible even when the invoice has none, preserving its leading zero');
  assert.equal(find('receiving-line-ean-0').lastElementChild.textContent, i18n.t('receiving.catalogEanLabel'), 'Catalog-only EAN is visibly distinguished from the invoice value');
  assert.equal(find('receiving-line-ean-0').title, i18n.t('receiving.eanSources.supplier_catalog'), 'The EAN source remains visible');
  assert(find('receiving-edit-line-0'), 'Quantity editing is available without expanding the list');
  assert(find('receiving-decrease-line-0').disabled, 'Zero received quantity cannot be decreased');
  await scan('0123456789012');
  assert.equal(scans.length, 1);
  for (let n = 0; n < 9; n++) await scan('0123456789012');
  assert.equal(scans.length, 1, 'Fast scans queue while the first request is pending');
  assert(find('receiving-scan-pending').textContent.includes('10'));
  assert(button('Ukončiť').disabled, 'Finishing is blocked until queued scans are confirmed');
  assert(find('receiving-product-prepare-0').disabled, 'Card preparation cannot overlap pending scans');
  assert(find('receiving-increase-line-0').disabled, 'Manual quantity cannot overlap pending scans');
  assert(find('receiving-decrease-line-0').disabled, 'Manual decrement cannot overlap pending scans');
  await act(async () => { find('receiving-increase-line-0').click(); await tick(); });
  assert.equal(quantityEdits.length, 0);
  await act(async () => { resolveHeld(); await tick(); });
  assert.equal(scans.length, 10, 'Every physical scan is delivered');
  assert.equal(new Set(scans.map(row => row.request_id)).size, 10);
  assert.equal(received, 10, 'Ten identical barcodes add ten pieces');
  assert.equal(find('receiving-scan-pending'), null);

  await input('0123456789012');
  await act(async () => { const el = find('receiving-scan'); el.click(); el.click(); await tick(); });
  assert.equal(scans.length, 11, 'A double click consumes one entered scan only');
  assert.equal(received, 11);

  lose = true; await scan('0123456789012');
  const lost = scans.at(-1); assert.equal(received, 12);
  assert(find('receiving-scan-retry'));
  await scan('0123456789012');
  assert.equal(scans.length, 12, 'New scans wait behind an uncertain result');
  const saved = JSON.parse(sessionStorage.getItem('receiving-scans:fixture:session-A'));
  assert.equal(saved.length, 2); assert.equal(saved[0].request_id, lost.request_id);
  await act(async () => { root.render(null); await tick(); });
  await act(async () => { root.render(page()); await tick(); });
  assert(find('receiving-scan-retry'), 'Unconfirmed scan queue survives page remount');
  await act(async () => { const retry = find('receiving-scan-retry'); retry.click(); retry.click(); await tick(); });
  assert.equal(scans.length, 14, 'Retry and one queued new scan are each sent once');
  assert.deepEqual(scans[12], lost, 'Retry preserves the exact original UUID and payload');
  assert.notEqual(scans[13].request_id, lost.request_id);
  assert.equal(received, 13, 'Lost acknowledgement never adds a piece twice');
  assert.equal(sessionStorage.getItem('receiving-scans:fixture:session-A'), null);

  ambiguous = true; await scan('DUPLICATE-INVOICE-CODE');
  assert(document.body.textContent.includes(i18n.t('actions.receiving.ambiguous')), 'Ambiguous invoice lines have actionable guidance');
  assert.equal(received, 13); assert.equal(find('receiving-scan-retry'), null, 'Known rejected scan is not retried forever');
  await scan('0123456789012'); assert.equal(received, 14, 'A new legitimate scan remains available after rejection');
  // An old page's late acknowledgement must not overwrite a restored queue.
  hold = true; await scan('0123456789012');
  const late = resolveHeld;
  await act(async () => { root.render(null); await tick(); });
  await act(async () => { root.render(page()); await tick(); });
  await scan('0123456789012');
  assert.equal(JSON.parse(sessionStorage.getItem('receiving-scans:fixture:session-A')).length, 2);
  await act(async () => { late(); await tick(); });
  assert.equal(JSON.parse(sessionStorage.getItem('receiving-scans:fixture:session-A')).length, 2,
    'Late response from the unmounted page cannot erase the new queue');
  await act(async () => { find('receiving-scan-retry').click(); await tick(); });
  assert.equal(received, 16, 'The old scan replays once and the new scan adds once');
  assert.equal(sessionStorage.getItem('receiving-scans:fixture:session-A'), null);
  lose = true; await scan('0123456789012');
  const lastLost = scans.at(-1); failSummary = true;
  await act(async () => { find('receiving-scan-retry').click(); await tick(); });
  assert.equal(received, 17);
  assert(find('receiving-scan-retry'), 'A failed refresh after replay keeps the operation pending');
  assert.equal(JSON.parse(sessionStorage.getItem('receiving-scans:fixture:session-A'))[0].request_id, lastLost.request_id);
  await act(async () => { find('receiving-scan-retry').click(); await tick(); });
  assert.equal(received, 17, 'Retry after refresh failure never changes the physical scan count');
  assert.equal(sessionStorage.getItem('receiving-scans:fixture:session-A'), null);
  for (const mode of ['empty', 'body-failure', 'wrong-id', 'invalid-summary']) {
    invalidResponse = mode;
    const previous = received;
    await scan('0123456789012');
    const uncertainScan = scans.at(-1);
    assert.equal(received, previous + 1);
    assert(find('receiving-scan-retry'), `Malformed acknowledgement (${mode}) remains retryable`);
    assert.equal(JSON.parse(sessionStorage.getItem('receiving-scans:fixture:session-A'))[0].request_id, uncertainScan.request_id);
    await act(async () => { find('receiving-scan-retry').click(); await tick(); });
    assert.deepEqual(scans.at(-1), uncertainScan, 'Invalid acknowledgement preserves the exact operation');
    assert.equal(received, previous + 1, 'Retry never adds another piece');
    assert.equal(find('receiving-scan-pending'), null);
  }

  const beforeInvalidQuantity = scans.length;
  for (const invalid of ['0', '-2', '']) {
    await act(async () => {
      const quantity = find('receiving-scan-quantity');
      Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, 'value').set.call(quantity, invalid);
      quantity.dispatchEvent(new dom.window.Event('input', { bubbles: true })); await tick();
    });
    await scan('0123456789012');
    assert.equal(scans.length, beforeInvalidQuantity, 'Zero, negative and blank quantities are rejected without a request');
    assert(document.body.textContent.includes(i18n.t('actions.receiving.invalidQuantity')));
    assert.equal(find('receiving-scan-quantity').value, invalid, 'Invalid quantity is not silently changed into one piece');
  }
  await act(async () => { root.render(null); await tick(); });
  holdSummary = true;
  await act(async () => { root.render(page()); await tick(); });
  const staleSummary = resolveSummary;
  assert(find('receiving-scan-code').disabled, 'Navigation-state lines cannot enable scans before server session status is read');
  assert.equal(find('receiving-finalize'), null, 'Unknown status cannot finalize');
  sessionStatus = 'completed';
  await act(async () => { navigation('/receiving/closed-invoice', { state: { sessionId: 'closed-session', supplier: 'fixture', lines: [line()] } }); await tick(); });
  assert(find('receiving-session-status'), 'The new session is closed');
  await act(async () => { staleSummary(); await tick(); });
  assert.equal(find('receiving-scan-code'), null, 'A late active summary for the previous session cannot unlock a completed receipt');
  await mountReceipt();

  // Switch the same mounted component while A is still sending.
  hold = true; await scan('0123456789012');
  const lateSessionA = resolveHeld; const requestA = scans.at(-1);
  await act(async () => { navigation('/receiving/other-invoice', { state: { sessionId: 'session-B', supplier: 'fixture', lines: [line()] } }); await tick(); });
  const beforeB = scans.length;
  await scan('0123456789012');
  assert.equal(scans.length, beforeB + 1, 'A different receipt does not inherit the previous sender lock');
  assert.equal(scanSessions.at(-1), 'session-B');
  assert.equal(find('receiving-scan-pending'), null, 'Receipt B drains without requiring an invisible retry');
  await act(async () => { lateSessionA(); await tick(); });
  assert.equal(JSON.parse(sessionStorage.getItem('receiving-scans:fixture:session-A'))[0].request_id, requestA.request_id, 'A late response for A preserves its recoverable queue');
  assert.equal(sessionStorage.getItem('receiving-scans:fixture:session-B'), null);
  await act(async () => { navigation('/receiving/test-invoice', { state: { sessionId: 'session-A', supplier: 'fixture', lines: [line()] } }); await tick(); });
  assert(find('receiving-scan-retry'));
  const beforeAReplay = received;
  await act(async () => { find('receiving-scan-retry').click(); await tick(); });
  assert.equal(received, beforeAReplay, 'Returning to A recovers the original scan without duplication');
  assert.deepEqual(scans.at(-1), requestA);

  // Both exit controls leave historical/paused sessions without any writes.
  for (const status of ['completed', 'cancelled', 'paused']) {
    for (const control of ['receiving-back', 'receiving-exit']) {
      await mountReceipt(status);
      assert(find('receiving-session-status'));
      assert.equal(find('receiving-scan-code'), null, `${status}: no new scan entry`);
      assert.equal(find('receiving-finalize'), null, `${status}: cannot finalize again`);
      assert(find('receiving-note').readOnly, `${status}: notes are read-only`);
      assert(find('receiving-lines'), `${status}: invoice items remain visible by default`);
      assert.equal(find('receiving-edit-line-0'), null, `${status}: no quantity editor`);
      assert.equal(find('receiving-increase-line-0'), null, `${status}: no manual increment`);
      assert.equal(find('receiving-decrease-line-0'), null, `${status}: no manual decrement`);
      assert.equal(button('Prijať všetko'), undefined);
      assert.equal(button('Resetovať'), undefined);
      const beforeExit = calls.length;
      await act(async () => { find(control).click(); await tick(); });
      assert(find('receiving-list'), `${status}: ${control} returns to the receiving list`);
      assert.equal(calls.length, beforeExit, `${status}: navigating never pauses or writes invoice metadata`);
    }
  }

  await mountReceipt();
  sessionStatus = 'completed';
  let beforeExit = calls.length;
  await act(async () => { find('receiving-exit').click(); await tick(); });
  assert(find('receiving-list'), 'Exit notices completion by another tab');
  assert(calls.slice(beforeExit).every(call => call.method === 'GET'), 'A remotely completed receipt is not mutated');

  await mountReceipt(); pauseMode = 'failed';
  await act(async () => { find('receiving-exit').click(); await tick(); });
  assert.equal(find('receiving-list'), null, 'A failed pause keeps the active receipt open');
  assert(document.body.textContent.includes(i18n.t('receiving.pauseError')));
  assert(!find('receiving-scan-code').disabled, 'The draft remains usable after a pause failure');
  for (const mode of ['lost', 'completed']) {
    await mountReceipt(); pauseMode = mode;
    await act(async () => { find('receiving-back').click(); await tick(); });
    assert(find('receiving-list'), `Status reconciliation permits exit after ${mode} pause response`);
  }
  pauseMode = 'ok';
  await mountReceipt();
  beforeExit = calls.length;
  await act(async () => { find('receiving-exit').click(); await tick(); });
  assert(find('receiving-list'), 'A successful active pause returns to the list');
  assert.equal(calls.slice(beforeExit).filter(call => call.path.endsWith('/pause')).length, 1, 'An active receipt is paused exactly once');

  // Acknowledgement recovery stays possible after another tab finalized. The
  // original UUID is retained even when the operator chooses to leave first.
  await mountReceipt(); lose = true;
  await scan('0123456789012');
  const closedScan = scans.at(-1), countAtClose = received;
  await mountReceipt('completed');
  assert(find('receiving-scan-retry'));
  assert(!find('receiving-exit').disabled, 'An idle uncertain scan does not trap a completed receipt');
  beforeExit = calls.length;
  await act(async () => { find('receiving-exit').click(); await tick(); });
  assert(find('receiving-list')); assert.equal(calls.length, beforeExit);
  assert.equal(JSON.parse(sessionStorage.getItem('receiving-scans:fixture:session-A'))[0].request_id, closedScan.request_id);
  await mountReceipt('completed');
  hold = true;
  await act(async () => { find('receiving-scan-retry').click(); await tick(); });
  assert(find('receiving-exit').disabled, 'Exit waits while acknowledgement recovery is actively sending');
  await act(async () => { resolveHeld(); await tick(); });
  assert.deepEqual(scans.at(-1), closedScan, 'Completed-session recovery reuses the exact operation');
  assert.equal(received, countAtClose, 'Recovery after completion cannot add another piece');
  assert.equal(find('receiving-scan-code'), null, 'The replay snapshot cannot reopen a completed receipt');
  assert.equal(sessionStorage.getItem('receiving-scans:fixture:session-A'), null);

  // Arrow changes use the existing absolute-quantity endpoint and serialize
  // with the scanner. A fast second click cannot overwrite an in-flight edit.
  received = 0;
  await mountReceipt();
  holdQuantity = true;
  const beforeQuantity = quantityEdits.length, beforeManualScan = scans.length;
  await act(async () => { const increase = find('receiving-increase-line-0'); increase.click(); increase.click(); await tick(); });
  assert.equal(quantityEdits.length, beforeQuantity + 1, 'Double click sends one pending quantity update');
  assert.equal(quantityEdits.at(-1).received_qty, 1, 'Up arrow adds one received piece');
  assert(find('receiving-increase-line-0').disabled);
  assert(find('receiving-decrease-line-0').disabled);
  assert(find('receiving-scan-code').disabled, 'Scanner waits until manual quantity is acknowledged');
  assert(find('receiving-finalize').disabled, 'Finalization cannot overlap a quantity update');
  await act(async () => {
    find('receiving-scan-code').dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    await tick();
  });
  assert.equal(scans.length, beforeManualScan);
  await act(async () => { resolveQuantity(); await tick(); });
  assert(!find('receiving-increase-line-0').disabled);
  await act(async () => { find('receiving-decrease-line-0').click(); await tick(); });
  assert.equal(quantityEdits.at(-1).received_qty, 0, 'Down arrow removes one received piece');
  const atZero = quantityEdits.length;
  await act(async () => { find('receiving-decrease-line-0').click(); await tick(); });
  assert.equal(quantityEdits.length, atZero, 'Down arrow at zero sends no update');
  received = 0.5;
  await mountReceipt();
  await act(async () => { find('receiving-decrease-line-0').click(); await tick(); });
  assert.equal(quantityEdits.at(-1).received_qty, 0, 'Decreasing a fractional quantity clamps at zero');

  loseQuantity = true;
  const beforeLostEdit = quantityEdits.length;
  await act(async () => { find('receiving-increase-line-0').click(); await tick(); });
  assert.equal(received, 1);
  assert.equal(quantityEdits.length, beforeLostEdit + 1, 'Lost manual acknowledgement is reconciled without resending a write');
  assert(!find('receiving-increase-line-0').disabled, 'Successful reconciliation restores editing');
  await act(async () => { find('receiving-increase-line-0').click(); await tick(); });
  assert.equal(quantityEdits.at(-1).received_qty, 2, 'Next arrow uses the reconciled server quantity');

  loseQuantity = true; failSummary = true;
  await act(async () => { find('receiving-increase-line-0').click(); await tick(); });
  assert.equal(received, 3);
  assert(find('receiving-quantity-refresh'), 'Failed reconciliation offers a read-only refresh');
  assert(!find('receiving-increase-line-0') || find('receiving-increase-line-0').disabled,
    'Uncertain manual quantity prevents another increment');
  assert(!find('receiving-scan-code') || find('receiving-scan-code').disabled,
    'Uncertain manual quantity prevents a scan');
  const beforeRecovery = calls.length;
  await act(async () => { find('receiving-quantity-refresh').click(); await tick(); });
  assert(calls.slice(beforeRecovery).every(call => call.method === 'GET'), 'Recovery reads state without repeating the quantity write');
  assert.equal(find('receiving-quantity-refresh'), null);
  await act(async () => { find('receiving-decrease-line-0').click(); await tick(); });
  assert.equal(quantityEdits.at(-1).received_qty, 2, 'Recovery uses the committed quantity before the next edit');

  for (const mode of ['empty', 'wrong-quantity', 'wrong-line']) {
    invalidQuantityResponse = mode; holdSummary = true;
    const beforeMalformed = calls.length, beforeMalformedEdits = quantityEdits.length, previous = received;
    await act(async () => { find('receiving-increase-line-0').click(); await tick(); });
    assert.equal(received, previous + 1, 'The server may commit before sending a malformed acknowledgement');
    assert.equal(quantityEdits.length, beforeMalformedEdits + 1, `${mode}: the uncertain write is not resubmitted`);
    assert.equal(calls.slice(beforeMalformed).filter(call => call.path.endsWith('/summary')).length, 1,
      `${mode}: malformed HTTP 200 requires a fresh server summary`);
    assert(!find('receiving-increase-line-0') || find('receiving-increase-line-0').disabled, `${mode}: edits wait for the current quantity`);
    assert(find('receiving-scan-code').disabled, `${mode}: scanning cannot race reconciliation`);
    assert(!find('receiving-finalize') || find('receiving-finalize').disabled, `${mode}: closure cannot race reconciliation`);
    await act(async () => { resolveSummary(); await tick(); });
    assert(!find('receiving-increase-line-0').disabled);
    assert.equal(quantityEdits.length, beforeMalformedEdits + 1, `${mode}: successful recovery only reads`);
    await act(async () => { find('receiving-decrease-line-0').click(); await tick(); });
    assert.equal(quantityEdits.at(-1).received_qty, previous, `${mode}: next edit uses the real row and reconciled quantity`);
  }

  received = 20;
  await mountReceipt();
  await act(async () => { find('receiving-finalize').click(); await tick(); });
  assert(find('receiving-finalize-incomplete'), 'A complete receipt also shows the impact before posting');
  await act(async () => { find('receiving-finalize-incomplete').click(); await tick(); });
  assert.deepEqual(finalizations.at(-1), { force: false, received: 20 }, 'Fully received invoice uses normal finalization');
  assert.equal(find('receiving-finalize'), null, 'A successful finalization immediately switches the receipt to read-only');
  assert(find('receiving-session-status')); assert.equal(find('receiving-scan-code'), null);
  await mountReceipt('completed');
  beforeExit = calls.length;
  await act(async () => { find('receiving-back').click(); await tick(); });
  assert(find('receiving-list')); assert.equal(calls.length, beforeExit, 'Reopening after finalization does not require another finalize or pause call');

  // Historical invoices can close with fewer or zero received pieces. Merely
  // opening/cancelling confirmation must not accept the missing quantities.
  for (const historicalQuantity of [3, 0]) {
    received = historicalQuantity;
    await mountReceipt();
    const beforeConfirmation = calls.length, beforeFinalizations = finalizations.length;
    await act(async () => { find('receiving-finalize').click(); await tick(); });
    assert(find('receiving-finalize-incomplete'), 'Incomplete receipt requires a separate confirmation');
    assert.equal(calls.length, beforeConfirmation, 'Opening confirmation makes no API writes');
    await act(async () => { button('Pokračovať v skenovaní').click(); await tick(); });
    assert.equal(finalizations.length, beforeFinalizations, 'Cancelling leaves the receipt unfinished');
    assert.equal(calls.length, beforeConfirmation, 'Cancelling confirmation changes no quantities');
    await act(async () => { find('receiving-finalize').click(); await tick(); });
    await act(async () => { const finish = find('receiving-finalize-incomplete'); finish.click(); finish.click(); await tick(); });
    assert.equal(finalizations.length, beforeFinalizations + 1, 'Double confirmation sends one finalization');
    assert.deepEqual(finalizations.at(-1), { force: true, received: historicalQuantity }, 'Historical closure submits only the confirmed received quantity');
    assert.equal(received, historicalQuantity, 'Closure does not pretend unreceived goods were delivered');
    assert.equal(find('receiving-finalize'), null);
    assert(find('receiving-session-status'));
    const afterClose = calls.length;
    if (historicalQuantity === 0) {
      await act(async () => { await new Promise(resolve => setTimeout(resolve, 2100)); });
      assert.equal(calls.length, afterClose, 'Non-Paul-Lange receipt never starts a legacy export after completion');
    }
    assert(find('receiving-completion-back'), 'Completed receipt offers a direct return to the list');
    await act(async () => { find('receiving-completion-back').click(); await tick(); });
    assert(find('receiving-list'));
    assert.equal(calls.length, afterClose, 'Returning from completion makes no additional API writes');
  }
  assert(!calls.some(call => call.path.endsWith('/accept-all')), 'Historical closure never calls accept-all');


  // Card preparation reuses the editor and receipt, never manufacturing stock.
  received = 3;
  extraLines = ['basic_card', 'matched', 'not_found', 'ambiguous', 'feed_unverified'].map((status, index) => ({ ...line(), id: 42 + index, scm: `PART-${index}`, product_code: `PART-${index}`, title: `Synthetic ${status}`, received_qty: 0 }));
  matches = [matches[0], ...extraLines.map((item, index) => ({ ...matches[0], line_id: item.id, status: ['basic_card', 'matched', 'not_found', 'ambiguous', 'feed_unverified'][index],
    can_prepare: index !== 1 && index !== 3, product_id: index < 2 ? 100 + index : null, product_code: index < 2 ? `EXISTING-${index}` : null,
    own_stock_qty: index === 1 ? 5 : null, stock_confirmed: index === 1, display_ean: item.ean }))];
  matches[4] = { ...matches[4], candidate_products: [{ id: 900, code: 'EXISTING CONFLICT', name: 'Known card' }], candidate_sources: [{ id: 88, code: 'FEED-CONFLICT', name: 'Feed item', eans: ['0123456789012'] }], warnings: ['EAN on the invoice conflicts with the feed'] };
  await mountReceipt();
  assert(find('receiving-product-stock-0').textContent.includes(i18n.t('receiving.products.stockUnverified')), 'Unknown stock is never displayed as zero');
  assert(find('receiving-product-stock-2').textContent.includes('5'), 'Own confirmed warehouse stock is separate from received quantity');
  assert(find('receiving-product-match-1').textContent.includes(i18n.t('receiving.products.completeCard')), 'Basic cards offer enrichment');
  assert.equal(find('receiving-product-open-2').getAttribute('href'), '/products/EXISTING-1', 'Existing cards preserve and open their canonical code');
  assert(find('receiving-product-select-4').disabled, 'Ambiguous identity is never eligible for automatic creation');
  const conflict = find('receiving-product-conflicts-4');
  assert(conflict.textContent.includes('FEED-CONFLICT') && conflict.textContent.includes('0123456789012'), 'Ambiguity details show exact source evidence');
  assert(conflict.querySelector('a[href="/products/EXISTING%20CONFLICT"]'), 'Ambiguous candidate can be reviewed in its existing card');
  assert(!find('receiving-product-select-5').disabled, 'A missing feed still permits preparation from invoice evidence');
  await act(async () => { find('receiving-products-filter-review').click(); await tick(); });
  assert.equal(find('receiving-product-match-0'), null);
  assert(find('receiving-product-match-4') && find('receiving-product-match-5'), 'Review filter distinguishes ambiguity and feed verification failure');
  await act(async () => { find('receiving-products-filter-all').click(); find('receiving-products-select-missing').click(); await tick(); });
  assert.equal(document.querySelectorAll('[data-testid^="receiving-product-select-"]:checked').length, 4, 'Only eligible missing/basic cards are bulk selected');
  // Separate paid/bonus invoice rows can represent one stock card.
  const previousMatches = structuredClone(matches);
  matches = matches.map(item => ({ ...item, can_prepare: item.line_id === 41 || item.line_id === 44, product_id: null, catalog_product_id: 77 }));
  await act(async () => { find('receiving-products-refresh').click(); await tick(); });
  await act(async () => { find('receiving-products-select-missing').click(); await tick(); });
  assert.equal(find('receiving-products-prepare').textContent, i18n.t('receiving.products.createCards', { count: 1 }), 'Repeated invoice rows from one catalog item count as one card');
  assert.equal(document.querySelectorAll('[data-testid^="receiving-product-select-"]:checked').length, 2, 'The original invoice lines remain separately selected');
  matches = previousMatches;
  await act(async () => { find('receiving-products-refresh').click(); await tick(); });
  await act(async () => { find('receiving-products-select-missing').click(); await tick(); });
  const beforeCards = finalizations.length;
  const beforePrepareCalls = calls.length;
  await act(async () => { const create = find('receiving-products-prepare'); create.click(); create.click(); await tick(); });
  assert.equal(preparations.length, 1, 'Double click opens a single durable preparation');
  assert.deepEqual(preparations[0].line_ids, [41, 42, 44, 46]);
  assert(find('product-import-draft'));
  assert.equal(currentLocation.search, '?draft=invoice-draft');
  assert.equal(sessionStatus, 'paused');
  assert.equal(received, 3, 'Preparing cards does not alter received quantities');
  assert.equal(finalizations.length, beforeCards, 'Preparing cards creates no stock movement');
  const prepareCalls = calls.slice(beforePrepareCalls);
  assert(prepareCalls.findIndex(call => call.path.endsWith('/received-items')) < prepareCalls.findIndex(call => call.path.endsWith('/products/prepare')), 'Receipt context is saved before opening preparation');
  assert(prepareCalls.some(call => call.path.endsWith('/note') && call.method === 'POST'), 'Invoice notes are persisted even when empty');
  matches[0] = { ...matches[0], status: 'matched', product_id: 999, product_code: 'EXISTING-A', can_prepare: false, draft_id: 'invoice-draft', stock_confirmed: false };
  await act(async () => { navigation('/receiving/test-invoice?session=session-A&supplier=fixture'); await tick(); });
  assert(find('receiving-resume'), 'Direct durable return loads the same paused receipt without router state');
  assert(find('receiving-product-prepare-0').textContent.includes(i18n.t('receiving.products.continuePreparation')), 'Saved card remains linked to its unfinished editor');
  assert.equal(find('receiving-increase-line-0'), null, 'Returned paused receipt does not accept accidental quantity edits');
  await act(async () => { find('receiving-resume').click(); await tick(); });
  assert.equal(sessionStatus, 'in_progress'); assert.equal(received, 3);
  assert(find('receiving-increase-line-0'), 'Inline resume restores receiving without returning to the invoice list');
  await act(async () => { find('receiving-product-prepare-0').click(); await tick(); });
  assert.equal(preparations.length, 2); assert.deepEqual(preparations[1].line_ids, [41]);
  assert.equal(currentLocation.search, '?draft=invoice-draft', 'A saved card reopens its original preparation');

  // Strict note persistence and mixed-draft feedback keep this transition recoverable.
  await mountReceipt(); failNote = true;
  const beforeFailedPrepare = preparations.length;
  await act(async () => { find('receiving-product-prepare-0').click(); await tick(); });
  assert.equal(preparations.length, beforeFailedPrepare, 'Failed receipt note save prevents leaving with lost local context');
  assert(find('receiving-lines'));
  failNote = false; failPrepare = true;
  await act(async () => { find('receiving-product-prepare-0').click(); await tick(); });
  assert(document.body.textContent.includes(i18n.t('receiving.products.mixedDrafts')), 'Mixed existing drafts have actionable feedback');
  assert.equal(sessionStatus, 'in_progress');
  failPrepare = false; pauseMode = 'lost';
  await act(async () => { find('receiving-product-prepare-0').click(); await tick(); });
  assert(find('product-import-draft'), 'Lost pause acknowledgement is reconciled before navigating');
  assert.equal(sessionStatus, 'paused'); assert.equal(received, 3);
  pauseMode = 'ok';

  // Failure to read the feed does not disable ordinary receiving or imply not-found.
  extraLines = []; failProducts = true;
  await mountReceipt();
  assert(document.body.textContent.includes(i18n.t('receiving.products.loadError')));
  assert.equal(find('receiving-product-match-0'), null);
  assert(!find('receiving-scan-code').disabled);
  failProducts = false;

  await mountReceipt('future-status');
  assert(find('receiving-scan-code').disabled, 'An unknown server status never enables edits');
  assert(document.body.textContent.includes(i18n.t('actions.receiving.summaryFailed')));
  beforeExit = calls.length;
  await act(async () => { find('receiving-exit').click(); await tick(); });
  assert(find('receiving-list'), 'A failed initial read does not trap navigation');
  assert.equal(calls.length, beforeExit, 'Leaving an unloaded receipt never attempts a mutation');
  assert(productRequests.every(request => request.headers?.['X-Hub-Request'] === '1' && request.credentials === 'same-origin'), 'All new receiving product APIs use the shared session-aware client');
  for (const operation of ['reconcile', 'prepare']) {
    const requests = productRequests.filter(request => request.path.endsWith(`/products/${operation}`));
    assert(requests.length > 0, `Actual page interactions exercise protected ${operation} requests`);
    assert(requests.every(request => request.method === 'POST' && request.headers['X-Hub-Request'] === '1' && request.credentials === 'same-origin'));
  }
  failSummary = false;
  received = 0; chooseAmbiguous = true;
  extraLines = [{ ...line(), id: 42, unit_price: 17.25, title: 'Same code, different cost' }];
  await mountReceipt();
  await scan('DUPLICATE-WITH-CHOICE');
  const ambiguousRequest = { ...scans.at(-1) };
  assert(find('receiving-ambiguous'), 'Ambiguous scan stays queued for an explicit line choice');
  assert.equal(received, 0);
  assert(find('receiving-finalize').disabled, 'Unresolved line choice blocks posting');
  lose = true;
  await act(async () => { find('receiving-choose-line-41').click(); await tick(); });
  assert.deepEqual(scans.at(-1), { ...ambiguousRequest, line_id: 41 }, 'Only a proven rejected scan gains a line selection');
  assert(find('receiving-scan-retry'));
  await act(async () => { find('receiving-scan-retry').click(); await tick(); });
  assert.deepEqual(scans.at(-1), { ...ambiguousRequest, line_id: 41 }, 'Uncertain retry preserves the selected line and UUID');
  assert.equal(received, 1, 'Replaying the selected line cannot double receive');
  chooseAmbiguous = false; extraLines = [];
  await act(async () => { root.unmount(); });
  console.log('Receiving UI: scan UUID/retry/context protections, visible invoice items, manual quantity serialization/recovery, explicit historical closure, readonly receipts and pause reconciliation passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
