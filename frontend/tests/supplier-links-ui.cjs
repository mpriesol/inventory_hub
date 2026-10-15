/* Synthetic local API only: supplier observations never contact a supplier or change stock. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'https://hub.example.test/stock' });
global.window = dom.window; global.document = dom.window.document;
Object.defineProperty(global, 'navigator', { value: dom.window.navigator, configurable: true });
global.HTMLElement = dom.window.HTMLElement; global.IS_REACT_ACT_ENVIRONMENT = true;
for (const ext of ['.ts', '.tsx']) require.extensions[ext] = (module, filename) => module._compile(ts.transpileModule(
  fs.readFileSync(filename, 'utf8').replace(/import\.meta\.env/g, '{}'), {
    compilerOptions: { jsx: ts.JsxEmit.React, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, esModuleInterop: true },
  }).outputText, filename);
require.extensions['.css'] = () => {};
const i18n = require('../src/i18n/index.ts').default;
const React = require('react'); const { act } = React;
const { createRoot } = require('react-dom/client');
const { ProductSupplierLinksPanel } = require('../src/components/product/ProductSupplierLinksPanel.tsx');
const { unlockHub } = require('../src/api/access.ts');
const clone = value => JSON.parse(JSON.stringify(value));
const tick = () => new Promise(resolve => setTimeout(resolve, 5));
const element = id => document.querySelector(`[data-testid="${id}"]`);
const required = id => { const found = element(id); assert(found, id + ' exists'); return found; };
const disabled = id => !element(id) || element(id).disabled;
async function click(id) { await act(async () => { required(id).click(); await tick(); }); }
async function input(id, value) {
  await act(async () => {
    const target = required(id), select = target.tagName === 'SELECT';
    Object.getOwnPropertyDescriptor(select ? dom.window.HTMLSelectElement.prototype : dom.window.HTMLInputElement.prototype, 'value').set.call(target, value);
    target.dispatchEvent(new dom.window.Event(select ? 'change' : 'input', { bubbles: true })); await tick();
  });
}
const card = { id: 41, sku: 'BU-32682SII-281M', common: { name: 'NORTHKIT modrá M' } };
const product = { id: card.id, sku: card.sku, name: card.common.name, eans: ['8585047234448'], attributes: [{ name: 'Farba', value: 'Modrá' }, { name: 'Veľkosť', value: 'M' }] };
const source = (id, reasons = ['exact_ean']) => ({ id, supplier_id: 7, supplier_code: 'northfinder', supplier_name: 'Northfinder',
  supplier_sku: id === 72 ? '103404-281-104' : '103404-281-103', shop_sku: id === 72 ? 'NF-103404-281-104' : 'NF-103404-281-103',
  name: id === 72 ? 'NORTHKIT modrá L' : 'NORTHKIT modrá M', eans: id === 72 ? [] : ['8585047234448'],
  attributes: [{ name: 'Farba', value: 'Modrá' }, { name: 'Veľkosť', value: id === 72 ? 'L' : 'M' }],
  group_code: '103404', active: true, last_seen_at: '2026-10-15T10:00:00Z', match_reasons: reasons, warnings: [] });
const sources = [source(71), source(72, ['name']), source(73, ['search'])];
const calls = [], batches = new Map();
let previewMode = 'valid', applyMode = 'valid', releaseApply, changed = 0, closed = 0, root, currentLinks = [];
const response = value => ({ ok: true, status: 200, json: async () => clone(value) });
const fail = (code, status, knownUnapplied = false) => ({ ok: false, status,
  json: async () => ({ detail: { code, known_unapplied: knownUnapplied } }) });
function preview(body) {
  const selected = sources.find(row => row.id === body.supplier_product_id);
  const copying = !!body.copy_supplier_eans;
  return { id: body.request_id, status: 'prepared', preview_hash: 'a'.repeat(64), expires_at: '2099-01-01T00:00:00Z',
    product, source: selected, link_status: previewMode === 'conflict' ? 'conflict' : 'ready',
    ean_adoption: { selected: copying, available: !product.eans.length && !!selected.eans.length, eans: selected.eans, reason: null },
    warnings: [], errors: previewMode === 'conflict' ? ['identifier_conflict'] : [],
    planned_changes: copying ? ['supplier_eans'] : ['supply_source', 'supplier_sku_alias'], result: null };
}
function complete(batch) {
  const copied = batch.ean_adoption.selected ? batch.ean_adoption.eans : [];
  if (copied.length) product.eans = [...copied];
  batch.status = 'completed'; batch.result = { status: 'linked', product_id: product.id, supplier_product_id: batch.source.id,
    changes: batch.planned_changes, copied_eans: copied };
}
global.fetch = async (path, init) => {
  assert(path.startsWith('/api/supplier-card-links/'), 'Only local supplier-link endpoints may be called');
  assert.equal(init.cache, 'no-store'); assert.equal(init.credentials, 'same-origin');
  assert.equal(init.headers.Authorization, 'Bearer synthetic-supplier-links-token');
  const body = init.body ? JSON.parse(init.body) : undefined; calls.push({ path, method: init.method, body });
  if (path.startsWith('/api/supplier-card-links/options?')) {
    assert.equal(init.method, 'GET'); assert.equal(new URL(path, dom.window.location).searchParams.get('product_id'), String(product.id));
    return response({ product, suppliers: [{ id: 7, code: 'northfinder', name: 'Northfinder' }], current_links: currentLinks });
  }
  if (path.endsWith('/candidates')) {
    assert.equal(init.method, 'POST'); assert.equal(body.product_id, product.id); assert.equal(body.supplier_code, 'northfinder');
    return response({ product, rows: sources, truncated: false });
  }
  if (path.endsWith('/preview')) {
    assert.equal(init.method, 'POST'); assert.equal(body.product_id, product.id); assert(body.request_id);
    if (previewMode === 'network') throw new TypeError('Lost supplier preview response');
    const batch = preview(body); batches.set(batch.id, batch); return response(batch);
  }
  const match = path.match(/^\/api\/supplier-card-links\/([^/]+)(\/apply)?$/); assert(match, path);
  const batch = batches.get(match[1]); if (!batch) return fail('supplier_links_not_found', 404);
  if (!match[2]) { assert.equal(init.method, 'GET'); return response(batch); }
  assert.equal(init.method, 'POST'); assert.deepEqual(body, { preview_hash: batch.preview_hash, confirmed: true });
  assert.equal(batch.link_status, 'ready', 'Conflicts cannot be applied');
  if (applyMode === 'stale') return fail('supplier_links_identity_changed', 409, true);
  if (applyMode === 'pending') return new Promise((resolve, reject) => { releaseApply = () => reject(new TypeError('Lost apply response')); });
  if (applyMode === 'lost_prepared') throw new TypeError('Unconfirmed apply');
  complete(batch); if (applyMode === 'lost_completed') throw new TypeError('Committed result lost');
  return response(batch);
};
async function mount(selected = card, extra = {}) {
  root = createRoot(document.getElementById('root'));
  await act(async () => { root.render(React.createElement(ProductSupplierLinksPanel,
    { product: selected, onChanged: () => changed++, onClose: () => closed++, ...extra })); await tick(); });
}
async function unmount() { await act(async () => { root.unmount(); await tick(); }); }
const writes = () => calls.filter(call => call.path.endsWith('/apply'));
const previews = () => calls.filter(call => call.path.endsWith('/preview'));
async function select(id = 71) {
  await input('supplier-links-supplier', 'northfinder'); await click('supplier-links-search');
  await click('supplier-links-candidate-' + id);
}

(async () => {
  await i18n.changeLanguage('sk'); unlockHub('synthetic-supplier-links-token');
  await mount(); assert.equal(previews().length, 0); assert.equal(writes().length, 0);
  await input('supplier-links-supplier', 'northfinder'); await click('supplier-links-search');
  assert(disabled('supplier-links-preview'), 'A search result is never silently chosen');
  assert(document.body.textContent.includes('103404-281-103') && document.body.textContent.includes('NF-103404-281-103'));
  assert(document.body.textContent.includes('8585047234448'));
  assert(required('supplier-links-candidate-71').closest('label').textContent.includes(i18n.t('supplierLinks.reasons.exact_ean')),
    'Exact EAN evidence is visible next to the candidate');
  assert(required('supplier-links-candidate-72').closest('label').textContent.includes(i18n.t('supplierLinks.reasons.name')),
    'Name-only suggestions do not masquerade as exact identity matches');
  await click('supplier-links-candidate-71'); assert.equal(writes().length, 0);
  await click('supplier-links-preview'); assert.equal(writes().length, 0, 'Preview alone does not link the card');
  assert(!disabled('supplier-links-apply')); await click('supplier-links-apply');
  assert(element('supplier-links-completed')); assert.equal(changed, 1);
  await click('supplier-links-done'); assert.equal(closed, 1, 'Completion has a visible close action'); await unmount();

  previewMode = 'conflict'; await mount(); await select(72); await click('supplier-links-preview');
  assert(disabled('supplier-links-apply'), 'Conflicting supplier identity is not selectable for apply');
  const conflictsBefore = writes().length; if (element('supplier-links-apply')) await click('supplier-links-apply');
  assert.equal(writes().length, conflictsBefore); await unmount(); dom.window.sessionStorage.clear();

  previewMode = 'network'; await mount(); await select(); await click('supplier-links-preview');
  const lostPreview = clone(previews().at(-1).body);
  assert(element('supplier-links-recovery') && disabled('supplier-links-search'));
  await click('supplier-links-recover'); assert(!disabled('supplier-links-retry-preview'));
  previewMode = 'valid'; await click('supplier-links-retry-preview');
  assert.deepEqual(previews().at(-1).body, lostPreview, 'Missing preview recovery retains the same request UUID and source');
  applyMode = 'stale'; await click('supplier-links-apply');
  assert(!element('supplier-links-recovery') && !disabled('supplier-links-preview'), 'Durable known-unapplied rejection permits a fresh preview');
  assert(disabled('supplier-links-apply'), 'The expired preview cannot be reused after an identity change');
  await unmount(); dom.window.sessionStorage.clear();

  previewMode = 'valid'; await mount(); await select(); await click('supplier-links-preview');
  applyMode = 'pending'; const duplicateBefore = writes().length;
  await act(async () => { required('supplier-links-apply').click(); required('supplier-links-apply').click(); await tick(); });
  assert.equal(writes().length, duplicateBefore + 1, 'Immediate duplicate confirmations send once');
  await act(async () => { releaseApply(); await tick(); });
  const uncertainApply = clone(writes().at(-1));
  assert(disabled('supplier-links-preview') && disabled('supplier-links-search'));
  await click('supplier-links-recover');
  assert(disabled('supplier-links-preview'), 'Prepared GET does not settle an ambiguous apply');
  applyMode = 'valid'; await click('supplier-links-retry-apply');
  assert.deepEqual(writes().at(-1), uncertainApply, 'Recovery retries exactly the frozen preview and hash');
  assert(element('supplier-links-completed')); await unmount();

  await mount(); await select(); await click('supplier-links-preview');
  applyMode = 'lost_completed'; await click('supplier-links-apply');
  const completedWrites = writes().length;
  const storage = Object.values(dom.window.sessionStorage).join('');
  assert(!storage.includes('synthetic-supplier-links-token') && !storage.includes('8585047234448'), 'Recovery storage excludes credentials and source content');
  await unmount(); await mount(null);
  assert(element('supplier-links-completed')); assert.equal(writes().length, completedWrites, 'Resume recovers completed result without repeating writes');
  await unmount();

  product.eans = []; currentLinks = [{ ...source(71), link_id: 1, is_active: true }];
  const searchCount = calls.filter(call => call.path.endsWith('/candidates')).length;
  await mount(card, { fillEans: true });
  assert(required('supplier-links-copy-eans').checked, 'Explicit fill-EAN entry selects the existing source without another search');
  assert.equal(document.getElementById('supplier-links-title').textContent, i18n.t('supplierLinks.fillEans'));
  const optionalSearch = required('supplier-links-search').closest('details');
  assert(optionalSearch && !optionalSearch.open, 'Already-linked EAN filling keeps optional source search collapsed');
  assert.equal(calls.filter(call => call.path.endsWith('/candidates')).length, searchCount);
  await click('supplier-links-preview');
  assert.equal(previews().at(-1).body.copy_supplier_eans, true);
  assert(element('supplier-links-eans-planned').textContent.includes('8585047234448'));
  assert.equal(required('supplier-links-apply').textContent, i18n.t('supplierLinks.confirmEans'));
  applyMode = 'lost_completed'; await click('supplier-links-apply');
  const eanWrites = writes().length; await unmount(); await mount(null);
  assert(element('supplier-links-eans-copied') && document.body.textContent.includes('8585047234448'));
  assert.equal(writes().length, eanWrites, 'EAN adoption recovers the saved result without duplicating writes');
  await unmount();

  await mount(card, { fillEans: true });
  await click('supplier-links-current-71');
  assert(!element('supplier-links-copy-eans'), 'An existing card barcode is never silently replaced');
  await click('supplier-links-preview'); assert.equal(previews().at(-1).body.copy_supplier_eans, undefined);
  await unmount(); dom.window.sessionStorage.clear();

  product.eans = []; currentLinks = [];
  await mount(); await select();
  assert(!required('supplier-links-copy-eans').checked, 'Normal supplier linking requires a separate explicit EAN choice');
  await click('supplier-links-copy-eans'); await click('supplier-links-preview');
  assert.equal(previews().at(-1).body.copy_supplier_eans, true);
  await unmount(); dom.window.sessionStorage.clear();

  product.eans = []; currentLinks = [{ ...source(71), link_id: 1, is_active: true }];
  await mount(card, { fillEans: true }); await click('supplier-links-preview');
  applyMode = 'stale'; await click('supplier-links-apply');
  product.eans = ['8585047234448']; previewMode = 'conflict'; await click('supplier-links-preview');
  assert(required('supplier-links-copy-eans').checked, 'A stale selected EAN choice remains visible after refreshed card data');
  await click('supplier-links-copy-eans');
  assert(!element('supplier-links-copy-eans'), 'The operator can opt out after the card is no longer empty');
  previewMode = 'valid'; await click('supplier-links-preview');
  assert.equal(previews().at(-1).body.copy_supplier_eans, undefined, 'The fresh preview no longer requests unavailable EAN copying');
  await unmount(); dom.window.sessionStorage.clear();

  unlockHub(''); console.log('Supplier card linking UI checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
