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
const { MemoryRouter, Routes, Route, useNavigate } = require('react-router-dom');
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
const calls = [], scans = [], scanSessions = [], applied = new Map();
let navigation;
function RouteControl() { navigation = useNavigate(); return null; }
let received = 0, hold = true, resolveHeld, lose = false, ambiguous = false, failSummary = false, invalidResponse = null, holdSummary = false, resolveSummary;
let sessionStatus = 'in_progress', pauseMode = 'ok';
const line = () => ({ id: 41, scm: 'PART-A', product_code: 'PART-A', ean: '0123456789012', title: 'Synthetic part', ordered_qty: 20, received_qty: received, status: received >= 20 ? 'matched' : 'partial' });
const summary = () => ({ matched: received >= 20 ? 1 : 0, partial: received > 0 && received < 20 ? 1 : 0, pending: received ? 0 : 1, overage: 0, unexpected: 0 });
global.fetch = async (path, options = {}) => {
  calls.push({ path, method: options.method || 'GET' });
  assert(path.startsWith('/api/suppliers/fixture/'), 'Only local receiving APIs are used');
  if (path.endsWith('/summary')) {
    if (failSummary) { failSummary = false; throw new TypeError('Synthetic local refresh lost'); }
    const snapshot = { invoice_no: 'test-invoice', status: sessionStatus, lines: [line()], summary: summary() };
    if (holdSummary) { holdSummary = false; return new Promise(resolve => { resolveSummary = () => resolve(response(snapshot)); }); }
    return response(snapshot);
  }
  if (path.endsWith('/note')) return response({ note: '' });
  if (path.endsWith('/received-items')) return response({ success: true });
  if (path.endsWith('/pause')) {
    if (pauseMode === 'failed') return response({ detail: 'Synthetic pause failure' }, 500);
    sessionStatus = pauseMode === 'completed' ? 'completed' : 'paused';
    if (pauseMode !== 'ok') throw new TypeError('Synthetic lost pause acknowledgement');
    return response({ success: true });
  }
  if (path.endsWith('/finalize')) {
    sessionStatus = 'completed';
    return response({ success: true, invoice_no: 'test-invoice', session_id: 'session-A', completed_at: '2026-09-24T10:00:00Z',
      stats: { total_lines: 1, received_complete: 1, received_partial: 0, received_overage: 0, not_received: 0, total_scans: received, unexpected_scans: 0 },
      total_ordered: 20, total_received: received, received_items_count: 1, message: 'Completed' });
  }
  assert(path.endsWith('/scan'), 'No stock mutation or finalization is triggered by scanning');
  const body = JSON.parse(options.body); scans.push(body); scanSessions.push(path.match(/sessions\/([^/]+)\/scan/)[1]);
  assert.match(body.request_id, /^[0-9a-f-]{36}$/i);
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
  await scan('0123456789012');
  assert.equal(scans.length, 1);
  for (let n = 0; n < 9; n++) await scan('0123456789012');
  assert.equal(scans.length, 1, 'Fast scans queue while the first request is pending');
  assert(find('receiving-scan-pending').textContent.includes('10'));
  assert(button('Ukončiť').disabled, 'Finishing is blocked until queued scans are confirmed');
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
      await act(async () => { button('Zobraziť položky').click(); await tick(); });
      assert.equal(find('receiving-edit-line-0'), null, `${status}: no quantity editor`);
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

  await mountReceipt();
  await act(async () => { find('receiving-finalize').click(); await tick(); });
  assert.equal(find('receiving-finalize'), null, 'A successful finalization immediately switches the receipt to read-only');
  assert(find('receiving-session-status')); assert.equal(find('receiving-scan-code'), null);
  await mountReceipt('completed');
  beforeExit = calls.length;
  await act(async () => { find('receiving-back').click(); await tick(); });
  assert(find('receiving-list')); assert.equal(calls.length, beforeExit, 'Reopening after finalization does not require another finalize or pause call');

  await mountReceipt('future-status');
  assert(find('receiving-scan-code').disabled, 'An unknown server status never enables edits');
  assert(document.body.textContent.includes(i18n.t('actions.receiving.summaryFailed')));
  beforeExit = calls.length;
  await act(async () => { find('receiving-exit').click(); await tick(); });
  assert(find('receiving-list'), 'A failed initial read does not trap navigation');
  assert.equal(calls.length, beforeExit, 'Leaving an unloaded receipt never attempts a mutation');
  await act(async () => { root.unmount(); });
  console.log('Receiving UI: scan UUID/retry/context protections, readonly completed receipts, both exit controls, stale-status reconciliation and active pause errors passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
