/* Real mapping editor and API client, with synthetic feed/shop responses only. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'https://hub.example.test/suppliers/fixture/feed-mapping' });
global.window = dom.window; global.document = dom.window.document;
Object.defineProperty(global, 'navigator', { value: dom.window.navigator, configurable: true });
global.HTMLElement = dom.window.HTMLElement; global.FormData = dom.window.FormData;
global.IS_REACT_ACT_ENVIRONMENT = true;
for (const ext of ['.ts', '.tsx']) require.extensions[ext] = (module, filename) => {
  module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8').replace(/import\.meta\.env/g, '{}'), {
    compilerOptions: { jsx: ts.JsxEmit.React, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, esModuleInterop: true },
  }).outputText, filename);
};
require.extensions['.css'] = () => {};
require('../src/i18n/index.ts');
const React = require('react'); const { act } = React;
const { createRoot } = require('react-dom/client');
const { createMemoryRouter, RouterProvider, Link, Outlet } = require('react-router-dom');
const { FeedMappingPage } = require('../src/pages/FeedMappingPage.tsx');
const access = require('../src/api/access.ts');
const { emptyFeedDefinition } = require('../src/api/feedMapping.ts');

const fields = [
  { key: 'code', label: 'Code', type: 'text' },
  { key: 'name', label: 'Name', type: 'text' },
  { key: 'seo_title', label: 'SEO title', type: 'text' },
];
const base = { supplier: 'fixture', feed_key: 'products', shop: '', revision: 3, definition: emptyFeedDefinition(), fields, native_parser: true, configured: true };
let baseStored = structuredClone(base);
let shopStored = { ...structuredClone(base), shop: 'biketrek', revision: 1, base_revision: 3, inherited_definition: structuredClone(base.definition) };
const inspection = {
  format: 'xml', record_path: 'SHOP/ITEM', total_records: 2,
  fields: [
    { path: 'ITEM_NAME', type: 'string', examples: ['Duša 26 × 1.75'], populated: 2, total: 2 },
    { path: 'ITEM_CODE', type: 'string', examples: ['TUBE-001'], populated: 2, total: 2 },
    { path: 'CATEGORY', type: 'string', examples: ['Komponenty / Duše'], populated: 2, total: 2 },
  ], sample_records: [{ ITEM_NAME: 'Duša 26 × 1.75', ITEM_CODE: 'TUBE-001' }], source_categories: ['Komponenty / Duše'],
};
const product = { code: 'TUBE-001', name: 'Overená duša', eans: ['1234567890128'], brand: 'Fixture', category: 'Komponenty / Duše', prices: { purchase_net: 3.5, retail_gross: 6, currency: 'EUR' }, seo_title: 'Overená duša | BIKETREK' };
const calls = [];
let rejectSave = false;
window.confirm = () => true;
global.fetch = async (path, init = {}) => {
  const url = new URL(path, 'https://hub.example.test');
  const multipart = init.body instanceof dom.window.FormData;
  const body = init.body && !multipart ? JSON.parse(init.body) : init.body;
  calls.push({ path: url.pathname, query: url.searchParams, ...init, body });
  let data; let status = 200;
  if (url.pathname === '/api/suppliers/fixture/catalog') data = { name: 'Fixture supplier', sources: [{ key: 'products', name: 'Produktový feed' }], shops: [{ code: 'biketrek', name: 'BIKETREK' }] };
  else if (url.pathname === '/api/suppliers/fixture/feed-mapping') {
    if (init.method === 'PUT') {
      if (rejectSave) { status = 409; data = { detail: { code: 'feed_mapping_revision_conflict', message: 'Saved mapping changed' } }; }
      else {
        const previous = body.shop ? shopStored : baseStored;
        assert.equal(body.expected_revision, previous.revision, 'Writes must compare the revision loaded by the editor');
        const definition = structuredClone(body.definition);
        definition.bindings = definition.bindings.map(binding => ({ ...binding, constant: binding.constant ?? null }));
        data = { ...previous, definition, revision: previous.revision + 1 };
        if (body.shop) shopStored = structuredClone(data); else baseStored = structuredClone(data);
      }
    } else data = structuredClone(url.searchParams.get('shop') ? shopStored : baseStored);
  } else if (url.pathname.endsWith('/feed-mapping/inspection')) data = structuredClone(inspection);
  else if (url.pathname.endsWith('/feed-mapping/preview')) data = { items: [structuredClone(product)], errors: [], mapping_revision: body.shop ? shopStored.revision : baseStored.revision };
  else if (url.pathname.endsWith('/feed-mapping/upload')) data = { ...structuredClone(inspection), sample_id: 'synthetic-sample' };
  else if (url.pathname.endsWith('/feed-mapping/remap')) data = { items: 2, status: 'completed' };
  else if (url.pathname === '/api/shops/biketrek/import/options') data = {
    categories: [
      { code: 'menu', parent_code: null, names: { sk: 'Menu' }, active: true, assignable: false },
      { code: 'components', parent_code: 'menu', names: { sk: 'Komponenty' }, active: true, assignable: true },
      { code: 'tubes', parent_code: 'components', names: { sk: 'Duše' }, active: true, assignable: true },
      { code: 'hidden', parent_code: 'components', names: { sk: 'Neaktívne' }, active: false, assignable: true },
    ], parameters: [],
  };
  else throw new Error('Unexpected feed mapping request: ' + url.pathname);
  return { ok: status === 200, status, json: async () => data };
};
const root = createRoot(document.getElementById('root'));
const router = createMemoryRouter([{ element: React.createElement(React.Fragment, null, React.createElement(Link, {to: '/suppliers'}, 'Sidebar suppliers'), React.createElement(Outlet)), children: [
  { path: '/suppliers', element: React.createElement('div', null, 'Supplier list') },
  { path: '/suppliers/:supplier/feed-mapping', element: React.createElement(FeedMappingPage) },
]}], {initialEntries: ['/suppliers', '/suppliers/fixture/feed-mapping'], initialIndex: 1});
const tick = () => new Promise(resolve => setTimeout(resolve, 5));
const button = text => [...document.querySelectorAll('button')].find(element => element.textContent.trim() === text);
const labelled = label => document.querySelector(`[aria-label="${label}"]`);
const byLabelText = text => [...document.querySelectorAll('label')].find(element => element.textContent.trim() === text)?.querySelector('input');
const count = suffix => calls.filter(call => call.path.endsWith(suffix)).length;
async function click(element) { assert(element, 'Expected UI button'); await act(async () => { element.click(); await tick(); }); }
async function input(element, value) {
  assert(element, 'Expected editable field');
  await act(async () => {
    const select = element.tagName === 'SELECT';
    Object.getOwnPropertyDescriptor(select ? dom.window.HTMLSelectElement.prototype : dom.window.HTMLInputElement.prototype, 'value').set.call(element, value);
    element.dispatchEvent(new dom.window.Event(select ? 'change' : 'input', { bubbles: true }));
    await tick();
  });
}
async function tab(text) { await click([...document.querySelectorAll('.fm-tabs button')].find(element => element.textContent.includes(text))); }

(async () => {
  await act(async () => {
    root.render(React.createElement(RouterProvider, {router}));
    await tick();
  });
  assert(document.body.textContent.includes('Na zobrazenie a úpravu mapovania sa prihláste.'));
  assert.equal(calls.length, 0, 'Signed-out page does not fetch protected supplier details');
  assert(!button('Uložiť mapovanie'));
  await act(async () => { access.setHubUser({ id: 1, username: 'fixture', display_name: 'Fixture User', role: 'admin', active: true }); await tick(); });
  await act(tick);
  assert(button('Uložiť mapovanie').disabled);
  await click(button('Analyzovať aktuálny feed'));
  assert(document.body.textContent.includes('ITEM_NAME'));
  assert(document.body.textContent.includes('Duša 26 × 1.75'), 'Inspection displays actual example values');
  assert.equal(count('/preview'), 0, 'Inspecting a feed does not automatically preview, save or remap it');
  await click(button('Prejsť na mapovanie polí'));
  await click(button('Pridať pole'));
  await input(labelled('Cieľové pole, riadok 1'), 'name');
  await input(labelled('Zdroj hodnoty, riadok 1'), 'ITEM_NAME');
  await click(button('+ Pridať úpravu hodnoty'));
  await input(labelled('Úprava hodnoty'), 'prefix');
  await input(labelled('Hodnota úpravy'), 'BIKETREK ');
  await click(byLabelText('Doplniť chýbajúce SEO z názvu a popisu'));
  await click(button('Overiť náhľad'));
  const draftPreview = calls.find(call => call.path.endsWith('/preview'));
  assert.deepEqual(draftPreview.body.definition.bindings, [{ target: 'name', source: 'ITEM_NAME', transforms: [{ op: 'prefix', value: 'BIKETREK ' }] }]);
  assert.equal(draftPreview.body.definition.seo_fallback, false);
  assert(document.body.textContent.includes('Overená duša | BIKETREK'), 'Preview shows the server-computed SEO output');
  assert.equal(calls.filter(call => call.method === 'PUT').length, 0, 'Preview cannot persist mapping edits');
  assert.equal(count('/remap'), 0, 'Preview cannot reimport the supplier catalog');
  assert(button('Prepočítať katalóg podľa mapovania').disabled, 'Unsaved mapping cannot be applied to the catalog');

  await click(byLabelText('Doplniť chýbajúce SEO z názvu a popisu'));
  await click(button('Uložiť mapovanie'));
  const saved = calls.find(call => call.method === 'PUT');
  assert.equal(saved.body.expected_revision, 3);
  assert.equal(saved.body.shop, '');
  assert.equal(saved.body.definition.seo_fallback, true);
  assert.equal(saved.credentials, 'same-origin');
  assert.equal(saved.headers['X-Hub-Request'], '1');
  assert(button('Uložiť mapovanie').disabled);
  assert(document.body.textContent.includes('Uložená verzia 4'));
  assert.equal(labelled('Zdroj hodnoty').value, 'source', 'Nullable constant from the API must not turn a feed binding into a fixed value');
  assert.equal(labelled('Zdroj hodnoty, riadok 1').value, 'ITEM_NAME');
  assert.equal(count('/remap'), 0, 'Saving configuration is separate from reimporting products');
  assert(button('Prepočítať katalóg podľa mapovania').disabled, 'A saved configuration still needs a valid preview');
  await click(button('Overiť náhľad'));
  assert(!button('Prepočítať katalóg podľa mapovania').disabled);
  await click(button('Prepočítať katalóg podľa mapovania'));
  assert.deepEqual(calls.find(call => call.path.endsWith('/remap')).body, { feed_key: 'products', expected_revision: 4 });

  await input(labelled('Zdroj hodnoty, riadok 1'), 'ALTERNATE_NAME');
  rejectSave = true;
  const beforeConflict = calls.length;
  await click(button('Uložiť mapovanie'));
  assert(document.querySelector('[role="alert"]').textContent.includes('Vaše úpravy zostali otvorené'));
  assert.equal(labelled('Zdroj hodnoty, riadok 1').value, 'ALTERNATE_NAME', 'Conflict leaves the user draft intact');
  assert.equal(baseStored.definition.bindings[0].source, 'ITEM_NAME', 'Rejected edits do not replace saved mapping');
  assert.equal(calls.length, beforeConflict + 1, 'Conflict does not automatically retry or reload another revision');
  assert(!button('Uložiť mapovanie').disabled);
  rejectSave = false;

  await input(labelled('Platnosť mapovania'), 'biketrek');
  await act(tick);
  await tab('Zdrojové údaje');
  assert(button('Znovu analyzovať'), 'Changing only the shop retains the already analyzed supplier source');
  await tab('Kategórie');
  await click(button('Pridať kategórie zo vzorky'));
  assert.equal(labelled('Kategória dodávateľa, riadok 1').value, 'Komponenty / Duše');
  const categoryButtons = [...document.querySelectorAll('.category-tree button')];
  assert(!categoryButtons.some(element => element.textContent.includes('Komponenty')), 'Parent category is navigation, never an assignable mapping target');
  assert(!categoryButtons.some(element => element.textContent.includes('Neaktívne')), 'Inactive leaf category cannot be selected');
  await click(categoryButtons.find(element => element.textContent.startsWith('Duše')));
  assert(document.querySelector('.category-tree summary').textContent.includes('Komponenty / Duše'), 'Selected leaf shows its category path');
  await click(button('Uložiť mapovanie'));
  const shopSave = calls.filter(call => call.method === 'PUT').at(-1);
  assert.equal(shopSave.body.shop, 'biketrek');
  assert.equal(shopSave.body.expected_revision, 1);
  assert.deepEqual(shopSave.body.definition.category_rules, [{ source: 'Komponenty / Duše', target_code: 'tubes' }]);
  assert.deepEqual(baseStored.definition.category_rules, [], 'Shop category rules do not modify the shared supplier mapping');
  assert(!button('Prepočítať katalóg podľa mapovania'), 'Shop-specific mapping does not expose shared catalog remap');

  await input(labelled('Platnosť mapovania'), '');
  await act(tick);
  await tab('Zdrojové údaje');
  const file = new dom.window.File(['<SHOP><ITEM><ITEM_NAME>Duša</ITEM_NAME></ITEM></SHOP>'], 'fixture.xml', { type: 'application/xml' });
  await act(async () => {
    const upload = document.querySelector('input[type="file"]');
    Object.defineProperty(upload, 'files', { value: [file], configurable: true });
    upload.dispatchEvent(new dom.window.Event('change', { bubbles: true }));
    await tick();
  });
  const uploadCall = calls.find(call => call.path.endsWith('/upload'));
  assert.equal(uploadCall.body.get('file').name, 'fixture.xml');
  assert.equal(uploadCall.body.get('feed_key'), 'products');
  assert.equal(uploadCall.credentials, 'same-origin');
  assert.equal(uploadCall.headers['X-Hub-Request'], '1');
  assert(!uploadCall.headers['Content-Type'], 'Browser must supply the multipart boundary');
  assert(document.body.textContent.includes('Nahraná vzorka'));
  assert(button('Prepočítať katalóg podľa mapovania').disabled, 'Changing the source invalidates the remap preview');
  await click(button('Overiť náhľad'));
  assert.equal(calls.filter(call => call.path.endsWith('/preview')).at(-1).body.sample_id, 'synthetic-sample');
  assert.equal(count('/remap'), 1, 'Uploading and previewing never start catalog remap automatically');
  await tab('Produktové polia');
  await input(labelled('Zdroj hodnoty, riadok 1'), 'UNSAVED_FIELD');
  let confirmations = 0;
  window.confirm = () => { confirmations++; return false; };
  await click([...document.querySelectorAll('a')].find(link => link.textContent === 'Sidebar suppliers'));
  assert.equal(router.state.location.pathname, '/suppliers/fixture/feed-mapping', 'Sidebar navigation cannot discard unsaved mapping when cancelled');
  assert.equal(labelled('Zdroj hodnoty, riadok 1').value, 'UNSAVED_FIELD');
  assert.equal(confirmations, 1, 'Sidebar navigation prompts exactly once');
  await act(async () => { void router.navigate(-1); await tick(); });
  assert.equal(router.state.location.pathname, '/suppliers/fixture/feed-mapping', 'History Back cancellation stays in the editor');
  assert.equal(labelled('Zdroj hodnoty, riadok 1').value, 'UNSAVED_FIELD');
  assert.equal(confirmations, 2);
  const unload = new dom.window.Event('beforeunload', {cancelable:true});
  window.dispatchEvent(unload);
  assert(unload.defaultPrevented, 'Hard reload/closing the tab protects the same unsaved mapping');
  window.confirm = () => { confirmations++; return true; };
  await act(async () => { void router.navigate(-1); await tick(); });
  assert.equal(router.state.location.pathname, '/suppliers', 'Confirmed Back navigation proceeds');
  assert.equal(confirmations, 3);
  await act(async () => root.unmount());
  router.dispose();
  console.log('Feed mapping UI passed: authentication, source analysis, editable transforms/SEO, preview isolation, revision conflicts, leaf categories, multipart upload and explicit remap.');
})().catch(error => { console.error(error); process.exitCode = 1; });
