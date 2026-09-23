import React, { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';
import { ClipboardList, Lock } from 'lucide-react';
import { Button } from '../components/ui/Button.new';
import { accessRevision, hubUnlocked, subscribeAccess, unlockHub } from '../api/access';
import { AuditClassification, fetchOrderAudit, OrderAudit } from '../api/orderAudit';
import './OrdersAuditPage.css';

const classifications: AuditClassification[] = ['mapped', 'identified', 'manual', 'non_stock', 'unresolved', 'conflict'];

export function OrdersAuditPage() {
  const { t, i18n } = useTranslation();
  const revision = useSyncExternalStore(subscribeAccess, accessRevision);
  const unlocked = hubUnlocked();
  const [token, setToken] = useState('');
  const [shop, setShop] = useState('biketrek');
  const [days, setDays] = useState(30);
  const [loaded, setLoaded] = useState<{ audit: OrderAudit; revision: number } | null>(null);
  const result = loaded?.revision === revision ? loaded.audit : null;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const requestId = useRef(0);
  const controller = useRef<AbortController | null>(null);

  function clearResult() {
    requestId.current += 1;
    controller.current?.abort();
    controller.current = null;
    setLoaded(null);
    setBusy(false);
  }
  useEffect(() => {
    clearResult();
    return () => { requestId.current += 1; controller.current?.abort(); };
  }, [revision]);

  async function load(page = 1) {
    if (!hubUnlocked()) return;
    controller.current?.abort();
    const currentController = new AbortController();
    controller.current = currentController;
    const id = ++requestId.current;
    const credentialRevision = accessRevision();
    const current = () => id === requestId.current && credentialRevision === accessRevision() && !currentController.signal.aborted;
    setBusy(true); setError(''); setLoaded(null);
    try {
      const data = await fetchOrderAudit(shop, days, page, currentController.signal);
      if (current()) setLoaded({ audit: data, revision: credentialRevision });
    } catch (e) {
      if (!current()) return;
      const code = (e as Error & { code?: string }).code || 'request_failed';
      if (code === 'hub_access_required' || code === 'hub_access_not_configured') {
        unlockHub('');
        setError(t(`orderAudit.errors.${code}`, { defaultValue: t('orderAudit.accessError') }));
      } else {
        setError(t(`orderAudit.errors.${code}`, { defaultValue: t('orderAudit.requestError') }));
      }
    } finally {
      if (current()) setBusy(false);
    }
  }
  const reason = (code: string) => t(`orderAudit.reasons.${code}`, { defaultValue: t('orderAudit.candidate.review') });
  const date = (value: string | null) => {
    if (!value) return t('orderAudit.unknown');
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? t('orderAudit.unknown') : parsed.toLocaleString(i18n.language);
  };

  return <div className="orders-audit">
    <header className="orders-audit-header">
      <div><h1><ClipboardList size={26} aria-hidden="true" />{t('orderAudit.title')}</h1><p>{t('orderAudit.subtitle')}</p></div>
      {unlocked && <Button variant="secondary" icon={<Lock size={16} />} onClick={() => { unlockHub(''); setToken(''); setError(''); }}>{t('orderAudit.lock')}</Button>}
    </header>
    <div className="orders-audit-notice">{t('orderAudit.readOnly')}</div>
    <p><Link to={`/orders/stock?shop=${encodeURIComponent(shop)}`}>{t('orderStock.open')}</Link></p>
    <p><Link to={`/orders/inbox?shop=${encodeURIComponent(shop)}`}>{t('orderCollection.open')}</Link></p>

    {!unlocked ? <form className="orders-audit-panel orders-audit-unlock" onSubmit={event => {
      event.preventDefault();
      if (!token.trim()) return;
      setError(''); unlockHub(token.trim()); setToken('');
    }}>
      <p>{t('orderAudit.tokenHelp')}</p>
      <label>{t('orderAudit.token')}<input type="password" autoComplete="off" value={token} onChange={event => setToken(event.target.value)} /></label>
      <Button type="submit" disabled={!token.trim()}>{t('orderAudit.unlock')}</Button>
    </form> : <>
      <form className="orders-audit-panel orders-audit-controls" onSubmit={event => { event.preventDefault(); load(); }}>
        <label>{t('orderAudit.shop')}<select value={shop} onChange={event => { clearResult(); setError(''); setShop(event.target.value); }}>
          <option value="biketrek">BIKETREK</option><option value="xtrek">xTrek</option>
        </select></label>
        <label>{t('orderAudit.period')}<select value={days} onChange={event => { clearResult(); setError(''); setDays(Number(event.target.value)); }}>
          {[7, 30, 90].map(value => <option key={value} value={value}>{t('orderAudit.periodDays', { count: value })}</option>)}
        </select></label>
        <Button type="submit" loading={busy}>{t(busy ? 'common.loading' : 'orderAudit.load')}</Button>
      </form>
      {!result && !busy && !error && <p className="orders-audit-muted">{t('orderAudit.prompt')}</p>}
      <div aria-live="polite" aria-busy={busy}>
        {result && <>
          <div className="orders-audit-summary">
            <div><strong>{result.summary.orders}</strong><span>{t('orderAudit.orders')}</span></div>
            {classifications.map(key => <div key={key}><strong>{result.summary[key]}</strong><span>{t(`orderAudit.classification.${key}`)}</span></div>)}
          </div>
          <p className="orders-audit-muted">{t('orderAudit.fetchedAt', { at: date(result.fetched_at) })}</p>
          <p className="orders-audit-muted">{t('orderAudit.manualHelp')} {t('orderAudit.candidateHelp')}</p>
          {!!result.warnings.length && <ul className="orders-audit-warnings">{result.warnings.map((warning, index) => <li key={index}>{reason(warning)}</li>)}</ul>}
          {!result.orders.length && <div className="orders-audit-panel">{t('orderAudit.empty')}</div>}
          {result.orders.map((order, index) => <details className="orders-audit-order" key={`${order.order_number}:${index}`}>
            <summary>
              <div className="orders-audit-order-title"><strong>{order.order_number || t('orderAudit.unknown')}</strong><span>{t(`orderAudit.origins.${order.origin}`, { defaultValue: order.origin || t('orderAudit.unknown') })} · {order.status_name || t('orderAudit.unknown')}</span></div>
              <span className={`orders-audit-badge orders-audit-${order.candidate}`}>{t(`orderAudit.candidate.${order.candidate}`)}</span>
              {!!order.warnings.length && order.candidate !== 'review' && <span className="orders-audit-badge orders-audit-review">{t('orderAudit.candidate.review')}</span>}
              <span className="orders-audit-muted">{order.lines.length} {t('orderAudit.lines')}</span>
              <span className="orders-audit-muted">{t('orderAudit.details')}</span>
            </summary>
            <div className="orders-audit-order-body">
              <Link to={`/orders/stock?shop=${encodeURIComponent(shop)}&order=${encodeURIComponent(order.order_number)}`}>{t('orderStock.openOrder')}</Link>
              <p>{t('orderAudit.createdAt')}: {date(order.created_at)} · {reason(order.candidate_reason)}</p>
              {!!order.warnings.length && <ul className="orders-audit-warnings">{order.warnings.map((warning, warningIndex) => <li key={warningIndex}>{reason(warning)}</li>)}</ul>}
              <div className="orders-audit-table-scroll"><table>
                <thead><tr><th scope="col">{t('orderAudit.lineTitle')}</th><th scope="col">{t('orderAudit.code')}</th><th scope="col">{t('orderAudit.quantity')}</th><th scope="col">{t('orderAudit.identity')}</th><th scope="col">{t('orderAudit.product')}</th><th scope="col">{t('orderAudit.reason')}</th></tr></thead>
                <tbody>{order.lines.map((line, lineIndex) => <tr key={`${line.line_key}:${lineIndex}`}>
                  <td>{line.title || t('orderAudit.unknown')}</td><td><code>{line.code || '—'}</code>{line.ean && <div className="orders-audit-muted">EAN: {line.ean}</div>}</td>
                  <td className="orders-audit-quantity">{line.quantity ?? '—'} {line.unit}</td>
                  <td><span className={`orders-audit-badge orders-audit-${line.classification}`}>{t(`orderAudit.classification.${line.classification}`)}</span></td>
                  <td><code>{line.sku || '—'}</code></td>
                  <td>{line.reasons.map((code, reasonIndex) => <div key={reasonIndex}>{reason(code)}</div>)}</td>
                </tr>)}</tbody>
              </table></div>
            </div>
          </details>)}
          <nav className="orders-audit-pagination" aria-label={t('orderAudit.title')}>
            <Button variant="secondary" disabled={busy || result.page <= 1} onClick={() => load(result.page - 1)}>{t('orderAudit.previous')}</Button>
            <span>{t('orderAudit.page', { page: result.page, pages: Math.max(1, result.number_of_pages) })}</span>
            <Button variant="secondary" disabled={busy || !result.has_more || result.page >= 1000} onClick={() => load(result.page + 1)}>{t('orderAudit.next')}</Button>
          </nav>
        </>}
      </div>
    </>}
    {error && <div className="orders-audit-notice orders-audit-error" role="alert">{error}</div>}
  </div>;
}
