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
const calls = []; let jobs = []; let reviewed;
global.fetch = async (path, init = {}) => {
  const body = init.body ? JSON.parse(init.body) : undefined; calls.push({ path, body, headers: init.headers });
  let data;
  if (path.endsWith('/status')) data = { enabled: true, key_configured: true, access_configured: true, model: 'fixture', monthly_limit_usd: '20', job_limit_usd: '2' };
  else if (path.endsWith('/rules')) data = rules;
  else if (path.endsWith('/selection')) data = families;
  else if (path.includes('/catalog?')) data = { shops: [{ code: 'biketrek', name: 'BikeTrek', ready: true }, { code: 'xtrek', name: 'xTrek', ready: true }] };
  else if (path.endsWith('/import/options')) data = { languages: [{ code: 'sk', currency: 'EUR' }], pricelists: [{ name: 'Predvolené', default: true }], categories: [{ code: 'K1', names: { sk: 'Category' } }] };
  else if (path.endsWith('/batches')) { jobs = body.targets.map((target, i) => ({ id: 'job' + i, batch_id: 'batch', kind: 'product', status: 'estimate', revision: 1, name: 'Test bunda', code: 'NF-G-TEST', shop: target.shop, policy, origins: {}, checks: {}, use_ai: true, estimate_usd: '0.3', actual_usd: null, product_ids: [1, 2] })); data = { jobs }; }
  else if (path.endsWith('/jobs')) data = jobs;
  else if (path.endsWith('/review')) { reviewed = body; data = { ...jobs[0], output: body.content, revision: 2, status: body.approve ? 'preparing_import' : 'review' }; }
  else if (path.includes('/jobs/')) data = jobs.find(j => path.endsWith(j.id)) || jobs[0];
  else throw new Error('Unexpected synthetic endpoint ' + path);
  return { ok: true, json: async () => data };
};
const button = text => [...document.querySelectorAll('button')].find(b => b.textContent === text);
const tick = () => new Promise(resolve => setTimeout(resolve, 5));
async function click(element) { assert(element, 'Element exists'); await act(async () => { element.click(); await tick(); }); }
async function input(element, value) { await act(async () => { Object.getOwnPropertyDescriptor(dom.window.HTMLTextAreaElement.prototype, 'value').set.call(element, value); element.dispatchEvent(new dom.window.Event('input', { bubbles: true })); await tick(); }); }

(async () => {
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
  await act(async () => { root.render(React.createElement(AiJobDetail, { job: { ...jobs[0], status: 'review', output: content, facts: [], events: [] }, onChange: () => {} })); await tick(); });
  const title = [...document.querySelectorAll('label')].find(l => l.textContent.startsWith('Názov')).querySelector('textarea');
  await input(title, 'Upravený názov');
  assert.equal(title.value, 'Upravený názov', 'Content fields accept edits');
  await click(button('Uložiť koncept obsahu'));
  assert.equal(reviewed.content.title, 'Upravený názov'); assert.equal(reviewed.approve, false);
  await click(button('Schváliť obsah a pripraviť import')); assert.equal(reviewed.approve, true);
  await act(async () => root.unmount());
  console.log('AI UI passed: per-family checkbox, original-content selection, two shops, cost pause, scoped token, editable content and explicit approval.');
})().catch(error => { console.error(error); process.exitCode = 1; root.unmount(); });
