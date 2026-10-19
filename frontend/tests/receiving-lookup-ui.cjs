/* Real receiving page, synthetic local API. No supplier or shop connections. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'https://hub.example.test/receiving/test-invoice' });
global.window = dom.window; global.document = dom.window.document;
Object.defineProperty(global, 'navigator', { value: dom.window.navigator, configurable: true });
global.HTMLElement = dom.window.HTMLElement; global.sessionStorage = dom.window.sessionStorage;
global.IS_REACT_ACT_ENVIRONMENT = true;
for (const ext of ['.ts', '.tsx']) require.extensions[ext] = (module, filename) => module._compile(ts.transpileModule(
  fs.readFileSync(filename, 'utf8').replace(/import\.meta\.env/g, '{}'),
  { compilerOptions: { jsx: ts.JsxEmit.React, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, esModuleInterop: true } }).outputText, filename);
require.extensions['.css'] = () => {};
const React = require('react'); const { act } = React; const { createRoot } = require('react-dom/client');
const { MemoryRouter, Routes, Route, useNavigate, useLocation } = require('react-router-dom');
const i18n = require('../src/i18n/index.ts').default;
const { ReceivingSessionPage } = require('../src/pages/ReceivingSessionPage.tsx');
const { ReceivingPage } = require('../src/pages/ReceivingPage.tsx');
const root = createRoot(document.getElementById('root'));
const tick = () => new Promise(resolve => setTimeout(resolve, 8));
const find = id => document.querySelector(`[data-testid="${id}"]`);
const button = text => [...document.querySelectorAll('button')].find(el => el.textContent.includes(text));
const response = (value, status = 200) => ({ ok: status < 400, status, statusText: status === 200 ? 'OK' : 'Conflict',
  headers: { get: () => 'application/json' }, text: async () => JSON.stringify(value), json: async () => value });

let candidateCount = 2, state = 'in_progress', received = 0, applied = new Map(), scans = [], creates = 0, unreadable = 0;
const line = () => ({ id:41, scm:'DEMO', product_code:'DEMO', ean:'5901234123457', title:'Synthetic item', ordered_qty:3, received_qty:received, status:received?'partial':'pending' });
const summary = () => ({ matched:0,partial:received?1:0,pending:received?0:1,overage:0,unexpected:0 });
const candidate = n => ({ supplier:'fixture', supplier_name:'Synthetic supplier', invoice_no:'invoice-'+n, session_id:'session-'+n, status:state, invoice_date:null,
  lines:[{id:41,title:'Synthetic item',sku:'DEMO',line_number:1,ordered_qty:'3',received_qty:String(received)}] });
global.fetch = async (path, options={}) => {
  if (path === '/api/suppliers') return response([{code:'fixture',name:'Synthetic supplier'}]);
  if (path.includes('/invoices/index')) return response({supplier:'fixture',count:0,invoices:[]});
  if (path.includes('/receiving/lookup?')) return response({ code:'5901234123457',candidates:Array.from({length:candidateCount},(_,n)=>candidate(n+1)),unreadable_invoices:unreadable });
  if (path.endsWith('/summary')) return response({invoice_no:'invoice-1',status:state,lines:[line()],summary:summary()});
  if (path.endsWith('/resume')) {state='in_progress';return response({lines:[line()]});}
  if (/\/products(?:\/reconcile)?$/.test(path)) return response({session_id:'session-1',items:[],counts:{},feed_verified:true});
  if (path.endsWith('/note')) return response({note:''});
  if (path.endsWith('/scan')) {
    const body=JSON.parse(options.body);scans.push(body);
    if(!applied.has(body.request_id)){received++;applied.set(body.request_id,true);}
    return response({request_id:body.request_id,replayed:scans.filter(s=>s.request_id===body.request_id).length>1,status:'partial',line:line(),summary:summary()});
  }
  if (path.endsWith('/sessions') && options.method==='POST') creates++;
  throw new Error('Unexpected request: '+path);
};
let navigate, location;
function Control(){ navigate=useNavigate();location=useLocation();return null; }
function page(entry='/receiving') {return React.createElement(MemoryRouter,{initialEntries:[entry]},React.createElement(React.Fragment,null,
  React.createElement(Control),React.createElement(Routes,null,
    React.createElement(Route,{path:'/receiving',element:React.createElement(ReceivingPage)}),
    React.createElement(Route,{path:'/receiving/:invoiceId',element:React.createElement(ReceivingSessionPage)}))));}
async function mount(entry){await act(async()=>{root.render(null);await tick();});await act(async()=>{root.render(page(entry));await tick();});}
async function scan(){await act(async()=>{
  const input=find('receiving-invoice-scan');
  Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype,'value').set.call(input,'5901234123457');
  input.dispatchEvent(new dom.window.Event('input',{bubbles:true}));await tick();
  input.closest('form').dispatchEvent(new dom.window.Event('submit',{bubbles:true,cancelable:true}));await tick();});}
(async()=>{
  await mount();await scan();
  assert(document.querySelector('[role="dialog"]'),'Multiple invoices require an explicit choice');
  assert.equal(scans.length,0);assert.equal(creates,0);
  await act(async()=>{button('invoice-1').click();await tick();await tick();});
  assert.equal(scans.length,1);assert.equal(received,1,'The landing scan is counted in the selected receipt');
  const entry={pathname:location.pathname,search:location.search,state:location.state};
  await mount(entry);await act(tick);
  assert.equal(scans.length,1,'Remounting the same navigation never repeats an acknowledged entry scan');
  candidateCount=1;state='completed';await mount();await scan();await act(tick);
  assert.equal(scans.length,1,'Completed receipts open without incrementing quantities');
  assert(document.body.textContent.includes(i18n.t('receiving.lookup.closed')));
  assert(find('receiving-line-0').style.backgroundColor,'Matched historical line is highlighted');
  state='in_progress';unreadable=1;await mount();await scan();
  assert(document.querySelector('[role="dialog"]'),'A single result from an incomplete search still requires a choice');
  assert.equal(scans.length,1);
  candidateCount=0;unreadable=0;await mount();await scan();
  assert(document.body.textContent.includes(i18n.t('receiving.lookup.notFound')));
  await act(async()=>{root.unmount();});
  console.log('Receiving lookup UI: explicit invoice choice, one entry scan, remount safety, completed receipt review and incomplete lookup passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
