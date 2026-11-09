/* Real recovery controls and API client; synthetic local responses only. */
const assert = require('node:assert/strict');
const fs = require('node:fs'), ts = require('typescript');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'https://hub.example.test/receiving/fixture' });
global.window = dom.window; global.document = dom.window.document;
Object.defineProperty(global, 'navigator', { value: dom.window.navigator, configurable: true });
global.HTMLElement = dom.window.HTMLElement; global.sessionStorage = dom.window.sessionStorage;
global.IS_REACT_ACT_ENVIRONMENT = true;
for (const ext of ['.ts', '.tsx']) require.extensions[ext] = (module, filename) => module._compile(ts.transpileModule(
  fs.readFileSync(filename, 'utf8').replace(/import\.meta\.env/g, '{}'),
  { compilerOptions: { jsx: ts.JsxEmit.React, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, esModuleInterop: true } }).outputText, filename);
require.extensions['.css'] = () => {};
const React = require('react'), { act } = React, { createRoot } = require('react-dom/client');
require('../src/i18n/index.ts');
const { ReceivingRecoveryPanel } = require('../src/components/ReceivingRecoveryPanel.tsx');
const { ReceivingPage } = require('../src/pages/ReceivingPage.tsx');
const { MemoryRouter, Routes, Route, useLocation } = require('react-router-dom');
const root = createRoot(document.getElementById('root'));
const tick = () => new Promise(resolve => setTimeout(resolve, 5));
const find = id => document.querySelector(`[data-testid="${id}"]`);
const response = (value, status = 200) => ({ ok: status < 400, status, statusText: 'Synthetic',
  headers: { get: () => 'application/json' }, text: async () => JSON.stringify(value), json: async () => value });
const calls = [], operations = new Map(), applied = new Map();
const lastCommand = () => calls.filter(call => call.body).at(-1);
let lostApply = false, invalidPreview = false, blocked = false, stale = false, commits = 0, changes = 0, locked = false;
const impact = { product_id: 10, sku: 'PART', name: 'Part', stock_unit: 'pcs', before_quantity: '5', delta: '-2',
  after_quantity: '3', reserved: '1', quarantined: '0', available_before: '4', available_after: '2' };
global.fetch = async (path, options = {}) => {
  const body = options.body ? JSON.parse(options.body) : undefined;
  calls.push({ path, body, method: options.method || 'GET' });
  if (path === '/api/suppliers') return response([{ code: 'fixture', name: 'Fixture' }]);
  if (path.includes('/invoices/index')) return response({ invoices: [{ invoice_id: 'INV', number: 'INV', issue_date: null,
    csv_path: '', status: 'processed', receiving_session_id: '12', receiving_archived: path.includes('include_archived=true') }] });
  if (path.endsWith('/operations')) return response({ session_id: 12, limit: 50, operations: [...applied.values()].map(item => ({
    id: item.id, action: item.action, reason: item.reason, actor: item.actor, at: item.completed_at,
    stock_effect: item.stock_effect, impacts: item.impacts, status: item.status, archived: item.archived,
  })) });
  assert(path.startsWith('/api/suppliers/fixture/receiving/sessions/12/operations/'), 'Only local receipt operation API');
  assert.equal(options.credentials, 'same-origin'); assert.equal(options.headers['X-Hub-Request'], '1');
  if (path.endsWith('/preview')) {
    const undo = ['reopen', 'reverse'].includes(body.action);
    const value = operations.get(body.request_id) || { id: body.request_id, session_id: 12, action: body.action,
      reason: body.reason, actor: 'user:1', status: blocked ? 'blocked' : 'prepared', preview_hash: 'a'.repeat(64),
      ready: !blocked, expires_at: '2099-01-01T00:00:00Z', invoice_number: 'INV', receipt_status: 'completed',
      after_status: body.action === 'reopen' ? 'paused' : body.action === 'restore' ? 'completed' : 'cancelled',
      warehouse: { id: 1, code: 'main', name: 'Main' }, posting_revision: 0, archived: body.action === 'restore',
      stock_effect: undo ? 'reverse_receipt' : 'unchanged', impacts: undo ? [impact] : [],
      errors: blocked ? [{ code: 'receiving_reversal_used_stock', product_id: 10 }] : [], result: null };
    operations.set(body.request_id, value);
    if (invalidPreview) { invalidPreview = false; return response({ success: true }); }
    return response(value);
  }
  const id = path.split('/').at(-2), review = operations.get(id);
  assert(review); assert.deepEqual(body, { preview_hash: review.preview_hash, confirmed: true });
  if (stale) return response({ detail: { code: 'receiving_operation_stale' } }, 409);
  if (!applied.has(id)) {
    commits++;
    applied.set(id, { id, session_id: 12, supplier_code: 'fixture', invoice_number: 'INV', action: review.action,
      reason: review.reason, actor: 'user:1', status: review.after_status, archived: review.action === 'archive',
      posting_revision: 1, stock_effect: review.stock_effect, impacts: review.impacts,
      movement_ids: review.stock_effect === 'unchanged' ? [] : [50], completed_at: '2026-11-09T12:00:00Z' });
  }
  if (lostApply) { lostApply = false; throw new TypeError('Synthetic response lost after commit'); }
  return response(applied.get(id));
};
const onChanged = async () => { changes++; }, onLockChange = value => { locked = value; };
async function render(key, props = {}) { await act(async () => { root.render(React.createElement(ReceivingRecoveryPanel,
  { key, supplier: 'fixture', sessionId: '12', status: 'completed', archived: false, reversed: false,
    disabled: false, onChanged, onLockChange, ...props })); await tick(); }); }
async function click(id) { await act(async () => { const button = find(id); assert(button && !button.disabled, id); button.click(); await tick(); }); }
async function reason(value = 'Correct duplicate receipt') { await act(async () => {
  const element = find('receiving-recovery-reason');
  Object.getOwnPropertyDescriptor(dom.window.HTMLTextAreaElement.prototype, 'value').set.call(element, value);
  element.dispatchEvent(new dom.window.Event('input', { bubbles: true })); await tick();
}); }
async function preview() { await reason(); await click('receiving-recovery-preview'); }

(async () => {
  await render('first'); assert(find('receiving-recovery-preview').disabled, 'Reason is mandatory');
  await preview(); assert(find('receiving-recovery-review').textContent.includes('-2 pcs'));
  assert(find('receiving-recovery-apply').disabled, 'Exact impact needs explicit confirmation');
  await click('receiving-recovery-confirm'); lostApply = true; await click('receiving-recovery-apply');
  assert(find('receiving-recovery-pending')); assert(locked); assert.equal(commits, 1); assert.equal(changes, 0);
  const originalApply = lastCommand();
  await render('reload'); assert(find('receiving-recovery-pending'), 'Lost response survives reload');
  await click('receiving-recovery-retry'); assert.equal(commits, 1); assert.equal(changes, 1); assert(!locked);
  assert.deepEqual(lastCommand(), originalApply, 'Retry preserves exact path and body');
  assert(find('receiving-recovery-history-row').textContent.includes('Correct duplicate receipt'), 'Audit shows the durable correction reason');
  assert(find('receiving-recovery-history-row').textContent.includes('PART: -2 pcs'), 'Audit shows actual stock effect');

  await render('malformed'); invalidPreview = true; await preview();
  assert(find('receiving-recovery-pending'), 'HTTP 200 without scoped review is not acknowledgment');
  const originalPreview = lastCommand(); await click('receiving-recovery-retry');
  assert.deepEqual(lastCommand(), originalPreview); assert(find('receiving-recovery-review'));
  stale = true; await click('receiving-recovery-confirm'); await click('receiving-recovery-apply'); stale = false;
  assert(!find('receiving-recovery-pending')); assert(!find('receiving-recovery-review')); assert.equal(commits, 1);

  await render('blocked'); blocked = true; await preview(); blocked = false;
  assert(!find('receiving-recovery-apply'), 'Consumed goods cannot be reversed');
  await render('archive', { status: 'paused' }); await preview();
  assert(find('receiving-recovery-no-stock')); await click('receiving-recovery-confirm'); await click('receiving-recovery-apply');
  assert.equal(applied.get(lastCommand().path.split('/').at(-2)).movement_ids.length, 0);
  await render('restore', { status: 'cancelled', archived: true });
  assert.equal(find('receiving-recovery-action').options.length, 1); assert.equal(find('receiving-recovery-action').value, 'restore');

  let location;
  function Destination() { location = useLocation(); return React.createElement('p', null, 'Receipt detail'); }
  await act(async () => { root.render(React.createElement(MemoryRouter, { initialEntries: ['/receiving?supplier=fixture'] },
    React.createElement(Routes, null, React.createElement(Route, { path: '/receiving', element: React.createElement(ReceivingPage) }),
      React.createElement(Route, { path: '/receiving/:id', element: React.createElement(Destination) })))); await tick(); });
  await click('receiving-include-archived');
  assert(calls.some(call => call.path.includes('include_archived=true'))); assert(find('receiving-archived-badge'));
  const beforeNavigation = calls.length;
  // Resolve translated text using the same catalog as the real page.
  const label = require('../src/i18n/index.ts').default.t('receiving.recovery.openRecovery');
  const open = [...document.querySelectorAll('button')].find(button => button.textContent.includes(label));
  assert(open); await act(async () => { open.click(); await tick(); });
  assert.equal(location.pathname, '/receiving/INV'); assert.equal(location.state.sessionId, '12');
  assert.equal(calls.length, beforeNavigation, 'Opening recovery is read-only navigation');
  await act(async () => root.unmount());
  console.log('receiving-recovery-ui: preview, exact effects, durable replay, stale review, guards, archive and read-only navigation passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
