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
const values = id => ({ code: `PL-${id}`, supplier_code: String(id), group_name: id < 3 ? 'Rodina duší' : '', name: `Duša ${id}`, brand: 'Test', manufacturer_code: '', eans: [`0000000${id}`], images: ['https://images.example.test/item.jpg'], description_html: '<p>Feed</p>', short_description: 'Z feedu', seo_title: `Duša ${id}`, seo_description: 'SEO z feedu', seo_url: '', category_code: 'TUBES', parameters: [{ name: 'Ventil', value: 'AV' }], variant_attributes: id < 3 ? [{ name: 'Veľkosť', value: String(id) }] : [], metadata: {}, purchase_net: '5', retail_gross: '9', sale_gross: '10', vat_percent: '23', currency: 'EUR', availability: 'do 5 dní', ai_enabled: false, ai_category_profile: 'auto', target_shops: ['biketrek'], target_category_codes: {} });
let server = { id: 'test-draft', revision: 1, status: 'draft', supplier: 'paul-lange', shop: 'biketrek', feed_key: 'products', options, categories, default_shop: 'biketrek', shops: [{code:'biketrek',name:'BIKETREK',categories},{code:'xtrek',name:'xTrek',categories:[{code:'XTOOLS',names:{sk:'Výbava xTrek'}}]}], rows: [1, 2, 3].map(id => ({ id, group_key: id < 3 ? 'family-1' : `single-${id}`, is_variant: id < 3, source_category: 'Duše dodávateľ', mapping_revision: 2, values: values(id), manual_fields: [], provenance: { category_code: 'mapping', seo_title: 'derived' }, warnings: [], errors: [], hub_product_id: null, ai_job: null })), publication: null, publication_result: null };
const calls = []; let failEdit = false, deferRead = false, releaseRead, failSourceRetry = false, failSourceSkip = false;
global.fetch = async (path, init = {}) => {
  const body = init.body ? JSON.parse(init.body) : undefined;
  calls.push({ path, method: init.method || 'GET', body });
  if (path === '/api/sku-policy/brands') return {ok:true,status:200,json:async()=>({brands:[{id:1,code:'TP',name:'Topeak',kind:'brand',aliases:[]}]})};
  if (path === '/api/sku-policy/preview') {
    const item=body.sku_identity;
    return {ok:true,status:200,json:async()=>({status:item?.brand&&item.source_code?'new':'blocked',sku:item?'TP-'+item.source_code:null,product_id:null,brand_id:1,reasons:[],candidate_product_ids:[]})};
  }
  if (path === '/api/ai-content/rules') return { ok: true, status: 200, json: async () => ({book:{categories:[{id:'general',name:'Všeobecné'},{id:'inner_tubes',name:'Duše'}]}}) };
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
    for (const patch of body.rows) { const row = server.rows.find(item => item.id === patch.id); Object.assign(row.values, patch.values); if (patch.values.sku_identity) row.values.code='TP-'+patch.values.sku_identity.source_code; Object.keys(patch.values).forEach(field => { row.manual_fields.push(field); row.provenance[field] = 'manual'; }); }
    server.revision++; server.status = 'draft'; server.publication = null; server.publication_result = null;
  } else if (path.endsWith('/categories-refresh')) {
    assert.equal(body.expected_revision, server.revision);
    server.revision++;
    server.categories.push({code:'SADDLES',parent_code:'PARTS',active:false,names:{sk:'Sedlá a návleky na sedlá'}},{code:'COVERS',parent_code:'SADDLES',active:false,names:{sk:'Návleky na sedlá'}});
  } else if (path.endsWith('/source-retry')) {
    assert.deepEqual(body, { expected_revision: server.revision });
    server.revision++; server.status = 'source_pending'; server.source_lookup = { ...server.source_lookup, state: 'queued', error: null, retryable: false, skippable: false };
    if (failSourceRetry) throw new TypeError('Synthetic lost source-retry response');
  } else if (path.endsWith('/source-skip')) {
    if (failSourceSkip === 'conflict') return { ok: false, status: 409, json: async () => ({ detail: { code: 'import_draft_changed' } }) };
    assert.deepEqual(body, { expected_revision: server.revision });
    assert.equal(server.source_lookup.skippable, true);
    server.revision++; server.status = 'draft';
    server.rows.filter(row => row.source_status !== 'completed').forEach(row => { row.source_status = 'skipped'; row.warnings.push('sloger_b2b_feed_fallback'); });
    server.source_lookup = { ...server.source_lookup, state: 'skipped', failed: 0, skipped: server.rows.filter(row => row.source_status === 'skipped').length, retryable: false, skippable: false };
    if (failSourceSkip === 'lost') throw new TypeError('Synthetic lost source-skip response');
  } else if (path.endsWith('/save')) { assert.equal(body.expected_revision, server.revision); server.revision++; server.status = 'saved'; server.rows.forEach(row => row.hub_product_id = row.id + 100); }
  else if (path.endsWith('/ai')) { assert.equal(body.research, 'official'); server.revision++; server.rows.filter(row => row.values.ai_enabled).forEach(row => row.ai_job = { id: `job-${row.group_key}`, status: 'estimate', estimate_usd: '0.04', actual_usd: null, checks: {} }); }
  else if (path.endsWith('/ai-start')) { server.revision++; server.rows.filter(row => row.ai_job).forEach(row => { row.ai_job.status = 'generating'; row.ai_job.actual_usd = '0.03'; }); }
  else if (path.endsWith('/ai-apply')) { server.revision++; server.status = 'draft'; server.rows.filter(row => row.ai_job).forEach(row => { if (!row.manual_fields.includes('name')) row.values.name = 'AI názov'; if (!row.manual_fields.includes('description_html')) { row.values.description_html = '<p>Vylepšené AI</p>'; row.provenance.description_html = 'ai'; } row.ai_job.status = 'completed'; }); }
  else if (path.endsWith('/publish-preview')) { assert.equal(server.status, 'saved'); server.revision++; server.publication_result = null; server.publication_preview_retryable = false; server.publication = { preview_id: 'preview-1', options, errors: [], warnings: [], expires_at: new Date(Date.now() + 60000).toISOString(), items: ['biketrek','xtrek'].map(shop => ({ shop, code: 'PL-G', name: 'Rodina duší', product_ids: [1, 2], variants_count: 2, status: 'ready', warnings: [], errors: [], payload: { code: 'PL-G', active_yn: false } })) }; }
  else if (path.endsWith('/publish')) { assert.equal(body.preview_id, server.publication.preview_id); server.revision++; server.publication_result = { ...server.publication, status: 'completed', items: server.publication.items.map(item => ({ ...item, status: item.status === 'exists' ? 'exists' : 'created' })) }; server.rows.forEach(row => row.publication_links = row.values.target_shops.map(shop => ({shop,status:'created',shop_admin_url:`https://${shop}.example.test/admin/${row.id}`,shop_url:`https://${shop}.example.test/product/${row.id}`}))); }
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
  const router = createMemoryRouter([{ path: '/product-import', element: React.createElement(ProductImportPage) }, { path: '/suppliers/:supplier/catalog', element: React.createElement('p', null, 'Catalog') }, { path: '/receiving/:invoiceId', element: React.createElement('p', { 'data-testid': 'return-receiving' }, 'Invoice receipt') }], { initialEntries: [entry] });
  await act(async () => root.render(React.createElement(React.StrictMode, null, React.createElement(RouterProvider, { key: typeof entry === 'string' ? entry : 'new', router }))));
  return router;
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
  // Preview and photos work before a Hub card exists, including in the category editor.
  await click(label('Náhľad produktu: PL-1'));
  assert.ok(document.querySelector('[role="dialog"]').textContent.includes('Duša 1'));
  await click(label('Zväčšiť fotografiu'));
  assert.ok(document.querySelector('.import-product-large-image'));
  await key(document.querySelector('[role="dialog"]'), 'Escape');
  assert.equal(document.querySelector('[role="dialog"]'), null);
  await click(button('Načítať kategórie z e-shopov'));
  assert.equal(calls.filter(call => call.path.endsWith('/categories-refresh')).length, 1);
  for (const category of ['SADDLES', 'COVERS', 'TUBES']) {
    await click(field(1, 'category_code').querySelector('button'));
    assert.ok(document.querySelector('[role="dialog"] .import-product-summary'));
    await click(label('Zväčšiť fotografiu'));
    assert.ok(document.querySelector('[role="dialog"] .import-product-large-image'));
    await change(label(i18n.t('ai.searchCategories')), category);
    const choices = [...document.querySelectorAll('.category-tree-list button')];
    const choice = choices.find(element => element.textContent.includes(category));
    assert.ok(choice, 'Inactive parent and leaf must both be selectable');
    await click(choice);
    await click(button('Použiť zmenu'));
    assert.equal(document.querySelector('[role="dialog"]'), null);
  }
  assert.ok(field(1, 'category_code').textContent.includes('Duše'));
  assert.equal(label('Import do BIKETREK: PL-1').checked, true);
  assert.equal(label('Import do xTrek: PL-1').checked, false);
  const beforeTargets = calls.length;
  await click(label('Import do xTrek: PL-1'));
  assert.equal(label('Import do xTrek: PL-2').checked, true, 'Target shop choices propagate through the product family');
  await change(label('Kategória xTrek: PL-1'), 'XTOOLS');
  assert.equal(label('Kategória xTrek: PL-2').value, 'XTOOLS', 'Each alternate shop has its own family category');
  await click(label('Import do BIKETREK: PL-3'));
  assert.equal(label('Import do xTrek: PL-3').checked, false, 'Both destinations may stay unselected for a Hub-only row');
  assert.equal(calls.length, beforeTargets, 'Destination selection performs no writes or paid AI requests');
  assert.equal(document.querySelector('.import-ai-panel'), null);
  assert.equal(button('Pripraviť AI obsah (2)'), undefined);
  assert.equal(document.body.textContent.includes('Iba feed'), false);
  await click(button('Uložiť koncept (3)'));
  assert.deepEqual(server.rows[0].values.target_shops, ['biketrek', 'xtrek']);
  assert.deepEqual(server.rows[2].values.target_shops, []);
  await change(label('Pravidlá AI pre PL-1'),'inner_tubes');
  assert.equal(label('Pravidlá AI pre PL-2').value,'inner_tubes','Explicit AI rule profile is shared by sibling variants');
  assert.equal(label('Pravidlá AI pre PL-3').value,'auto');
  await change(label('Pravidlá AI pre PL-1'),'auto');
  await change(label('Pole hromadnej úpravy'),'ai_category_profile');
  await change(label('Spoločná hodnota'),'inner_tubes');
  await click(button('Použiť na označené'));
  assert.equal(label('Pravidlá AI pre PL-3').value,'inner_tubes','Bulk profile selection reaches all selected families');
  await change(label('Spoločná hodnota'),'auto');
  await click(button('Použiť na označené'));
  assert.equal(button('Uložiť koncept (0)').disabled,true,'Returning profiles to automatic selection clears pending edits');
  assert.equal(button('Pripraviť import').disabled, false);
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
  assert.equal(server.rows[0].values.name, 'Duša 1', 'Typing/paste remain local until explicit save');
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
  await change(label('Pravidlá AI pre PL-1'),'inner_tubes');
  await click(button('Uložiť koncept (2)'));
  assert.deepEqual(server.rows.map(row=>row.values.ai_category_profile),['inner_tubes','inner_tubes','auto'],'A saved explicit profile persists per family without changing unrelated products');
  assert(server.rows.every(row=>row.values.category_code === 'TUBES'),'Choosing a rule profile leaves shop category placement intact');
  assert.equal(server.status, 'draft');
  assert.equal(calls.some(call => call.path.endsWith('/publish')), false);

  await click(label('Označiť PL-3'));
  await change(label('Pole hromadnej úpravy'), 'sale_gross');
  await change(label('Spoločná hodnota'), '12,50');
  await change(label(i18n.t('productImport.search')), 'PL-1');
  assert(document.body.textContent.includes('mimo nej: 1'), 'Filtered-out selection remains explicitly counted');
  assert.equal(label(i18n.t('productImport.bulkScope')).value, 'visible', 'Bulk edits default to selected rows on the visible page');
  await click(button('Použiť na označené'));
  await change(label(i18n.t('productImport.search')), '');
  await click(button('Ceny'));
  assert(!field(2, 'sale_gross').textContent.includes('12.50'), 'Default visible scope leaves the hidden selected row untouched');
  await change(label(i18n.t('productImport.bulkScope')), 'all');
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
  assert.equal(calls.some(call => call.path.endsWith('/ai') || call.path.endsWith('/ai-start')), false, 'AI checkbox and bulk selection do not charge or prepare until the primary action');
  await click(button('Spracovať podľa pravidiel'));
  assert.equal(calls.some(call => call.path.endsWith('/ai-start')), false, 'Estimate is separate from paid AI');
  await change(label('Pravidlá AI pre PL-1'),'general');
  assert.equal(button('Spustiť AI · odhad 0.040 USD'),undefined,'An edited profile cannot start a stale cost estimate');
  await change(label('Pravidlá AI pre PL-1'),'inner_tubes');
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
  assert.equal(calls.some(call => call.path.endsWith('/publish-preview')), false, 'Hub save cannot accidentally preview feed-only content while AI awaits acceptance');
  assert(document.querySelector('a[href="/ai-content?job=job-family-1"]'), 'Review is reachable for the specific AI family');
  await click(button('Prijať výsledky AI a pokračovať'));
  assert.equal(server.rows[0].values.name, 'Ručný názov počas AI', 'Manual values survive accepted AI output');
  assert.equal(server.status, 'saved', 'The primary action saves accepted results into Hub before preview');
  assert(server.publication, 'The primary action continues into a read-only shop preview');
  assert.equal(calls.some(call => call.path.endsWith('/publish')), false, 'Preparation never writes the shop before confirmation');
  await change(label('Pravidlá AI pre PL-1'),'general');
  assert(button('Odoslať 2 produktov do BIKETREK + xTrek').disabled, 'A changed AI profile blocks publication of an old preview');
  assert(document.body.textContent.includes('Pravidlá zmenené – priprav AI obsah znova'));
  await change(label('Pravidlá AI pre PL-1'),'inner_tubes');
  assert(!button('Odoslať 2 produktov do BIKETREK + xTrek').disabled);
  server.rows.filter(row=>row.ai_job).forEach(row=>row.ai_job.staging={ai_applied:false});
  await click(button('Obnoviť'));
  assert(button('Odoslať 2 produktov do BIKETREK + xTrek').disabled, 'A persisted profile mismatch blocks publication despite a historical completed job');
  assert(document.body.textContent.includes('Pravidlá zmenené – priprav AI obsah znova'));
  server.rows.filter(row=>row.ai_job).forEach(row=>row.ai_job.staging={ai_applied:true});
  await click(button('Obnoviť'));
  await click(button('Odoslať 2 produktov do BIKETREK + xTrek'));
  assert.equal(calls.filter(call => call.path.endsWith('/publish')).length, 1);
  assert.ok(document.body.textContent.includes('Vytvorené'));
  assert.ok(document.body.textContent.includes('Import do e-shopu dokončený'));
  assert.equal(button('Pripraviť import'), undefined, 'Completed publication no longer offers a fresh preview');
  assert.equal(button('Uložiť produkty do Hubu'), undefined, 'A completed create-only preparation cannot imply it updates shop products');
  assert.equal(label('Vylepšiť pomocou AI: PL-1').disabled,true);
  for (const shop of ['biketrek', 'xtrek']) {
    assert(document.querySelector(`a[href="https://${shop}.example.test/admin/1"]`), 'Completed rows expose each shop administration URL');
    assert(document.querySelector(`a[href="https://${shop}.example.test/product/1"]`), 'Completed rows expose each storefront URL');
  }
  assert(!button('Obnoviť').disabled, 'The final result can still be refreshed');
  assert(!document.body.textContent.includes('Až nasledujúce potvrdenie vytvorí produkty'), 'Result copy replaces preview instructions');

  const previewCallsBeforeRecovery = calls.filter(call => call.path.endsWith('/publish-preview')).length;
  server.publication_result.status = 'failed'; server.publication_result.items[1].status = 'failed';
  await click(button('Obnoviť'));
  assert(label('Import do xTrek: PL-1').disabled, 'A partial delivery locks both destination choices');
  assert.equal(button('Pripraviť import'), undefined, 'A partially delivered batch cannot start a fresh preview');
  await click(button('Overiť výsledok / zopakovať neodoslané'));
  assert.equal(calls.filter(call => call.path.endsWith('/publish-preview')).length, previewCallsBeforeRecovery);
  assert.equal(calls.filter(call => call.path.endsWith('/publish')).at(-1).body.retry_failed, true, 'Partial recovery uses the original idempotent publication request');

  // Older imports may have been published while approved AI still awaited table acceptance.
  server.rows.filter(row => row.ai_job).forEach(row => row.ai_job.status = 'ready');
  await click(button('Obnoviť'));
  assert(document.body.textContent.includes('AI výsledky zostali neprevzaté'));
  assert.equal(button('Prijať výsledky AI a pokračovať'),undefined,'Applying AI cannot erase an already completed delivery record');

  const deliveredResult = clone(server.publication_result);
  server.publication_result = null;
  server.rows.filter(row=>row.ai_job).forEach(row=>{ row.ai_job.status='import_queued'; row.ai_job.staging={ai_applied:true,profile_matches:true,auto_apply:true,auto_publish:true}; });
  await click(button('Obnoviť'));
  assert(label('Vylepšiť pomocou AI: PL-1').disabled,'Automatic publication keeps editing locked while the queued import is tracked');
  assert(field(1,'name').querySelector('button').disabled,'Automatic publication locks table edits until delivery settles');
  const automaticPoll = [...pollers.values()].at(-1);
  assert(automaticPoll,'An automatically queued import continues polling even after content generation finished');
  server.publication_result = deliveredResult;
  server.rows.filter(row=>row.ai_job).forEach(row=>row.ai_job.status='completed');
  await act(async()=>automaticPoll());
  assert(document.body.textContent.includes('Import do e-shopu dokončený'));
  assert(!button('Pripraviť import'),'Completed automatic publication does not offer another preview');

  await render('/product-import');
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)); });
  await click(button('Všetky'));
  assert.ok(document.querySelector('a[href="/product-import?draft=test-draft"]'), 'Saved drafts are discoverable without route state');
  await click(document.querySelector('a[href="/product-import?draft=test-draft"]'));
  assert.equal(document.querySelectorAll('tr[data-row]').length, 3, 'Draft resumes from server via durable URL');
  assert.equal(calls.filter(call => call.path === '/api/product-imports' && call.method === 'POST').length, 1);
  server = {...server, revision: server.revision + 1, status: 'draft', publication: null, publication_result: null, rows: [{...clone(initial[2]), values: {...values(3), target_shops: []}, ai_job: null}]};
  await render('/product-import?draft=test-draft&case=hub-only');
  const writesBeforeHubOnly = calls.filter(call => call.path.endsWith('/publish-preview') || call.path.endsWith('/publish')).length;
  await click(button('Pripraviť a uložiť do Hubu'));
  assert.equal(server.status, 'saved');
  assert.equal(calls.filter(call => call.path.endsWith('/publish-preview') || call.path.endsWith('/publish')).length, writesBeforeHubOnly, 'A Hub-only selection saves locally without preparing or sending any shop write');
  server.shops[1].error = 'shop_not_configured';
  await render('/product-import?draft=test-draft&case=unavailable');
  assert(label('Import do xTrek: PL-3').disabled, 'An unavailable shop cannot be newly selected');
  assert(document.body.textContent.includes('E-shop nie je pripravený'));
  server.rows[0].values.target_shops = ['xtrek'];
  await click(button('Obnoviť'));
  assert(!label('Import do xTrek: PL-3').disabled, 'An already selected unavailable destination may still be cleared for Hub-only saving');
  await click(label('Import do xTrek: PL-3'));
  await click(button('Uložiť koncept (1)'));
  assert.deepEqual(server.rows[0].values.target_shops, []);
  delete server.shops[1].error;
  server.rows = [{...clone(initial[0]), values: {...values(1), target_shops: ['biketrek']}}];
  server.status = 'saved';
  server.publication = {preview_id:'already-existing',options,errors:[],warnings:[],expires_at:new Date(Date.now()+60000).toISOString(),items:[{shop:'biketrek',code:'PL-1',name:'Existing product',product_ids:[1],variants_count:0,status:'exists',warnings:[],errors:[],payload:{}}]};
  await render('/product-import?draft=test-draft&case=all-existing');
  assert(!button('Dokončiť prípravu - produkty už existujú').disabled, 'An all-existing preview has a terminal confirmation instead of a permanently disabled zero-create button');
  await click(button('Dokončiť prípravu - produkty už existujú'));
  assert(document.body.textContent.includes('Import do e-shopu dokončený'));
  assert.equal(server.publication_result.items[0].status, 'exists', 'No-op completion retains the already-existing outcome');
  assert(document.querySelector('a[href="https://biketrek.example.test/admin/1"]'));

  server.status = 'saved';
  server.publication_result = { ...server.publication_result, status: 'failed', errors: ['import_source_availability_expired'], items: server.publication_result.items.map(item => ({ ...item, status: 'failed', errors: ['import_source_availability_expired'] })) };
  server.publication_preview_retryable = true;
  await render('/product-import?draft=test-draft&case=expired-with-no-writes');
  const freshPreview = () => document.querySelector('[data-testid="import-fresh-preview"]');
  assert(freshPreview() && !freshPreview().disabled, 'Server proof of zero attempted product writes permits a fresh preview');
  assert(field(1, 'name').querySelector('button').disabled && label('Vylepšiť pomocou AI: PL-1').disabled, 'Expiry recovery does not reopen editing or AI');
  assert.equal(button('Uložiť produkty do Hubu'), undefined, 'Expiry recovery does not save the product again');
  const beforeFreshPreview = calls.length;
  await act(async () => { freshPreview().click(); freshPreview().click(); });
  const freshCalls = calls.slice(beforeFreshPreview).filter(call => call.method !== 'GET');
  assert.deepEqual(freshCalls.map(call => call.path), ['/api/product-imports/test-draft/publish-preview'], 'Fresh preview is explicit and invokes no save, AI or product publication');
  assert.equal(server.publication_result, null); assert.equal(freshPreview(), null);
  server.publication_result = { ...server.publication, status: 'failed', items: server.publication.items.map((item, index) => ({ ...item, status: index ? 'uncertain' : 'created' })) };
  server.publication_preview_retryable = false;
  await render('/product-import?draft=test-draft&case=mixed-outcome');
  assert.equal(freshPreview(), null, 'A mixed or uncertain result never offers a replacement preview');
  assert(document.body.textContent.includes(i18n.t('productImport.recover')), 'Ordinary recovery remains available for existing publication journals');

  server = { ...server, revision: server.revision + 1, supplier: 'sloger', status: 'source_pending', publication: null, publication_result: null,
    rows: [{ ...clone(initial[0]), values: { ...values(1), code: 'SLG-1' }, source_status: 'pending', source_error: null }],
    source_lookup: { state: 'queued', total: 1, completed: 0, failed: 0, skipped: 0, error: null, retryable: false, skippable: false } };
  await render('/product-import?draft=test-draft&case=source-pending');
  const sourceControl = id => document.querySelector(`[data-testid="${id}"]`);
  const sourceWrites = () => calls.filter(call => call.method !== 'GET');
  const beforeSourceWrites = sourceWrites().length;
  assert(sourceControl('import-source-status'));
  assert(button('Pripraviť import').disabled && button('Uložiť produkty do Hubu').disabled);
  assert(label('Vylepšiť pomocou AI: SLG-1').disabled && label('Import do BIKETREK: SLG-1').disabled);
  assert(label('Pravidlá AI pre SLG-1').disabled);
  assert(field(1, 'name').querySelector('button').disabled);
  await key(field(1, 'name'), 'F2'); await paste(field(1, 'name'), 'Must not edit');
  assert(!field(1, 'name').querySelector('input'), 'Pending B2B facts block keyboard and paste editing');
  assert.equal(sourceWrites().length, beforeSourceWrites);
  assert.equal(sourceControl('import-source-retry'), null);
  assert.equal(sourceControl('import-source-skip'), null, 'Queued fetching cannot be bypassed');
  server.status = 'source_running'; server.source_lookup.state = 'running';
  await act(async () => { for (const poll of [...pollers.values()]) await poll(); });
  assert.equal(sourceWrites().length, beforeSourceWrites, 'Source progress polling only reads the existing draft');
  assert(sourceControl('import-source-status').textContent.includes('Načítavajú sa'));
  assert.equal(sourceControl('import-source-skip'), null, 'Running fetching cannot be bypassed');

  server.status = 'source_failed'; server.source_lookup = { ...server.source_lookup, state: 'failed', failed: 1, error: 'sloger_credentials_not_configured', retryable: true, skippable: true };
  server.rows[0].source_status = 'failed'; server.rows[0].source_error = 'sloger_credentials_not_configured';
  await click(sourceControl('import-source-refresh'));
  assert(button('Pripraviť import').disabled && field(1, 'name').querySelector('button').disabled);
  assert(document.querySelector('a[href="/suppliers?edit=sloger"]'), 'Missing B2B login links to supplier settings');
  assert(!sourceControl('import-source-skip').disabled, 'Failed B2B facts offer an explicit feed fallback');
  failSourceRetry = true; await click(sourceControl('import-source-retry'));
  assert(sourceControl('import-source-retry').disabled, 'Unconfirmed retry cannot be blindly repeated');
  assert(sourceControl('import-source-skip').disabled, 'An uncertain retry also blocks a conflicting skip until GET recovery');
  assert.equal(sourceWrites().length, beforeSourceWrites + 1);
  await click(sourceControl('import-source-refresh'));
  assert.equal(sourceControl('import-source-retry'), null, 'GET recovery observes the already queued source request');
  assert.equal(sourceWrites().length, beforeSourceWrites + 1);

  failSourceRetry = false; server.status = 'source_failed'; server.source_lookup = { ...server.source_lookup, state: 'failed', retryable: true };
  await click(sourceControl('import-source-refresh'));
  await act(async () => { sourceControl('import-source-retry').click(); sourceControl('import-source-retry').click(); });
  assert.equal(sourceWrites().length, beforeSourceWrites + 2, 'Rapid retry clicks enqueue only once');
  server.status = 'draft'; server.source_lookup = { ...server.source_lookup, state: 'completed', completed: 1, failed: 0, error: null, retryable: false };
  server.rows[0].source_status = 'completed'; server.rows[0].source_error = null; server.rows[0].provenance.eans = 'supplier';
  await act(async () => { for (const poll of [...pollers.values()]) await poll(); });
  assert.equal(sourceControl('import-source-status'), null);
  assert(!button('Pripraviť import').disabled && !field(1, 'name').querySelector('button').disabled);
  assert(field(1, 'eans').textContent.includes('B2B dodávateľa'));
  assert.equal(sourceWrites().length, beforeSourceWrites + 2, 'Completed B2B facts never automatically invoke AI or import');
  const beforeNoAi = calls.filter(call => /\/ai(?:-|$)/.test(call.path) && call.method !== 'GET').length;
  await click(button('Pripraviť import'));
  assert.equal(calls.filter(call => /\/ai(?:-|$)/.test(call.path) && call.method !== 'GET').length, beforeNoAi, 'Ordinary import with AI unchecked uses fetched supplier facts without an AI call');

  // A deliberate fallback unlocks the ordinary flow while preserving partial B2B facts.
  const failedSource = () => ({ ...server, revision: server.revision + 1, status: 'source_failed', publication: null, publication_result: null,
    rows: [1, 2].map(id => ({ ...clone(initial[id - 1]), values: { ...values(id), code: `SLG-${id}`, purchase_net: id === 1 ? '67.08' : null, availability: id === 1 ? 'do 2-4 dní' : 'overíme' }, source_status: id === 1 ? 'completed' : 'failed', source_error: id === 1 ? null : 'sloger_b2b_unavailable' })),
    source_lookup: { state: 'failed', total: 2, completed: 1, failed: 1, skipped: 0, error: 'sloger_b2b_unavailable', retryable: true, skippable: true } });
  server = failedSource();
  await render('/product-import?draft=test-draft&case=feed-fallback');
  assert.equal(sourceControl('import-source-skip').textContent, 'Pokračovať iba s údajmi z feedu');
  assert(sourceControl('import-source-status').textContent.includes('Nákupná cena a dostupnosť môžu zostať neznáme'));
  const beforeSkipWrites = sourceWrites().length;
  const completedFacts = clone(server.rows[0]);
  const failedFacts = clone(server.rows[1].values);
  await act(async () => { sourceControl('import-source-skip').click(); sourceControl('import-source-skip').click(); });
  assert.deepEqual(sourceWrites().slice(beforeSkipWrites).map(call => call.path), ['/api/product-imports/test-draft/source-skip'], 'Rapid fallback clicks perform one CAS update without AI, Hub save or shop publication');
  assert.equal(sourceControl('import-source-skip'), null);
  assert.equal(sourceControl('import-source-retry'), null);
  assert(sourceControl('import-source-status').textContent.includes('bez B2B: 1'));
  assert(document.querySelector('tr[data-row="2"] .import-row-state').textContent.includes('Iba feed: B2B preskočené'));
  assert(document.querySelector('tr[data-row="2"] .import-row-state details').textContent.includes('Údaje zo Sloger B2B sa nepodarilo načítať.'), 'Original error remains available as an explanation');
  assert(document.querySelector('tr[data-row="2"] .import-row-state').textContent.includes(i18n.t('productImport.errors.sloger_b2b_feed_fallback')));
  assert.equal(document.querySelector('tr[data-row="2"] .import-row-state .import-error'), null, 'Skipped source errors are explanatory, not active validation failures');
  assert.deepEqual(server.rows[0], completedFacts);
  assert.deepEqual(server.rows[1].values, failedFacts, 'Fallback does not invent purchase or availability values');
  assert(!button('Pripraviť import').disabled && !button('Uložiť produkty do Hubu').disabled && !field(2, 'name').querySelector('button').disabled);
  assert(!label('Vylepšiť pomocou AI: SLG-2').disabled && !label('Import do xTrek: SLG-2').disabled);
  await click(button('Pripraviť import'));
  assert.deepEqual(sourceWrites().slice(beforeSkipWrites).map(call => call.path), ['/api/product-imports/test-draft/source-skip', '/api/product-imports/test-draft/save', '/api/product-imports/test-draft/publish-preview'], 'The operator can explicitly prepare the ordinary feed-only import afterward');

  // English controls use the same conflict and uncertain-response recovery path.
  server = failedSource();
  await act(async () => i18n.changeLanguage('en'));
  await render('/product-import?draft=test-draft&case=feed-fallback-recovery');
  assert.equal(sourceControl('import-source-skip').textContent, 'Continue with feed data only');
  failSourceSkip = 'conflict';
  await click(sourceControl('import-source-skip'));
  assert(button('Prepare import').disabled && field(2, 'name').querySelector('button').disabled, 'A stale revision never unlocks the draft locally');
  assert(sourceControl('import-source-skip').disabled && sourceControl('import-source-retry').disabled);
  await click(sourceControl('import-source-refresh'));
  failSourceSkip = 'lost';
  const beforeLostSkip = sourceWrites().length;
  await click(sourceControl('import-source-skip'));
  assert(button('Prepare import').disabled, 'A lost response does not optimistically unlock the draft');
  assert(sourceControl('import-source-skip').disabled && sourceControl('import-source-retry').disabled);
  await click(sourceControl('import-source-refresh'));
  assert.equal(sourceWrites().length, beforeLostSkip + 1, 'GET recovery never blindly repeats the skip');
  assert(!button('Prepare import').disabled && !field(2, 'name').querySelector('button').disabled);
  assert(sourceControl('import-source-status').textContent.includes('without B2B: 1'));
  assert(document.querySelector('tr[data-row="2"] .import-row-state').textContent.includes('Feed only: B2B skipped'));
  assert.equal(pollers.size, 0, 'Skipped source data never resumes background polling');
  failSourceSkip = false;
  await act(async () => i18n.changeLanguage('sk'));
  server = { ...server, status: 'draft', source_lookup: null, publication: null, publication_result: null,
    receiving_origin: { kind: 'receiving', supplier: 'sk-profi-bike', session_id: 42, invoice_no: 'INV 26/7' },
    rows: server.rows.map(row => ({ ...row, ai_job: null, hub_product_id: null, expected_hub_product_id: row.id === 1 ? 500 : null, source_status: undefined, source_error: null, errors: ['missing_price', 'missing_vat'],
      values: { ...row.values, ai_enabled: false, sale_gross: null, vat_percent: null, target_shops: [] } })) };
  const receiptRouter = await render('/product-import?draft=test-draft&case=invoice-origin');
  assert(document.querySelector('[data-testid="import-receiving-help"]').textContent.includes('nákupné ceny na faktúre'), 'Receipt costs and quantities stay outside merchandising edits');
  assert(document.querySelector('[data-testid="import-back"]').textContent.includes('INV 26/7'), 'Return action identifies the source invoice');
  assert(!button('Uložiť produkty do Hubu').disabled, 'Invoice cards can be saved without invented sale price or VAT');
  await click(button('Všetky polia'));
  assert(field(1, 'supplier_code').querySelector('button').disabled && field(1, 'eans').querySelector('button').disabled, 'An existing basic card cannot offer identity edits before its first draft save');
  await click(button('Uložiť produkty do Hubu'));
  assert.equal(server.rows[0].values.sale_gross, null);
  assert.equal(server.rows[0].values.vat_percent, null);
  await click(field(2, 'name').querySelector('button'));
  assert(document.querySelector('[data-testid="import-back"]').disabled, 'Return waits for an active cell to be committed');
  await change(label('Názov'), 'Doplnený názov pred návratom');
  await key(label('Názov'), 'Enter');
  failEdit = true;
  await click(document.querySelector('[data-testid="import-back"]'));
  assert(!document.querySelector('[data-testid="return-receiving"]'), 'Failed draft persistence keeps edits in the preparation editor');
  assert(field(2, 'name').textContent.includes('Doplnený názov pred návratom'));
  failEdit = false;
  const beforeReturn = calls.length;
  await click(document.querySelector('[data-testid="import-back"]'));
  assert.equal(server.rows[1].values.name, 'Doplnený názov pred návratom', 'Return persists edits in the same durable draft');
  assert(document.querySelector('[data-testid="return-receiving"]'));
  assert.equal(receiptRouter.state.location.pathname, '/receiving/INV%2026%2F7');
  assert.equal(new URLSearchParams(receiptRouter.state.location.search).get('session'), '42');
  assert.equal(new URLSearchParams(receiptRouter.state.location.search).get('supplier'), 'sk-profi-bike');
  assert.deepEqual(calls.slice(beforeReturn).map(call => [call.method, call.path]), [['PUT', '/api/product-imports/test-draft']], 'Returning saves only the draft and performs no publication or stock operation');
  server = { ...server, status:'draft', supplier_id:8, receiving_origin:null, publication:null, publication_result:null, rows:[{...clone(initial[0]), values:{...values(1),brand:'Topeak',manufacturer_code:'0001410',supplier_code:'SUP-900',target_shops:[],sale_gross:null,vat_percent:null},errors:['missing_price','missing_vat'],hub_errors:[]}]};
  await render('/product-import?draft=test-draft&case=sku-policy');
  const beforeIdentity = calls.length;
  await click(document.querySelector('[data-testid="import-sku-1"]'));
  const dialog=document.querySelector('[role="dialog"]'); assert(dialog);
  assert.equal(dialog.querySelector('[data-testid="sku-source-code"]').value,'SUP-900','Supplier leaf code is the normal fallback, without claiming MPN evidence');
  assert(dialog.querySelector('[data-testid="sku-brand"]') && !dialog.querySelector('[data-testid="sku-leaf-confirmed"]'),'Brand prefix is automatic without a mandatory MPN confirmation');
  assert(dialog.textContent.includes('SUP-900'),'The scoped supplier code remains visible');
  await change(dialog.querySelector('[data-testid="sku-source-code"]'),'0001410');
  await act(async()=>new Promise(resolve=>setTimeout(resolve,300)));
  const proof=calls.filter(call=>call.path==='/api/sku-policy/preview').at(-1).body;
  assert.equal(proof.supplier_id,8); assert.equal(proof.supplier_sku,'SUP-900'); assert.equal(proof.sku_identity.source_code,'0001410'); assert.equal(proof.sku_identity.code_kind,'supplier'); assert.equal(proof.sku_identity.leaf_confirmed,false);
  await click(dialog.querySelector('[data-testid="import-sku-apply"]'));
  assert.equal(document.querySelector('[role="dialog"]'),null);
  assert(!button('Uložiť produkty do Hubu').disabled,'A basic card is savable despite missing shop price/VAT');
  await click(button('Uložiť produkty do Hubu'));
  assert.equal(server.rows[0].values.code,'TP-0001410');
  assert(!('code' in calls.filter(call=>call.method==='PUT').at(-1).body.rows[0].values),'SKU uniqueness and prefix generation belong to the server');
  assert.equal(server.rows[0].values.supplier_code,'SUP-900');
  assert.equal(server.rows[0].values.sale_gross,null);
  assert(!calls.slice(beforeIdentity).some(call=>/publish|finalize|stock/.test(call.path)),'Identity preparation and Hub save never publish or receive stock');
  await act(async () => root.unmount());
  console.log('Product import UI passed: durable drafts, CAS preservation, family propagation, persisted/bulk AI rule profiles, multiline TSV, selected bulk changes, safe HTML display, mixed AI estimates/results, explicit Hub save and shop publication.');
})().catch(error => { console.error(error); process.exitCode = 1; root.unmount(); });
