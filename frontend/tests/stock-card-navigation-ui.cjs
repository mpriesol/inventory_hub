/* Real shared card/router integration with synthetic Hub APIs only. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'https://hub.example.test/invoices/1' });
global.window = dom.window; global.document = dom.window.document;
Object.defineProperty(global, 'navigator', { value: dom.window.navigator, configurable: true });
global.HTMLElement = dom.window.HTMLElement;
global.localStorage = dom.window.localStorage; global.sessionStorage = dom.window.sessionStorage;
global.IS_REACT_ACT_ENVIRONMENT = true;
let desktopChanged;
const desktop = { matches: false, addEventListener: (_, fn) => { desktopChanged = fn; }, removeEventListener: () => {} };
window.matchMedia = () => desktop;
for (const ext of ['.ts', '.tsx']) require.extensions[ext] = (module, filename) => module._compile(ts.transpileModule(
  fs.readFileSync(filename, 'utf8').replace(/import\.meta\.env/g, '{}'),
  { compilerOptions: { jsx: ts.JsxEmit.React, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, esModuleInterop: true } }).outputText, filename);
require.extensions['.css'] = () => {};
const React = require('react'); const { act } = React;
const { createRoot } = require('react-dom/client');
const { createMemoryRouter, RouterProvider } = require('react-router-dom');
const i18n = require('../src/i18n/index.ts').default;
const { unlockHub } = require('../src/api/access.ts');
const { StockCardLink } = require('../src/components/product/StockCardLink.tsx');
const { ProductDetailPage } = require('../src/pages/ProductDetailPage.tsx');
const { Layout } = require('../src/components/layout/Layout.tsx');
const clone = value => JSON.parse(JSON.stringify(value));
const tick = () => new Promise(resolve => setTimeout(resolve, 12));
const find = id => document.querySelector(`[data-testid="${id}"]`);
const required = id => { const node = typeof id === 'string' ? find(id) : id; assert(node, `${id} exists`); return node; };
const click = async id => act(async () => { required(id).click(); await tick(); });
const type = async (id, value) => act(async () => {
  const node = required(id);
  Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, 'value').set.call(node, value);
  node.dispatchEvent(new dom.window.Event('input', { bubbles: true })); await tick();
});
const reply = value => ({ ok: true, status: 200, headers: { get: () => 'application/json' }, json: async () => clone(value) });
let row = { id: 18, sku: 'HUB/18? červená', is_active: true, item_type: 'stock', stock_unit: 'pcs', item_definition_locked: false,
  group: null, attributes: [], eans: ['8580000000010'], supplier_codes: [], supplier_linked: false, suppliers: [], image_url: null,
  common: { name: 'Synthetic stock card', brand: null, description: 'Fixture only', internal_note: '' },
  variant: { sale_price_gross: '20.00', vat_rate: '23.00', note: null },
  stock: { known: true, qty_on_hand: '4', qty_reserved: '1', qty_available: '3', qty_quarantined: '0', avg_cost: null, total_value: null },
  shops: [], overrides: { common: {}, variant: {}, warehouses: {}, shops: {} }, revision: 1, snapshot_hash: 'a'.repeat(64), audit: [] };
const calls = [];
const options = { warehouses: [{ id: 1, code: 'main', name: 'Main', is_default: true }, { id: 2, code: 'secondary', name: 'Secondary' }], shops: [], brands: [], page_sizes: [50], currency: 'EUR', price_basis: 'incl_vat' };
global.fetch = async (path, init = {}) => {
  const url = new URL(path, window.location.origin), body = init.body && JSON.parse(init.body);
  calls.push({ path: url.pathname, query: url.searchParams, body });
  assert(url.pathname.startsWith('/api/product-editor/'), 'All entry points use the protected editor API');
  if (url.pathname.endsWith('/options')) return reply(options);
  if (url.pathname === '/api/product-editor/save') {
    assert.equal(body.changes[0].product_id, 18);
    assert.equal(body.changes[0].expected_revision, row.revision);
    row = { ...row, common: { ...row.common, ...body.changes[0].common }, revision: row.revision + 1 };
    return reply({ request_id: body.request_id, status: 'completed', external_write_enabled: false, results: [{ product_id: 18, status: 'saved', row, errors: [] }] });
  }
  assert(['/api/product-editor/products/18', '/api/product-editor/products/resolve'].includes(url.pathname));
  const warehouse = url.searchParams.get('warehouse_code');
  return reply({ ...row, warehouse: warehouse ? { code: warehouse, location: 'A1', min_quantity: '0' } : null,
    warehouse_context: warehouse ? { id: warehouse === 'main' ? 1 : 2, code: warehouse, name: warehouse } : null });
};
let refreshed = 0;
function Parent() {
  const [note, setNote] = React.useState('');
  return React.createElement('div', null,
    React.createElement('input', { 'data-testid': 'invoice-draft', value: note, onChange: event => setNote(event.target.value) }),
    React.createElement(StockCardLink, { productId: 18, sku: row.sku, warehouseCode: 'secondary', 'data-testid': 'invoice-card', onChanged: () => { refreshed++; } }, 'Product'),
    React.createElement(StockCardLink, { sku: row.sku, 'data-testid': 'legacy-card' }, 'SKU link'));
}
const root = createRoot(document.getElementById('root'));
let router;
async function mount(path, layout = false) {
  await act(async () => { root.render(null); await tick(); });
  const routes = [
    { path: '/invoices/1', element: React.createElement(Parent) },
    { path: '/products/card/:productId', element: React.createElement(ProductDetailPage) },
    { path: '/products/:sku', element: React.createElement(ProductDetailPage) },
    { path: '/stock', element: React.createElement('h1', { 'data-testid': 'stock-page' }, 'Stock') },
  ];
  router = createMemoryRouter(layout ? [{ element: React.createElement(Layout), children: routes }] : routes, { initialEntries: [path] });
  await act(async () => { root.render(React.createElement(RouterProvider, { router })); await tick(); });
}
(async () => {
  await i18n.changeLanguage('sk'); unlockHub('synthetic-card-navigation');
  await mount('/invoices/1');
  await type('invoice-draft', 'Keep this unfinished invoice note');
  const anchor = required('invoice-card'); anchor.focus();
  assert.equal(anchor.getAttribute('href'), '/products/card/18?warehouse=secondary');
  const modified = new dom.window.MouseEvent('click', { bubbles: true, cancelable: true, ctrlKey: true });
  let handledByCard;
  // Observe React's decision before preventing jsdom's unsupported native navigation.
  document.addEventListener('click', event => { handledByCard = event.defaultPrevented; event.preventDefault(); }, { once: true });
  await act(async () => { anchor.dispatchEvent(modified); await tick(); });
  assert(!handledByCard && !find('stock-card'), 'A modified click remains a normal new-tab link');
  assert.equal(calls.length, 0, 'A dormant card link reads nothing');
  await click(anchor);
  assert(find('stock-card') && router.state.location.pathname === '/invoices/1', 'Invoice context stays mounted under the shared card');
  assert.equal(required('invoice-draft').value, 'Keep this unfinished invoice note');
  assert.equal(required('stock-card').closest('#root'), null, 'The card portal is not clipped by the parent layout');
  assert.equal(calls.find(call => call.path.endsWith('/products/18')).query.get('warehouse_code'), 'secondary');
  assert.equal(document.getElementById('root').hasAttribute('inert'), true);
  await click('stock-card-edit-product'); await type('stock-card-input-name', 'Updated fixture'); await click('stock-card-save');
  assert.equal(refreshed, 0, 'Parent refresh is deferred while its card is open');
  assert(find('stock-card') && required('invoice-draft').value.includes('unfinished'));
  await click('stock-card-close');
  assert.equal(refreshed, 1); assert(!find('stock-card'));
  assert.equal(document.activeElement, anchor, 'Focus returns to the original product link');
  assert(!document.getElementById('root').hasAttribute('inert'));
  assert.equal(required('invoice-draft').value, 'Keep this unfinished invoice note');
  assert.equal(required('legacy-card').getAttribute('href'), `/products/${encodeURIComponent(row.sku)}`);

  await mount(`/products/${encodeURIComponent(row.sku)}`);
  assert(find('stock-card'), 'Legacy direct SKU links use the same dialog');
  const resolved = calls.filter(call => call.path.endsWith('/resolve')).at(-1);
  assert.equal(resolved.query.get('sku'), row.sku, 'The query resolver preserves punctuation and Unicode in a SKU');
  assert.equal(resolved.query.get('warehouse_code'), 'main', 'An unspecified warehouse uses the configured default');
  await click('stock-card-close'); assert(find('stock-page'));
  await mount('/products/card/18?warehouse=');
  assert.equal(required('stock-card-warehouse').value, '', 'An explicit all-warehouse context is not overwritten by the default');
  assert.equal(calls.filter(call => call.path.endsWith('/products/18')).at(-1).query.get('warehouse_code'), null);
  await click('stock-card-close');

  await mount('/invoices/1', true);
  const menu = document.querySelector('.hub-mobile-header button');
  await click(menu);
  assert.equal(menu.getAttribute('aria-expanded'), 'true');
  assert.equal(document.querySelector('.hub-sidebar').getAttribute('aria-modal'), 'true');
  assert.equal(document.body.style.overflow, 'hidden');
  assert(document.querySelector('.hub-main').hasAttribute('inert'), 'Mobile drawer blocks background interactions');
  await act(async () => {
    required('invoice-draft').focus();
    document.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true })); await tick();
  });
  assert(document.querySelector('.hub-sidebar').contains(document.activeElement), 'Tab recaptures stray focus inside navigation');
  await act(async () => { document.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true })); await tick(); });
  assert.equal(menu.getAttribute('aria-expanded'), 'false'); assert.equal(document.activeElement, menu);
  assert.equal(document.body.style.overflow, '');
  assert(!document.querySelector('.hub-main').hasAttribute('inert'));
  await click(menu); desktop.matches = true;
  await act(async () => { desktopChanged(); await tick(); });
  assert.equal(menu.getAttribute('aria-expanded'), 'false', 'Resizing to desktop releases the mobile navigation lock');
  desktop.matches = false; await click(menu);
  await click(document.querySelector('.hub-sidebar a[href="/stock"]'));
  assert.equal(router.state.location.pathname, '/stock'); assert.equal(menu.getAttribute('aria-expanded'), 'false');
  await act(async () => root.unmount());
  console.log('Stock card navigation passed: shared reads, stable direct links, parent draft preservation, context, focus and mobile menu.');
})().catch(error => { console.error(error); process.exitCode = 1; });
