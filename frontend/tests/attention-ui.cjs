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
const { AttentionCenter } = require('../src/components/AttentionCenter.tsx');
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
let fail = false, conflict = false, held = false, finish;
let rules = [
  {kind:'supplier_availability.partial',scope:'*',revision:0,enabled:true,min_occurrences:1},
  {kind:'supplier_availability.failed',scope:'*',revision:0,enabled:true,min_occurrences:1},
];
let item = { id:1, kind:'supplier_availability.partial', scope:'husky', source:'supplier_availability', severity:'warning',
  revision:1, active:true, ignored_at:null, first_seen:'2026-11-16T10:00:00Z', last_seen:'2026-11-16T11:00:00Z',
  occurrences:1, episode:1, link:'/settings/availability?supplier=husky', settings_link:'/settings/availability?supplier=husky',
  log_link:'/settings/logs?attention_id=1', details:{run_id:200,total:3105,accepted:3000,rejected:0,unknown:105,
    warnings:['supplier_availability_incomplete_feed'],coverage:[{target:'husky_products_feed',rows:3105,previous_rows:3129,required_rows:3129,min_coverage_percent:100}]}};
const response = data => ({ok:true,status:200,json:async()=>data});
global.fetch = async (path, options) => {
  const url = new URL(path, dom.window.location.origin), body = options.body ? JSON.parse(options.body) : null;
  calls.push({path,...options,body});
  if (url.pathname === '/api/attention/rules') return response({items:rules});
  if (options.method === 'PUT') {
    const scope = url.searchParams.get('scope'); assert.equal(body.expected_revision, rules.find(r=>r.scope===scope)?.revision || 0);
    rules=rules.filter(r=>!(r.kind===item.kind&&r.scope===scope));
    const saved={kind:item.kind,scope,revision:body.expected_revision+1,enabled:body.enabled,min_occurrences:body.min_occurrences};
    rules.push(saved); return response(saved);
  }
  if (url.pathname.endsWith('/action')) {
    if (conflict) return {ok:false,status:409,json:async()=>({detail:{code:'attention_changed'}})};
    assert.equal(body.expected_revision,item.revision); item={...item,revision:item.revision+1,ignored_at:body.action==='ignore'?'2026-11-16T11:00:00Z':null};
    return response({id:item.id,revision:item.revision});
  }
  if (url.pathname === '/api/attention') {
    if (fail) return {ok:false,status:503,json:async()=>({detail:{code:'attention_load_failed'}})};
    const selected=rules.find(r=>r.scope==='husky')||rules[0], state=url.searchParams.get('state');
    const muted=!selected.enabled||selected.min_occurrences>item.occurrences;
    const visible=state==='ignored'?!!item.ignored_at:state==='muted'?muted:state==='resolved'?false:!muted&&!item.ignored_at;
    const data={items:visible?[item]:[],total:visible?1:0,limit:20,offset:0,sources:['supplier_availability']};
    if (held) return new Promise(resolve=>{finish=()=>resolve(response(data));});
    return response(data);
  }
  throw new Error('Unexpected API '+path);
};
(async()=>{
  await i18n.changeLanguage('sk');
  await act(async()=>{root.render(React.createElement(MemoryRouter,null,React.createElement(AttentionCenter,{compact:true}))); await tick();});
  assert.equal(calls.length,0,'Locked dashboard does not read private notifications');
  await act(async()=>{unlockHub('attention-fixture-token'); await tick();});
  assert(document.body.textContent.replace(/\s/g,' ').includes('3 000'));
  assert(document.body.textContent.replace(/\s/g,' ').includes('3 105'));
  assert(document.querySelector('a[href="/settings/logs?attention_id=1"]'));
  await click('attention-configure-1');
  assert.equal(required('attention-rule-scope').value,'husky');
  await input('attention-rule-minimum','3'); await click('attention-rule-save');
  const saved=calls.find(c=>c.method==='PUT');assert(saved.path.endsWith('scope=husky'));
  assert.deepEqual(saved.body,{expected_revision:0,enabled:true,min_occurrences:3});
  assert(!element('attention-item-1'));
  await act(async()=>{root.render(React.createElement(MemoryRouter,null,React.createElement(AttentionCenter)));await tick();});
  await input('attention-state','muted'); assert(element('attention-item-1'));
  await click('attention-configure-1');await input('attention-rule-minimum','1');await click('attention-rule-save');
  await input('attention-state','active');assert(element('attention-item-1'));
  await click('attention-ignore-1');assert(!element('attention-item-1'));
  await input('attention-state','ignored');assert(element('attention-item-1'));
  await click('attention-ignore-1');assert(!element('attention-item-1'));
  await input('attention-state','active');conflict=true;await click('attention-ignore-1');
  assert(document.querySelector('[role="alert"]').textContent.includes('medzičasom'));
  assert(element('attention-item-1'),'Conflict does not pretend the incident was hidden');
  conflict=false;await click('attention-refresh');
  await click('attention-configure-1');
  await act(async()=>{required('attention-rule-enabled').click();await tick();});await click('attention-rule-save');
  assert(!element('attention-item-1'));
  await click('attention-rules');assert(document.body.textContent.includes('Upozornenie vypnuté'));
  fail=true;await click('attention-refresh');assert(document.querySelector('[role="alert"]'));
  fail=false;held=true;await click('attention-refresh');const delayed=finish;
  const pending=calls.findLast(c=>c.path.startsWith('/api/attention?'));
  await act(async()=>{unlockHub('');await tick();});assert(pending.signal.aborted);
  await act(async()=>{delayed();await tick();});assert(!element('attention-item-1'));
  assert(!calls.some(c=>c.path.includes('/supplier-availability/')&&c.method!=='GET'),'Notification controls never trigger an import');
  await act(async()=>root.unmount());
  console.log('attention-ui: summaries, source/log links, parameters, mute, ignore/restore, CAS conflict and logout passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
