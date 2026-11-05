const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<html><body><div id="root"></div></body></html>', { url: 'https://hub.example.test/migration/atomer-progress' });
global.window = dom.window; global.document = dom.window.document;
Object.defineProperty(global, 'navigator', { value: dom.window.navigator, configurable: true });
global.HTMLElement = dom.window.HTMLElement; global.localStorage = dom.window.localStorage;
global.FormData = dom.window.FormData; global.File = dom.window.File;
global.IS_REACT_ACT_ENVIRONMENT = true;
require.extensions['.css'] = () => {};
for (const ext of ['.ts', '.tsx']) require.extensions[ext] = (module, filename) => {
  const source = fs.readFileSync(filename, 'utf8').replace(/import\.meta\.env/g, '{}');
  module._compile(ts.transpileModule(source, { compilerOptions: { jsx: ts.JsxEmit.React, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, esModuleInterop: true } }).outputText, filename);
};
const i18n = require('../src/i18n/index.ts').default;
const React = require('react'); const { act } = React;
const { createRoot } = require('react-dom/client');
const { MemoryRouter } = require('react-router-dom');
const { AtomerMigrationProgress } = require('../src/pages/AtomerMigrationProgress.tsx');
const shop = 'https://example.test/shop';
const admin = 'https://admin.example.test/product/1';
let filter = new URLSearchParams();
const item = {code:'S-1',name:'Jacket blue',parent:'J',kind:'variant',variant_label:'Blue · M',state:'linked',
  card_state:'exists',hub_id:3,hub_sku:'HUB-3',candidates:[],reasons:[],ean:'',product_id:'1',variant_id:'2',
  target_code:'S-1',shop_url:shop,shop_admin_url:admin,bike_state:'unlinked',bike:null};
item.source_state={product:null,variant:false};
const baseWork={revision:0,status:'open',scope:'default',decision:'undecided',note:'',instruction:'',result:''};
let saved,extraWork={...baseWork};
const target={key:'1/3',family_key:'v:J',name:'Jacket',code:'OLD-L',ean:'',product_id:'1',variant_id:'3',axes:{size:['L']},check:'eligible',warnings:[],card_ids:[],alternatives:[],stock:0,work:extraWork};
const run = {id:2,checked_at:new Date().toISOString(),imported_at:new Date().toISOString(),totals:{items:1,admin_codes:4,admin_only_items:1,
  kinds:{variant:1},states:{linked:1},bike:{unlinked:1}}};
global.fetch = async (path, init) => {
  assert.equal(init.credentials,'same-origin');
  const url=new URL(path,'https://hub.example.test');
  filter = url.searchParams;
  const ok=value=>({ok:true,status:200,json:async()=>value});
  if(init.method==='POST'){
    saved=JSON.parse(init.body);return ok({...baseWork,...saved,revision:1});
  }
  if(url.pathname.endsWith('/targets'))return ok({run_id:2,total:1,items:[target]});
  if(url.pathname.endsWith('/work'))return ok({run_id:2,total:0,items:[]});
  return {ok:true,status:200,json:async()=>({run,total:1,families:1,groups:[{key:'v:J',name:'Jacket',items:[item]}]})};
};
(async()=>{
  await i18n.changeLanguage('sk');
  const root=createRoot(document.getElementById('root'));
  await act(async()=>root.render(React.createElement(MemoryRouter,null,React.createElement(AtomerMigrationProgress))));
  const settle=()=>act(async()=>new Promise(resolve=>setTimeout(resolve,230)));
  await settle();
  assert.ok(document.body.textContent.includes('CSV vybraných položiek') && document.body.textContent.includes('Predajné položky iba v CSV 1'));
  const csvInput=document.querySelector('input[accept*=".csv"]');
  const chooseCsv=async(name)=>act(async()=>{
    Object.defineProperty(csvInput,'files',{configurable:true,value:[new File([''],name)]});
    csvInput.dispatchEvent(new window.Event('change',{bubbles:true}));
  });
  await chooseCsv('whole.csv');
  const scopeCheck=document.querySelector('.progress-upload input[type="checkbox"]');
  assert.equal(scopeCheck.checked,false);
  await act(async()=>scopeCheck.click());
  assert.equal(scopeCheck.checked,true);
  await chooseCsv('selection.csv');
  assert.equal(scopeCheck.checked,false);
  const toggle=document.querySelector('button[aria-expanded]');
  assert.equal(toggle.getAttribute('aria-expanded'),'false');
  assert.ok(!document.querySelector('.progress-child'));
  await act(async()=>toggle.click());
  const child=document.querySelector('.progress-child');
  assert.ok(child.textContent.includes('Blue · M') && child.textContent.includes('Karta bez väzby'));
  for(const url of [shop,admin,'/products/HUB-3']) assert.ok(child.querySelector('a[href="'+url+'"]'));
  const select=[...document.querySelectorAll('select')].find(x=>x.parentNode.textContent.startsWith('BIKETREK'));
  await act(async()=>{ select.value='unlinked'; select.dispatchEvent(new window.Event('change',{bubbles:true})); });
  await settle();
  assert.equal(filter.get('bike'),'unlinked');
  assert.equal(document.querySelector('button[aria-expanded]').getAttribute('aria-expanded'),'false');
  const button=(label,el=document)=>[...el.querySelectorAll('button')].find(b=>b.textContent===label);
  assert.ok([...document.querySelectorAll('th')].some(h=>h.textContent==='Odstrániť?'));
  await act(async()=>document.querySelector('button[aria-expanded]').click());
  const leaf=document.querySelector('.progress-child');
  assert.ok(leaf.textContent.includes('Neaktívny')&&leaf.textContent.includes('Neuvedené'));
  await act(async()=>button('Riešiť',leaf).click());
  let dialog=document.querySelector('dialog');
  await act(async()=>{let sel=dialog.querySelector('select');sel.value='xtrek';sel.dispatchEvent(new window.Event('change',{bubbles:true}));});
  await act(async()=>button('Označiť ako hotové',dialog).click());await settle();
  assert.equal(saved.scope,'xtrek');assert.equal(saved.command,'done');assert.equal(saved.key,'S-1');
  const removals=[...document.querySelectorAll('.review-tabs button')].find(b=>b.textContent.startsWith('Na rozhodnutie'));
  await act(async()=>removals.click());await settle();
  const choice=document.querySelector('select[aria-label="Odstrániť? OLD-L"]');assert.ok(choice);
  await act(async()=>{choice.value='yes';choice.dispatchEvent(new window.Event('change',{bubbles:true}));});
  dialog=document.querySelector('dialog');assert.equal(dialog.querySelector('select').value,'yes');
  await act(async()=>button('Uložiť',dialog).click());await settle();
  assert.equal(saved.kind,'extra');assert.equal(saved.key,'1/3');assert.equal(saved.decision,'yes');
  await act(async()=>button('Pokyny pre AI').click());await settle();
  assert.ok(document.body.textContent.includes('V tomto stave nie sú žiadne pokyny.'));
  await act(async()=>root.unmount());
  console.log('Grouped migration rows, card and shop links, filters passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
