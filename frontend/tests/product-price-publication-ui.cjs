/* Synthetic publication API only. No live product, shop, price or stock writes. */
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
const React = require('react'); const { act } = React;
const { createRoot } = require('react-dom/client');
const { MemoryRouter } = require('react-router-dom');
const i18n = require('../src/i18n/index.ts').default;
const { unlockHub } = require('../src/api/access.ts');
const { ProductPricePublicationPanel } = require('../src/components/product/ProductPricePublicationPanel.tsx');
const { ProductPublicationPanel } = require('../src/components/product/ProductPublicationPanel.tsx');
const clone = value => JSON.parse(JSON.stringify(value));
const reply = value => ({ ok: true, json: async () => clone(value) });
const tick = () => new Promise(resolve => setTimeout(resolve, 5));
const el = id => document.querySelector(`[data-testid="${id}"]`);
const required = id => { const value = el(id); assert(value, `${id} exists`); return value; };
const disabled = id => !el(id) || el(id).disabled;
async function click(id) { await act(async () => { required(id).click(); await tick(); }); }
async function input(id, value) { await act(async () => {
  const target = required(id);
  Object.getOwnPropertyDescriptor(target.tagName === 'TEXTAREA' ? dom.window.HTMLTextAreaElement.prototype : dom.window.HTMLInputElement.prototype, 'value').set.call(target, value);
  target.dispatchEvent(new dom.window.Event('input', { bubbles: true })); await tick();
}); }
const makeRow = id => ({ id, sku: `FIXTURE-${id}`, revision: 3, snapshot_hash: `synthetic-source-${id}-3`,
  item_type: 'stock', stock_unit: 'pcs', is_active: true, supplier_linked: true, group: null, attributes: [], eans: ['1234567890123'], supplier_codes: [], image_url: null,
  common: { name: `Fixture ${id}`, brand: 'Fixture', internal_note: null },
  variant: { sale_price_gross: '29.90', vat_rate: '23.00', note: null }, warehouse: null,
  stock: { known: true, qty_on_hand: '5', qty_reserved: '1', qty_quarantined: '0', qty_available: '4', avg_cost: null, total_value: null },
  shops: ['biketrek', 'xtrek'].map((shop_code, index) => ({ shop_code, mapped: true,
    mapping: { code: `${shop_code}-${id}`, external_product_id: `${100 + id}`, external_variant_id: `${1000 + id}`, parent_code: `${shop_code}-parent`, is_variant: true },
    overrides: { name: null, sale_price_gross: index ? '34.90' : null, visible: null },
    effective: { name: `Fixture ${id}`, sale_price_gross: index ? '34.90' : '29.90', visible: true },
    observed: { name: 'Remote fixture', price: '20.00', price_basis: 'unknown', visible: false }, state: 'saved_unpublished' })), overrides: {}, audit: [],
});
const rows = new Map(Array.from({ length: 10 }, (_, index) => { const row = makeRow(index + 1); return [row.id, row]; }));
const calls = [], operations = new Map(), writes = [], busyEvents = [];
let nextOperation = 1, changed = 0, closeCalls = 0, historyError = false, releaseSend;
let modes = { biketrek: 'completed', xtrek: 'completed' };
global.fetch = async (path, init = {}) => {
  const body = init.body ? JSON.parse(init.body) : undefined;
  calls.push({ path, method: init.method, body, authorization: init.headers.Authorization });
  assert(path.startsWith('/api/product-editor/'), 'Only protected product publication APIs are used');
  assert.equal(init.cache, 'no-store'); assert(init.headers.Authorization.startsWith('Bearer synthetic-'));
  assert(!path.includes('synthetic-'), 'Credentials stay out of URLs');
  let match = path.match(/^\/api\/product-editor\/products\/(\d+)\/publications$/);
  if (match) {
    assert.equal(init.method, 'GET');
    if (historyError) throw new TypeError('Synthetic history unavailable');
    return reply({ items: [...operations.values()].filter(item => item.product_id === Number(match[1])).reverse() });
  }
  match = path.match(/^\/api\/product-editor\/products\/(\d+)\/publication\/preview$/);
  if (match) {
    const row = rows.get(Number(match[1]));
    assert.deepEqual(Object.keys(body).sort(), ['expected_revision', 'fields', 'shop_code']);
    assert.equal(body.expected_revision, row.revision, 'Only a saved current revision is previewed');
    assert.deepEqual(body.fields, ['sale_price_gross'], 'Price publication cannot include visibility, name, stock, or any other field');
    const price = row.shops.find(shop => shop.shop_code === body.shop_code).effective.sale_price_gross;
    const operation = { id: `price-operation-${nextOperation++}`, product_id: row.id, shop_code: body.shop_code,
      revision: row.revision, fields: clone(body.fields), state: 'ready', expires_at: '2099-12-31T23:59:59Z', error: null,
      before: { sale_price_gross: '24.60', price_original: '20.00', vat: '23.00', product_discount: '10', price_sale: null },
      after: { sale_price_gross: price, price_original: (Number(price) / 1.23).toFixed(2), vat: '23.00', product_discount: '10', price_sale: null } };
    operations.set(operation.id, operation); return reply(operation);
  }
  match = path.match(/^\/api\/product-editor\/publications\/([^/]+)(?:\/(send|resolve))?$/);
  assert(match, `Unexpected request ${path}`);
  const operation = operations.get(match[1]); assert(operation);
  if (match[2] === 'send') {
    assert.deepEqual(body, { confirmed: true });
    assert(!writes.includes(operation.id), 'A confirmed operation is never submitted twice');
    writes.push(operation.id);
    const mode = modes[operation.shop_code];
    if (mode === 'preflight') return { ok: false, status: 409, json: async () => ({ detail: { code: 'product_publication_remote_changed' } }) };
    if (mode === 'lost-before-intent') throw new TypeError('Synthetic transport failure before the intent becomes visible');
    operation.state = mode === 'deferred' ? 'sending' : mode === 'lost' || mode === 'uncertain' ? 'uncertain' : mode;
    if (mode === 'deferred') await new Promise(resolve => { releaseSend = () => { operation.state = 'completed'; resolve(); }; });
    if (mode === 'lost') throw new TypeError('Synthetic lost send response');
    return reply(operation);
  }
  if (match[2] === 'resolve') {
    assert.equal(body.confirmed, true); assert.equal(body.original_request_settled, true);
    assert(body.note.trim().length >= 10);
    assert(['sending', 'uncertain'].includes(operation.state));
    operation.state = 'resolved'; return reply(operation);
  }
  assert.equal(init.method, 'GET'); return reply(operation);
};
let root = createRoot(document.getElementById('root'));
const changedCallback = () => { changed++; };
const busyCallback = busy => { busyEvents.push(busy); };
async function render(row, key = String(row.id)) {
  await act(async () => { root.render(React.createElement(ProductPricePublicationPanel, { row, key, onChanged: changedCallback, onBusyChange: busyCallback })); await tick(); });
}
async function remount(row) {
  await act(async () => { root.unmount(); await tick(); });
  root = createRoot(document.getElementById('root')); await render(row);
}
async function prepareBoth() { await click('price-publication-both'); await click('price-publication-preview'); }
async function sendPrepared() { await click('price-publication-confirm'); await click('price-publication-send'); }
const writesFor = (product, shop) => writes.filter(id => { const operation = operations.get(id); return operation.product_id === product && operation.shop_code === shop; });

(async () => {
  unlockHub('synthetic-price-session');
  const original = clone(rows.get(1)); await render(rows.get(1));
  assert.equal(document.querySelectorAll('[role="dialog"]').length, 0, 'The price panel is embedded, never another modal');
  assert(disabled('price-publication-preview'), 'Targets are explicitly selected before a preview');
  await prepareBoth();
  assert.equal(calls.filter(call => call.path.endsWith('/publication/preview')).length, 2);
  assert.equal(writes.length, 0, 'Both comparisons are prepared without sending a price');
  assert(required('price-publication-result-biketrek').textContent.includes('29,90'));
  assert(required('price-publication-result-xtrek').textContent.includes('34,90'), 'Shop-specific saved price is shown honestly');
  assert(required('price-publication-result-biketrek').textContent.includes('23.00 %'));
  assert(required('price-publication-result-biketrek').textContent.includes('10 %'));
  assert(disabled('price-publication-send'), 'Publishing requires explicit confirmation of the real previews');
  await click('price-publication-confirm');
  await act(async () => { required('price-publication-send').click(); required('price-publication-send')?.click(); await tick(); });
  assert.equal(writes.length, 2); assert.equal(changed, 1, 'One batch refresh follows independently verified shop results');
  assert(!el('price-publication-partial'));
  assert(required('price-publication-state-biketrek').textContent.includes(i18n.t('productPublication.states.completed')));
  assert(disabled('price-publication-target-biketrek') && disabled('price-publication-target-xtrek'), 'Successful current prices cannot be replayed');
  assert.deepEqual(rows.get(1), original, 'Publication does not alter saved Hub fields or their revision in the browser');
  assert(busyEvents.includes(true) && busyEvents.at(-1) === false);
  operations.set('historical-ready', { ...clone([...operations.values()][0]), id: 'historical-ready', state: 'ready' });
  await remount(rows.get(1));
  assert(disabled('price-publication-target-biketrek'), 'Completed price is recovered from durable history after remount');
  await click('price-publication-history-historical-ready');
  assert(disabled('price-publication-target-biketrek'), 'Viewing an old preview cannot remove the successful current-price fence');
  rows.get(1).revision++; rows.get(1).snapshot_hash = 'synthetic-source-1-4';
  rows.get(1).variant.sale_price_gross = '30.90'; rows.get(1).shops[0].effective.sale_price_gross = '30.90';
  await render(rows.get(1)); await click('price-publication-target-biketrek'); await click('price-publication-preview');
  assert.equal(calls.filter(call => call.path.endsWith('/publication/preview')).at(-1).body.expected_revision, 4);
  const oldPreview = [...operations.values()].at(-1).id;
  rows.get(1).revision++; rows.get(1).snapshot_hash = 'synthetic-source-1-5'; rows.get(1).variant.sale_price_gross = '31.90'; rows.get(1).shops[0].effective.sale_price_gross = '31.90';
  await render(rows.get(1));
  assert(disabled('price-publication-send'), 'An older ready preview cannot send after a different price is saved');
  assert(!writes.includes(oldPreview));

  modes = { biketrek: 'completed', xtrek: 'uncertain' }; await render(rows.get(2)); await prepareBoth(); await sendPrepared();
  assert(el('price-publication-partial'), 'Partial success is visible explicitly');
  assert.equal(writesFor(2, 'biketrek').length, 1); assert.equal(writesFor(2, 'xtrek').length, 1);
  const beforeRecovery = writes.length; await click('price-publication-read-xtrek');
  assert.equal(writes.length, beforeRecovery, 'Reading an uncertain result does not repeat any shop write');
  await remount(rows.get(2));
  assert(required('price-publication-state-xtrek').textContent.includes(i18n.t('productPublication.states.uncertain')));
  assert(disabled('price-publication-target-biketrek') && disabled('price-publication-target-xtrek'));
  assert(disabled('price-publication-resolve-xtrek'));
  await input('price-publication-note-xtrek', 'Verified the original request has settled.');
  assert(disabled('price-publication-resolve-xtrek'), 'A note alone cannot acknowledge the settled request');
  await click('price-publication-settled-xtrek'); await click('price-publication-resolve-xtrek');
  assert.equal(writes.length, beforeRecovery, 'Explicit reconciliation reads the shop without publishing again');
  modes.xtrek = 'completed'; await click('price-publication-target-xtrek'); await click('price-publication-preview'); await sendPrepared();
  assert.equal(writesFor(2, 'biketrek').length, 1, 'An explicit new attempt for xTrek does not resend BIKETREK');
  assert.equal(writesFor(2, 'xtrek').length, 2);

  modes = { biketrek: 'completed', xtrek: 'lost' }; await render(rows.get(3)); await prepareBoth(); await sendPrepared();
  assert(el('price-publication-partial')); const lostWrites = writes.length;
  await remount(rows.get(3)); await click('price-publication-read-xtrek');
  assert.equal(writes.length, lostWrites); assert(disabled('price-publication-target-xtrek'));

  modes = { biketrek: 'deferred', xtrek: 'completed' }; await render(rows.get(4)); await prepareBoth(); await sendPrepared();
  assert(releaseSend); assert.equal(writesFor(4, 'biketrek').length, 1); assert.equal(writesFor(4, 'xtrek').length, 0);
  await act(async () => { unlockHub('synthetic-price-second-account'); await tick(); });
  assert(required('price-publication-state-biketrek').textContent.includes(i18n.t('productPublication.states.sending')));
  await act(async () => { releaseSend(); await tick(); });
  assert.equal(writesFor(4, 'xtrek').length, 0, 'Changing access stops the remaining batch and ignores the old response');
  await click('price-publication-read-biketrek'); assert(disabled('price-publication-target-biketrek'));

  modes = { biketrek: 'lost-before-intent', xtrek: 'completed' }; await render(rows.get(5));
  await click('price-publication-target-biketrek'); await click('price-publication-preview'); await sendPrepared();
  const lostReadyWrites = writes.length; await click('price-publication-read-biketrek');
  assert(disabled('price-publication-send') && disabled('price-publication-target-biketrek'), 'A ready read cannot prove a lost request will not start later');
  await remount(rows.get(5));
  await act(async () => { unlockHub('synthetic-price-third-account'); await tick(); });
  assert(el('price-publication-read-biketrek')); assert.equal(writes.length, lostReadyWrites, 'Remount/access changes retain the no-replay fence for a lost response');

  modes = { biketrek: 'preflight', xtrek: 'completed' }; await render(rows.get(6));
  await click('price-publication-target-biketrek'); await click('price-publication-preview'); await sendPrepared();
  assert(!el('price-publication-read-biketrek'), 'A structured preflight rejection is not a lost send result');
  assert(!disabled('price-publication-target-biketrek'), 'A rejected stale preview can be replaced by a new explicit comparison');
  assert(disabled('price-publication-send'), 'A rejected operation cannot be blindly sent again');

  const shared = rows.get(7); shared.shops[0].shared_stock = true; shared.shops[0].mapping = null;
  shared.shops[0].sale_mappings = [{ code: 'PACK-8', sale_to_stock_factor: '8', is_variant: true, parent_code: 'PACK-PARENT' }];
  shared.shops[1].mapped = false; shared.shops[1].mapping = null;
  await render(shared); assert(disabled('price-publication-both'));
  assert(disabled('price-publication-target-biketrek') && disabled('price-publication-target-xtrek'));
  assert(el('price-publication-unavailable-biketrek') && el('price-publication-unavailable-xtrek'));
  operations.set('foreign-workflow', { id: 'foreign-workflow', product_id: 7, shop_code: 'biketrek', revision: 3, state: 'uncertain', workflow: 'atomer_content', fields: ['name'], before: {}, after: {}, error: null });
  await remount(shared); assert(el('price-publication-read-biketrek'), 'Pending durable history is accessible even when current mapping is unsupported');
  assert(!el('price-publication-resolve-biketrek'), 'Price panel cannot resolve or send another workflow');

  historyError = true; await render(rows.get(8));
  assert(disabled('price-publication-both') && disabled('price-publication-preview'), 'Failure to load recovery history blocks new publication');
  historyError = false;
  await act(async () => { root.render(React.createElement(MemoryRouter, null, React.createElement(ProductPublicationPanel, {
    shop: 'biketrek', rows: [rows.get(9)], embedded: true, onClose: () => { closeCalls++; }, onBusyChange: busyCallback,
  }))); await tick(); });
  assert.equal(document.querySelectorAll('.product-editor-modal-backdrop').length, 0);
  assert.equal(document.querySelectorAll('[role="dialog"]').length, 0, 'Embedded general publication has no nested dialog');
  await act(async () => { document.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true })); await tick(); });
  assert.equal(closeCalls, 0, 'Embedded publication leaves Escape/focus management to the stock card');
  await click('publication-close'); assert.equal(closeCalls, 1);
  await act(async () => { root.render(React.createElement(MemoryRouter, null, React.createElement(ProductPublicationPanel, {
    shop: 'biketrek', rows: [rows.get(9)], onClose: () => { closeCalls++; },
  }))); await tick(); });
  assert.equal(document.querySelectorAll('.product-editor-modal-backdrop').length, 1);
  assert.equal(document.querySelectorAll('[role="dialog"]').length, 1, 'Existing standalone publication modal remains available');
  await act(async () => { document.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true })); await tick(); });
  assert.equal(closeCalls, 2);
  await act(async () => { root.unmount(); await tick(); }); unlockHub('');
  assert.equal(dom.window.localStorage.length, 0, 'Protected prices, operation bodies and credentials are not persisted in browser storage');
  console.log('Price publication UI: two shops, selected price only, saved revision, VAT details, partial/uncertain recovery, access changes and embedded general publication passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
