/* Real page and transport with synthetic data; no Atomer or production writes. */
const assert = require('node:assert/strict');
const fs = require('node:fs'), ts = require('typescript');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'https://hub.example.test/sync/atomer' });
global.window = dom.window; global.document = dom.window.document; global.sessionStorage = dom.window.sessionStorage;
Object.defineProperty(global, 'navigator', { value: dom.window.navigator, configurable: true });
global.HTMLElement = dom.window.HTMLElement; global.IS_REACT_ACT_ENVIRONMENT = true;
for (const ext of ['.ts', '.tsx']) require.extensions[ext] = (module, filename) => module._compile(ts.transpileModule(
  fs.readFileSync(filename, 'utf8').replace(/import\.meta\.env/g, '{}'),
  { compilerOptions: { jsx: ts.JsxEmit.React, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, esModuleInterop: true } }).outputText, filename);
require.extensions['.css'] = () => {};
const React = require('react'), { act } = React, { createRoot } = require('react-dom/client');
const { MemoryRouter } = require('react-router-dom');
const i18n = require('../src/i18n/index.ts').default;
const { AtomerAvailabilityPage } = require('../src/pages/AtomerAvailabilityPage.tsx');
const { unlockHub, setHubUser } = require('../src/api/access.ts');
const root = createRoot(document.getElementById('root'));
const tick = () => new Promise(resolve => setTimeout(resolve, 5));
const find = id => document.querySelector(`[data-testid="${id}"]`);
const required = id => { const element = find(id); assert(element, `${id} exists`); return element; };
const clone = value => JSON.parse(JSON.stringify(value));
const response = (value, status = 200) => ({ ok: status < 400, status, json: async () => clone(value) });
const batch = '33333333-3333-4333-8333-333333333333';
let options = { revision: 1, enabled: true, warehouse_code: 'main', source_configured: true, source_format: 'buyers', interval_seconds: 86400,
  last_started_at: null, last_completed_at: null, last_error: null, next_run_at: null,
  admin_source_configured: false, admin_complete: false, admin_upload: null, warehouses: [{ code: 'main', name: 'Main warehouse' }] };
let runs = [], createMode = 'ok', exportMode = 'ok', settingsMode = 'ok', editMode = 'ok', delayedRead, delayedDetail;
const calls = [], exportBodies = [], editBodies = [], configs = [], editResults = new Map();
const labels = ['SKLADOM', 'do 2 dní', 'do 3 dní', 'do 4 dní', 'do 5 dní', 'do 7 dní', 'overíme'];
const rows = [
  { code: 'PARENT', name: 'Family jacket', image: 'https://images.example.test/parent.jpg', kind: 'parent', parent_code: '', product_id: null, sku: null,
    current_visibility: false, current_availability: 'overíme', proposed_visibility: true, proposed_availability: 'overíme', qty_available: null, status: 'changed', reasons: [], manual_override: true },
  { code: 'VARIANT-M', name: 'Jacket M', image: 'https://images.example.test/variant.jpg', kind: 'variant', parent_code: 'PARENT', parent_name: 'Family jacket', parent_image: 'https://images.example.test/parent.jpg', product_id: 10, sku: 'BR-JACKET-M',
    current_visibility: false, current_availability: 'do 5 dní', proposed_visibility: true, proposed_availability: 'SKLADOM', qty_available: '2', status: 'changed', reasons: [] },
  { code: 'UNKNOWN', name: 'Unmatched product', image: null, kind: 'standalone', parent_code: '', product_id: null, sku: null,
    current_visibility: true, current_availability: 'overíme', proposed_visibility: true, proposed_availability: 'overíme', qty_available: null, status: 'unmatched', reasons: ['hub_product_missing'] },
];
function editable(row) { return row.manualAllowed || !['unmatched', 'blocked'].includes(row.status); }
function matches(row, filter) {
  return (filter.view === 'all' || !filter.view || (filter.view === 'changes' ? row.status === 'changed' : ['blocked', 'unmatched'].includes(row.status)))
    && (!filter.search || [row.name, row.code, row.sku].join(' ').toLowerCase().includes(filter.search.toLowerCase()))
    && (!filter.kind || filter.kind === 'all' || row.kind === filter.kind)
    && (!filter.status || filter.status === 'all' || row.status === filter.status)
    && (filter.include_in_export === undefined || String(row.include_in_export !== false) === String(filter.include_in_export))
    && (filter.manual_override === undefined || String(!!row.manual_override) === String(filter.manual_override));
}
function detail(run, params) {
  const offset = Number(params.get('offset') || 0), filter = Object.fromEntries(params), view = params.get('view') || 'all';
  const all = clone(run.rows || rows), selected = all.filter(row => matches(row, filter));
  const parents = new Set(selected.map(row => row.parent_code).filter(Boolean));
  const filtered = all.filter(row => selected.includes(row) || row.kind === 'parent' && parents.has(row.code));
  if (params.get('direction') === 'desc') filtered.reverse();
  return { ...clone(run), rows: filtered.slice(offset, offset + 100).map(row => ({ ...row, matches_filter: selected.includes(row),
    editable: run.status === 'ready' && run.trigger !== 'daily' && editable(row), include_in_export: row.include_in_export !== false })),
    total: filtered.length, matched_total: selected.length, selectable_total: selected.filter(editable).length, offset, view,
    filter_options: { availability: labels }, availability_options: labels };
}
function ready(id, receiving = null) { return { id, status: 'ready', revision: 2, trigger: receiving ? 'receiving' : 'manual', receiving_batch_id: receiving,
  created_at: '2026-11-16T15:00:00Z', source_fetched_at: '2026-11-16T15:00:01Z', source_sha256: 'a'.repeat(64), completed_at: null, error: null,
  summary: { total: 3, changed: 2, exportable: 2, blocked: 0, unmatched: 1 }, download_available: false, coverage: { kind: 'buyers_only', hidden_rows: 2, missing_parents: 0 }, warnings: [] }; }
let downloads = 0;
global.URL.createObjectURL = () => 'blob:synthetic-download'; global.URL.revokeObjectURL = () => {};
dom.window.HTMLAnchorElement.prototype.click = function () { if (this.download) downloads++; };
global.fetch = async (path, init = {}) => {
  const body = init.body && typeof init.body === 'string' ? JSON.parse(init.body) : undefined;
  const headers = new Headers(init.headers);
  assert.equal(init.credentials, 'same-origin'); assert.equal(init.cache, 'no-store'); assert.equal(init.redirect, 'error');
  assert.equal(headers.get('X-Hub-Request'), '1');
  assert(path.startsWith('/api/atomer-availability/'), 'Only the local Atomer export API may be called');
  calls.push({ path, body, method: init.method || 'GET' });
  const url = new URL(path, dom.window.location.href);
  if (url.pathname.endsWith('/options')) {
    if (delayedRead) { const wait = delayedRead; delayedRead = null; return new Promise(resolve => { wait.resolve = () => resolve(response(options)); }); }
    return response(options);
  }
  if (url.pathname.endsWith('/configure')) {
    configs.push(body);
    if (settingsMode === 'lost') { settingsMode = 'ok'; throw new TypeError('Synthetic lost configuration response'); }
    options = { ...options, enabled: body.enabled, warehouse_code: body.warehouse_code, revision: options.revision + 1, source_configured: true };
    return response(options);
  }
  if (url.pathname.endsWith('/runs') && init.method === 'POST') {
    assert(body.request_id); const old = runs.find(run => run.id === body.request_id);
    const value = old || ready(body.request_id, body.receiving_batch_id || null); if (!old) runs.unshift(value);
    if (createMode === 'lost') { createMode = 'ok'; throw new TypeError('Synthetic lost create response'); }
    return response(value);
  }
  if (url.pathname.endsWith('/runs')) return response({ items: runs });
  const match = /^\/api\/atomer-availability\/runs\/([^/]+)(?:\/(export|download|edit|edits\/[^/]+))?$/.exec(url.pathname);
  assert(match, path); const run = runs.find(item => item.id === match[1]);
  if (!run) return response({ detail: { code: 'atomer_availability_run_missing' } }, 404);
  if (match[2]?.startsWith('edits/')) {
    const result = editResults.get(match[2].slice(6));
    return result ? response(result) : response({ detail: { code: 'atomer_availability_edit_missing', known_unapplied: true } }, 404);
  }
  if (match[2] === 'edit') {
    editBodies.push(clone(body));
    if (editMode === 'before') { editMode = 'ok'; throw new TypeError('Synthetic transport failed before commit'); }
    if (editMode === 'rejected') { editMode = 'ok'; return response({ detail: { code: 'availability_value_missing', known_unapplied: true } }, 422); }
    if (!editResults.has(body.request_id)) {
      assert.equal(body.expected_revision, run.revision); run.rows = clone(run.rows || rows);
      const selected = run.rows.filter(row => (body.selection.all_filtered ? matches(row, body.selection.filter) : body.selection.codes.includes(row.code)) && !body.selection.excluded_codes?.includes(row.code) && editable(row));
      for (const row of selected) {
        if ('visibility' in body.changes) row.proposed_visibility = body.changes.visibility;
        if ('availability' in body.changes) { assert(labels.includes(body.changes.availability)); row.proposed_availability = body.changes.availability; }
        if ('include_in_export' in body.changes) row.include_in_export = body.changes.include_in_export;
        if ('apply_to_every_export' in body.changes) row.apply_to_every_export = body.changes.apply_to_every_export;
        row.status = row.current_visibility !== row.proposed_visibility || row.current_availability !== row.proposed_availability ? 'changed' : 'unchanged';
      }
      run.revision++;
      run.summary.exportable = run.rows.filter(row => row.status === 'changed' && row.include_in_export !== false).length;
      editResults.set(body.request_id, { request_id: body.request_id, id: run.id, revision: run.revision, updated: selected.length, skipped: 0, skipped_persistent: 0 });
    }
    if (editMode === 'lost') { editMode = 'ok'; throw new TypeError('Synthetic lost edit response after commit'); }
    return response(editResults.get(body.request_id));
  }
  if (match[2] === 'download') { assert(run.download_available); return { ok: true, blob: async () => new Blob(['synthetic csv']) }; }
  if (match[2] === 'export') {
    exportBodies.push(clone(body));
    if (exportMode === 'rejected') { exportMode = 'ok'; return response({ detail: { code: 'atomer_availability_override_blocked', known_unapplied: true } }, 422); }
    if (run.status !== 'exported') {
      assert.equal(body.expected_revision, run.revision);
      run.status = 'exported'; run.revision++; run.download_available = true; run.completed_at = '2026-11-16T15:01:00Z';
    }
    if (exportMode === 'lost') { exportMode = 'ok'; throw new TypeError('Synthetic lost export response after commit'); }
    return response(run);
  }
  const preview = detail(run, url.searchParams);
  if (delayedDetail?.view === preview.view) { const wait = delayedDetail; delayedDetail = null; return new Promise(resolve => { wait.resolve = () => resolve(response(preview)); }); }
  return response(preview);
};
async function render(key, path = '/sync/atomer') { await act(async () => { root.render(React.createElement(MemoryRouter, { key, initialEntries: [path], future: { v7_startTransition: true, v7_relativeSplatPath: true } }, React.createElement(AtomerAvailabilityPage))); await tick(); await tick(); }); }
async function click(id) { await act(async () => { const element = required(id); assert(!element.disabled, `${id} is enabled`); element.click(); await tick(); await tick(); }); }
async function input(id, value) { await act(async () => { const target = required(id), prototype = target.tagName === 'SELECT' ? dom.window.HTMLSelectElement.prototype : dom.window.HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(prototype, 'value').set.call(target, value); target.dispatchEvent(new dom.window.Event(target.tagName === 'SELECT' ? 'change' : 'input', { bubbles: true })); await tick(); await tick(); }); }

(async () => {
  await render('locked'); assert.equal(calls.length, 0, 'Locked page does not read private data');
  await act(async () => { unlockHub('synthetic-atomer-token'); await tick(); });
  assert.equal(required('atomer-feed-url').value, '', 'Saved feed credentials are not fetched into the form');
  await render('receiving', `/sync/atomer?receiving_batch=${batch}`);
  createMode = 'lost'; await click('atomer-create');
  const original = calls.findLast(call => call.method === 'POST' && call.path.endsWith('/runs'));
  assert.deepEqual(Object.keys(original.body).sort(), ['receiving_batch_id', 'request_id']);
  assert.equal(original.body.receiving_batch_id, batch, 'Receipt scope is passed without replacing its warehouse');
  assert(required('atomer-create').disabled, 'Unknown creation outcome prevents a new run');
  await click('atomer-recover-run');
  assert.equal(calls.filter(call => call.method === 'POST' && call.path.endsWith('/runs')).length, 1, 'Recovery reads the stored run without resubmitting');
  assert(required('atomer-coverage').textContent.includes('skrytých'));
  const tableRows = [...document.querySelectorAll('.atomer-table tbody tr')];
  assert(tableRows.findIndex(row => row.dataset.testid === 'atomer-row-PARENT') < tableRows.findIndex(row => row.dataset.testid === 'atomer-row-VARIANT-M'));
  assert.equal(required('atomer-row-VARIANT-M').querySelector('img').src, rows[1].image);
  assert(!find('atomer-persist-PARENT'), 'Parent context cannot persist a stock-card override');
  assert(required('atomer-row-PARENT').textContent.includes(i18n.t('atomerAvailability.edited')));
  assert(!required('atomer-row-PARENT').textContent.includes(i18n.t('atomerAvailability.manualOverride')), 'Draft-only parent edit is not described as a saved stock-card override');
  assert.equal(required('atomer-availability-VARIANT-M').tagName, 'SELECT', 'Availability is chosen from the dictionary, not arbitrary text');
  assert.equal(required('atomer-view').value, 'changes'); assert(!find('atomer-row-UNKNOWN'));
  await input('atomer-view', 'issues'); assert(required('atomer-visible-UNKNOWN').disabled);
  await input('atomer-view', 'changes');
  await click('atomer-include-VARIANT-M');
  assert.equal(required('atomer-visible-VARIANT-M').checked, true, 'Excluding an export row does not hide the product');
  assert.deepEqual(editBodies.at(-1).changes, { include_in_export: false });
  await click('atomer-include-VARIANT-M');
  await click('atomer-visible-VARIANT-M'); await input('atomer-availability-VARIANT-M', 'overíme'); await click('atomer-persist-VARIANT-M');
  await render('reload-edits', `/sync/atomer?receiving_batch=${batch}&run=${original.body.request_id}`);
  assert.equal(required('atomer-visible-VARIANT-M').checked, false); assert.equal(required('atomer-availability-VARIANT-M').value, 'overíme');
  assert.equal(required('atomer-persist-VARIANT-M').checked, true, 'Persistent intent survives reload in server draft');
  const savedRevision = runs.find(run => run.id === original.body.request_id).revision;
  exportMode = 'lost'; await click('atomer-export');
  assert.deepEqual(exportBodies[0], { expected_revision: savedRevision, overrides: [] }, 'Export reuses durable edits across pages');
  assert(required('atomer-export').disabled); assert(required('atomer-visible-VARIANT-M').disabled);
  await click('atomer-recover-export'); assert.equal(exportBodies.length, 1); assert(required('atomer-download')); assert(!find('atomer-export'));
  assert(required('atomer-visible-VARIANT-M').disabled, 'Generated CSV is immutable');
  assert(document.body.textContent.includes('ručne do Atomeru')); await click('atomer-download'); assert.equal(downloads, 1);
  await click('atomer-tab-history'); assert(required('atomer-preview').hidden, 'CSV history is separate from the preview editor');
  await click('atomer-tab-preview'); assert(!required('atomer-preview').hidden);

  await render('settings'); assert(!find('atomer-preview'), 'Default editor does not open an immutable CSV as an editable preview');
  await input('atomer-feed-url', 'https://source.example.test/private-feed.xml'); settingsMode = 'lost'; await click('atomer-save-settings');
  assert(required('atomer-feed-url').disabled); await click('atomer-save-settings');
  assert.deepEqual(configs[0], configs[1]); assert.equal(required('atomer-feed-url').value, '');
  assert(!Object.values(sessionStorage).some(value => value.includes('private-feed.xml')));

  const uncertain = ready('44444444-4444-4444-8444-444444444444'); runs.unshift(uncertain);
  await render('uncertain-edit', `/sync/atomer?run=${uncertain.id}`);
  editMode = 'lost'; await input('atomer-availability-VARIANT-M', 'do 3 dní');
  const editCount = editBodies.length;
  assert(required('atomer-export').disabled); assert(required('atomer-visible-VARIANT-M').disabled);
  await render('reload-uncertain-edit', `/sync/atomer?run=${uncertain.id}`);
  assert(required('atomer-export').disabled, 'Uncertain edit recovery survives browser reload');
  await click('atomer-recover-edit'); assert.equal(editBodies.length, editCount, 'Committed edit recovered by GET, not another POST');
  assert.equal(required('atomer-availability-VARIANT-M').value, 'do 3 dní');
  editMode = 'before'; await input('atomer-availability-VARIANT-M', 'do 2 dní');
  const unsavedEdit = clone(editBodies.at(-1)); await click('atomer-recover-edit'); await click('atomer-retry-edit');
  assert.deepEqual(editBodies.at(-1), unsavedEdit, 'Only a confirmed absent edit retries with its exact UUID/payload');
  editMode = 'rejected'; await input('atomer-availability-VARIANT-M', 'do 7 dní');
  assert(!required('atomer-availability-VARIANT-M').disabled, 'Definite rejection permits correction');

  const changingFilter = ready('99999999-9999-4999-8999-999999999999');
  changingFilter.rows = ['A', 'B', 'C'].map(code => ({ ...clone(rows[1]), code, name: code, kind: 'standalone', parent_code: '',
    current_visibility: true, proposed_visibility: true }));
  changingFilter.summary = { total: 3, changed: 3, exportable: 3, blocked: 0, unmatched: 0 }; runs.unshift(changingFilter);
  await render('selection-filter-membership', `/sync/atomer?run=${changingFilter.id}`);
  await click('atomer-select-all'); await click('atomer-select-A');
  assert.equal(document.querySelector('.atomer-bulk strong').textContent, i18n.t('atomerAvailability.selectedCount', { count: 2 }));
  await input('atomer-availability-A', 'do 5 dní');
  assert(!find('atomer-row-A'), 'The excluded row leaves the changes filter after its edit');
  assert.equal(document.querySelector('.atomer-bulk strong').textContent, i18n.t('atomerAvailability.selectedCount', { count: 0 }), 'Confirmed edits clear the completed selection instead of subtracting stale exclusions');
  assert.equal(required('atomer-select-B').checked, false); assert(required('atomer-clear-selection').disabled);
  await click('atomer-select-all');
  assert.equal(document.querySelector('.atomer-bulk strong').textContent, i18n.t('atomerAvailability.selectedCount', { count: 2 }), 'Fresh selection uses the new full filtered total');
  assert(required('atomer-select-B').checked); assert(required('atomer-select-C').checked);

  const paged = ready('55555555-5555-4555-8555-555555555555');
  paged.rows = [{ ...clone(rows[0]), code: 'BIG', name: 'Large family', status: 'context' },
    ...Array.from({ length: 6002 }, (_, index) => ({ ...clone(rows[1]), code: `LEAF-${index}`, name: `Bulk variant ${index}`, parent_code: 'BIG', parent_name: 'Large family', product_id: index + 30 })),
    { ...clone(rows[1]), code: 'STABLE', name: 'Stable product', kind: 'standalone', parent_code: '', status: 'unchanged', current_availability: 'SKLADOM', current_visibility: true },
    { ...clone(rows[1]), code: 'BAD', name: 'Blocked item', parent_code: 'BIG', status: 'blocked', reasons: ['source_identity_invalid'] }];
  paged.summary = { total: 6005, changed: 6002, exportable: 6002, blocked: 1, unmatched: 0 }; runs.unshift(paged);
  await render('paged', `/sync/atomer?run=${paged.id}`);
  assert(required('atomer-select-BIG').disabled, 'Implicit parent context is not a direct filter match');
  await click('atomer-select-LEAF-0'); await click('atomer-next');
  assert(document.querySelector('.atomer-family-row').textContent.includes('Large family'));
  await click('atomer-select-LEAF-100'); await input('atomer-bulk-availability', 'do 7 dní'); await click('atomer-bulk-apply');
  assert.deepEqual(editBodies.at(-1).selection.codes, ['LEAF-0', 'LEAF-100'], 'Explicit selection survives pagination');
  assert(calls.at(-1).path.includes('offset=100'), 'Saving row edits retains the current page');
  await input('atomer-search', 'Bulk'); await click('atomer-search-submit'); await input('atomer-sort', 'qty_available');
  assert(calls.at(-1).path.includes('search=Bulk') && calls.at(-1).path.includes('sort=qty_available'), 'Search and sorting are server-side');
  await click('atomer-select-all'); await click('atomer-select-LEAF-0');
  await input('atomer-bulk-availability', 'do 4 dní'); await input('atomer-bulk-visibility', 'false'); await click('atomer-bulk-persist'); await click('atomer-bulk-apply');
  const bulk = editBodies.at(-1);
  assert.equal(bulk.selection.all_filtered, true); assert.equal(bulk.selection.filter.search, 'Bulk');
  assert.deepEqual(bulk.selection.excluded_codes, ['LEAF-0']); assert(!bulk.selection.codes, 'Whole-result edit has no page or 5000-code cutoff');
  assert.equal(bulk.changes.apply_to_every_export, true);
  assert.equal(paged.rows.find(row => row.code === 'LEAF-6001').proposed_availability, 'do 4 dní', 'Bulk edit reaches later pages beyond 5000 rows');
  assert.equal(paged.rows.find(row => row.code === 'LEAF-0').proposed_availability, 'do 7 dní', 'Excluded row preserved');
  assert.equal(paged.rows.find(row => row.code === 'STABLE').proposed_visibility, true, 'Unmatched filter rows unchanged');
  await input('atomer-view', 'issues'); assert(calls.at(-1).path.includes('offset=0'));
  await click('atomer-reset-filters'); await input('atomer-view', 'issues');
  assert(required('atomer-row-BIG')); assert(required('atomer-row-BAD')); assert(!find('atomer-row-LEAF-0'));
  const staleFilter = { view: 'all' }; delayedDetail = staleFilter;
  await input('atomer-view', 'all'); await input('atomer-view', 'issues');
  await act(async () => { staleFilter.resolve(); await tick(); });
  assert.equal(required('atomer-view').value, 'issues'); assert(required('atomer-row-BAD')); assert(!find('atomer-row-STABLE'));
  await click('atomer-export'); assert.deepEqual(exportBodies.at(-1).overrides, []);

  const nativeTimeout = window.setTimeout, nativeClearTimeout = window.clearTimeout, polls = new Map(); let timerId = 100000;
  window.setTimeout = (callback, delay, ...args) => { if (delay !== 3000) return nativeTimeout.call(window, callback, delay, ...args); const id = ++timerId; polls.set(id, callback); return id; };
  window.clearTimeout = id => { if (!polls.delete(id)) nativeClearTimeout.call(window, id); };
  const poll = async () => { assert.equal(polls.size, 1); const [id, callback] = polls.entries().next().value; polls.delete(id); await act(async () => { callback(); await tick(); await tick(); }); };
  const daily = { ...ready('77777777-7777-4777-8777-777777777777'), trigger: 'daily', status: 'fetching' }; runs.unshift(daily);
  const writesBeforeDaily = calls.filter(call => call.method === 'POST').length;
  await render('daily-fetching', `/sync/atomer?run=${daily.id}`); daily.status = 'ready'; await poll();
  assert.equal(required('atomer-run-status').textContent, i18n.t('atomerAvailability.runStatus.exporting'));
  assert(required('atomer-visible-VARIANT-M').disabled); assert(required('atomer-availability-VARIANT-M').disabled);
  assert(!find('atomer-export')); assert(required('atomer-create').disabled); await poll();
  daily.status = 'exported'; daily.revision++; daily.download_available = true; await poll();
  assert(required('atomer-download')); assert.equal(polls.size, 0); assert.equal(calls.filter(call => call.method === 'POST').length, writesBeforeDaily);
  options.enabled = false; daily.status = 'ready'; daily.download_available = false;
  await render('daily-resume-disabled', `/sync/atomer?run=${daily.id}`); assert.equal(polls.size, 1);
  daily.status = 'failed'; daily.error = 'atomer_availability_source_changed'; await poll();
  assert.equal(polls.size, 0); assert(!find('atomer-export')); assert(!required('atomer-create').disabled);
  const manual = ready('88888888-8888-4888-8888-888888888888'); runs.unshift(manual);
  await render('manual-ready', `/sync/atomer?run=${manual.id}`); assert.equal(polls.size, 0);
  assert(!required('atomer-visible-VARIANT-M').disabled); assert(!required('atomer-availability-VARIANT-M').disabled); assert(!required('atomer-export').disabled);
  window.setTimeout = nativeTimeout; window.clearTimeout = nativeClearTimeout;
  manual.rows = [{ ...clone(rows[1]), code: 'MANUAL-UNKNOWN', kind: 'standalone', parent_code: '', manualAllowed: true,
    status: 'blocked', reasons: ['stock_unconfirmed'], proposed_availability: 'do 5 dní', qty_available: null }];
  await render('manual-unknown-stock', `/sync/atomer?run=${manual.id}`); await input('atomer-view', 'issues');
  assert(!required('atomer-availability-MANUAL-UNKNOWN').disabled, 'Server-confirmed manual availability exception permits editing with unconfirmed stock');
  await input('atomer-availability-MANUAL-UNKNOWN', 'overíme');
  assert.equal(editBodies.at(-1).changes.availability, 'overíme');
  assert.equal(manual.rows[0].qty_available, null, 'Availability editing does not invent a confirmed zero quantity');
  await act(async () => { setHubUser({ id: 20, username: 'operator', display_name: 'Operator', role: 'operator', active: true }); await tick(); });
  assert(required('atomer-feed-url').disabled);
  const late = {}; delayedRead = late; await render('late-response');
  await act(async () => { setHubUser(null); await tick(); late.resolve(); await tick(); });
  assert(required('atomer-unlock')); assert(!find('atomer-preview')); assert(!find('atomer-feed-url'));
  await act(async () => { root.unmount(); await tick(); });
  const sk = JSON.parse(fs.readFileSync(require.resolve('../src/i18n/sk.json'), 'utf8')).atomerAvailability;
  const en = JSON.parse(fs.readFileSync(require.resolve('../src/i18n/en.json'), 'utf8')).atomerAvailability;
  const keys = (value, prefix = '') => Object.entries(value).flatMap(([key, item]) => typeof item === 'string' ? [`${prefix}${key}`] : keys(item, `${prefix}${key}.`));
  assert.deepEqual(keys(sk).sort(), keys(en).sort()); console.log('Atomer availability UI interactions passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
