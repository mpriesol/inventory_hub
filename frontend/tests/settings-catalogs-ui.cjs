/* Shared dictionary and generic override controls use synthetic Hub APIs only. */
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
require('../src/i18n/index.ts');
const React = require('react'); const { act } = React;
const { createRoot } = require('react-dom/client');
const { CatalogSettingsPanel } = require('../src/components/settings/CatalogSettingsPanel.tsx');
const { ProductAvailabilityOverridePanel } = require('../src/components/product/ProductAvailabilityOverridePanel.tsx');
const { unlockHub } = require('../src/api/access.ts');
const root = createRoot(document.getElementById('root'));
const tick = () => new Promise(resolve => setTimeout(resolve, 5));
const required = id => { const result = document.querySelector(`[data-testid="${id}"]`); assert(result, `${id} exists`); return result; };
async function click(id) { await act(async () => { required(id).click(); await tick(); }); }
async function input(id, value) {
  await act(async () => {
    const target = required(id), proto = target.tagName === 'SELECT' ? dom.window.HTMLSelectElement.prototype : dom.window.HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, 'value').set.call(target, value);
    target.dispatchEvent(new dom.window.Event(target.tagName === 'SELECT' ? 'change' : 'input', { bubbles: true })); await tick();
  });
}
const items = [{ id: 1, label: 'SKLADOM', is_active: true, sort_order: 0, revision: 1 }, { id: 2, label: 'overíme', is_active: true, sort_order: 10, revision: 1 }];
const writes = [], results = new Map(); let lost = false;
const reply = (value, status = 200) => ({ ok: status < 400, status, json: async () => JSON.parse(JSON.stringify(value)) });
global.fetch = async (url, request = {}) => {
  assert(url.startsWith('/api/')); assert.equal(request.credentials, 'same-origin'); assert.equal(request.cache, 'no-store');
  if (request.method === 'GET') {
    if (url.startsWith('/api/settings/catalogs/availabilities')) return reply({ items });
    if (url.startsWith('/api/settings/catalogs/requests/') || url.startsWith('/api/product-editor/saves/')) return results.has(url.split('/').at(-1)) ? reply(results.get(url.split('/').at(-1))) : reply({ detail: { code: 'catalog_request_missing' } }, 404);
    throw Error(`Unexpected read ${url}`);
  }
  const body = JSON.parse(request.body); writes.push({ url, body });
  if (url === '/api/settings/catalogs/availabilities') {
    assert.equal(body.expected_revision, 0); assert.equal(body.label, 'do 8 dní');
    const item = { id: 3, label: body.label, is_active: true, sort_order: 0, revision: 1 };
    items.push(item); results.set(body.request_id, item); return reply(item);
  }
  assert.equal(url, '/api/settings/catalogs/product-availability');
  results.set(body.request_id, { request_id: body.request_id, results: [] });
  if (lost) { lost = false; throw Error('Lost response after commit'); }
  return reply(results.get(body.request_id));
};
(async () => {
  unlockHub('synthetic-fixture-token');
  await act(async () => { root.render(React.createElement(CatalogSettingsPanel, { kind: 'availability' })); await tick(); });
  await input('catalog-name', 'do 8 dní'); await click('catalog-save');
  assert.equal(writes.length, 1); assert(required('catalog-availability').textContent.includes('do 8 dní'));
  const row = { id: 91, revision: 4, availability_override: null };
  let dirty = false, busy = false, changed = 0;
  const props = { row, disabled: false, onChanged: () => changed++, onDirtyChange: value => { dirty = value; }, onBusyChange: value => { busy = value; } };
  await act(async () => { root.render(React.createElement(ProductAvailabilityOverridePanel, props)); await tick(); });
  assert.equal(required('manual-availability-label').tagName, 'SELECT');
  assert([...required('manual-availability-label').options].some(option => option.value === 'do 8 dní'));
  await input('manual-availability-label', 'do 8 dní'); await input('manual-availability-visible', 'false');
  assert(dirty); lost = true; await click('manual-availability-save');
  const saved = writes.at(-1).body;
  assert.equal(saved.changes[0].expected_revision, 4); assert.equal(saved.changes[0].visible, false);
  assert.equal(saved.changes[0].label, 'do 8 dní'); assert(required('manual-availability-label').disabled);
  assert.equal(changed, 0); assert(!busy);
  const count = writes.length;
  await act(async () => { root.render(React.createElement('div')); await tick(); });
  await act(async () => { root.render(React.createElement(ProductAvailabilityOverridePanel, props)); await tick(); });
  assert(required('manual-availability-label').disabled, 'Uncertain intent is restored after reopening');
  await click('manual-availability-recover');
  assert.equal(writes.length, count, 'Lost response resolves by read, not a fresh write'); assert.equal(changed, 1);
  await act(async () => { root.render(React.createElement(ProductAvailabilityOverridePanel, { ...props,
    row: { ...row, revision: 5, availability_override: { label: 'do 8 dní', visible: false, source: 'manual', actor: 'user:1', updated_at: '2026-11-17T10:00:00Z' } } })); await tick(); });
  assert(required('product-availability-override').textContent.includes('Ručne nastavené'));
  await click('manual-availability-clear'); assert.equal(writes.at(-1).body.changes[0].clear, true);
  assert(!('label' in writes.at(-1).body.changes[0]));
  await act(async () => root.unmount());
  console.log('Shared settings UI: database choices, false visibility, revision save, remounted response recovery and reset passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
