import React, { useEffect, useState, useSyncExternalStore } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { accessRevision, HubUser, hubRequest, hubUser, logoutHub, setHubUser, subscribeAccess, unlockHub } from '../api/access';
import './AiContentPage.css';

export function AccountPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  useSyncExternalStore(subscribeAccess, accessRevision);
  const user = hubUser();
  const [tokenMode, setTokenMode] = useState(false);
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [role, setRole] = useState('operator');
  const [users, setUsers] = useState<HubUser[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  async function execute(action: () => Promise<void>) {
    setBusy(true); setError(''); setNotice('');
    try { await action(); } catch (e) { setError(t(`accounts.errors.${(e as {code?: string}).code}`, {defaultValue: t('accounts.requestFailed')})); }
    finally { setBusy(false); }
  }
  async function loadUsers() { setUsers(await hubRequest<HubUser[]>('/api/auth/users')); }
  useEffect(() => { if (user?.role === 'admin') void execute(loadUsers); }, [user?.id, user?.role]);
  async function signIn(event: React.FormEvent) {
    event.preventDefault();
    await execute(async () => {
      let result: {user: HubUser};
      if (tokenMode) {
        unlockHub(password);
        try { result = await hubRequest('/api/auth/token-session', {}); }
        finally { unlockHub(''); }
      } else result = await hubRequest('/api/auth/login', { username: username.trim(), password });
      setPassword(''); setHubUser(result.user); navigate('/settings/ai-content');
    });
  }
  async function create(event: React.FormEvent) {
    event.preventDefault();
    await execute(async () => {
      await hubRequest('/api/auth/users', {username: username.trim(), display_name: displayName.trim(), password, role});
      setUsername(''); setPassword(''); setDisplayName(''); await loadUsers(); setNotice(t('accounts.created'));
    });
  }
  return <div className="ai-content"><header><h1>{t(user ? 'accounts.title' : 'accounts.login')}</h1><Link to="/settings">{t('accounts.back')}</Link></header>
    {error && <p className="ai-notice ai-error" role="alert">{error}</p>}{notice && <p className="ai-notice" role="status">{notice}</p>}
    {!user ? <form className="ai-card" onSubmit={signIn}><p>{t('accounts.remember')}</p>
      {!tokenMode && <label>{t('accounts.username')}<input name="username" autoComplete="username" required value={username} onChange={e => setUsername(e.target.value)} /></label>}
      <label>{t(tokenMode ? 'ai.hubAccessToken' : 'accounts.password')}<input name="password" type="password" autoComplete={tokenMode ? 'off' : 'current-password'} required value={password} onChange={e => setPassword(e.target.value)} /></label>
      <div className="ai-actions"><button className="ai-primary" disabled={busy || !password}>{t('accounts.login')}</button>
        <button type="button" disabled={busy} onClick={() => { setTokenMode(!tokenMode); setPassword(''); setError(''); }}>{t(tokenMode ? 'accounts.usePassword' : 'accounts.useToken')}</button></div>
    </form> : <>
      <div className="ai-card"><h2>{user.display_name}</h2><p>{t(`accounts.roles.${user.role}`)}</p><p>{t('accounts.remember')}</p>
        <button disabled={busy} onClick={() => execute(async () => { await logoutHub(); setUsers([]); })}>{t('accounts.logout')}</button></div>
      {user.role === 'admin' && <><form className="ai-card" onSubmit={create}><h2>{t('accounts.create')}</h2><p>{t('accounts.createHelp')}</p>
        <div className="ai-grid"><label>{t('accounts.username')}<input name="new-username" autoComplete="off" pattern="[a-zA-Z0-9_.@-]+" maxLength={80} required value={username} onChange={e => setUsername(e.target.value)} /></label>
          <label>{t('accounts.displayName')}<input maxLength={120} required value={displayName} onChange={e => setDisplayName(e.target.value)} /></label>
          <label>{t('accounts.password')}<input name="new-password" type="password" autoComplete="new-password" minLength={12} maxLength={200} required value={password} onChange={e => setPassword(e.target.value)} /></label>
          <label>{t('accounts.role')}<select value={role} onChange={e => setRole(e.target.value)}><option value="operator">{t('accounts.roles.operator')}</option><option value="admin">{t('accounts.roles.admin')}</option></select></label></div>
        <button className="ai-primary" disabled={busy || !password || !username.trim() || !displayName.trim()}>{t('accounts.create')}</button></form>
        <div className="ai-card"><h2>{t('accounts.users')}</h2>{!users.length && <p>{t('accounts.noUsers')}</p>}{users.map(row => <div className="ai-row" key={row.id}><span className="ai-grow">{row.display_name} · {row.username} · {t(`accounts.roles.${row.role}`)}</span><button disabled={busy || row.id === user.id} onClick={() => execute(async () => { await hubRequest(`/api/auth/users/${row.id}`, {active: !row.active}, undefined, 'PUT'); await loadUsers(); })}>{t(row.active ? 'accounts.disable' : 'accounts.enable')}</button></div>)}</div>
      </>}
    </>}
  </div>;
}
