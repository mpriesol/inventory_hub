/* Synthetic archive/ledger APIs only. No real invoices, financial writes or downloads. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'https://hub.example.test/invoices' });
global.window = dom.window; global.document = dom.window.document;
global.sessionStorage = dom.window.sessionStorage;
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
const { createMemoryRouter, RouterProvider } = require('react-router-dom');
const { InvoicesPage } = require('../src/pages/InvoicesPage.tsx');
const { InvoiceDetailPage } = require('../src/pages/InvoiceDetailPage.tsx');
const { invoiceMoney, invoiceSourceDiagnostic } = require('../src/pages/invoiceArchiveUi.tsx');
const { unlockHub, setHubUser, hubUser } = require('../src/api/access.ts');
const { todayDate } = require('../src/utils/format.ts');
let root = createRoot(document.getElementById('root')), router;
const tick = () => new Promise(resolve => setTimeout(resolve, 5));
const element = id => document.querySelector(`[data-testid="${id}"]`);
const required = id => { const result = typeof id === 'string' ? element(id) : id; assert(result, `${id} exists`); return result; };
async function click(id) { await act(async () => { required(id).click(); await tick(); }); }
async function input(id, value) { await act(async () => {
  const target = required(id), prototype = target.tagName === 'SELECT' ? dom.window.HTMLSelectElement.prototype : target.tagName === 'TEXTAREA' ? dom.window.HTMLTextAreaElement.prototype : dom.window.HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(prototype, 'value').set.call(target, value);
  target.dispatchEvent(new dom.window.Event(target.tagName === 'SELECT' ? 'change' : 'input', { bubbles: true })); await tick();
}); }
async function dropFiles(files) { await act(async () => {
  const event = new dom.window.Event('drop', { bubbles: true, cancelable: true });
  Object.defineProperty(event, 'dataTransfer', { value: { types: ['Files'], files } });
  required('invoice-dropzone').dispatchEvent(event); await tick();
}); }
async function withUnavailableStorage(silent, action) {
  const original = dom.window.Storage.prototype.setItem;
  dom.window.Storage.prototype.setItem = function() { if (!silent) throw new dom.window.DOMException('Synthetic denied storage', 'SecurityError'); };
  try { await action(); } finally { dom.window.Storage.prototype.setItem = original; }
}
async function render(path) {
  await act(async () => {
    root.unmount(); document.getElementById('root').innerHTML = ''; root = createRoot(document.getElementById('root'));
    router = createMemoryRouter([
      { path: '/invoices', element: React.createElement(InvoicesPage) },
      { path: '/invoices/:invoiceId', element: React.createElement(InvoiceDetailPage) },
      { path: '*', element: React.createElement('div', null, 'Other route') },
    ], { initialEntries: [path] });
    root.render(React.createElement(RouterProvider, { router })); await tick();
  });
}
const clone = value => JSON.parse(JSON.stringify(value));
const reply = value => ({ ok: true, status: 200, json: async () => clone(value) });
const failure = (status, code, knownUnapplied = false, detail = {}) => ({ ok: false, status, json: async () => ({ detail: { code, known_unapplied: knownUnapplied, ...detail } }) });
const finance = (overrides = {}) => ({ payment_revision: 0, currency: 'EUR', gross_amount: '100.00', paid_amount: '0.00', outstanding_amount: '100.00', status: 'unpaid', is_overdue: true, opening_required: false, payments: [], ...overrides });
const makeInvoice = (id, extra = {}) => ({ id, direction: 'purchase', document_type: 'invoice', source_kind: 'upload', supplier_id: 7, supplier_code: 'fixture-vendor', supplier_name: 'Fixture vendor', shop_code: null, shop_name: null,
  original_filename: `fixture-${id}.pdf`, file_type: 'pdf', invoice_number: `FA-${id}`, invoice_date: '2026-10-01', due_date: '2026-10-15', currency: 'EUR',
  total_amount: '100.00', total_with_vat: '100.00', total_without_vat: '81.30', vat_amount: '18.70', vat_rate: '23', vat_included: true, items_count: 1,
  payment_status: 'unpaid', receiving_status: 'in_progress', is_overdue: true, days_until_due: -7, is_parsed: true, created_at: '2026-10-01T12:00:00Z', updated_at: '2026-10-01T12:00:00Z',
  paid_at: null, paid_amount: '0.00', payment_revision: 0, parse_error: null, receiving_session_id: 67, receiving_invoice_number: 'SUP/2026 98', notes: null,
  finance: finance(), lines: [{ id: 1, line_number: 1, ean: '8580000000001', supplier_sku: 'RAW-1', product_name: 'Fixture part', quantity: '2.5', unit: 'm', unit_price: '0.00', unit_price_with_vat: '0.00', total_price: '0.00', total_price_with_vat: '0.00', matched_product_id: 18, product_sku: 'TP-RAW-1' }], ...extra });
const invoices = new Map([[10, makeInvoice(10)], [11, makeInvoice(11, { finance: finance({ paid_amount: null, outstanding_amount: null, status: 'unknown', opening_required: true }), source_kind: 'supplier' })],
  [12, makeInvoice(12, { total_amount: '0.00', total_with_vat: '0.00', finance: finance({ gross_amount: '0.00', paid_amount: '0.00', outstanding_amount: '0.00', status: 'paid', is_overdue: false }) })],
  [13, makeInvoice(13, { direction: 'sale', source_kind: 'shop', supplier_id: null, supplier_name: null, shop_code: 'biketrek', shop_name: 'BIKETREK' })]]);
const stats = direction => ({ direction, total_invoices: 4, unpaid_count: 2, overdue_count: 2,
  totals_by_currency: [{ currency: 'EUR', invoice_count: 3, total_amount: '200.00', outstanding_amount: null, unpaid_amount: null, overdue_amount: null, unpaid_count: 1, overdue_count: 1, unknown_amount_count: 0, unknown_outstanding_count: 1 },
    { currency: 'USD', invoice_count: 1, total_amount: '20.00', outstanding_amount: '5.00', unpaid_amount: '5.00', overdue_amount: '0.00', unpaid_count: 1, overdue_count: 0, unknown_amount_count: 0 }],
  monthly: [{ month: '2026-10', currency: 'EUR', total_amount: '200.00', invoice_count: 3, unknown_amount_count: 0 }, { month: '2026-10', currency: 'USD', total_amount: '20.00', invoice_count: 1, unknown_amount_count: 0 }],
  aging: [{ bucket: '1_30', currency: 'EUR', outstanding_amount: '100.00', invoice_count: 1 }],
});
let paymentMode = 'success', hideRecovery = false, downloadFail = false, deferredList = false, releaseList;
let shopSyncMode = 'success';
let processingMode = 'success';
let processingPreviewMode = 'valid', processingAckMode = 'valid', paymentAckMode = 'valid';
let paymentCounter = 100;
const calls = [], operations = new Map(), attempts = new Map(), downloads = [], uploadedFiles = [];
const processingOperations = new Map(), processingAttempts = new Map();
global.URL.createObjectURL = blob => { assert(blob instanceof Blob); return 'blob:https://hub.example.test/synthetic-download'; };
global.URL.revokeObjectURL = () => {};
dom.window.HTMLAnchorElement.prototype.click = function() { if (this.download) downloads.push({ href: this.href, filename: this.download }); };
function applyPayment(id, body, paymentId) {
  const invoice = invoices.get(id), current = invoice.finance;
  assert.equal(body.expected_revision, current.payment_revision);
  if (!current.payments.length) current.payments.push({ id: 90, kind: 'opening', amount: body.opening_paid_amount || current.paid_amount, currency: invoice.currency, paid_at: null, note: null, actor: 'Fixture operator', reverses_payment_id: null, reversed_by_id: null });
  const original = paymentId && current.payments.find(row => row.id === paymentId);
  const row = { id: ++paymentCounter, kind: original ? 'reversal' : body.kind, amount: original ? `-${original.amount}` : body.amount, currency: invoice.currency, paid_at: body.paid_at || '2026-10-22T12:00:00Z', note: body.note, actor: 'Fixture operator', reverses_payment_id: paymentId || null, reversed_by_id: null };
  if (original) original.reversed_by_id = row.id;
  current.payments.push(row); current.payment_revision += 1; current.opening_required = false; current.status = 'partial'; current.paid_amount = '12.34'; current.outstanding_amount = '87.66'; invoice.payment_revision = current.payment_revision;
  return { invoice_id: id, request_id: body.request_id, payment_revision: current.payment_revision, finance: clone(current) };
}
function processingAfter(before, action) {
  const after = { ...clone(before), revision: before.revision + 1 };
  if (action === 'process_without_stock') after.status = after.manual_status = 'processed_without_stock';
  else if (action === 'reopen') after.status = after.manual_status = 'unprocessed';
  else { after.archived = action === 'archive'; after.archived_at = after.archived ? '2026-11-09T12:00:00Z' : null; }
  after.allowed_actions = after.archived ? ['restore'] : [after.manual_status === 'unprocessed' ? 'process_without_stock' : 'reopen', 'archive'];
  return after;
}
function acknowledgment(value, mode, kind) {
  if (mode === 'malformed') return { invoice_id: value.invoice_id, request_id: value.request_id };
  if (mode === 'wrong-invoice') return { ...value, invoice_id: 9999 };
  if (mode === 'wrong-uuid') return { ...value, request_id: '00000000-0000-4000-8000-000000000000' };
  if (mode === 'wrong-action') return { ...value, action: 'unrelated-action' };
  if (mode === 'wrong-revision') return { ...value, [kind + '_revision']: value[kind + '_revision'] + 1 };
  return value;
}
global.fetch = async (path, init = {}) => {
  assert(path.startsWith('/api/'), 'Only protected API calls');
  const headers = new Headers(init.headers);
  assert.equal(headers.get('Authorization'), hubUser() ? null : 'Bearer synthetic-invoice-token');
  assert.equal(headers.get('X-Hub-Request'), '1');
  assert.equal(init.credentials, 'same-origin'); assert.equal(init.cache, 'no-store'); assert.equal(init.redirect, 'error');
  assert(!path.includes('synthetic-invoice-token'));
  const body = init.body && typeof init.body === 'string' ? JSON.parse(init.body) : init.body;
  calls.push({ path, method: init.method || 'GET', body: clone(body || null) });
  const url = new URL(path, dom.window.location.href);
  if (url.pathname === '/api/suppliers') return reply([{ code: 'fixture-vendor', name: 'Fixture vendor' }]);
  if (url.pathname === '/api/invoices/supplier-options') return reply([{ code: 'retired-vendor', name: 'Archived inactive vendor' }]);
  if (url.pathname === '/api/invoices/archive-status') return reply({ items: [{ supplier_code: 'fixture-vendor', status: 'attention', scanned: 100, conflicts: [{ source_key: 'BAD-1', code: 'invoice_index_ambiguous' }], uploads: [] }] });
  if (url.pathname === '/api/invoices/calendar') return reply({
    range_from: url.searchParams.get('range_from'), range_to: url.searchParams.get('range_to'),
    items: [], total: 0, limit: 5000,
  });
  if (url.pathname === '/api/invoices/upload') {
    assert(body instanceof FormData);
    uploadedFiles.push({ file: body.get('file'), supplier: body.get('supplier_code') });
    return reply({ id: 20, invoice_number: null });
  }
  if (url.pathname === '/api/product-editor/options') return reply({ shops: [{ code: 'biketrek', name: 'BIKETREK' }] });
  if (url.pathname === '/api/invoices/stats') return reply({ ...stats(url.searchParams.get('direction')), include_archived: url.searchParams.get('include_archived') === 'true' });
  if (url.pathname === '/api/invoices') {
    const page = Number(url.searchParams.get('page')); const direction = url.searchParams.get('direction');
    let rows = direction === 'sale' ? [invoices.get(13)] : page === 2 ? [invoices.get(12)] : [invoices.get(10), invoices.get(11), invoices.get(12), ...(invoices.has(14) ? [invoices.get(14)] : [])];
    if (url.searchParams.get('include_archived') !== 'true') rows = rows.filter(row => !row.archived_at);
    const value = { items: clone(rows), total: 26, page, page_size: 25, total_pages: 2 };
    if (deferredList) { deferredList = false; return new Promise(resolve => { releaseList = () => resolve(reply(value)); }); }
    return reply(value);
  }
  if (url.pathname.endsWith('/export') || url.pathname.endsWith('/download')) {
    if (downloadFail) return failure(409, 'invoice_export_original_missing');
    return { ok: true, status: 200, headers: new Headers({ 'Content-Disposition': 'attachment; filename="originals.zip"' }), blob: async () => new Blob(['Synthetic original'], { type: 'application/zip' }) };
  }
  if (url.pathname === '/api/invoices/reconcile') {
    assert.equal(body.limit, 100);
    return reply({ supplier_code: body.supplier_code, scanned: 100, created: 2, existing: 98, conflicts: [], next_after_key: body.after_key ? null : 'source-key-100' });
  }
  if (url.pathname === '/api/invoices/sync/shop/biketrek') {
    assert.equal(body.date_from, '2026-10-01'); assert.equal(body.date_to, '2026-10-22');
    if (body.offset) assert.equal(body.page_token, 'same-page-token', 'Continuation preserves upstream page identity');
    if (shopSyncMode === 'list-http') return failure(502, 'invoice_source_http_failed', false, {
      upstream_status: 400, operation: 'list', url: 'https://private.invalid/PRIVATE-INVOICE', body: 'private credentials and customer',
      rejected_parameters: ['creation_time_from', 'private', 'query.page', 'creation_time_from'],
    });
    if (shopSyncMode === 'invalid-diagnostic') return failure(502, 'invoice_source_http_failed', false, {
      upstream_status: '400 private', operation: 'https://private.invalid/PRIVATE-INVOICE',
    });
    if (shopSyncMode === 'pdf-http') return reply({ imported: 1, unchanged: 0, failed: 0, items: [],
      next: { page: 1, offset: 1, page_token: 'same-page-token' }, total_in_source: 12, source_pages: 1,
      stopped_error: 'invoice_source_http_failed', upstream_status: 503, operation: 'pdf' });
    return reply({ imported: 2, unchanged: 0, failed: 0, items: [], next: body.offset ? null : { page: 1, offset: 10, page_token: 'same-page-token' }, total_in_source: 12, source_pages: 1 });
  }
  const recovery = url.pathname.match(/^\/api\/invoices\/(\d+)\/payment-operations\/(.+)$/);
  if (recovery) return hideRecovery || !operations.has(recovery[2]) ? failure(404, 'invoice_payment_operation_not_found', true) : reply(acknowledgment(operations.get(recovery[2]), paymentAckMode, 'payment'));
  const payment = url.pathname.match(/^\/api\/invoices\/(\d+)\/payments(?:\/(\d+)\/reverse)?$/);
  if (payment) {
    assert.equal(init.method, 'POST'); assert.match(body.request_id, /^[0-9a-f-]{36}$/i);
    if (attempts.has(body.request_id)) assert.deepEqual(body, attempts.get(body.request_id), 'An uncertain retry retains the entire payload and UUID');
    else attempts.set(body.request_id, clone(body));
    if (operations.has(body.request_id)) return reply(acknowledgment(operations.get(body.request_id), paymentAckMode, 'payment'));
    if (paymentMode === 'conflict') return failure(409, 'invoice_payment_revision_changed', true);
    if (paymentMode === 'lost-before') throw new TypeError('Synthetic request lost before persistence');
    const value = applyPayment(Number(payment[1]), body, payment[2] ? Number(payment[2]) : undefined); operations.set(body.request_id, value);
    if (paymentMode === 'lost-after') throw new TypeError('Synthetic response lost after persistence');
    return reply(acknowledgment(value, paymentAckMode, 'payment'));
  }
  const processingRecovery = url.pathname.match(/^\/api\/invoices\/(\d+)\/processing-operations\/(.+)$/);
  if (processingRecovery) return processingOperations.has(processingRecovery[2]) ? reply(acknowledgment(processingOperations.get(processingRecovery[2]), processingAckMode, 'processing')) : failure(404, 'invoice_processing_operation_not_found', true);
  const processing = url.pathname.match(/^\/api\/invoices\/(\d+)\/processing\/(preview|apply)$/);
  if (processing) {
    assert.equal(init.method, 'POST'); assert(body.note.trim());
    const invoice = invoices.get(Number(processing[1]));
    if (processing[2] === 'preview') {
      assert.equal(body.expected_revision, invoice.processing.revision);
      const value = { invoice_id: invoice.id, invoice_number: invoice.invoice_number, action: body.action, note: body.note,
        preview_hash: 'a'.repeat(64), before: invoice.processing, after: processingAfter(invoice.processing, body.action), stock_effect: false, payment_effect: false };
      if (processingPreviewMode === 'wrong-invoice') value.invoice_id = 9999;
      if (processingPreviewMode === 'wrong-note') value.note = 'Different operator intent';
      if (processingPreviewMode === 'wrong-hash') value.preview_hash = 'bad';
      if (processingPreviewMode === 'malformed') value.before = {};
      return reply(value);
    }
    assert.equal(body.confirmed, true); assert.match(body.request_id, /^[0-9a-f-]{36}$/i);
    if (processingAttempts.has(body.request_id)) assert.deepEqual(body, processingAttempts.get(body.request_id), 'Processing retry retains exact reason, preview, revision and UUID');
    else processingAttempts.set(body.request_id, clone(body));
    if (processingOperations.has(body.request_id)) return reply(acknowledgment(processingOperations.get(body.request_id), processingAckMode, 'processing'));
    if (processingMode === 'conflict') return failure(409, 'invoice_processing_changed', true);
    if (processingMode === 'lost-before') throw new TypeError('Synthetic processing request lost before persistence');
    assert.equal(body.expected_revision, invoice.processing.revision);
    const before = clone(invoice.processing), after = processingAfter(before, body.action);
    after.history.unshift({ request_id: body.request_id, revision: after.revision, action: body.action, note: body.note,
      actor: 'Fixture operator', created_at: '2026-11-09T12:00:00Z', before_data: before, after_data: { ...after, history: [] } });
    invoice.processing = after; invoice.processing_revision = after.revision;
    invoice.processing_status = after.manual_status; invoice.archived_at = after.archived_at;
    const result = { invoice_id: invoice.id, request_id: body.request_id, action: body.action, processing_revision: after.revision,
      processing: clone(after), stock_effect: false, payment_effect: false };
    processingOperations.set(body.request_id, result);
    if (processingMode === 'lost-after') throw new TypeError('Synthetic processing response lost after persistence');
    return reply(acknowledgment(result, processingAckMode, 'processing'));
  }
  const detail = url.pathname.match(/^\/api\/invoices\/(\d+)$/);
  if (detail) {
    const invoice = invoices.get(Number(detail[1])); assert(invoice);
    if (init.method === 'PATCH') { assert.equal(body.expected_revision, invoice.payment_revision); Object.assign(invoice, body); invoice.payment_revision++; invoice.finance.payment_revision++; return reply({ id: invoice.id, payment_revision: invoice.payment_revision }); }
    return reply(invoice);
  }
  throw new Error(`Unexpected API ${init.method}: ${path}`);
};

(async () => {
  await act(async () => { await i18n.changeLanguage('en'); unlockHub('synthetic-invoice-token'); });
  assert.equal(invoiceMoney('9007199254740993.00', 'EUR', 'en'), '9,007,199,254,740,993.00 EUR', 'Display does not lose monetary digits');
  assert.equal(invoiceMoney(null), '—'); assert.match(invoiceMoney('0.00'), /^0,/);
  assert.equal(invoiceSourceDiagnostic({ upstream_status: '400 private', operation: 'list' }, i18n.t), 'Shop invoice list');
  assert.equal(invoiceSourceDiagnostic({ upstream_status: 400, operation: 'https://private.invalid' }, i18n.t), '');
  const rejected = { upstream_status: 400, operation: 'list', rejected_parameters: ['creation_time_to', 'page', 'creation_time_from', 'private', 'page'] };
  assert.equal(invoiceSourceDiagnostic(rejected, i18n.t), 'Shop invoice list (HTTP 400). The shop rejected: document creation period start, document creation period end, list page.');
  assert.equal(invoiceSourceDiagnostic({ ...rejected, operation: 'pdf' }, i18n.t), 'Original PDF download (HTTP 400)');
  assert.equal(invoiceSourceDiagnostic({ ...rejected, upstream_status: 500 }, i18n.t), 'Shop invoice list (HTTP 500)');
  assert.equal(invoiceSourceDiagnostic({ ...rejected, rejected_parameters: 'page' }, i18n.t), 'Shop invoice list (HTTP 400)');
  await act(async () => { await i18n.changeLanguage('sk'); });
  assert.equal(invoiceSourceDiagnostic(rejected, i18n.t), 'Zoznam faktúr v e-shope (HTTP 400). E-shop odmietol: začiatok obdobia vytvorenia dokladov, koniec obdobia vytvorenia dokladov, stranu zoznamu.');
  await act(async () => { await i18n.changeLanguage('en'); });
  await render('/invoices?page=2');
  assert.equal(calls.filter(call => new URL(call.path, dom.window.location.href).pathname === '/api/invoices').at(-1).path.includes('page=2'), true, 'Opening a paginated URL does not reset the page');
  await click('invoice-prev'); assert(element('invoice-row-10'));
  assert(required('invoice-row-12').textContent.includes('0.00 EUR'), 'Known zero is not shown as missing');
  assert(!element('invoice-dashboard'), 'Overview is collapsed by default');
  assert(!element('invoice-dropzone'), 'Drop and manual upload appear only after Add invoice');
  assert(!element('calendar-main-grid'));
  assert.equal(calls.filter(call => call.path.startsWith('/api/invoices/calendar')).length, 0, 'Hidden calendar makes no request');
  assert(required('invoice-archive-status').textContent.includes('BAD-1'), 'Automatic indexing exposes document conflicts');
  assert(!element('invoice-reconcile-open'), 'Routine imports do not require reconciliation clicks');
  await click('invoice-calendar-toggle'); assert(element('calendar-main-grid'));
  await click('invoice-calendar-toggle'); assert(!element('calendar-main-grid'));
  assert.equal(calls.filter(call => call.path.startsWith('/api/invoices/stats')).length, 0, 'Hidden overview does not load financial summaries');
  assert.equal(required('invoice-summary-toggle').getAttribute('aria-expanded'), 'false');
  assert(required('invoice-supplier-filter').textContent.includes('Archived inactive vendor'), 'Historical inactive suppliers remain available');
  await input('invoice-supplier-filter', 'fixture-vendor');
  assert.equal(router.state.location.search.includes('supplier_code=fixture-vendor'), true);
  assert.equal(new URLSearchParams(router.state.location.search).get('page'), '1', 'Supplier selection resets pagination');
  await click('invoice-sort-due_date');
  let sortedUrl = new URL(calls.filter(call => call.path.startsWith('/api/invoices?')).at(-1).path, dom.window.location.href);
  assert.equal(sortedUrl.searchParams.get('supplier_code'), 'fixture-vendor');
  assert.equal(sortedUrl.searchParams.get('sort_by'), 'due_date'); assert.equal(sortedUrl.searchParams.get('sort_order'), 'asc');
  assert.equal(required('invoice-sort-due_date').closest('th').getAttribute('aria-sort'), 'ascending');
  await click('invoice-sort-due_date'); assert.equal(required('invoice-sort-due_date').closest('th').getAttribute('aria-sort'), 'descending');
  await click('invoice-sort-outstanding_amount');
  assert.equal(new URLSearchParams(router.state.location.search).get('sort_by'), 'outstanding_amount');
  await click('invoice-summary-toggle');
  assert(!required('invoice-dashboard').textContent.includes('220.00'), 'Currencies are never added together');
  assert(element('invoice-unknown-payments'), 'Unknown payment amounts are explained even when invoice totals are complete');
  await input('dashboard-currency', 'USD'); assert(required('invoice-dashboard').textContent.includes('20.00 USD'));
  assert(!required('invoice-dashboard').textContent.includes('200.00 EUR'));
  await click('invoice-select-page'); await click('invoice-batch-download');
  assert.deepEqual(calls.find(call => call.path === '/api/invoices/export').body, { invoice_ids: [10, 11, 12] });
  assert.equal(downloads.length, 1); assert.equal(downloads[0].filename, 'originals.zip');
  await click('invoice-status-overdue');
  const overdueUrl = new URL(calls.filter(call => call.path.startsWith('/api/invoices?')).at(-1).path, dom.window.location.href);
  assert.equal(overdueUrl.searchParams.get('is_overdue'), 'true'); assert.equal(overdueUrl.searchParams.has('payment_status'), false, 'Overdue includes partial payments too');
  assert(required('invoice-batch-download').disabled, 'Filter changes clear export selection');
  downloadFail = true; await click('invoice-download-10'); assert(required('invoice-error').textContent.includes('invoice_export_original_missing')); assert.equal(downloads.length, 1); downloadFail = false;
  const uploadsBefore = calls.filter(call => call.path === '/api/invoices/upload').length;
  await click('invoice-upload-open');
  await dropFiles([new File(['synthetic scan'], 'invoice-scan.png', { type: 'image/png' })]);
  assert(element('invoice-upload-form')); assert.equal(required('invoice-selected-file').textContent, 'invoice-scan.png');
  assert.equal(required('upload-supplier').value, 'fixture-vendor', 'The selected supplier is used for a dropped invoice');
  assert.equal(calls.filter(call => call.path === '/api/invoices/upload').length, uploadsBefore, 'Dropping never submits automatically');
  await click('invoice-upload-save');
  assert.equal(uploadedFiles.length, 1); assert.equal(uploadedFiles[0].file.name, 'invoice-scan.png'); assert.equal(uploadedFiles[0].supplier, 'fixture-vendor');
  assert(!element('invoice-upload-form'), 'Successful original upload closes the form');
  assert(!element('invoice-dropzone')); await click('invoice-upload-open');
  await dropFiles([new File(['bad'], 'unsupported.exe')]);
  assert(element('invoice-upload-error')); assert(required('invoice-upload-save').disabled);
  assert.equal(uploadedFiles.length, 1, 'Invalid dropped formats never reach the upload API');
  await dropFiles([new File(['1'], 'one.pdf'), new File(['2'], 'two.pdf')]);
  assert(required('invoice-upload-error').textContent.includes('one invoice'));
  await dropFiles([new File([], 'empty.pdf')]); assert(required('invoice-upload-error').textContent.includes('nonempty'));
  const oversized = new File(['synthetic'], 'large.pdf'); Object.defineProperty(oversized, 'size', { value: 50 * 1024 * 1024 + 1 });
  await dropFiles([oversized]); assert(required('invoice-upload-save').disabled);
  const chosenScan = new File(['synthetic PDF'], 'chosen-scan.pdf', { type: 'application/pdf' });
  await act(async () => { Object.defineProperty(required('invoice-upload-file'), 'files', { configurable: true, value: [chosenScan] }); required('invoice-upload-file').dispatchEvent(new dom.window.Event('change', { bubbles: true })); await tick(); });
  assert.equal(required('invoice-selected-file').textContent, 'chosen-scan.pdf', 'File chooser shares the same validation and original retention flow');
  assert(!element('invoice-upload-error'));
  assert.equal(calls.filter(call => call.path === '/api/invoices/reconcile').length, 0, 'The invoice UI never needs manual indexing');
  await click('invoice-calendar-toggle');
  await click('invoice-direction-sale'); assert(element('invoice-row-13')); assert(!element('invoice-upload-open'));
  assert(!element('invoice-calendar-toggle')); assert(!element('calendar-main-grid'), 'Shop invoices do not display the due calendar');
  await click('invoice-sync-open'); await input('sync-shop', 'biketrek'); await input('sync-date_from', '2026-10-01'); await input('sync-date_to', '2026-10-22'); await click('invoice-sync-start');
  assert.equal(calls.filter(call => call.path.includes('/sync/shop/')).length, 1);
  await click('invoice-sync-next'); assert.equal(calls.filter(call => call.path.includes('/sync/shop/')).length, 2);
  shopSyncMode = 'list-http'; await click('invoice-sync-start');
  assert(required('invoice-error').textContent.includes('Shop invoice list (HTTP 400)'));
  assert(required('invoice-error').textContent.includes('invoice_source_http_failed'));
  assert(required('invoice-error').textContent.includes('The shop rejected: document creation period start.'));
  assert(!required('invoice-error').textContent.includes('private'), 'Only allowed diagnostics reach the visible error');
  await act(async () => { await i18n.changeLanguage('sk'); });
  shopSyncMode = 'pdf-http'; await click('invoice-sync-start');
  assert(required('invoice-sync-stopped').textContent.includes('Stiahnutie originálu PDF (HTTP 503)'));
  assert(required('invoice-sync-stopped').textContent.includes('invoice_source_http_failed'));
  assert.equal(calls.filter(call => call.path.includes('/sync/shop/')).length, 4, 'Diagnostic failures are never retried automatically');
  shopSyncMode = 'invalid-diagnostic'; await click('invoice-sync-start');
  assert(!required('invoice-error').textContent.includes('private'), 'Unexpected operation/status values are not rendered');
  assert(!required('invoice-error').textContent.includes('HTTP 400'));
  shopSyncMode = 'success'; await act(async () => { await i18n.changeLanguage('en'); });
  await render('/invoices/10');
  assert(calls.some(call => call.path === '/api/invoices/10'));
  const receipt = new URL(required('invoice-receiving-link').href);
  assert.equal(decodeURIComponent(receipt.pathname), '/receiving/SUP/2026 98'); assert.equal(receipt.searchParams.get('supplier'), 'fixture-vendor'); assert.equal(receipt.searchParams.get('session'), '67');
  assert(!calls.some(call => /undefined|NaN/.test(call.path)), 'Detail uses the real invoiceId route parameter');
  assert(document.querySelector('a[href="/products/card/18"]'), 'Linked invoice products use the shared stock card');
  await click('invoice-payment-open');
  assert.equal(required('invoice-payment-amount').value, '100.00');
  assert.equal(required('invoice-payment-date').value, todayDate());
  await input('invoice-payment-amount', '12,34'); await input('invoice-payment-date', '2026-10-22');
  const paymentPostsBeforeStorageFailure = calls.filter(call => /\/10\/payments$/.test(call.path)).length;
  for (const silent of [false, true]) {
    await withUnavailableStorage(silent, () => click('invoice-payment-save'));
    assert.equal(calls.filter(call => /\/10\/payments$/.test(call.path)).length, paymentPostsBeforeStorageFailure, 'Denied or silently discarded pending storage prevents a payment POST');
    assert.match(required('invoice-detail-error').textContent, /nothing was sent/i);
    assert(!element('invoice-payment-pending'));
  }
  paymentMode = 'lost-after'; hideRecovery = true; await click('invoice-payment-save');
  assert(element('invoice-payment-pending')); assert(required('invoice-edit-open').disabled, 'Uncertain payment blocks metadata changes');
  const first = calls.filter(call => /\/10\/payments$/.test(call.path)).at(-1); assert.equal(first.body.amount, '12.34'); assert.equal(typeof first.body.amount, 'string');
  assert.equal(JSON.parse(sessionStorage.getItem('hub.invoice-payment.10')).body.request_id, first.body.request_id);
  hideRecovery = false; await click('invoice-payment-recover'); assert(!element('invoice-payment-pending'));
  assert.equal(calls.filter(call => /\/10\/payments$/.test(call.path)).length, 1, 'Recovery GET does not write another payment');
  assert(!element('invoice-reverse-90'), 'Opening balance is never offered as reversible');
  paymentMode = 'lost-before'; await click('invoice-payment-open');
  assert.equal(required('invoice-payment-amount').value, '87.66', 'A partial invoice defaults only its remaining balance');
  await input('invoice-payment-amount', '0.01'); await input('invoice-payment-date', '2026-10-22'); await click('invoice-payment-save');
  const lostBody = clone(calls.filter(call => /\/10\/payments$/.test(call.path)).at(-1).body); assert(element('invoice-payment-pending'));
  await render('/invoices/10'); assert(element('invoice-payment-pending'), 'Uncertain UUID survives a page remount');
  const paymentRetryBeforeSwitch = required('invoice-payment-retry'), paymentRecoverBeforeSwitch = required('invoice-payment-recover');
  const paymentCallsBeforeSwitch = calls.filter(call => /\/10\/(payments|payment-operations)/.test(call.path)).length;
  await act(async () => {
    setHubUser({ id: 42, username: 'other-operator', display_name: 'Other operator', role: 'operator', active: true });
    paymentRetryBeforeSwitch.click(); paymentRecoverBeforeSwitch.click(); await tick();
  });
  assert(!element('invoice-payment-pending'), 'Another account cannot inherit a pending payment');
  assert.equal(calls.filter(call => /\/10\/(payments|payment-operations)/.test(call.path)).length, paymentCallsBeforeSwitch);
  assert.deepEqual(JSON.parse(sessionStorage.getItem('hub.invoice-payment.10')).body, lostBody, 'Account change retains the original user\'s pending identity');
  await act(async () => { setHubUser(null); unlockHub('synthetic-invoice-token'); await tick(); });
  assert(element('invoice-payment-pending'));
  paymentMode = 'success'; await click('invoice-payment-retry');
  assert.deepEqual(calls.filter(call => /\/10\/payments$/.test(call.path)).at(-1).body, lostBody); assert(!element('invoice-payment-pending'));
  paymentMode = 'conflict'; await click('invoice-payment-open'); await input('invoice-payment-amount', '2.00'); await input('invoice-payment-date', '2026-10-22'); await click('invoice-payment-save');
  assert(element('invoice-conflict-reload')); assert(!element('invoice-payment-pending')); assert(required('invoice-edit-open').disabled);
  paymentMode = 'success'; await click('invoice-conflict-reload');
  await click('invoice-reverse-101'); await input('invoice-reverse-note', 'Duplicate bank statement entry'); await click('invoice-reverse-confirm');
  assert(required('invoice-payment-row-101').textContent.includes('Corrected by a subsequent entry')); assert(!element('invoice-reverse-101'));
  assert(calls.some(call => call.path === '/api/invoices/10/payments/101/reverse' && call.body.note === 'Duplicate bank statement entry'));
  await render('/invoices/11'); await click('invoice-edit-open'); assert(required('invoice-edit-total').disabled, 'Supplier source facts cannot be overwritten');
  await input('invoice-edit-notes', 'Accountant review'); await click('invoice-edit-save');
  const metadata = calls.filter(call => call.path === '/api/invoices/11' && call.method === 'PATCH').at(-1).body;
  assert.deepEqual(Object.keys(metadata).sort(), ['due_date', 'expected_revision', 'notes']);
  await click('invoice-payment-open'); await input('invoice-payment-amount', '1.00'); await input('invoice-payment-date', '2026-10-22');
  assert(required('invoice-payment-save').disabled, 'Unknown opening amount is not guessed as zero');
  await input('invoice-opening-paid', '0.00'); assert(!required('invoice-payment-save').disabled); await click('invoice-payment-save');
  assert.equal(calls.filter(call => /\/11\/payments$/.test(call.path)).at(-1).body.opening_paid_amount, '0.00');
  await render('/invoices/13'); await click('invoice-edit-open'); assert(required('invoice-edit-invoice_number').disabled); assert(required('invoice-edit-total').disabled);
  assert(!element('invoice-receiving-link'), 'Sales archive cannot create a stock receipt');
  invoices.set(14, makeInvoice(14, { receiving_status: 'not_started', receiving_session_id: null, receiving_invoice_number: null,
    finance: finance({ paid_amount: null, outstanding_amount: null, status: 'unknown', opening_required: true }),
    processing_status: 'unprocessed', processing_revision: 0, archived_at: null,
    processing: { revision: 0, status: 'unprocessed', manual_status: 'unprocessed', archived: false, archived_at: null,
      receipts: [{ id: 88, invoice_number: 'OLD-TEST-RECEIPT', status: 'cancelled', archived: true, received: true, reversed: false, posting_revision: 0 }],
      stock_effect: false, payment_effect: false, allowed_actions: ['process_without_stock', 'archive'], action_blockers: {}, history: [] } }));
  const originalFinance = clone(invoices.get(14).finance), paymentsBeforeProcessing = operations.size;
  await render('/invoices/14');
  assert.match(required('invoice-archived-receipts').textContent, /original receipt retained/i, 'Archived stock history remains visible without owning current handling');
  assert(required('invoice-processing-preview').disabled, 'A reason is mandatory even for a preview');
  assert(!element('invoice-processing-confirm'));
  await input('invoice-processing-note', '  Paperwork reviewed; no goods received  ');
  for (const mode of ['wrong-invoice', 'wrong-note', 'wrong-hash', 'malformed']) {
    processingPreviewMode = mode; await click('invoice-processing-preview');
    assert(element('invoice-processing-error')); assert(!element('invoice-processing-preview-result'), 'Malformed or unbound preview never becomes confirmable');
    assert.equal(processingOperations.size, 0);
  }
  processingPreviewMode = 'valid';
  await click('invoice-processing-preview');
  assert(element('invoice-processing-preview-result')); assert(element('invoice-processing-no-effects'));
  assert.equal(processingOperations.size, 0, 'Reviewing a preview never changes invoice handling');
  assert.equal(invoices.get(14).processing.status, 'unprocessed');
  const processingPostsBeforeStorageFailure = calls.filter(call => call.path === '/api/invoices/14/processing/apply').length;
  for (const silent of [false, true]) {
    await withUnavailableStorage(silent, () => click('invoice-processing-confirm'));
    assert.equal(calls.filter(call => call.path === '/api/invoices/14/processing/apply').length, processingPostsBeforeStorageFailure, 'Denied or silently discarded pending storage prevents processing writes');
    assert.match(required('invoice-processing-error').textContent, /nothing was sent/i);
    assert(!element('invoice-processing-pending'));
  }
  processingMode = 'lost-after'; await click('invoice-processing-confirm');
  assert.equal(processingOperations.size, 1, 'Lost response is recovered without a new operation');
  assert(!element('invoice-processing-pending')); assert(required('invoice-processing-status').textContent.includes('without stock'));
  let processingBody = calls.filter(call => call.path === '/api/invoices/14/processing/apply').at(-1).body;
  assert.equal(processingBody.note, 'Paperwork reviewed; no goods received'); assert.equal(processingBody.confirmed, true);
  assert.deepEqual(invoices.get(14).finance, originalFinance, 'Handling never guesses or edits unknown payments');
  await input('invoice-processing-action', 'reopen'); await input('invoice-processing-note', 'Review the paperwork again');
  await click('invoice-processing-preview'); processingMode = 'lost-before'; await click('invoice-processing-confirm');
  assert(element('invoice-processing-pending')); assert(required('invoice-payment-open').disabled, 'Pending operation cannot be bypassed through another workflow');
  const lostProcessingBody = clone(calls.filter(call => call.path === '/api/invoices/14/processing/apply').at(-1).body);
  await render('/invoices/14'); assert(element('invoice-processing-pending'), 'Exact pending operation survives a remount');
  const processingRetryBeforeSwitch = required('invoice-processing-retry'), processingRecoverBeforeSwitch = required('invoice-processing-recover');
  const processingCallsBeforeSwitch = calls.filter(call => /\/14\/(processing\/apply|processing-operations)/.test(call.path)).length;
  await act(async () => {
    setHubUser({ id: 42, username: 'other-operator', display_name: 'Other operator', role: 'operator', active: true });
    processingRetryBeforeSwitch.click(); processingRecoverBeforeSwitch.click(); await tick();
  });
  assert(!element('invoice-processing-pending'), 'Another account cannot recover or retry the original operation');
  assert.equal(calls.filter(call => /\/14\/(processing\/apply|processing-operations)/.test(call.path)).length, processingCallsBeforeSwitch);
  await act(async () => { setHubUser({ id: 42, username: 'other-operator', display_name: 'Other operator', role: 'admin', active: true }); await tick(); });
  assert(!element('invoice-processing-pending'), 'Changing role does not adopt another actor\'s saved operation');
  assert.deepEqual(JSON.parse(sessionStorage.getItem('hub.invoice-processing.14')).body, lostProcessingBody);
  await act(async () => { setHubUser(null); unlockHub('synthetic-invoice-token'); await tick(); });
  assert(element('invoice-processing-pending'));
  processingMode = 'success'; await click('invoice-processing-retry');
  assert.deepEqual(calls.filter(call => call.path === '/api/invoices/14/processing/apply').at(-1).body, lostProcessingBody);
  assert.equal(invoices.get(14).processing.status, 'unprocessed'); assert(!element('invoice-processing-pending'));
  assert.match(required('invoice-archived-receipts').textContent, /original receipt retained/i);
  await input('invoice-processing-action', 'archive'); await input('invoice-processing-note', 'Retire the synthetic document');
  await click('invoice-processing-preview'); assert.equal(invoices.get(14).archived_at, null);
  await click('invoice-processing-confirm'); assert(element('invoice-archived-badge'));
  assert(required('invoice-payment-open').disabled); assert(required('invoice-edit-open').disabled);
  await render('/invoices'); assert(!element('invoice-row-14'), 'Default view excludes archived documents');
  await click('invoice-include-archived'); await click('invoice-apply-filters'); assert(element('invoice-row-14'));
  assert.equal(new URLSearchParams(router.state.location.search).get('include_archived'), 'true');
  await click('invoice-summary-toggle');
  assert(calls.filter(call => call.path.startsWith('/api/invoices/stats')).at(-1).path.includes('include_archived=true'));
  assert.match(required('invoice-dashboard').textContent, /including archived/i);
  await render('/invoices/14'); assert.equal(required('invoice-processing-action').value, 'restore');
  await input('invoice-processing-note', 'Restore the same original'); await click('invoice-processing-preview');
  processingMode = 'conflict'; await click('invoice-processing-confirm');
  assert(element('invoice-processing-error')); assert(!element('invoice-processing-pending'), 'Known stale rejection permits a fresh preview');
  assert(invoices.get(14).archived_at);
  processingMode = 'success'; await click('invoice-processing-preview'); await click('invoice-processing-confirm');
  assert.equal(invoices.get(14).archived_at, null); assert(!element('invoice-archived-badge'));
  assert.equal(invoices.get(14).processing.history.length, 4);
  assert.deepEqual(invoices.get(14).finance, originalFinance); assert.equal(operations.size, paymentsBeforeProcessing);
  for (const mode of ['malformed', 'wrong-invoice', 'wrong-uuid', 'wrong-action', 'wrong-revision']) {
    const action = invoices.get(14).processing.manual_status === 'unprocessed' ? 'process_without_stock' : 'reopen';
    await input('invoice-processing-action', action); await input('invoice-processing-note', `Verify ${mode} acknowledgment recovery`);
    await click('invoice-processing-preview'); const count = processingOperations.size;
    processingAckMode = mode; await click('invoice-processing-confirm');
    assert(element('invoice-processing-pending'), 'Malformed or unrelated HTTP 200 must retain the pending UUID');
    assert.equal(processingOperations.size, count + 1);
    const body = calls.filter(call => call.path === '/api/invoices/14/processing/apply').at(-1).body;
    assert.equal(JSON.parse(sessionStorage.getItem('hub.invoice-processing.14')).body.request_id, body.request_id);
    processingAckMode = 'valid'; await click('invoice-processing-recover');
    assert(!element('invoice-processing-pending')); assert.equal(processingOperations.size, count + 1, 'Recovery never writes a replacement operation');
  }
  invoices.get(14).processing = { ...invoices.get(14).processing, status: 'received', allowed_actions: [],
    action_blockers: { process_without_stock: ['invoice_processing_receipt_owned'], archive: ['invoice_processing_archive_receipt_first'] } };
  await render('/invoices/14'); assert(!element('invoice-processing-action'), 'A receipt-backed invoice is not offered a no-stock processing action');
  assert(required('invoice-processing-status').textContent.includes('receipt'));
  await render('/invoices/10');
  for (const mode of ['malformed', 'wrong-invoice', 'wrong-uuid', 'wrong-revision']) {
    await click('invoice-payment-open'); await input('invoice-payment-amount', '0.01');
    const count = operations.size; paymentAckMode = mode; await click('invoice-payment-save');
    assert(element('invoice-payment-pending'), 'Malformed or unrelated payment HTTP 200 cannot erase recovery state');
    const body = calls.filter(call => /\/10\/payments$/.test(call.path)).at(-1).body;
    assert.equal(JSON.parse(sessionStorage.getItem('hub.invoice-payment.10')).body.request_id, body.request_id);
    assert.equal(operations.size, count + 1);
    paymentAckMode = 'valid'; await click('invoice-payment-recover');
    assert(!element('invoice-payment-pending')); assert.equal(operations.size, count + 1);
  }
  assert(!calls.some(call => /\/issue|\/billing|\/stock\/|\/payment$/.test(call.path)), 'Archive never issues documents or uses status-only payment shortcuts');
  await act(async () => root.unmount());
  console.log('invoice UI: suppliers, financial overview, original uploads/exports, payment recovery, reasoned processing/archive, exact UUID retries, archived filters and receipt safeguards passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
