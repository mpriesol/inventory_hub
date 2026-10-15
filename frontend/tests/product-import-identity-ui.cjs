/* Real import identity controls against synthetic local data; no supplier or shop traffic. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'https://hub.example.test/product-import?draft=identity-draft' });
global.window = dom.window; global.document = dom.window.document;
Object.defineProperty(global, 'navigator', { value: dom.window.navigator, configurable: true });
global.HTMLElement = dom.window.HTMLElement; global.localStorage = dom.window.localStorage;
global.IS_REACT_ACT_ENVIRONMENT = true; window.confirm = () => true;
for (const ext of ['.ts', '.tsx']) require.extensions[ext] = (module, filename) => module._compile(ts.transpileModule(
  fs.readFileSync(filename, 'utf8').replace(/import\.meta\.env/g, '{}'), {
    compilerOptions: { jsx: ts.JsxEmit.React, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, esModuleInterop: true },
  }).outputText, filename);
require.extensions['.css'] = () => {};
const i18n = require('../src/i18n/index.ts').default;
const React = require('react'); const { act } = React;
const { createRoot } = require('react-dom/client');
const { createMemoryRouter, RouterProvider } = require('react-router-dom');
const { ImportIdentityReviewPanel } = require('../src/components/product/ImportIdentityReviewPanel.tsx');
const { ProductImportPage } = require('../src/pages/ProductImportPage.tsx');
const { unlockHub } = require('../src/api/access.ts');
const clone = value => JSON.parse(JSON.stringify(value));
const tick = () => new Promise(resolve => setTimeout(resolve, 5));
const element = id => document.querySelector(`[data-testid="${id}"]`);
const required = id => { const found = element(id); assert(found, id + ' exists'); return found; };
const disabled = id => !element(id) || element(id).disabled;
async function click(id) { await act(async () => { required(id).click(); await tick(); }); }
const options = { language: 'sk', currency: 'EUR', pricelist: 'Default', category_code: null, pricing: 'configured', include_images: true, include_description: true, include_parameters: true };
const values = id => ({ code: 'NF-' + id, supplier_code: String(id), group_name: '', name: 'Supplier jacket ' + id, brand: 'Northfinder',
  manufacturer_code: '', eans: [], images: [], description_html: '', short_description: '', seo_title: '', seo_description: '', seo_url: '',
  category_code: null, parameters: [], variant_attributes: [], metadata: {}, purchase_net: null, retail_gross: null, sale_gross: null,
  vat_percent: null, currency: 'EUR', availability: '', ai_enabled: false, ai_category_profile: 'auto', target_shops: [], target_category_codes: {} });
const initialDraft = () => ({ id: 'identity-draft', revision: 7, status: 'draft', supplier: 'northfinder', shop: 'biketrek', feed_key: 'products',
  options, categories: [], shops: [], rows: [1, 2, 3].map(id => ({ id, group_key: 'single-' + id, is_variant: false, source_category: null,
    mapping_revision: null, values: values(id), manual_fields: [], provenance: {}, warnings: [], errors: [], hub_product_id: null, ai_job: null })),
  publication: null, publication_result: null, identity_review_required: true, identity_review_count: 3 });
const candidate = (id, reasons = ['exact_ean']) => ({ product_id: id, sku: 'EXISTING-' + id, name: 'Existing jacket ' + id, brand: 'Northfinder',
  eans: ['8585047234448'], attributes: [{ name: 'Farba', value: 'Modrá' }, { name: 'Veľkosť', value: 'M' }], is_active: true,
  reasons, warnings: [], can_link: true, errors: [] });
let server = initialDraft(), reviewMode = 'normal', decisionMode = 'normal', changed = [], closed = 0, root;
const calls = [];
const response = value => ({ ok: true, status: 200, json: async () => clone(value) });
const fail = (code, status) => ({ ok: false, status, json: async () => ({ detail: { code } }) });
function review() {
  const rows = server.rows.map(row => ({ row_id: row.id,
    source: { sku: row.values.code, supplier_sku: row.values.supplier_code, name: row.values.name, eans: [], attributes: [], brand: 'Northfinder' },
    status: row.id === 2 ? 'new' : 'review', matched_product_id: row.id === 1 ? 501 : null, can_create: row.id !== 1,
    candidates: row.id === 2 ? [] : [candidate(row.id === 1 ? 501 : 503, row.id === 1 ? ['exact_ean'] : ['similar_name'])],
    truncated: false, decision: null, errors: [] }));
  // A provisional candidate can look linkable while the complete source row
  // is contradictory; the row-level conflict must remain authoritative.
  if (reviewMode === 'conflict') { rows[0].status = 'conflict'; rows[0].can_create = false; rows[0].candidates[0].can_link = true;
    rows[0].candidates[0].errors = ['identifier_conflict']; rows[0].errors = ['identifier_conflict']; }
  return { draft_id: server.id, revision: server.revision, review_hash: 'b'.repeat(64), rows };
}
global.fetch = async (path, init) => {
  if (path === '/api/ai-content/rules') return response({ book: { categories: [{ id: 'general', name: 'General' }] } });
  assert(path.startsWith('/api/product-imports/identity-draft'), 'Review must stay within the existing draft');
  assert.equal(init.cache, 'no-store'); assert.equal(init.credentials, 'same-origin');
  assert.equal(init.headers.Authorization, 'Bearer synthetic-identity-token');
  const body = init.body ? JSON.parse(init.body) : undefined; calls.push({ path, method: init.method, body });
  if (path === '/api/product-imports/identity-draft') { assert.equal(init.method, 'GET'); return response(server); }
  assert.equal(init.method, 'POST');
  if (path.endsWith('/identity-review')) { assert.equal(body.expected_revision, server.revision); return response(review()); }
  assert(path.endsWith('/identity-decisions'), 'Identity review must not silently continue into Hub save, AI or publication');
  assert.equal(body.expected_revision, server.revision); assert.equal(body.review_hash, 'b'.repeat(64));
  if (decisionMode === 'conflict') return fail('import_draft_changed', 409);
  server.revision++; server.identity_review_required = false; server.identity_review_count = 0;
  if (decisionMode === 'lost') throw new TypeError('Committed draft decision response lost');
  return response(server);
};
async function mount() {
  root = createRoot(document.getElementById('root'));
  await act(async () => { root.render(React.createElement(ImportIdentityReviewPanel,
    { draft: clone(server), onChanged: draft => changed.push(draft), onClose: () => closed++ })); await tick(); });
}
async function unmount() { await act(async () => { root.unmount(); await tick(); }); }
async function mountPage() {
  root = createRoot(document.getElementById('root'));
  const router = createMemoryRouter([{ path: '/product-import', element: React.createElement(ProductImportPage) }], {
    initialEntries: ['/product-import?draft=identity-draft'],
  });
  await act(async () => { root.render(React.createElement(RouterProvider, { router })); await tick(); });
}
const decisions = () => calls.filter(call => call.path.endsWith('/identity-decisions'));
async function chooseAll() {
  if (!element('identity-review-create-2')) await act(async () => {
    document.querySelector('.identity-review-toolbar input[type="checkbox"]').click(); await tick();
  });
  await click('identity-review-link-1-501'); await click('identity-review-create-2'); await click('identity-review-link-3-503');
}

(async () => {
  await i18n.changeLanguage('sk'); unlockHub('synthetic-identity-token');
  await mount(); assert.equal(decisions().length, 0, 'Opening duplicate review only reads identity evidence');
  assert(!required('identity-review-link-1-501').checked,
    'An exact match proposal still requires an explicit operator decision');
  assert(disabled('identity-review-create-1'), 'An exact existing identity cannot be bypassed by creating a duplicate');
  assert(document.body.textContent.includes('EXISTING-501') && document.body.textContent.includes('Supplier jacket 1'));
  await chooseAll(); assert.equal(decisions().length, 0, 'Choosing an existing card makes no product or draft write');
  await click('identity-review-save');
  assert.deepEqual(decisions().at(-1).body.decisions, [
    { row_id: 1, action: 'link', product_id: 501 }, { row_id: 2, action: 'create' }, { row_id: 3, action: 'link', product_id: 503 },
  ]);
  assert.equal(changed.length, 1); assert.equal(changed[0].revision, 8);
  assert.equal(changed[0].identity_review_required, false); await unmount();

  server = initialDraft(); reviewMode = 'conflict'; await mount();
  assert(disabled('identity-review-link-1-501') && disabled('identity-review-create-1'), 'Conflicting identity has no apply or duplicate escape');
  await unmount(); reviewMode = 'normal';

  server = initialDraft(); decisionMode = 'conflict'; const changedBefore = changed.length;
  await mount(); await chooseAll(); await click('identity-review-save');
  assert.equal(changed.length, changedBefore, 'A stale CAS response does not report a resolved draft');
  assert(element('identity-review-save'), 'A rejected write keeps the review open');
  assert(document.querySelector('[role="alert"]'), 'The operator sees why their identity choices were not accepted');
  assert(disabled('identity-review-save'), 'CAS rejection freezes decision writes until a fresh read');
  const conflictWrites = decisions().length; await click('identity-review-refresh');
  assert.equal(decisions().length, conflictWrites, 'CAS recovery reads the draft and does not repeat the rejected choices');
  assert(!required('identity-review-link-1-501').checked, 'Recovery discards the old unaccepted decision selection');
  await unmount();

  server = initialDraft(); decisionMode = 'lost'; await mount(); await chooseAll();
  const changedBeforeLost = changed.length;
  const beforeLost = decisions().length; await click('identity-review-save');
  assert.equal(changed.length, changedBeforeLost, 'A lost response does not optimistically continue the import');
  assert(disabled('identity-review-save'), 'Uncertain decisions must be recovered before another write');
  assert.equal(decisions().length, beforeLost + 1); await click('identity-review-refresh');
  assert.equal(decisions().length, beforeLost + 1, 'A completed lost response is recovered without repeating the decision write');
  assert.equal(changed.at(-1).revision, 8); assert.equal(changed.at(-1).identity_review_required, false);
  await unmount();

  server = initialDraft(); decisionMode = 'normal'; await mountPage();
  const hubSave = () => [...document.querySelectorAll('button')].find(button => button.textContent.trim() === 'Uložiť produkty do Hubu');
  assert(hubSave() && hubSave().disabled, 'The import page blocks Hub registration while identity review is unresolved');
  await click('import-identity-review'); await chooseAll();
  const beforePageDecisions = decisions().length; await click('identity-review-save');
  assert.equal(decisions().length, beforePageDecisions + 1);
  assert(hubSave() && !hubSave().disabled, 'Accepted decisions unlock a separate explicit Hub save');
  assert.equal(calls.some(call => /\/(save|ai|publish|publish-preview)$/.test(call.path)), false,
    'Resolving duplicates alone never registers cards, starts AI, or sends shop products');
  await unmount();
  unlockHub(''); console.log('Product import duplicate identity UI checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
