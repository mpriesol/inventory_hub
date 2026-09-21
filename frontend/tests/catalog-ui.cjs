/* Behavioral UI checks, with synthetic API data and no network access. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', { url: 'https://hub.example.test/suppliers/paul-lange/catalog' });
global.window = dom.window;
global.document = dom.window.document;
Object.defineProperty(global, 'navigator', { value: dom.window.navigator, configurable: true });
global.HTMLElement = dom.window.HTMLElement;
global.localStorage = dom.window.localStorage;
global.IS_REACT_ACT_ENVIRONMENT = true;
// Compile the actual components with the existing TypeScript dependency.
for (const ext of ['.ts', '.tsx']) require.extensions[ext] = (module, filename) => {
  const source = fs.readFileSync(filename, 'utf8').replace(/import\.meta\.env/g, '{}');
  module._compile(ts.transpileModule(source, { compilerOptions: { jsx: ts.JsxEmit.React, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, esModuleInterop: true } }).outputText, filename);
};
require.extensions['.css'] = () => {};
require('../src/i18n/index.ts');
const React = require('react');
const { act } = React;
const { createRoot } = require('react-dom/client');
const { MemoryRouter, Routes, Route } = require('react-router-dom');
const { SupplierCatalogPage } = require('../src/pages/SupplierCatalogPage.tsx');

const products = [1, 2, 3].map((id) => ({
  id, supplier: 'paul-lange', feed_key: 'products', run_id: 1, code: `A-${id}`, shop_code: `PL-A-${id}`, name: id === 3 ? 'Rukavice' : `Prilba ${id === 1 ? 'M' : 'L'}`,
  brand: 'TEST', images: [`https://images.example.test/${id}.jpg`], eans: [`0000${id}`], group_code: id < 3 ? 'G1' : null, group_name: 'Prilba',
  variant_relationship: id < 3 ? 'explicit' : 'not_provided', variant_attributes: id < 3 ? [{ name: 'Veľkosť', value: id === 1 ? 'M' : 'L' }] : [],
  prices: { currency: 'EUR', retail_gross: '123', retail_net: '100', purchase_gross: '73.80', purchase_net: '60', vat_percent: '23', vat_source: 'configured' },
  warnings: [], listed: false, supplier_stock_raw: '6+', availability: 'skladom', parameters: [],
}));
products[0].images = ['http://xml.paul-lange-oslany.sk:8081/ito5-S123.jpg'];
const displayImages = ['/api/suppliers/paul-lange/catalog/images/ito5-S123.jpg', products[1].images[0]];
const calls = [];
let snapshot = 1;
global.fetch = async (path, init = {}) => {
  const url = new URL(path, 'https://hub.example.test');
  const body = init.body ? JSON.parse(init.body) : undefined;
  calls.push({ path: url.pathname, query: url.searchParams, body });
  let data;
  if (url.pathname.endsWith('/catalog')) data = { name: 'Paul Lange', sources: [{ key: 'products', name: 'Produkty', supported: true, configured: true }], shops: [{ code: 'biketrek', name: 'BikeTrek', ready: true }, { code: 'xtrek', name: 'xTrek', ready: true }, { code: 'pending', name: 'Pending', ready: false }], defaults: { category_code: 'K-TEST' }, status: 'completed' };
  else if (url.pathname.endsWith('/catalog/products')) {
    const rows = url.searchParams.get('page') === '2' ? [{ key: 'item:3', product: products[2], is_group: false, variants_count: 0, matching_ids: [3], variants: [] }] : [{ key: 'group:G1', product: products[0], is_group: true, variants_count: 2, matching_ids: [1, 2], variants: products.slice(0, 2) }];
    data = { run_id: snapshot, fetched_at: '2026-01-01T12:00:00Z', total: 2, total_items: 3, pages: 2, manufacturers: ['TEST'], items: rows };
  } else if (url.pathname.endsWith('/catalog/products/1')) data = { product: products[0], variants: [], source_fields: {}, source_xml: '<SHOPITEM/>', description_html: '' };
  else if (url.pathname.endsWith('/selection')) data = { ids: [1, 2, 3], run_id: snapshot };
  else if (url.pathname.endsWith('/import/options')) data = { prices_with_vat: true, languages: [{ code: 'sk', currency: 'EUR', default: true }], pricelists: [{ name: 'Predvolené', default: true }], categories: [{ code: 'K-TEST', names: { sk: 'Test' } }], create_validation_field: false };
  else if (url.pathname.endsWith('/import/preview')) data = { preview_id: 'a'.repeat(32), shop: 'biketrek', supplier: 'paul-lange', options: body.options, errors: [], warnings: [], expires_at: new Date(Date.now() + 3600000).toISOString(), items: [{ code: 'PL-G-G1', name: 'Prilba', product_ids: body.product_ids, variants_count: body.product_ids.length, status: 'ready', errors: [], warnings: [], payload: { images: [{ url: products[0].images[0] }], variants: [{ code: 'PL-A-1', image: { url: products[0].images[0] } }] } }] };
  else throw new Error(`Unexpected request: ${url.pathname}`);
  return { ok: true, status: 200, json: async () => data };
};
const root = createRoot(document.getElementById('root'));
const button = text => [...document.querySelectorAll('button')].find(b => b.textContent.trim() === text);
const checkbox = label => [...document.querySelectorAll('input[type="checkbox"]')].find(b => b.getAttribute('aria-label') === label);
async function click(element) { assert.ok(element, 'Expected UI element'); await act(async () => element.dispatchEvent(new window.MouseEvent('click', { bubbles: true }))); }
async function change(element, value) { await act(async () => { Object.getOwnPropertyDescriptor(element.tagName === 'SELECT' ? window.HTMLSelectElement.prototype : window.HTMLInputElement.prototype, 'value').set.call(element, value); element.dispatchEvent(new window.Event(element.tagName === 'SELECT' ? 'change' : 'input', { bubbles: true })); }); }
async function settle() { await act(async () => { await new Promise(resolve => setTimeout(resolve, 400)); }); }

(async () => {
  await act(async () => root.render(React.createElement(MemoryRouter, { initialEntries: ['/suppliers/paul-lange/catalog'] }, React.createElement(Routes, null, React.createElement(Route, { path: '/suppliers/:supplier/catalog', element: React.createElement(SupplierCatalogPage) })))));
  await settle();
  assert.equal(document.querySelectorAll('tbody tr').length, 1, 'Variants start collapsed');
  await click(document.querySelector('button[aria-expanded="false"][aria-label]'));
  assert.equal(document.querySelectorAll('tbody tr').length, 3, 'Expanding a parent shows both variants');
  assert.deepEqual([...document.querySelectorAll('.catalog-variant img')].map(i => i.getAttribute('src')), displayImages);
  await click(button('Prilba M'));
  assert.equal(document.querySelector('.catalog-detail-head img').getAttribute('src'), displayImages[0], 'Detail uses the HTTPS origin for HTTP supplier photos');
  assert.equal(document.querySelector('.catalog-gallery img').getAttribute('src'), displayImages[0]);
  await click(document.querySelector('button[aria-label="Close modal"]'));
  await click(checkbox('Vybrať produkt: Prilba M'));
  assert.equal(checkbox('Vybrať varianty: Prilba').indeterminate, true, 'Parent reflects partial selection');
  await click(button('Ďalej')); await settle();
  await click(checkbox('Vybrať produkt: Rukavice'));
  assert.ok(document.body.textContent.includes('Vybrané položky: 2'), 'Selection persists across pages');
  await click(button('Späť')); await settle();
  assert.equal(checkbox('Vybrať varianty: Prilba').indeterminate, true);
  await click(button('Vybrať všetky nájdené'));
  assert.ok(document.body.textContent.includes('Vybrané položky: 3'));
  await click(button('Zrušiť výber'));
  await click(checkbox('Vybrať varianty: Prilba'));
  assert.ok(document.body.textContent.includes('Vybrané položky: 2'));
  await change(document.getElementById('catalog-shop'), 'xtrek'); await settle();
  await change(document.getElementById('catalog-shop'), 'biketrek'); await settle();
  assert.equal(localStorage.getItem('catalog.targetShop'), 'biketrek');
  assert.equal(document.querySelector('#catalog-shop option[value="pending"]').disabled, true);
  await click(button('Pripraviť import'));
  const request = calls.find(c => c.path.endsWith('/import/preview'));
  assert.deepEqual(request.body.product_ids, [1, 2], 'Preview receives only the explicit selection');
  assert.equal(request.body.run_id, 1);
  assert.equal(request.path, '/api/shops/biketrek/import/preview');
  assert.equal(calls.some(c => c.path.endsWith('/import')), false, 'Preview does not start an import');
  assert.ok(button('Importovať do e-shopu (1)'), 'Confirmation is a separate action');
  assert.equal(document.querySelector('.catalog-import-item summary img').getAttribute('src'), displayImages[0]);
  assert.equal(document.querySelector('.catalog-preview-variant img').getAttribute('src'), displayImages[0]);
  assert.equal(products[0].images[0], 'http://xml.paul-lange-oslany.sk:8081/ito5-S123.jpg', 'Display leaves the original source URL intact');
  await click(button('Zavrieť'));
  snapshot = 2;
  await click(button('Ďalej')); await settle();
  assert.ok(document.body.textContent.includes('Vybrané položky: 0'), 'Changed feed invalidates the selection');
  await act(async () => root.unmount());
  console.log('Catalog UI passed: expandable variants and images, partial selection, pagination, all-results selection, target shop, explicit preview, snapshot invalidation.');
})().catch(error => { console.error(error); process.exitCode = 1; root.unmount(); });
