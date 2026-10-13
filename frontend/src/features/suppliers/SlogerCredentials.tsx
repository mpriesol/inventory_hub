import React, { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { accessRevision, hubUnlocked, subscribeAccess } from '../../api/access';
import { readSlogerCredentials, saveSlogerCredentials, type SlogerCredentials } from '../../api/slogerProbe';
import '../../pages/AiContentPage.css';

export function SlogerCredentials() {
  const { t } = useTranslation();
  const revision = useSyncExternalStore(subscribeAccess, accessRevision, accessRevision);
  const [username, setUsername] = useState('info@xtrek.sk');
  const [password, setPassword] = useState('');
  const [credentials, setCredentials] = useState<SlogerCredentials | null>(null);
  const [credentialsBusy, setCredentialsBusy] = useState<'loading' | 'saving' | null>(null);
  const [credentialsError, setCredentialsError] = useState('');
  const [editingCredentials, setEditingCredentials] = useState(false);
  const credentialsFlight = useRef(false);
  const credentialsSequence = useRef(0);
  const mounted = useRef(true);
  const current = (started: number) => mounted.current && accessRevision() === started;

  async function loadCredentials() {
    if (credentialsFlight.current || !hubUnlocked()) return;
    credentialsFlight.current = true;
    const sequence = ++credentialsSequence.current;
    const started = accessRevision();
    setCredentialsBusy('loading'); setCredentialsError(''); setCredentials(null); setPassword(''); setEditingCredentials(false);
    try {
      const result = await readSlogerCredentials();
      if (current(started) && sequence === credentialsSequence.current) {
        setCredentials(result);
        setUsername(result.username || 'info@xtrek.sk');
      }
    } catch {
      if (current(started) && sequence === credentialsSequence.current) setCredentialsError(t('slogerProbe.credentialsLoadFailed'));
    } finally {
      if (sequence === credentialsSequence.current) {
        credentialsFlight.current = false;
        if (mounted.current) setCredentialsBusy(null);
      }
    }
  }

  async function saveCredentials(event: React.FormEvent) {
    event.preventDefault();
    if (credentialsFlight.current || !hubUnlocked() || !username.trim() || !password) return;
    const body = { username: username.trim(), password };
    const sequence = ++credentialsSequence.current;
    const started = accessRevision();
    credentialsFlight.current = true;
    setPassword(''); setCredentials(null); setCredentialsBusy('saving'); setCredentialsError('');
    try {
      const result = await saveSlogerCredentials(body);
      if (current(started) && sequence === credentialsSequence.current) {
        setCredentials(result); setUsername(result.username || 'info@xtrek.sk'); setEditingCredentials(false);
      }
    } catch {
      if (current(started) && sequence === credentialsSequence.current) setCredentialsError(t('slogerProbe.credentialsSaveFailed'));
    } finally {
      body.password = '';
      if (sequence === credentialsSequence.current) {
        credentialsFlight.current = false;
        if (mounted.current) { setPassword(''); setCredentialsBusy(null); }
      }
    }
  }

  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => {
    setPassword(''); credentialsSequence.current += 1; credentialsFlight.current = false;
    setCredentials(null); setCredentialsBusy(null); setCredentialsError(''); setEditingCredentials(false);
    if (hubUnlocked()) void loadCredentials();
  }, [revision]);

  if (!hubUnlocked()) return <p><Link to="/login">{t('slogerProbe.signIn')}</Link></p>;
  return <div className="ai-content" style={{ padding: 0, marginTop: 24, maxWidth: 672 }}>
    <section className="ai-card" data-testid="sloger-credentials-settings">
        <h2>{t('slogerProbe.credentialsTitle')}</h2>
        {credentialsBusy && <p role="status" data-testid="sloger-credentials-pending">{t(credentialsBusy === 'saving' ? 'slogerProbe.credentialsSaving' : 'slogerProbe.credentialsLoading')}</p>}
        {credentialsError && <><p className="ai-notice ai-error" role="alert" data-testid="sloger-credentials-error">{credentialsError}</p>
          <button data-testid="sloger-credentials-reload" type="button" onClick={() => void loadCredentials()}>{t('slogerProbe.credentialsReload')}</button></>}
        {credentials && !credentialsBusy && !credentialsError && <>
          {credentials.configured && <p role="status" data-testid="sloger-credentials-status">{t('slogerProbe.credentialsConfigured', { username: credentials.username })}</p>}
          {credentials.configured && !editingCredentials && <button data-testid="sloger-credentials-change" type="button" onClick={() => { setPassword(''); setEditingCredentials(true); }}>{t('slogerProbe.credentialsChange')}</button>}
          {(!credentials.configured || editingCredentials) && <form onSubmit={saveCredentials} autoComplete="off">
            <div className="ai-grid">
              <label>{t('slogerProbe.username')}<input data-testid="sloger-username" value={username} autoComplete="off" maxLength={200} required onChange={event => setUsername(event.target.value)} /></label>
              <label>{t('slogerProbe.password')}<input data-testid="sloger-password" type="password" value={password} autoComplete="off" maxLength={500} required onChange={event => setPassword(event.target.value)} /></label>
            </div>
            <div className="ai-actions">
              <button data-testid="sloger-credentials-save" className="ai-primary" type="submit" disabled={!username.trim() || !password}>{t('slogerProbe.credentialsSave')}</button>
              {credentials.configured && <button data-testid="sloger-credentials-cancel" type="button" onClick={() => { setPassword(''); setUsername(credentials.username || 'info@xtrek.sk'); setEditingCredentials(false); }}>{t('slogerProbe.credentialsCancel')}</button>}
            </div>
          </form>}
        </>}
        <p>{t('slogerProbe.credentialsHelp')}</p>
      </section>
  </div>;
}
