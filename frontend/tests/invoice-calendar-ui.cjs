/* Synthetic calendar responses only; no real invoices or side effects. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'https://hub.example.test/invoices' });
global.window = dom.window; global.document = dom.window.document;
Object.defineProperty(global, 'navigator', { value: dom.window.navigator, configurable: true });
global.HTMLElement = dom.window.HTMLElement; global.IS_REACT_ACT_ENVIRONMENT = true;
for (const extension of ['.ts', '.tsx']) require.extensions[extension] = (module, filename) => {
  module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8').replace(/import\.meta\.env/g, '{}'), {
    compilerOptions: { jsx: ts.JsxEmit.React, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, esModuleInterop: true },
  }).outputText, filename);
};
require.extensions['.css'] = () => {};
const React = require('react'); const { act } = React;
const { createRoot } = require('react-dom/client');
const { MemoryRouter, useLocation } = require('react-router-dom');
const i18n = require('../src/i18n/index.ts').default;
const { unlockHub } = require('../src/api/access.ts');
const { invoiceArchiveDefaults } = require('../src/api/invoiceArchive.ts');
const { InvoiceDueCalendar, calendarPeriod, moveCalendar } = require('../src/components/InvoiceDueCalendar.tsx');
const { todayDate } = require('../src/utils/format.ts');
const tick = () => new Promise(resolve => setTimeout(resolve, 5));
const root = createRoot(document.getElementById('root'));
const element = id => document.querySelector(`[data-testid="${id}"]`);
function required(id) { const target = element(id); assert(target, `${id} exists`); return target; }
async function click(id) { await act(async () => { required(id).click(); await tick(); }); }
async function date(value) { await act(async () => {
  const target = required('calendar-anchor');
  Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, 'value').set.call(target, value);
  target.dispatchEvent(new dom.window.Event('input', { bubbles: true })); await tick();
}); }
function Location() { return React.createElement('span', { 'data-testid': 'location' }, useLocation().pathname); }
let filters = { ...invoiceArchiveDefaults, supplier_code: 'exact-vendor' }, refresh = 0;
async function draw() { await act(async () => {
  root.render(React.createElement(MemoryRouter, null, React.createElement(React.Fragment, null,
    React.createElement(InvoiceDueCalendar, { filters, refresh }), React.createElement(Location)))); await tick();
}); }
const calls = [];
let mode = 'ok', release;
const documents = [
  { id: 101, invoice_number: 'CAL-UNKNOWN', original_filename: 'unknown.pdf', supplier_name: 'Fixture supplier',
    supplier_code: 'exact-vendor', direction: 'purchase', due_date: '2024-02-29', currency: 'EUR', gross_amount: '100.00', outstanding_amount: null,
    payment_status: 'unknown', is_overdue: false },
  { id: 102, invoice_number: 'CAL-PAID', original_filename: 'paid.pdf', supplier_name: 'Fixture supplier',
    supplier_code: 'exact-vendor', direction: 'purchase', due_date: '2024-02-29', currency: 'CZK', gross_amount: '200.00', outstanding_amount: '0.00',
    payment_status: 'paid', is_overdue: false },
];
const reply = data => ({ ok: true, status: 200, json: async () => data });
global.fetch = async (path, options = {}) => {
  const url = new URL(path, dom.window.location.href); calls.push({ url, options });
  assert.equal(url.pathname, '/api/invoices/calendar');
  assert.equal(options.method, 'GET');
  assert.equal(options.cache, 'no-store');
  assert.equal(url.searchParams.has('sort_by'), false);
  assert.equal(url.searchParams.has('page'), false);
  const items = documents.filter(item => item.due_date >= url.searchParams.get('range_from') && item.due_date <= url.searchParams.get('range_to'));
  if (mode === 'failed' || mode === 'limit') return { ok: false, status: mode === 'limit' ? 409 : 502,
    json: async () => ({ detail: { code: mode === 'limit' ? 'invoice_calendar_range_too_large' : 'request_failed' } }) };
  if (mode === 'deferred') return new Promise(resolve => { release = () => resolve(reply({ items: [{ ...documents[0], invoice_number: 'STALE-RESULT' }], total: 1 })); });
  const totals = [{currency:'EUR',outstanding_amount:'125.37',unknown_count:1,invoice_count:2}, {currency:'CZK',outstanding_amount:'0.00',unknown_count:0,invoice_count:1}, {currency:'USD',outstanding_amount:null,unknown_count:2,invoice_count:2}];
  return reply({ range_from: url.searchParams.get('range_from'), range_to: url.searchParams.get('range_to'), items, total: items.length, limit: 5000,
    totals, weeks:[{from:'2024-02-26',to:'2024-02-29',totals,invoice_ids:items.map(item=>item.id)}],
    months:[{from:'2024-02-01',to:'2024-02-29',totals,invoice_ids:items.map(item=>item.id)}] });
};

(async () => {
  assert.equal(todayDate(new Date('2024-02-29T23:30:00Z')), '2024-03-01', 'Payment defaults use the Slovak business date');
  assert.deepEqual(calendarPeriod('2024-02-29', 'month'), { from: '2024-02-01', to: '2024-02-29' });
  assert.deepEqual(calendarPeriod('2025-12-31', 'week'), { from: '2025-12-29', to: '2026-01-04' });
  assert.deepEqual(calendarPeriod('2024-02-29', 'year'), { from: '2024-01-01', to: '2024-12-31' });
  assert.equal(moveCalendar('2024-01-31', 'month', 1), '2024-02-29');
  assert.equal(moveCalendar('2024-02-29', 'year', 1), '2025-02-28');
  await act(async () => { await i18n.changeLanguage('en'); unlockHub('synthetic-calendar-token'); });
  await draw(); await date('2024-02-29');
  assert.equal(calls.at(-1).url.searchParams.get('supplier_code'), 'exact-vendor');
  assert.equal(calls.at(-1).url.searchParams.get('range_to'), '2024-02-29');
  assert.equal(required('calendar-main-grid').querySelectorAll('.calendar-day').length, 42);
  assert(required('calendar-payment-summary').textContent.includes('125.37 EUR'));
  assert(required('calendar-payment-summary').textContent.includes('0.00 CZK'));
  assert(required('calendar-payment-summary').textContent.includes(i18n.t('invoiceCalendar.unknownAmount') + ' · USD'));
  assert(!required('calendar-payment-summary').textContent.includes('0.00 USD'), 'All-unverified amounts must not look like zero payments due');
  assert(required('calendar-payment-summary').textContent.includes(i18n.t('invoiceCalendar.unknownTotal', {count:1})), 'Unknown settlements are visible alongside confirmed sums');
  const week = required('calendar-week-2024-02-26');
  await act(async()=>{week.querySelector('summary').click(); await tick();});
  assert(week.open); assert(week.querySelector('a[href="/invoices/101"]'));
  assert(required('calendar-day-2024-01-29').getAttribute('aria-label').includes('Open this month'));
  assert(!required('calendar-day-2024-01-29').getAttribute('aria-label').includes('0 invoices'), 'Unqueried adjacent month days do not claim zero invoices');
  assert(document.querySelector('a[href="/invoices/101"]').classList.contains('unknown'));
  assert(!document.querySelector('a[href="/invoices/101"]').classList.contains('overdue'));
  await click('calendar-day-2024-02-29');
  assert(required('calendar-selected-invoices').textContent.includes('CZK'));
  assert(required('calendar-selected-invoices').textContent.includes(i18n.t('invoiceArchive.status.unknown')));
  await click('calendar-view-week');
  assert.equal(calls.at(-1).url.searchParams.get('range_from'), '2024-02-26');
  assert.equal(calls.at(-1).url.searchParams.get('range_to'), '2024-03-03');
  assert.equal(required('calendar-main-grid').querySelectorAll('.calendar-day').length, 7);
  await click('calendar-view-year');
  assert.equal(document.querySelectorAll('.calendar-mini-month').length, 12);
  assert(required('calendar-month-total-2').textContent.includes('125.37 EUR'), 'Year overview includes each monthly payment sum');
  assert.equal(calls.at(-1).url.searchParams.get('range_to'), '2024-12-31');
  await date('2024-10-15');
  await click('calendar-mini-2024-02-29');
  assert(required('calendar-selected-invoices').querySelector('a[href="/invoices/102"]'));
  await click('calendar-view-month');
  assert.equal(calls.at(-1).url.searchParams.get('range_from'), '2024-02-01', 'A selected year-view date becomes the month anchor');
  await click('calendar-view-week');
  assert.equal(calls.at(-1).url.searchParams.get('range_from'), '2024-02-26', 'A selected year-view date becomes the week anchor');
  await click('calendar-view-year');
  await act(async () => { await i18n.changeLanguage('sk'); });
  assert(document.body.textContent.includes('Kalendár splatností'));
  mode = 'failed'; refresh += 1; await draw();
  assert(required('calendar-error')); assert.equal(element('calendar-year-grid'), null, 'Failed data never leaves a stale calendar visible');
  mode = 'ok'; await act(async () => { required('calendar-error').querySelector('button').click(); await tick(); });
  assert(element('calendar-year-grid'));
  mode = 'limit'; refresh += 1; await draw();
  assert(required('calendar-error').textContent.includes('5 000')); assert.equal(element('calendar-year-grid'), null);
  mode = 'deferred'; refresh += 1; await draw(); assert(release);
  mode = 'ok'; filters = { ...filters, supplier_code: 'changed-vendor', payment_status: 'unknown', date_from: '2024-01-01' }; await draw();
  assert.equal(calls.at(-1).url.searchParams.get('supplier_code'), 'changed-vendor');
  assert.equal(calls.at(-1).url.searchParams.get('payment_status'), 'unknown');
  assert.equal(calls.at(-1).url.searchParams.get('date_from'), '2024-01-01');
  await act(async () => { release(); await tick(); });
  assert(!document.body.textContent.includes('STALE-RESULT'), 'An aborted old filter response cannot replace the current calendar');
  await click('calendar-mini-2024-02-29');
  await act(async () => { required('calendar-selected-invoices').querySelector('a[href="/invoices/101"]').click(); await tick(); });
  assert.equal(required('location').textContent, '/invoices/101');
  await act(async () => root.unmount());
  console.log('Invoice calendar interactions passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
