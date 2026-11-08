/* Synthetic local API only: no production, native-shop or physical-stock writes. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html><button id="opener">Merge cards</button><div id="root"></div>',
  { url: 'https://hub.example.test/stock' });
global.window = dom.window;
global.document = dom.window.document;
global.HTMLElement = dom.window.HTMLElement;
Object.defineProperty(global, 'navigator', { value: dom.window.navigator, configurable: true });
global.IS_REACT_ACT_ENVIRONMENT = true;
for (const ext of ['.ts', '.tsx']) require.extensions[ext] = (module, filename) => module._compile(ts.transpileModule(
  fs.readFileSync(filename, 'utf8').replace(/import\.meta\.env/g, '{}'), {
    compilerOptions: { jsx: ts.JsxEmit.React, module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2020, esModuleInterop: true },
  }).outputText, filename);
require.extensions['.css'] = () => {};
const i18n = require('../src/i18n/index.ts').default;
const React = require('react');
const { act } = React;
const { createRoot } = require('react-dom/client');
const { ProductConsolidationPanel } = require('../src/components/product/ProductConsolidationPanel.tsx');
const { unlockHub } = require('../src/api/access.ts');
const { PRODUCT_CONSOLIDATION_PENDING_KEY, pendingProductConsolidation } = require('../src/api/productConsolidation.ts');

const clone = value => JSON.parse(JSON.stringify(value));
const tick = () => new Promise(resolve => setTimeout(resolve, 5));
const element = id => document.querySelector(`[data-testid="${id}"]`);
const required = id => { const value = element(id); assert(value, id + ' exists'); return value; };
const disabled = id => !element(id) || element(id).disabled;
async function click(id) { await act(async () => { required(id).click(); await tick(); }); }
async function input(id, value) {
  await act(async () => {
    const target = required(id);
    const prototype = target.tagName === 'SELECT' ? dom.window.HTMLSelectElement.prototype
      : target.tagName === 'TEXTAREA' ? dom.window.HTMLTextAreaElement.prototype : dom.window.HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(prototype, 'value').set.call(target, value);
    target.dispatchEvent(new dom.window.Event(target.tagName === 'SELECT' ? 'change' : 'input', { bubbles: true }));
    await tick();
  });
}
async function check(id, value = true) { if (required(id).checked !== value) await click(id); }
const attributes = [{ name: 'Veľkosť', value: 'M' }, { name: 'Farba', value: 'Modrá' }];
const cards = [1, 2, 3, 4].map(id => ({ id, sku: 'SYN-CARD-' + id, name: 'Synthetic blue shirt M ' + id,
  is_active: true, item_type: 'stock', stock_unit: 'pcs', eans: ['5901234123457'],
  attributes: clone(attributes), manual_eans: null, canonical_product_id: null }));
const editorRows = cards.map(card => ({ ...clone(card), common: { name: card.name }, shops: [] }));
const mapping = (id, product_id, shop_code, external_code, external_product_id, extra = {}) => ({
  id, product_id, shop_code, shop_name: shop_code === 'biketrek' ? 'BIKETREK' : 'xTrek', shop_available: true,
  external_code, external_product_id, external_variant_id: null, parent_code: null, is_variant: false,
  is_listed: true, shared_stock: false, sale_to_stock_factor: '1', push_pending: false, ...extra,
});
const mappings = [mapping(101, 1, 'biketrek', 'SYN-RETAIL', '1001'),
  mapping(102, 2, 'xtrek', 'SYN-RETAIL-M', '2001', { external_variant_id: '2002', parent_code: 'SYN-RETAIL', is_variant: true }),
  mapping(103, 2, 'biketrek', 'SYN-UNLISTED', '1003', { is_listed: false }),
  mapping(104, 3, 'biketrek', 'SYN-OTHER-3', '3001'), mapping(105, 4, 'xtrek', 'SYN-OTHER-4', '4001')];
const note = 'The manufacturer article, blue colour and size M identify the same complete retail shirt.';
const physicalUnit = 'One complete blue shirt in size M';
const sourceUrl = 'https://manufacturer.example/products/synthetic-shirt';

const calls = [], batches = new Map(), requests = new Map(), fetchFailures = [];
let previewMode, applyMode, discardMode, selectionOverride, expectedToken, releasePreview, releaseApply;
let root, changed = 0, closed = 0, passed = 0;
const response = value => ({ ok: true, status: 200, json: async () => clone(value) });
const fail = (code, status = 409, knownUnapplied = false) => ({ ok: false, status,
  json: async () => ({ detail: { code, known_unapplied: knownUnapplied } }) });
const previews = () => calls.filter(call => call.path === '/api/product-consolidations/preview');
const writes = () => calls.filter(call => call.path.endsWith('/apply'));
const discards = () => calls.filter(call => call.path.endsWith('/discard'));
const reads = () => calls.filter(call => call.method === 'GET');
const saved = () => JSON.parse(dom.window.sessionStorage.getItem(PRODUCT_CONSOLIDATION_PENDING_KEY) || 'null');
function selection(ids) {
  const value = { products: clone(cards.filter(card => ids.includes(card.id))),
    mappings: clone(mappings.filter(row => ids.includes(row.product_id))), blockers: [] };
  return selectionOverride ? selectionOverride(value) : value;
}
function review(body) {
  const ids = [body.keeper_product_id, ...body.retired_product_ids];
  const current = selection(ids), native = {};
  for (const action of body.mapping_actions) {
    if (!action.target) continue;
    const target = action.target;
    const observation = native[target.shop_code] ||= { fingerprint: 'synthetic-connection', requests: 1,
      leaves: [], absent_mapping_ids: [] };
    observation.leaves.push({ code: target.code, parent_code: target.parent_code, is_variant: target.is_variant,
      parent_id: String(target.native_product_id), external_id: String(target.is_variant ? target.native_variant_id : target.native_product_id),
      name: 'Synthetic remote retail shirt', eans: ['5901234123457'], raw_barcodes: ['5901234123457'],
      invalid_barcodes: [], attributes: clone(attributes) });
  }
  const blockers = previewMode === 'blocked'
    ? [{ code: 'consolidation_dependency_in_use', table: 'receiving_lines', count: 2 }] : [];
  if ([...batches.values()].some(batch => batch.status === 'prepared' && batch.id !== body.request_id &&
      [batch.keeper_product_id, ...batch.retired_product_ids].some(id => ids.includes(id))))
    blockers.push({ code: 'consolidation_dependency_in_use', table: 'product_consolidations', count: 1 });
  return { id: body.request_id, status: 'prepared', preview_hash: 'a'.repeat(64), expires_at: '2099-01-01T00:00:00Z',
    eligible: !blockers.length, keeper_product_id: body.keeper_product_id, retired_product_ids: [...body.retired_product_ids],
    identity_evidence: clone(body.identity_evidence), products: current.products.map(card => ({
      id: card.id, sku: card.sku, name: card.name, is_active: card.is_active, item_type: card.item_type,
      stock_unit: card.stock_unit, created_from_source: 'synthetic-test', identity: { product_id: card.id,
        sku: card.sku, name: card.name, is_active: card.is_active, eans: card.eans, attributes: card.attributes, manual_eans: card.manual_eans } })),
    existing_mappings: current.mappings, plan: { actions: clone(body.mapping_actions), additional_sales: [], absence: {} },
    native, dependencies: { blockers, facts: {}, history_hash: 'unchanged-synthetic-history' }, result: null };
}
function complete(batch) {
  batch.status = 'completed';
  batch.result = { keeper_product_id: batch.keeper_product_id, retired_product_ids: batch.retired_product_ids,
    retired_mapping_ids: batch.plan.actions.filter(action => action.action !== 'retain').map(action => action.mapping_id),
    current_mappings: batch.plan.actions.filter(action => action.target).map(action => ({ id: action.mapping_id + 1000,
      product_id: batch.keeper_product_id, shop_code: action.target.shop_code, external_code: action.target.code,
      external_product_id: String(action.target.native_product_id), external_variant_id: action.target.native_variant_id == null
        ? null : String(action.target.native_variant_id), is_variant: action.target.is_variant,
      parent_code: action.target.is_variant ? action.target.parent_code : null, is_listed: true,
      shared_stock: action.target.shop_code === 'biketrek', sale_to_stock_factor: 1 })),
    history_hash_unchanged: 'unchanged-synthetic-history', physical_stock_changed: false,
    remote_write_performed: false, applied_by: { source: 'operator_token' } };
}
async function fakeFetch(path, init) {
  assert(path.startsWith('/api/product-consolidations/'), 'Only the reusable Hub consolidation API is called');
  assert.equal(init.cache, 'no-store'); assert.equal(init.credentials, 'same-origin'); assert.equal(init.redirect, 'error');
  assert.equal(init.headers.Authorization, 'Bearer ' + expectedToken); assert.equal(init.headers['X-Hub-Request'], '1');
  const body = init.body ? JSON.parse(init.body) : undefined;
  calls.push({ path, method: init.method, body });
  if (path.startsWith('/api/product-consolidations/selection?')) {
    assert.equal(init.method, 'GET');
    const ids = new URL(path, dom.window.location).searchParams.getAll('product_ids').map(Number);
    assert(ids.length >= 2 && ids.length <= 10 && new Set(ids).size === ids.length);
    return response(selection(ids));
  }
  if (path === '/api/product-consolidations/preview') {
    assert.equal(init.method, 'POST');
    assert.deepEqual(saved()?.request, body, 'The exact review is saved before a preview request can leave the browser');
    assert.equal(saved()?.apply, undefined);
    assert.equal(saved()?.discard, undefined);
    assert.equal(body.mode, 'identity_only_preserve_unconfirmed_history');
    assert.equal(body.same_physical_retail_unit_confirmed, true); assert.equal(body.legacy_initial_history_preserved, true);
    assert(body.mapping_actions.every(action => !action.target || action.target.sale_to_stock_factor === 1));
    assert.equal(body.additional_sales?.length || 0, 0);
    if (previewMode === 'invalid') return fail('consolidation_invalid_request', 422);
    if (previewMode === 'fenced_preview') return fail('consolidation_dependency_snapshot_changed', 409, true);
    if (previewMode === 'network') throw new TypeError('Synthetic preview response lost');
    if (requests.has(body.request_id)) assert.deepEqual(body, requests.get(body.request_id), 'A reused UUID preserves all review evidence');
    const batch = batches.get(body.request_id) || review(body);
    batches.set(batch.id, batch); requests.set(batch.id, clone(body));
    if (previewMode === 'pending') return new Promise(resolve => { releasePreview = () => resolve(response(batch)); });
    if (previewMode === 'lost_saved') throw new TypeError('Synthetic stored preview response lost');
    return response(batch);
  }
  const match = path.match(/^\/api\/product-consolidations\/([^/]+)(\/(?:apply|discard))?$/);
  assert(match, path);
  const batch = batches.get(match[1]);
  if (!batch) return fail('consolidation_not_found', 404);
  if (!match[2]) { assert.equal(init.method, 'GET'); return response(batch); }
  if (match[2] === '/discard') {
    assert.equal(init.method, 'POST');
    assert.deepEqual(body, { preview_hash: batch.preview_hash, confirmed: true });
    assert.deepEqual(saved()?.discard, body, 'The discard decision is persisted before expiring a prepared review');
    if (saved()?.apply) assert.deepEqual(saved().apply, body,
      'A deliberate stop retains the original uncertain apply and uses exactly the same review hash');
    assert.equal(saved()?.request.request_id, batch.id);
    if (batch.status === 'completed') return response(batch);
    if (discardMode === 'lost_prepared') throw new TypeError('Synthetic discard outcome unknown');
    if (discardMode === 'apply_won') complete(batch);
    else batch.status = 'expired';
    if (discardMode === 'lost_expired') throw new TypeError('Synthetic committed discard response lost');
    return response(batch);
  }
  assert.equal(init.method, 'POST');
  assert.deepEqual(body, { preview_hash: batch.preview_hash, confirmed: true });
  assert.deepEqual(saved()?.apply, body, 'The exact confirmation is saved before an apply request can leave the browser');
  assert.equal(saved()?.discard, undefined, 'An uncertain discard cannot be replaced by an apply decision');
  assert.equal(saved()?.request.request_id, batch.id);
  assert.equal(batch.eligible, true, 'A blocked dependency review cannot be submitted');
  if (batch.status === 'completed') return response(batch);
  if (applyMode === 'stale' || applyMode === 'expired' || applyMode === 'fenced_unavailable') {
    batch.status = 'expired';
    return fail(applyMode === 'stale' ? 'consolidation_identity_changed' : applyMode === 'expired'
      ? 'consolidation_preview_expired' : 'source_unavailable', applyMode === 'fenced_unavailable' ? 502 : 409, true);
  }
  if (applyMode === 'unfenced') return fail('consolidation_preview_changed');
  if (applyMode === 'pending') return new Promise((resolve, reject) => { releaseApply = () => reject(new TypeError('Synthetic uncertain apply')); });
  if (applyMode === 'lost_prepared') throw new TypeError('Synthetic apply result unconfirmed');
  complete(batch);
  if (applyMode === 'lost_completed') throw new TypeError('Synthetic committed apply response lost');
  return response(batch);
}
global.fetch = async (...args) => {
  try { return await fakeFetch(...args); }
  catch (error) {
    if (error.code === 'ERR_ASSERTION') fetchFailures.push(error);
    throw error;
  }
};
async function mount(rows = editorRows.slice(0, 2), strictMode = false) {
  document.getElementById('opener').focus();
  root = createRoot(document.getElementById('root'));
  const panel = React.createElement(ProductConsolidationPanel,
    { rows, onClose: () => closed++, onChanged: () => changed++ });
  await act(async () => { root.render(strictMode ? React.createElement(React.StrictMode, null, panel) : panel); await tick(); });
}
async function unmount() {
  if (!root) return;
  await act(async () => { root.unmount(); root = null; await tick(); });
}
async function fillEvidence(keeper = 1) {
  await click('consolidation-keeper-' + keeper);
  await input('consolidation-unit', physicalUnit);
  await input('consolidation-note', note);
  await input('consolidation-sources', sourceUrl);
  await check('consolidation-confirm-unit');
  await check('consolidation-confirm-history');
}
async function prepare(keeper = 1) { await fillEvidence(keeper); await click('consolidation-preview'); }
async function confirmApply() { await check('consolidation-confirm-plan'); await click('consolidation-apply'); }
async function scenario(name, run) {
  previewMode = applyMode = discardMode = 'valid'; selectionOverride = null; releasePreview = releaseApply = null;
  calls.length = 0; fetchFailures.length = 0; batches.clear(); requests.clear(); dom.window.sessionStorage.clear();
  expectedToken = 'synthetic-consolidation-token'; unlockHub(expectedToken); changed = closed = 0;
  try { await run(); if (fetchFailures.length) throw fetchFailures[0]; passed++; }
  catch (error) { error.message = name + ': ' + error.message; throw error; }
  finally { await unmount(); }
}

(async () => {
  await i18n.changeLanguage('sk');
  await scenario('Explicit card choice and complete exact mapping plan', async () => {
    await mount();
    assert.equal(previews().length, 0); assert.equal(writes().length, 0);
    assert.equal(reads().length, 1, 'Opening only reads the selected local card context');
    assert(!required('consolidation-keeper-1').checked && !required('consolidation-keeper-2').checked);
    assert(disabled('consolidation-preview'), 'The keeper must be explicitly selected');
    assert(document.body.textContent.includes('5901234123457') && document.body.textContent.includes('Modrá'));
    required('consolidation-mapping-103');
    await fillEvidence(2);
    assert.equal(required('consolidation-action-101').value, 'bind');
    assert.equal(required('consolidation-action-102').value, 'retain');
    assert.equal(required('consolidation-action-103').value, 'retain');
    await click('consolidation-preview');
    const request = previews().at(-1).body;
    assert.equal(request.keeper_product_id, 2); assert.deepEqual(request.retired_product_ids, [1]);
    assert.deepEqual(request.mapping_actions.map(action => action.mapping_id).sort(), [101, 102, 103]);
    const variant = request.mapping_actions.find(action => action.mapping_id === 102);
    assert.equal(variant.target.native_product_id, 2001); assert.equal(variant.target.native_variant_id, 2002);
    assert.equal(variant.target.code, 'SYN-RETAIL-M'); assert.equal(variant.target.parent_code, 'SYN-RETAIL');
    assert.equal(request.mapping_actions.find(action => action.mapping_id === 103).target.code, 'SYN-UNLISTED');
    assert(!request.preserve_keeper_invoice_receiving && !request.use_keeper_supplier_gtin,
      'The optional supplier and receiving evidence paths require a separate explicit choice');
    assert.equal(writes().length, 0, 'Preview does not merge cards');
    assert(disabled('consolidation-apply') && !required('consolidation-confirm-plan').checked);
    await confirmApply();
    required('consolidation-completed');
    assert.equal(changed, 1); assert.equal(saved(), null);
    assert.equal(batches.get(request.request_id).result.physical_stock_changed, false);
    await click('consolidation-done'); assert.equal(closed, 1);
  });

  await scenario('Evidence and both domain confirmations are required', async () => {
    await mount(); await fillEvidence();
    for (const [id, value] of [['consolidation-unit', 'pcs'], ['consolidation-note', 'Same shirt'],
      ['consolidation-sources', 'http://manufacturer.example/item'],
      ['consolidation-sources', 'https://user:private@manufacturer.example/item']]) {
      await input(id, value); await click('consolidation-preview');
      assert.equal(previews().length, 0, 'Invalid evidence cannot reach the server');
      await input(id, id === 'consolidation-unit' ? physicalUnit : id === 'consolidation-note' ? note : sourceUrl);
    }
    await check('consolidation-confirm-unit', false); await click('consolidation-preview'); assert.equal(previews().length, 0);
    await check('consolidation-confirm-unit'); await check('consolidation-confirm-history', false);
    await click('consolidation-preview'); assert.equal(previews().length, 0);
    await check('consolidation-confirm-history'); await click('consolidation-preview'); assert.equal(previews().length, 1);
  });

  await scenario('Dependency blockers keep the operation reviewable without applying', async () => {
    previewMode = 'blocked'; await mount(); await prepare();
    const blocker = required('consolidation-blockers');
    assert(blocker.textContent.length > 0); assert(disabled('consolidation-apply'));
    if (element('consolidation-confirm-plan')) await check('consolidation-confirm-plan');
    if (element('consolidation-apply')) await click('consolidation-apply');
    assert.equal(writes().length, 0);
    await click('consolidation-edit');
    assert(!disabled('consolidation-note'), 'A known blocked preview can be corrected');
    assert(disabled('consolidation-apply'));
  });

  await scenario('Editing requires a new proof and a new final confirmation', async () => {
    await mount(); await prepare();
    const first = clone(previews().at(-1).body);
    assert(disabled('consolidation-note') && disabled('consolidation-keeper-2'));
    await check('consolidation-confirm-plan'); await click('consolidation-edit');
    assert.equal(discards().length, 1, 'A prepared review must be expired server-side before replacing its proof');
    assert.equal(batches.get(first.request_id).status, 'expired');
    await input('consolidation-note', note + ' Independently reviewed again.');
    assert(disabled('consolidation-apply'));
    await click('consolidation-preview');
    const next = previews().at(-1).body;
    assert.notEqual(next.request_id, first.request_id); assert.notEqual(next.identity_evidence.review_note, first.identity_evidence.review_note);
    assert(!required('consolidation-confirm-plan').checked && disabled('consolidation-apply'));
  });

  await scenario('Missing native IDs are never invented and absence actions are explicit', async () => {
    selectionOverride = value => ({ ...value, mappings: value.mappings.map(row => row.id === 103
      ? { ...row, external_product_id: null } : row) });
    await mount(); await fillEvidence();
    assert.equal(required('consolidation-product-id-103').value, '');
    assert(disabled('consolidation-preview'), 'An unverified target cannot be guessed from its code or EAN');
    await input('consolidation-action-103', 'retire_unverified_code_absent');
    await check('consolidation-keep-receiving'); await check('consolidation-supplier-gtin');
    previewMode = 'invalid'; await click('consolidation-preview');
    const request = previews().at(-1).body;
    assert.deepEqual(request.mapping_actions.find(action => action.mapping_id === 103),
      { mapping_id: 103, action: 'retire_unverified_code_absent', target: null });
    assert.equal(request.preserve_keeper_invoice_receiving, true); assert.equal(request.use_keeper_supplier_gtin, true);
    assert.equal(writes().length, 0, 'The server still verifies absence and both opt-in proofs');
  });

  await scenario('Unknown discard cannot start a replacement review until the same operation settles', async () => {
    await mount(); await prepare(); const original = clone(previews()[0].body);
    discardMode = 'lost_prepared'; await click('consolidation-edit');
    required('consolidation-recovery'); assert(disabled('consolidation-preview') && disabled('consolidation-note'));
    assert.equal(previews().length, 1); assert.equal(discards().length, 1); assert.equal(writes().length, 0);
    const pending = clone(saved()), firstDiscard = clone(discards()[0]);
    await click('consolidation-recover');
    assert.deepEqual(saved(), pending, 'Prepared GET does not prove discard has completed');
    assert(disabled('consolidation-preview'));
    discardMode = 'valid'; await click('consolidation-retry-discard');
    assert.deepEqual(discards().at(-1), firstDiscard, 'Discard retries preserve the same UUID and preview hash');
    assert(!element('consolidation-recovery') && !disabled('consolidation-preview'));
    await click('consolidation-preview');
    const next = previews().at(-1).body;
    assert.notEqual(next.request_id, original.request_id);
    assert.deepEqual(next.retired_product_ids, original.retired_product_ids);
    assert(batches.get(next.request_id).eligible, 'The superseded review cannot block its replacement');
    assert.equal(writes().length, 0);
  });

  await scenario('A lost committed discard reopens as editable without repeating the decision', async () => {
    await mount(); await prepare(); discardMode = 'lost_expired'; await click('consolidation-edit');
    const original = clone(saved()); const discardCount = discards().length;
    assert(original?.discard); await unmount(); await mount([]);
    assert.equal(discards().length, discardCount); assert(!element('consolidation-recovery'));
    assert(!disabled('consolidation-preview')); assert.equal(writes().length, 0);
    await click('consolidation-preview');
    assert.notEqual(previews().at(-1).body.request_id, original.request.request_id);
    assert.deepEqual(previews().at(-1).body.retired_product_ids, original.request.retired_product_ids);
  });

  await scenario('Discard racing with a completed apply shows the completed result', async () => {
    await mount(); await prepare(); discardMode = 'apply_won'; await click('consolidation-edit');
    required('consolidation-completed'); assert.equal(changed, 1); assert.equal(saved(), null);
    assert.equal(previews().length, 1); assert.equal(writes().length, 0);
    assert(disabled('consolidation-preview'), 'A completed merge cannot be disguised as an editable discarded preview');
  });

  await scenario('A rejected preview permits corrections without losing the selected cards', async () => {
    previewMode = 'invalid'; await mount(); await prepare();
    assert(!element('consolidation-recovery')); assert(!disabled('consolidation-note'));
    assert.equal(saved(), null); assert.equal(writes().length, 0);
    previewMode = 'valid'; await click('consolidation-preview');
    required('consolidation-preview-result'); assert.equal(previews().length, 2);
  });

  await scenario('Unknown preview recovers or retries exactly the same request', async () => {
    previewMode = 'network'; await mount(); await prepare();
    const original = clone(previews().at(-1).body);
    required('consolidation-recovery'); assert(disabled('consolidation-note') && disabled('consolidation-preview'));
    assert(disabled('consolidation-retry-preview'), 'Retry waits for a missing-review GET');
    await click('consolidation-recover');
    previewMode = 'valid'; await click('consolidation-retry-preview');
    assert.deepEqual(previews().at(-1).body, original);
    required('consolidation-preview-result');
    assert(!required('consolidation-confirm-plan').checked);
  });

  await scenario('A rejected preview retry cannot abandon an original request that may still commit', async () => {
    previewMode = 'network'; await mount(); await prepare();
    const original = clone(previews()[0].body), pending = clone(saved());
    await click('consolidation-recover'); assert(!disabled('consolidation-retry-preview'));
    for (const mode of ['invalid', 'fenced_preview']) {
      previewMode = mode; await click('consolidation-retry-preview');
      required('consolidation-recovery'); assert(disabled('consolidation-preview') && disabled('consolidation-note'));
      assert.deepEqual(saved(), pending,
        'A rejected retry only describes its own transaction, not the earlier request that is still running');
      assert.deepEqual(previews().at(-1).body, original);
    }
    previewMode = 'valid';
    const delayedOriginal = review(original);
    batches.set(original.request_id, delayedOriginal); requests.set(original.request_id, clone(original));
    await click('consolidation-recover');
    required('consolidation-preview-result'); assert(!element('consolidation-recovery'));
    assert.equal(saved().request.request_id, original.request_id);
    assert(previews().every(call => call.body.request_id === original.request_id));
    assert.equal(writes().length, 0);
    await click('consolidation-edit');
    assert.equal(batches.get(original.request_id).status, 'expired');
    await click('consolidation-preview');
    assert.notEqual(previews().at(-1).body.request_id, original.request_id,
      'Only the server-side expiry fence permits a replacement UUID');
    assert.equal(discards().length, 1);
  });

  await scenario('A stored preview is recovered without posting a replacement', async () => {
    previewMode = 'lost_saved'; await mount(); await prepare();
    const original = clone(previews().at(-1).body); await unmount();
    await mount([]);
    assert.equal(previews().length, 1); assert.equal(writes().length, 0);
    required('consolidation-card-1'); required('consolidation-card-2');
    assert.equal(saved().request.request_id, original.request_id);
    assert(!required('consolidation-confirm-plan').checked);
  });

  await scenario('Duplicate clicks and prepared GET preserve uncertain apply identity', async () => {
    await mount(); await prepare(); await check('consolidation-confirm-plan'); applyMode = 'pending';
    await act(async () => { required('consolidation-apply').click(); required('consolidation-apply').click(); await tick(); });
    assert.equal(writes().length, 1, 'Immediate duplicate clicks send once');
    assert(disabled('consolidation-close'));
    await act(async () => { releaseApply(); await tick(); });
    const original = clone(writes()[0]); const pending = clone(saved());
    required('consolidation-recovery');
    await click('consolidation-recover');
    assert(disabled('consolidation-preview') && disabled('consolidation-note'));
    assert.deepEqual(saved(), pending, 'Prepared GET is not evidence that apply did not commit');
    applyMode = 'unfenced'; await click('consolidation-retry-apply');
    required('consolidation-recovery'); assert(disabled('consolidation-preview'));
    assert.deepEqual(saved(), pending, 'A conflict without a committed unapplied fence cannot release the original request');
    applyMode = 'valid'; await click('consolidation-retry-apply');
    assert.deepEqual(writes().at(-1), original, 'Every apply retry retains the same UUID and exact frozen hash');
    required('consolidation-completed'); assert.equal(changed, 1); assert.equal(saved(), null);
  });

  await scenario('A deliberately stopped uncertain apply retains both decisions until discard is settled', async () => {
    await mount(); await prepare(); applyMode = 'unfenced'; await confirmApply();
    assert(disabled('consolidation-discard-pending'), 'An apply rejection alone cannot silently choose a new operation');
    const original = clone(saved());
    await click('consolidation-recover'); assert(!disabled('consolidation-discard-pending'));
    discardMode = 'lost_prepared'; await click('consolidation-discard-pending');
    const stopped = clone(saved());
    assert.deepEqual(stopped.request, original.request); assert.deepEqual(stopped.apply, original.apply);
    assert.deepEqual(stopped.discard, original.apply);
    assert(disabled('consolidation-preview') && disabled('consolidation-retry-apply'));
    assert.equal(previews().length, 1); assert.equal(writes().length, 1);
    await unmount(); await mount(editorRows.slice(2));
    required('consolidation-recovery'); assert(disabled('consolidation-retry-apply'));
    assert.deepEqual(saved(), stopped, 'Reload keeps the original apply as well as the deliberate stop decision');
    assert(disabled('consolidation-preview'));
    discardMode = 'valid'; await click('consolidation-retry-discard');
    assert(!element('consolidation-recovery') && !disabled('consolidation-preview'));
    await click('consolidation-preview');
    assert.notEqual(previews().at(-1).body.request_id, original.request.request_id);
    assert.deepEqual(previews().at(-1).body.retired_product_ids, original.request.retired_product_ids);
    assert.equal(writes().length, 1);
  });

  await scenario('Stopping an uncertain apply reports completion when the original apply wins', async () => {
    await mount(); await prepare(); applyMode = 'unfenced'; await confirmApply();
    await click('consolidation-recover'); discardMode = 'apply_won'; await click('consolidation-discard-pending');
    required('consolidation-completed'); assert.equal(changed, 1); assert.equal(saved(), null);
    assert.equal(previews().length, 1); assert.equal(writes().length, 1);
    assert(disabled('consolidation-preview'));
  });

  await scenario('Reopening a committed lost result preserves its original scope', async () => {
    await mount(); await prepare(); applyMode = 'lost_completed'; await confirmApply();
    const original = clone(previews()[0].body), writeCount = writes().length;
    assert(saved()?.apply); const stored = dom.window.sessionStorage.getItem(PRODUCT_CONSOLIDATION_PENDING_KEY);
    assert(!stored.includes(expectedToken) && !stored.includes('Synthetic remote retail shirt') && !stored.includes('5901234123457'));
    await unmount(); await mount(editorRows.slice(2));
    required('consolidation-completed'); required('consolidation-card-1'); required('consolidation-card-2');
    assert(!element('consolidation-card-3') && !element('consolidation-card-4'));
    assert.equal(writes().length, writeCount); assert.equal(saved(), null);
    assert(calls.some(call => call.method === 'GET' && call.path.endsWith('/' + original.request_id)));
    assert.equal(changed, 1);
  });

  for (const mode of ['stale', 'expired', 'fenced_unavailable']) await scenario('Known unapplied ' + mode + ' allows a fresh proof', async () => {
    await mount(); await prepare(); const original = clone(previews()[0].body);
    applyMode = mode; await confirmApply();
    assert(!element('consolidation-recovery')); assert(!disabled('consolidation-preview'));
    assert(disabled('consolidation-apply')); assert(!saved()?.apply);
    applyMode = 'valid'; await click('consolidation-preview');
    const next = previews().at(-1).body;
    assert.notEqual(next.request_id, original.request_id);
    assert.equal(next.keeper_product_id, original.keeper_product_id);
    assert.deepEqual(next.retired_product_ids, original.retired_product_ids);
    assert(!required('consolidation-confirm-plan').checked);
  });

  await scenario('Global resume survives expiry and a rejected replacement preview', async () => {
    await mount(); await prepare(); const original = clone(previews()[0].body); await unmount();
    await mount([]); applyMode = 'expired'; await confirmApply();
    previewMode = 'invalid'; await click('consolidation-preview');
    assert(!disabled('consolidation-preview'));
    previewMode = 'valid'; await click('consolidation-preview');
    const next = previews().at(-1).body;
    assert.equal(next.keeper_product_id, original.keeper_product_id);
    assert.deepEqual(next.retired_product_ids, original.retired_product_ids);
    assert.notEqual(next.request_id, original.request_id);
  });

  await scenario('Pending apply stays frozen after reopening until its original result is resolved', async () => {
    await mount(); await prepare(); applyMode = 'lost_prepared'; await confirmApply();
    const original = clone(saved()); await unmount(); await mount(editorRows.slice(2));
    required('consolidation-recovery'); required('consolidation-card-1');
    assert(!element('consolidation-card-3') && disabled('consolidation-preview'));
    assert.deepEqual(saved(), original);
    applyMode = 'stale'; await click('consolidation-retry-apply');
    assert(!element('consolidation-recovery') && !disabled('consolidation-preview'));
    await click('consolidation-preview');
    assert.equal(previews().at(-1).body.keeper_product_id, original.request.keeper_product_id);
    assert.deepEqual(previews().at(-1).body.retired_product_ids, original.request.retired_product_ids);
  });

  await scenario('Elapsed preview time is not an unapplied fence but persisted expiry is', async () => {
    await mount(); await prepare(); applyMode = 'lost_prepared'; await confirmApply();
    const original = clone(saved()), batch = batches.get(original.request.request_id);
    batch.expires_at = '2000-01-01T00:00:00Z';
    await click('consolidation-recover');
    required('consolidation-recovery'); assert(disabled('consolidation-preview'));
    assert.deepEqual(saved(), original, 'Wall-clock expiry cannot settle an apply that may have started earlier');
    batch.status = 'expired';
    await click('consolidation-recover');
    assert(!element('consolidation-recovery') && !disabled('consolidation-preview'));
    assert(disabled('consolidation-apply') && !saved()?.apply);
    await click('consolidation-preview');
    assert.notEqual(previews().at(-1).body.request_id, original.request.request_id);
    assert.deepEqual(previews().at(-1).body.retired_product_ids, original.request.retired_product_ids);
  });

  await scenario('Session storage failure prevents preview, apply and discard transmission', async () => {
    await mount(); await fillEvidence();
    const prototype = dom.window.Storage.prototype, originalSet = prototype.setItem;
    try {
      prototype.setItem = function (key, value) {
        if (key === PRODUCT_CONSOLIDATION_PENDING_KEY) throw new dom.window.DOMException('Synthetic quota failure', 'QuotaExceededError');
        return originalSet.call(this, key, value);
      };
      await click('consolidation-preview'); assert.equal(previews().length, 0);
    } finally { prototype.setItem = originalSet; }
    await click('consolidation-preview'); await check('consolidation-confirm-plan'); const prior = clone(saved());
    try {
      prototype.setItem = function (key, value) {
        if (key === PRODUCT_CONSOLIDATION_PENDING_KEY) throw new dom.window.DOMException('Synthetic quota failure', 'QuotaExceededError');
        return originalSet.call(this, key, value);
      };
      await click('consolidation-apply'); assert.equal(writes().length, 0);
      assert.deepEqual(saved(), prior);
      await click('consolidation-edit'); assert.equal(discards().length, 0);
      assert.deepEqual(saved(), prior);
    } finally { prototype.setItem = originalSet; }
  });

  await scenario('Credential revision prevents a late preview from unlocking apply', async () => {
    previewMode = 'pending'; await mount(); await fillEvidence(); await click('consolidation-preview');
    assert.equal(previews().length, 1);
    await act(async () => { expectedToken = 'synthetic-other-operator'; unlockHub(expectedToken); releasePreview(); await tick(); });
    assert(disabled('consolidation-apply')); assert(!element('consolidation-preview-result'));
    assert.equal(changed, 0); assert.equal(writes().length, 0);
    assert(pendingProductConsolidation(), 'The original request is retained for a deliberate recovery');
  });

  await scenario('Out-of-scope card counts never issue a selection or write request', async () => {
    await mount([editorRows[0]]); assert.equal(calls.length, 0); assert(disabled('consolidation-preview')); await unmount();
    await mount(Array.from({ length: 11 }, (_, index) => ({ ...editorRows[0], id: index + 1 })));
    assert.equal(calls.length, 0); assert(disabled('consolidation-preview'));
  });

  await scenario('StrictMode starts one selection read and keyboard focus stays in the dialog', async () => {
    await mount(undefined, true);
    assert.equal(reads().length, 1, 'StrictMode effect replay does not start overlapping selection loads');
    assert.equal(previews().length, 0); assert.equal(writes().length, 0);
    required('consolidation-card-1'); required('consolidation-card-2');
    const close = required('consolidation-close');
    const special = document.querySelector('.consolidation-special');
    assert(special && !special.open);
    const summary = special.querySelector('summary');
    await act(async () => {
      close.focus();
      close.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Tab', shiftKey: true, bubbles: true, cancelable: true }));
    });
    assert.equal(document.activeElement, summary, 'Closed advanced fields are not part of the tab loop');
    await act(async () => {
      summary.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true }));
    });
    assert.equal(document.activeElement, close, 'Tab from the last visible control returns to the first');
    await act(async () => { summary.click(); await tick(); });
    assert(special.open);
    await act(async () => {
      close.focus();
      close.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Tab', shiftKey: true, bubbles: true, cancelable: true }));
    });
    assert.equal(document.activeElement, required('consolidation-supplier-gtin'), 'Opening advanced options makes their fields reachable');
    await unmount(); assert.equal(document.activeElement, document.getElementById('opener'));
  });

  unlockHub('');
  console.log(`Product consolidation UI: ${passed} behavior scenarios passed`);
})().catch(error => { console.error(error); process.exitCode = 1; });
