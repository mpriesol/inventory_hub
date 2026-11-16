/* Synthetic protected API interactions. No supplier downloads or shop writes. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'https://hub.example.test/settings/availability' });
global.window = dom.window; global.document = dom.window.document;
Object.defineProperty(global, 'navigator', { value: dom.window.navigator, configurable: true });
global.HTMLElement = dom.window.HTMLElement; global.IS_REACT_ACT_ENVIRONMENT = true;
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
const { OperationLogsPage } = require('../src/pages/OperationLogsPage.tsx');
const { unlockHub } = require('../src/api/access.ts');
const root = createRoot(document.getElementById('root'));
const tick = () => new Promise(resolve => setTimeout(resolve, 5));
const element = id => document.querySelector(`[data-testid="${id}"]`);
const required = id => { const result = element(id); assert(result, `${id} exists`); return result; };
async function click(id) { await act(async () => { required(id).click(); await tick(); }); }
async function input(id, value) {
  await act(async () => {
    const target = required(id), proto = target.tagName === 'SELECT' ? dom.window.HTMLSelectElement.prototype : target.tagName === 'TEXTAREA' ? dom.window.HTMLTextAreaElement.prototype : dom.window.HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, 'value').set.call(target, value);
    target.dispatchEvent(new dom.window.Event(target.tagName === 'SELECT' ? 'change' : 'input', { bubbles: true })); await tick();
  });
}

const calls = [];
let mode = 'ok', finish;
let event = { id: 'a'.repeat(32), timestamp: '2026-11-13T20:51:30.000000Z', component: 'husky', event: 'catalog_validation_failed', level: 'error', supplier: 'husky', run_id: 1240, phase: 'parse_page', page: 2, observed_page: 1, reason: 'pager_marker_mismatch', error_code: 'husky_b2b_incomplete' };
global.fetch = async (path, options) => {
  calls.push({ path, ...options });
  if (mode === 'pending') return new Promise(resolve => { finish = () => resolve({ ok: true, json: async () => ({ items: [event], next_cursor: null, max_bytes: 20000000 }) }); });
  if (mode === 'expired') return { ok: false, status: 409, json: async () => ({ detail: { code: 'operation_log_cursor_expired' } }) };
  return { ok: true, json: async () => ({ items: [event], next_cursor: path.includes('before=') ? null : 'b'.repeat(32), max_bytes: 20000000 }) };
};
(async () => {
  await i18n.changeLanguage('sk');
  await act(async () => {
    unlockHub('synthetic-log-access-token');
    root.render(React.createElement(MemoryRouter, { initialEntries: ['/settings/logs?supplier=husky&run_id=1240'] }, React.createElement(OperationLogsPage)));
    await tick();
  });
  assert(calls[0].path.includes('supplier=husky&run_id=1240'));
  assert(document.body.textContent.includes('pager_marker_mismatch'));
  assert.equal(required('log-run_id').value, '1240');
  assert.equal(document.querySelector('[role="tooltip"]').hidden, true);
  await act(async () => { document.querySelector('.help-hint-button').focus(); await tick(); });
  assert.equal(document.querySelector('[role="tooltip"]').hidden, false);
  await act(async () => { document.querySelector('.help-hint-button').dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true })); await tick(); });
  assert.equal(document.querySelector('[role="tooltip"]').hidden, true);
  await click('log-next'); assert(calls.at(-1).path.includes('before=')); assert(required('log-next').disabled);
  await click('log-refresh'); assert(!calls.at(-1).path.includes('before='));
  await input('log-level', 'error'); await input('log-since', '2026-11-13T18:00'); await click('log-apply');
  assert(calls.at(-1).path.includes('level=error') && calls.at(-1).path.includes('since='));
  assert(!calls.at(-1).path.includes('before='));
  mode = 'expired'; await click('log-next');
  assert(document.querySelector('[role="alert"]').textContent.includes('rotované'));
  assert(!document.querySelector('tbody'), 'A failed read cannot show stale events as its result');
  mode = 'ok'; await click('log-refresh');
  await click('log-reset'); assert.equal(required('log-supplier').value, ''); assert(!calls.at(-1).path.includes('supplier='));
  const base = { id: event.id, timestamp: event.timestamp, component: 'supplier_availability', supplier: 'husky', run_id: 1240, event: 'availability_pass_failed', level: 'error' };
  event = { ...base, error_code: 'husky_stock_incomplete', phase: 'acceptance', reason: 'coverage_below_minimum', target: 'husky_stock_feed',
    rows: 9939, previous_rows: 9940, required_rows: 9940, min_coverage_percent: 100, catalog_rows: 3125, stock_rows: 9939, b2b_rows: 10655, matched_rows: 3000, unknown: 200 };
  await click('log-refresh');
  const readable = () => document.querySelector('tbody').textContent.replace(/\s+/g, ' ');
  assert(readable().includes('načítané 9 939, predtým 9 940'));
  assert(readable().includes('Limit 100 % vyžaduje aspoň 9 940; do limitu chýba 1'));
  assert(readable().includes('Nové dostupnosti sa neuložili'));
  assert(readable().includes('10 655'));
  assert(!readable().includes('nemá zhodné kódy'), 'Coverage failure must not be confused with a failed code join');
  event = { ...event, event: 'availability_coverage_warning', level: 'warning' };
  await click('log-refresh');
  assert(readable().includes('načítané 9 939, predtým 9 940'));
  assert(readable().includes('Platné dostupnosti boli uložené'));
  assert(!readable().includes('Nové dostupnosti sa neuložili'), 'A committed partial update is not a historical rejection');
  await input('log-attention_id', '123'); await click('log-apply');
  assert(calls.at(-1).path.includes('attention_id=123'));
  event = { ...base, error_code: 'supplier_availability_incomplete_feed' };
  await click('log-refresh');
  assert(readable().includes('Celá dávka bola odmietnutá'));
  assert(readable().includes('Tento starší záznam neobsahuje počty'));
  assert(!readable().includes('Platné údaje boli prijaté'), 'Historical Husky rejection cannot claim partial acceptance');
  event = { ...event, supplier: 'paul-lange' };
  await click('log-refresh'); assert(readable().includes('Platné údaje boli prijaté'), 'Ordinary partial-feed semantics are retained');
  event = { ...base, event: 'snapshot_parsed', level: 'info', catalog_rows: 2, stock_rows: 3, b2b_rows: 10, matched_rows: 1, unknown: 2 };
  await click('log-refresh');
  assert(readable().includes('Zdroje spracované') && readable().includes('Údaje ešte neboli prijaté do Hubu'));
  assert.equal(document.querySelectorAll('.operation-log-counts dd').length, 5);
  mode = 'pending'; await click('log-refresh'); const pending = calls.at(-1), resolveOld = finish;
  await act(async () => { unlockHub(''); await tick(); }); assert(pending.signal.aborted);
  await act(async () => { resolveOld(); await tick(); });
  assert(!document.querySelector('tbody'), 'Logging out clears all retained events and rejects late responses');
  assert(calls.every(call => call.method === 'GET'), 'Log browsing never writes or retries a supplier request');
  await act(async () => root.unmount());
  console.log('operation-logs-ui: protected reads, filters, cursor recovery, stale access and accessible hints passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
