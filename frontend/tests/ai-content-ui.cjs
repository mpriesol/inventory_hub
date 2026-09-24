/* Behavioral UI tests against synthetic APIs only. Never calls OpenAI/Upgates. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'https://hub.example.test/ai-content' });
global.window = dom.window; global.document = dom.window.document;
Object.defineProperty(global, 'navigator', { value: dom.window.navigator, configurable: true });
global.HTMLElement = dom.window.HTMLElement; global.IS_REACT_ACT_ENVIRONMENT = true;
for (const ext of ['.ts', '.tsx']) require.extensions[ext] = (module, filename) => {
  module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8').replace(/import\.meta\.env/g, '{}'), {
    compilerOptions: { jsx: ts.JsxEmit.React, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, esModuleInterop: true },
  }).outputText, filename);
};
require.extensions['.css'] = () => {};
require('../src/i18n/index.ts');
const React = require('react'); const { act } = React;
const { createRoot } = require('react-dom/client');
const { MemoryRouter } = require('react-router-dom');
const { AiContentPage } = require('../src/pages/AiContentPage.tsx');
const { AiRuleEditor } = require('../src/components/product/AiRuleEditor.tsx');
const { CategoryTree } = require('../src/components/product/CategoryTree.tsx');
const { AiJobDetail } = require('../src/components/product/AiJobDetail.tsx');
const { unlockAi } = require('../src/api/aiContent.ts');
const root = createRoot(document.getElementById('root'));
const policy = { review_required: true, active_after_import: false, show_cost_estimate: true, confirm_import: true };
const scope = { shop: '', supplier: '', brand: '', product: '', category: '' };
const book = { rules: [{ id: 'common', name: 'Common', scope, policy, instructions: 'Verified rules', official_domains: [], enabled: true }],
  categories: [{ id: 'general', name: 'General', instructions: '', parameters: [], policy: {}, shop_categories: {}, automatic_import_ready: true }] };
const rules = { published_id: 1, book, used_usd: '0', versions: [{ id: 1, note: 'Initial' }] };
const options = { language: 'sk', currency: 'EUR', pricelist: 'Predvolené', category_code: null, pricing: 'configured', include_images: true, include_description: true, include_parameters: true };
const families = [{ code: 'NF-G-TEST', name: 'Test bunda', image: 'https://images.example.test/1.jpg', product_ids: [1, 2], products: [1, 2].map(id => ({ id, shop_code: 'NF-' + id, variant_attributes: [{ name: 'Veľkosť', value: id === 1 ? 'S' : 'M' }] })) },
  { code: 'NF-OTHER', name: 'Test nohavice', image: null, product_ids: [3], products: [{ id: 3, shop_code: 'NF-3', variant_attributes: [] }] }];
const content = { title: 'Test bunda', short_description: 'Short', long_description: '<p>Long</p>', seo_title: 'SEO', meta_description: 'Meta', h1_descriptor: 'Bunda', future_name: 'TEST', h1_descr_suffix: '', parameters: [], evidence: [], warnings: [], missing_facts: [] };
const calls = []; let jobs = []; let reviewed; let historicalBook = book;
global.fetch = async (path, init = {}) => {
  const body = init.body ? JSON.parse(init.body) : undefined; calls.push({ path, body, headers: init.headers });
  let data;
  if (path.endsWith('/status')) data = { enabled: true, key_configured: true, access_configured: true, model: 'fixture', monthly_limit_usd: '20', job_limit_usd: '2' };
  else if (path.endsWith('/rules')) data = body ? { id: 2 } : rules;
  else if (path.endsWith('/rules/1')) data = { id: 1, book: historicalBook };
  else if (path.endsWith('/existing-products/options')) data = { shops: [{code:'biketrek',name:'BikeTrek'}] };
  else if (path.endsWith('/existing-products')) { const job = { ...jobs[0], id:'existing-job', status:'estimate', update_only:true, source_kind:'shop', product_ids:[], code:body.code }; jobs.push(job); data = {job}; }
  else if (path.endsWith('/selection')) data = families;
  else if (path.includes('/catalog?')) data = { shops: [{ code: 'biketrek', name: 'BikeTrek', ready: true }, { code: 'xtrek', name: 'xTrek', ready: true }] };
  else if (path.endsWith('/import/options')) data = { languages: [{ code: 'sk', currency: 'EUR' }], pricelists: [{ name: 'Predvolené', default: true }], categories: [{ code: 'K1', names: { sk: 'Category' } }] };
  else if (path.endsWith('/batches')) { jobs = body.targets.map((target, i) => ({ id: 'job' + i, batch_id: 'batch', kind: 'product', status: 'estimate', revision: 1, name: 'Test bunda', code: 'NF-G-TEST', shop: target.shop, policy, origins: {}, checks: {}, use_ai: true, estimate_usd: '0.3', actual_usd: null, product_ids: [1, 2] })); data = { jobs }; }
  else if (path.includes('/jobs?') || path.endsWith('/jobs')) data = jobs.filter(job => !!job.archived === path.includes('archived=true'));
  else if (path.endsWith('/action')) { const job = jobs.find(j => path.includes('/jobs/' + j.id + '/')); if (body.action === 'archive' || body.action === 'restore') job.archived = body.action === 'archive'; data = job; }
  else if (path.endsWith('/review')) { reviewed = body; data = { ...jobs[0], output: body.content, revision: 2, status: body.approve ? 'preparing_import' : 'review' }; }
  else if (path.includes('/jobs/')) data = jobs.find(j => path.endsWith(j.id)) || jobs[0];
  else throw new Error('Unexpected synthetic endpoint ' + path);
  return { ok: true, json: async () => data };
};
const button = text => [...document.querySelectorAll('button')].find(b => b.textContent === text);
const parameterRows = () => [...document.querySelectorAll('select[aria-label^="Názov parametra "]')].map(select => select.closest('tr'));
const parameterRow = (name, productId = null) => parameterRows().find(row => row.querySelector('select').value === name && row.querySelectorAll('select')[1].value === (productId === null ? 'parent' : String(productId)));
const tick = () => new Promise(resolve => setTimeout(resolve, 5));
async function click(element) { assert(element, 'Element exists'); await act(async () => { element.click(); await tick(); }); }
async function input(element, value) { await act(async () => { Object.getOwnPropertyDescriptor(element.tagName === 'TEXTAREA' ? dom.window.HTMLTextAreaElement.prototype : dom.window.HTMLInputElement.prototype, 'value').set.call(element, value); element.dispatchEvent(new dom.window.Event('input', { bubbles: true })); await tick(); }); }

(async () => {
  unlockAi('');
  await act(async () => { root.render(React.createElement(MemoryRouter, {key:'locked'}, React.createElement(AiContentPage))); await tick(); });
  assert(button('Odomknúť správu AI').disabled, 'Unlock stays disabled until the access token is filled');
  await input(document.querySelector('input[type="password"]'), 'synthetic-ui-fixture-token');
  assert(!button('Odomknúť správu AI').disabled, 'Unlock becomes available when the token is entered');
  unlockAi('synthetic-ui-fixture-token');
  await act(async () => { root.render(React.createElement(MemoryRouter, { initialEntries: [{ pathname: '/ai-content', state: { selection: { supplier: 'northfinder', feed_key: 'products', product_ids: [1, 2, 3], run_id: 1, shop: 'biketrek', options } } }] }, React.createElement(AiContentPage))); await tick(); });
  await act(tick);
  assert(document.body.textContent.includes('Test bunda'));
  assert(document.querySelector('img').src.includes('/1.jpg'));
  const aiChecks = [...document.querySelectorAll('label')].filter(l => l.textContent.includes('Vylepšiť obsah pomocou AI')).map(l => l.querySelector('input'));
  assert.equal(aiChecks.length, 2); await click(aiChecks[1]);
  const xtrek = [...document.querySelectorAll('label')].find(l => l.textContent === 'xTrek').querySelector('input'); await click(xtrek);
  await click(button('Pripraviť / spustiť podľa nastavení'));
  const batch = calls.find(c => c.path.endsWith('/batches')).body;
  assert.deepEqual(batch.ai_product_ids, [1, 2], 'Only marked family goes through AI');
  assert.deepEqual(batch.product_ids, [1, 2, 3], 'Original-content selection retained');
  assert.deepEqual(batch.targets.map(t => t.shop), ['biketrek', 'xtrek']);
  assert(!calls.some(c => c.path.endsWith('/action')), 'Cost pause causes no paid processing');
  assert(calls.filter(c => c.path.includes('/ai-content/') && !c.path.endsWith('/status')).every(c => c.headers.Authorization === 'Bearer synthetic-ui-fixture-token'));
  jobs[0].status = 'failed'; jobs[1].status = 'generating';
  jobs.push({...jobs[1],id:'pending-job',status:'exists',update_state:'uncertain'});
  await click(button('Obnoviť'));
  const jobChecks = [...document.querySelectorAll('input[aria-label^="Označiť úlohu"]')];
  for (const checkbox of jobChecks) if (!checkbox.checked) await click(checkbox);
  await click(button('Odstrániť označené zo zoznamu'));
  assert.deepEqual(calls.filter(c => c.path.endsWith('/action')).map(c => [c.body.action, c.path]), [['archive', '/api/ai-content/jobs/job0/action']], 'Only selected inactive work is archived; processing and unconfirmed updates remain');
  assert.equal(document.querySelectorAll('input[aria-label^="Označiť úlohu"]').length, 2);
  await click([...document.querySelectorAll('label')].find(l => l.textContent === 'Archív').querySelector('input'));
  const archivedCheck = document.querySelector('input[aria-label^="Označiť úlohu"]');
  assert(archivedCheck && !archivedCheck.checked, 'Archive view resets selection');
  await click(archivedCheck); await click(button('Obnoviť označené'));
  assert.equal(calls.findLast(c => c.path.endsWith('/action')).body.action, 'restore');
  await click(button('Existujúce produkty')); await act(tick);
  const exactCode = [...document.querySelectorAll('label')].find(l => l.textContent === 'Presný kód produktu v e-shope').querySelector('input');
  await input(exactCode, 'PL-EXISTING');
  const paidBefore = calls.filter(c => c.path.endsWith('/action') && c.body.action === 'start').length;
  await click(button('Načítať produkt a pripraviť odhad'));
  const existingRequest = calls.findLast(c => c.path.endsWith('/existing-products')).body;
  assert.equal(existingRequest.code, 'PL-EXISTING'); assert.equal(existingRequest.shop, 'biketrek');
  assert.equal(calls.filter(c => c.path.endsWith('/action') && c.body.action === 'start').length, paidBefore, 'Existing-product entry captures source without starting paid generation');
  assert(document.body.textContent.includes('Táto príprava upravuje existujúci produkt'));
  await act(async () => { root.render(React.createElement(AiJobDetail, { job: { ...jobs[0], status: 'review', output: content, facts: [], events: [] }, onChange: () => {} })); await tick(); });
  const title = [...document.querySelectorAll('label')].find(l => l.textContent.startsWith('Názov')).querySelector('textarea');
  await input(title, 'Upravený názov');
  assert.equal(title.value, 'Upravený názov', 'Content fields accept edits');
  await click(button('Uložiť koncept obsahu'));
  assert.equal(reviewed.content.title, 'Upravený názov'); assert.equal(reviewed.approve, false);
  await click(button('Schváliť obsah a pripraviť import')); assert.equal(reviewed.approve, true);
  const missingParameterJob = { ...jobs[0], status:'blocked', output:content, revision:3, facts:[],
    parameter_registry:[{name:'Materiál',required:true,scope:'parent',values:['Hliník','Termoplast'],unit:'',instructions:''}],
    checks:{errors:['ai_required_parameter:Materiál']}, events:[] };
  await act(async () => { root.render(React.createElement(AiJobDetail,{key:'missing-parameter',job:missingParameterJob,onChange:() => {}})); await tick(); });
  assert(document.body.textContent.includes('Chýbajúce povinné parametre: Materiál'));
  await click([...parameterRow('Materiál').querySelectorAll('label')].find(label => label.textContent === 'Termoplast').querySelector('input'));
  await click(button('Uložiť koncept obsahu'));
  assert.deepEqual(reviewed.content.parameters,[{name:'Materiál',product_id:null,values:['Termoplast']}], 'A missing required parameter can be corrected through normal controls and saved as structured data');

  const pumpRequired = ['Držiak na bicykel v balení', 'Hadica', 'Manometer'];
  const pumpMaterial = {name:'Materiál',product_id:null,values:['Hliník']};
  const pumpMissingFacts = ['Chýba overený maximálny tlak.'];
  const pumpJob = {...missingParameterJob, status:'review', checks:{}, output:{...content,parameters:[pumpMaterial],missing_facts:pumpMissingFacts},
    parameter_registry:[{name:'Materiál',required:true,scope:'parent',values:['Hliník','Termoplast']},
      ...pumpRequired.map(name => ({name,required:true,scope:'parent',values:['Áno','Nie']})),
      {name:'Farba',required:false,scope:'parent',values:[]}]};
  await act(async () => { root.render(React.createElement(AiJobDetail,{key:'pump-parameters',job:pumpJob,onChange:() => {}})); await tick(); });
  assert.equal(parameterRows().length,4, 'Only existing values and missing required slots appear automatically; absent optional parameters stay absent');
  assert(parameterRow('Hadica').closest('details').open, 'Missing required values open the editor even without backend checks');
  for (const name of pumpRequired) {
    const row = parameterRow(name);
    assert(row.classList.contains('ai-parameter-missing') && row.textContent.includes('Povinný parameter · chýba hodnota'), 'Each required blank row has visible translated missing guidance');
    assert.equal(row.querySelector('fieldset').getAttribute('aria-invalid'),'true', 'Missing choice groups expose their invalid state');
    assert([...row.querySelectorAll('input[type="checkbox"]')].every(checkbox => !checkbox.checked), 'Required yes/no parameters start unanswered');
    assert([...row.querySelectorAll('select')].every(select => select.disabled), 'Synthetic rows keep their required name and scope');
    assert(!row.querySelector('button'), 'An unanswered required placeholder cannot be removed');
  }
  assert(!parameterRow('Materiál').classList.contains('ai-parameter-missing'));
  assert(parameterRow('Materiál').querySelector('input').checked, 'Existing structured values remain selected');
  await click([...parameterRow('Hadica').querySelectorAll('label')].find(label => label.textContent === 'Áno').querySelector('input'));
  assert(!parameterRow('Hadica').classList.contains('ai-parameter-missing'), 'An answered required row clears its missing state');
  await click(button('Uložiť koncept obsahu'));
  assert.deepEqual(reviewed.content.parameters,[pumpMaterial,{name:'Hadica',product_id:null,values:['Áno']}], 'Saving one answer preserves existing values and excludes untouched required placeholders');
  assert.deepEqual(reviewed.content.missing_facts,pumpMissingFacts, 'Answering a parameter does not silently clear unrelated missing facts');

  const blankVariant = {name:'Dĺžka hadice',product_id:11,values:[' ']};
  const filledVariant = {name:'Dĺžka hadice',product_id:12,values:['35']};
  const variantJob = {...missingParameterJob, status:'review', checks:{}, output:{...content,parameters:[blankVariant,filledVariant]},
    facts:[11,12,13,14].map(id => ({id,name:`Pumpa ${id}`,description:'',variant_attributes:[]})),
    parameter_registry:[{name:'Dĺžka hadice',required:true,scope:'variant',values:[],unit:'cm'}]};
  await act(async () => { root.render(React.createElement(AiJobDetail,{key:'variant-parameters',job:variantJob,onChange:() => {}})); await tick(); });
  assert.equal(parameterRows().length,4, 'Each fact has one required slot; an existing blank row is not duplicated');
  for (const id of [11,13,14]) {
    assert(parameterRow('Dĺžka hadice',id).classList.contains('ai-parameter-missing'));
    assert.equal(parameterRow('Dĺžka hadice',id).querySelector('textarea').getAttribute('aria-invalid'),'true', 'Both blank and absent variant values expose a missing free-text control');
  }
  assert.equal(parameterRow('Dĺžka hadice',12).querySelector('textarea').value,'35');
  assert(!parameterRow('Dĺžka hadice',12).classList.contains('ai-parameter-missing'), 'A populated sibling remains satisfied');
  const missingVariantInput = parameterRow('Dĺžka hadice',14).querySelector('textarea');
  await input(missingVariantInput,'4');
  assert.equal(parameterRow('Dĺžka hadice',14).querySelector('textarea'),missingVariantInput, 'Editing a later placeholder preserves its textarea while an earlier slot remains unanswered');
  await input(missingVariantInput,'45');
  await click(button('Uložiť koncept obsahu'));
  assert.deepEqual(reviewed.content.parameters,[filledVariant,{name:'Dĺžka hadice',product_id:14,values:['45']}], 'Editing a missing variant targets its fact, preserves the filled sibling and excludes unanswered blank values');
  await input(parameterRow('Dĺžka hadice',11).querySelector('textarea'),'25');
  assert.equal(parameterRows().length,4, 'Correcting an existing blank value updates its row');
  await click(parameterRow('Dĺžka hadice',14).querySelector('button'));
  const restoredVariant = parameterRow('Dĺžka hadice',14);
  assert(restoredVariant.classList.contains('ai-parameter-missing') && restoredVariant.querySelector('textarea').value === '' && !restoredVariant.querySelector('button'), 'Removing a filled required variant restores its blank placeholder');
  await click(button('Uložiť koncept obsahu'));
  assert.deepEqual(reviewed.content.parameters,[{name:'Dĺžka hadice',product_id:11,values:['25']},filledVariant], 'A restored placeholder stays out of the saved draft and sibling values survive removal');
  await act(async () => { root.render(React.createElement(AiJobDetail,{key:'readonly-parameters',job:{...variantJob,status:'completed'},onChange:() => {}})); await tick(); });
  assert.equal(parameterRows().length,4);
  assert(parameterRows().every(row => [...row.querySelectorAll('select,textarea,button')].every(control => control.disabled)), 'Read-only jobs keep actual and synthetic parameter controls disabled');
  assert(button('Pridať parameter').disabled && !button('Uložiť koncept obsahu'));

  const verifiedEvidence = { claim:'Presný názov', source:'feed:1', quote:'Test bunda' };
  const unsupportedEvidence = { claim:'Nepodložené príslušenstvo', source:'https://manufacturer.example.test/unopened.pdf', quote:'Accessory' };
  await act(async () => { root.render(React.createElement(AiJobDetail,{key:'evidence-fix',job:{...jobs[0],status:'blocked',revision:4,output:{...content,evidence:[unsupportedEvidence,verifiedEvidence]},checks:{errors:['ai_unverified_official_evidence']},facts:[],events:[]},onChange:() => {}})); await tick(); });
  const unsupportedRow = [...document.querySelectorAll('tr')].find(row => row.textContent.includes('Nepodložené príslušenstvo'));
  await click(unsupportedRow.querySelector('button'));
  assert(document.body.textContent.includes('odstráň aj tvrdenia'), 'Evidence removal explains how unsupported claims must be corrected');
  await click(button('Uložiť koncept obsahu'));
  assert.deepEqual(reviewed.content.evidence,[verifiedEvidence], 'Operator can remove the blocking source while preserving other evidence in the saved review');
  const failedJob = { ...jobs[0], status: 'import_failed', output: content, revision: 7,
    checks: { warnings: ['EAN je vo feede, samostatný návod nebol priložený.'] },
    import_result: { errors: [], items: [{ status: 'uncertain', errors: ['import_outcome_unknown'] }] }, facts: [], events: [] };
  await act(async () => { root.render(React.createElement(AiJobDetail, { key: 'failed', job: failedJob, onChange: () => {} })); await tick(); });
  assert(document.querySelector('[role="alert"]').textContent.includes('Výsledok odoslania nie je potvrdený'), 'Concrete import blocker is visible above content warnings');
  assert(document.body.textContent.includes('samy osebe neblokujú import'), 'Warnings are explicitly distinguished from blockers');
  assert(!button('Nová AI príprava s odhadom'), 'Unknown previous import cannot be duplicated');
  await click(button('Overiť výsledok v e-shope'));
  assert.deepEqual(calls.findLast(c => c.path.endsWith('/action')).body, { action: 'retry_import', expected_revision: 7 }, 'Uncertain outcome offers explicit reconciliation');

  const partialJob = { ...jobs[0], status: 'import_blocked', output: content, revision: 8,
    facts: [{ id: 1, name: 'Modrá bunda', description: '', variant_attributes: [] }, { id: 2, name: 'Červená bunda', description: '', variant_attributes: [] }], events: [] };
  await act(async () => { root.render(React.createElement(AiJobDetail, { key: 'partial', job: partialJob, onChange: () => {} })); await tick(); });
  await click([...document.querySelectorAll('label')].find(l => l.textContent.includes('Červená bunda')).querySelector('input'));
  await click(button('Pripraviť pôvodný obsah bez AI'));
  assert.deepEqual(calls.findLast(c => c.path.endsWith('/fork')).body, { expected_revision: 8, product_ids: [1], reuse_content: false, use_ai: false }, 'Partial recovery keeps only selected variants and explicitly uses original content');

  const comparison = { id: 'update-fixture', state: 'ready', availability_basis:'supplier_unset_shop_stock', fields: ['title','long_description','metas','parameters','categories','availability'],
    before: { descriptions: [{ language: 'en', title: 'English old title' }, { language: 'sk', title: 'Pôvodný názov', long_description: '<p>Pôvodný popis</p>' }], metas: [{ key: 'future_name', value: 'Pôvodný model' }], parameters: [], categories: [{ code: 'K1', main_yn: true }], availability: 'Overíme' },
    after: { descriptions: [{ language: 'sk', title: 'Vylepšený názov', long_description: '<p>Vylepšený popis</p>' }], metas: [{ key: 'future_name', values: [{ language: 'sk', value: 'Nový model' }] }], parameters: [{ descriptions: [{ language: 'sk', name: 'Materiál' }], values: [{ descriptions: [{ language: 'sk', value: 'Hliník' }] }] }], categories: [{ code: 'K1', main_yn: false }, { code: 'K2', main_yn: true }], availability: 'Na objednávku' } };
  const updateJob = { ...jobs[0], status: 'completed', output: content, options, update_preview: comparison, facts: [], events: [] };
  await act(async () => { root.render(React.createElement(AiJobDetail, { key: 'update', job: updateJob, onChange: () => {} })); await tick(); });
  assert(document.body.textContent.includes('Pôvodný názov') && document.body.textContent.includes('Vylepšený názov'));
  assert(document.body.textContent.includes('Skladové množstvo e-shopu nie je vyplnené. Dostupnosť sa nastaví podľa dodávateľa; skladové množstvo sa nemení.'), 'Supplier availability preview explains unset shop stock and preservation of quantity');
  assert(!document.body.textContent.includes('English old title'), 'Comparison shows requested shop language only');
  assert(document.body.textContent.includes('Nový model') && document.body.textContent.includes('Materiál') && document.body.textContent.includes('Hliník'));
  const frames = [...document.querySelectorAll('iframe')];
  assert(frames.some(f => f.srcdoc.includes('Vylepšený popis')), 'HTML descriptions are previewed visually');
  assert(frames.every(f => f.hasAttribute('sandbox') && f.getAttribute('sandbox') === ''), 'Untrusted HTML comparisons remain sandboxed');
  assert.equal(document.querySelectorAll('pre').length, 0, 'Regular comparison does not expose API JSON');
  await click(button('Potvrdiť vybrané zmeny v e-shope'));
  assert.equal(calls.findLast(c => c.path.endsWith('/update-confirm')).body.preview_id, 'update-fixture');
  await act(async () => { root.render(React.createElement(AiJobDetail, { key: 'rejected', job: { ...updateJob, update_preview: { ...comparison, state: 'rejected' }, update_result: { status: 'rejected', fields: comparison.fields, error: 'upgates_update_http_422' } }, onChange: () => {} })); await tick(); });
  assert(document.querySelector('[role="alert"]'), 'Rejected update exposes a specific error');
  assert(!button('Overiť výsledok aktualizácie'), 'Known rejection is not presented as uncertain reconciliation');

  const uncertainReadback = { ...updateJob, status:'exists', update_only:true, update_state:'uncertain', update_preview:{...comparison,state:'uncertain'},
    update_result:{status:'uncertain',fields:comparison.fields,error:'ai_update_readback_mismatch',mismatched_fields:['parameters','categories'],
      observed:{...comparison.after,parameters:null,categories:[{code:'K2',main_yn:true}],descriptions:[{language:'sk',title:'Unrelated readback must not be shown'}]}} };
  await act(async () => { root.render(React.createElement(AiJobDetail,{key:'readback-mismatch',job:uncertainReadback,onChange:() => {}})); await tick(); });
  assert(document.querySelector('.ai-badge').textContent.includes('Výsledok nie je potvrdený') && !document.querySelector('.ai-badge').textContent.includes('Pripravené na aktualizáciu'), 'An existing-product job displays the uncertain update status rather than a ready badge');
  assert(document.querySelector('.ai-next-step').textContent.includes('overiť bez opakovaného odoslania'), 'Next action matches update reconciliation');
  assert(!button("Otvoriť obsah na úpravu") && button("Porovnať so stavom v e-shope").disabled, 'An uncertain update cannot be reopened or replaced by a new preview');
  const readbackAlert = document.querySelector('[role="alert"]');
  assert(readbackAlert.textContent.includes('Nepotvrdené polia') && readbackAlert.textContent.includes('Parametre') && readbackAlert.textContent.includes('Kategórie vrátane predkov'), 'Readback diagnostics identify the specific mismatched fields');
  const observedColumns = [...document.querySelectorAll('h4')].filter(heading => heading.textContent === 'Hodnota načítaná z e-shopu').map(heading => heading.parentElement);
  assert.equal(observedColumns.length,2, 'Actual readback appears only for mismatched fields');
  assert(observedColumns[0].textContent.includes('API e-shopu nevrátilo hodnotu tohto poľa.'));
  assert(observedColumns[1].textContent.includes('K2') && !observedColumns[1].textContent.includes('K1'), 'Observed categories are displayed separately from the expected chain');
  assert(!document.body.textContent.includes('Unrelated readback must not be shown'));
  assert(observedColumns.every(column => column.closest('details').open), 'Mismatch comparisons are opened for the operator');
  await click(button('Overiť výsledok aktualizácie'));
  assert.equal(calls.findLast(c => c.path.endsWith('/update-confirm')).body.preview_id,'update-fixture', 'Operator can reconcile the same update intent without creating a new update');
  assert.equal(calls.findLast(c => c.path.endsWith('/update-confirm')).body.original_request_settled, undefined, 'Readback alone never claims the original request is settled');
  const resolveButton = () => document.querySelector('[data-testid="ai-update-resolve"]');
  const settleCheckbox = () => document.querySelector('[data-testid="ai-update-settled"]');
  const resolutionNote = () => document.querySelector('[data-testid="ai-update-resolution-note"]');
  assert(resolveButton().disabled, 'Releasing uncertainty requires explicit documented confirmation');
  await input(resolutionNote(), 'Original request confirmed terminated');
  await click(settleCheckbox()); assert(!resolveButton().disabled);
  await input(resolutionNote(), 'Original request confirmed completed by operator');
  assert(!settleCheckbox().checked && resolveButton().disabled, 'Editing settlement evidence revokes the old confirmation');
  await click(settleCheckbox()); await click(resolveButton());
  assert.equal(calls.findLast(c => c.path.endsWith('/update-confirm')).body.original_request_settled, true);
  assert.equal(calls.findLast(c => c.path.endsWith('/update-confirm')).body.resolution_note, 'Original request confirmed completed by operator');

  await act(async () => { root.render(React.createElement(AiJobDetail,{key:'pending-content',job:{...uncertainReadback,status:'review',update_only:false},onChange:() => {}})); await tick(); });
  assert([...document.querySelectorAll('label')].find(label => label.textContent.startsWith('Názov')).querySelector('textarea').disabled, 'Pending updates keep content immutable until reconciled');
  assert(!button('Uložiť koncept obsahu') && !button('Nová AI príprava s odhadom') && !button("Zrušiť túto úlohu"), 'Blocked edit, fork and cancel actions are absent while the update is pending');
  assert(!button('Overiť výsledok aktualizácie').disabled, 'An uncertain update retains its reconciliation action');
  await act(async () => { root.render(React.createElement(AiJobDetail,{key:'sending-content',job:{...uncertainReadback,update_preview:{...comparison,state:'sending',sending_at:new Date().toISOString()},update_state:'sending',update_result:{status:'sending',fields:comparison.fields}},onChange:() => {}})); await tick(); });
  assert(!button('Overiť výsledok aktualizácie'), 'An active send displays its pending state without a redundant submission control');
  assert.equal(resolveButton(), null, 'A currently active send has no early settlement control');
  await act(async () => { root.render(React.createElement(AiJobDetail,{key:'old-sending-content',job:{...uncertainReadback,update_preview:{...comparison,state:'sending',sending_at:new Date(Date.now()-6*60*1000).toISOString()},update_state:'sending'},onChange:() => {}})); await tick(); });
  assert(resolveButton().disabled && !settleCheckbox().checked, 'An abandoned sending intent can be resolved only with fresh evidence and confirmation');

  const existingReview = { ...updateJob, update_only: true, source_kind: 'shop', update_preview: null, status: 'review',
    applied_rules: [{id:'common',name:'Overené spoločné pravidlá',text:'Použitá konkrétna inštrukcia.'}], parameter_registry: [{name:'Materiál',scope:'parent',values:['Hliník'],required:true}],
    facts:[{id:77,name:'Existujúci produkt',description:'Pôvodný text z e-shopu',variant_attributes:[]}] };
  await act(async () => { root.render(React.createElement(AiJobDetail, { key:'existing-review', job:existingReview, onChange:() => {} })); await tick(); });
  assert(document.body.textContent.includes('Aktuálny obsah e-shopu'), 'Existing source is identified separately from supplier feed');
  assert(button('Schváliť obsah pre aktualizáciu'), 'Approval explains update-only purpose');
  assert(!button('Schváliť obsah a pripraviť import') && !button('Nová AI príprava s odhadom'), 'Shop-source jobs cannot create or fork imports');
  const availability = [...document.querySelectorAll('label')].find(l => l.textContent === 'Dostupnosť (samostatný produkt)').querySelector('input');
  assert(availability.disabled && !availability.checked, 'Shop-source update preserves availability without supplier stock');
  assert(document.body.textContent.includes('Použitá konkrétna inštrukcia.') && document.body.textContent.includes('Materiál'), 'Job exposes exact frozen rules and parameter registry');

  await act(async () => { root.render(React.createElement(AiJobDetail, { key:'supplier-policy-review', job:{...existingReview, source_kind:'catalog',
    resolved_import_policy:{orderable:'do 7 dní',unknown:'overíme',hide_zero_stock:false,supplier_name:'Supplier from config'}}, onChange:() => {} })); await tick(); });
  const appliedPolicy = [...document.querySelectorAll('h3')].find(heading => heading.textContent === 'Importné pravidlá – dostupnosť a dodávateľ').parentElement;
  assert(appliedPolicy.textContent.includes('do 7 dní') && appliedPolicy.textContent.includes('overíme') && appliedPolicy.textContent.includes('Supplier from config'), 'Review displays effective availability returned by the API');
  assert(appliedPolicy.textContent.includes('Texty dostupnosti sa berú z konfigurácie dodávateľa'), 'Review identifies supplier configuration as the availability source');

  const treeCategories = [{ code: 'P', parent_code: null, assignable: false, names: { sk: 'Doplnky' } }, { code: 'C', parent_code: 'P', names: { sk: 'Ručné pumpy' } }, { code: 'S', parent_code: 'P', names: { sk: 'Servis' } }];
  let selectedCategory;
  function TreeHarness() { const [value, setValue] = React.useState(''); return React.createElement(CategoryTree, { categories: treeCategories, value, onChange: code => { selectedCategory = code; setValue(code); } }); }
  await act(async () => { root.render(React.createElement(TreeHarness)); await tick(); });
  assert(![...document.querySelectorAll('button')].some(b => b.textContent.startsWith('Doplnky')), 'Navigation root is visible but cannot be selected as a product category');
  await click([...document.querySelectorAll('button')].find(b => b.textContent.startsWith('Ručné pumpy')));
  assert.equal(selectedCategory, 'C');
  assert(document.querySelector('summary').textContent.includes('Doplnky / Ručné pumpy'), 'Selected category displays full ancestor path');
  await input(document.querySelector('input'), 'rucne');
  assert(document.body.textContent.includes('Doplnky / Ručné pumpy'));
  assert(!document.body.textContent.includes('Servis'), 'Accent-insensitive search filters unrelated categories');
  await click(button('Zrušiť výber')); assert.equal(selectedCategory, '');

  const editorRules = { ...rules, book: { ...book, rules: [...book.rules,
    { ...book.rules[0], id: 'supplier', name: 'Paul Lange pravidlo', scope: { ...scope, supplier: 'paul-lange' }, import_policy: { orderable:'Old ignored override', unknown:'Old unknown override', hide_zero_stock:true, supplier_name:'Original name' } },
    { ...book.rules[0], id: 'brand', name: 'Zéfal pravidlo', scope: { ...scope, brand: 'Zéfal' } }] } };
  await act(async () => { root.render(React.createElement(AiRuleEditor, { rules: editorRules, onReload: () => {}, onJob: () => {} })); await tick(); });
  assert(button('Publikovať koncept').disabled, 'A missing draft has a readable action label without an invented version number');
  assert(!document.body.textContent.includes('#—'));
  assert(document.getElementById(button('Publikovať koncept').getAttribute('aria-describedby')).textContent.includes('Najskôr vyplň dôvod zmeny'), 'Disabled publishing explains how to enable the action');
  await click([...document.querySelectorAll('button')].find(b => b.textContent.startsWith('Dodávatelia (')));
  const profileSelect = [...document.querySelectorAll('label')].find(l => l.textContent.startsWith('Profil')).querySelector('select');
  assert.equal(profileSelect.options.length, 1); assert.equal(profileSelect.options[0].textContent, 'Paul Lange pravidlo', 'Rule groups filter profiles');
  assert(document.body.textContent.includes('Texty dostupnosti sa berú z konfigurácie dodávateľa'), 'Rule editor explains where availability can be changed');
  assert(!document.querySelector('input[value="Old ignored override"]') && !document.querySelector('input[value="Old unknown override"]'), 'Obsolete availability overrides are not editable');
  assert(![...document.querySelectorAll('label')].some(label => label.textContent.startsWith('Skryť nové varianty s potvrdenou nulovou zásobou')), 'Rule editor cannot hide orderable items based on zero supplier stock');
  await input([...document.querySelectorAll('label')].find(label => label.textContent === 'Názov dodávateľa v e-shope').querySelector('input'), 'Updated name');
  await input([...document.querySelectorAll('label')].find(label => label.textContent === 'Dôvod zmeny').querySelector('input'), 'Update supplier display name');
  await click(button('Uložiť novú verziu konceptu'));
  assert.deepEqual(calls.findLast(c => c.path.endsWith('/rules') && c.body).body.book.rules.find(rule => rule.id === 'supplier').import_policy,
    {...editorRules.book.rules.find(rule => rule.id === 'supplier').import_policy,supplier_name:'Updated name'}, 'Changing supplier name preserves unrelated legacy values for historical rule compatibility');
  window.confirm = () => false; await click(button('Odstrániť pravidlo / profil'));
  assert(document.querySelector('input[value="Paul Lange pravidlo"]'), 'Cancelling delete keeps rule');
  window.confirm = () => true; await click(button('Odstrániť pravidlo / profil'));
  assert(!document.querySelector('input[value="Paul Lange pravidlo"]'));
  const note = [...document.querySelectorAll('label')].find(l => l.textContent === 'Dôvod zmeny').querySelector('input');
  await input(note, 'Odstránené staré pravidlo'); await click(button('Uložiť novú verziu konceptu'));
  assert.deepEqual(calls.findLast(c => c.path.endsWith('/rules') && c.body).body.book.rules.map(r => r.id), ['common', 'brand'], 'Delete persists in draft without touching other groups');
  assert(!calls.some(c => /\/publish$/.test(c.path)), 'Rule deletion does not publish automatically');
  assert(!button('Publikovať verziu #2').disabled, 'Saving a draft enables publishing the actual version');
  assert(!document.getElementById('ai-publish-help'), 'No-draft instruction disappears when a draft is available');

  const legacyRule = { ...book.rules[0], id: 'legacy-category', name: 'Pôvodné pravidlo duší', scope: { ...scope, category: 'general' }, official_domains: ['example.com'], import_policy: { orderable: 'Na objednávku' } };
  historicalBook = { ...book, rules: [...book.rules, legacyRule] };
  await act(async () => { root.render(React.createElement(AiRuleEditor, { key: 'single-category-tab', rules, onReload: () => {}, onJob: () => {} })); await tick(); });
  const categoryButtons = [...document.querySelectorAll('.ai-rule-groups button')].filter(b => b.textContent.startsWith('Kategórie'));
  assert.equal(categoryButtons.length, 1, 'Only one category navigation entry is offered');
  assert(!button('Profily kategórií'), 'The duplicate category profiles navigation is removed');
  await click(categoryButtons[0]);
  assert(document.body.textContent.includes('Priradenie ku kategóriám e-shopov'));
  assert(button('Pridať parameter'), 'Categories opens profile parameters directly');
  assert(!document.body.textContent.includes('Doplnkové pravidlá zo starších verzií'), 'Normal profiles show no legacy controls');
  await input([...document.querySelectorAll('label')].find(l => l.textContent === 'Názov profilu').querySelector('input'), 'Upravený profil');
  await input([...document.querySelectorAll('label')].find(l => l.textContent === 'Dôvod zmeny').querySelector('input'), 'Úprava profilu kategórie');
  await click(button('Uložiť novú verziu konceptu'));
  const savedProfileBook = calls.findLast(c => c.path.endsWith('/rules') && c.body).body.book;
  assert.equal(savedProfileBook.categories[0].name, 'Upravený profil');
  assert.deepEqual(savedProfileBook.rules, book.rules, 'Editing a profile preserves other rules');
  const historySelect = [...document.querySelectorAll('label')].find(l => l.textContent.startsWith('História verzií')).querySelector('select');
  await act(async () => { historySelect.value = '1'; historySelect.dispatchEvent(new dom.window.Event('change', { bubbles: true })); await tick(); });
  assert(document.body.textContent.includes('Doplnkové pravidlá zo starších verzií'), 'Loading old versions keeps their scoped rules accessible');
  await input([...document.querySelectorAll('label')].find(l => l.textContent === 'Názov profilu').querySelector('input'), 'Profil zo staršej verzie');
  await click(button('Uložiť novú verziu konceptu'));
  assert.deepEqual(calls.findLast(c => c.path.endsWith('/rules') && c.body).body.book.rules, historicalBook.rules, 'Saving profiles preserves legacy rules including domains and import policy');
  const legacyDetails = [...document.querySelectorAll('details')].find(d => d.querySelector('summary')?.textContent === 'Doplnkové pravidlá zo starších verzií');
  await click(legacyDetails.querySelector('summary'));
  await click(button('Pôvodné pravidlo duší'));
  assert(document.querySelector('input[value="example.com"]'), 'Legacy rules remain editable');
  window.confirm = () => true; await click(button('Odstrániť pravidlo / profil'));
  await click(button('Kategórie (1)'));
  assert(!document.body.textContent.includes('Doplnkové pravidlá zo starších verzií'), 'Removing the final legacy rule leaves only profiles');
  await click(button('Uložiť novú verziu konceptu'));
  assert.deepEqual(calls.findLast(c => c.path.endsWith('/rules') && c.body).body.book.rules, book.rules, 'Legacy deletion preserves unrelated rules');
  await act(async () => root.unmount());
  console.log('AI UI passed: selection, two shops, cost pause, content edits, archive/restore, actionable import errors, partial recovery, readable updates, category tree/search and scoped rule deletion.');
})().catch(error => { console.error(error); process.exitCode = 1; root.unmount(); });
