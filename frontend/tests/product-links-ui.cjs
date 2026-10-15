/* Synthetic local API only: no shop traffic or production mutations. */
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
const { ProductShopLinksPanel } = require('../src/components/product/ProductShopLinksPanel.tsx');
const { unlockHub } = require('../src/api/access.ts');
const { PRODUCT_LINK_PENDING_KEY } = require('../src/api/productLinks.ts');
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
const rows = [1, 2, 3, 4].map(id => ({ id, sku: 'HUB-' + id, common: { name: 'Local card ' + id } }));
const calls = [], batches = new Map();
let previewMode = 'valid', applyMode = 'valid', barcodeMode = 'verified', releaseApply, changed = 0, closed = 0, root;
const response = value => ({ ok: true, status: 200, json: async () => clone(value) });
const fail = (code, status, knownUnapplied = false) => ({ ok: false, status,
  json: async () => ({ detail: { code, known_unapplied: knownUnapplied } }) });
function preview(body) {
  const manual = new Map((body.manual_matches || []).map(item => [item.product_id, item]));
  const barcodeEvidence = {
    verified: { eans: ['5901234123457'], raw_barcodes: ['5901234123457'], invalid_barcodes: [] },
    invalid: { eans: [], raw_barcodes: ['858048234448'], invalid_barcodes: ['858048234448'] },
    missing: { eans: [], raw_barcodes: [], invalid_barcodes: [] },
    legacy_invalid: { eans: [] },
  }[barcodeMode];
  return { id: body.request_id, shop_code: body.shop_code, status: 'prepared', preview_hash: 'a'.repeat(64),
    expires_at: '2099-01-01T00:00:00Z', result: null,
    rows: body.product_ids.map(id => ({ product_id: id, sku: 'HUB-' + id, name: 'Local card ' + id,
      local_eans: barcodeMode === 'legacy_invalid' ? undefined : barcodeMode === 'invalid' ? ['8585047234448'] : [],
      status: manual.has(id) ? 'ready' : ({ 1: 'ready', 2: 'already_linked', 3: 'conflict', 4: 'not_found' })[id] || 'ready',
      match_by: manual.has(id) ? 'manual' : 'shared_sku',
      remote: id === 4 && !manual.has(id) ? null : { code: manual.get(id)?.code || 'HUB-' + id,
        parent_code: manual.get(id)?.parent_code || 'PARENT', is_variant: manual.get(id)?.is_variant ?? true,
        external_id: String(1000 + id), parent_id: '999', name: 'Shop card ' + id,
        ...barcodeEvidence, attributes: [] },
      warnings: [...(id === 1 ? ['variant_axes_unknown'] : []),
        ...(['invalid', 'legacy_invalid'].includes(barcodeMode) ? ['unverified_barcode', 'barcode_missing'] : [])],
      errors: id === 3 ? ['identifier_conflict'] : [] })) };
}
function complete(batch, body) {
  batch.status = 'completed'; batch.result = { product_ids: body.product_ids,
    linked_product_ids: body.product_ids, already_linked_product_ids: [] };
}
global.fetch = async (path, init) => {
  assert(path.startsWith('/api/product-links/')); assert.equal(init.cache, 'no-store');
  assert.equal(init.credentials, 'same-origin'); assert.equal(init.headers.Authorization, 'Bearer synthetic-links-token');
  const body = init.body ? JSON.parse(init.body) : undefined; calls.push({ path, method: init.method, body });
  if (path.endsWith('/preview')) {
    assert.equal(init.method, 'POST');
    if (previewMode === 'invalid') return fail('product_links_invalid_request', 422);
    if (previewMode === 'network') throw new TypeError('Response lost before preview');
    const batch = preview(body); batches.set(batch.id, batch);
    return response(batch);
  }
  const match = path.match(/^\/api\/product-links\/([^/]+)(\/apply)?$/); assert(match, path);
  const batch = batches.get(match[1]);
  if (!batch) return fail('product_links_not_found', 404);
  if (!match[2]) { assert.equal(init.method, 'GET'); return response(batch); }
  assert.equal(init.method, 'POST'); assert.equal(body.confirmed, true); assert.equal(body.preview_hash, batch.preview_hash);
  assert(body.product_ids.every(id => batch.rows.some(row => row.product_id === id && row.status === 'ready')));
  if (applyMode === 'stale') return fail('product_links_identity_changed', 409, true);
  if (applyMode === 'source_stale') return fail('source_identity_changed', 409, true);
  if (applyMode === 'expired') return fail('product_links_preview_expired', 409, true);
  if (applyMode === 'fenced_unavailable') return fail('source_unavailable', 502, true);
  if (applyMode === 'unfenced_stale') return fail('source_identity_changed', 409);
  if (applyMode === 'pending') return new Promise((resolve, reject) => { releaseApply = () => reject(new TypeError('Lost response')); });
  if (applyMode === 'lost_prepared') throw new TypeError('No confirmed result');
  complete(batch, body);
  if (applyMode === 'lost_completed') throw new TypeError('Committed result lost');
  return response(batch);
};
async function mount(selected = rows) {
  root = createRoot(document.getElementById('root'));
  await act(async () => { root.render(React.createElement(ProductShopLinksPanel,
    { rows: selected, onChanged: () => changed++, onClose: () => closed++ })); await tick(); });
}
async function unmount() { await act(async () => { root.unmount(); await tick(); }); }
const writes = () => calls.filter(call => call.path.endsWith('/apply'));
const previews = () => calls.filter(call => call.path.endsWith('/preview'));
function clearPending() { dom.window.sessionStorage.removeItem(PRODUCT_LINK_PENDING_KEY); }

(async () => {
  await i18n.changeLanguage('sk'); unlockHub('synthetic-links-token');
  await mount(); assert.equal(calls.length, 0, 'Opening the workflow does not send a request');
  await input('product-links-shop', 'xtrek'); await click('product-links-preview');
  assert.deepEqual(previews()[0].body.product_ids, [1, 2, 3, 4]); assert.equal(previews()[0].body.shop_code, 'xtrek');
  for (const status of ['ready', 'already_linked', 'conflict', 'not_found']) assert(required('product-links-count-' + status).textContent.startsWith('1'));
  assert(!element('product-links-done'), 'A mixed batch still needs the selected new links to be confirmed');
  assert(!disabled('product-links-select-1'));
  for (const id of [2, 3, 4]) assert(disabled('product-links-select-' + id));
  assert(document.body.textContent.includes('1001') && document.body.textContent.includes('5901234123457'));
  assert(document.body.textContent.includes(i18n.t('productLinks.reasons.variant_axes_unknown')), 'Unknown axes have an understandable explanation');
  await click('product-links-select-1'); assert(disabled('product-links-apply')); await click('product-links-select-1');
  await click('product-links-apply'); assert.deepEqual(writes().at(-1).body.product_ids, [1]);
  assert(element('product-links-completed')); assert.equal(changed, 1); assert.equal(dom.window.sessionStorage.length, 0);
  assert(!disabled('product-links-done'), 'A completed apply has an explicit finish action');
  await unmount();

  const beforeAlready = writes().length;
  await mount([rows[1]]); await click('product-links-preview');
  assert(required('product-links-completed').textContent.includes(i18n.t('productLinks.allAlreadyLinked', { shop: 'BIKETREK' })));
  assert(!element('product-links-apply') && !element('product-links-preview'));
  assert.equal(dom.window.sessionStorage.length, 0, 'A no-op preview does not remain a pending operation');
  await click('product-links-done'); assert.equal(closed, 1);
  assert.equal(writes().length, beforeAlready, 'Already-linked cards do not need an apply request');
  await unmount();

  const legacyRequest = { request_id: crypto.randomUUID(), shop_code: 'xtrek', product_ids: [2] };
  const legacyBatch = preview(legacyRequest); batches.set(legacyBatch.id, legacyBatch);
  dom.window.sessionStorage.setItem(PRODUCT_LINK_PENDING_KEY, JSON.stringify({ version: 1, request: legacyRequest }));
  const beforeRecovery = calls.length;
  await mount([rows[3]]);
  assert(element('product-links-done') && !element('product-links-recovery'));
  assert(element('product-links-row-2') && !element('product-links-row-4'));
  assert(document.body.textContent.includes(i18n.t('productLinks.recoveredScope')),
    'Finishing a recovered no-op keeps its original card scope clear');
  assert.equal(dom.window.sessionStorage.length, 0, 'A recovered legacy no-op preview is finished locally');
  assert.equal(calls.length, beforeRecovery + 1); assert.equal(calls.at(-1).method, 'GET');
  await unmount();

  const uncertainAlready = { version: 1, request: legacyRequest,
    apply: { preview_hash: legacyBatch.preview_hash, product_ids: [2], confirmed: true } };
  dom.window.sessionStorage.setItem(PRODUCT_LINK_PENDING_KEY, JSON.stringify(uncertainAlready));
  await mount([]);
  assert(!element('product-links-done') && !element('product-links-completed'));
  assert(element('product-links-recovery') && !disabled('product-links-retry-apply'));
  assert(disabled('product-links-shop') && disabled('product-links-preview'));
  assert.deepEqual(JSON.parse(dom.window.sessionStorage.getItem(PRODUCT_LINK_PENDING_KEY)), uncertainAlready,
    'An all-already-linked prepared GET cannot settle a previously uncertain apply');
  await unmount(); clearPending();

  barcodeMode = 'invalid'; await mount([rows[0]]); await input('product-links-shop', 'xtrek'); await click('product-links-preview');
  assert.equal(required('product-links-hub-ean-1').textContent, '8585047234448');
  const invalidRemote = required('product-links-shop-barcode-1');
  assert(invalidRemote.textContent.startsWith('858048234448'), 'The exact invalid shop barcode remains visible');
  assert(invalidRemote.textContent.includes(i18n.t('productLinks.barcodeInvalid', { value: '858048234448' })));
  assert(!invalidRemote.textContent.includes('8585047234448'), 'The Hub barcode is not substituted for the shop value');
  assert(!document.body.textContent.includes(i18n.t('productLinks.reasons.barcode_missing')), 'Known invalid values do not get a misleading generic missing warning');
  assert(!disabled('product-links-apply'), 'An invalid barcode warning does not disable an otherwise approved exact SKU match');
  await unmount(); clearPending();

  barcodeMode = 'missing'; await mount([rows[0]]); await click('product-links-preview');
  assert.equal(required('product-links-shop-barcode-1').textContent, i18n.t('productLinks.barcodeAbsent'));
  assert.equal(required('product-links-hub-ean-1').textContent, i18n.t('productLinks.barcodeAbsent'));
  assert(!document.querySelector('.product-links-barcode-warning'), 'An absent value is not called invalid');
  await unmount(); clearPending();

  barcodeMode = 'legacy_invalid'; await mount([rows[0]]); await click('product-links-preview');
  assert.equal(required('product-links-shop-barcode-1').textContent, i18n.t('productLinks.barcodeNotRecorded'));
  assert(document.body.textContent.includes(i18n.t('productLinks.reasons.unverified_barcode')),
    'An older stored result retains its warning without inventing or claiming an absent raw value');
  await unmount(); clearPending(); barcodeMode = 'verified';

  await mount([rows[3]]); await input('product-links-parent-4', 'EXACT-PARENT'); await input('product-links-variant-4', 'EXACT-LEAF');
  await click('product-links-preview');
  assert.deepEqual(previews().at(-1).body.manual_matches, [{ product_id: 4, code: 'EXACT-LEAF', parent_code: 'EXACT-PARENT', is_variant: true }]);
  assert(!disabled('product-links-select-4')); await click('product-links-apply'); await unmount();

  previewMode = 'invalid'; await mount([rows[0]]); await click('product-links-preview');
  assert(!disabled('product-links-shop') && !disabled('product-links-preview'), 'A definite input rejection unlocks corrections');
  assert(!element('product-links-recovery')); assert.equal(dom.window.sessionStorage.length, 0); await unmount();

  previewMode = 'network'; await mount([rows[0]]); await click('product-links-preview');
  const pendingPreview = clone(previews().at(-1).body);
  assert(disabled('product-links-shop') && disabled('product-links-preview') && element('product-links-recovery'));
  await click('product-links-recover'); assert(!disabled('product-links-retry-preview'));
  previewMode = 'valid'; await click('product-links-retry-preview');
  assert.deepEqual(previews().at(-1).body, pendingPreview, 'Preview retry retains its UUID and exact original request');
  applyMode = 'pending'; const beforeApply = writes().length;
  await act(async () => { required('product-links-apply').click(); required('product-links-apply').click(); await tick(); });
  assert.equal(writes().length, beforeApply + 1, 'Immediate duplicate apply clicks send once');
  await act(async () => { releaseApply(); await tick(); });
  const originalApply = clone(writes().at(-1));
  assert(disabled('product-links-preview') && disabled('product-links-select-1'));
  await click('product-links-recover');
  assert(disabled('product-links-preview') && disabled('product-links-select-1'), 'Prepared GET cannot unfreeze a possibly still running apply');
  applyMode = 'unfenced_stale'; await click('product-links-retry-apply');
  assert(disabled('product-links-preview') && element('product-links-recovery'), 'A retry rejection without a durable fence cannot settle the earlier ambiguous request');
  applyMode = 'valid'; await click('product-links-retry-apply');
  assert.deepEqual(writes().at(-1), originalApply, 'Apply retries retain the original batch, hash and exact subset');
  assert(element('product-links-completed')); await unmount();

  await mount([rows[0]]); await click('product-links-preview'); applyMode = 'lost_completed'; await click('product-links-apply');
  assert(element('product-links-recovery')); const committedId = writes().at(-1).path; const writeCount = writes().length;
  const stored = dom.window.sessionStorage.getItem(PRODUCT_LINK_PENDING_KEY);
  assert(stored && !stored.includes('synthetic-') && !stored.includes('Shop card') && !stored.includes('5901234123457'));
  await unmount(); await mount([rows[3]]);
  assert(element('product-links-completed') && element('product-links-row-1') && !element('product-links-row-4'));
  assert(document.body.textContent.includes(i18n.t('productLinks.recoveredScope')),
    'A recovered completed batch does not imply that the different current selection was linked');
  assert.equal(writes().length, writeCount, 'Reopening recovers the completed batch without repeating apply');
  assert.equal(calls.at(-1).path + '/apply', committedId); assert.equal(dom.window.sessionStorage.length, 0); await unmount();

  for (const mode of ['stale', 'source_stale', 'fenced_unavailable']) {
    applyMode = mode; await mount([rows[0]]); await click('product-links-preview'); await click('product-links-apply');
    assert(!disabled('product-links-preview'), mode + ': a committed expiry fence permits a new preview');
    assert(!element('product-links-recovery')); await unmount(); clearPending();
  }

  await mount([rows[0]]); await click('product-links-preview'); applyMode = 'lost_prepared'; await click('product-links-apply');
  const fencedOriginal = clone(previews().at(-1).body);
  applyMode = 'source_stale'; await click('product-links-retry-apply');
  assert(!disabled('product-links-preview') && !element('product-links-recovery'), 'A durable expiry fence settles a previous ambiguous apply');
  assert(disabled('product-links-apply'), 'An expired batch must receive a fresh preview before applying');
  await click('product-links-preview');
  assert.notEqual(previews().at(-1).body.request_id, fencedOriginal.request_id);
  assert.deepEqual(previews().at(-1).body.product_ids, fencedOriginal.product_ids);
  await unmount(); clearPending();

  await mount([rows[0], rows[1]]); await click('product-links-preview');
  const resumedRequest = clone(previews().at(-1).body); await unmount();
  await mount([]); assert(element('product-links-row-1') && element('product-links-row-2'));
  applyMode = 'expired'; await click('product-links-apply');
  assert(!disabled('product-links-preview'), 'Global Resume retains the recovered cards after an expired preview');
  previewMode = 'invalid'; await click('product-links-preview');
  assert(!disabled('product-links-preview'), 'Recovered scope survives a failed replacement preview');
  previewMode = 'valid'; await click('product-links-preview');
  assert.deepEqual(previews().at(-1).body.product_ids, resumedRequest.product_ids);
  assert.notEqual(previews().at(-1).body.request_id, resumedRequest.request_id);
  await unmount(); clearPending();

  await mount([rows[0]]); await click('product-links-preview'); await unmount();
  await mount([rows[3]]); applyMode = 'expired'; await click('product-links-apply');
  await click('product-links-preview');
  assert.deepEqual(previews().at(-1).body.product_ids, [4], 'Explicit current selection stays the scope of a new preview');
  await unmount(); clearPending();

  await mount(Array.from({ length: 101 }, (_, index) => ({ id: index + 1, sku: 'LIMIT-' + index, common: { name: 'Limit' } })));
  assert(disabled('product-links-preview')); assert(document.body.textContent.includes(i18n.t('productLinks.limit'))); await unmount();
  unlockHub('');
  console.log('Product linking UI behavior checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
