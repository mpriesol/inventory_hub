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
const { MemoryRouter } = require('react-router-dom');
const { AccountPage } = require('../src/pages/AccountPage.tsx');
const { SessionBootstrap } = require('../src/components/SessionBootstrap.tsx');
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
  console.log('Accounts UI passed: login, shared unlock, refresh, stable editor session, logout and stale-response guard.');
})().catch(error => { console.error(error); process.exitCode = 1; root.unmount(); });
