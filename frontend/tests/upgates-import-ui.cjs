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
const i18n = require('../src/i18n/index.ts').default;
const React = require('react');
const { act } = React;
const { createRoot } = require('react-dom/client');
const { UpgatesImportModal } = require('../src/components/UpgatesImportModal.tsx');
const requests = [];
let imported = 0;
let preview = {
  shop: 'biketrek', total_in_upgates: 1, already_in_db: 0, without_any_code: 0, new_count: 1,
  catalog_source: 'cache', catalog_age_s: 1,
  new_products: [{ key: 'TEST-1', code: 'TEST-1', title: 'Test product', manufacturer: 'Test', variants_count: 0 }],
};
let importResult = { message: 'Product data imported', skipped: [], stock_initialized: 0 };
let previewAfterImport;
let failPreview = false;
global.fetch = async (path, init = {}) => {
  const body = init.body ? JSON.parse(init.body) : undefined;
  requests.push({ path, body });
  let data;
  if (path.includes('/preview')) {
    if (failPreview) return { ok: false, status: 502, statusText: 'Bad Gateway', headers: { get: () => 'application/json' }, text: async () => JSON.stringify({ detail: 'Synthetic preview failure' }) };
    data = structuredClone(preview);
  }
  else if (path.endsWith('/import')) {
    data = structuredClone(importResult);
    if (previewAfterImport) preview = structuredClone(previewAfterImport);
  }
  else throw new Error(`Unexpected request: ${path}`);
  return { ok: true, headers: { get: () => 'application/json' }, text: async () => JSON.stringify(data) };
};
const root = createRoot(document.getElementById('root'));
async function click(element) {
  assert.ok(element, 'Expected UI element');
  await act(async () => element.dispatchEvent(new window.MouseEvent('click', { bubbles: true })));
}
const importButton = () => [...document.querySelectorAll('button')].find(button => button.textContent.includes('Importovať vybrané'));
const button = text => [...document.querySelectorAll('button')].find(button => button.textContent.trim() === text);
async function mount(key) {
  await act(async () => root.render(React.createElement(UpgatesImportModal, {
    key, shop: 'biketrek', onClose: () => {}, onImported: () => imported++,
  })));
}

(async () => {
  await mount('legacy-response');
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

  const conflict = { code: 'CONFLICT-1', reasons: ['remote_code_duplicate', 'mapping_identifier_conflict'], candidate_product_ids: [41, 42] };
  preview = { ...preview, total_in_upgates: 6, already_in_db: 2, new_count: 3, conflict_count: 1, conflicts: [conflict],
    new_products: ['new', 'identified', 'partial'].map((status, i) => ({ key: `TEST-${i + 1}`, code: `TEST-${i + 1}`, title: `Product ${i + 1}`, manufacturer: 'Test', variants_count: i === 2 ? 2 : 0, identity_status: status })) };
  await mount('identity-preview');
  assert(document.body.textContent.includes('prepojené s týmto e-shopom: 2'), 'The known count describes same-shop links, not arbitrary products in the database');
  assert.equal(document.querySelectorAll('tbody tr').length, 3);
  assert.deepEqual([...document.querySelectorAll('tbody tr')].map(row => row.children[4].textContent), ['Nový produkt', 'Nájdený v Hube', 'Čiastočne prepojené varianty']);
  const previewAlert = document.querySelector('[role="alert"]');
  assert(previewAlert.textContent.includes('Konfliktné skupiny: 1') && previewAlert.textContent.includes('CONFLICT-1'));
  assert(previewAlert.textContent.includes('Rovnaký kód sa v e-shope opakuje.') && previewAlert.textContent.includes('Identifikátor nezodpovedá existujúcemu prepojeniu.'));
  assert(previewAlert.textContent.includes('41, 42'), 'Conflicts retain candidate identities for diagnosis');
  assert(!document.querySelector('input[aria-label="Vybrať produkt: CONFLICT-1"]'), 'Conflicted families cannot be selected for import');
  assert(document.getElementById('upgates-refresh-snapshots-help').textContent.includes('Spoločné produktové údaje, nákupné ceny a fyzický sklad sa nemenia.'));

  importResult = { created_products: 1, created_variants: 1, linked_products: 2, updated_products: 0, content_saved: 3, stock_initialized: 0,
    message: 'Backend message is not a success count', skipped: [], conflict_count: 0, conflicts: [] };
  previewAfterImport = { ...preview, already_in_db: 5, new_count: 0, new_products: [] };
  await click(importButton());
  sent = requests.filter(request => request.body).at(-1);
  assert.deepEqual(sent.body.codes, ['TEST-1', 'TEST-2', 'TEST-3'], 'New and safely identified rows can import together, excluding conflicts');
  assert.equal(sent.body.include_stock, false);
  assert(document.querySelector('[role="status"]').textContent.includes('nové prepojenia: 2'), 'Results separate linking from product creation');
  assert(document.body.textContent.includes('Na spracovanie nezostali žiadne položky bez konfliktu.'));
  assert(!document.body.textContent.includes('Všetky produkty s kódom sú prepojené'), 'Conflict-only results are not presented as fully linked');
  assert(importButton().disabled, 'No safe selected items means no new import');
  await click(document.querySelector('label input[type="checkbox"]'));
  await click(button('Obnoviť uložené údaje e-shopu'));
  sent = requests.filter(request => request.body).at(-1);
  assert.deepEqual(sent.body, { codes: [], update_existing: true, include_stock: false }, 'Snapshot refresh remains separate from creating products or touching physical stock');

  preview = { ...preview, already_in_db: 5, new_count: 1, conflict_count: 0, conflicts: [],
    new_products: [{ key: 'CHANGED', code: 'CHANGED', title: 'Changed identity', manufacturer: 'Test', variants_count: 0, identity_status: 'identified' }] };
  await mount('identity-changed-during-import');
  const changedConflict = { code: 'CHANGED', reasons: ['identity_changed'], candidate_product_ids: [41] };
  importResult = { ...importResult, created_products: 0, created_variants: 0, linked_products: 0, content_saved: 0,
    conflict_count: 1, conflicts: [changedConflict], skipped: [{ code: 'CHANGED', reason: 'identity_changed' }] };
  previewAfterImport = { ...preview, new_count: 0, new_products: [], conflict_count: 1, conflicts: [changedConflict] };
  await click(importButton());
  const resultAlert = [...document.querySelectorAll('[role="alert"]')].find(alert => alert.textContent.startsWith('Konflikty pri importe'));
  assert(resultAlert.textContent.includes('Identita sa od náhľadu zmenila. Obnov náhľad.'), 'Import-time conflicts remain visible with their reason');
  assert(document.querySelector('[role="status"]').getAttribute('style').includes('var(--color-warning)'), 'A conflicted import does not use an all-success status');
  assert(document.querySelector('[role="status"]').textContent.includes('Vytvorené produkty: 0'));

  await act(async () => i18n.changeLanguage('en'));
  assert(document.body.textContent.includes('Identity changed since the preview. Refresh the preview.'), 'Diagnostics have English translations');
  assert(document.body.textContent.includes('No items without conflicts remain to process.'));
  await act(async () => i18n.changeLanguage('sk'));

  // An unsuccessful follow-up preview must not erase the result of a completed request.
  preview = { ...preview, new_count: 1, new_products: [{ key: 'CHANGED', code: 'CHANGED', title: 'Changed identity', manufacturer: 'Test', variants_count: 0 }] };
  await mount('post-import-preview-fails');
  failPreview = true;
  await click(importButton());
  assert(document.body.textContent.includes('Synthetic preview failure'));
  assert([...document.querySelectorAll('[role="alert"]')].some(alert => alert.textContent.includes('Konflikty pri importe')));
  assert(importButton().disabled, 'A failed follow-up preview blocks a blind second submission');
  failPreview = false;
  await click(button('Skúsiť znovu'));
  assert(document.body.textContent.includes('Na spracovanie nezostali žiadne položky bez konfliktu.'));
  assert(!requests.at(-1).path.includes('refresh=true'), 'Retrying failed preview does not accidentally pass a click event as refresh=true');
  await act(async () => root.unmount());
  console.log('Upgates pull UI passed: safe payload, identity status, conflicts, snapshot refresh, import-time change and preview retry.');
})().catch(error => { console.error(error); process.exitCode = 1; });
