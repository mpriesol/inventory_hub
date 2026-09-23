/* Actual pull modal and API client, synthetic responses, no network. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<html><body><div id="root"></div></body></html>', { url: 'https://hub.example.test/stock' });
global.window = dom.window;
global.document = dom.window.document;
Object.defineProperty(global, 'navigator', { value: dom.window.navigator, configurable: true });
global.HTMLElement = dom.window.HTMLElement;
global.localStorage = dom.window.localStorage;
global.IS_REACT_ACT_ENVIRONMENT = true;
for (const ext of ['.ts', '.tsx']) require.extensions[ext] = (module, filename) => {
  const source = fs.readFileSync(filename, 'utf8').replace(/import\.meta\.env/g, '{}');
  module._compile(ts.transpileModule(source, { compilerOptions: { jsx: ts.JsxEmit.React, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, esModuleInterop: true } }).outputText, filename);
};
require('../src/i18n/index.ts');
const React = require('react');
const { act } = React;
const { createRoot } = require('react-dom/client');
const { UpgatesImportModal } = require('../src/components/UpgatesImportModal.tsx');
const requests = [];
let imported = 0;
global.fetch = async (path, init = {}) => {
  const body = init.body ? JSON.parse(init.body) : undefined;
  requests.push({ path, body });
  let data;
  if (path.endsWith('/preview')) data = {
    shop: 'biketrek', total_in_upgates: 1, already_in_db: 0, without_any_code: 0, new_count: 1,
    catalog_source: 'cache', catalog_age_s: 1,
    new_products: [{ key: 'TEST-1', code: 'TEST-1', title: 'Test product', manufacturer: 'Test', variants_count: 0 }],
  };
  else if (path.endsWith('/import')) data = { message: 'Product data imported', skipped: [], stock_initialized: 0 };
  else throw new Error(`Unexpected request: ${path}`);
  return { ok: true, headers: { get: () => 'application/json' }, text: async () => JSON.stringify(data) };
};
const root = createRoot(document.getElementById('root'));
async function click(element) {
  assert.ok(element, 'Expected UI element');
  await act(async () => element.dispatchEvent(new window.MouseEvent('click', { bubbles: true })));
}
const importButton = () => [...document.querySelectorAll('button')].find(button => button.textContent.includes('Importovať vybrané'));

(async () => {
  await act(async () => root.render(React.createElement(UpgatesImportModal, {
    shop: 'biketrek', onClose: () => {}, onImported: () => imported++,
  })));
  assert.ok(document.body.textContent.includes('Fyzický sklad a nákupné ceny sa tým nemenia.'));
  assert.equal([...document.querySelectorAll('label')].some(label => label.textContent.includes('skladové zásoby')), false);
  await click(importButton());
  let sent = requests.filter(request => request.body).at(-1);
  assert.deepEqual(sent.body, { codes: ['TEST-1'], update_existing: false, include_stock: false });
  assert.equal(imported, 1);
  await click(document.querySelector('label input[type="checkbox"]'));
  await click(importButton());
  sent = requests.filter(request => request.body).at(-1);
  assert.equal(sent.body.update_existing, true);
  assert.equal(sent.body.include_stock, false, 'Updating content must also leave physical stock untouched');
  assert.equal(imported, 2);
  await act(async () => root.unmount());
  console.log('Upgates product-only pull UI checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
