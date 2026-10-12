/* Supplier configuration round trips with synthetic API responses only. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'https://hub.example.test/suppliers?edit=fixture' });
global.window = dom.window;
global.document = dom.window.document;
Object.defineProperty(global, 'navigator', { value: dom.window.navigator, configurable: true });
global.HTMLElement = dom.window.HTMLElement;
global.localStorage = dom.window.localStorage;
global.IS_REACT_ACT_ENVIRONMENT = true;
for (const ext of ['.ts', '.tsx']) require.extensions[ext] = (module, filename) => {
  module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8').replace(/import\.meta\.env/g, '{}'), {
    compilerOptions: { jsx: ts.JsxEmit.React, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, esModuleInterop: true },
  }).outputText, filename);
};
require.extensions['.css'] = () => {};
require('../src/i18n/index.ts');
const React = require('react');
const { act } = React;
const { createRoot } = require('react-dom/client');
const { MemoryRouter } = require('react-router-dom');
const { SuppliersPage } = require('../src/pages/SuppliersPage.tsx');

const fixture = {
  name: 'Fixture supplier', is_active: true,
  product_prefix: 'TEST-', product_prefix_locked: true, product_prefix_lock_reason: 'used_for_product_identity',
  feeds: { current_key: 'products', sources: { products: { mode: 'remote', remote: { url: 'https://supplier.example.test/feed.xml', auth: { mode: 'none' } } } } },
  invoices: { layout: 'yearly', months_back_default: 3, download: { strategy: 'manual', web: { notes: 'Keep invoice settings' } } },
  adapter_settings: {
    currency: 'EUR', vat_rate: 23, custom: { retained: true },
    mapping: { invoice_to_canon: { name: 'ITEM_NAME' }, postprocess: { product_code_prefix: 'TEST-', unit_price_source: 'net' }, canon_to_upgates: { retained: true } },
  },
  extra_root_setting: { retained: true },
};
const original = structuredClone(fixture);
let stored = structuredClone(fixture);
let failSave = false;
let lockOnSave = false;
const writes = [];
global.fetch = async (path, init = {}) => {
  const url = new URL(path, 'https://hub.example.test');
  let data;
  let status = 200;
  if (url.pathname === '/api/suppliers') data = [];
  else if (['/api/suppliers/fixture/config', '/api/suppliers/sloger/config'].includes(url.pathname)) {
    if (init.method === 'PUT') {
      const body = JSON.parse(init.body);
      writes.push(body);
      if (failSave) { status = 422; data = { detail: 'Synthetic save failure' }; }
      else {
        // Mimic the server's documented normalization and return authoritative state.
        const availability = body.adapter_settings?.availability || {};
        stored = { ...body, adapter_settings: { ...body.adapter_settings, availability: {
          ...availability,
          orderable: availability.orderable?.trim() || 'do 5 dní',
          unknown: availability.unknown?.trim() || 'overíme',
        } } };
        if (lockOnSave) stored = { ...stored, product_prefix: stored.adapter_settings.mapping.postprocess.product_code_prefix, product_prefix_locked: true, product_prefix_lock_reason: 'used_for_product_identity' };
        data = structuredClone(stored);
      }
    } else data = structuredClone(stored);
  } else if (url.pathname === '/api/suppliers/fixture/history') data = [{ version: 'older', timestamp: '2026-09-01T12:00:00Z', size_bytes: 100 }];
  else if (url.pathname === '/api/suppliers/fixture/restore/older') { status = 409; data = { detail: { code: 'supplier_prefix_locked', message: 'Prefix remains locked on restore' } }; }
  else throw new Error(`Unexpected endpoint: ${url.pathname}`);
  return { ok: status === 200, status, statusText: status === 200 ? 'OK' : 'Unprocessable Entity', headers: { get: () => 'application/json' }, text: async () => JSON.stringify(data) };
};
const root = createRoot(document.getElementById('root'));
const button = text => [...document.querySelectorAll('button')].find(element => element.textContent.trim() === text);
const field = key => document.getElementById(`supplier-availability-${key}`);
const tick = () => new Promise(resolve => setTimeout(resolve, 5));
async function mount(key, supplier = 'fixture') {
  await act(async () => { root.render(React.createElement(MemoryRouter, { key, initialEntries: [`/suppliers?edit=${supplier}`] }, React.createElement(SuppliersPage))); await tick(); });
}
async function click(element) {
  assert(element, 'Expected UI control');
  await act(async () => { element.click(); await tick(); });
}
async function input(element, value) {
  await act(async () => {
    Object.getOwnPropertyDescriptor(element.tagName === 'TEXTAREA' ? dom.window.HTMLTextAreaElement.prototype : dom.window.HTMLInputElement.prototype, 'value').set.call(element, value);
    element.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
    await tick();
  });
}

(async () => {
  await mount('legacy');
  assert.equal(field('orderable').value, 'do 5 dní', 'A legacy supplier without availability uses the default lead time');
  assert.equal(field('unknown').value, 'overíme');
  assert.equal(field('orderable').maxLength, 100);
  assert.equal(button('Uložiť').disabled, true, 'Opening the editor does not manufacture unsaved changes');
  assert.equal(writes.length, 0);
  assert(document.getElementById('supplier-product-prefix').disabled, 'Server-owned prefix lock disables the form');

  await input(field('orderable'), 'do 7 dní');
  await input(field('unknown'), 'overíme u dodávateľa');
  await click(button('JSON'));
  const jsonDraft = JSON.parse(document.querySelector('textarea').value);
  assert.deepEqual(jsonDraft.adapter_settings.availability, { orderable: 'do 7 dní', unknown: 'overíme u dodávateľa' }, 'JSON and form use the same config draft');
  await click(button('Obecné'));
  await click(button('Uložiť'));
  const expected = structuredClone(fixture);
  expected.adapter_settings.availability = { orderable: 'do 7 dní', unknown: 'overíme u dodávateľa' };
  assert.deepEqual(writes[0], expected, 'Saving availability preserves unrelated root, feed, invoice and adapter settings');
  assert.deepEqual(fixture, original, 'Editing does not mutate the original response fixture');
  assert.equal(button('Uložiť').disabled, true);

  await mount('saved');
  assert.equal(field('orderable').value, 'do 7 dní', 'The saved supplier override survives reopening');
  assert.equal(field('unknown').value, 'overíme u dodávateľa');
  await input(field('orderable'), '   ');
  await input(field('unknown'), '');
  await click(button('Uložiť'));
  assert.equal(field('orderable').value, 'do 5 dní', 'The editor displays the normalized API response after saving blank values');
  assert.equal(field('unknown').value, 'overíme');
  assert.equal(button('Uložiť').disabled, true);

  stored.adapter_settings.availability.retained_extension = { mode: 'fixture' };
  await mount('nested-extension');
  await input(field('orderable'), 'do 8 dní');
  failSave = true;
  await click(button('Uložiť'));
  assert.equal(field('orderable').value, 'do 8 dní', 'Failed saves keep the draft');
  assert.equal(button('Uložiť').disabled, false, 'Failed saves remain retryable');
  assert(document.body.textContent.includes('Synthetic save failure'));
  assert.equal(stored.adapter_settings.availability.orderable, 'do 5 dní');
  failSave = false;
  await click(button('Uložiť'));
  assert.deepEqual(writes.at(-1).adapter_settings.availability.retained_extension, { mode: 'fixture' }, 'Unknown nested availability settings survive edits');
  assert.equal(stored.adapter_settings.availability.orderable, 'do 8 dní');
  assert.equal(stored.adapter_settings.availability.unknown, 'overíme');

  await click(button('JSON'));
  let draft = JSON.parse(document.querySelector('textarea').value);
  draft.product_prefix_locked = false;
  draft.adapter_settings.mapping.postprocess.product_code_prefix = 'FORGED-';
  const beforeTamper = writes.length;
  await input(document.querySelector('textarea'), JSON.stringify(draft));
  assert(button('Uložiť').disabled, 'JSON prefix changes cannot bypass the server lock metadata');
  assert(document.body.textContent.includes('Prefix je uzamknutý'));
  await click(button('Formátovať'));
  assert(button('Uložiť').disabled, 'Formatting invalid identity changes does not re-enable saving');
  await click(button('Obecné'));
  assert(document.getElementById('supplier-product-prefix').disabled);
  assert.equal(document.getElementById('supplier-product-prefix').value, 'TEST-');
  assert.equal(writes.length, beforeTamper);

  await click(button('JSON'));
  draft = JSON.parse(document.querySelector('textarea').value);
  draft.product_prefix_locked = false;
  draft.name = 'Edited supplier';
  await input(document.querySelector('textarea'), JSON.stringify(draft));
  assert.equal(JSON.parse(document.querySelector('textarea').value).product_prefix_locked, true, 'JSON cannot forge editable server metadata');
  await click(button('Uložiť'));
  assert.equal(writes.at(-1).product_prefix_locked, true);
  assert.equal(stored.name, 'Edited supplier');

  global.confirm = () => true;
  await click(button('História')); await click([...document.querySelectorAll('button')].filter(element => element.textContent.trim() === 'Obnoviť').at(-1));
  assert(document.body.textContent.includes('Prefix remains locked on restore'), 'A restore lock rejection remains visible');
  await click(button('Obecné')); assert(document.getElementById('supplier-product-prefix').disabled);

  stored.product_prefix_locked = false; stored.product_prefix_lock_reason = null;
  await mount('unused-prefix');
  assert(!document.getElementById('supplier-product-prefix').disabled, 'Only a confirmed unused prefix is editable');
  await input(document.getElementById('supplier-product-prefix'), 'NEW-');
  lockOnSave = true;
  await click(button('Uložiť'));
  assert.equal(writes.at(-1).adapter_settings.mapping.postprocess.product_code_prefix, 'NEW-');
  assert(document.getElementById('supplier-product-prefix').disabled, 'Authoritative lock returned by save takes effect immediately');

  stored.adapter_settings.price_coefficients = Object.fromEntries(Array.from({ length: 37 }, (_, i) => [`Brand ${i + 1}`, 1]));
  stored.adapter_settings.price_coefficients['Brand & Wheel'] = 1;
  stored.feeds.sources.products.remote = { ...stored.feeds.sources.products.remote, url: '', urls: Array.from({ length: 38 }, (_, i) => `https://supplier.example.test/brand-${i + 1}.xml`) };
  const beforePrices = structuredClone(stored);
  await mount('sloger-prices', 'sloger');
  await click(button('Ceny'));
  assert.equal(document.querySelectorAll('[data-price-brand]').length, 38, 'Every configured brand is editable');
  assert.equal(button('Uložiť').disabled, true, 'Opening price settings does not modify the config');
  const coefficient = () => document.getElementById('supplier-price-coefficient-Brand%20%26%20Wheel');
  await input(coefficient(), '0.95');
  await click(button('JSON'));
  assert.equal(JSON.parse(document.querySelector('textarea').value).adapter_settings.price_coefficients['Brand & Wheel'], '0.95', 'The price form and JSON share one decimal-preserving draft');
  await click(button('Ceny'));
  await input(coefficient(), '0');
  assert(button('Uložiť').disabled, 'A zero coefficient cannot be saved');
  assert.equal(coefficient().getAttribute('aria-invalid'), 'true');
  await click(button('Obecné'));
  assert(button('Uložiť').disabled, 'Switching tabs cannot bypass invalid price coefficients');
  await click(button('Ceny'));
  await input(coefficient(), '');
  assert(button('Uložiť').disabled, 'An unfinished numeric edit cannot become a zero/default coefficient');
  await input(coefficient(), '0.95');
  await input(document.getElementById('supplier-price-new-brand'), ' brand & wheel ');
  assert(button('Pridať s koeficientom 1').disabled, 'Brand matching is case-insensitive and trims whitespace');
  await input(document.getElementById('supplier-price-new-brand'), 'new brand');
  await click(button('Pridať s koeficientom 1'));
  assert.equal(document.getElementById('supplier-price-coefficient-NEW%20BRAND').value, '1');
  await click(button('Uložiť'));
  const expectedPrices = structuredClone(beforePrices);
  expectedPrices.adapter_settings.price_coefficients['Brand & Wheel'] = '0.95';
  expectedPrices.adapter_settings.price_coefficients['NEW BRAND'] = 1;
  assert.deepEqual(writes.at(-1), expectedPrices, 'Editing brand coefficients preserves feeds, purchase settings and unrelated config');
  await mount('sloger-prices-saved', 'sloger');
  await click(button('Ceny'));
  assert.equal(coefficient().value, '0.95', 'Saved brand coefficients survive reopening');
  await click(button('JSON'));
  draft = JSON.parse(document.querySelector('textarea').value);
  draft.adapter_settings.price_coefficients['Brand & Wheel'] = 'Infinity';
  await input(document.querySelector('textarea'), JSON.stringify(draft));
  assert(button('Uložiť').disabled, 'JSON edits cannot bypass finite positive coefficient validation');
  draft.adapter_settings.price_coefficients['Brand & Wheel'] = '0.95';
  await input(document.querySelector('textarea'), JSON.stringify(draft));

  await click(button('Feedy'));
  const urlsField = document.getElementById('supplier-sloger-feed-urls');
  assert.equal(urlsField.value.split('\n').length, 38, 'All brand listing URLs are visible in Sloger settings');
  urlsField.focus();
  await input(urlsField, `${urlsField.value}\n`);
  assert.equal(document.activeElement, urlsField, 'Typing a newline preserves focus and the trailing line');
  assert(urlsField.value.endsWith('\n'));
  await input(urlsField, `${urlsField.value}https://supplier.example.test/new-brand.xml\n`);
  await click(button('Obecné'));
  await click(button('Uložiť'));
  assert.deepEqual(writes.at(-1).feeds.sources.products.remote.urls, [...beforePrices.feeds.sources.products.remote.urls, 'https://supplier.example.test/new-brand.xml'], 'Multiline URLs are saved without blank lines and survive switching tabs');
  assert.equal(writes.at(-1).feeds.sources.products.remote.url, '');
  assert.deepEqual(writes.at(-1).adapter_settings, expectedPrices.adapter_settings);
  await mount('other-supplier-feeds');
  await click(button('Feedy'));
  assert.equal(document.getElementById('supplier-sloger-feed-urls'), null, 'Other suppliers keep the existing single-URL editor');
  await act(async () => root.unmount());
  console.log('Supplier configuration UI checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
