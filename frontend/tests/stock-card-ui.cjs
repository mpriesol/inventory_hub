/* Unified card interactions against synthetic APIs only; no real stock/shop writes. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html><button id="launcher">Open card</button><div id="root"></div>', { url: 'https://hub.example.test/products' });
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
const { StockCard } = require('../src/components/product/StockCard.tsx');
const helpers = require('../src/components/product/stockCardHelpers.ts');
const { setHubUser } = require('../src/api/access.ts');
const root = createRoot(document.getElementById('root'));
const tick = () => new Promise(resolve => setTimeout(resolve, 8));
const find = id => document.querySelector(`[data-testid="${id}"]`);
const required = id => { const value = find(id); assert(value, `${id} exists`); return value; };
const visible = element => element && !element.closest('[hidden]');
async function click(id) { await act(async () => { const el = required(id); assert(!el.matches(':disabled'), `${id} is enabled`); el.click(); await tick(); }); }
async function input(id, value) { await act(async () => {
  const el = required(id); assert(!el.matches(':disabled'), `${id} is editable`);
  const proto = el.tagName === 'SELECT' ? dom.window.HTMLSelectElement.prototype : el.tagName === 'TEXTAREA' ? dom.window.HTMLTextAreaElement.prototype : dom.window.HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, value);
  el.dispatchEvent(new dom.window.Event(el.tagName === 'SELECT' ? 'change' : 'input', { bubbles: true })); await tick();
}); }
async function keyboard(key, shiftKey = false) { await act(async () => { document.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key, shiftKey, bubbles: true, cancelable: true })); await tick(); }); }
const clone = value => JSON.parse(JSON.stringify(value));
const reply = (value, status = 200) => ({ ok: status < 400, status, json: async () => clone(value) });
const actor = id => ({ id, username: `operator-${id}`, display_name: `Operator ${id}`, role: 'admin', active: true });
const options = { warehouses: [{ id: 2, code: 'other', name: 'Other warehouse', is_default: false }, { id: 1, code: 'main', name: 'Main warehouse', is_default: true }], shops: [{ id: 1, code: 'biketrek', name: 'BIKETREK' }, { id: 2, code: 'xtrek', name: 'xTrek' }], brands: [], page_sizes: [25], currency: 'EUR', price_basis: 'incl_vat' };
const quantities = { main: 5, other: 2 };
const records = new Map();
function seed(id) {
  const value = { id, canonical_product_id: id, sku: `PART-${id}`, is_active: true, supplier_linked: true, item_type: 'stock', stock_unit: 'pcs', item_definition_locked: true,
    group: { id: 10, code: 'HUB-GROUP-DIFFERENT', name: 'Family' }, attributes: [{ name: 'Size', value: 'M' }], eans: ['1234567890123'], supplier_codes: [{ supplier_code: 'supply', code: 'SUP-7' }],
    image_url: 'https://images.example.test/part.jpg', suppliers: [{ supplier_id: 1, supplier_code: 'supply', supplier_name: 'Primary supplier', supplier_product_id: 77, supplier_sku: 'SUP-7', product_url: 'https://supplier.example.test/exact-product', is_primary: true, is_active: true }],
    revision: 1, snapshot_hash: 'a'.repeat(64), common: { name: `Product ${id}`, brand: 'Brand', internal_note: null, image_url: 'https://images.example.test/part.jpg', description: '<p>Tire &amp; tube</p><script>window.bad = true</script>' },
    variant: { sale_price_gross: '19.99', vat_rate: '23', note: null },
    warehouse: null, stock: {}, overrides: { common: {}, variant: {}, shops: {} }, audit: [],
    shops: ['biketrek', 'xtrek'].map((code, index) => ({ shop_code: code, mapped: true, shop_url: `https://${code}.example.test/product`, shop_admin_url: `https://${code}.example.test/admin/product`,
      mapping: { code: `${code}-VARIANT`, parent_code: `${code}-PARENT`, external_product_id: String(40 + index), external_variant_id: String(400 + index), is_variant: true },
      overrides: { name: null, sale_price_gross: null, visible: null }, effective: { name: `Product ${id}`, sale_price_gross: '19.99', visible: true },
      observed: { name: `Product ${id}`, price: '19.99', price_basis: 'unknown', visible: true }, state: 'inherited' })) };
  records.set(id, value); return value;
}
seed(1); seed(2); seed(3);
function detail(id, warehouse) {
  const value = clone(records.get(id)); const tracked = !!warehouse && id !== 3;
  value.warehouse_context = warehouse ? { id: warehouse === 'main' ? 1 : 2, code: warehouse, name: `${warehouse === 'main' ? 'Main' : 'Other'} warehouse` } : null;
  value.warehouse = warehouse ? { code: warehouse, location: `${warehouse}-A1`, min_quantity: '2', ...(value.overrides.warehouses?.[warehouse] || {}) } : null;
  value.stock = { managed: true, known: tracked, tracking_status: tracked ? 'confirmed' : warehouse ? 'unconfirmed' : 'no_warehouse', qty_on_hand: tracked ? String(quantities[warehouse]) : null, qty_reserved: tracked ? '1' : null, qty_quarantined: tracked ? '0' : null, qty_available: tracked ? String(quantities[warehouse] - 1) : null, avg_cost: null, total_value: null };
  return value;
}
function fifo(id, warehouse) { const row = detail(id, warehouse); return { product_id: id, sku: row.sku, stock_unit: row.stock_unit, warehouse: row.warehouse_context,
  balance: row.stock.known ? { ...row.stock, last_purchase_price: null } : null,
  valuation: { mode: row.stock.known ? 'fifo' : 'unconfirmed', revision: 1, known_value: null, provisional_value: null, unknown_qty: row.stock.qty_on_hand, provisional_qty: '0', quarantined_qty: '0', value_complete: false, avg_cost: null, total_value: null, next_layer: null },
  layers: [], total: 0, limit: 50, offset: 0, snapshot_hash: 'b'.repeat(64) }; }
const reads = [], writes = [], saves = new Map(); let nextSave = '', holdRead = null, releaseRead, failDetail = false, changedRows = [], closeCount = 0;
global.fetch = async (url, request = {}) => {
  const parsed = new URL(url, 'https://hub.example.test'), path = parsed.pathname;
  assert(path.startsWith('/api/'), 'Only synthetic Hub APIs are used');
  assert.equal(request.cache, 'no-store'); assert.equal(request.credentials, 'same-origin');
  if (request.method === 'GET') {
    reads.push({ path, query: parsed.search });
    if (path === '/api/product-editor/options' || path === '/api/fifo/options') return reply(options);
    if (path === '/api/product-editor/products/resolve' || /^\/api\/product-editor\/products\/\d+$/.test(path)) {
      const id = path.endsWith('/resolve') ? Number(parsed.searchParams.get('sku').split('-').at(-1)) : Number(path.split('/').at(-1));
      const canonical = id === 999 ? 1 : id, warehouse = parsed.searchParams.get('warehouse_code') || '';
      const result = detail(canonical, warehouse);
      if (failDetail) { failDetail = false; throw new Error('Synthetic read failed'); }
      if (holdRead && holdRead.id === canonical && holdRead.warehouse === warehouse) {
        holdRead = null; return new Promise(resolve => { releaseRead = () => resolve(reply(result)); });
      }
      return reply(result);
    }
    if (path.startsWith('/api/product-editor/saves/')) return saves.has(path.split('/').at(-1)) ? reply(saves.get(path.split('/').at(-1))) : reply({ detail: { code: 'product_editor_save_missing' } }, 404);
    if (path === '/api/fifo/stock') return reply(fifo(Number(parsed.searchParams.get('product_id')), parsed.searchParams.get('warehouse_code')));
    if (path === '/api/fifo/history') return reply({ movements: [], total: 0 });
    if (path.endsWith('/publications')) return reply({ items: [] });
    throw new Error(`Unexpected GET ${path}`);
  }
  const body = JSON.parse(request.body); writes.push({ path, body });
  assert.equal(path, '/api/product-editor/save', 'This suite never issues stock commands or shop publication');
  assert.equal(body.confirmed, true); assert.equal(body.changes.length, 1); assert.match(body.request_id, /^[0-9a-f-]{36}$/i);
  if (nextSave === 'uncommitted') { nextSave = ''; throw new Error('Synthetic request lost before arrival'); }
  if (!saves.has(body.request_id)) {
    const change = body.changes[0], record = records.get(change.product_id);
    if (change.expected_revision !== record.revision || change.snapshot_hash !== record.snapshot_hash) {
      saves.set(body.request_id, { request_id: body.request_id, status: 'completed', external_write_enabled: false, results: [{ product_id: record.id, status: 'conflict', row: detail(record.id, body.warehouse_code), errors: [] }] });
    } else {
      const before = detail(record.id, body.warehouse_code);
      if (change.common) { Object.assign(record.common, change.common); Object.assign(record.overrides.common, change.common); if ('image_url' in change.common) record.image_url = change.common.image_url; }
      if (change.variant) { Object.assign(record.variant, change.variant); if (change.variant.eans) record.eans = change.variant.eans; }
      if (change.warehouse) record.overrides.warehouses = { ...record.overrides.warehouses, [body.warehouse_code]: { ...record.overrides.warehouses?.[body.warehouse_code], ...change.warehouse } };
      if (change.shops) for (const [code, values] of Object.entries(change.shops)) {
        const shop = record.shops.find(item => item.shop_code === code); Object.assign(shop.overrides, values);
        for (const key of ['shop_url', 'shop_admin_url']) if (key in values) shop[key] = values[key] || `https://${code}.example.test/${key === 'shop_url' ? 'source' : 'admin/source'}`;
      }
      record.revision++; record.snapshot_hash = String(record.revision).padStart(64, '0');
      const after = detail(record.id, body.warehouse_code); record.audit.unshift({ id: record.revision, created_at: '2026-11-09T10:00:00Z', before, after });
      saves.set(body.request_id, { request_id: body.request_id, status: 'completed', external_write_enabled: false, results: [{ product_id: record.id, status: 'saved', row: detail(record.id, body.warehouse_code), errors: [] }] });
    }
  }
  if (nextSave === 'lost') { nextSave = ''; throw new Error('Synthetic response lost after commit'); }
  return reply(saves.get(body.request_id));
};
let currentProps;
async function render(props = {}) {
  currentProps = { productId: 1, warehouseCode: 'main', onChanged: row => changedRows.push(clone(row)), onClose: () => { closeCount++; root.render(null); }, ...props };
  await act(async () => { root.render(React.createElement(StockCard, currentProps)); await tick(); });
}
async function remount(props = {}) { await act(async () => { root.render(null); await tick(); }); await render(props); }
async function editName(value) { await click('stock-card-edit-product'); await input('stock-card-input-name', value); }

(async () => {
  await i18n.changeLanguage('sk');
  assert.equal(helpers.stockCardSafeUrl('javascript:alert(1)'), null);
  assert.equal(helpers.stockCardSafeUrl('https://user:pass@example.test/'), null);
  assert.equal(helpers.stockCardSafeUrl('https://example.test/with space'), null);
  assert.equal(helpers.stockCardSafeUrl('https://example.test\\@evil.test/'), null);
  assert.equal(helpers.stockCardDescription('<p>Part &amp; price</p><script>alert(1)</script>'), 'Part & price');
  document.getElementById('launcher').focus();
  await render({ productId: 999, warehouseCode: undefined }); assert.equal(reads.length, 0, 'Locked card performs no protected reads');
  await act(async () => { setHubUser(actor(42)); await tick(); });
  assert.equal(required('stock-card-warehouse').value, 'main', 'Configured default wins over first option');
  assert.equal(required('stock-card-sku').textContent, 'PART-1', 'A retired ID resolves to canonical data');
  assert(!document.getElementById('root').contains(required('stock-card')), 'Card is portalled out of transformed/table parents');
  assert.equal(document.querySelectorAll('[role="dialog"]').length, 1);
  assert(document.getElementById('root').hasAttribute('inert'), 'Background is inert while the card is open');
  assert.deepEqual([...required('stock-card-product-data').querySelectorAll('dt')].map(node => node.textContent), ['Dodávateľ', 'Značka', 'EAN', 'Dodávateľský kód', 'Umiestnenie v sklade', 'Typ / skladová jednotka']);
  assert.equal(required('stock-card-supplier-link').href, 'https://supplier.example.test/exact-product');
  assert.equal(required('stock-card-supplier-link').rel, 'noopener noreferrer');
  assert(document.body.textContent.includes('Tire & tube')); assert(!document.body.textContent.includes('window.bad'));
  const cardNode = required('stock-card'), headerNode = required('stock-card-summary');
  await click('stock-card-tab-shops');
  assert.equal(required('stock-card-parent-biketrek').textContent, 'biketrek-PARENT');
  assert.equal(required('stock-card-parent-xtrek').textContent, 'xtrek-PARENT');
  assert(required('stock-card-native-biketrek').textContent.includes('40') && required('stock-card-native-biketrek').textContent.includes('400'));
  assert(!required('stock-card-parent-biketrek').textContent.includes('HUB-GROUP'), 'Native parent is never inferred from the Hub group');
  await click('stock-card-tab-stock'); await click('stock-card-tab-prices'); await click('stock-card-tab-history');
  assert.equal(required('stock-card-all-movements').getAttribute('href'), '/stock/movements?sku=PART-1');
  assert.equal(required('stock-card-product-trace').getAttribute('href'), '/product-trace?product_id=1');
  for (const id of ['stock-card-all-movements', 'stock-card-product-trace']) { assert.equal(required(id).target, '_blank'); assert.equal(required(id).rel, 'noopener noreferrer'); }
  await click('stock-card-tab-overview');
  assert.strictEqual(required('stock-card'), cardNode); assert.strictEqual(required('stock-card-summary'), headerNode, 'Tab changes keep the same shell and stock header');
  assert.equal(writes.length, 0, 'Opening sections never mutates stock or publishes to shops');
  required('stock-card-close').focus(); await keyboard('Tab', true);
  assert(document.activeElement.closest('.stock-card__footer'), 'Focus trap skips controls in closed advanced details');
  await keyboard('Tab'); assert.strictEqual(document.activeElement, required('stock-card-close'));

  await editName('Draft only'); await click('stock-card-tab-prices');
  assert(required('stock-card-dirty-guard')); assert(visible(required('stock-card-editor')));
  await click('stock-card-keep-editing'); assert.equal(required('stock-card-input-name').value, 'Draft only');
  await keyboard('Escape'); await click('stock-card-discard');
  assert.equal(find('stock-card'), null); assert.strictEqual(document.activeElement, document.getElementById('launcher'));
  assert(!document.getElementById('root').hasAttribute('inert'));

  await remount(); await editName('Saved name');
  await input('stock-card-input-description', 'A useful description'); await input('stock-card-input-eans', '1111111111111\n2222222222222');
  const beforeSave = writes.length;
  await act(async () => { required('stock-card-save').click(); required('stock-card-save').click(); await tick(); });
  assert.equal(writes.length, beforeSave + 1, 'Double clicks produce one immutable save UUID');
  const savedBody = writes.at(-1).body; assert.equal(savedBody.warehouse_code, 'main'); assert.equal(savedBody.changes[0].product_id, 1);
  assert.deepEqual(savedBody.changes[0].common, { name: 'Saved name', description: 'A useful description' });
  assert.deepEqual(savedBody.changes[0].variant.eans, ['1111111111111', '2222222222222']); assert(!savedBody.changes[0].stock);
  assert(changedRows.length && changedRows.every(row => row.id === 1), 'Only the affected canonical row is returned to the caller');
  assert.equal(find('stock-card-editor'), null); assert(required('stock-card-close').disabled === false);

  await editName('Storage-protected draft');
  const nativeSetItem = dom.window.Storage.prototype.setItem;
  dom.window.Storage.prototype.setItem = function(key, value) { if (!key.startsWith('stock-card-save:')) nativeSetItem.call(this, key, value); };
  const blockedCount = writes.length; await click('stock-card-save');
  assert.equal(writes.length, blockedCount, 'Silently blocked storage prevents an unrecoverable POST');
  dom.window.Storage.prototype.setItem = nativeSetItem;
  nextSave = 'lost'; await click('stock-card-save');
  const lostBody = clone(writes.at(-1).body), pendingKey = helpers.stockCardSaveKey(1, 'main');
  assert(required('stock-card-save-recovery')); assert.equal(JSON.parse(sessionStorage.getItem(pendingKey)).request_id, lostBody.request_id);
  assert(pendingKey.includes(':42:'), 'Pending metadata is scoped to the signed-in actor without credentials');
  await act(async () => { setHubUser(actor(43)); await tick(); });
  assert.equal(find('stock-card-save-recovery'), null, 'Another account never offers the prior actor’s save for retry');
  assert(sessionStorage.getItem(pendingKey), 'Original actor can recover their operation later');
  await act(async () => { setHubUser(actor(42)); await tick(); });
  assert(required('stock-card-save-recovery'));
  const beforeRecovery = writes.length; await click('stock-card-recover');
  assert.equal(writes.length, beforeRecovery); assert.equal(sessionStorage.getItem(pendingKey), null); assert.equal(find('stock-card-save-recovery'), null);

  await editName('Draft after network loss'); nextSave = 'uncommitted'; await click('stock-card-save');
  const missingBody = clone(writes.at(-1).body); await click('stock-card-recover'); await click('stock-card-retry-save');
  assert.deepEqual(writes.at(-1).body, missingBody, 'Retry preserves the exact CAS values and UUID');

  await editName('My concurrent name'); records.get(1).revision++; records.get(1).snapshot_hash = 'f'.repeat(64); records.get(1).common.brand = 'Changed by colleague';
  const latestRevision = records.get(1).revision;
  await click('stock-card-save'); assert(required('stock-card-conflict')); assert(required('stock-card-save').disabled);
  const beforeRebase = writes.length; await click('stock-card-rebase');
  assert.equal(writes.length, beforeRebase, 'Rebasing is review only'); await click('stock-card-save');
  assert.equal(writes.at(-1).body.changes[0].expected_revision, latestRevision);
  assert.equal(records.get(1).common.brand, 'Changed by colleague', 'Untouched concurrent fields are preserved');

  await click('stock-card-edit-product'); await click('stock-card-restore-description'); await click('stock-card-save');
  assert.equal(writes.at(-1).body.changes[0].common.description, null, 'Restoring source description is distinct from an explicit empty description');
  await click('stock-card-tab-shops'); await click('stock-card-edit-links-biketrek');
  await input('stock-card-input-shop_url', 'javascript:alert(1)'); const badLinks = writes.length; await click('stock-card-save'); assert.equal(writes.length, badLinks);
  await input('stock-card-input-shop_url', 'https://biketrek.example.test/new-location'); await click('stock-card-save');
  assert.deepEqual(writes.at(-1).body.changes[0].shops, { biketrek: { shop_url: 'https://biketrek.example.test/new-location' } });
  assert.equal(records.get(1).shops[0].mapping.parent_code, 'biketrek-PARENT', 'Navigation editing leaves exact mapping intact');

  await click('stock-card-tab-stock'); assert.equal(required('adjustment-count').value, '5');
  await click('stock-card-tab-prices'); quantities.main = 7; await click('fifo-refresh');
  assert(required('stock-card-physical').textContent.startsWith('7'));
  await click('stock-card-tab-stock'); assert.equal(required('adjustment-count').value, '7', 'A read refresh also updates an already mounted pristine stock adjustment');
  await input('adjustment-count', '9'); await click('stock-card-tab-overview'); assert(required('stock-card-dirty-guard')); await click('stock-card-discard');

  await editName('Warehouse recovery'); nextSave = 'lost'; await click('stock-card-save');
  holdRead = { id: 1, warehouse: 'other' }; await input('stock-card-warehouse', 'other'); await click('stock-card-discard');
  assert.equal(find('stock-card-save-recovery'), null, 'Old warehouse recovery controls disappear before the new read completes');
  await act(async () => { releaseRead(); await tick(); }); assert.equal(required('stock-card-warehouse').value, 'other'); assert(required('stock-card-physical').textContent.startsWith('2'));
  await input('stock-card-warehouse', 'main'); assert(required('stock-card-save-recovery')); await click('stock-card-recover');

  await remount({ warehouseCode: null }); assert.equal(required('stock-card-warehouse').value, '', 'Explicit no-warehouse is not replaced by the default');
  await click('stock-card-tab-prices'); assert.equal(find('fifo-panel'), null); assert.equal(required('stock-card-physical').textContent, i18n.t('stockCard.unknown'));
  await remount({ productId: undefined, sku: 'PART-1', warehouseCode: 'other' });
  assert(reads.some(read => read.path.endsWith('/resolve') && read.query.includes('sku=PART-1') && read.query.includes('warehouse_code=other')));
  assert.equal(required('stock-card-sku').textContent, 'PART-1');

  await remount({ productId: 3, warehouseCode: 'main' });
  assert.equal(required('stock-card-physical').textContent, i18n.t('stockCard.unknown'), 'Unconfirmed stock never renders as a fabricated zero');
  records.get(3).suppliers[0].is_active = false;
  await remount({ productId: 3, warehouseCode: 'main' });
  const supplierWrites = writes.length;
  assert(required('stock-card-overview-supplier').textContent.includes('Primary supplier'));
  assert(required('stock-card-overview-supplier').textContent.includes(i18n.t('stockCard.inactiveSupplierSource')));
  await click('stock-card-edit-product');
  assert(required('stock-card-editor-supplier').textContent.includes('Primary supplier'), 'Editor retains the same stored inactive supplier as the overview');
  assert(required('stock-card-editor-supplier').textContent.includes(i18n.t('stockCard.inactiveSupplierSource')));
  assert(!required('stock-card-editor-supplier').textContent.includes(i18n.t('stockCard.notLinked')));
  await click('stock-card-cancel-edit');
  assert.equal(writes.length, supplierWrites, 'Displaying the inactive source does not reactivate or replace its mapping');
  records.get(3).suppliers[0].product_url = 'javascript:alert(1)'; records.get(3).image_url = 'data:text/html,<script>alert(1)</script>';
  records.get(3).shops[1].mapped = false;
  await remount({ productId: 3, warehouseCode: 'main' }); assert.equal(find('stock-card-supplier-link'), null); assert(!required('stock-card-image').querySelector('img'));
  await click('stock-card-tab-shops'); assert.equal(required('stock-card-prepare-listing-xtrek').getAttribute('href'), '/product-import');
  assert.equal(required('stock-card-prepare-listing-xtrek').target, '_blank'); assert.equal(required('stock-card-prepare-listing-xtrek').rel, 'noopener noreferrer');

  holdRead = { id: 2, warehouse: 'main' }; await render({ productId: 2 });
  await render({ productId: 1 }); await act(async () => { releaseRead(); await tick(); });
  assert.equal(required('stock-card-sku').textContent, 'PART-1', 'Late reads cannot replace another product context');

  await remount({ readOnlyReason: 'Save the table draft first.' }); const writesBeforeReadOnly = writes.length;
  assert.equal(find('stock-card-edit-product'), null); await click('stock-card-tab-stock'); assert.equal(find('stock-adjustment-panel'), null);
  await click('stock-card-tab-prices'); assert.equal(find('stock-card-edit-price'), null); assert.equal(find('fifo-receive'), null);
  await click('stock-card-tab-shops'); assert.equal(find('stock-card-mappings'), null); assert.equal(find('stock-card-edit-links-biketrek'), null);
  assert.equal(writes.length, writesBeforeReadOnly, 'Read-only table context exposes no mutation path');
  await act(async () => { root.unmount(); await tick(); });
  console.log('Stock card UI: canonical ID/SKU, supplier order, fixed shell, native mappings, focus/dirty guards, scoped save/recovery/rebase, warehouse refresh, and read-only safety passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
