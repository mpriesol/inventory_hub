/* FIFO operator workflows against synthetic APIs. No live writes. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'https://hub.example.test/products' });
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
const { FifoPanel } = require('../src/components/product/FifoPanel.tsx');
const { unlockHub } = require('../src/api/access.ts');
const root = createRoot(document.getElementById('root'));
const tick = () => new Promise(resolve => setTimeout(resolve, 5));
const find = id => document.querySelector(`[data-testid="${id}"]`);
const required = id => { const el = find(id); assert(el, `${id} exists`); return el; };
async function click(id) { await act(async () => { required(id).click(); await tick(); }); }
async function input(id, value) { await act(async () => {
  const el = required(id); const proto = el.tagName === 'SELECT' ? dom.window.HTMLSelectElement.prototype : dom.window.HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, value);
  el.dispatchEvent(new dom.window.Event(el.tagName === 'SELECT' ? 'change' : 'input', { bubbles: true })); await tick();
}); }
const reply = data => ({ ok: true, json: async () => JSON.parse(JSON.stringify(data)) });
const allocation = { id: 1, layer_id: 10, sequence: 0, quantity: '2.000', returned_quantity: '0.000',
  unit_cost_at_issue: null, total_cost_at_issue: null, cost_status_at_issue: 'unknown',
  unit_cost_current: null, total_cost_current: null, cost_status_current: 'unknown' };
let mode = 'fifo', lost = false, deferInspection = false, deferCommand = false;
const writes = []; let lastPreviewBody, resolveInspection, resolveCommand;
const stock = (productId = 1) => ({ product_id: productId, sku: productId === 1 ? 'PART-A' : 'PART-B', warehouse: { id: 2, code: 'main', name: 'Main warehouse' },
  balance: { qty_on_hand: '3.000', qty_reserved: '1.000', qty_quarantined: '1.000', qty_available: '1.000', last_purchase_price: null },
  valuation: { mode, revision: 1, known_value: '0.0000', provisional_value: '0.0000', unknown_qty: '3.000', provisional_qty: '0.000',
    quarantined_qty: '1.000', value_complete: false, avg_cost: null, total_value: null, next_layer: { id: 10, unit_cost: null, cost_status: 'unknown' } },
  snapshot_hash: 'a'.repeat(64), total: mode === 'fifo' ? 1 : 0, limit: 50, offset: 0,
  layers: mode === 'fifo' ? [{ id: 10, root_cost_layer_id: 10, physical_received_at: '2026-09-01T10:00:00Z',
    quantity_original: '5.000', quantity_remaining: '1.000', unit_cost: null, cost_status: 'unknown', stock_status: 'quarantine', cost_revision: 0, provenance: { source_reference: 'Receipt-A' } }] : [] });
global.fetch = async (url, options = {}) => {
  assert.equal(options.cache, 'no-store'); assert.equal(options.headers.Authorization, 'Bearer synthetic-token');
  const parsed = new URL(url, 'https://hub.example.test'), path = parsed.pathname;
  if (options.method === 'POST') {
    const body = JSON.parse(options.body); writes.push({ path, body });
    if (path.endsWith('/preview')) { lastPreviewBody = body; return reply({ id: body.request_id, status: 'prepared', preview_hash: 'b'.repeat(64), preview_data: body }); }
    if (lost) { lost = false; throw new TypeError('Response lost after possible commit'); }
    if (deferCommand) return new Promise(resolve => { resolveCommand = () => resolve(reply({ status: 'completed' })); });
    return reply({ status: 'completed' });
  }
  if (path === '/api/fifo/options') return reply({ warehouses: [{ id: 2, code: 'main', name: 'Main warehouse' }] });
  if (path === '/api/fifo/stock') return reply(stock(Number(parsed.searchParams.get('product_id'))));
  if (path === '/api/fifo/history') return reply({ movements: [{ id: 20, movement_type: 'sale_out', quantity: '-2.000', unit_cost: null,
    total_cost: null, balance_after: '3.000', reference_id: parsed.searchParams.get('product_id') === '1' ? 'ORDER-A' : 'ORDER-B', created_at: '2026-09-10T10:00:00Z' }], total: 1 });
  if (path === '/api/fifo/issues/20/return-options') {
    const result = { issue_movement_id: 20, issued_quantity: '2.000', returned_quantity: '0.000', returnable_quantity: '2.000', allocations: [allocation] };
    if (deferInspection) return new Promise(resolve => { resolveInspection = () => resolve(reply(result)); });
    return reply(result);
  }
  if (path === '/api/fifo/costs/10') return reply({ root_layer_id: 10, revision: 4, unit_cost: null, cost_status: 'unknown', allocations: [allocation],
    net_consumption: { quantity: '2.000', known_cost: '0.0000', provisional_cost: '0.0000', total_cost: null, value_complete: false }, revisions: [] });
  throw new Error(`Unexpected fixture route ${path}`);
};
(async () => {
  await i18n.changeLanguage('en'); unlockHub('synthetic-token');
  await act(async () => { root.render(React.createElement(FifoPanel, { productId: 1, warehouseCode: 'main' })); await tick(); });
  assert(document.body.textContent.includes('Valuation is incomplete'));
  assert(document.body.textContent.includes('Unknown'));
  assert.equal(writes.length, 0, 'Opening stock information never writes');
  await click('fifo-receive'); assert(required('fifo-submit').disabled);
  await input('fifo-operator', 'Warehouse operator'); await input('fifo-source', 'DELIVERY-1');
  await input('fifo-layer-0-quantity', '2'); await input('fifo-layer-0-source_reference', 'DELIVERY-1');
  assert(required('fifo-layer-0-cost').disabled, 'Unknown cost is not an editable zero');
  await click('fifo-confirm'); await input('fifo-layer-0-quantity', '3');
  assert(!required('fifo-confirm').checked && required('fifo-submit').disabled, 'Changing a confirmed receipt quantity clears authorization');
  await input('fifo-layer-0-quantity', '2');
  await click('fifo-confirm'); await click('fifo-submit');
  assert.equal(writes.length, 1); assert.equal(lastPreviewBody.unit_cost, null);
  assert.equal(lastPreviewBody.cost_status, 'unknown'); assert.equal(typeof lastPreviewBody.quantity, 'string');
  assert(required('fifo-submit').disabled, 'Preview requires a fresh confirmation');
  await click('fifo-confirm'); await click('fifo-submit');
  assert(writes[1].path.endsWith('/apply')); assert.equal(writes[1].body.quantities_verified, true);
  await click('fifo-revise-10'); await input('fifo-cost-status', 'known'); await input('fifo-cost', '80.1234');
  await input('fifo-source', 'INVOICE-80'); await input('fifo-reason', 'Invoice received'); await click('fifo-confirm');
  await input('fifo-cost', '80.5678');
  assert(!required('fifo-confirm').checked && required('fifo-submit').disabled, 'Changing confirmed acquisition cost requires fresh authorization');
  await input('fifo-cost', '80.1234'); await click('fifo-confirm'); await click('fifo-submit');
  const revision = writes.at(-1); assert.equal(revision.path, '/api/fifo/costs/revise');
  assert.equal(revision.body.expected_revision, 4); assert.equal(revision.body.new_unit_cost, '80.1234');
  await click('fifo-return-20'); await input('fifo-quantity', '3'); await input('fifo-source', 'RMA-1'); await input('fifo-reason', 'Customer returned goods');
  await click('fifo-confirm'); const before = writes.length; await click('fifo-submit'); assert.equal(writes.length, before, 'Over-return blocked before POST');
  await input('fifo-quantity', '1');
  assert(!required('fifo-confirm').checked && required('fifo-submit').disabled, 'Correcting a return quantity clears the earlier confirmation');
  lost = true; await click('fifo-confirm'); await click('fifo-submit');
  const original = writes.at(-1); assert(required('fifo-retry')); assert(required('fifo-submit').disabled);
  assert(sessionStorage.getItem('fifo-pending:1:main'), 'Uncertain command can recover after remount');
  await act(async () => { root.render(null); await tick(); });
  await act(async () => { root.render(React.createElement(FifoPanel, { productId: 1, warehouseCode: 'main' })); await tick(); });
  await click('fifo-retry'); assert.deepEqual(writes.at(-1), original, 'Retry uses the exact same UUID and payload');
  assert.equal(sessionStorage.getItem('fifo-pending:1:main'), null);
  await click('fifo-release-10'); await input('fifo-quantity', '1'); await input('fifo-reason', 'Goods checked and undamaged'); await click('fifo-confirm'); await click('fifo-submit');
  assert.equal(writes.at(-1).body.source_layer_id, 10); assert.equal(writes.at(-1).body.target_warehouse_id, 2); assert.equal(writes.at(-1).body.condition_verified, true);

  deferInspection = true; await click('fifo-return-20');
  assert(required('fifo-receive').disabled, 'Inspecting the old product is initially busy');
  await act(async () => { root.render(React.createElement(FifoPanel, { productId: 2, warehouseCode: 'main' })); await tick(); });
  assert(document.body.textContent.includes('ORDER-B'));
  assert(!required('fifo-receive').disabled, 'Changing product context clears busy state from an unresolved inspection');
  await click('fifo-receive'); await input('fifo-operator', 'Product B operator');
  await act(async () => { resolveInspection(); await tick(); });
  assert.equal(required('fifo-operator').value, 'Product B operator');
  assert(!required('fifo-form').textContent.includes('#20'), 'A late return inspection cannot insert another product’s issue into the current form');
  deferInspection = false;

  let oldChanged = 0, newChanged = 0;
  await act(async () => { root.render(React.createElement(FifoPanel, { productId: 1, warehouseCode: 'main', onChanged: () => { oldChanged++; } })); await tick(); });
  await click('fifo-return-20'); await input('fifo-quantity', '1'); await input('fifo-source', 'RMA-ASYNC'); await input('fifo-reason', 'Received and inspected'); await click('fifo-confirm');
  deferCommand = true; const beforeDuplicate = writes.length;
  await act(async () => { required('fifo-submit').click(); required('fifo-submit').click(); await tick(); });
  assert.equal(writes.length, beforeDuplicate + 1, 'Two clicks in the same tick create only one FIFO return command');
  assert.equal(writes.at(-1).path, '/api/fifo/returns');
  await act(async () => { root.render(React.createElement(FifoPanel, { productId: 2, warehouseCode: 'main', onChanged: () => { newChanged++; } })); await tick(); });
  assert(!required('fifo-receive').disabled && !find('fifo-form'), 'A pending command from another product does not lock or restore its form');
  await act(async () => { resolveCommand(); await tick(); });
  assert.equal(oldChanged, 0, 'A late command response never invokes the old product callback');
  assert.equal(newChanged, 0, 'A late command response never invokes the new product callback either');
  assert(document.body.textContent.includes('ORDER-B') && !find('fifo-form'));
  deferCommand = false;

  mode = 'legacy';
  await act(async () => { root.render(null); await tick(); });
  await act(async () => { root.render(React.createElement(FifoPanel, { productId: 1, warehouseCode: 'main' })); await tick(); });
  await click('fifo-cutover'); assert(required('fifo-layer-0-cost').disabled);
  await input('fifo-operator', 'Operator'); await input('fifo-source', 'COUNT-1'); await input('fifo-layer-0-quantity', '3'); await input('fifo-layer-0-source_reference', 'COUNT-1');
  await click('fifo-confirm'); await click('fifo-submit'); assert.equal(writes.at(-1).path, '/api/fifo/cutovers/preview');
  assert.equal(writes.at(-1).body.layers[0].unit_cost, null, 'Cutover never inherits invented historical price');
  await act(async () => root.unmount()); console.log('FIFO UI: unknown cost, receipt preview, revision CAS, return limit, durable retry, duplicate/context guards, quarantine and cutover passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
