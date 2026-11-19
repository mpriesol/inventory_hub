/* Mixed receiving with synthetic API responses. No production/supplier writes. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'https://hub.example.test/receiving' });
global.window = dom.window; global.document = dom.window.document;
Object.defineProperty(global, 'navigator', { value: dom.window.navigator, configurable: true });
global.HTMLElement = dom.window.HTMLElement; global.sessionStorage = dom.window.sessionStorage;
global.IS_REACT_ACT_ENVIRONMENT = true;
for (const ext of ['.ts', '.tsx']) require.extensions[ext] = (module, filename) => module._compile(ts.transpileModule(
  fs.readFileSync(filename, 'utf8').replace(/import\.meta\.env/g, '{}'),
  { compilerOptions: { jsx: ts.JsxEmit.React, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, esModuleInterop: true } }).outputText, filename);
require.extensions['.css'] = () => {};
const React = require('react'); const { act } = React; const { createRoot } = require('react-dom/client');
const { MemoryRouter, Routes, Route, useLocation } = require('react-router-dom');
const i18n = require('../src/i18n/index.ts').default;
const { MixedReceivingPage } = require('../src/pages/MixedReceivingPage.tsx');
const { BarcodeCamera } = require('../src/components/BarcodeCamera.tsx');
const root = createRoot(document.getElementById('root'));
const tick = () => new Promise(resolve => setTimeout(resolve, 8));
const find = id => document.querySelector(`[data-testid="${id}"]`);
const response = (value, status = 200) => ({ ok: status < 400, status, statusText: status === 200 ? 'OK' : 'Conflict',
  headers: { get: () => 'application/json' }, text: async () => JSON.stringify(value), json: async () => value });
const id = 'c40e9329-dfed-420b-b28c-3c26f95712b5';
let scans = [], posted = 0, status = 'open', requests = [], failAck = false, malformedAck = false, loseFinalize = false, needsChoice = false, uploadCount = 0, blocked = false, loseManual = false, loseArchive = false, archiveCount = 0, openDrafts = [], archived = false, chosenWarehouse = 'main';
const seen = new Map(), mutations = new Map();
const candidate = (supplier = 'fixture-two', invoice = 'INV-2') => ({ supplier, supplier_name: supplier, invoice_no: invoice,
  invoice_date: '2026-11-12', session_id: 2, status: 'open', lines: [{ id: 52, line_number: 1, title: 'Synthetic item', ordered_qty: '8', received_qty: '0', remaining_qty: '8', unit_price: '12.50' }] });
function summary() {
  const active = scans.filter(scan => scan.status === 'active');
  return { id, status, archived, warehouse_code: 'main', warehouse_name: 'Main test warehouse', created_at: '2026-11-16T12:00:00Z', completed_at: status === 'completed' ? '2026-11-16T12:30:00Z' : null,
    scans: structuredClone(scans), total_qty: String(active.length), blockers: blocked ? [{ code: 'receiving_product_preparation_required', line_id: 51, session_id: 1 }] : [],
    items: [...new Map(active.map(scan => [scan.source_kind === 'manual' ? scan.id : `${scan.session_id}:${scan.line_id}`, scan])).values()].map(scan => ({ scan_id: scan.id, source_kind: scan.source_kind, manual_reason: scan.manual_reason, cost_status: scan.cost_status, session_id: scan.session_id, supplier: scan.supplier, invoice_no: scan.invoice_no, line_id: scan.line_id, title: scan.title, qty: '1',
      product_id: blocked ? null : 91, sku: blocked ? null : 'AA-TEST', card_status: blocked ? 'feed_only' : 'matched', can_prepare: blocked, draft_id: null, warnings: [],
      stock_qty_before: '2', stock_qty_after: '3', stock_quantity: '1', stock_unit: 'pcs', stock_confirmed: true,
      shops: [{ code: 'biketrek', name: 'BIKETREK', linked: true, sync_enabled: true, status: status === 'completed' ? 'queued' : 'ready' },
        { code: 'xtrek', name: 'xTrek', linked: false, sync_enabled: false, status: 'unlinked' }] })),
    invoices: [...new Map(active.filter(scan => scan.source_kind !== 'manual').map(scan => [scan.invoice_no, { supplier: scan.supplier, invoice_no: scan.invoice_no, session_id: scan.session_id,
      invoice_date: '2026-11-12', ordered_qty: '8', received_qty: '0', scanned_qty: String(active.filter(x => x.invoice_no === scan.invoice_no).length), complete_after: false }])).values()] };
}
global.fetch = async (path, options = {}) => {
  if (path === '/api/suppliers') return response([{ code: 'fixture', name: 'Synthetic supplier' }]);
  if (path === '/api/invoices/supplier-options') return response([]);
  if (path === '/api/product-editor/options') return response({ warehouses: [{code:'secondary',name:'Secondary'}, {code:'main',name:'Main',is_default:true}], shops:[], brands:[] });
  if (path === '/api/receiving/batches?status=open') return response({ batches: openDrafts });
  if (path === '/api/receiving/batches?status=completed') return response({ batches: [{id, total_qty:'3',warehouse_code:'main',created_at:'2026-11-16T12:00:00Z'}] });
  if (path === '/api/receiving/batches' && options.method === 'POST') { chosenWarehouse = JSON.parse(options.body).warehouse_code; return response(summary()); }
  if (path === `/api/receiving/batches/${id}`) return response(summary());
  if (path.endsWith('/preview')) return response({ ...summary(), preview_hash: 'a'.repeat(64) });
  if (path === '/api/invoices/upload') { uploadCount++; return response({ id:uploadCount+1, invoice_number:'INV-'+(uploadCount+1), supplier_code:'fixture', processing:{status:'complete',parsed_lines:1,stored_lines:1,receiving_ready:true,issues:[],missing_fields:[]} }); }
  if (path.includes('/manual-product?')) return response({ product_id:91,sku:'AA-TEST',title:'Synthetic manual',stock_unit:'pcs' });
  if (path.endsWith('/manual-scan')) {
    const body=JSON.parse(options.body); requests.push(body);
    const replayed=seen.has(body.request_id);
    if (!replayed) {
      const scan = {id:body.request_id,code:body.code,qty:body.qty,source_kind:'manual',supplier:null,supplier_name:null,invoice_no:null,invoice_date:null,session_id:null,line_id:null,line_number:null,
        title:'Synthetic manual',product_id:91,sku:'AA-TEST',unit_price:body.unit_cost,cost_status:body.cost_status,manual_reason:body.reason,status:'active',alternatives:[]};
      scans.push(scan);seen.set(body.request_id,scan);
    }
    if (loseManual) {loseManual=false;throw new Error('Lost manual acknowledgement');}
    return response({request_id:body.request_id,scan_id:body.request_id,replayed,summary:summary()});
  }
  if (path.endsWith('/archive')) {
    const body=JSON.parse(options.body); requests.push(body); const replayed=mutations.has(body.request_id);
    if(!replayed){archiveCount++;mutations.set(body.request_id,body);openDrafts=[];}
    if(loseArchive){loseArchive=false;throw new Error('Lost archive acknowledgement');}
    return response({request_id:body.request_id,batch_id:path.split('/').at(-2),archived:true,replayed});
  }
  if (path.endsWith('/products/prepare')) return response({ draft_id: 'draft-1', created: true });
  if (path.endsWith('/scan')) {
    const body = JSON.parse(options.body); requests.push(body);
    if (body.code === 'LEGACY') return response({ detail: { code: 'mixed_receiving_legacy_pending', candidates: [candidate('fixture', 'OLD-1')] } }, 409);
    if ((body.code === 'NO-INVOICE' && !uploadCount) || body.code === 'MANUAL') return response({ detail: { code: 'mixed_receiving_invoice_missing', candidates: [] } }, 409);
    if (needsChoice && !body.invoice_no) return response({ detail: { code: 'mixed_receiving_invoice_choice', candidates: [candidate()] } }, 409);
    let replayed = seen.has(body.request_id);
    if (!replayed) {
      const other = body.code === 'SECOND' || !!body.invoice_no;
      const scan = { id: body.request_id, code: body.code, qty: body.qty, supplier: body.supplier || (other ? 'fixture-two' : 'fixture'), supplier_name: other ? 'Supplier two' : 'Supplier one',
        invoice_no: body.invoice_no || (other ? 'INV-2' : 'INV-1'), invoice_date: '2026-11-12', session_id: other ? 2 : 1, line_id: body.line_id || 51,
        line_number: 1, title: 'Synthetic ' + body.code, product_id: 91, sku: 'AA-TEST', unit_price: '12.50', status: 'active', alternatives: [candidate(), candidate('fixture-three', 'INV-3')] };
      scans.push(scan); seen.set(body.request_id, scan);
    }
    if (failAck) { failAck = false; throw new Error('Lost acknowledgement'); }
    if (malformedAck) { malformedAck = false; return response({ request_id: body.request_id, scan_id: 'wrong-operation', replayed: false, summary: summary() }); }
    return response({ request_id: body.request_id, scan_id: body.request_id, replayed, summary: summary() });
  }
  if (/\/(reassign|cancel)$/.test(path)) {
    const body = JSON.parse(options.body); const scanId = path.split('/').at(-2), scan = seen.get(scanId);
    const replayed = mutations.has(body.request_id);
    if (!replayed) {
      if (path.endsWith('/cancel')) scan.status = 'cancelled';
      else { scan.supplier = body.supplier; scan.invoice_no = body.invoice_no; scan.line_id = body.line_id; }
      mutations.set(body.request_id, body);
    }
    return response({ request_id: body.request_id, scan_id: scanId, replayed, summary: summary() });
  }
  if (path.endsWith('/finalize')) {
    const body = JSON.parse(options.body); requests.push(body);
    if (!mutations.has(body.request_id)) { posted++; status = 'completed'; mutations.set(body.request_id, body); }
    if (loseFinalize) { loseFinalize = false; throw new Error('Lost finalization acknowledgement'); }
    return response({ request_id: body.request_id, batch_id: id, status, result: {}, summary: summary() });
  }
  throw new Error('Unexpected request ' + path);
};
let location;
function Control() { location = useLocation(); return null; }
function page(entry = '/receiving') { return React.createElement(MemoryRouter, { initialEntries: [entry] }, React.createElement(React.Fragment, null,
  React.createElement(Control), React.createElement(Routes, null,
    React.createElement(Route, { path: '/receiving', element: React.createElement(MixedReceivingPage) }),
    React.createElement(Route, { path: '/product-import', element: React.createElement('div', { 'data-testid': 'prepared-product' }, 'Product preparation') })))); }
async function mount(entry) { await act(async () => { root.render(null); await tick(); }); await act(async () => { root.render(page(entry)); await tick(); await tick(); }); }
async function click(testId) { await act(async () => { assert(find(testId), testId); find(testId).click(); await tick(); await tick(); }); }
async function scan(code) {
  await act(async () => { const input = find('mixed-code');
    Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, 'value').set.call(input, code);
    input.dispatchEvent(new dom.window.Event('input', { bubbles: true })); await tick(); });
  await click('mixed-scan');
}
(async () => {
  await mount();
  assert(find('mixed-upload-open'), 'Upload available before starting a receipt');
  assert.equal(find('mixed-warehouse').value, 'main', 'Configured default wins over warehouse order');
  await act(async()=>{find('mixed-warehouse').value='secondary';find('mixed-warehouse').dispatchEvent(new dom.window.Event('change',{bubbles:true}));await tick();});
  await click('mixed-start'); assert.equal(chosenWarehouse,'secondary');
  assert.equal(find('mixed-code').getAttribute('type'),'text');
  assert.equal(document.activeElement,find('mixed-code'),'Starting receipt focuses EAN input');
  assert.equal(location.search, '?batch=' + id);
  await scan('FIRST'); await scan('SECOND'); await scan('FIRST');
  assert.equal(scans.length, 3); assert.equal(new Set(scans.map(scan => scan.id)).size, 3, 'Every physical piece has its own UUID');
  assert.equal(new Set(scans.map(scan => scan.supplier)).size, 2, 'One batch supports different suppliers');
  assert.equal(posted, 0, 'Scanning never posts physical stock');
  await scan('LEGACY');
  const legacyLink = [...document.querySelectorAll('a')].find(link => link.textContent.includes('OLD-1'));
  assert.equal(legacyLink.getAttribute('href'), '/receiving/OLD-1?supplier=fixture&session=2', 'Legacy drafts offer their original receipt without posting');
  failAck = true; await scan('FIRST');
  assert(find('mixed-retry-scan')); assert(find('mixed-preview').disabled);
  const uncertainRequest = requests.at(-1);
  await mount('/receiving?batch=' + id); await click('mixed-retry-scan');
  assert.deepEqual(requests.at(-1), uncertainRequest, 'Reload retries the original payload and UUID');
  assert.equal(scans.length, 4, 'Retry cannot add another piece');
  malformedAck = true; await scan('MALFORMED');
  assert(find('mixed-retry-scan'), 'A malformed 200 acknowledgement remains retryable');
  const malformedRequest = requests.at(-1); await click('mixed-retry-scan');
  assert.deepEqual(requests.at(-1), malformedRequest);
  needsChoice = true; await scan('CHOICE');
  assert(find('mixed-scan-choice')); const choiceRequest = requests.at(-1);
  await click('mixed-invoice-choice');
  assert.equal(requests.at(-1).request_id, choiceRequest.request_id, 'Explicit rejected choice keeps the scan UUID');
  assert.equal(requests.at(-1).invoice_no, 'INV-2'); needsChoice = false;
  await scan('NO-INVOICE'); assert(find('mixed-missing-invoice')); const missingRequest = requests.at(-1); await click('mixed-upload-open');
  await act(async () => {
    const select = find('upload-supplier'); select.value = 'fixture'; select.dispatchEvent(new dom.window.Event('change', { bubbles: true })); await tick();
  });
  await act(async () => {
    const input = find('invoice-upload-file');
    Object.defineProperty(input, 'files', { value: ['fixture.pdf', 'second.pdf'].map(name => new File(['synthetic'], name, { type: 'application/pdf' })), configurable: true });
    input.dispatchEvent(new dom.window.Event('change', { bubbles: true })); await tick(); await tick();
  });
  const beforeUploadReplay = requests.length;
  await click('invoice-upload-save');
  assert.equal(document.querySelectorAll('[data-testid^="invoice-upload-result"]').length, 2, 'Processing feedback remains in modal');
  assert.equal(location.search, '?batch='+id, 'Upload never leaves receiving');
  await click('invoice-upload-close'); assert.equal(document.activeElement,find('mixed-code'));
  assert.equal(uploadCount, 2);
  assert.equal(requests.length, beforeUploadReplay+1, 'Successful files trigger one replay after the batch');
  assert.equal(requests.at(-1).request_id, missingRequest.request_id, 'Upload retries the rejected scan without losing its operation identity');
  const reassignment = [...document.querySelectorAll('button')].find(button => button.textContent.includes(i18n.t('receiving.mixed.changeInvoice')));
  await act(async () => { reassignment.click(); await tick(); });
  await click('mixed-invoice-choice');
  assert.equal(scans.at(-1).invoice_no, 'INV-2', 'A staged scan can be assigned to another eligible invoice');
  const beforeCancel = scans.filter(scan => scan.status === 'active').length;
  await click('mixed-cancel-scan');
  assert.equal(scans.filter(scan => scan.status === 'active').length, beforeCancel - 1, 'Cancel removes only the staged piece');
  assert.equal(posted, 0);
  await scan('MANUAL'); await click('mixed-manual-open');
  assert(find('mixed-manual-form')); assert(find('mixed-manual-confirm').disabled,'Manual reason is required');
  await act(async()=>{const input=find('mixed-manual-reason');Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype,'value').set.call(input,'Delivery without invoice');input.dispatchEvent(new dom.window.Event('input',{bubbles:true}));await tick();});
  const beforeStorageFailure=requests.length, originalStorage=dom.window.Storage.prototype.setItem;
  dom.window.Storage.prototype.setItem=function(key,value){if(key.startsWith('receiving-batch-command:'))return;return originalStorage.call(this,key,value);};
  await click('mixed-manual-confirm'); assert.equal(requests.length,beforeStorageFailure,'Unavailable recovery storage prevents a new manual mutation');
  dom.window.Storage.prototype.setItem=originalStorage;
  loseManual=true; await click('mixed-manual-confirm'); const manualRequest=requests.at(-1); const manualCount=scans.length;
  assert.equal(manualRequest.unit_cost,null,'Unknown cost remains null'); assert.equal(manualRequest.confirmed_without_invoice,true);
  assert(find('mixed-retry-operation')); await mount('/receiving?batch='+id); await click('mixed-retry-operation');
  assert.deepEqual(requests.at(-1),manualRequest); assert.equal(scans.length,manualCount,'Manual retry cannot duplicate the physical item');
  assert.equal(posted,0,'Manual staging does not post stock');
  await click('mixed-preview');
  assert(document.body.textContent.includes(i18n.t('receiving.mixed.withoutInvoice')));
  assert(find('mixed-summary')); assert.equal(posted, 0); assert(find('mixed-confirm'));
  assert(document.body.textContent.includes('BIKETREK')); assert(document.body.textContent.includes('xTrek'));
  assert(document.body.textContent.includes(i18n.t('receiving.mixed.shopStatus.unlinked')));
  loseFinalize = true; await click('mixed-confirm'); assert.equal(posted, 1); assert(find('mixed-retry-operation'));
  const originalFinalize = requests.at(-1);
  await mount('/receiving?batch=' + id); assert(find('mixed-retry-operation')); await click('mixed-retry-operation');
  assert.equal(posted, 1); assert.deepEqual(requests.at(-1), originalFinalize, 'Finalization recovery preserves exact request');
  assert.equal(find('mixed-atomer').getAttribute('href'), '/sync/atomer?receiving_batch=' + id);
  assert(!find('mixed-confirm')); assert(document.body.textContent.includes(i18n.t('receiving.mixed.shopStatus.queued')));
  status = 'open'; blocked = true; await mount('/receiving?batch=' + id); await click('mixed-preview');
  assert(find('mixed-confirm').disabled, 'Missing cards block final posting');
  await click('mixed-prepare-card');
  assert(find('prepared-product')); assert(location.search.includes('receiving_batch=' + id), 'Preparation retains return-to-batch context');

  blocked=false; scans=[]; openDrafts=[{id,total_qty:'0',scan_count:0,warehouse_code:'main',created_at:'2026-11-16T12:00:00Z'}];
  await mount(); loseArchive=true; await click('mixed-remove-batch'); const archiveRequest=requests.at(-1);
  assert(find('mixed-retry-operation')); await mount(); await click('mixed-retry-operation');
  assert.deepEqual(requests.at(-1),archiveRequest); assert.equal(archiveCount,1); assert(!find('mixed-remove-batch'));
  await click('mixed-history'); assert(find('mixed-history-list')); assert.equal(location.pathname,'/receiving','Viewing receipt history retains receiving context');
  archived=true; await mount('/receiving?batch='+id); assert(!find('mixed-code')); assert(!find('mixed-confirm')); assert(!find('mixed-atomer'));
  assert(document.body.textContent.includes(i18n.t('receiving.mixed.archivedReceipt')),'An archived draft is not called a completed physical receipt'); archived=false;

  // The camera reports a usable fallback without requesting an insecure media stream.
  await act(async () => { root.render(React.createElement(BarcodeCamera, { onScan: () => { throw new Error('No scan expected'); } })); await tick(); });
  await click('receiving-camera-toggle');
  assert(document.body.textContent.includes(i18n.t('receiving.camera.secureRequired')));
  const Module = require('node:module'); const realLoad = Module._load;
  let decoded, cameraScans = [], stopped = 0;
  Module._load = function (request, parent, isMain) {
    if (request === '@zxing/library') return { BarcodeFormat: { EAN_13: 13, EAN_8: 8, UPC_A: 1, UPC_E: 2, CODE_128: 128 }, DecodeHintType: { POSSIBLE_FORMATS: 1 } };
    if (request === '@zxing/browser') return { BrowserMultiFormatReader: class {
      async decodeFromConstraints(constraints, video, callback) {
        assert.equal(constraints.video.facingMode.ideal, 'environment'); decoded = callback;
        return { stop: () => { stopped++; }, switchTorch: async () => {} };
      }
    } };
    return realLoad.call(this, request, parent, isMain);
  };
  Object.defineProperty(window, 'isSecureContext', { value: true, configurable: true });
  Object.defineProperty(navigator, 'mediaDevices', { value: { getUserMedia: async () => ({}) }, configurable: true });
  await act(async () => { root.render(React.createElement(BarcodeCamera, { onScan: code => cameraScans.push(code) })); await tick(); });
  await click('receiving-camera-toggle');
  await act(async () => { for (let n = 0; n < 10; n++) decoded({ getText: () => '5901234123457' }); await tick(); });
  assert.deepEqual(cameraScans, ['5901234123457'], 'Repeated camera frames count one physical piece');
  await click('receiving-camera-next');
  await act(async () => { decoded({ getText: () => '5901234123457' }); await tick(); });
  assert.equal(cameraScans.length, 2, 'Explicit next piece allows the same EAN again');
  await click('receiving-camera-toggle'); assert.equal(stopped, 1, 'Stopping camera releases its controls');
  Module._load = realLoad;
  await act(async () => { root.unmount(); });
  console.log('Mixed receiving UI: suppliers, scan UUID/reload, invoice choice, upload retry, review, exact finalization recovery, missing cards, Atomer link and camera fallback passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
