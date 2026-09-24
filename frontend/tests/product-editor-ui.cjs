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
const { MemoryRouter } = require('react-router-dom');
const { ProductEditorPage } = require('../src/pages/ProductEditorPage.tsx');
const { unlockHub } = require('../src/api/access.ts');
const { COLUMN_PREFERENCES_KEY, columnPreferences, loadColumnPreferences } = require('../src/pages/productEditorGrid.ts');
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
const makeRow = id => ({ id, sku: `SKU-${id}`, group: id < 3 ? { id: 10, code: 'BIKE', name: 'Fixture bike' } : null,
  attributes: [{ name: id === 1 ? 'Veľkosť' : 'Size', value: id === 1 ? 'M' : 'L' }, { name: id === 1 ? 'Farba' : 'Colour', value: 'Blue' }, { name: 'Wheel size', value: '29' }], eans: [`000000000${String(id).padStart(4, '0')}`],
  supplier_codes: [{ supplier_code: 'fixture-supplier', code: `SUP-${id}` }], image_url: null, revision: 3, snapshot_hash: hash(id, 3),
  common: { name: `Fixture product ${id}`, brand: 'Fixture', internal_note: '' },
  variant: { sale_price_gross: '199.90', vat_rate: '23.00', note: '' },
  warehouse: { code: 'main', location: `A-${String(id).padStart(2, '0')}`, min_quantity: '0' },
  stock: id === 2 ? { known: false, qty_on_hand: null, qty_reserved: null, qty_quarantined: null, qty_available: null, avg_cost: null, total_value: null }
    : { known: true, qty_on_hand: '3', qty_reserved: '3', qty_quarantined: '0', qty_available: '0', avg_cost: '0.0000', total_value: '0.0000' },
  shops: ['biketrek', 'xtrek'].map(shop_code => ({ shop_code, mapped: true, overrides: { name: null, sale_price_gross: null, visible: null },
    effective: { name: `Fixture product ${id}`, sale_price_gross: '199.90', visible: true },
    observed: { name: `Observed ${shop_code} ${id}`, price: '189.90', price_basis: 'unknown', visible: false }, state: 'inherited' })),
  overrides: { common: {}, variant: {}, warehouses: {}, shops: {} },
});
const rows = new Map(Array.from({ length: 51 }, (_, index) => { const row = makeRow(index + 1); return [row.id, row]; }));
const editorOptions = { shops: [{ id: 1, code: 'biketrek', name: 'BIKETREK' }, { id: 2, code: 'xtrek', name: 'xTrek' }],
  warehouses: [{ id: 7, code: 'main', name: 'Main fixture' }], brands: ['Fixture', 'Other'], page_sizes: [25, 50, 100], currency: 'EUR', price_basis: 'incl_vat' };
const calls = [], saves = new Map(), saveBodies = new Map();
let listMode = 'valid', saveMode = 'valid', conflictId = null, resolveList, rejectSave;
const saveCalls = () => calls.filter(call => call.path === '/api/product-editor/save');
const rowForWarehouse = (row, warehouseCode) => ({ ...clone(row), warehouse: warehouseCode ? clone(row.warehouse) : null });
global.fetch = async (path, init = {}) => {
  const body = init.body ? JSON.parse(init.body) : undefined;
  calls.push({ path, ...init, body });
  assert(path.startsWith('/api/product-editor/'), 'The grid only calls its protected local editing API');
  assert.equal(init.cache, 'no-store'); assert(init.headers.Authorization.startsWith('Bearer synthetic-'));
  assert(!path.includes('synthetic-'), 'Credentials never enter query strings');
  const url = new URL(path, dom.window.location.origin);
  if (url.pathname.endsWith('/options')) return reply(editorOptions);
  if (url.pathname.endsWith('/products')) {
    assert.equal(init.method, 'GET');
    const page = Number(url.searchParams.get('page')), pageSize = Number(url.searchParams.get('page_size'));
    assert([25, 50, 100].includes(pageSize));
    const warehouseCode = url.searchParams.get('warehouse_code');
    const query = (url.searchParams.get('q') || '').toLowerCase();
    const ordered = [...rows.values()].filter(row => (!url.searchParams.get('brand') || row.common.brand === url.searchParams.get('brand')) &&
      (!query || [row.sku, row.common.name, ...row.eans, ...row.supplier_codes.map(value => value.code)].some(value => value.toLowerCase().includes(query))));
    const snapshot = { items: ordered.slice((page - 1) * pageSize, page * pageSize).map(row => rowForWarehouse(row, warehouseCode)),
      total: ordered.length, page, page_size: pageSize, warehouse: warehouseCode ? { code: warehouseCode, name: 'Main fixture' } : null,
      currency: 'EUR', price_basis: 'incl_vat' };
    if (listMode === 'pending') return new Promise(resolve => { resolveList = () => resolve(reply(snapshot)); });
    return reply(snapshot);
  }
  const detailMatch = url.pathname.match(/^\/api\/product-editor\/products\/(\d+)$/);
  if (detailMatch) {
    assert.equal(init.method, 'GET');
    const row = rowForWarehouse(rows.get(Number(detailMatch[1])), url.searchParams.get('warehouse_code'));
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
const cellText = (id, field) => cell(id, field).querySelector('.product-editor-cell-value').textContent;
async function edit(id, field, value, commit = 'Enter') {
  await key(cell(id, field), 'Enter'); await input('cell-editor', value); await key('cell-editor', commit);
}
const focusedCell = () => document.activeElement.closest('[data-testid^="cell-"]')?.getAttribute('data-testid');

(async () => {
  unlockHub('');
  await act(async () => { root.render(React.createElement(MemoryRouter, { initialEntries: ['/products'] }, React.createElement(ProductEditorPage))); await tick(); });
  assert.equal(calls.length, 0, 'Mount never fetches protected product data');
  await input('unlock-token', 'synthetic-product-editor-token'); await click('unlock');
  assert.equal(calls.length, 0, 'Entering the shared token does not load or save automatically');
  await click('load');
  assert.equal(calls.length, 2, 'First explicit load requests options and one product page');
  assert.equal(calls[0].path, '/api/product-editor/options');
  assert.equal(new URL(calls[1].path, dom.window.location).searchParams.get('page_size'), '50');
  assert.equal(required('mode-common').getAttribute('aria-selected'), 'true');
  assert(cellText(1, 'available').includes('0'));
  assert(cellText(2, 'available').includes(t('unknown')), 'Unknown stock never silently becomes zero');
  assert(cellText(1, 'cost').includes('0.0000'), 'A known zero cost stays an exact decimal string');
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
  assert.equal(required('header-name').nextElementSibling, required('header-brand'));
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
  await click('detail-1'); await click('detail-tab-audit');
  assert.equal(required('movement-history').getAttribute('href'), '/stock/movements?sku=SKU-1');
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
  await click('columns'); await click('column-brand');
  assert(!element('cell-1-brand'));
  await paste(cell(1, 'name'), 'Visible columns only\t149.90');
  assert.equal(cellText(1, 'name'), 'Visible columns only');
  assert.equal(cellText(1, 'sale_price_gross'), '149.90', 'TSV advances across visible columns without silently inserting hidden columns');
  await click('column-brand'); await click('columns');
  assert.equal(cellText(1, 'brand'), 'Brand one', 'Hidden columns keep their earlier staged value');
  assert.equal(saveCalls().length, 0, 'Editing and pasting never save on their own');
  await click('discard'); assert.equal(cellText(1, 'name'), 'Fixture product 1'); assert(cannotUse('save'));
  await edit(1, 'sale_price_gross', '12.345');
  assert(cellText(1, 'sale_price_gross').includes('12.345') && cell(1, 'sale_price_gross').getAttribute('aria-invalid') === 'true', 'An invalid decimal remains visible for correction');
  const beforeInvalidSave = saveCalls().length; await click('save');
  assert.equal(saveCalls().length, beforeInvalidSave, 'Invalid decimals cannot be submitted');
  assert(document.body.textContent.includes(t('errors.invalid_price')));
  await click('detail-1'); await click('discard-row-1'); await click('close-detail');
  assert.equal(cellText(1, 'sale_price_gross'), '199.90'); assert(cannotUse('save'));

  await edit(1, 'sale_price_gross', '179,90');
  assert.equal(cellText(1, 'sale_price_gross'), '179.90');
  const beforeMode = calls.length; await click('mode-biketrek'); assert.equal(calls.length, beforeMode);
  await edit(1, 'shop_name', 'BIKETREK desired name'); await edit(1, 'shop_price', '159.95'); await edit(1, 'shop_visible', 'false');
  await click('mode-xtrek');
  assert(!cellText(1, 'shop_name').includes('BIKETREK desired name'), 'Shop drafts do not leak across channel modes');
  await edit(1, 'shop_name', 'xTrek desired name');
  await click('mode-common'); assert.equal(cellText(1, 'sale_price_gross'), '179.90', 'Common drafts survive channel-mode changes');
  await click('save');
  assert.deepEqual(saveCalls().at(-1).body.changes, [{ product_id: 1, expected_revision: 3, snapshot_hash: hash(1, 3),
    variant: { sale_price_gross: '179.90' }, shops: { biketrek: { name: 'BIKETREK desired name', sale_price_gross: '159.95', visible: false }, xtrek: { name: 'xTrek desired name' } } }]);
  assert(cannotUse('save'), 'A confirmed successful local save clears those drafts');

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
  await click('select-page'); await input('bulk-field', 'brand'); await input('bulk-value', 'Only current page'); await click('bulk-apply');
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

  await click('mode-biketrek'); await edit(1, 'shop_price', ''); await click('save');
  assert.deepEqual(saveCalls().at(-1).body.changes[0].shops, { biketrek: { sale_price_gross: null } }, 'Clearing an existing shop price sends an explicit null override');
  assert.equal(cellText(1, 'shop_price'), '179.90', 'Cleared shop price inherits the desired common sale price, not the observed external price');
  await click('mode-common');

  const beforeFilter = calls.length; await input('search', 'SUP-51'); await input('shop-filter', 'xtrek'); await input('page-size', '25');
  assert.equal(calls.length, beforeFilter, 'Filter edits are applied only by an explicit load');
  await click('load'); let query = new URL(calls.at(-1).path, dom.window.location).searchParams;
  assert.equal(query.get('q'), 'SUP-51'); assert.equal(query.get('shop_code'), 'xtrek'); assert.equal(query.get('page_size'), '25'); assert.equal(query.get('page'), '1');
  assert(element('cell-51-name') && !element('cell-1-name'));
  await input('search', ''); await input('shop-filter', ''); await input('page-size', '100'); await click('load');
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

  await edit(1, 'name', 'Secret unsaved draft'); listMode = 'pending'; await click('load');
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
  await act(async () => { root.render(React.createElement(MemoryRouter, null, React.createElement(ProductEditorPage))); await tick(); });
  assert.equal(calls.length, beforeRemount, 'Restoring column preferences does not load protected rows');
  await click('load');
  assert.equal(required('header-name').style.width, '375px');
  assert.equal(required('header-brand').nextElementSibling, required('header-name'));
  assert(element('cell-1-ean') && !element('cell-1-size'), 'Visibility, width and order survive a new page instance');
  await act(async () => { root.unmount(); await tick(); }); unlockHub('');
  const sanitized = columnPreferences({ version: 1, order: ['evil', 'brand', 'brand'], visible: ['name', 'constructor', 'name'], widths: { name: 10000, sku: -1, brand: '100', evil: 120 }, token: 'private' });
  assert.equal(sanitized.order[0], 'sku'); assert.equal(new Set(sanitized.order).size, sanitized.order.length);
  assert.deepEqual(sanitized.visible, ['sku', 'name']); assert.deepEqual(sanitized.widths, { sku: 80, name: 640 });
  dom.window.localStorage.setItem(COLUMN_PREFERENCES_KEY, '{invalid-json');
  assert.deepEqual(loadColumnPreferences(), columnPreferences(), 'Corrupt browser data falls back to valid default columns');
  console.log('Product editor UI behavior checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
