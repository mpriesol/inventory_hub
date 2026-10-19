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


const requests=[];let fail=true;let done=false;
global.fetch=async(path,options={})=>{
  requests.push({path,options});
  if(!options.method || options.method==='GET')return response({items:done?[]:[{product_id:12,old_code:'TOPEAK-1',new_code:'ZOO-1',name:'Synthetic item'},
    {product_id:13,old_code:'SEN-1',new_code:'SEN-1',name:'Synthetic Sensor item'}]});
  assert.equal(options.method,'POST');assert.match(path,/^\/api\/supplier-availability\/zookee\/legacy-code-batches$/);
  if(fail){fail=false;return response({detail:{code:'code_rename_native_id_changed'}},409);}
  done=true;return response({results:JSON.parse(options.body).rows.map(row=>({product_id:row.product_id,status:'completed'}))});
};
(async()=>{
 await act(async()=>{root.render(React.createElement(ZookeeCodeReconciliation));await tick();});
 assert.equal(requests.length,0,'Opening the supplier settings does not start a rename or a read');
 await act(async()=>{button(i18n.t('suppliers.codeRename.load')).click();await tick();});
 assert.equal(requests.length,1);
 assert(document.body.textContent.includes('TOPEAK-1 → ZOO-1'),'Exact old/new codes are reviewable');
 assert(document.body.textContent.includes('SEN-1') && !document.body.textContent.includes('SEN-1 →'),'Unchanged Sensor codes are shown as repairs');
 await act(async()=>{button(i18n.t('suppliers.codeRename.apply')).click();await tick();});
 assert.equal(requests.length,2,'A known mismatch is not automatically retried');
 assert(document.body.textContent.includes(i18n.t('suppliers.codeRename.notVerified')));
 assert.equal(document.querySelector('code')?.textContent,'code_rename_native_id_changed','The rejected identity is diagnosable');
 const original=JSON.parse(requests[1].options.body);
 await act(async()=>{button(i18n.t('suppliers.codeRename.apply')).click();await tick();});
 assert.deepEqual(JSON.parse(requests[2].options.body),original,'An explicit retry retains its operation UUID');
 assert(document.body.textContent.includes(i18n.t('suppliers.codeRename.results.completed')));
 await act(async()=>{button(i18n.t('suppliers.codeRename.apply')).click();await tick();});
 assert.equal(requests.length,3,'Completed local reconciliation is not resent');
 assert(requests.every(r=>r.path.startsWith('/api/supplier-availability/zookee/legacy-code')),'The tool calls only the scoped Hub endpoints');
 await act(async()=>{root.unmount();});console.log('Zookee code reconciliation UI: explicit review, exact scope, UUID retry and no automatic request passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
