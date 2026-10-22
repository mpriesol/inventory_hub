/* Synthetic product editor API only. No live product, price or stock changes. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'https://hub.example.test/products' });
global.window = dom.window; global.document = dom.window.document;
Object.defineProperty(global, 'navigator', { value: dom.window.navigator, configurable: true });
global.HTMLElement = dom.window.HTMLElement; global.IS_REACT_ACT_ENVIRONMENT = true;
dom.window.HTMLElement.prototype.scrollIntoView = () => {};
for (const ext of ['.ts', '.tsx']) require.extensions[ext] = (module, filename) => {
  module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8').replace(/import\.meta\.env/g, '{}'), {
    compilerOptions: { jsx: ts.JsxEmit.React, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, esModuleInterop: true },
  }).outputText, filename);
};
require.extensions['.css'] = () => {};
const i18n = require('../src/i18n/index.ts').default;
const React = require('react'); const { act } = React;
const { createRoot } = require('react-dom/client');
const { createMemoryRouter, RouterProvider } = require('react-router-dom');
let router;
const renderEditor = () => { router = createMemoryRouter([{ path: '*', element: React.createElement(ProductEditorPage) }], { initialEntries: ['/products'] }); return React.createElement(RouterProvider, { router }); };
const { ProductEditorPage } = require('../src/pages/ProductEditorPage.tsx');
const { unlockHub } = require('../src/api/access.ts');
const { COLUMN_PREFERENCES_KEY, EDITOR_COLUMNS, columnPreferences, loadColumnPreferences, sortEditorRows } = require('../src/pages/productEditorGrid.ts');
let root = createRoot(document.getElementById('root'));
const tick = () => new Promise(resolve => setTimeout(resolve, 5));
const t = key => i18n.t(`productEditor.${key}`);
const element = id => document.querySelector(`[data-testid="${id}"]`);
const required = id => { const value = typeof id === 'string' ? element(id) : id; assert(value, `${id} exists`); return value; };
const cannotUse = id => !element(id) || element(id).disabled;
async function click(id) { await act(async () => { required(id).click(); await tick(); }); }
async function input(id, value) {
  await act(async () => {
    const target = required(id);
    const prototype = target.tagName === 'SELECT' ? dom.window.HTMLSelectElement.prototype : target.tagName === 'TEXTAREA' ? dom.window.HTMLTextAreaElement.prototype : dom.window.HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(prototype, 'value').set.call(target, value);
    target.dispatchEvent(new dom.window.Event(target.tagName === 'SELECT' ? 'change' : 'input', { bubbles: true }));
    await tick();
  });
}
async function key(id, value, options = {}) {
  await act(async () => { const target = required(id); target.focus(); target.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: value, bubbles: true, cancelable: true, ...options })); await tick(); });
}
async function paste(id, value) {
  await act(async () => {
    const target = required(id); target.focus();
    const event = new dom.window.Event('paste', { bubbles: true, cancelable: true });
    Object.defineProperty(event, 'clipboardData', { value: { getData: type => type === 'text/plain' ? value : '' } });
    target.dispatchEvent(event); await tick();
  });
}
const clone = value => JSON.parse(JSON.stringify(value));
const reply = value => ({ ok: true, json: async () => clone(value) });
const hash = (id, revision) => `${id.toString(16).padStart(4, '0')}${revision.toString(16).padStart(4, '0')}`.padEnd(64, 'a');
const makeRow = id => ({ id, item_type:'stock', stock_unit:'pcs', item_definition_locked:id===1, is_active: true, supplier_linked: true, sku: `SKU-${id}`, group: id < 3 ? { id: 10, code: 'BIKE', name: 'Fixture bike' } : null,
  attributes: [{ name: id === 1 ? 'Veľkosť' : 'Size', value: id === 1 ? 'M' : 'L' }, { name: id === 1 ? 'Farba' : 'Colour', value: 'Blue' }, { name: 'Wheel size', value: '29' }], eans: [`000000000${String(id).padStart(4, '0')}`],
  supplier_availability: { available: true, fresh: ![2, 3, 4, 5].includes(id), status: ({ 2: 'stale', 3: 'missing_link', 4: 'missing_observation', 5: 'conflict' })[id] || 'fresh', label: [2, 3, 4, 5].includes(id) ? 'overíme' : 'do 5 dní', source: 'fixture-supplier', quantity: id === 2 ? '900' : '6', quantity_kind: 'minimum', observed_at: null, expires_at: null, orderable: true },
  supplier_codes: [{ supplier_code: 'fixture-supplier', code: `SUP-${id}` }], image_url: id === 1 ? 'https://images.example.test/item.jpg' : null, revision: 3, snapshot_hash: hash(id, 3),
  common: { name: `Fixture product ${id}`, brand: 'Fixture', internal_note: '' },
  variant: { sale_price_gross: '199.90', vat_rate: '23.00', note: '' },
  warehouse: { code: 'main', location: `A-${String(id).padStart(2, '0')}`, min_quantity: '0' },
  stock: id === 2 ? { known: false, qty_on_hand: null, qty_reserved: null, qty_quarantined: null, qty_available: null, avg_cost: null, total_value: null }
    : { known: true, qty_on_hand: '3', qty_reserved: '3', qty_quarantined: '0', qty_available: '0', avg_cost: '0.0000', total_value: '0.0000' },
  shops: ['biketrek', 'xtrek'].map(shop_code => ({ shop_code, mapped: true, shop_url: `https://${shop_code}.example.test/product-${id}`, shop_admin_url: `https://${shop_code}.example.test/admin/product-${id}`, overrides: { name: null, sale_price_gross: null, visible: null },
    effective: { name: `Fixture product ${id}`, sale_price_gross: '199.90', visible: true },
    observed: { name: `Observed ${shop_code} ${id}`, price: '189.90', price_basis: 'unknown', visible: false }, state: 'inherited' })),
  overrides: { common: {}, variant: {}, warehouses: {}, shops: {} },
});
const rows = new Map(Array.from({ length: 51 }, (_, index) => { const row = makeRow(index + 1); return [row.id, row]; }));
const editorOptions = { shops: [{ id: 1, code: 'biketrek', name: 'BIKETREK' }, { id: 2, code: 'xtrek', name: 'xTrek' }],
  warehouses: [{ id: 8, code: 'other', name: 'Other fixture', is_default: false }, { id: 7, code: 'main', name: 'Main fixture', is_default: true }], brands: ['Fixture', 'Other'], page_sizes: [25, 50, 100], currency: 'EUR', price_basis: 'incl_vat' };
let loseCreateResponse = true; const createdRequests = new Map();
let skuPreviewMode = 'new', migrationMode = 'review', deferMigration = false, releaseMigration;
const migrationResult = body => ({created_at:'2026-10-21T16:00:00Z',preview_hash:'synthetic-preview-hash',can_apply:false,required_checks:['migration_supported_upgates_rename'],items:[{product_id:body.items[0].product_id,name:'Fixture product 1',old_sku:'SKU-1',proposed_sku:'TP-'+body.items[0].sku_identity.source_code,status:migrationMode,reasons:migrationMode==='blocked'?['migration_remote_native_id_changed']:[],candidate_product_ids:migrationMode==='blocked'?[1,2]:[],identity:{...body.items[0].sku_identity,barcodes:['5901234123457'],source_hash:'synthetic-source'},aliases:[{type:'internal_sku',value:'OLD-001',supplier_id:null,origin:'product_identifiers'}],mappings:[{shop_code:'biketrek',mapping_id:9,is_variant:true,old_code:'SKU-1',proposed_code:'TP-'+body.items[0].sku_identity.source_code,native_id:'9812',parent_code:'PARENT-1',parent_native_id:'4823',remote_status:body.verify_remote?'verified':'not_checked',reasons:[],remote_identity:body.verify_remote?{variant_id:'9812'}:null,remote_hash:body.verify_remote?'synthetic-remote':null}],impacts:{counts:{stock_movements:3},stock_state:'confirmed',requires_review:[],unverified:['advertising_feed_ids_not_inspected'],refresh_required:['catalog_identity_index']},remote_verified:body.verify_remote}]});
const skuBrands = [{ id: 1, code: 'TP', name: 'Topeak', kind: 'brand', aliases: [] }, { id: 2, code: 'SV', name: 'Workshop', kind: 'service', aliases: [] }];
const calls = [], saves = new Map(), saveBodies = new Map();
const publications = new Map(); let publicationWrites = 0, publicationMode = 'valid';
let listMode = 'valid', saveMode = 'valid', conflictId = null, resolveList, rejectSave, linkRefreshMode = 'complete', detailMode = 'valid', resolveDetail;
const saveCalls = () => calls.filter(call => call.path === '/api/product-editor/save');
const rowForWarehouse = (row, warehouseCode) => ({ ...clone(row), warehouse: warehouseCode ? clone(row.warehouse) : null });
global.fetch = async (path, init = {}) => {
  const body = init.body ? JSON.parse(init.body) : undefined;
  calls.push({ path, ...init, body });
  assert(['/api/product-editor/', '/api/product-links/refresh', '/api/supplier-card-links/options', '/api/sku-policy/'].some(prefix => path.startsWith(prefix)), 'The grid calls only its protected editing and explicit linking APIs');
  assert.equal(init.cache, 'no-store'); assert(init.headers.Authorization.startsWith('Bearer synthetic-'));
  assert(!path.includes('synthetic-'), 'Credentials never enter query strings');
  const url = new URL(path, dom.window.location.origin);
  if (url.pathname === '/api/sku-policy/migration-preview') {
    assert.equal(init.method,'POST'); assert.equal(body.items.length,1); assert.equal(body.items[0].product_id,1);
    assert.deepEqual(Object.keys(body).sort(),['items','verify_remote']);
    const value=migrationResult(body);
    if(deferMigration){deferMigration=false;return new Promise(resolve=>{releaseMigration=()=>resolve(reply(value));});}
    return reply(value);
  }
  if (url.pathname === '/api/sku-policy/brands') return reply({ brands: skuBrands });
  if (url.pathname === '/api/sku-policy/preview') {
    assert.equal(init.method, 'POST');
    if (skuPreviewMode === 'existing') return reply({status:'existing',sku:'OLD-001',product_id:51,brand_id:null,reasons:[],candidate_product_ids:[51]});
    if (skuPreviewMode === 'conflict') return reply({status:'conflict',sku:null,product_id:null,brand_id:null,reasons:['supplier_identifier_conflict'],candidate_product_ids:[1,2]});
    const item=body.sku_identity, valid=!!(item?.brand && item.source_code);
    return reply({status:valid?'new':'blocked',sku:valid?'TP-'+item.source_code:null,product_id:null,brand_id:valid?1:null,reasons:valid?[]:['sku_identity_required'],candidate_product_ids:[]});
  }
  if (url.pathname === '/api/product-links/refresh') {
    assert.equal(init.method, 'POST'); assert.deepEqual(body, { shop_code: 'xtrek', product_ids: [1] });
    if (linkRefreshMode === 'error') return { ok: false, status: 409, json: async () => ({ detail: { code: 'product_links_identity_changed' } }) };
    const shop = rows.get(1).shops.find(item => item.shop_code === 'xtrek');
    if (linkRefreshMode === 'complete') {
      shop.shop_url = 'https://xtrek.example.test/exact-variant'; shop.shop_admin_url = 'https://xtrek.example.test/admin/exact-variant';
    }
    return reply({ shop_code: 'xtrek', remote_requests: 1, rows: [{ product_id: 1, status: shop.shop_url && shop.shop_admin_url ? 'refreshed' : 'incomplete', shop_url: shop.shop_url, shop_admin_url: shop.shop_admin_url }] });
  }
  if (url.pathname === '/api/supplier-card-links/options') {
    assert.equal(init.method, 'GET'); assert.equal(url.searchParams.get('product_id'), '1');
    const row = rows.get(1);
    return reply({ product: { id: row.id, sku: row.sku, name: row.common.name, eans: row.eans, attributes: row.attributes },
      suppliers: [{ id: 8, code: 'fixture-supplier', name: 'Fixture supplier' }], current_links: [{
        id: 81, supplier_id: 8, supplier_code: 'fixture-supplier', supplier_name: 'Fixture supplier', supplier_sku: 'SUP-1', shop_sku: 'SUP-1', name: row.common.name,
        eans: ['8585047234448'], attributes: row.attributes, group_code: null, active: true, last_seen_at: null,
        link_id: 18, is_active: true, orderable: true, is_primary: true, priority: 0,
      }] });
  }
  if (url.pathname.endsWith('/options')) return reply(editorOptions);
  if (url.pathname.endsWith('/products') && init.method === 'POST') {
    assert(!('qty' in body) && !('stock' in body) && !('description' in body), 'Basic card save does not include physical stock or shop content');
    if (createdRequests.has(body.request_id)) {
      const old = createdRequests.get(body.request_id); assert.deepEqual(old.body, body); return reply(old.result);
    }
    const row = skuPreviewMode === 'existing' ? { ...makeRow(51), sku: 'OLD-001' } : { ...makeRow(100), sku: 'TP-'+body.sku_identity.source_code, item_type: body.item_type, stock_unit: body.stock_unit, common: { name: body.name, brand: null, internal_note: '' }, is_active: body.is_active, supplier_linked: false };
    rows.set(row.id, row);
    const result = { request_id: body.request_id, results: [{ product_id: row.id, status: 'saved', row, errors: [] }] };
    createdRequests.set(body.request_id, { body, result });
    if (loseCreateResponse) { loseCreateResponse = false; throw new TypeError('Lost create response'); }
    return reply(result);
  }
  if (url.pathname.endsWith('/products')) {
    assert.equal(init.method, 'GET');
    const page = Number(url.searchParams.get('page')), pageSize = Number(url.searchParams.get('page_size'));
    assert([25, 50, 100].includes(pageSize));
    const warehouseCode = url.searchParams.get('warehouse_code');
    const query = (url.searchParams.get('q') || '').toLowerCase();
    const ordered = [...rows.values()].filter(row =>
      (!['active', 'inactive'].includes(url.searchParams.get('activity')) || row.is_active === (url.searchParams.get('activity') === 'active')) &&
      (!['linked', 'unlinked'].includes(url.searchParams.get('supplier_link')) || row.supplier_linked === (url.searchParams.get('supplier_link') === 'linked')) && (!url.searchParams.get('brand') || row.common.brand === url.searchParams.get('brand')) &&
      (!query || [row.sku, row.common.name, ...row.eans, ...row.supplier_codes.map(value => value.code)].some(value => value.toLowerCase().includes(query))));
    const snapshot = { items: ordered.slice((page - 1) * pageSize, page * pageSize).map(row => rowForWarehouse(row, warehouseCode)),
      total: ordered.length, page, page_size: pageSize, warehouse: warehouseCode ? { code: warehouseCode, name: 'Main fixture' } : null,
      currency: 'EUR', price_basis: 'incl_vat' };
    if (listMode === 'incomplete' && page === 2) snapshot.items.shift();
    if (listMode === 'pending') return new Promise(resolve => { resolveList = () => resolve(reply(snapshot)); });
    return reply(snapshot);
  }
  const historyMatch = url.pathname.match(/^\/api\/product-editor\/products\/(\d+)\/publications$/);
  if (historyMatch) return reply({ items: [...publications.values()].filter(item => item.product_id === Number(historyMatch[1])).reverse() });
  const previewMatch = url.pathname.match(/^\/api\/product-editor\/products\/(\d+)\/publication\/preview$/);
  if (previewMatch) {
    assert.equal(init.method, 'POST');
    const product = rows.get(Number(previewMatch[1])); assert.equal(body.expected_revision, product.revision);
    assert.equal(body.shop_code, 'biketrek'); assert.deepEqual(body.fields, ['sale_price_gross', 'visible']);
    const publication = { id: `00000000-0000-4000-8000-${String(publications.size + 1).padStart(12, '0')}`, product_id: product.id,
      shop_code: body.shop_code, state: 'ready', revision: product.revision, fields: body.fields,
      before: { sale_price_gross: '189.90', visible: false }, after: { sale_price_gross: product.variant.sale_price_gross, visible: true },
      expires_at: '2026-12-31T12:00:00Z', created_at: '2026-09-24T12:00:00Z', error: null };
    publications.set(publication.id, publication); return reply(publication);
  }
  const publicationMatch = url.pathname.match(/^\/api\/product-editor\/publications\/([^/]+)(?:\/(send|resolve))?$/);
  if (publicationMatch) {
    const publication = publications.get(publicationMatch[1]); assert(publication);
    if (publicationMatch[2] === 'send') {
      assert.equal(init.method, 'POST'); assert.deepEqual(body, { confirmed: true }); publicationWrites++;
      publication.state = publicationMode === 'lost' ? 'uncertain' : 'completed';
      if (publicationMode === 'lost') throw new TypeError('Lost shop publication result');
    } else if (publicationMatch[2] === 'resolve') {
      assert.equal(body.original_request_settled, true); assert.equal(body.confirmed, true); assert(body.note.length >= 10); publication.state = 'resolved';
    } else assert.equal(init.method, 'GET');
    return reply(publication);
  }
  const detailMatch = url.pathname.match(/^\/api\/product-editor\/products\/(\d+)$/);
  if (detailMatch) {
    assert.equal(init.method, 'GET');
    const row = rowForWarehouse(rows.get(Number(detailMatch[1])), url.searchParams.get('warehouse_code'));
    if (detailMode === 'pending') return new Promise(resolve => { resolveDetail = () => resolve(reply({ ...row, audit: [] })); });
    const before = { common: { ...row.common, name: 'Audit previous name' }, variant: { ...row.variant, sale_price_gross: '109.90' },
      warehouse: clone(row.warehouse), shops: clone(row.shops), overrides: {}, snapshot_hash: hash(row.id, 1) };
    const after = { ...clone(before), common: { ...before.common, name: 'Audit current name' }, variant: { ...before.variant, sale_price_gross: '119.95' }, snapshot_hash: hash(row.id, 2) };
    return reply({ ...row, audit: [{ id: 81, request_id: '00000000-0000-4000-8000-000000000081', revision: 2,
      created_at: '2026-09-23T12:00:00Z', before, after }] });
  }
  if (url.pathname.endsWith('/save')) {
    assert.equal(init.method, 'POST'); assert.equal(body.confirmed, true); assert.equal(body.warehouse_code, 'main');
    assert.deepEqual(Object.keys(body).sort(), ['changes', 'confirmed', 'request_id', 'warehouse_code']);
    assert.match(body.request_id, /^[0-9a-f]{8}-[0-9a-f-]{27}$/i);
    if (saveBodies.has(body.request_id)) assert.deepEqual(body, saveBodies.get(body.request_id), 'A retry must preserve the exact request ID, snapshot and edits');
    else saveBodies.set(body.request_id, clone(body));
    if (saves.has(body.request_id)) return reply(saves.get(body.request_id));
    if (saveMode === 'missing') throw new TypeError('Synthetic connection lost before persistence');
    const results = body.changes.map(change => {
      const row = rows.get(change.product_id);
      assert.equal(change.expected_revision, row.revision); assert.equal(change.snapshot_hash, row.snapshot_hash);
      assert(!('stock' in change), 'Stock quantities and acquisition costs cannot be sent by this editor');
      if (change.product_id === conflictId) {
        const changed = { ...clone(row), revision: row.revision + 1, snapshot_hash: hash(row.id, row.revision + 1), common: { ...row.common, name: 'Concurrent server name' } };
        rows.set(row.id, changed);
        return { product_id: row.id, status: 'conflict', row: clone(changed), errors: [{ code: 'product_editor_changed' }] };
      }
      const changed = clone(row);
      if ('is_active' in change) changed.is_active = change.is_active;
      for (const field of ['item_type','stock_unit']) if (field in change) changed[field] = change[field];
      for (const scope of ['common', 'variant']) if (change[scope]) {
        Object.assign(changed[scope], change[scope]); Object.assign(changed.overrides[scope], change[scope]);
      }
      if (change.warehouse) {
        Object.assign(changed.warehouse, change.warehouse); changed.overrides.warehouses.main = { ...changed.overrides.warehouses.main, ...change.warehouse };
      }
      for (const target of changed.shops) target.effective = {
        name: target.overrides.name ?? changed.common.name,
        sale_price_gross: target.overrides.sale_price_gross ?? changed.variant.sale_price_gross,
        visible: target.overrides.visible ?? true,
      };
      if (change.shops) for (const [shop, overrides] of Object.entries(change.shops)) {
        const target = changed.shops.find(item => item.shop_code === shop); Object.assign(target.overrides, overrides);
        changed.overrides.shops[shop] = { ...changed.overrides.shops[shop] };
        for (const [field, value] of Object.entries(overrides)) {
          if (value === null) delete changed.overrides.shops[shop][field]; else changed.overrides.shops[shop][field] = value;
          target.effective[field] = value ?? (field === 'sale_price_gross' ? changed.variant.sale_price_gross : field === 'name' ? changed.common.name : true);
        }
        target.state = 'saved_unpublished';
      }
      changed.revision += 1; changed.snapshot_hash = hash(changed.id, changed.revision); rows.set(changed.id, changed);
      return { product_id: changed.id, status: 'saved', row: clone(changed), errors: [] };
    });
    const result = { request_id: body.request_id, status: 'completed', results, external_write_enabled: false }; saves.set(body.request_id, result);
    if (saveMode === 'pending') return new Promise((resolve, reject) => { rejectSave = () => reject(new TypeError('Synthetic connection lost')); });
    return reply(result);
  }
  const recoveryMatch = url.pathname.match(/^\/api\/product-editor\/saves\/([^/]+)$/);
  if (recoveryMatch) {
    assert.equal(init.method, 'GET');
    return saves.has(recoveryMatch[1]) ? reply(saves.get(recoveryMatch[1]))
      : { ok: false, status: 404, json: async () => ({ detail: { code: 'product_editor_save_missing' } }) };
  }
  throw new Error('Unexpected synthetic endpoint ' + path);
};
const cell = (id, field) => required(`cell-${id}-${field}`);
const cellText = (id, field) => cell(id, field).querySelector('.product-editor-cell-value').textContent.trim();
async function edit(id, field, value, commit = 'Enter') {
  await key(cell(id, field), 'Enter'); await input('cell-editor', value); await key('cell-editor', commit);
}
const focusedCell = () => document.activeElement.closest('[data-testid^="cell-"]')?.getAttribute('data-testid');

(async () => {
  assert.deepEqual(columnPreferences().visible.slice(0, 6), ['sku', 'image_url', 'name', 'available', 'on_hand', 'reserved']);
  dom.window.localStorage.setItem(COLUMN_PREFERENCES_KEY, JSON.stringify({ version: 1, order: EDITOR_COLUMNS.map(c => c.key), visible: EDITOR_COLUMNS.filter(c => !c.hidden).map(c => c.key), widths: {} }));
  unlockHub('');
  await act(async () => { root.render(renderEditor()); await tick(); });
  assert.equal(calls.length, 0, 'Mount never fetches protected product data');
  await input('unlock-token', 'synthetic-product-editor-token'); await click('unlock');
  assert.equal(calls.length, 2, 'Unlock loads options and the first page automatically');
  assert.equal(calls[0].path, '/api/product-editor/options');
  assert.equal(new URL(calls[1].path, dom.window.location).searchParams.get('page_size'), '50');
  assert.equal(new URL(calls[1].path, dom.window.location).searchParams.get('stock_scope'), 'all', 'All cards are discoverable by default, including unconfirmed stock');
  assert.equal(required('warehouse').value, 'main', 'The server-designated default is selected even when it is not first');
  assert.equal(new URL(calls[1].path, dom.window.location).searchParams.get('warehouse_code'), 'main');
  assert(!element('mode-common') && !element('mode-biketrek') && !element('mode-xtrek'), 'One unified grid replaces the three shop tabs');
  assert(element('cell-1-image_url') && element('cell-1-biketrek') && element('cell-1-xtrek'), 'Image and both shop-presence columns are visible by default');
  assert.equal(cell(1, 'image_url').querySelector('img').getAttribute('src'), 'https://images.example.test/item.jpg');
  assert.equal(cell(1, 'biketrek').querySelector('a').getAttribute('href'), 'https://biketrek.example.test/product-1');
  assert.equal(cell(1, 'xtrek').querySelectorAll('a')[1].getAttribute('href'), 'https://xtrek.example.test/admin/product-1');
  assert(cellText(1, 'supplier_quantity').includes('6+')); assert(!cellText(2, 'supplier_quantity').includes('900'), 'Stale supplier quantity is never presented as current');
  assert(cellText(2, 'supplier_availability').includes(t('supplierStatus.stale')));
  assert(cellText(3, 'supplier_availability').includes(t('supplierStatus.missing_link')), 'A missing supplier link is distinguished from expired data');
  assert(cellText(4, 'supplier_availability').includes(t('supplierStatus.missing_observation')));
  assert(cellText(5, 'supplier_availability').includes(t('supplierStatus.conflict')));
  assert(!cellText(3, 'supplier_quantity').includes('6'), 'An unresolved supplier link never exposes a quantity as current');
  assert(cellText(1, 'available').includes('0'));
  assert(cellText(2, 'available').includes(t('unknown')), 'Unknown stock never silently becomes zero');
  assert(cellText(1, 'cost') === '0', 'Known zero cost remains distinct from unknown');
  assert(element('new-card'), 'Unified card preparation is available from stock');
  assert(element('view-stock') && element('view-products') && element('view-shops'));
  await click(cell(1, 'name').querySelector('.product-editor-cell-value'));
  assert(element('cell-editor') && !element('close-detail'), 'Clicking the displayed name edits the cell instead of opening a modal');
  await input('cell-editor', 'Unsaved clicked name'); await key('cell-editor', 'Escape');
  assert.equal(cellText(1, 'name'), 'Fixture product 1');
  const beforeSort = calls.length; await click('sort-sale_price_gross');
  assert.equal(calls.length, beforeSort, 'Non-server columns sort only the loaded page');
  assert.equal(required('header-sale_price_gross').getAttribute('aria-sort'), 'ascending');
  assert(document.querySelector('.product-editor-local-sort').textContent.includes(t('sortedPage')));
  await click('sort-sale_price_gross'); assert.equal(required('header-sale_price_gross').getAttribute('aria-sort'), 'descending');
  await click(document.querySelector('.product-editor-local-sort button'));
  const { getEditorProducts } = require('../src/api/productEditor.ts');
  const filters = { q: '', brand: '', shop_code: '', warehouse_code: 'main', stock_scope: 'all', activity: 'all', supplier_link: 'all', page: 1, page_size: 500, sort: 'sku', direction: 'asc' };
  for (let id = 52; id <= 620; id++) rows.set(id, makeRow(id));
  const beforeLarge = calls.length;
  let large = await getEditorProducts(filters);
  assert.equal(large.items.length, 500); assert.equal(large.page_size, 500);
  assert.deepEqual(large.items.map(row => row.id), Array.from({ length: 500 }, (_, i) => i + 1));
  assert.equal(calls.length - beforeLarge, 5, 'A 500-row page uses five existing bounded GET requests');
  large = await getEditorProducts({ ...filters, page: 2, page_size: 250 });
  assert.deepEqual(large.items.map(row => row.id), Array.from({ length: 250 }, (_, i) => i + 251), '250-row pages align correctly across API page boundaries');
  large = await getEditorProducts({ ...filters, page: 2 });
  assert.deepEqual(large.items.map(row => row.id), Array.from({ length: 120 }, (_, i) => i + 501));
  listMode = 'incomplete'; await assert.rejects(getEditorProducts(filters), /Incomplete product list/); listMode = 'valid';
  for (let id = 52; id <= 620; id++) rows.delete(id);
  await input('page-size', '500');
  assert(element('cell-51-name') && cannotUse('next'), 'All matching products up to 500 can be displayed together');
  await input('page-size', '50');
  const cheap = { ...makeRow(1), variant: { sale_price_gross: '9.00' } };
  const expensive = { ...makeRow(2), variant: { sale_price_gross: '100.00' } };
  const unknown = { ...makeRow(3), variant: { sale_price_gross: null } };
  assert.deepEqual(sortEditorRows([unknown, expensive, cheap], 'sale_price_gross', 'asc', 'sk').map(row => row.id), [1, 2, 3]);
  assert.deepEqual(sortEditorRows([cheap, unknown, expensive], 'sale_price_gross', 'desc', 'sk').map(row => row.id), [2, 1, 3], 'Unknown prices remain unknown and last in either direction');
  assert(cannotUse('link-selected-products'), 'Linking requires explicit current-page selection');
  await click('select-1'); await click('select-2');
  const beforeLinkOpen = calls.length; await click('link-selected-products');
  assert(element('product-links-row-1') && element('product-links-row-2') && !element('product-links-row-3'), 'Bulk linking includes only the explicitly selected cards');
  assert.equal(calls.length, beforeLinkOpen, 'Opening linking does not run any shop request');
  await click('product-links-close'); await click('select-1'); await click('select-2');
  await click('detail-1'); await click('link-detail-product');
  assert(element('product-links-row-1') && !element('product-links-row-2'), 'The detail action links one concrete card');
  assert(!element('close-detail'), 'Only one modal focus trap stays open');
  await click('product-links-close');
  await input('warehouse', 'other'); await click('load');
  assert.equal(new URL(calls.at(-1).path, dom.window.location).searchParams.get('warehouse_code'), 'other', 'An explicit alternate warehouse is preserved');
  await input('warehouse', ''); await click('load');
  assert.equal(new URL(calls.at(-1).path, dom.window.location).searchParams.get('warehouse_code'), null, 'Explicit all-warehouses retains the aggregate API semantics');
  await act(async () => { unlockHub('synthetic-product-editor-refresh'); await tick(); }); await click('load');
  assert.equal(required('warehouse').value, '', 'Reloading options after a credential refresh does not replace explicit all-warehouses');
  assert.equal(new URL(calls.at(-1).path, dom.window.location).searchParams.get('warehouse_code'), null);
  await key(cell(1, 'sku'), 'F2'); assert(!element('cell-editor'), 'SKU identity stays read-only');
  await key(cell(1, 'available'), 'Enter'); assert(!element('cell-editor'), 'Stock cannot be edited through the grid');
  await key(cell(1, 'location'), 'Enter'); assert(!element('cell-editor'), 'Aggregate view cannot edit warehouse-specific fields');
  const beforeWarehouse = calls.length; await input('warehouse', 'main'); assert.equal(calls.length, beforeWarehouse);
  await click('load'); assert.equal(calls.length, beforeWarehouse + 1);
  assert.equal(new URL(calls.at(-1).path, dom.window.location).searchParams.get('warehouse_code'), 'main');

  assert.equal(cellText(1, 'size'), 'M'); assert.equal(cellText(2, 'size'), 'L');
  assert.equal(cellText(1, 'color'), 'Blue'); assert.equal(cellText(2, 'color'), 'Blue', 'Colour and size aliases share named columns without inventing product relationships');
  assert(cell(1, 'sku').querySelector('small').textContent.includes('Veľkosť: M'), 'Inline parameters have names, not just unexplained values');
  const beforeColumnEdits = calls.length;
  await key('resize-name', 'ArrowRight');
  assert.equal(required('header-name').style.width, '275px');
  await act(async () => {
    required('resize-name').dispatchEvent(new dom.window.MouseEvent('mousedown', { clientX: 200, button: 0, bubbles: true }));
    window.dispatchEvent(new dom.window.MouseEvent('mousemove', { clientX: 250, bubbles: true }));
    window.dispatchEvent(new dom.window.MouseEvent('mouseup', { bubbles: true })); await tick();
  });
  assert.equal(required('header-name').style.width, '325px', 'Mouse resize changes the actual table column width');
  await key('resize-name', 'Home'); assert.equal(required('header-name').style.width, '80px');
  await key('resize-name', 'End'); assert.equal(required('header-name').style.width, '640px');
  await click('columns'); await input('width-name', '345'); await click('column-up-brand');
  assert.equal(required('header-name').style.width, '345px');
  assert.equal(required('header-brand').nextElementSibling, required('header-name'), 'Column order is changed by accessible controls');
  await click('column-attributes'); await click('column-quarantined'); await click('column-channels');
  assert(cellText(1, 'attributes').includes('Wheel size: 29'));
  assert.equal(cellText(1, 'quarantined'), '0'); assert.equal(cellText(2, 'quarantined'), t('unknown'));
  assert.equal(cellText(1, 'channels'), 'BIKETREK, xTrek');
  await key(cell(1, 'quarantined'), 'Enter'); assert(!element('cell-editor'), 'Quarantine is an operation, never an editable quantity');
  await paste(cell(1, 'brand'), 'Reordered brand\tReordered name');
  assert.equal(cellText(1, 'brand'), 'Reordered brand'); assert.equal(cellText(1, 'name'), 'Reordered name', 'TSV follows the current visible column order');
  await click('reset-columns');
  assert.equal(cellText(1, 'name'), 'Reordered name', 'Resetting presentation does not discard draft data');
  assert.equal(required('header-name').style.width, '265px');
  assert.equal(required('header-name').nextElementSibling, required('header-available'));
  // Exercise the remaining spreadsheet cases using the user's saved broad view.
  for (const column of EDITOR_COLUMNS) if (!column.hidden && !required(`column-${column.key}`).checked) await click(`column-${column.key}`);
  for (const [index, column] of EDITOR_COLUMNS.entries()) {
    if (index < 1) continue;
    for (let step = 0; step < EDITOR_COLUMNS.length; step++) {
      const before = [...document.querySelectorAll('.product-editor-column-setting')].findIndex(node => node.querySelector(`[data-testid="column-${column.key}"]`));
      if (before <= index) break;
      assert(!required(`column-up-${column.key}`).disabled, `Can move ${column.key} from ${before} to ${index}`);
      await click(`column-up-${column.key}`);
      const after = [...document.querySelectorAll('.product-editor-column-setting')].findIndex(node => node.querySelector(`[data-testid="column-${column.key}"]`));
      assert(after < before, `${column.key} moved from ${before} to ${after}`);
    }
  }
  await click('columns'); await click('discard');
  assert.equal(calls.length, beforeColumnEdits, 'Column settings and clipboard edits do not call the server');

  await click('select-family-10');
  assert(required('select-1').checked && required('select-2').checked && !required('select-3').checked, 'Family selection targets exactly its loaded physical rows');
  await edit(2, 'brand', 'Hidden family draft'); await click('toggle-family-10');
  assert(!element('cell-1-name') && !element('cell-2-name')); assert(cannotUse('select-family-10'));
  assert(!element('bulk-apply'), 'Collapsing a family deselects hidden descendants');
  assert(required('family-10').textContent.includes(t('familyDrafts')), 'Collapsed families retain and disclose existing drafts');
  await paste(cell(3, 'name'), 'Visible three\nVisible four');
  assert.equal(cellText(3, 'name'), 'Visible three'); assert.equal(cellText(4, 'name'), 'Visible four');
  await click('select-page'); assert(required('select-3').checked);
  await click('toggle-family-10');
  assert(!required('select-1').checked && !required('select-2').checked, 'Select visible page cannot silently select collapsed descendants');
  assert.equal(cellText(2, 'brand'), 'Hidden family draft');
  await click('select-page'); await click('select-page'); await click('discard');

  await key(cell(1, 'name'), 'F2'); await input('cell-editor', 'Cancelled name'); await key('cell-editor', 'Escape');
  assert.equal(cellText(1, 'name'), 'Fixture product 1'); assert(cannotUse('save'));
  await edit(1, 'name', 'Staged name');
  assert.equal(required('row-1').getAttribute('data-dirty'), 'true'); assert.equal(cell(1, 'name').getAttribute('data-dirty'), 'true'); assert(!cell(1, 'brand').hasAttribute('data-dirty'), 'Row and exact modified cell are both marked');
  assert(cannotUse('link-selected-products'), 'Unsaved edits block the linking workflow');
  assert.equal(cellText(1, 'name'), 'Staged name'); assert(!cannotUse('save')); assert.equal(saveCalls().length, 0);
  await key(cell(1, 'name'), 'F2'); await input('cell-editor', 'Cancelled replacement'); await key('cell-editor', 'Escape');
  assert.equal(cellText(1, 'name'), 'Staged name', 'Escape keeps a previously staged value');
  await edit(1, 'name', 'Tab committed name', 'Tab');
  assert.equal(focusedCell(), 'cell-1-brand', 'Tab commits and advances to the next editable visible cell');
  if (element('cell-editor')) await key('cell-editor', 'Escape');
  await key(cell(1, 'name'), 'ArrowDown'); assert.equal(focusedCell(), 'cell-2-name');
  await key(cell(1, 'name'), 'ArrowRight'); assert.equal(focusedCell(), 'cell-1-brand');
  const originalCell = cell(1, 'name'), grid = originalCell.closest('table,[role="grid"]');
  assert(grid); const scrollContainer = grid.parentElement; scrollContainer.scrollTop = 147;
  await click('open-1-sku'); assert(document.querySelector('[role=dialog][aria-modal=true]'), 'SKU opens the stock-style popup');
  assert.equal(required('movement-history').getAttribute('href'), '/stock/movements?sku=SKU-1');
  assert(!required('detail-identifiers').open && !required('detail-shops').open && !required('detail-diagnostics').open, 'Advanced card information starts collapsed');
  await click('detail-tab-audit');
  const audit = document.querySelector('.product-editor-audit');
  assert(audit && audit.textContent.includes('Audit previous name') && audit.textContent.includes('Audit current name'), 'Audit history shows the actual before and after names');
  assert(audit.textContent.includes('109.90') && audit.textContent.includes('119.95') && audit.textContent.includes(t('fields.sale_price_gross')), 'Audit prices are paired with their edited field and retain exact decimal strings');
  const auditRows = [...required('audit-diff-81').querySelectorAll('tbody tr')];
  assert.equal(auditRows.length, 2, 'Audit differences omit unchanged fields');
  const auditPriceRow = auditRows.find(row => row.querySelector('th').textContent.includes(t('fields.sale_price_gross')));
  assert.deepEqual([...auditPriceRow.querySelectorAll('td')].map(cell => cell.textContent), ['109.90', '119.95'], 'Audit shows previous price before the saved price');
  assert(!audit.textContent.includes('snapshot_hash') && !audit.textContent.includes('"common":'), 'Audit changes use readable field differences instead of raw snapshot JSON');
  await click('close-detail');
  assert.equal(cell(1, 'name'), originalCell, 'Opening detail preserves the grid DOM');
  assert.equal(scrollContainer.scrollTop, 147); assert.equal(cellText(1, 'name'), 'Tab committed name');

  await paste(cell(1, 'name'), 'Pasted one\tBrand one\nPasted two\tBrand two');
  assert.equal(cellText(1, 'name'), 'Pasted one'); assert.equal(cellText(1, 'brand'), 'Brand one');
  assert.equal(cellText(2, 'name'), 'Pasted two'); assert.equal(cellText(2, 'brand'), 'Brand two');
  const beforeRejectedPaste = cellText(1, 'name');
  await paste(cell(1, 'sku'), 'CHANGED-SKU\tShould not apply');
  assert.equal(cellText(1, 'sku'), 'SKU-1'); assert.equal(cellText(1, 'name'), beforeRejectedPaste, 'A rectangle touching identity cells is rejected atomically');
  await paste(cell(50, 'name'), 'Last row changed\nOverflow row');
  assert.equal(cellText(50, 'name'), 'Fixture product 50', 'A paste cannot overflow the displayed page');
  await click('columns'); await click('column-brand'); await click('column-is_active'); await click('column-supplier_linked');
  assert(!element('cell-1-brand'));
  await paste(cell(1, 'name'), 'Visible columns only\t149.90');
  assert.equal(cellText(1, 'name'), 'Visible columns only');
  assert.equal(cellText(1, 'sale_price_gross'), new Intl.NumberFormat(i18n.language, { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(149.90), 'TSV advances across visible columns without silently inserting hidden columns');
  await click('column-brand'); await click('column-is_active'); await click('column-supplier_linked'); await click('columns');
  assert.equal(cellText(1, 'brand'), 'Brand one', 'Hidden columns keep their earlier staged value');
  assert.equal(saveCalls().length, 0, 'Editing and pasting never save on their own');
  await click('discard'); assert.equal(cellText(1, 'name'), 'Fixture product 1'); assert(cannotUse('save'));
  await edit(1, 'sale_price_gross', '12.345');
  assert(cellText(1, 'sale_price_gross').includes('12.345') && cell(1, 'sale_price_gross').getAttribute('aria-invalid') === 'true', 'An invalid decimal remains visible for correction');
  const beforeInvalidSave = saveCalls().length; await click('save');
  assert.equal(saveCalls().length, beforeInvalidSave, 'Invalid decimals cannot be submitted');
  assert(document.body.textContent.includes(t('errors.invalid_price')));
  await click('detail-1'); await click('discard-row-1'); await click('close-detail');
  assert.equal(cellText(1, 'sale_price_gross'), new Intl.NumberFormat(i18n.language, { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(199.90)); assert(cannotUse('save'));

  await edit(1, 'sale_price_gross', '179,90');
  assert.equal(cellText(1, 'sale_price_gross'), new Intl.NumberFormat(i18n.language, { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(179.90));
  const beforeMode = calls.length; await click('columns');
  for (const field of ['biketrek_name', 'biketrek_price', 'biketrek_visible', 'xtrek_name']) await click(`column-${field}`);
  await click('columns'); assert.equal(calls.length, beforeMode, 'Revealing shop-specific columns is local');
  await edit(1, 'biketrek_name', 'BIKETREK desired name'); await edit(1, 'biketrek_price', '159.95'); await edit(1, 'biketrek_visible', 'false');
  assert(!cellText(1, 'xtrek_name').includes('BIKETREK desired name'), 'Shop drafts stay in separate columns/scopes');
  await edit(1, 'xtrek_name', 'xTrek desired name');
  assert.equal(cellText(1, 'sale_price_gross'), new Intl.NumberFormat(i18n.language, { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(179.90), 'Shop edits preserve the common value');
  await click('save');
  assert.deepEqual(saveCalls().at(-1).body.changes, [{ product_id: 1, expected_revision: 3, snapshot_hash: hash(1, 3),
    variant: { sale_price_gross: '179.90' }, shops: { biketrek: { name: 'BIKETREK desired name', sale_price_gross: '159.95', visible: false }, xtrek: { name: 'xTrek desired name' } } }]);
  assert(cannotUse('save'), 'A confirmed successful local save clears those drafts');

  await edit(1, 'color', 'Black');
  assert.equal(cellText(1, 'color'), 'Black'); assert(!cell(1, 'size').hasAttribute('data-dirty'), 'Editing colour marks that attribute only');
  assert(required('row-1').hasAttribute('data-dirty'));
  await click('columns'); await click('column-attributes'); if (!required('column-ean').checked) await click('column-ean'); await click('columns');
  assert(cellText(1, 'attributes').includes('Wheel size: 29') && cellText(1, 'attributes').includes('Veľkosť: M'), 'Editing colour preserves other named attributes');
  await edit(1, 'attributes', 'broken text'); await edit(1, 'size', 'XL'); assert(cellText(1, 'attributes').includes('Veľkosť: XL'));
  await edit(1, 'ean', '12345678, 1234567890123'); assert.equal(cellText(1, 'ean'), '12345678, 1234567890123');
  await edit(1, 'image_url', 'javascript:alert(1)'); assert.equal(cell(1, 'image_url').getAttribute('aria-invalid'), 'true', 'Image editing rejects unsafe protocols');
  await click('select-1'); assert(cannotUse('upload-biketrek'), 'Upload is blocked until all local drafts are saved or discarded');
  await click('discard'); assert(!cannotUse('upload-biketrek'));
  await click('upload-biketrek');
  assert(element('publication-product')); assert(!required('publication-field-name').checked, 'Variant parent-only name is not implicitly published');
  assert.equal(publicationWrites, 0, 'Opening Upload reads local history without any shop write');
  await click('publication-preview');
  assert(required('publication-preview-result').textContent.includes('189.90') && required('publication-preview-result').textContent.includes('179.90'), 'Frozen preview pairs remote and desired values');
  assert(cannotUse('publication-send') && publicationWrites === 0, 'Preview alone never sends');
  await click('publication-confirm'); await click('publication-send');
  assert.equal(publicationWrites, 1); assert(cannotUse('publication-send'), 'A completed publication cannot be submitted again');
  await click('publication-preview'); await click('publication-confirm'); publicationMode = 'lost';
  await click('publication-send'); assert(element('publication-recover') && cannotUse('publication-send'));
  const sent = publicationWrites; await click('publication-recover');
  assert.equal(publicationWrites, sent, 'Reading a lost result never replays its shop PUT');
  assert(element('publication-settled')); await click('publication-settled'); await input('publication-resolution-note', 'Original remote request has been settled by support');
  await click('publication-resolve'); assert.equal(publicationWrites, sent, 'Resolving a settled remote request reads and records without resending');
  await click('publication-close'); publicationMode = 'valid';
  await click('columns'); await click('column-attributes'); await click('column-ean'); await click('columns');

  await edit(1, 'name', 'Local conflict draft'); await edit(2, 'brand', 'Saved second brand'); conflictId = 1;
  await click('save'); const conflictSaveCount = saveCalls().length;
  assert.equal(cellText(1, 'name'), 'Local conflict draft', 'A row conflict never overwrites the unsaved local value');
  await click('detail-1');
  assert(element('confirm-rebase-1'), 'Conflicts require an explicit choice before adopting the latest server version');
  const conflict = document.querySelector('.product-editor-conflict');
  assert(conflict.textContent.includes(t('fields.name')) && conflict.textContent.includes('Concurrent server name') && conflict.textContent.includes('Local conflict draft'),
    'Conflict review pairs each changed field with its current server value and the local draft');
  const conflictRow = required('conflict-diff').querySelector('tbody tr');
  assert(conflictRow.querySelector('th').textContent.includes(t('fields.name')));
  assert.deepEqual([...conflictRow.querySelectorAll('td')].map(cell => cell.textContent), ['Concurrent server name', 'Local conflict draft']);
  assert(!conflict.textContent.includes('{"name":') && !conflict.textContent.includes('expected_revision'), 'Conflict review does not expose raw patch JSON');
  assert.equal(cellText(2, 'brand'), 'Saved second brand');
  await tick(); assert.equal(saveCalls().length, conflictSaveCount, 'The grid never retries conflicting writes automatically');
  conflictId = null; await click('confirm-rebase-1'); await click('save');
  assert.deepEqual(saveCalls().at(-1).body.changes, [{ product_id: 1, expected_revision: 5, snapshot_hash: hash(1, 5), common: { name: 'Local conflict draft' } }],
    'Only the unresolved row is saved, using the revision explicitly accepted during rebase');
  await click('close-detail');

  await click('columns'); await click('column-internal_note'); await click('columns');
  await edit(1, 'internal_note', 'Page-one draft');
  await click('select-1'); await click('next');
  assert.equal(new URL(calls.at(-1).path, dom.window.location).searchParams.get('page'), '2');
  assert(!element('cell-1-name') && element('cell-51-name'));
  assert(!required('select-page').checked, 'Changing pages clears the previous row selection');
  await click('select-page'); assert(!element('bulk-field'), 'Bulk editing is hidden by default'); await click('columns'); await input('bulk-field', 'brand'); await input('bulk-value', 'Only current page'); await click('bulk-apply'); await click('columns');
  await click('save');
  const pageChanges = saveCalls().at(-1).body.changes;
  assert.deepEqual(pageChanges.map(change => change.product_id).sort((a, b) => a - b), [1, 51], 'Existing hidden drafts are retained while bulk selection is limited to the current page');
  assert.deepEqual(pageChanges.find(change => change.product_id === 1).common, { internal_note: 'Page-one draft' });
  assert.deepEqual(pageChanges.find(change => change.product_id === 51).common, { brand: 'Only current page' });
  await click('previous'); assert.equal(cellText(1, 'name'), 'Local conflict draft');

  await edit(1, 'brand', 'Recovered brand'); saveMode = 'pending'; const beforeLostSave = saveCalls().length;
  await act(async () => { required('save').click(); required('save').click(); await tick(); });
  assert.equal(saveCalls().length, beforeLostSave + 1, 'Immediate repeated clicks create only one save request');
  const lostSaveCount = saveCalls().length;
  await act(async () => { rejectSave(); await tick(); });
  assert(cannotUse('save') && element('recover-save'), 'A lost save response requires reading its persisted result');
  await tick(); assert.equal(saveCalls().length, lostSaveCount);
  await click('recover-save');
  assert(calls.at(-1).path.includes('/saves/') && calls.at(-1).method === 'GET');
  assert.equal(saveCalls().length, lostSaveCount, 'Recovery never replays the write');
  assert.equal(cellText(1, 'brand'), 'Recovered brand'); assert(cannotUse('save')); saveMode = 'valid';

  await edit(1, 'internal_note', 'Explicit retry note'); saveMode = 'missing'; await click('save');
  const missingRequest = clone(saveCalls().at(-1).body), missingSaveCount = saveCalls().length;
  assert(!saves.has(missingRequest.request_id) && cannotUse('save') && cannotUse('retry-save'), 'A lost request cannot be retried before checking persisted state');
  await click('recover-save');
  assert.equal(calls.at(-1).path, `/api/product-editor/saves/${missingRequest.request_id}`);
  assert(!cannotUse('retry-save') && cannotUse('save'), 'A confirmed missing saved result exposes only the explicit retry action');
  await tick(); assert.equal(saveCalls().length, missingSaveCount, 'A missing result never automatically starts another write');
  saveMode = 'valid';
  await act(async () => { required('retry-save').click(); required('retry-save').click(); await tick(); });
  assert.equal(saveCalls().length, missingSaveCount + 1, 'Explicit retry still rejects duplicate clicks');
  assert.deepEqual(saveCalls().at(-1).body, missingRequest, 'The explicit retry preserves its UUID and every original body field');
  assert.equal(cellText(1, 'internal_note'), 'Explicit retry note');
  assert(cannotUse('save') && !element('retry-save'), 'Successful retry resolves the retained draft and recovery state');

  await edit(1, 'biketrek_price', ''); await click('save');
  assert.deepEqual(saveCalls().at(-1).body.changes[0].shops, { biketrek: { sale_price_gross: null } }, 'Clearing an existing shop price sends an explicit null override');
  assert.equal(cellText(1, 'biketrek_price'), new Intl.NumberFormat(i18n.language, { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(179.90), 'Cleared shop price inherits the desired common sale price, not the observed external price');

  const beforeFilter = calls.length; await input('search', 'SUP-51'); await input('shop-filter', 'xtrek');
  assert.equal(calls.length, beforeFilter, 'Filter edits are applied only by an explicit load');
  assert(element('filters-pending'), 'Pending filters cannot be mistaken for applied results');
  await input('page-size', '25'); let query = new URL(calls.at(-1).path, dom.window.location).searchParams;
  assert.equal(query.get('q'), 'SUP-51'); assert.equal(query.get('shop_code'), 'xtrek'); assert.equal(query.get('page_size'), '25'); assert.equal(query.get('page'), '1');
  assert(element('cell-51-name') && !element('cell-1-name'));
  await input('search', ''); await input('shop-filter', ''); await input('page-size', '100');
  query = new URL(calls.at(-1).path, dom.window.location).searchParams; assert.equal(query.get('page_size'), '100');
  assert(element('cell-51-name') && element('cell-1-name') && cannotUse('next'));

  listMode = 'pending'; await input('search', 'SUP-1'); await click('load');
  const oldFilterList = resolveList, oldFilterRequest = calls.at(-1), beforeFilterEdit = calls.length;
  await input('search', 'SUP-51');
  assert(oldFilterRequest.signal.aborted && calls.length === beforeFilterEdit);
  await act(async () => { oldFilterList(); await tick(); });
  assert(element('cell-51-name'), 'A delayed result for superseded filters cannot replace the displayed page');
  listMode = 'valid'; await click('load'); assert(element('cell-51-name') && !element('cell-1-name'));
  await input('search', ''); await click('load');

  window.confirm = () => false;
  await key(cell(1, 'name'), 'Enter'); await input('cell-editor', 'Open unsaved cell');
  await act(async () => { await router.navigate('/stock/movements'); await tick(); });
  assert.equal(router.state.location.pathname, '/products', 'SPA navigation protects an open cell before Enter');
  await key('cell-editor', 'Escape');
  await edit(1, 'name', 'Secret unsaved draft');
  await act(async () => { await router.navigate('/stock/movements'); await tick(); });
  assert.equal(router.state.location.pathname, '/products', 'SPA navigation protects staged row edits');
  const lockButton = [...document.querySelectorAll('button')].find(button => button.textContent === t('lock'));
  await act(async () => { lockButton.click(); await tick(); });
  assert(cellText(1, 'name').includes('Secret unsaved draft'), 'Cancelling lock retains the draft');
  listMode = 'pending'; await click('load');
  const staleList = resolveList, staleRequest = calls.at(-1), beforeToken = calls.length;
  await act(async () => { unlockHub('synthetic-replacement-token'); await tick(); staleList(); await tick(); });
  assert(staleRequest.signal.aborted); assert.equal(calls.length, beforeToken, 'Credential changes do not reload protected data automatically');
  assert(!element('cell-1-name') && !document.body.textContent.includes('Secret unsaved draft'), 'Credential changes clear drafts and ignore delayed reads');
  assert.equal(dom.window.localStorage.length, 1); assert.equal(dom.window.sessionStorage.length, 0);
  const stored = dom.window.localStorage.getItem(COLUMN_PREFERENCES_KEY);
  assert(stored && !stored.includes('Secret') && !stored.includes('synthetic-') && !stored.includes('Fixture'), 'Only whitelisted presentation keys and widths persist, never credentials or product data');
  assert.deepEqual(Object.keys(JSON.parse(stored)).sort(), ['order', 'version', 'visible', 'widths']);
  assert(!dom.window.location.href.includes('synthetic-'));
  listMode = 'valid';
  await click('columns'); await input('width-name', '375'); await click('column-up-brand'); await click('column-ean'); await click('column-size');
  await act(async () => { root.unmount(); await tick(); });
  root = createRoot(document.getElementById('root'));
  const beforeRemount = calls.length;
  await act(async () => { root.render(renderEditor()); await tick(); });
  assert.equal(calls.length, beforeRemount + 2, 'Returning restores the list automatically');
  assert.equal(required('header-name').style.width, '375px');
  assert.equal(required('header-brand').nextElementSibling, required('header-name'));
  assert(element('cell-1-ean') && !element('cell-1-size'), 'Visibility, width and order survive a new page instance');

  // Repair is explicit and repaints both the table and the open detail without relinking.
  const repairRow = rows.get(1), repairShop = repairRow.shops.find(item => item.shop_code === 'xtrek');
  repairShop.shop_url = null; repairShop.shop_admin_url = null; repairRow.eans = [];
  repairRow.supplier_availability = { ...repairRow.supplier_availability, available: null, fresh: false, quantity: null, availability_state: 'unknown', status: 'not_loaded', supplier: 'fixture-supplier', supplier_sku: 'SUP-1', action: 'refresh', refresh_state: 'idle' };
  await click('load');
  assert(cellText(1, 'xtrek').includes(t('mapped')) && cellText(1, 'xtrek').includes(t('linkUnavailable')));
  assert.equal(required('supplier-settings-1').getAttribute('href'), '/settings/availability?supplier=fixture-supplier');
  assert(cellText(1, 'supplier_availability').includes(t('supplierStatus.not_loaded')));
  assert(!cellText(1, 'supplier_availability').includes('overíme'), 'Unknown availability does not pretend that a refresh is running');
  detailMode = 'pending'; await click('detail-1');
  const oldDetailRequest = calls.at(-1), oldDetailResponse = resolveDetail; detailMode = 'valid';
  assert(element('fill-supplier-eans'), 'An empty EAN has a direct supplier action');
  const beforeRefresh = calls.length, beforeRepair = clone(repairRow);
  await act(async () => { required('detail-refresh-links-1-xtrek').click(); required('detail-refresh-links-1-xtrek').click(); await tick(); });
  assert.equal(calls.slice(beforeRefresh).filter(item => item.path === '/api/product-links/refresh').length, 1, 'Repeated clicks start one metadata refresh');
  assert(oldDetailRequest.signal.aborted, 'Metadata refresh invalidates the older detail read');
  await act(async () => { oldDetailResponse(); await tick(); });
  assert.deepEqual(calls.slice(beforeRefresh).map(item => item.method), ['POST', 'GET']);
  assert.equal(cell(1, 'xtrek').querySelector('a').href, 'https://xtrek.example.test/exact-variant');
  assert(document.querySelector('[role="dialog"] a[href="https://xtrek.example.test/admin/exact-variant"]'), 'The open detail receives the refreshed links too');
  assert(!element('detail-refresh-links-1-xtrek') && !element('grid-refresh-links-1-xtrek'));
  assert.deepEqual(rows.get(1).stock, beforeRepair.stock); assert.deepEqual(rows.get(1).eans, []);
  assert.deepEqual(rows.get(1).shops.map(item => item.effective), beforeRepair.shops.map(item => item.effective));

  await click('close-detail'); repairShop.shop_url = null; repairShop.shop_admin_url = null; await click('load');
  linkRefreshMode = 'incomplete'; await click('grid-refresh-links-1-xtrek');
  assert(cellText(1, 'xtrek').includes(t('linksNotProvided')) && !cell(1, 'xtrek').querySelector('a'), 'A successful read without addresses does not claim repaired links');
  linkRefreshMode = 'error'; await click('grid-refresh-links-1-xtrek');
  assert(cellText(1, 'xtrek').includes(i18n.t('productLinks.errors.product_links_identity_changed')), 'Identity changes give a useful repair error');
  await edit(1, 'name', 'Unsaved while links missing');
  assert(cannotUse('grid-refresh-links-1-xtrek'), 'Metadata repair cannot replace a row while it has unsaved edits');
  assert.equal(required('supplier-settings-1').getAttribute('aria-disabled'), 'true', 'Supplier settings cannot discard unsaved edits through internal navigation');
  const blockedNavigation = new dom.window.MouseEvent('click', { bubbles: true, cancelable: true });
  await act(async () => { required('supplier-settings-1').dispatchEvent(blockedNavigation); await tick(); });
  assert(blockedNavigation.defaultPrevented, 'The disabled route prevents navigation');
  await click('detail-1'); assert(cannotUse('fill-supplier-eans')); await click('discard-row-1'); await click('close-detail');

  repairRow.supplier_availability = { ...repairRow.supplier_availability, fresh: true, available: false, quantity: '0', availability_state: 'unavailable', status: 'fresh', action: 'check_source' };
  await click('load');
  assert(cellText(1, 'supplier_quantity').includes('0') && cellText(1, 'supplier_availability').includes(t('supplierOutOfStock')), 'Fresh zero is clearly unavailable, not awaiting verification');
  await click('detail-1'); const beforeEanFill = calls.length; await click('fill-supplier-eans');
  assert(!element('close-detail') && document.querySelector('.supplier-links-panel'), 'EAN adoption opens one focused supplier dialog');
  assert.equal(calls.slice(beforeEanFill).filter(item => item.method === 'POST').length, 0, 'Opening EAN adoption never writes or prepares an implicit apply');
  assert(document.querySelector('[role="dialog"]').textContent.includes('8585047234448'), 'The existing supplier EAN is visible without searching the item again');
  await click('supplier-links-close');
  const stockRow = clone(rows.get(1));
  rows.set(1,{...stockRow,item_type:'service',stock:{...stockRow.stock,managed:false,known:false}}); await click('load');
  assert(!element('stock-action-1-on_hand'), 'Services never offer physical stock actions');
  await click('detail-1'); assert(!element('detail-tab-adjustment') && !element('detail-tab-fifo'), 'Service cards have no stock or FIFO tab');
  assert(required('detail-item-type').disabled && required('detail-stock-unit').disabled, 'Locked definitions cannot be changed from the detail');
  await click('close-detail'); rows.set(1,stockRow); await click('load');
  await click('detail-2'); await input('detail-stock-unit','m');
  assert(document.body.textContent.includes(t('meterLimit')), 'Meter quantities clearly disclose channel limitations');
  await click('detail-save'); assert.equal(saveCalls().at(-1).body.changes[0].stock_unit,'m');
  assert.equal(rows.get(2).stock_unit,'m');
  // Migration preparation is read-only, including the reused identity controls.
  const beforeMigration = calls.length, originalMigrationRow = clone(rows.get(1));
  await click('detail-1'); await click('sku-migration-open');
  assert(required('sku-migration-readonly').textContent.includes('nič neprečísluje'));
  assert.equal(document.querySelectorAll('[role="dialog"]').length,1,'Only the migration dialog owns keyboard focus');
  assert(required('sku-migration-local').disabled && required('sku-migration-remote').disabled);
  assert(!element('sku-register-toggle'),'Read-only preview cannot create registry entries');
  await input('sku-brand','Topeak'); await input('sku-source-code','0001410'); await input('sku-source-ref','Synthetic official variant evidence');
  await click('sku-leaf-confirmed');
  assert(!required('sku-migration-local').disabled);
  await click('sku-migration-local');
  const localMigration = calls.filter(call=>call.path==='/api/sku-policy/migration-preview').at(-1).body;
  assert.equal(localMigration.verify_remote,false); assert.equal(localMigration.items[0].sku_identity.source_code,'0001410');
  assert(required('sku-migration-result').textContent.includes('SKU-1') && required('sku-migration-result').textContent.includes('TP-0001410'));
  assert(required('sku-migration-result').textContent.includes('9812') && required('sku-migration-result').textContent.includes('4823'),'Native variant and parent IDs are explicit');
  assert(required('sku-migration-result').textContent.includes('OLD-001'),'Historical alias remains visible');
  assert(!required('sku-migration-remote').disabled && element('sku-migration-download'));
  await click('sku-migration-remote');
  assert.equal(calls.filter(call=>call.path==='/api/sku-policy/migration-preview').at(-1).body.verify_remote,true,'Remote reads require a separate explicit action');
  assert(required('sku-migration-result').textContent.includes(i18n.t('skuMigration.remoteVerified')));
  let downloadedBlob, downloadName;
  const oldCreateObjectURL=URL.createObjectURL, oldAnchorClick=dom.window.HTMLAnchorElement.prototype.click;
  URL.createObjectURL=blob=>{downloadedBlob=blob;return 'blob:synthetic-migration-preview';};
  dom.window.HTMLAnchorElement.prototype.click=function(){downloadName=this.download;};
  await click('sku-migration-download');
  URL.createObjectURL=oldCreateObjectURL;dom.window.HTMLAnchorElement.prototype.click=oldAnchorClick;
  assert.equal(downloadName,'sku-migration-preview-1.json');
  const exportedPreview=JSON.parse(await downloadedBlob.text());
  assert.equal(exportedPreview.can_apply,false);assert.equal(exportedPreview.preview_hash,'synthetic-preview-hash');
  assert.equal(exportedPreview.items[0].mappings[0].native_id,'9812');
  assert.equal(exportedPreview.items[0].remote_verified,true,'Downloaded evidence includes the actual read result');
  await input('sku-source-code','0001411');
  assert.equal(element('sku-migration-result'),null,'Changing evidence invalidates the old preview');
  assert(required('sku-migration-remote').disabled && required('sku-migration-local').disabled,'A changed variant requires fresh leaf confirmation and local preview');
  await click('sku-leaf-confirmed'); migrationMode='blocked'; await click('sku-migration-local');
  assert(required('sku-migration-result').textContent.includes(i18n.t('skuMigration.statuses.blocked')));
  assert(required('sku-migration-result').textContent.includes('1, 2'));
  const migrationCalls=calls.slice(beforeMigration);
  assert(!migrationCalls.some(call=>call.method==='POST' && call.path!=='/api/sku-policy/migration-preview'),'Preview never saves proof, cards, aliases, stock or registry entries');
  assert(!migrationCalls.some(call=>call.path==='/api/sku-policy/preview'),'Migration never calls the card-creation preview');
  assert.deepEqual(rows.get(1),originalMigrationRow,'The existing card and its state remain unchanged');
  deferMigration=true; await click('sku-migration-local'); await click('sku-migration-close');
  await act(async()=>{releaseMigration();await tick();});
  assert(!element('sku-migration-dialog') && !element('sku-migration-result'),'A late read cannot reopen the closed dialog');
  migrationMode='review';
  // Card activity is local, retained with supplier links, and independently filterable.
  const unchangedStock = clone(rows.get(1).stock), unchangedShops = clone(rows.get(1).shops);
  await edit(1, 'is_active', 'false'); await click('save');
  assert.equal(saveCalls().at(-1).body.changes[0].is_active, false);
  assert.equal(rows.get(1).is_active, false); assert.equal(rows.get(1).supplier_linked, true);
  assert.deepEqual(rows.get(1).stock, unchangedStock); assert.deepEqual(rows.get(1).shops, unchangedShops);
  await input('activity-filter', 'inactive'); await input('supplier-link-filter', 'linked'); await click('load');
  assert(cellText(1, 'is_active').includes(t('activityStates.inactive'))); assert(!element('cell-2-sku'));
  await input('supplier-link-filter', 'unlinked'); await click('load'); assert(!element('cell-1-sku'));
  assert(element('new-card'), 'The stock page exposes the unified local card action');
  // Brand prefixes are automatic; ordinary item codes need no invented MPN proof. Retries preserve the exact request.
  const { ProductCardCreateForm } = require('../src/components/product/ProductCardCreateForm.tsx');
  let createdCard;
  await act(async () => { root.render(React.createElement(ProductCardCreateForm, { onClose: () => {}, onCreated: row => { createdCard = row; } })); await tick(); });
  await input('create-name', 'Local demo'); await input('create-activity', 'false');
  assert(required('create-submit').disabled, 'A new card requires a real code, never an invented suffix');
  assert(element('sku-brand') && !element('sku-source-ref') && !element('sku-leaf-confirmed'), 'Brand and item code do not require a mandatory MPN proof form');
  await input('sku-brand','Topeak'); await input('sku-source-code', '0001410'); await input('create-stock-unit', 'pair');
  await act(async () => new Promise(resolve => setTimeout(resolve, 300)));
  assert(required('sku-preview').textContent.includes('TP-0001410'), 'The generated SKU preserves leading zeros with the registered prefix');
  await click('create-submit');
  assert(required('create-sku').disabled && required('create-stock-unit').disabled, 'An uncertain create preserves its exact payload');
  assert(required('create-submit').textContent.includes(t('retryCreate')));
  await click('create-submit');
  assert.equal(createdRequests.size, 1, 'Retry does not create another card');
  assert.equal(createdCard.sku, 'TP-0001410'); assert.equal(createdCard.is_active, false); assert.equal(createdCard.supplier_linked, false);
  const newRequest = [...createdRequests.values()][0].body;
  assert(!('sku' in newRequest),'The browser does not assign the new SKU'); assert.equal(newRequest.sku_identity.source_code,'0001410'); assert.equal(newRequest.sku_identity.leaf_confirmed,false,'An operational code is not fabricated MPN evidence');
  assert.equal(newRequest.item_type,'stock'); assert.equal(newRequest.stock_unit,'pair');
  await act(async () => { root.render(React.createElement(ProductCardCreateForm, {key:'service',onClose:()=>{},onCreated:row=>{createdCard=row;}})); await tick(); });
  await input('create-name', 'Workshop work'); await input('sku-brand','Workshop'); await input('sku-source-code','SERVICE'); await input('create-item-type','service');
  assert(!element('create-stock-unit'), 'Services do not offer a physical stock unit choice');
  await act(async () => new Promise(resolve => setTimeout(resolve, 300))); await click('create-submit');
  assert.equal(calls.filter(call=>call.path.endsWith('/sku-policy/preview')).at(-1).body.item_type,'service');
  assert.equal(createdCard.item_type,'service'); assert.equal(createdCard.stock_unit,'pcs');
  assert.equal([...createdRequests.values()].at(-1).body.sku_identity.code_kind,'service', 'Registered service namespaces work without an extra code-kind control');
  skuPreviewMode = 'existing';
  await act(async () => { root.render(React.createElement(ProductCardCreateForm, {key:'existing',onClose:()=>{},onCreated:row=>{createdCard=row;}})); await tick(); });
  await input('create-ean', '5901234123457');
  await act(async () => new Promise(resolve => setTimeout(resolve, 300)));
  assert(required('create-submit').textContent.includes(i18n.t('skuPolicy.useExisting')));
  assert.equal(calls.filter(call => call.path.endsWith('/sku-policy/preview')).at(-1).body.sku_identity, null, 'Existing-card lookup does not invent an MPN');
  await click('create-submit'); assert.equal(createdCard.id,51); assert.equal(createdCard.sku,'OLD-001');
  skuPreviewMode = 'conflict';
  await act(async () => { root.render(React.createElement(ProductCardCreateForm, {key:'conflict',onClose:()=>{},onCreated:()=>{throw Error('Conflict cannot save');}})); await tick(); });
  await input('create-name', 'Conflict'); await input('create-ean', '5901234123457');
  await act(async () => new Promise(resolve => setTimeout(resolve, 300)));
  assert(required('create-submit').disabled); assert(required('sku-preview').textContent.includes('1, 2'));
  assert(required('sku-preview').textContent.includes(i18n.t('skuPolicy.errors.supplier_identifier_conflict')));
  await act(async () => { root.unmount(); await tick(); }); unlockHub('');
  const sanitized = columnPreferences({ version: 1, order: ['evil', 'brand', 'brand'], visible: ['name', 'constructor', 'name'], widths: { name: 10000, sku: -1, brand: '100', evil: 120 }, token: 'private' });
  assert.equal(sanitized.order[0], 'sku'); assert.equal(new Set(sanitized.order).size, sanitized.order.length);
  assert(sanitized.visible.includes('sku') && sanitized.visible.includes('name') && sanitized.visible.includes('available') && !sanitized.visible.includes('constructor'), 'New default columns migrate into existing browser preferences'); assert.deepEqual(sanitized.widths, { sku: 80, name: 640 });
  dom.window.localStorage.setItem(COLUMN_PREFERENCES_KEY, '{invalid-json');
  assert.deepEqual(loadColumnPreferences(), columnPreferences(), 'Corrupt browser data falls back to valid default columns');
  console.log('Product editor UI behavior checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
