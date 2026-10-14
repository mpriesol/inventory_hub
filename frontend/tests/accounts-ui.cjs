/* Persistent sessions through the real access client; synthetic API only. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'https://hub.example.test/settings/users' });
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
const { MemoryRouter, Routes, Route, Outlet, useLocation } = require('react-router-dom');
const { AccountPage } = require('../src/pages/AccountPage.tsx');
const { SessionBootstrap } = require('../src/components/SessionBootstrap.tsx');
const { RequireHubSession } = require('../src/components/RequireHubSession.tsx');
const access = require('../src/api/access.ts');
const root = createRoot(document.getElementById('root'));
const account = { id: 1, username: 'test-user', display_name: 'Test User', role: 'admin', active: true };
let serverUser = null, releaseSession;
const calls = [];
global.fetch = async (path, init = {}) => {
  const body = init.body ? JSON.parse(init.body) : undefined;
  calls.push({path, body, ...init});
  let result;
  if (path.endsWith('/session')) {
    if (releaseSession === 'hold') return new Promise(resolve => { releaseSession = () => resolve({ok:true,status:200,json:async()=>({user:account})}); });
    result = {user: serverUser};
  } else if (path.endsWith('/login')) { serverUser = account; result = {user:account}; }
  else if (path.endsWith('/logout')) { serverUser = null; result = {ok:true}; }
  else if (path.endsWith('/users')) result = body ? {...account,id:2,username:body.username} : [account];
  else throw new Error('Unexpected account fixture endpoint ' + path);
  return {ok:true,status:200,json:async()=>result};
};
const tick = () => new Promise(resolve => setTimeout(resolve, 5));
const button = text => [...document.querySelectorAll('button')].find(b => b.textContent === text);
async function click(element) { assert(element); await act(async () => { element.click(); await tick(); }); }
async function input(name, value) { await act(async () => {
  const element = document.querySelector(`[name="${name}"]`);
  Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, 'value').set.call(element, value);
  element.dispatchEvent(new dom.window.Event('input', {bubbles:true})); await tick();
}); }
async function render(key) { await act(async () => {
  root.render(React.createElement(MemoryRouter, {key}, React.createElement(SessionBootstrap, null, React.createElement(AccountPage)))); await tick();
}); await act(tick); }
(async () => {
  await render('initial');
  assert(!access.hubUnlocked());
  await input('username','test-user'); await input('password','synthetic-password-123');
  await click(button('Prihlásiť sa'));
  assert(access.hubUnlocked());
  assert(document.body.textContent.includes('Test User'));
  assert(button('Vytvoriť účet'));
  const login = calls.find(c => c.path.endsWith('/login'));
  assert.equal(login.credentials, 'same-origin');
  assert.equal(login.headers['X-Hub-Request'], '1');
  assert(!login.headers.Authorization);
  assert.equal(document.querySelector('input[type="password"]').value, '', 'Password cleared after login');
  await act(async () => { access.setHubUser(null); });
  await render('reload');
  assert(access.hubUnlocked(), 'Restored cookie session unlocks all shared features');
  const revision = access.accessRevision();
  await act(async () => { await access.restoreSession(); });
  assert.equal(access.accessRevision(), revision, 'Focus refresh does not remount editors or discard unsaved changes');
  assert.equal(dom.window.localStorage.length, 0);
  assert.equal(dom.window.sessionStorage.length, 0);
  await click(button('Odhlásiť sa'));
  assert(!access.hubUnlocked());
  assert(!button('Vytvoriť účet'));
  await render('signed-out-reload');
  assert(!access.hubUnlocked(), 'Sign-out persists after refresh');
  await act(async () => root.unmount());
  access.setHubUser(account);
  releaseSession = 'hold';
  const pending = access.restoreSession();
  await access.logoutHub(); releaseSession(); await pending;
  assert.equal(access.hubUser(), null, 'A stale session read cannot restore the account after sign-out');
  releaseSession = undefined;

  // Exercise the real route boundary: a deep link must not mount private content
  // until login, and successful login must return to that exact local route.
  const guardedRoot = createRoot(document.getElementById('root'));
  let privateMounts = 0;
  function PrivatePage() { React.useEffect(() => { privateMounts += 1; }, []); return React.createElement('div', { 'data-private': true }, 'Private warehouse'); }
  function Location() { const location = useLocation(); return React.createElement('output', { 'data-location': true }, location.pathname + location.search); }
  const privatePath = '/receiving/fixture?source=invoice';
  await act(async () => {
    guardedRoot.render(React.createElement(MemoryRouter, { initialEntries: [privatePath] },
      React.createElement(Location), React.createElement(Routes, null,
        React.createElement(Route, { element: React.createElement(SessionBootstrap, null, React.createElement(RequireHubSession, null, React.createElement(Outlet))) },
          React.createElement(Route, { path: '/login', element: React.createElement(AccountPage) }),
          React.createElement(Route, { path: '/receiving/:invoice', element: React.createElement(PrivatePage) })))));
    await tick();
  });
  await act(tick);
  assert.equal(privateMounts, 0, 'Protected page does not mount or request data while signed out');
  assert.equal(document.querySelector('[data-location]').textContent, '/login');
  await input('username', 'test-user'); await input('password', 'synthetic-password-123'); await click(button('Prihlásiť sa'));
  assert.equal(document.querySelector('[data-location]').textContent, privatePath, 'Login preserves the requested deep link');
  assert.equal(privateMounts, 1);
  await act(async () => access.setHubUser(null));
  assert.equal(document.querySelector('[data-location]').textContent, '/login');
  assert(!document.querySelector('[data-private]'), 'Expired sessions remove private screens');
  await act(async () => guardedRoot.unmount());

  // The browser-wide transport also covers legacy raw fetch, Headers, Request,
  // URL objects and multipart uploads without ever sending Hub auth externally.
  const transportCalls = [];
  let releaseOld;
  window.fetch = async (input, init) => {
    transportCalls.push({ input, init });
    const path = typeof input === 'string' ? input : input.url || input.href;
    if (path.includes('/old-request')) return new Promise(resolve => { releaseOld = () => resolve(new Response('{}', { status: 401 })); });
    return new Response('{}', { status: path.includes('/expired') ? 401 : 200 });
  };
  global.sessionStorage = dom.window.sessionStorage;
  sessionStorage.setItem('biketrek_client_logs_v1', JSON.stringify([{ ts: Date.now(), dir: '!', level: 'error', method: 'GET',
    url: '/api/feed?token=old-log-secret', note: 'Failed /api/feed?token=old-log-secret' }]));
  const client = require('../src/api/client.ts');
  assert(!sessionStorage.getItem('biketrek_client_logs_v1').includes('old-log-secret'), 'Legacy persisted logs are scrubbed on load');
  client.installFetchTap();
  access.unlockHub('synthetic-transport-secret');
  await window.fetch('/api/suppliers/example/config?key=never-log-this', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: '{}' });
  let request = transportCalls.at(-1).init;
  assert.equal(request.headers.get('X-Hub-Request'), '1');
  assert.equal(request.headers.get('Authorization'), 'Bearer synthetic-transport-secret');
  assert.equal(request.headers.get('Content-Type'), 'application/json');
  assert.equal(request.credentials, 'same-origin');
  assert.equal(request.redirect, 'error', 'An API redirect cannot forward credentials or request bodies elsewhere');
  assert.equal(request.cache, 'no-store');
  const form = new FormData(); form.set('file', 'fixture');
  await window.fetch(new URL('/api/invoices/upload', window.location.href), { method: 'POST', body: form });
  assert.equal(transportCalls.at(-1).init.body, form);
  assert(!transportCalls.at(-1).init.headers.has('Content-Type'), 'Multipart boundary remains browser-owned');
  await window.fetch(new Request('https://hub.example.test/api/invoices/1', { method: 'DELETE', headers: { 'X-Request-ID': 'fixture' } }));
  assert.equal(transportCalls.at(-1).init.headers.get('X-Request-ID'), 'fixture');
  assert.equal(client.getClientLogs().at(-1).method, 'DELETE');
  for (const path of ['https://external.example.test/api/private', '//external.example.test/api/private', '/api-evil', '/api/../outside']) {
    await window.fetch(path, { method: 'POST', body: '{}' });
    assert.equal(transportCalls.at(-1).init.headers, undefined, 'Never attach Hub headers outside its API boundary');
    assert.equal(transportCalls.at(-1).init.credentials, undefined);
  }
  const count = calls.length;
  await assert.rejects(access.hubRequest('https://external.example.test/api/login', { password: 'synthetic' }), /Invalid Hub API destination/);
  assert.equal(calls.length, count, 'Shared Hub client refuses external targets before a network request');
  assert(!JSON.stringify(client.getClientLogs()).includes('never-log-this'));
  assert(!JSON.stringify(client.getClientLogs()).includes('synthetic-transport-secret'));
  const oldRequest = window.fetch('/api/old-request');
  access.setHubUser(account); releaseOld(); await oldRequest;
  assert.equal(access.hubUser(), account, 'A stale 401 cannot clear a newly established login');
  await window.fetch('/api/receiving/fixture/scan', { method: 'POST', body: '{}' });
  assert.equal(transportCalls.at(-1).init.headers.get('X-Hub-Request'), '1');
  assert.equal(transportCalls.at(-1).init.headers.get('Authorization'), null, 'Cookie sessions do not retain the old operator token');
  assert.equal(transportCalls.at(-1).init.credentials, 'same-origin');
  await window.fetch('/api/expired');
  assert(!access.hubUnlocked(), 'Raw fetch 401 expires shared authentication too');
  console.log('Accounts UI passed: persistent login, private-route gate, deep-link return, global CSRF/credentials, external-target isolation, upload and stale-response guards.');
})().catch(error => { console.error(error); process.exitCode = 1; root.unmount(); });
