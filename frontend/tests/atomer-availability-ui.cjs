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
let runs = [], createMode = 'ok', exportMode = 'ok', settingsMode = 'ok', delayedRead, delayedDetail;
const calls = [], exportBodies = [], configs = [];
const rows = [
  { code: 'PARENT', name: 'Family jacket', image: 'https://images.example.test/parent.jpg', kind: 'parent', parent_code: '', product_id: null, sku: null,
    current_visibility: false, current_availability: 'overíme', proposed_visibility: true, proposed_availability: 'overíme', qty_available: null, status: 'changed', reasons: [] },
  { code: 'VARIANT-M', name: 'Jacket M', image: 'https://images.example.test/variant.jpg', kind: 'variant', parent_code: 'PARENT', parent_name: 'Family jacket', parent_image: 'https://images.example.test/parent.jpg', product_id: 10, sku: 'BR-JACKET-M',
    current_visibility: false, current_availability: 'do 5 dní', proposed_visibility: true, proposed_availability: 'SKLADOM', qty_available: '2', status: 'changed', reasons: [] },
  { code: 'UNKNOWN', name: 'Unmatched product', image: null, kind: 'standalone', parent_code: '', product_id: null, sku: null,
    current_visibility: true, current_availability: 'overíme', proposed_visibility: true, proposed_availability: 'overíme', qty_available: null, status: 'unmatched', reasons: ['hub_product_missing'] },
];
function detail(run, offset = 0, view = 'all') {
  const all = clone(run.rows || rows);
  const selected = all.filter(row => view === 'all' || (view === 'changes' ? row.status === 'changed' : ['blocked', 'unmatched'].includes(row.status)));
  const parents = new Set(selected.map(row => row.parent_code).filter(Boolean));
  const filtered = all.filter(row => selected.includes(row) || row.kind === 'parent' && parents.has(row.code));
  return { ...clone(run), rows: filtered.slice(offset, offset + 100), total: filtered.length, offset, view, availability_options: ['SKLADOM', 'do 5 dní', 'overíme'] };
}
function ready(id, receiving = null) { return { id, status: 'ready', revision: 2, trigger: receiving ? 'receiving' : 'manual', receiving_batch_id: receiving,
  created_at: '2026-11-16T15:00:00Z', source_fetched_at: '2026-11-16T15:00:01Z', source_sha256: 'a'.repeat(64), completed_at: null, error: null,
  summary: { total: 3, changed: 2, blocked: 0, unmatched: 1 }, download_available: false, coverage: { kind: 'buyers_only', hidden_rows: 2, missing_parents: 0 }, warnings: [] }; }
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
  const match = /^\/api\/atomer-availability\/runs\/([^/]+)(?:\/(export|download))?$/.exec(url.pathname);
  assert(match, path); const run = runs.find(item => item.id === match[1]);
  if (!run) return response({ detail: { code: 'atomer_availability_run_missing' } }, 404);
  if (match[2] === 'download') { assert(run.download_available); return { ok: true, blob: async () => new Blob(['synthetic csv']) }; }
  if (match[2] === 'export') {
    exportBodies.push(clone(body));
    if (exportMode === 'rejected') { exportMode = 'ok'; return response({ detail: { code: 'atomer_availability_override_blocked', known_unapplied: true } }, 422); }
    if (run.status !== 'exported') {
      assert.equal(body.expected_revision, run.revision);
      run.rows = clone(run.rows || rows);
      for (const override of body.overrides) {
        const row = run.rows.find(item => item.code === override.code); assert(row);
        if ('visibility' in override) row.proposed_visibility = override.visibility;
        if ('availability' in override) row.proposed_availability = override.availability;
      }
      run.status = 'exported'; run.revision++; run.download_available = true; run.completed_at = '2026-11-16T15:01:00Z';
    }
    if (exportMode === 'lost') { exportMode = 'ok'; throw new TypeError('Synthetic lost export response after commit'); }
    return response(run);
  }
  const preview = detail(run, Number(url.searchParams.get('offset') || 0), url.searchParams.get('view') || 'all');
  if (delayedDetail?.view === preview.view) { const wait = delayedDetail; delayedDetail = null; return new Promise(resolve => { wait.resolve = () => resolve(response(preview)); }); }
  return response(preview);
};
async function render(key, path = '/sync/atomer') { await act(async () => { root.render(React.createElement(MemoryRouter, { key, initialEntries: [path], future: { v7_startTransition: true, v7_relativeSplatPath: true } }, React.createElement(AtomerAvailabilityPage))); await tick(); await tick(); }); }
async function click(id) { await act(async () => { const element = required(id); assert(!element.disabled, `${id} is enabled`); element.click(); await tick(); await tick(); }); }
async function input(id, value) { await act(async () => { const target = required(id), prototype = target.tagName === 'SELECT' ? dom.window.HTMLSelectElement.prototype : dom.window.HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(prototype, 'value').set.call(target, value); target.dispatchEvent(new dom.window.Event(target.tagName === 'SELECT' ? 'change' : 'input', { bubbles: true })); await tick(); }); }

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
  assert(required('atomer-coverage').textContent.includes('skrytých'), 'Incomplete hidden-product coverage is visible');
  const tableRows = [...document.querySelectorAll('.atomer-table tbody tr')];
  assert(tableRows.findIndex(row => row.dataset.testid === 'atomer-row-PARENT') < tableRows.findIndex(row => row.dataset.testid === 'atomer-row-VARIANT-M'), 'Parent appears before its variant');
  assert.equal(required('atomer-row-VARIANT-M').querySelector('img').src, rows[1].image, 'Existing thumbnail component displays variant image');
  assert.equal(required('atomer-view').value, 'changes', 'Preview defaults to changes that can be exported');
  assert(!find('atomer-row-UNKNOWN'), 'Unmatched source rows do not crowd the change preview');
  await input('atomer-view', 'issues');
  assert(required('atomer-visible-UNKNOWN').disabled, 'Unmatched identities cannot be manually forced into CSV');
  await input('atomer-view', 'changes');
  await click('atomer-visible-VARIANT-M'); await input('atomer-availability-VARIANT-M', 'overíme');
  assert(required('atomer-create').disabled, 'A new preview cannot silently discard manual edits');
  await render('reload-edits', `/sync/atomer?receiving_batch=${batch}&run=${original.body.request_id}`);
  assert.equal(required('atomer-visible-VARIANT-M').checked, false); assert.equal(required('atomer-availability-VARIANT-M').value, 'overíme', 'Manual edits survive reload');
  exportMode = 'lost'; await click('atomer-export');
  assert.deepEqual(exportBodies[0], { expected_revision: 2, overrides: [{ code: 'VARIANT-M', visibility: false, availability: 'overíme' }] });
  assert(required('atomer-export').disabled, 'Uncertain export must be read before retry');
  assert(required('atomer-visible-VARIANT-M').disabled, 'Pending export freezes its exact edits');
  await click('atomer-recover-export');
  assert.equal(exportBodies.length, 1, 'Committed CSV recovered without a second export');
  assert(required('atomer-download')); assert(!find('atomer-export')); assert(required('atomer-visible-VARIANT-M').disabled, 'Generated CSV is immutable');
  assert(document.body.textContent.includes('ručne do Atomeru'), 'UI does not claim that CSV generation imported to Atomer');
  await click('atomer-download'); assert.equal(downloads, 1);

  await render('settings');
  await input('atomer-feed-url', 'https://source.example.test/private-feed.xml'); settingsMode = 'lost'; await click('atomer-save-settings');
  assert(required('atomer-feed-url').disabled, 'Unknown settings save preserves the exact pending payload');
  await click('atomer-save-settings');
  assert.deepEqual(configs[0], configs[1], 'Configuration retry preserves UUID and payload');
  assert.equal(required('atomer-feed-url').value, '');
  assert(!Object.values(sessionStorage).some(value => value.includes('private-feed.xml')), 'Feed URL is never stored in browser storage');

  runs.unshift(ready('44444444-4444-4444-8444-444444444444'));
  await render('rejected', '/sync/atomer?run=44444444-4444-4444-8444-444444444444');
  await input('atomer-availability-VARIANT-M', 'do 3 dní'); exportMode = 'rejected'; await click('atomer-export');
  assert(!required('atomer-availability-VARIANT-M').disabled, 'An explicit validation rejection permits correction');
  await input('atomer-availability-VARIANT-M', 'overíme'); await click('atomer-export'); assert(required('atomer-download'));

  createMode = 'lost'; await click('atomer-create');
  const pendingCreation = calls.findLast(call => call.method === 'POST' && call.path.endsWith('/runs'));
  const creationCount = calls.filter(call => call.method === 'POST' && call.path.endsWith('/runs')).length;
  assert(required(`atomer-open-${original.body.request_id}`).disabled, 'An unresolved creation cannot be replaced by opening an older run');
  await click('atomer-refresh');
  assert(required('atomer-export'), 'Refresh recovers the new preview instead of the previously selected exported run');
  assert.equal(sessionStorage.getItem('hub.atomer-availability.pending.all'), null);
  assert(calls.some(call => call.path.includes(pendingCreation.body.request_id) && call.method === 'GET'));
  assert.equal(calls.filter(call => call.method === 'POST' && call.path.endsWith('/runs')).length, creationCount, 'Refresh recovers creation without another POST');

  const paged = ready('55555555-5555-4555-8555-555555555555');
  paged.rows = [{ ...clone(rows[0]), code: 'BIG', name: 'Large family', status: 'context' },
    ...Array.from({ length: 102 }, (_, index) => ({ ...clone(rows[1]), code: `LEAF-${index}`, name: `Variant ${index}`, parent_code: 'BIG', parent_name: 'Large family', product_id: index + 30 })),
    { ...clone(rows[1]), code: 'STABLE', kind: 'standalone', parent_code: '', status: 'unchanged', current_availability: 'SKLADOM', current_visibility: true },
    { ...clone(rows[1]), code: 'BAD', parent_code: 'BIG', status: 'blocked', reasons: ['source_identity_invalid'] }];
  paged.summary = { total: 105, changed: 102, blocked: 1, unmatched: 0 }; runs.unshift(paged);
  await render('paged', `/sync/atomer?run=${paged.id}`);
  await input('atomer-availability-LEAF-0', 'do 7 dní'); await click('atomer-next');
  assert(document.querySelector('.atomer-family-row').textContent.includes('Large family'), 'Second page retains parent context');
  await click('atomer-visible-LEAF-100'); await input('atomer-view', 'issues');
  const issueRead = calls.at(-1); assert(issueRead.path.includes('offset=0') && issueRead.path.includes('view=issues'), 'Changing filter resets the server offset');
  assert(required('atomer-row-BIG')); assert(required('atomer-row-BAD')); assert(!find('atomer-row-LEAF-0'), 'Issue view retains parent context without unrelated variants');
  assert.equal(document.querySelector('.atomer-counts dd').textContent, '105', 'Summary counts remain global while filtered totals change');
  assert(document.querySelector('.atomer-pagination').textContent.includes('2'), 'Pagination describes the filtered rows');
  const staleFilter = { view: 'all' }; delayedDetail = staleFilter;
  await input('atomer-view', 'all'); await input('atomer-view', 'issues');
  await act(async () => { staleFilter.resolve(); await tick(); });
  assert.equal(required('atomer-view').value, 'issues'); assert(required('atomer-row-BAD')); assert(!find('atomer-row-STABLE'), 'A late filter response cannot replace the current view');
  await input('atomer-view', 'all'); await click('atomer-next');
  await input('atomer-availability-STABLE', 'do 2 dní'); await input('atomer-view', 'changes');
  assert(!find('atomer-row-STABLE'), 'An unchanged source row remains outside the default filter while its manual edit stays saved');
  assert.equal(required('atomer-availability-LEAF-0').value, 'do 7 dní', 'Edits survive pagination');
  await click('atomer-export');
  assert.deepEqual(exportBodies.at(-1).overrides, [{ code: 'LEAF-0', availability: 'do 7 dní' }, { code: 'LEAF-100', visibility: false }, { code: 'STABLE', availability: 'do 2 dní' }], 'Export includes edits from every page and filter');

  const unchanged = ready('66666666-6666-4666-8666-666666666666');
  unchanged.rows = [clone(paged.rows.find(row => row.code === 'STABLE'))];
  unchanged.summary = { total: 1, changed: 0, blocked: 0, unmatched: 0 }; runs.unshift(unchanged);
  await render('unchanged-only', `/sync/atomer?run=${unchanged.id}`);
  assert(!find('atomer-row-STABLE'));
  await input('atomer-view', 'all'); await input('atomer-availability-STABLE', 'do 4 dní'); await input('atomer-view', 'changes');
  assert(!find('atomer-row-STABLE')); assert(!required('atomer-export').disabled, 'An empty filtered page cannot block a manual export');
  await click('atomer-export');
  assert.deepEqual(exportBodies.at(-1).overrides, [{ code: 'STABLE', availability: 'do 4 dní' }], 'A manual edit is exported even when changes view has no source rows');

  await act(async () => { setHubUser({ id: 20, username: 'operator', display_name: 'Operator', role: 'operator', active: true }); await tick(); });
  assert(required('atomer-feed-url').disabled, 'Operators cannot edit source configuration');
  const late = {}; delayedRead = late; await render('late-response');
  await act(async () => { setHubUser(null); await tick(); late.resolve(); await tick(); });
  assert(required('atomer-unlock')); assert(!find('atomer-preview')); assert(!find('atomer-feed-url'), 'Late responses cannot restore private data after access is removed');
  await act(async () => { root.unmount(); await tick(); });
  const sk = JSON.parse(fs.readFileSync(require.resolve('../src/i18n/sk.json'), 'utf8')).atomerAvailability;
  const en = JSON.parse(fs.readFileSync(require.resolve('../src/i18n/en.json'), 'utf8')).atomerAvailability;
  const keys = (value, prefix = '') => Object.entries(value).flatMap(([key, item]) => typeof item === 'string' ? [`${prefix}${key}`] : keys(item, `${prefix}${key}.`));
  assert.deepEqual(keys(sk).sort(), keys(en).sort(), 'All new UI text has Slovak and English translations');
  console.log('Atomer availability UI interactions passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
