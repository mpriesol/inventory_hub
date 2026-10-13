import React, { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { accessRevision, hubUnlocked, subscribeAccess } from '../api/access';
import { CatalogApiError } from '../api/catalog';
import { readSlogerProbe, type SlogerProbe, type SlogerQuantity } from '../api/slogerProbe';
import './AiContentPage.css';

/** Historical probe links remain readable; ordinary work uses supplier settings and import. */
export function SlogerProbePage() {
  const { t, i18n } = useTranslation();
  const [params] = useSearchParams();
  const requestId = /^[0-9a-f]{32}$/.test(params.get('request') || '') ? params.get('request')! : '';
  const revision = useSyncExternalStore(subscribeAccess, accessRevision, accessRevision);
  const [data, setData] = useState<SlogerProbe | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const sequence = useRef(0), inFlight = useRef(false), mounted = useRef(true);
  const unknown = t('slogerProbe.unknown');
  const errorLabel = (code: string) => t(`slogerProbe.errors.${code}`, { defaultValue: t('slogerProbe.errors.generic') });
  async function recover() {
    if (inFlight.current || !requestId || !hubUnlocked()) return;
    const started = accessRevision(), operation = ++sequence.current;
    const current = () => mounted.current && operation === sequence.current && started === accessRevision();
    inFlight.current = true; setBusy(true); setError('');
    try { const result = await readSlogerProbe(requestId); if (current()) setData(result); }
    catch (reason) { if (current()) setError(errorLabel(reason instanceof CatalogApiError ? reason.code : 'generic')); }
    finally { if (current()) { inFlight.current = false; setBusy(false); } }
  }
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => {
    sequence.current++; inFlight.current = false; setBusy(false); setData(null); setError('');
    if (hubUnlocked() && requestId) void recover();
  }, [revision, requestId]);

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
    <header><h1>{t('slogerProbe.archiveTitle')}</h1><Link to="/suppliers?edit=sloger">{t('slogerProbe.settingsLink')}</Link></header>
    <p>{t('slogerProbe.archiveHelp')}</p>
    {!hubUnlocked() ? <p className="ai-notice"><Link to="/login">{t('slogerProbe.signIn')}</Link></p> : <>
      {error && <p className="ai-notice ai-error" role="alert">{error}</p>}
      {requestId && <section className="ai-card">
        <p role="status" data-testid="sloger-status">{t(`slogerProbe.states.${data?.status || (busy ? 'lookup_running' : 'lookup_uncertain')}`)}</p>
        <p>{t('slogerProbe.request')} <code data-testid="sloger-request-id">{requestId}</code></p>
        <p>{t('slogerProbe.recoveryHelp')}</p>
        {data?.error && <p className="ai-notice ai-error" role="alert">{errorLabel(data.error)}</p>}
        <button data-testid="sloger-refresh" type="button" disabled={busy} onClick={() => void recover()}>{t(busy ? 'slogerProbe.loading' : 'slogerProbe.refresh')}</button>
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
