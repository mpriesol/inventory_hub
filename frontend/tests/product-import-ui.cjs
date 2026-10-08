/* Actual React import-table interactions against synthetic APIs only. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', { url: 'https://hub.example.test/product-import' });
global.window = dom.window; global.document = dom.window.document;
Object.defineProperty(global, 'navigator', { value: dom.window.navigator, configurable: true });
global.HTMLElement = dom.window.HTMLElement; global.localStorage = dom.window.localStorage;
global.IS_REACT_ACT_ENVIRONMENT = true; window.confirm = () => true;
const pollers = new Map(); let timerId = 0;
window.setInterval = callback => { const id = ++timerId; pollers.set(id, callback); return id; };
window.clearInterval = id => pollers.delete(id);
for (const ext of ['.ts', '.tsx']) require.extensions[ext] = (module, filename) => module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8').replace(/import\.meta\.env/g, '{}'), { compilerOptions: { jsx: ts.JsxEmit.React, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, esModuleInterop: true } }).outputText, filename);
require.extensions['.css'] = () => {};
const i18n = require('../src/i18n/index.ts').default;
const React = require('react'); const { act } = React;
const { createRoot } = require('react-dom/client');
const { createMemoryRouter, RouterProvider } = require('react-router-dom');
const { ProductImportPage } = require('../src/pages/ProductImportPage.tsx');
const { unlockHub } = require('../src/api/access.ts');
const { IMPORT_COLUMNS, pasteImportValues, stageImportValue } = require('../src/pages/productImportGrid.ts');
const clone = value => JSON.parse(JSON.stringify(value));
const options = { language: 'sk', currency: 'EUR', pricelist: 'Default', category_code: null, pricing: 'configured', include_images: true, include_description: true, include_parameters: true };
const categories = [{ code: 'MENU', assignable: false, names: { sk: 'Menu' } }, { code: 'PARTS', parent_code: 'MENU', names: { sk: 'Komponenty' } }, { code: 'TUBES', parent_code: 'PARTS', names: { sk: 'Duše' } }];
const values = id => ({ code: `PL-${id}`, supplier_code: String(id), group_name: id < 3 ? 'Rodina duší' : '', name: `Duša ${id}`, brand: 'Test', manufacturer_code: '', eans: [`0000000${id}`], images: [], description_html: '<p>Feed</p>', short_description: 'Z feedu', seo_title: `Duša ${id}`, seo_description: 'SEO z feedu', seo_url: '', category_code: 'TUBES', parameters: [{ name: 'Ventil', value: 'AV' }], variant_attributes: id < 3 ? [{ name: 'Veľkosť', value: String(id) }] : [], metadata: {}, purchase_net: '5', retail_gross: '9', sale_gross: '10', vat_percent: '23', currency: 'EUR', availability: 'do 5 dní', ai_enabled: false });
let server = { id: 'test-draft', revision: 1, status: 'draft', supplier: 'paul-lange', shop: 'biketrek', feed_key: 'products', options, categories, rows: [1, 2, 3].map(id => ({ id, group_key: id < 3 ? 'family-1' : `single-${id}`, is_variant: id < 3, source_category: 'Duše dodávateľ', mapping_revision: 2, values: values(id), manual_fields: [], provenance: { category_code: 'mapping', seo_title: 'derived' }, warnings: [], errors: [], hub_product_id: null, ai_job: null })), publication: null, publication_result: null };
const calls = []; let failEdit = false, deferRead = false, releaseRead;
global.fetch = async (path, init = {}) => {
  const body = init.body ? JSON.parse(init.body) : undefined;
  calls.push({ path, method: init.method || 'GET', body });
  if (path === '/api/product-imports/test-draft' && init.method === 'GET' && deferRead) {
    deferRead = false;
    return new Promise(resolve => { releaseRead = value => resolve({ ok: true, status: 200, json: async () => value }); });
  }
  if (init.method === 'PUT' && failEdit) return { ok: false, status: 409, json: async () => ({ detail: { code: 'product_import_changed' } }) };
  if (path === '/api/product-imports' && init.method === 'GET') return { ok: true, status: 200, json: async () => ({ items: [{ id: server.id, revision: server.revision, status: server.status, supplier: server.supplier, shop: server.shop, rows_count: 3, updated_at: '2026-10-08T11:00:00Z' }] }) };
  if (path === '/api/product-imports' && init.method === 'POST') {
    assert.deepEqual(body.product_ids, [1, 2, 3]); assert.equal(body.run_id, 42); assert.ok(body.request_id);
  } else if (path === '/api/product-imports/test-draft' && init.method === 'PUT') {
    assert.equal(body.expected_revision, server.revision);
    for (const patch of body.rows) { const row = server.rows.find(item => item.id === patch.id); Object.assign(row.values, patch.values); Object.keys(patch.values).forEach(field => { row.manual_fields.push(field); row.provenance[field] = 'manual'; }); }
    server.revision++; server.status = 'draft'; server.publication = null; server.publication_result = null;
  } else if (path.endsWith('/save')) { assert.equal(body.expected_revision, server.revision); server.revision++; server.status = 'saved'; server.rows.forEach(row => row.hub_product_id = row.id + 100); }
  else if (path.endsWith('/ai')) { server.revision++; server.rows.filter(row => row.values.ai_enabled).forEach(row => row.ai_job = { id: `job-${row.group_key}`, status: 'estimate', estimate_usd: '0.04', actual_usd: null, checks: {} }); }
  else if (path.endsWith('/ai-start')) { server.revision++; server.rows.filter(row => row.ai_job).forEach(row => { row.ai_job.status = 'generating'; row.ai_job.actual_usd = '0.03'; }); }
  else if (path.endsWith('/ai-apply')) { server.revision++; server.status = 'draft'; server.rows.filter(row => row.ai_job).forEach(row => { if (!row.manual_fields.includes('name')) row.values.name = 'AI názov'; if (!row.manual_fields.includes('description_html')) { row.values.description_html = '<p>Vylepšené AI</p>'; row.provenance.description_html = 'ai'; } row.ai_job.status = 'completed'; }); }
  else if (path.endsWith('/publish-preview')) { assert.equal(server.status, 'saved'); server.revision++; server.publication = { preview_id: 'preview-1', options, errors: [], warnings: [], expires_at: new Date(Date.now() + 60000).toISOString(), items: [{ code: 'PL-G', name: 'Rodina duší', product_ids: [1, 2], variants_count: 2, status: 'ready', warnings: [], errors: [], payload: { code: 'PL-G', active_yn: false } }] }; }
  else if (path.endsWith('/publish')) { assert.equal(body.preview_id, server.publication.preview_id); server.revision++; server.publication_result = { ...server.publication, status: 'completed', items: server.publication.items.map(item => ({ ...item, status: 'created' })) }; }
  else if (path !== '/api/product-imports/test-draft') throw new Error(`Unexpected API ${path}`);
  return { ok: true, status: 200, json: async () => clone(server) };
};
const root = createRoot(document.getElementById('root'));
const button = text => [...document.querySelectorAll('button')].find(element => element.textContent.trim() === text);
const field = (id, key) => document.querySelector(`tr[data-row="${id}"] td[data-field="${key}"]`);
const label = text => document.querySelector(`[aria-label="${text}"]`);
async function click(element) { assert.ok(element, 'Expected element'); await act(async () => element.dispatchEvent(new window.MouseEvent('click', { bubbles: true, cancelable: true }))); }
async function change(element, value) { assert.ok(element); await act(async () => { Object.getOwnPropertyDescriptor(element.tagName === 'SELECT' ? window.HTMLSelectElement.prototype : element.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype, 'value').set.call(element, value); element.dispatchEvent(new window.Event(element.tagName === 'SELECT' ? 'change' : 'input', { bubbles: true })); }); }
async function key(element, value) { await act(async () => element.dispatchEvent(new window.KeyboardEvent('keydown', { key: value, bubbles: true }))); }
async function paste(element, text) { await act(async () => { const event = new window.Event('paste', { bubbles: true, cancelable: true }); Object.defineProperty(event, 'clipboardData', { value: { getData: () => text } }); element.dispatchEvent(event); }); }
async function render(entry) {
  const router = createMemoryRouter([{ path: '/product-import', element: React.createElement(ProductImportPage) }, { path: '/suppliers/:supplier/catalog', element: React.createElement('p', null, 'Catalog') }], { initialEntries: [entry] });
  await act(async () => root.render(React.createElement(React.StrictMode, null, React.createElement(RouterProvider, { key: typeof entry === 'string' ? entry : 'new', router }))));
}

(async () => {
  // Shared family changes and multiline paste must be atomic and preserve identities.
  const name = IMPORT_COLUMNS.find(column => column.key === 'name'), brand = IMPORT_COLUMNS.find(column => column.key === 'brand');
  const initial = clone(server.rows);
  assert.throws(() => pasteImportValues(initial, {}, [brand], 1, 'brand', 'A\nB'), /familyConflict/);
  assert.equal(initial[0].values.brand, 'Test');
  const pasted = pasteImportValues(initial, {}, [name], 1, 'name', '"Prvý\nriadok"\nDruhý');
  assert.equal(pasted[1].name, 'Prvý\nriadok'); assert.equal(pasted[2].name, 'Druhý');
  assert.deepEqual(Object.keys(stageImportValue(initial, {}, initial[0], 'category_code', null)), ['1', '2']);
  const stagedFamily = stageImportValue(initial, {}, initial[0], 'brand', 'Edited');
  assert.deepEqual(pasteImportValues([initial[0]], stagedFamily, [brand], 1, 'brand', 'Test', undefined, initial), {}, 'Pasting the original shared value also resets hidden/off-page family variants');

  unlockHub('synthetic-test-token');
  await render({ pathname: '/product-import', state: { selection: { supplier: 'paul-lange', feed_key: 'products', run_id: 42, product_ids: [1, 2, 3], shop: 'biketrek', options } } });
  assert.equal(calls.filter(call => call.path === '/api/product-imports' && call.method === 'POST').length, 1, 'StrictMode reuses the idempotent create request');
  assert.equal(document.querySelectorAll('tr[data-row]').length, 3);
  assert.ok(document.body.textContent.includes('Rodina duší'));
  assert.equal(button('Náhľad odoslania do e-shopu').disabled, true);
  assert.equal(calls.some(call => call.path.endsWith('/save') || call.path.endsWith('/publish')), false, 'Opening a draft performs no Hub registration or shop publication');
  await click(field(1, 'brand').querySelector('button'));
  await change(label('Značka'), 'Edited'); await key(label('Značka'), 'Enter');
  await change(label('Hľadať v tejto dávke'), 'Duša 1');
  await paste(field(1, 'brand'), 'Test');
  await change(label('Hľadať v tejto dávke'), '');
  assert.equal(button('Uložiť koncept (0)').disabled, true, 'Visible revert clears shared patches on hidden family rows');
  assert.ok(field(2, 'brand').textContent.includes('Test'));
  await click(button('Popisy a SEO'));
  await click(field(1, 'description_html').querySelector('button'));
  await change(label('Dlhý popis (HTML)'), '<p>Only inside the open dialog</p>');
  assert.equal(button('Uložiť koncept (0)').disabled, true, 'Modal text has not yet changed parent table patches');
  const modalUnload = new window.Event('beforeunload', { cancelable: true }); window.dispatchEvent(modalUnload);
  assert.equal(modalUnload.defaultPrevented, true, 'Modal-only text is protected from a hard reload');
  await click(button('Zrušiť'));
  const cleanUnload = new window.Event('beforeunload', { cancelable: true }); window.dispatchEvent(cleanUnload);
  assert.equal(cleanUnload.defaultPrevented, false, 'Discarding modal-only text restores a clean navigation state');
  await click(button('Prehľad'));
  await paste(field(1, 'name'), 'Ručný názov 1\nRučný názov 2');
  assert.ok(field(1, 'name').textContent.includes('Ručný názov 1'));
  assert.ok(field(2, 'name').textContent.includes('Ručný názov 2'));
  assert.equal(calls.some(call => call.method === 'PUT'), false, 'Typing/paste remain local until explicit save');
  let leavePrompts = 0; window.confirm = () => { leavePrompts++; return false; };
  await click(button('Späť do katalógu'));
  assert.equal(leavePrompts, 1); assert.ok(field(1, 'name'), 'Cancelled SPA navigation preserves the draft');
  window.confirm = () => true;
  failEdit = true;
  await click(button('Uložiť koncept (2)'));
  assert.ok(document.querySelector('[role="alert"]'));
  assert.ok(field(1, 'name').textContent.includes('Ručný názov 1'), 'CAS failure preserves pending edits');
  failEdit = false;
  await click(button('Uložiť koncept (2)'));
  assert.equal(server.rows[0].values.name, 'Ručný názov 1');
  assert.equal(server.status, 'draft');
  assert.equal(calls.some(call => call.path.endsWith('/publish')), false);

  await click(label('Označiť PL-3'));
  await change(label('Pole hromadnej úpravy'), 'sale_gross');
  await change(label('Spoločná hodnota'), '12,50');
  await click(button('Použiť na označené'));
  await click(button('Ceny'));
  assert.ok(field(1, 'sale_gross').textContent.includes('12.50'));
  assert.ok(field(2, 'sale_gross').textContent.includes('12.50'));
  assert.equal(field(3, 'sale_gross').textContent.includes('12.50'), false, 'Bulk edits affect selected rows only');
  await click(button('Popisy a SEO'));
  await click(field(1, 'description_html').querySelector('button'));
  await change(label('Dlhý popis (HTML)'), '<table class="info"><tr><td>Ručný popis</td></tr></table>');
  const unload = new window.Event('beforeunload', { cancelable: true }); window.dispatchEvent(unload);
  assert.equal(unload.defaultPrevented, true, 'Expanded editor text is protected before applying it to the table');
  await click(button('Použiť zmenu'));
  assert.ok(field(2, 'description_html').textContent.includes('Ručný popis'), 'Shared family fields propagate to selected family variants');
  assert.equal(document.querySelector('.info'), null, 'Edited HTML is text, never executed in the table');
  await click(button('Zapnúť AI'));
  assert.equal(label('Vylepšiť pomocou AI: PL-1').checked, true);
  assert.equal(label('Vylepšiť pomocou AI: PL-2').checked, true);
  assert.equal(label('Vylepšiť pomocou AI: PL-3').checked, false);
  await click(button('Pripraviť odhad (2)'));
  assert.equal(calls.some(call => call.path.endsWith('/ai-start')), false, 'Estimate is separate from paid AI');
  await click(button('Vypnúť AI'));
  assert.equal(button('Spustiť AI · odhad 0.040 USD'), undefined, 'Disabled families are excluded from actionable costs');
  await click(button('Zapnúť AI'));
  await click(button('Spustiť AI · odhad 0.040 USD'));
  const polling = [...pollers.values()].at(-1);
  assert.ok(polling, 'Running AI registers a progress poll');
  deferRead = true; const inFlight = polling();
  await click(field(1, 'name').querySelector('button'));
  await change(label('Názov'), 'Ručný názov počas AI');
  const newer = clone(server); newer.revision += 10; newer.rows[0].values.name = 'Remote overwrite';
  await act(async () => { releaseRead(newer); await inFlight; });
  assert.equal(label('Názov').value, 'Ručný názov počas AI');
  assert.equal(document.body.textContent.includes('Remote overwrite'), false, 'An in-flight poll cannot replace data after inline editing begins');
  const beforePoll = calls.length; await polling();
  assert.equal(calls.length, beforePoll, 'Polling pauses while an inline cell is open');
  await key(label('Názov'), 'Enter');
  server.rows.filter(row => row.ai_job).forEach(row => row.ai_job.status = 'review');
  await click(button('Uložiť koncept (1)'));
  await click(button('Uložiť produkty do Hubu'));
  assert.equal(button('Náhľad odoslania do e-shopu').disabled,true,'Selected but unapplied AI cannot accidentally publish feed-only content');
  assert(document.querySelector('a[href="/ai-content?job=job-family-1"]'),'Review is reachable for the specific AI family');
  await click(button('Prijať výsledky AI do tabuľky'));
  assert.equal(server.rows[0].values.name, 'Ručný názov počas AI', 'Manual values survive accepted AI output');
  assert.equal(button('Náhľad odoslania do e-shopu').disabled, true, 'AI changes require local Hub save');
  await click(button('Uložiť produkty do Hubu'));
  assert.equal(server.status, 'saved');
  assert.equal(calls.some(call => call.path.endsWith('/publish')), false, 'Saving to Hub never writes the shop');
  await click(button('Náhľad odoslania do e-shopu'));
  assert.equal(calls.some(call => call.path.endsWith('/publish')), false, 'Remote preview is read-only');
  await click(button('Odoslať 1 produktov do biketrek'));
  assert.equal(calls.filter(call => call.path.endsWith('/publish')).length, 1);
  assert.ok(document.body.textContent.includes('Vytvorené'));
  assert.ok(document.body.textContent.includes('Import do e-shopu dokončený'));
  assert.equal(button('Náhľad odoslania do e-shopu'),undefined,'Completed publication no longer offers a fresh preview');
  assert.equal(button('Uložiť produkty do Hubu'),undefined,'A completed create-only preparation cannot imply it updates shop products');
  assert.equal(label('Vylepšiť pomocou AI: PL-1').disabled,true);
  assert(!button('Obnoviť').disabled,'The final result can still be refreshed');
  assert(!document.body.textContent.includes('Až nasledujúce potvrdenie vytvorí produkty'),'Result copy replaces preview instructions');

  // Older imports may have been published while approved AI still awaited table acceptance.
  server.rows.filter(row => row.ai_job).forEach(row => row.ai_job.status = 'ready');
  await click(button('Obnoviť'));
  assert(document.body.textContent.includes('AI výsledky zostali neprevzaté'));
  assert.equal(button('Prijať výsledky AI do tabuľky'),undefined,'Applying AI cannot erase an already completed delivery record');

  await render('/product-import');
  assert.ok(document.querySelector('a[href="/product-import?draft=test-draft"]'), 'Saved drafts are discoverable without route state');
  await click(document.querySelector('a[href="/product-import?draft=test-draft"]'));
  assert.equal(document.querySelectorAll('tr[data-row]').length, 3, 'Draft resumes from server via durable URL');
  assert.equal(calls.filter(call => call.path === '/api/product-imports' && call.method === 'POST').length, 1);
  await act(async () => root.unmount());
  console.log('Product import UI passed: durable drafts, CAS preservation, family propagation, multiline TSV, selected bulk changes, safe HTML display, mixed AI estimates/results, explicit Hub save and shop publication.');
})().catch(error => { console.error(error); process.exitCode = 1; root.unmount(); });
