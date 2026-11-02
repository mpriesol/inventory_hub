/* Real content-pilot page and authenticated clients with synthetic API responses. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<html><body><div id="root"></div></body></html>', { url: 'https://hub.example.test/migration/atomer' });
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
const { unlockHub } = require('../src/api/access.ts');
const { AtomerContentMigration } = require('../src/pages/AtomerContentMigration.tsx');
const requests = []; let losePreview = true; let loseSend = true; let invalidPreview = false; let stored; let restored;
let loseRollback = true; let preflightRejected = false;
const base = { id: '', product_id: 1, shop_code: 'xtrek', workflow: 'atomer_content', state: 'ready',
  source_code: 'SOURCE', hub_sku: 'HUB', source_sha256: 'a'.repeat(64), no_op: false,
  fields: ['long_description', 'seo_title', 'seo_description'],
  before: { long_description: '<img src="unsafe" onerror="alert(1)">', seo_title: 'Old title', seo_description: 'Old description' },
  after: { long_description: '<p>New <strong>content</strong></p>', seo_title: 'New title', seo_description: 'New description' },
  expires_at: new Date(Date.now() + 3600000).toISOString(), error: null };
global.fetch = async (path, init = {}) => {
  assert.equal(init.headers['X-Hub-Request'], '1');
  assert.equal(init.credentials, 'same-origin');
  assert.equal(init.redirect, 'error');
  requests.push({ path, init });
  let data;
  if (path === '/api/migration/atomer-content/publications') data = { items: [stored, restored].filter(Boolean).map(item => structuredClone(item)) };
  else if (path.endsWith('/rollback-preview')) {
    const request = JSON.parse(init.body);
    if (!restored) restored = { ...structuredClone(stored), id: request.request_id, rollback_of: stored.id, state: 'ready', before: structuredClone(stored.after), after: structuredClone(stored.before) };
    assert.equal(request.request_id, restored.id, 'Rollback recovery retains the same operation UUID');
    if (loseRollback) { loseRollback = false; throw new Error('Lost rollback preview response'); }
    data = structuredClone(restored);
  }
  else if (path.endsWith('/preview')) {
    assert(init.body instanceof FormData); assert(!init.headers['Content-Type'], 'Browser sets the multipart boundary');
    if (invalidPreview) return { ok: false, status: 422, json: async () => ({ detail: { code: 'atomer_source_invalid_xml' } }) };
    const selection = JSON.parse(init.body.get('selection'));
    stored = { ...structuredClone(base), id: selection.items[0].request_id };
    if (losePreview) { losePreview = false; throw new Error('Lost preview response'); }
    data = { source: { sha256: stored.source_sha256, size_bytes: 10, total_rows: 1 }, items: [{ source_code: 'SOURCE', hub_sku: 'HUB', publication: structuredClone(stored) }] };
  } else if (path.endsWith('/send')) {
    assert.deepEqual(JSON.parse(init.body), { confirmed: true });
    if (preflightRejected) return { ok: false, status: 409, json: async () => ({ detail: { code: 'product_publication_remote_changed' } }) };
    stored.state = 'uncertain';
    if (loseSend) { loseSend = false; throw new Error('Lost send response'); }
    throw new Error('Unexpected repeated send');
  } else if (path.endsWith('/resolve')) {
    const body = JSON.parse(init.body);
    assert.equal(body.confirmed, true); assert.equal(body.original_request_settled, true);
    assert(body.note.length >= 10); stored.state = 'completed'; data = structuredClone(stored);
  } else if (path.startsWith('/api/product-editor/publications/')) data = structuredClone(stored);
  else throw new Error('Unexpected path: ' + path);
  return { ok: true, status: 200, json: async () => data };
};
const root = createRoot(document.getElementById('root'));
const button = text => [...document.querySelectorAll('button')].find(item => item.textContent.trim() === text);
async function click(element) { assert(element, 'Missing button'); await act(async () => element.dispatchEvent(new window.MouseEvent('click', { bubbles: true }))); }
async function input(element, value) {
  assert(element, 'Missing input');
  const proto = element.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
  await act(async () => { Object.getOwnPropertyDescriptor(proto, 'value').set.call(element, value); element.dispatchEvent(new window.Event('input', { bubbles: true })); });
}
(async () => {
  unlockHub('synthetic-test-token');
  await act(async () => root.render(React.createElement(MemoryRouter, null, React.createElement(AtomerContentMigration))));
  assert(button('Pripraviť náhľad').disabled);
  const fileInput = document.querySelector('input[type=file]');
  await act(async () => { Object.defineProperty(fileInput, 'files', { value: [new File(['<PRODUCTS/>'], 'buyers.xml', { type: 'application/xml' })], configurable: true }); fileInput.dispatchEvent(new window.Event('change', { bubbles: true })); });
  const textInputs = [...document.querySelectorAll('input')].filter(item => item.type === 'text');
  await input(textInputs[0], 'SOURCE'); await input(textInputs[1], 'HUB');
  await click(button('Pripraviť náhľad'));
  assert(button('Obnoviť rovnaký náhľad')); assert(textInputs[0].matches(':disabled'), 'Unknown preview retains a frozen selection');
  assert(!button('Pripraviť ďalší výber'), 'Lost preview cannot be replaced with fresh UUIDs');
  const original = JSON.parse(requests.find(item => item.path.endsWith('/preview')).init.body.get('selection'));
  await click(button('Obnoviť rovnaký náhľad'));
  const replay = JSON.parse(requests.filter(item => item.path.endsWith('/preview')).at(-1).init.body.get('selection'));
  assert.deepEqual(replay, original, 'Recovery retains exact operation IDs, fields and source selection');
  assert.deepEqual(replay.items[0].fields, base.fields);
  assert.equal(document.querySelectorAll('img').length, 0, 'Source and target HTML are displayed as text, never executed');
  assert(document.body.textContent.includes('<img src="unsafe"'));
  assert.equal(requests.filter(item => item.path.endsWith('/send')).length, 0, 'Preview never publishes automatically');
  await click(button('Odoslať vybrané texty do xTrek'));
  assert.equal(requests.filter(item => item.path.endsWith('/send')).length, 1);
  assert(!button('Odoslať vybrané texty do xTrek'), 'Lost send response hides publication retry');
  await click(button('Pripraviť ďalší výber'));
  assert(button('Pripraviť náhľad').disabled, 'An uncertain card cannot bypass recovery through a fresh preview');
  await click(button('Načítať uložený výsledok'));
  assert(!button('Odoslať vybrané texty do xTrek'));
  assert(button('Uložiť overenie a načítať aktuálny stav').disabled);
  await input(document.querySelector('textarea'), 'Support verified the request is settled.');
  assert(button('Uložiť overenie a načítať aktuálny stav').disabled, 'Settlement must also be explicitly checked');
  const settled = [...document.querySelectorAll('input[type=checkbox]')].find(item => !item.closest('fieldset'));
  await click(settled);
  await click(button('Uložiť overenie a načítať aktuálny stav'));
  assert(document.body.textContent.includes('Overené v e-shope'));
  assert.equal(requests.filter(item => item.path.endsWith('/send')).length, 1, 'Resolution does not repeat PUT');
  await click(button('Pripraviť návrat pôvodných textov'));
  await click(button('Obnoviť rovnaký náhľad návratu'));
  assert.equal(requests.filter(item => item.path.endsWith('/send')).length, 1, 'Rollback preview does not send');
  assert(document.body.textContent.includes('Návrat textov operácie'));
  const restoreCard = document.querySelector(`[data-publication-id="${restored.id}"]`);
  assert(restoreCard.textContent.includes('Odoslať vybrané texty do xTrek'), 'Restoration requires a separate reviewed send');
  assert(!restoreCard.textContent.includes('Pripraviť návrat pôvodných textov'), 'Nested rollback is not offered');
  restored.state = 'completed'; restored.no_op = true;
  stored.no_op = true;
  await click(button('Načítať históriu'));
  assert(document.body.textContent.includes('Bez zmien – hodnoty už zodpovedajú'));
  assert(!button('Odoslať vybrané texty do xTrek'), 'No-op offers no send');
  invalidPreview = true;
  await click(button('Pripraviť náhľad'));
  assert(!textInputs[0].matches(':disabled'), 'Definitive XML validation failure unlocks source corrections');
  assert(button('Pripraviť náhľad'), 'Invalid XML does not become an indefinitely frozen recovery');
  // A terminal history read clears the uncertainty left by a lost send response.
  restored = null; stored = { ...structuredClone(base), id: 'b38b04ef-70ef-4118-89ce-8babcd4e1d15' }; loseSend = true;
  await act(async () => root.render(React.createElement(MemoryRouter, null, React.createElement(AtomerContentMigration, { key: 'history-recovery' }))));
  await click(button('Odoslať vybrané texty do xTrek'));
  stored.state = 'completed'; stored.no_op = true;
  await click(button('Načítať históriu'));
  assert(!document.body.textContent.includes('Výsledok odoslania nie je potvrdený.'), 'Terminal history clears obsolete uncertainty');
  await act(async () => i18n.changeLanguage('en'));
  assert(document.body.textContent.includes('No changes – values already match'));
  assert(button('Prepare preview'));
  // A definite preflight rejection leaves correction possible and never enters write recovery.
  await act(async () => i18n.changeLanguage('sk'));
  stored = { ...structuredClone(base), id: 'db510ef6-99d7-4265-aa8d-923f1ece28e5' }; preflightRejected = true;
  await act(async () => root.render(React.createElement(MemoryRouter, null, React.createElement(AtomerContentMigration, { key: 'preflight-rejection' }))));
  await click(button('Odoslať vybrané texty do xTrek'));
  assert(document.body.textContent.includes('Požiadavka bola zastavená pred odoslaním.'));
  assert(!document.body.textContent.includes('Výsledok odoslania nie je potvrdený.'));
  await click(button('Načítať uložený výsledok'));
  assert(!button('Odoslať vybrané texty do xTrek'), 'Re-read does not repeat a rejected old preview');
  assert(![...document.querySelectorAll('input')].find(item => item.type === 'text').matches(':disabled'), 'Source corrections remain available');
  await act(async () => unlockHub(''));
  assert(!document.body.textContent.includes('Old title'), 'Changing access clears private preview data');
  assert(button('Pripraviť náhľad').disabled);
  await act(async () => root.unmount());
  console.log('Atomer content UI: multipart authentication, exact preview replay, text-only HTML, no automatic send, ambiguous recovery and account scope passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
