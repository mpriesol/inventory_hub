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
const writes = [];
global.fetch = async (path, init = {}) => {
  const url = new URL(path, 'https://hub.example.test');
  let data;
  let status = 200;
  if (url.pathname === '/api/suppliers') data = [];
  else if (url.pathname === '/api/suppliers/fixture/config') {
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
        data = structuredClone(stored);
      }
    } else data = structuredClone(stored);
  } else throw new Error(`Unexpected endpoint: ${url.pathname}`);
  return { ok: status === 200, status, statusText: status === 200 ? 'OK' : 'Unprocessable Entity', headers: { get: () => 'application/json' }, text: async () => JSON.stringify(data) };
};
const root = createRoot(document.getElementById('root'));
const button = text => [...document.querySelectorAll('button')].find(element => element.textContent.trim() === text);
const field = key => document.getElementById(`supplier-availability-${key}`);
const tick = () => new Promise(resolve => setTimeout(resolve, 5));
async function mount(key) {
  await act(async () => { root.render(React.createElement(MemoryRouter, { key, initialEntries: ['/suppliers?edit=fixture'] }, React.createElement(SuppliersPage))); await tick(); });
}
async function click(element) {
  assert(element, 'Expected UI control');
  await act(async () => { element.click(); await tick(); });
}
async function input(element, value) {
  await act(async () => {
    Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, 'value').set.call(element, value);
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
  await act(async () => root.unmount());
  console.log('Supplier configuration UI checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
