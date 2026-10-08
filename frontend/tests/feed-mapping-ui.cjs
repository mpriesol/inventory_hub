/* Real mapping editor/API client; synthetic supplier and shop responses, no live writes. */
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
const copy = require('../src/i18n/sk.json').feedMapping;
const f = (key, values = {}) => Object.entries(values).reduce((text, [name, value]) => text.replaceAll('{{' + name + '}}', String(value)), copy[key]);
const React = require('react'); const { act } = React;
const { createRoot } = require('react-dom/client');
const { createMemoryRouter, RouterProvider, Link, Outlet } = require('react-router-dom');
const { FeedMappingPage } = require('../src/pages/FeedMappingPage.tsx');
const access = require('../src/api/access.ts');
const { emptyFeedDefinition } = require('../src/api/feedMapping.ts');
const clone = value => structuredClone(value);
const fields = [
  { key: 'code', label: 'Code', type: 'text' },
  { key: 'name', label: 'Name', type: 'text' },
  { key: 'seo_title', label: 'SEO title', type: 'text' },
  { key: 'category', label: 'Category', type: 'text' },
  { key: 'category_code', label: 'Category code', type: 'text' },
];
const base = { supplier: 'fixture', feed_key: 'products', shop: '', revision: 3, definition: emptyFeedDefinition(), fields, native_parser: true, configured: true };
let baseStored = clone(base);
let shopStored = { ...clone(base), shop: 'biketrek', revision: 1, base_revision: 3, inherited_definition: clone(base.definition) };
const inspection = {
  format: 'xml', record_path: 'SHOP/SHOPITEMS/SHOPITEM', total_records: 2,
  fields: [
    { path: 'ITEM_NAME', type: 'string', examples: ['Duša 26 × 1.75'], populated: 2, total: 2 },
    { path: 'ITEM_CODE', type: 'string', examples: ['TUBE-001'], populated: 2, total: 2 },
    { path: 'CATEGORYTEXT', type: 'string', examples: ['Komponenty / Duše'], populated: 2, total: 2 },
    { path: 'CATEGORYID', type: 'string', examples: ['supplier-tubes'], populated: 2, total: 2 },
  ], sample_records: [{ ITEM_NAME: 'Duša 26 × 1.75', ITEM_CODE: 'TUBE-001' }],
  source_categories: ['supplier-tubes', 'supplier-tires'],
  source_category_options: [
    { source: 'supplier-tubes', code: 'supplier-tubes', label: 'Komponenty / Duše', count: 1 },
    { source: 'supplier-tires', code: 'supplier-tires', label: 'Komponenty / Plášte', count: 1 },
  ],
  category_fields: { code_path: 'CATEGORYID', label_path: 'CATEGORYTEXT' },
  source_parameters: [
    { name: 'Šírka', source: 'DYN_PARAMS/PARAM', param_name_path: 'DESC', param_value_path: 'VAL', examples: ['1.75', '2.00'], populated: 2, total: 2 },
    { name: 'Dĺžka', source: 'DYN_PARAMS/PARAM', param_name_path: 'DESC', param_value_path: 'VAL', examples: ['35 mm'], populated: 1, total: 2 },
  ],
};
const options = {
  rules_version: 15, checked_at: '2026-10-08T12:00:00Z', warnings: [],
  parameters: [{ id: 11, names: { sk: 'Šírka plášťa', en: 'Tyre width' } }, { id: 12, names: { sk: 'Dĺžka ventilu' } }],
  category_profiles: [{ id: 'tires', name: 'Plášte', registry_status: 'configured', category_codes: ['tires'], parameters: [{ name: 'Šírka plášťa', required: true, scope: 'product', values: ['1.75', '2.00'], unit: 'palce' }] }],
};
const product = { code: 'TUBE-001', name: 'Overená duša', eans: ['1234567890128'], brand: 'Fixture', category: 'Komponenty / Duše', prices: { purchase_net: 3.5, retail_gross: 6, currency: 'EUR' }, seo_title: 'Overená duša | BIKETREK' };
const calls = [];
let rejectSave = false;
let releaseSlowStatus, releaseSlowInspection;
window.confirm = () => true;
global.fetch = async (path, init = {}) => {
  const url = new URL(path, 'https://hub.example.test');
  const multipart = init.body instanceof dom.window.FormData;
  const body = init.body && !multipart ? JSON.parse(init.body) : init.body;
  calls.push({ path: url.pathname, query: url.searchParams, ...init, body });
  const supplier = url.pathname.split('/')[3];
  let data; let status = 200;
  if (/^\/api\/suppliers\/[^/]+\/catalog$/.test(url.pathname)) {
    const value = { name: supplier, sources: [{ key: 'products', name: supplier === 'next' ? 'Nový produktový feed' : supplier === 'slow' ? 'Starý produktový feed' : 'Produktový feed' }], shops: [{ code: 'biketrek', name: 'BIKETREK' }] };
    data = supplier === 'slow' ? await new Promise(resolve => { releaseSlowStatus = () => resolve(value); }) : value;
  } else if (/\/feed-mapping$/.test(url.pathname)) {
    if (init.method === 'PUT') {
      if (rejectSave) { status = 409; data = { detail: { code: 'feed_mapping_revision_conflict', message: 'Saved mapping changed' } }; }
      else {
        const previous = body.shop ? shopStored : baseStored;
        assert.equal(body.expected_revision, previous.revision, 'Writes compare the loaded revision');
        const definition = clone(body.definition);
        definition.bindings = definition.bindings.map(binding => ({ ...binding, constant: binding.constant ?? null }));
        data = { ...previous, definition, revision: previous.revision + 1 };
        if (body.shop) shopStored = clone(data); else baseStored = clone(data);
      }
    } else data = { ...clone(url.searchParams.get('shop') ? shopStored : baseStored), supplier };
  } else if (url.pathname.endsWith('/feed-mapping/inspection')) {
    const value = clone(inspection);
    if (supplier !== 'fixture') {
      value.source_category_options = [{ source: supplier, label: supplier === 'next' ? 'Nové kategórie' : 'Staré kategórie', count: 2 }];
      value.source_categories = [supplier];
    }
    data = supplier === 'slow' ? await new Promise(resolve => { releaseSlowInspection = () => resolve(value); }) : value;
  } else if (url.pathname.endsWith('/feed-mapping/parameter-options')) data = clone(options);
  else if (url.pathname.endsWith('/feed-mapping/preview')) data = { items: [clone(product)], errors: [], mapping_revision: body.shop ? shopStored.revision : baseStored.revision };
  else if (url.pathname.endsWith('/feed-mapping/upload')) data = { ...clone(inspection), sample_id: 'synthetic-sample' };
  else if (url.pathname.endsWith('/feed-mapping/remap')) data = { items: 2, status: 'completed' };
  else if (url.pathname === '/api/shops/biketrek/import/options') data = {
    categories: [
      { code: 'menu', parent_code: null, names: { sk: 'Menu' }, active: true, assignable: false },
      { code: 'components', parent_code: 'menu', names: { sk: 'Komponenty' }, active: true, assignable: true },
      { code: 'tubes', parent_code: 'components', names: { sk: 'Duše' }, active: true, assignable: true },
      { code: 'tires', parent_code: 'components', names: { sk: 'Plášte' }, active: true, assignable: true },
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
const labelled = label => [...document.querySelectorAll('[aria-label]')].find(element => element.getAttribute('aria-label') === label);
const byLabelText = text => [...document.querySelectorAll('label')].find(element => element.textContent.trim() === text)?.querySelector('input,select');
const count = suffix => calls.filter(call => call.path.endsWith(suffix)).length;
const lastCall = suffix => calls.filter(call => call.path.endsWith(suffix)).at(-1);
const sourceCategory = text => [...document.querySelectorAll('.fm-source-item .fm-pick-card')].find(element => element.querySelector('strong').textContent === text);
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
async function drag(source, target) {
  assert(source && target, 'Expected drag source and destination');
  const values = new Map();
  const dataTransfer = { setData: (key, value) => values.set(key, value), getData: key => values.get(key) || '', effectAllowed: '' };
  await act(async () => {
    for (const [element, type] of [[source, 'dragstart'], [target, 'dragover'], [target, 'drop']]) {
      const event = new dom.window.Event(type, { bubbles: true, cancelable: true });
      Object.defineProperty(event, 'dataTransfer', { value: dataTransfer });
      element.dispatchEvent(event);
    }
    await tick();
  });
}
async function tab(key) { await click([...document.querySelectorAll('.fm-tabs button')].find(element => element.textContent.startsWith(f('steps_' + key)))); }
async function navigate(path) { await act(async () => { void router.navigate(path); await tick(); }); await act(tick); }

(async () => {
  await act(async () => { root.render(React.createElement(RouterProvider, {router})); await tick(); });
  assert(document.body.textContent.includes(f('loginRequired')));
  assert.equal(calls.length, 0, 'Signed-out page does not fetch protected supplier details');
  assert(!button(f('save')));
  await act(async () => { access.setHubUser({ id: 1, username: 'fixture', display_name: 'Fixture User', role: 'admin', active: true }); await tick(); });
  await act(tick);
  assert(button(f('save')).disabled);
  assert.equal(count('/inspection'), 1, 'The retained feed is inspected automatically after authentication');
  assert(sourceCategory('Komponenty / Duše'), 'Supplier category names and codes are immediately visible');
  assert.equal(count('/preview'), 0);
  assert.equal(calls.filter(call => call.method === 'PUT').length, 0);
  assert.equal(count('/remap'), 0, 'Opening the workbench cannot save or reimport products');

  await tab('fields');
  assert(document.body.textContent.includes('Duša 26 × 1.75'), 'Source cards display real example values');
  await click(labelled(f('selectField', {name: 'ITEM_NAME'})));
  await click(button(f('connectField')));
  await click(button('+ ' + f('addTransformation')));
  await input(labelled(f('transformation')), 'prefix');
  await input(labelled(f('transformValue')), 'BIKETREK ');
  await input(byLabelText(f('fallback')), 'Náhradný názov');
  await click(byLabelText(f('seoFallback')));
  await click(button(f('preview')));
  assert.deepEqual(lastCall('/preview').body.definition.bindings, [{ target: 'name', source: 'ITEM_NAME', default: 'Náhradný názov', transforms: [{ op: 'prefix', value: 'BIKETREK ' }] }]);
  assert.equal(lastCall('/preview').body.definition.seo_fallback, false);
  assert(document.body.textContent.includes('Overená duša | BIKETREK'));
  assert.equal(calls.filter(call => call.method === 'PUT').length, 0, 'Preview does not persist edits');
  assert(button(f('apply')).disabled, 'Unsaved mappings cannot be applied to the catalog');

  await input(labelled(f('newFieldTarget')), 'code');
  await drag(labelled(f('selectField', {name: 'ITEM_CODE'})), document.querySelector('.fm-new-binding'));
  assert.equal(labelled(f('sourceForRow', {row: 2})).value, 'ITEM_CODE', 'Dropping a source creates the selected target binding');
  await input(labelled(f('newFieldTarget')), 'name');
  await click(labelled(f('selectField', {name: 'ITEM_CODE'})));
  let confirmations = 0;
  window.confirm = () => { confirmations++; return false; };
  await click(button(f('connectField')));
  assert.equal(confirmations, 1);
  assert.equal(labelled(f('sourceForRow', {row: 1})).value, 'ITEM_NAME', 'Cancelling duplicate-target replacement preserves the existing source and transforms');
  assert.equal(document.querySelectorAll('.fm-binding-card').length, 2);
  window.confirm = () => true;
  await click(byLabelText(f('seoFallback')));
  await click(button(f('save')));
  const saved = calls.find(call => call.method === 'PUT');
  assert.equal(saved.body.expected_revision, 3);
  assert.equal(saved.body.shop, '');
  assert.equal(saved.credentials, 'same-origin');
  assert.equal(saved.headers['X-Hub-Request'], '1');
  assert.equal(saved.body.definition.seo_fallback, true);
  assert(button(f('save')).disabled);
  assert.equal(labelled(f('sourceMode')).value, 'source', 'API null constant does not turn a source mapping into a fixed value');
  assert.equal(count('/remap'), 0, 'Saving remains separate from catalog remap');
  assert(button(f('apply')).disabled, 'Saving a changed mapping still requires a valid preview');
  await click(button(f('preview')));
  await click(button(f('apply')));
  assert.deepEqual(lastCall('/remap').body, { feed_key: 'products', expected_revision: 4 });

  await input(labelled(f('manualSourceForRow', {row: 1})), 'ALTERNATE_NAME');
  rejectSave = true;
  const beforeConflict = calls.length;
  await click(button(f('save')));
  assert(document.querySelector('[role="alert"]').textContent.includes('Vaše úpravy zostali otvorené'));
  assert.equal(labelled(f('manualSourceForRow', {row: 1})).value, 'ALTERNATE_NAME');
  assert.equal(baseStored.definition.bindings[0].source, 'ITEM_NAME');
  assert.equal(calls.length, beforeConflict + 1, 'A revision conflict does not retry or discard the open edits');
  rejectSave = false;

  const beforeShopInspection = count('/inspection');
  await input(labelled(f('scope')), 'biketrek');
  await act(tick);
  assert.equal(count('/inspection'), beforeShopInspection, 'A shop overlay reuses the analyzed supplier feed');
  assert(document.body.textContent.includes(f('inherited', {revision: 3})));
  await tab('fields');
  assert(![...labelled(f('newFieldTarget')).options].some(option => option.value === 'code'), 'Shop overlays cannot replace supplier identity');
  await tab('categories');
  const destinations = [...document.querySelectorAll('.fm-drop-target')];
  assert.equal(destinations.length, 2, 'Only active leaf categories are offered as destinations');
  assert(!destinations.some(element => element.querySelector('strong').textContent === 'Komponenty'));
  assert(!destinations.some(element => element.textContent.includes('Neaktívne')));
  await click(sourceCategory('Komponenty / Duše'));
  await click(labelled(f('connectCategory', {name: 'Menu / Komponenty / Duše'})));
  await drag(sourceCategory('Komponenty / Plášte'), labelled(f('connectCategory', {name: 'Menu / Komponenty / Plášte'})));
  await click(button(f('save')));
  const categorySave = calls.filter(call => call.method === 'PUT').at(-1);
  assert.equal(categorySave.body.shop, 'biketrek');
  assert.equal(categorySave.body.expected_revision, 1);
  assert.deepEqual(categorySave.body.definition.category_rules, [
    { source: 'supplier-tubes', target_code: 'tubes' }, { source: 'supplier-tires', target_code: 'tires' },
  ], 'Mappings save supplier codes with readable labels, through click and drag/drop');
  assert.deepEqual(baseStored.definition.category_rules, []);
  assert(!button(f('apply')), 'Shop overlays do not expose shared catalog remap');

  await tab('parameters');
  await input(labelled(f('parameterProfile')), 'tires');
  assert(!labelled(f('connectParameter', {name: 'Dĺžka ventilu'})), 'Category profiles narrow the available registry parameters');
  await drag(labelled(f('selectParameter', {name: 'Šírka'})), labelled(f('connectParameter', {name: 'Šírka plášťa'})));
  await click(button(f('preview')));
  assert.deepEqual(lastCall('/preview').body.definition.bindings, [{ target: 'parameter:Šírka plášťa', source: 'DYN_PARAMS/PARAM', param_match_name: 'Šírka', param_name_path: 'DESC', param_value_path: 'VAL', transforms: [] }], 'A parameter binds the exact sibling name/value pair, not every value from a repeated XML list');
  await input(labelled(f('parameterProfile')), '');
  await click(labelled(f('selectParameter', {name: 'Dĺžka'})));
  await click(labelled(f('connectParameter', {name: 'Dĺžka ventilu'})));
  const beforeRefresh = calls.filter(call => call.method === 'PUT').length;
  await click(button(f('refreshParameters')));
  assert.equal(lastCall('/parameter-options').query.get('refresh'), 'true');
  assert.equal(lastCall('/parameter-options').query.get('shop'), 'biketrek');
  assert.equal(calls.filter(call => call.method === 'PUT').length, beforeRefresh, 'Refreshing the Upgates catalog never saves mappings');
  await click(button(f('save')));
  assert.equal(shopStored.definition.bindings.length, 2);
  assert.equal(shopStored.definition.bindings[1].param_match_name, 'Dĺžka');

  await input(labelled(f('scope')), '');
  await act(tick);
  await tab('source');
  const file = new dom.window.File(['<SHOP><SHOPITEMS><SHOPITEM><ITEM_NAME>Duša</ITEM_NAME></SHOPITEM></SHOPITEMS></SHOP>'], 'fixture.xml', { type: 'application/xml' });
  await act(async () => {
    const upload = document.querySelector('input[type="file"]');
    Object.defineProperty(upload, 'files', { value: [file], configurable: true });
    upload.dispatchEvent(new dom.window.Event('change', { bubbles: true }));
    await tick();
  });
  const uploadCall = lastCall('/upload');
  assert.equal(uploadCall.body.get('file').name, 'fixture.xml');
  assert.equal(uploadCall.body.get('feed_key'), 'products');
  assert.equal(uploadCall.credentials, 'same-origin');
  assert.equal(uploadCall.headers['X-Hub-Request'], '1');
  assert(!uploadCall.headers['Content-Type'], 'Browser supplies the multipart boundary');
  assert(document.body.textContent.includes(f('uploadedSample')));
  assert(button(f('apply')).disabled);
  await click(button(f('preview')));
  assert.equal(lastCall('/preview').body.sample_id, 'synthetic-sample');
  assert.equal(count('/remap'), 1, 'Upload and preview cannot trigger a second catalog remap');

  await tab('fields');
  await input(labelled(f('manualSourceForRow', {row: 1})), 'UNSAVED_FIELD');
  confirmations = 0;
  window.confirm = () => { confirmations++; return false; };
  await click([...document.querySelectorAll('a')].find(link => link.textContent === 'Sidebar suppliers'));
  assert.equal(router.state.location.pathname, '/suppliers/fixture/feed-mapping');
  assert.equal(labelled(f('manualSourceForRow', {row: 1})).value, 'UNSAVED_FIELD');
  assert.equal(confirmations, 1);
  await act(async () => { void router.navigate(-1); await tick(); });
  assert.equal(router.state.location.pathname, '/suppliers/fixture/feed-mapping');
  assert.equal(confirmations, 2);
  const unload = new dom.window.Event('beforeunload', {cancelable: true});
  window.dispatchEvent(unload);
  assert(unload.defaultPrevented, 'Hard reload also protects unsaved mapping');
  window.confirm = () => true;
  await act(async () => { void router.navigate(-1); await tick(); });
  assert.equal(router.state.location.pathname, '/suppliers');

  await navigate('/suppliers/slow/feed-mapping');
  assert(releaseSlowStatus && releaseSlowInspection, 'Both old supplier requests are still pending');
  await navigate('/suppliers/next/feed-mapping');
  assert(sourceCategory('Nové kategórie'));
  await act(async () => { releaseSlowStatus(); releaseSlowInspection(); await tick(); });
  assert(sourceCategory('Nové kategórie'), 'Late inspection from the old supplier cannot replace current categories');
  assert(!document.body.textContent.includes('Staré kategórie'));
  assert(document.body.textContent.includes('Nový produktový feed'));
  assert(!document.body.textContent.includes('Starý produktový feed'), 'Late supplier metadata cannot overwrite the current feed selector');
  assert.equal(count('/remap'), 1);
  await act(async () => root.unmount());
  router.dispose();
  console.log('Feed mapping UI passed: authenticated automatic inspection, click/drop mapping, exact parameter pairs, leaf categories, isolated scopes/preview/refresh, revision conflicts, upload, navigation and stale supplier responses.');
})().catch(error => { console.error(error); process.exitCode = 1; });
