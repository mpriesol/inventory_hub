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
const { ZookeeCodeReconciliation } = require('../src/components/ZookeeCodeReconciliation.tsx');
const root = createRoot(document.getElementById('root'));
const tick = () => new Promise(resolve => setTimeout(resolve, 8));
const find = id => document.querySelector(`[data-testid="${id}"]`);
const button = text => [...document.querySelectorAll('button')].find(el => el.textContent.includes(text));
const response = (value, status = 200) => ({ ok: status < 400, status, statusText: status === 200 ? 'OK' : 'Conflict',
  headers: { get: () => 'application/json' }, text: async () => JSON.stringify(value), json: async () => value });


const requests=[];
const id='c18d2154-ad0f-4d4a-9bfd-75620bd90001', missing='c18d2154-ad0f-4d4a-9bfd-75620bd90002';
const saved={request_id:id,product_id:12,old_code:'TOPEAK-1',new_code:'ZOO-1',status:'completed',completed_at:'2026-10-01T10:00:00Z'};
let fail=true;
global.fetch=async(path,options={})=>{
  requests.push({path,options});
  assert.equal(options.method,'GET','Retired reconciliation only reads stored results');
  if(path.endsWith('/legacy-codes')) {
    if(fail){fail=false;throw new TypeError('History temporarily unavailable');}
    return response({retired:true,reason:'code_rename_retired',items:[],results:[saved]});
  }
  if(path.endsWith('/legacy-code-results/'+id)) return response(saved);
  if(path.endsWith('/legacy-code-results/'+missing)) return response({detail:{code:'code_rename_result_missing'}},404);
  throw Error('Unexpected request '+path);
};
async function click(id){await act(async()=>{find(id).click();await tick();});}
async function input(value){await act(async()=>{
 const node=find('zookee-history-id');Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype,'value').set.call(node,value);
 node.dispatchEvent(new dom.window.Event('input',{bubbles:true}));await tick();
});}
(async()=>{
 await act(async()=>{root.render(React.createElement(ZookeeCodeReconciliation));await tick();});
 assert.equal(requests.length,0,'Opening settings does not read or start a conversion');
 assert(document.body.textContent.includes(i18n.t('suppliers.codeRename.help')));
 assert(find('zookee-history-recover').disabled);
 await click('zookee-history-load');
 assert.equal(requests.length,1,'Failed reads are never automatically retried');
 assert(document.body.textContent.includes(i18n.t('suppliers.codeRename.failed')));
 await click('zookee-history-load');
 assert(document.body.textContent.includes('TOPEAK-1 → ZOO-1'),'Saved old/new codes remain reviewable');
 assert(document.body.textContent.includes(id));
 await input('not-a-uuid');assert(find('zookee-history-recover').disabled);
 await input(id);await click('zookee-history-recover');
 assert.equal(requests.at(-1).path,'/api/supplier-availability/zookee/legacy-code-results/'+id);
 assert.equal(document.querySelectorAll('tbody tr').length,1,'Recovering the saved UUID does not duplicate history');
 await input(missing);await click('zookee-history-recover');
 assert(document.body.textContent.includes(i18n.t('suppliers.codeRename.missing')));
 assert(document.body.textContent.includes('TOPEAK-1 → ZOO-1'),'A missing UUID leaves loaded history intact');
 assert(requests.every(r=>r.options.method==='GET' && r.path.startsWith('/api/supplier-availability/zookee/legacy-code')));
 assert.equal(document.querySelectorAll('button').length,2,'There is no action to start or repeat historical renaming');
 await act(async()=>{root.unmount();});console.log('Zookee retired conversion UI: saved history and UUID recovery are read-only; no automatic request passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
