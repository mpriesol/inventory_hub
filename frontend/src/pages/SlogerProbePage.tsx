import React, { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { accessRevision, hubUnlocked, subscribeAccess } from '../api/access';
import { CatalogApiError } from '../api/catalog';
import { readSlogerCredentials, saveSlogerCredentials, readSlogerProbe, startSlogerProbe, type SlogerCredentials, type SlogerProbe, type SlogerQuantity } from '../api/slogerProbe';
import './AiContentPage.css';

const knownRejections = new Set([
  'sloger_probe_request_invalid', 'ai_not_configured', 'ai_job_budget', 'ai_monthly_budget',
  'hub_access_required', 'hub_access_not_configured', 'sloger_credentials_not_configured', 'sloger_credentials_unavailable',
]);

export function SlogerProbePage() {
  const { t, i18n } = useTranslation();
  const [params, setParams] = useSearchParams();
  const revision = useSyncExternalStore(subscribeAccess, accessRevision, accessRevision);
  const initialId = /^[0-9a-f]{32}$/.test(params.get('request') || '') ? params.get('request')! : '';
  const [requestId, setRequestId] = useState(initialId);
  const idRef = useRef(initialId);
  const [username, setUsername] = useState('info@xtrek.sk');
  const [password, setPassword] = useState('');
  const [credentials, setCredentials] = useState<SlogerCredentials | null>(null);
  const [credentialsBusy, setCredentialsBusy] = useState<'loading' | 'saving' | null>(null);
  const [credentialsError, setCredentialsError] = useState('');
  const [editingCredentials, setEditingCredentials] = useState(false);
  const credentialsFlight = useRef(false);
  const credentialsSequence = useRef(0);
  const [sku, setSku] = useState('7700860');
  const [data, setData] = useState<SlogerProbe | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [canReset, setCanReset] = useState(false);
  const inFlight = useRef(false);
  const mounted = useRef(true);
  const unknown = t('slogerProbe.unknown');
  const errorLabel = (code: string) => t(`slogerProbe.errors.${code}`, { defaultValue: t('slogerProbe.errors.generic') });
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

  function accept(result: SlogerProbe) {
    setData(result);
    setCanReset(['lookup_completed', 'lookup_failed'].includes(result.status));
    if (result.result) setSku(result.result.sku);
  }

  async function recover() {
    if (inFlight.current || !idRef.current || !hubUnlocked()) return;
    inFlight.current = true;
    const started = accessRevision();
    setBusy(true); setError('');
    try {
      const result = await readSlogerProbe(idRef.current);
      if (current(started)) accept(result);
    } catch (reason) {
      if (current(started)) setError(errorLabel(reason instanceof CatalogApiError ? reason.code : 'generic'));
    } finally {
      inFlight.current = false;
      if (mounted.current) setBusy(false);
    }
  }

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);
  useEffect(() => {
    setPassword(''); setData(null); setError(''); setCanReset(false);
    credentialsSequence.current += 1; credentialsFlight.current = false;
    setCredentials(null); setCredentialsBusy(null); setCredentialsError(''); setEditingCredentials(false);
    if (hubUnlocked()) void loadCredentials();
    if (hubUnlocked() && idRef.current) void recover();
  }, [revision]);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (inFlight.current || idRef.current || !hubUnlocked() || !credentials?.configured || credentialsFlight.current || credentialsError || editingCredentials || !sku.trim()) return;
    const uuid = crypto.randomUUID();
    const id = uuid.replace(/-/g, '');
    const body = { request_id: uuid, sku: sku.trim() };
    const started = accessRevision();
    inFlight.current = true;
    idRef.current = id;
    setRequestId(id); setParams({ request: id }, { replace: true });
    setPassword(''); setData(null); setError(''); setCanReset(false); setBusy(true);
    try {
      const result = await startSlogerProbe(body);
      if (current(started)) accept(result);
    } catch (reason) {
      if (current(started)) {
        const code = reason instanceof CatalogApiError ? reason.code : 'generic';
        setError(errorLabel(code));
        setCanReset(knownRejections.has(code));
      }
    } finally {
      inFlight.current = false;
      if (mounted.current) { setPassword(''); setBusy(false); }
    }
  }

  function reset() {
    if (inFlight.current || !canReset) return;
    idRef.current = '';
    setRequestId(''); setParams({}, { replace: true }); setData(null); setError(''); setPassword(''); setCanReset(false);
    void loadCredentials();
  }

  function quantity(value: SlogerQuantity) {
    if (value.quantity === null) return unknown;
    const number = Number(value.quantity).toLocaleString(i18n.language, { maximumFractionDigits: 3 });
    if (value.quantity_relation === 'greater_than') return `> ${number}`;
    if (value.quantity_relation === 'at_least') return `≥ ${number}`;
    if (value.quantity_relation === 'equal') return number;
    return `${number} (${t('slogerProbe.quantityUnknown')})`;
  }
  const facts = data?.result;
  const price = (value: string | null, basis: string) => value === null ? unknown :
    `${Number(value).toLocaleString(i18n.language, { minimumFractionDigits: 2, maximumFractionDigits: 4 })} ${facts?.currency || unknown} · ${t(`slogerProbe.basis.${basis}`)}`;

  return <main className="ai-content" style={{ maxWidth: 1000 }}>
    <header><h1>{t('slogerProbe.title')}</h1><Link to="/suppliers">{t('slogerProbe.back')}</Link></header>
    <p>{t('slogerProbe.description')}</p>
    {!hubUnlocked() ? <p className="ai-notice"><Link to="/login">{t('slogerProbe.signIn')}</Link></p> : <>
      {!requestId && <section className="ai-card">
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
      </section>}
      {!requestId && <form className="ai-card" onSubmit={submit} autoComplete="off">
        <div className="ai-grid">
          <label>{t('slogerProbe.sku')}<input data-testid="sloger-sku" value={sku} autoComplete="off" maxLength={100} required onChange={event => setSku(event.target.value)} /></label>
        </div>
        <div className="ai-actions"><button data-testid="sloger-start" className="ai-primary" type="submit" disabled={busy || !credentials?.configured || !!credentialsBusy || !!credentialsError || editingCredentials || !sku.trim()}>{t('slogerProbe.start')}</button></div>
      </form>}
      {error && <p className="ai-notice ai-error" role="alert">{error}</p>}
      {requestId && <section className="ai-card">
        <p role="status" data-testid="sloger-status">{t(`slogerProbe.states.${data?.status || (busy ? 'lookup_running' : canReset ? 'lookup_failed' : 'lookup_uncertain')}`)}</p>
        <p>{t('slogerProbe.request')} <code data-testid="sloger-request-id">{requestId}</code></p>
        {!canReset && <p>{t('slogerProbe.recoveryHelp')}</p>}
        {data?.error && <p className="ai-notice ai-error" role="alert">{errorLabel(data.error)}</p>}
        <div className="ai-actions">
          <button data-testid="sloger-refresh" type="button" disabled={busy} onClick={() => void recover()}>{t(busy ? 'slogerProbe.loading' : 'slogerProbe.refresh')}</button>
          {canReset && <button data-testid="sloger-new" type="button" disabled={busy} onClick={reset}>{t('slogerProbe.newLookup')}</button>}
        </div>
        {data && <p>{t('slogerProbe.model')}: {data.model} · {t('slogerProbe.cost')}: {data.actual_usd === null ? unknown : `${data.actual_usd} USD`}
          {data.usage.input_tokens !== undefined && <> · {t('slogerProbe.tokens')}: {data.usage.input_tokens} / {data.usage.output_tokens ?? 0}</>}</p>}
      </section>}
      {facts && <section className="ai-card" data-testid="sloger-result">
        <h2>{facts.product_name || facts.sku}</h2>
        <div className="ai-scroll"><table><tbody>
          <tr><th>{t('slogerProbe.sku')}</th><td>{facts.sku}</td></tr>
          <tr><th>{t('slogerProbe.manufacturerCode')}</th><td>{facts.manufacturer_code || unknown}</td></tr>
          <tr><th>EAN</th><td>{facts.eans.length ? facts.eans.join(', ') : unknown}</td></tr>
          <tr><th>{t('slogerProbe.purchasePrice')}</th><td>{price(facts.purchase_price, facts.price_basis)}</td></tr>
          <tr><th>{t('slogerProbe.retailPrice')}</th><td>{price(facts.retail_price, facts.retail_price_basis)}</td></tr>
          <tr><th>{t('slogerProbe.ownAvailability')}</th><td>{facts.availability || unknown}</td></tr>
          <tr><th>{t('slogerProbe.available')}</th><td>{facts.available === null ? unknown : t(facts.available ? 'slogerProbe.yes' : 'slogerProbe.no')}</td></tr>
          <tr><th>{t('slogerProbe.ownQuantity')}</th><td data-testid="sloger-own-quantity">{quantity(facts)}</td></tr>
          <tr><th>{t('slogerProbe.observed')}</th><td>{new Date(facts.observed_at).toLocaleString(i18n.language)}</td></tr>
          <tr><th>{t('slogerProbe.source')}</th><td><a href={facts.source_url} target="_blank" rel="noreferrer">{facts.source_url}</a></td></tr>
        </tbody></table></div>
        {!!facts.availability_tiers.length && <><h3>{t('slogerProbe.tiers')}</h3><div className="ai-scroll"><table data-testid="sloger-tiers"><thead><tr><th>{t('slogerProbe.tier')}</th><th>{t('slogerProbe.quantity')}</th><th>{t('slogerProbe.leadDays')}</th></tr></thead><tbody>
          {facts.availability_tiers.map((tier, index) => <tr key={index}><td>{tier.label}</td><td>{quantity(tier)}</td><td>{tier.lead_days === null ? unknown : tier.lead_days}</td></tr>)}
        </tbody></table></div></>}
        <p>{t('slogerProbe.stockHelp')}</p>
      </section>}
    </>}
  </main>;
}
