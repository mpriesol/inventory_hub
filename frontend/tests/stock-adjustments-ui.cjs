/* Physical recount UI against synthetic local APIs; no real warehouse or shop writes. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'https://hub.example.test/stock' });
global.window = dom.window; global.document = dom.window.document;
Object.defineProperty(global, 'navigator', { value: dom.window.navigator, configurable: true });
global.HTMLElement = dom.window.HTMLElement; global.sessionStorage = dom.window.sessionStorage;
global.IS_REACT_ACT_ENVIRONMENT = true;
for (const ext of ['.ts', '.tsx']) require.extensions[ext] = (module, filename) => module._compile(ts.transpileModule(
  fs.readFileSync(filename, 'utf8').replace(/import\.meta\.env/g, '{}'),
  { compilerOptions: { jsx: ts.JsxEmit.React, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, esModuleInterop: true } }).outputText, filename);
require.extensions['.css'] = () => {};
const React = require('react'); const { act } = React; const { createRoot } = require('react-dom/client');
const i18n = require('../src/i18n/index.ts').default;
const { StockAdjustmentPanel } = require('../src/components/product/StockAdjustmentPanel.tsx');
const { unlockHub } = require('../src/api/access.ts');
const root = createRoot(document.getElementById('root'));
const tick = () => new Promise(resolve => setTimeout(resolve, 5));
const find = id => document.querySelector(`[data-testid="${id}"]`);
const required = id => { const el = find(id); assert(el, `${id} exists`); return el; };
async function click(id) { await act(async () => { required(id).click(); await tick(); }); }
async function input(id, value) { await act(async () => {
  const el = required(id); assert(!el.matches(':disabled'), `${id} is editable`);
  Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, 'value').set.call(el, value);
  el.dispatchEvent(new dom.window.Event('input', { bubbles: true })); await tick();
}); }
const reply = (data, status = 200) => ({ ok: status < 400, status, json: async () => JSON.parse(JSON.stringify(data)) });
const key = (id = 1, warehouse = 'main') => `stock-adjustment:${id}:${warehouse}`;
const writes = [], reads = [], prepared = new Map(), committed = new Map(), counts = new Map();
let nextResponse = '', heldResponse, nextReadHeld = false, heldRead, rejectCode = '', movementCount = 0;
let oldChanges = 0, newChanges = 0;
const missingBalances = new Set([3]);
const quantity = product => String(counts.get(product) ?? 5);
const stock = (product, warehouse) => ({ product_id: product, sku: `PART-${product}`, warehouse: { id: warehouse === 'main' ? 2 : 3, code: warehouse, name: `${warehouse} warehouse` },
  balance: missingBalances.has(product) ? null : { qty_on_hand: quantity(product), qty_reserved: product === 3 ? '0' : '1', qty_quarantined: product === 3 ? '0' : '1', qty_available: String(Number(quantity(product)) - (product === 3 ? 0 : 2)) },
  valuation: { mode: missingBalances.has(product) ? 'missing' : 'fifo', revision: 1, known_value: null, provisional_value: '0', unknown_qty: quantity(product), provisional_qty: '0',
    quarantined_qty: product === 3 ? '0' : '1', value_complete: product === 3 && !missingBalances.has(product), avg_cost: product === 3 && !missingBalances.has(product) ? '0' : null, total_value: product === 3 && !missingBalances.has(product) ? '0' : null }, layers: [], total: 0, limit: 50, offset: 0, snapshot_hash: 'a'.repeat(64) });
global.fetch = async (url, options = {}) => {
  const parsed = new URL(url, 'https://hub.example.test'), path = parsed.pathname;
  assert(path.startsWith('/api/fifo/stock') || path.startsWith('/api/stock-adjustments/'), 'Only local recount APIs are used');
  assert.equal(options.cache, 'no-store'); assert.equal(options.headers.Authorization, 'Bearer fixture-token');
  if (options.method === 'GET') {
    reads.push(path); const result = stock(Number(parsed.searchParams.get('product_id')), parsed.searchParams.get('warehouse_code'));
    if (nextReadHeld) { nextReadHeld = false; return new Promise(resolve => { heldRead = () => resolve(reply(result)); }); }
    return reply(result);
  }
  assert.equal(options.method, 'POST');
  const body = JSON.parse(options.body); writes.push({ path, body });
  if (rejectCode) { const code = rejectCode; rejectCode = ''; return reply({ detail: { code, message: code } }, 409); }
  let result;
  if (path.endsWith('/preview')) {
    assert.match(body.request_id, /^[0-9a-f-]{36}$/i);
    if (!prepared.has(body.request_id)) {
      const product = Number(body.sku.split('-').at(-1));
      const initialZero = missingBalances.has(product) && body.counted_quantity === '0';
      prepared.set(body.request_id, { id: body.request_id, status: 'prepared', preview_hash: 'b'.repeat(64),
        preview: { ...body, product_id: product, initial_zero_count: initialZero, before_quantity: initialZero ? null : quantity(product), delta: initialZero ? '0' : String(Number(body.counted_quantity) - Number(quantity(product))) }, result: null });
    }
    result = prepared.get(body.request_id);
  } else {
    assert(path.endsWith('/apply')); const id = path.split('/')[3], preview = prepared.get(id);
    assert(preview, 'An apply always refers to an existing frozen preview');
    assert.equal(body.preview_hash, preview.preview_hash); assert.equal(body.confirmed, true);
    assert.equal(body.quantities_verified, true); assert.equal(body.costs_documented, true);
    if (!committed.has(id)) {
      if (!preview.preview.initial_zero_count) movementCount++;
      missingBalances.delete(preview.preview.product_id);
      counts.set(preview.preview.product_id, Number(preview.preview.counted_quantity));
      committed.set(id, { adjustment_id: id, movement_id: preview.preview.initial_zero_count ? null : movementCount, initial_zero_count: preview.preview.initial_zero_count, before_quantity: preview.preview.before_quantity, product_id: preview.preview.product_id,
        warehouse_id: 2, counted_quantity: preview.preview.counted_quantity, delta: preview.preview.delta });
    }
    result = committed.get(id);
  }
  const mode = nextResponse; nextResponse = '';
  if (mode === 'lost') throw new TypeError('Synthetic connection lost after commit');
  if (mode === 'bad-body') return { ok: true, status: 200, json: async () => { throw new SyntaxError('Truncated response'); } };
  if (mode === 'http502') return reply({ detail: 'Proxy lost upstream response' }, 502);
  if (mode === 'malformed') return reply({});
  if (mode === 'zero-without-audit') return reply({ ...result, movement_id: null, initial_zero_count: undefined, before_quantity: null, counted_quantity: '0', delta: '0' });
  if (mode === 'zero-with-nonzero-count') return reply({ ...result, movement_id: null, initial_zero_count: true, before_quantity: null, counted_quantity: '1', delta: '0' });
  if (mode === 'zero-with-wrong-product') return reply({ ...result, movement_id: null, initial_zero_count: true, product_id: 999, before_quantity: null, counted_quantity: '0', delta: '0' });
  if (mode === 'zero-with-wrong-adjustment') return reply({ ...result, adjustment_id: '00000000-0000-4000-8000-000000000000', movement_id: null, initial_zero_count: true, before_quantity: null, counted_quantity: '0', delta: '0' });
  if (mode === 'nonpositive-movement-id') return reply({ ...result, movement_id: -1 });
  if (mode === 'held') return new Promise(resolve => { heldResponse = () => resolve(reply(result)); });
  return reply(result);
};
async function render(product = 1, warehouse = 'main', onChanged = () => { oldChanges++; }) {
  await act(async () => { root.render(React.createElement(StockAdjustmentPanel, { productId: product, sku: `PART-${product}`, warehouseCode: warehouse, onChanged })); await tick(); });
}
async function remount(product = 1, warehouse = 'main', onChanged) {
  await act(async () => { root.render(null); await tick(); }); await render(product, warehouse, onChanged);
}
async function fill(count) {
  await input('adjustment-count', count); await input('adjustment-operator', 'Counter');
  await input('adjustment-source', 'COUNT-LOCAL-1'); await input('adjustment-reason', 'Physical count differs');
}
async function preview(count) { await fill(count); await click('adjustment-confirm'); await click('adjustment-submit'); }
async function apply() { await click('adjustment-confirm'); await click('adjustment-submit'); }
(async () => {
  await i18n.changeLanguage('en');
  await render(); assert.equal(reads.length, 0, 'Locked panel never loads protected stock');
  assert.equal(find('adjustment-count'), null);
  await act(async () => { unlockHub('fixture-token'); await tick(); });
  assert.equal(writes.length, 0, 'Opening a recount never creates a movement or preview');
  assert(required('adjustment-submit').disabled);
  assert(document.querySelector('[data-action-effects="hub-write"]'));
  assert(!document.querySelector('[data-action-effects*="upgates"]'));

  await fill('7'); await click('adjustment-confirm'); await input('adjustment-reason', 'Rechecked count');
  assert(!required('adjustment-confirm').checked, 'Editing a documented value revokes confirmation');
  assert(required('adjustment-submit').disabled);
  for (const value of ['-1', '1.5', '1000000000', '']) {
    await input('adjustment-count', value); await click('adjustment-confirm');
    assert(required('adjustment-submit').disabled, 'Invalid physical counts cannot submit');
  }
  await input('adjustment-count', '7'); await click('adjustment-known'); await input('adjustment-cost', '12.12345'); await click('adjustment-confirm');
  assert(required('adjustment-submit').disabled, 'More than four cost decimals cannot submit');
  await click('adjustment-known'); assert.equal(find('adjustment-cost'), null);
  await click('adjustment-confirm'); const beforePreview = writes.length;
  await act(async () => { required('adjustment-submit').click(); required('adjustment-submit').click(); await tick(); });
  assert.equal(writes.length, beforePreview + 1, 'Duplicate clicks create one request UUID');
  assert.equal(movementCount, 0, 'Preview has no ledger effect');
  const unknown = writes.at(-1); assert.equal(unknown.body.cost_status, 'unknown'); assert.equal(unknown.body.unit_cost, null);
  assert.equal(unknown.body.counted_quantity, '7'); assert.equal(unknown.body.sku, 'PART-1'); assert.equal(unknown.body.warehouse_code, 'main');
  assert(required('adjustment-preview')); assert(required('adjustment-count').matches(':disabled'));
  assert(required('adjustment-preview-cost').textContent.includes(i18n.t('stockAdjustment.unknown')), 'Frozen unknown cost is explicitly reviewable');
  assert(required('adjustment-submit').disabled, 'A preview needs a new explicit confirmation');
  await apply(); assert.equal(movementCount, 1); assert.equal(quantity(1), '7'); assert.equal(oldChanges, 1);
  assert.equal(find('adjustment-preview'), null); assert.equal(required('adjustment-count').value, '7', 'Apply refreshes the actual stock');
  assert(document.body.textContent.includes(i18n.t('stockAdjustment.completed')));
  await render(2);
  assert(!document.body.textContent.includes(i18n.t('stockAdjustment.completed')), 'Success for another product is cleared on context change');
  await render();

  await fill('8'); await click('adjustment-known'); await input('adjustment-cost', '0'); await click('adjustment-confirm'); await click('adjustment-submit');
  assert.equal(writes.at(-1).body.cost_status, 'known'); assert.equal(writes.at(-1).body.unit_cost, '0', 'An explicit documented zero stays distinct from unknown');
  await apply();
  await fill('4'); assert.equal(find('adjustment-known'), null, 'A decrease never asks for replacement receipt costs');
  await click('adjustment-confirm'); await click('adjustment-submit');
  assert.equal(writes.at(-1).body.unit_cost, null); assert.equal(writes.at(-1).body.cost_status, 'unknown', 'Decrease cost is derived by the server from receipt layers');
  assert(required('adjustment-preview-cost').textContent.includes(i18n.t('stockAdjustment.derivedCost')));
  await apply();

  nextResponse = 'lost'; await preview('6');
  const lostPreview = writes.at(-1), movementsBeforeRetry = movementCount;
  assert(required('adjustment-retry')); assert.equal(find('adjustment-preview'), null);
  await remount(); await click('adjustment-retry');
  assert.deepEqual(writes.at(-1), lostPreview, 'Lost preview replay preserves UUID, timestamp, and exact inputs');
  assert.equal(movementCount, movementsBeforeRetry);
  assert(required('adjustment-preview')); await apply();

  await fill('7'); await click('adjustment-known'); await input('adjustment-cost', '12.3400');
  nextResponse = 'lost'; await click('adjustment-confirm'); await click('adjustment-submit');
  const documentedPreview = writes.at(-1);
  await remount(); await click('adjustment-retry');
  assert.deepEqual(writes.at(-1), documentedPreview);
  assert.equal(required('adjustment-count').value, '7'); assert.equal(required('adjustment-cost').value, '12.3400');
  assert(required('adjustment-known').checked, 'Recovered preview restores its actual cost classification');
  assert(required('adjustment-preview-cost').textContent.includes('12.3400'), 'The frozen acquisition cost is visible before confirmation after remount');
  assert(required('adjustment-preview').textContent.includes('PART-1') && required('adjustment-preview').textContent.includes('Counter'));
  assert(required('adjustment-submit').disabled, 'Recovering documented cost still requires fresh confirmation');
  await apply();

  for (const responseMode of ['lost', 'http502', 'bad-body', 'malformed', 'zero-without-audit', 'zero-with-nonzero-count', 'zero-with-wrong-product', 'zero-with-wrong-adjustment', 'nonpositive-movement-id']) {
    await preview(String(Number(quantity(1)) + 1)); nextResponse = responseMode;
    const before = movementCount;
    await apply(); const request = writes.at(-1);
    assert.equal(movementCount, before + 1);
    assert(required('adjustment-retry'), `${responseMode}: uncertain result is recoverable`);
    assert(required('adjustment-submit').disabled, `${responseMode}: a second adjustment is blocked`);
    assert.deepEqual(JSON.parse(sessionStorage.getItem(key())), request, `${responseMode}: exact apply persisted`);
    await remount(); await click('adjustment-retry');
    assert.deepEqual(writes.at(-1), request, `${responseMode}: replay targets the same frozen adjustment`);
    assert.equal(movementCount, before + 1, `${responseMode}: the same result does not post twice`);
    assert.equal(sessionStorage.getItem(key()), null);
  }

  await preview(String(Number(quantity(1)) + 1)); rejectCode = 'fifo_preview_stale'; const beforeStale = movementCount;
  counts.set(1, Number(quantity(1)) + 2); const externallyChangedQuantity = quantity(1);
  await apply(); assert.equal(movementCount, beforeStale);
  assert.equal(find('adjustment-retry'), null, 'Definitively stale previews can be replaced');
  assert.equal(find('adjustment-preview'), null); assert(!required('adjustment-count').matches(':disabled'));
  assert(document.body.textContent.includes(i18n.t('stockAdjustment.errors.fifo_preview_stale')));
  const beforeReload = writes.length; await click('adjustment-reload');
  assert.equal(required('adjustment-count').value, externallyChangedQuantity, 'Reload updates stale recorded stock before replanning');
  assert.equal(writes.length, beforeReload, 'Refreshing stock does not post a correction');
  assert(!required('adjustment-confirm').checked);
  await input('adjustment-count', String(Number(externallyChangedQuantity) - 1));
  assert.equal(find('adjustment-known'), null, 'Direction and cost classification use the refreshed balance');

  // Supplier-only products need an explicitly verified physical zero, not an inferred balance.
  await render(3); const beforeZeroWrites = writes.length, beforeZeroMovements = movementCount;
  assert.equal(required('adjustment-count').value, '', 'Missing balance never pre-fills a presumed zero');
  assert(document.body.textContent.includes(i18n.t('stockAdjustment.unknownQuantity')));
  assert(required('adjustment-initial-count-help')); assert(required('adjustment-submit').disabled);
  await input('adjustment-operator', 'Zero counter'); await input('adjustment-source', 'COUNT-ZERO-1'); await input('adjustment-reason', 'No physical units found');
  await click('adjustment-confirm'); assert(required('adjustment-submit').disabled, 'A count must be explicitly entered before even previewing zero');
  await input('adjustment-count', '0'); assert(!required('adjustment-confirm').checked);
  await click('adjustment-confirm'); await click('adjustment-submit');
  assert.equal(writes.length, beforeZeroWrites + 1); assert.equal(writes.at(-1).body.counted_quantity, '0');
  assert.equal(writes.at(-1).body.unit_cost, null); assert.equal(writes.at(-1).body.cost_status, 'unknown');
  assert(required('adjustment-preview').textContent.includes(i18n.t('stockAdjustment.initialZeroPreview')));
  assert(required('adjustment-preview-cost').textContent.includes(i18n.t('stockAdjustment.zeroNoMovement')));
  assert(required('adjustment-submit').disabled && movementCount === beforeZeroMovements, 'Verified zero still requires a second explicit confirmation');
  nextResponse = 'lost'; await apply(); const lostZero = writes.at(-1), zeroCallbacks = oldChanges;
  assert(required('adjustment-retry')); assert.equal(movementCount, beforeZeroMovements, 'Initial zero creates an audit but no movement');
  await remount(3); await click('adjustment-retry');
  assert.deepEqual(writes.at(-1), lostZero, 'Initial zero recovery uses the original adjustment');
  assert.equal(find('adjustment-retry'), null); assert.equal(sessionStorage.getItem(key(3)), null);
  assert.equal(required('adjustment-count').value, '0'); assert.equal(movementCount, beforeZeroMovements);
  assert.equal(oldChanges, zeroCallbacks + 1); assert(document.body.textContent.includes(i18n.t('stockAdjustment.zeroCompleted')));
  assert(!document.body.textContent.includes(i18n.t('stockAdjustment.completed')), 'Zero initialization never claims a movement was posted');
  await render();

  // A late result from an old page cannot remove a newer saved apply request.
  nextResponse = 'held'; await preview(String(Number(quantity(1)) + 1)); const oldPreviewResponse = heldResponse;
  await remount(); await click('adjustment-retry'); assert(required('adjustment-preview'));
  nextResponse = 'lost'; await apply(); const newerRequest = JSON.parse(sessionStorage.getItem(key()));
  assert(newerRequest.path.endsWith('/apply'));
  await act(async () => { oldPreviewResponse(); await tick(); });
  assert.deepEqual(JSON.parse(sessionStorage.getItem(key())), newerRequest, 'Old page acknowledgement cannot erase new pending apply');
  assert(required('adjustment-retry')); await click('adjustment-retry');

  // Late reads/writes cannot populate another product or invoke its callback.
  await act(async () => { root.render(null); await tick(); }); nextReadHeld = true;
  await render(); const oldReadResponse = heldRead;
  await render(2, 'overflow', () => { newChanges++; }); await input('adjustment-operator', 'Counter for part two');
  await act(async () => { oldReadResponse(); await tick(); });
  assert.equal(required('adjustment-operator').value, 'Counter for part two');
  assert(document.body.textContent.includes('PART-2') && document.body.textContent.includes('overflow warehouse'));
  await render(); nextResponse = 'held'; await preview(String(Number(quantity(1)) + 1)); const oldCommandResponse = heldResponse;
  const oldCallbacks = oldChanges;
  await render(2, 'main', () => { newChanges++; }); await input('adjustment-operator', 'New context');
  await act(async () => { oldCommandResponse(); await tick(); });
  assert.equal(find('adjustment-preview'), null); assert.equal(required('adjustment-operator').value, 'New context');
  assert.equal(oldChanges, oldCallbacks); assert.equal(newChanges, 0);
  await preview('6'); nextResponse = 'held'; await apply(); const abandonedApplyResponse = heldResponse;
  await act(async () => { root.render(null); await tick(); });
  await act(async () => { abandonedApplyResponse(); await tick(); });
  assert.equal(newChanges, 0, 'An unmounted apply result cannot invoke onChanged');
  await render(2); assert.equal(required('adjustment-count').value, '6', 'A fresh read recovers actual completed stock');
  await act(async () => { root.unmount(); });
  console.log('Stock adjustments UI: documented preview/apply, cost classification, durable lost-response replay, duplicate clicks and stale/unmounted contexts passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
