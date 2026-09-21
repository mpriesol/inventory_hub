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
products[1].warnings = ['inherited_retail_price', 'retail_below_purchase'];
products[2].warnings = ['duplicate_supplier_code'];
products[2].import_blockers = ['duplicate_supplier_code'];
Object.assign(products[0], { listed: true, shop_url: 'https://shop.example.test/p/prilba', shop_admin_url: 'https://admin.example.test/product/101', shop_active: false });
const displayImages = ['/api/suppliers/paul-lange/catalog/images/ito5-S123.jpg', products[1].images[0]];
const calls = [];
let snapshot = 1;
let previewNumber = 0;
let failPreview = false;
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
  else if (url.pathname.endsWith('/catalog/download')) data = { relpath: 'suppliers/paul-lange/feeds/xml/catalog_products_test.xml', size_bytes: 1024, downloaded_at: '2026-01-01T12:00:00Z' };
  else if (url.pathname.endsWith('/import/options')) data = { cache: { checked_at: '2026-01-01T12:00:00Z', expires_at: '2026-01-01T12:15:00Z', from_cache: url.searchParams.get('refresh') !== 'true', max_age_seconds: 900 }, prices_with_vat: true, languages: [{ code: 'sk', currency: 'EUR', default: true }], pricelists: [{ name: 'Predvolené', default: true }], categories: [{ code: 'K-TEST', names: { sk: 'Test' } }], create_validation_field: false };
  else if (url.pathname.endsWith('/import/preview')) {
    if (failPreview) return { ok: false, status: 502, json: async () => ({ detail: { code: 'upgates_unavailable' } }) };
    data = { preview_id: (++previewNumber).toString().padStart(32, '0'), shop: 'biketrek', supplier: 'paul-lange', options: body.options, errors: [], warnings: [], expires_at: new Date(Date.now() + 3600000).toISOString(), items: [{ code: 'PL-G-G1', name: 'Prilba', product_ids: body.product_ids, variants_count: body.product_ids.length, status: 'ready', errors: [], warnings: ['retail_below_purchase'], payload: { images: [{ url: products[0].images[0] }], variants: [{ code: 'PL-A-1', image: { url: products[0].images[0] } }] } }],
      sale_price_overrides: body.sale_price_overrides,
      price_lines: body.product_ids.map(id => ({ product_id: id, code: products[id - 1].shop_code, name: products[id - 1].name, image: products[id - 1].images[0], attributes: products[id - 1].variant_attributes, retail_gross: '123', purchase_net: '60', sale_gross: body.sale_price_overrides[id] || '123', overridden: id in body.sale_price_overrides, blocked: false, warnings: products[id - 1].warnings })) };
  }
  else throw new Error(`Unexpected request: ${url.pathname}`);
  if (url.pathname.endsWith('/import/preview')) data.shop_check = { checked_at: '2026-01-01T12:01:00Z', full_checked_at: '2026-01-01T12:00:00Z', mode: body.refresh_shop ? 'full' : 'changes' };
  return { ok: true, status: 200, json: async () => data };
};
const root = createRoot(document.getElementById('root'));
const button = text => [...document.querySelectorAll('button')].find(b => b.textContent.trim() === text);
const checkbox = label => [...document.querySelectorAll('input[type="checkbox"]')].find(b => b.getAttribute('aria-label') === label);
const saleInput = code => [...document.querySelectorAll('.catalog-edit-prices label')].find(label => label.textContent === `Predajná cena s DPH: ${code}`).querySelector('input');
async function click(element) { assert.ok(element, 'Expected UI element'); await act(async () => element.dispatchEvent(new window.MouseEvent('click', { bubbles: true }))); }
async function change(element, value) { await act(async () => { Object.getOwnPropertyDescriptor(element.tagName === 'SELECT' ? window.HTMLSelectElement.prototype : window.HTMLInputElement.prototype, 'value').set.call(element, value); element.dispatchEvent(new window.Event(element.tagName === 'SELECT' ? 'change' : 'input', { bubbles: true })); }); }
async function settle() { await act(async () => { await new Promise(resolve => setTimeout(resolve, 400)); }); }

(async () => {
  await act(async () => root.render(React.createElement(MemoryRouter, { initialEntries: ['/suppliers/paul-lange/catalog'] }, React.createElement(Routes, null, React.createElement(Route, { path: '/suppliers/:supplier/catalog', element: React.createElement(SupplierCatalogPage) })))));
  await settle();
  assert.equal(document.querySelectorAll('tbody tr').length, 1, 'Variants start collapsed');
  assert.ok(document.body.textContent.includes('Cena z hlavného produktu'), 'Collapsed groups show warnings from their variants');
  assert.ok(document.body.textContent.includes('MOC pod nákupnou cenou'));
  await click(button('Získať pôvodný feed'));
  assert.ok(document.body.textContent.includes('Produkty sa týmto nespracovali.'));
  assert.equal(document.querySelector('a[href^="/api/files/download"]').getAttribute('href'), '/api/files/download?relpath=suppliers%2Fpaul-lange%2Ffeeds%2Fxml%2Fcatalog_products_test.xml');
  assert.equal(calls.some(c => c.path.endsWith('/refresh') || c.path.endsWith('/import')), false, 'Raw download does not index or import products');
  await click(document.querySelector('button[aria-expanded="false"][aria-label]'));
  assert.equal(document.querySelectorAll('tbody tr').length, 3, 'Expanding a parent shows both variants');
  assert.deepEqual([...document.querySelectorAll('.catalog-variant img')].map(i => i.getAttribute('src')), displayImages);
  const shopLink = document.querySelector('.catalog-variant a.catalog-badge');
  assert.equal(shopLink.getAttribute('href'), products[0].shop_url);
  assert.equal(shopLink.getAttribute('target'), '_blank');
  shopLink.addEventListener('click', e => e.preventDefault(), { once: true });
  await click(shopLink);
  assert.equal(document.querySelector('[role="dialog"]'), null, 'Shop link must not open the row detail');
  assert.ok(document.body.textContent.includes('Produkt je v e-shope skrytý'));
  await click(button('Prilba M'));
  assert.equal(document.querySelector('.catalog-detail-head img').getAttribute('src'), displayImages[0], 'Detail uses the HTTPS origin for HTTP supplier photos');
  assert.equal(document.querySelector('.catalog-gallery img').getAttribute('src'), displayImages[0]);
  assert.equal(document.querySelector('.catalog-detail a[href="https://shop.example.test/p/prilba"]').target, '_blank');
  assert.ok(document.querySelector('.catalog-detail a[href="https://admin.example.test/product/101"]'));
  await click(document.querySelector('button[aria-label="Close modal"]'));
  await click(checkbox('Vybrať produkt: Prilba M'));
  assert.equal(checkbox('Vybrať varianty: Prilba').indeterminate, true, 'Parent reflects partial selection');
  await click(button('Ďalej')); await settle();
  assert.ok(document.body.textContent.includes('Import blokovaný'));
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
  assert.ok(document.body.textContent.includes('použité uložené údaje'));
  assert.ok(document.body.textContent.includes('najviac 15 minút'));
  await click(button('Obnoviť nastavenia e-shopu')); await settle();
  assert.equal(calls.filter(c => c.path.endsWith('/import/options')).at(-1).query.get('refresh'), 'true');
  assert.ok(document.body.textContent.includes('načítané z Upgates'));
  await click(button('Pripraviť import'));
  const request = calls.find(c => c.path.endsWith('/import/preview'));
  assert.deepEqual(request.body.product_ids, [1, 2], 'Preview receives only the explicit selection');
  assert.equal(request.body.run_id, 1);
  assert.equal(request.body.refresh_shop, false);
  assert.ok(document.body.textContent.includes('Kódy a EAN v e-shope overené:'));
  assert.ok(document.body.textContent.includes('Posledná úplná kontrola:'));
  assert.equal(request.path, '/api/shops/biketrek/import/preview');
  assert.equal(calls.some(c => c.path.endsWith('/import')), false, 'Preview does not start an import');
  assert.ok(button('Importovať do e-shopu (1)'), 'Confirmation is a separate action');
  assert.equal(document.querySelector('.catalog-import-item summary img').getAttribute('src'), displayImages[0]);
  assert.equal(document.querySelector('.catalog-preview-variant img').getAttribute('src'), displayImages[0]);
  assert.equal(products[0].images[0], 'http://xml.paul-lange-oslany.sk:8081/ito5-S123.jpg', 'Display leaves the original source URL intact');
  assert.ok(document.querySelector('.catalog-import > .catalog-notice[role="status"]').textContent.startsWith('MOC je nižšia'), 'MOC warnings are visible before expanding an item');
  await click(button('Upraviť predajné ceny'));
  const beforeTyping = calls.length;
  await change(saleInput('PL-A-1'), '99,95');
  assert.equal(calls.length, beforeTyping, 'Typing does not contact either API');
  assert.equal(button('Importovať do e-shopu (1)').disabled, true, 'Unverified price edits prevent confirmation');
  assert.equal(saleInput('PL-A-2').value, '123', 'Sibling prices stay unchanged');
  failPreview = true;
  await click(button('Prepočítať a overiť náhľad'));
  assert.equal(saleInput('PL-A-1').value, '99,95', 'Failed recheck keeps the price draft');
  assert.equal(button('Importovať do e-shopu (1)').disabled, true);
  assert.ok(document.querySelector('.catalog-import [role="alert"]'));
  failPreview = false;
  await click(button('Prepočítať a overiť náhľad'));
  assert.deepEqual(calls.filter(c => c.path.endsWith('/import/preview')).at(-1).body.sale_price_overrides, { 1: '99.95' });
  assert.equal(button('Importovať do e-shopu (1)').disabled, false);
  await click(button('Upraviť predajné ceny'));
  assert.equal(saleInput('PL-A-1').value, '99.95');
  await change(saleInput('PL-A-1'), '-1');
  assert.equal(button('Prepočítať a overiť náhľad').disabled, true, 'Invalid price cannot be submitted');
  await click(button('Použiť cenu podľa dodávateľa'));
  await click(button('Prepočítať a overiť náhľad'));
  assert.deepEqual(calls.filter(c => c.path.endsWith('/import/preview')).at(-1).body.sale_price_overrides, {});
  assert.equal(calls.some(c => c.path.endsWith('/import')), false, 'Repricing never starts an import');
  await click(button('Zavrieť'));
  await click(button('Možnosti'));
  const fullCheck = [...document.querySelectorAll('label')].find(e => e.textContent.includes('Úplná kontrola e-shopu pri ďalšom náhľade')).querySelector('input');
  await click(fullCheck);
  await click(button('Pripraviť import'));
  assert.equal(calls.filter(c => c.path.endsWith('/import/preview')).at(-1).body.refresh_shop, true);
  await click(button('Upraviť predajné ceny'));
  await change(saleInput('PL-A-2'), '125');
  await click(button('Prepočítať a overiť náhľad'));
  assert.equal(calls.filter(c => c.path.endsWith('/import/preview')).at(-1).body.refresh_shop, false, 'Repricing uses incremental verification after the initial full check');
  await click(button('Zavrieť'));
  snapshot = 2;
  await click(button('Ďalej')); await settle();
  assert.ok(document.body.textContent.includes('Vybrané položky: 0'), 'Changed feed invalidates the selection');
  await act(async () => root.unmount());
  console.log('Catalog UI passed: variants/images, selection, shop freshness, visible price warnings, per-variant price edits/reset, failed recheck protection, snapshot invalidation.');
})().catch(error => { console.error(error); process.exitCode = 1; root.unmount(); });
