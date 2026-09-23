import React, { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Button } from '../components/ui/Button.new';
import { accessRevision, hubUnlocked, subscribeAccess, unlockHub } from '../api/access';
import { CollectionRun, CollectionStockPreview, configureCollection, getCollectionRuns, getCollectionStatus,
  getOrderInbox, OrderCollectionStatus, OrderInbox, previewCollectionStock, refreshCollection } from '../api/orderCollection';
import './OpeningStockPage.css';
import './OrdersInboxPage.css';

type Operation = 'load' | 'inbox' | 'configure' | 'refresh' | 'stock' | null;
interface View { revision: number; shop: string; status: OrderCollectionStatus; inbox: OrderInbox; runs: CollectionRun[] }

export function OrdersInboxPage() {
  const { t, i18n } = useTranslation();
  const [query] = useSearchParams();
  const revision = useSyncExternalStore(subscribeAccess, accessRevision);
  const [shop, setShop] = useState(query.get('shop') === 'xtrek' ? 'xtrek' : 'biketrek');
  const [token, setToken] = useState('');
  const [loaded, setLoaded] = useState<View | null>(null);
  const view = loaded?.revision === revision && loaded.shop === shop ? loaded : null;
  const [confirmed, setConfirmed] = useState(false);
  const [skuText, setSkuText] = useState('');
  const [stockLoaded, setStockLoaded] = useState<{ value: CollectionStockPreview; revision: number; shop: string } | null>(null);
  const stock = stockLoaded?.revision === revision && stockLoaded.shop === shop ? stockLoaded.value : null;
  const [busy, setBusy] = useState<Operation>(null);
  const [error, setError] = useState('');
  const [uncertain, setUncertain] = useState(false);
  const [refreshQueued, setRefreshQueued] = useState(false);
  const generation = useRef(0);
  const stockGeneration = useRef(0);
  const controller = useRef<AbortController | null>(null);
  const busyRef = useRef<Operation>(null);
  const mutation = useRef(false);
  const writing = busy === 'configure' || busy === 'refresh';

  function reset() {
    generation.current += 1; stockGeneration.current += 1; controller.current?.abort(); controller.current = null;
    setLoaded(null); setStockLoaded(null); setSkuText(''); setConfirmed(false); setError(''); setUncertain(false);
    setRefreshQueued(false); setBusy(null); busyRef.current = null;
  }
  useEffect(() => { reset(); setToken(''); return () => { generation.current += 1; stockGeneration.current += 1; controller.current?.abort(); }; }, [revision]);
  function report(value: unknown) {
    const code = (value as Error & { code?: string }).code || 'request_failed';
    if (code === 'hub_access_required' || code === 'hub_access_not_configured') unlockHub('');
    setError(code);
  }
  async function read<T>(operation: Operation, request: (signal: AbortSignal) => Promise<T>, accept: (value: T) => void) {
    if (busyRef.current || !hubUnlocked()) return;
    const id = generation.current, credential = accessRevision(), skuRevision = stockGeneration.current;
    const abort = new AbortController(); controller.current = abort; busyRef.current = operation; setBusy(operation); setError('');
    const current = () => id === generation.current && credential === accessRevision() && !abort.signal.aborted
      && (operation !== 'stock' || skuRevision === stockGeneration.current);
    try { const value = await request(abort.signal); if (current()) accept(value); }
    catch (value) { if (current()) report(value); }
    finally { if (current()) { busyRef.current = null; setBusy(null); } }
  }
  function load() {
    if (busyRef.current) return;
    setLoaded(null); setConfirmed(false);
    read('load', signal => Promise.all([getCollectionStatus(shop, signal), getOrderInbox(shop, 0, signal), getCollectionRuns(shop, signal)]),
      ([status, inbox, runs]) => { setLoaded({ revision: accessRevision(), shop, status, inbox, runs: runs.runs }); setUncertain(false); setRefreshQueued(false); });
  }
  function page(offset: number) {
    read('inbox', signal => getOrderInbox(shop, offset, signal), inbox => setLoaded(previous => previous ? { ...previous, inbox } : null));
  }
  async function changeCollection(operation: 'configure' | 'refresh', enabled?: boolean) {
    if (!view || busyRef.current || mutation.current || uncertain) return;
    if (operation === 'configure' && enabled && (!confirmed || !view.status.policy || !view.status.connection_configured || view.status.connection_matches === false)) return;
    if (operation === 'refresh' && (!view.status.collector?.enabled || view.status.connection_matches === false)) return;
    const id = generation.current, credential = accessRevision();
    const expected = view.status.collector?.revision ?? null;
    mutation.current = true; busyRef.current = operation; setBusy(operation); setError(''); setRefreshQueued(false);
    const current = () => id === generation.current && credential === accessRevision();
    try {
      const status = operation === 'configure'
        ? await configureCollection({ shop_code: shop, enabled: !!enabled, expected_revision: expected, confirmed: true })
        : await refreshCollection({ shop_code: shop, expected_revision: expected!, confirmed: true });
      if (current()) { setLoaded(previous => previous ? { ...previous, status } : null); setConfirmed(false); setRefreshQueued(operation === 'refresh'); }
    } catch (value) { if (current()) { setUncertain(true); setConfirmed(false); report(value); } }
    finally { mutation.current = false; if (current()) { busyRef.current = null; setBusy(null); } }
  }
  function changeSkus(value: string) {
    stockGeneration.current += 1; setStockLoaded(null); setSkuText(value); setError('');
    if (busyRef.current === 'stock') { controller.current?.abort(); busyRef.current = null; setBusy(null); }
  }
  function stockPreview() {
    const skus = skuText.split(/\r?\n/).map(value => value.trim()).filter(Boolean);
    if (!skus.length || skus.length > 100 || new Set(skus).size !== skus.length || skus.some(sku => sku.length > 100)) { setError('invalid_skus'); return; }
    setStockLoaded(null);
    read('stock', signal => previewCollectionStock(shop, skus, signal), value => setStockLoaded({ value, revision: accessRevision(), shop }));
  }
  const message = (code: string) => t(`orderCollection.errors.${code}`, { defaultValue: t('orderCollection.errors.request_failed') });
  const date = (value: string | null | undefined) => value ? new Date(value).toLocaleString(i18n.language) : '—';
  const state = (value: string | null) => t(`orderCollection.stockStates.${value || 'none'}`, { defaultValue: t('orderCollection.stockStates.unknown') });

  return <div className="opening-stock orders-inbox">
    <Link className="opening-stock-back" to="/orders">← {t('orderCollection.back')}</Link>
    <header><div><h1>{t('orderCollection.title')}</h1><p>{t('orderCollection.subtitle')}</p></div>{hubUnlocked() && <Button variant="secondary" onClick={() => { unlockHub(''); setError(''); }}>{t('orderCollection.lock')}</Button>}</header>
    <div className="opening-stock-notice">{t('orderCollection.scope')}</div>
    {!hubUnlocked() ? <form className="opening-stock-panel opening-stock-unlock" onSubmit={event => { event.preventDefault(); if (token.trim()) { setError(''); unlockHub(token.trim()); setToken(''); } }}>
      <p>{t('orderCollection.tokenHelp')}</p><label>{t('orderCollection.token')}<input type="password" autoComplete="off" value={token} onChange={event => setToken(event.target.value)} /></label>
      <Button type="submit" disabled={!token.trim()}>{t('orderCollection.unlock')}</Button>
    </form> : <>
      <section className="opening-stock-panel">
        <div className="opening-stock-actions"><label>{t('orderCollection.shop')}<select value={shop} disabled={writing} onChange={event => { reset(); setShop(event.target.value); }}><option value="biketrek">BIKETREK</option><option value="xtrek">xTrek</option></select></label>
          <Button variant="secondary" disabled={!!busy} onClick={load}>{t('orderCollection.load')}</Button></div>
        {uncertain && <div className="opening-stock-notice" role="alert">{t('orderCollection.uncertain')}</div>}
        {refreshQueued && <div className="opening-stock-notice" role="status">{t('orderCollection.refreshQueued')}</div>}
        {view && <>
          <div className="orders-inbox-status"><strong>{t(view.status.collector?.enabled ? 'orderCollection.enabled' : 'orderCollection.paused')}</strong><span>{t('orderCollection.seen', { count: view.status.collector?.entries_seen ?? 0 })}</span></div>
          <p className="opening-stock-muted">{t('orderCollection.lastCompleted')}: {date(view.status.collector?.last_completed_at)} · {t('orderCollection.nextCheck')}: {date(view.status.collector?.next_poll_at)}</p>
          {view.status.collector?.last_error && <div className="opening-stock-errors" role="alert">{message(view.status.collector.last_error)}</div>}
          {!view.status.connection_configured && <p className="opening-stock-errors">{t('orderCollection.connectionMissing')}</p>}
          {view.status.connection_matches === false && <p className="opening-stock-errors">{t('orderCollection.connectionChanged')}</p>}
          {!view.status.policy ? <p><Link to={`/orders/stock?shop=${encodeURIComponent(shop)}`}>{t('orderCollection.setupPolicy')}</Link></p> : <p>{t('orderCollection.since', { at: date(view.status.policy.starts_at), warehouse: view.status.policy.warehouse_code })}</p>}
          <p className="opening-stock-muted">{t('orderCollection.backgroundHelp')}</p>
          <div className="opening-stock-confirmations">{!view.status.collector?.enabled && <label><input type="checkbox" checked={confirmed} disabled={!!busy || uncertain} onChange={event => setConfirmed(event.target.checked)} />{t('orderCollection.enableConfirm')}</label>}
            <div className="opening-stock-actions">{view.status.collector?.enabled ? <Button variant="secondary" disabled={!!busy || uncertain} onClick={() => changeCollection('configure', false)}>{t('orderCollection.pause')}</Button>
              : <Button disabled={!!busy || uncertain || !confirmed || !view.status.policy || !view.status.connection_configured || view.status.connection_matches === false} onClick={() => changeCollection('configure', true)}>{t('orderCollection.enable')}</Button>}
              <Button variant="secondary" disabled={!!busy || uncertain || !view.status.collector?.enabled || view.status.connection_matches === false} onClick={() => changeCollection('refresh')}>{t('orderCollection.fetchNow')}</Button></div>
          </div>
        </>}
      </section>
      {view && <>
        <section className="opening-stock-panel"><h2>{t('orderCollection.inbox')}</h2><p className="opening-stock-muted">{t('orderCollection.inboxHelp')}</p>
          {!view.inbox.entries.length ? <p>{t('orderCollection.noOrders')}</p> : <div className="opening-stock-table-scroll"><table><thead><tr>{['orderNumber', 'origin', 'statusId', 'updatedAt', 'observedAt', 'review', 'stockState', 'action'].map(key => <th key={key}>{t(`orderCollection.${key}`)}</th>)}</tr></thead><tbody>{view.inbox.entries.map(entry => <tr key={entry.id}>
            <td>{entry.order_number}</td><td>{t(`orderCollection.origins.${entry.origin}`, { defaultValue: entry.origin || '—' })}</td><td>{entry.status_id ?? t('orderCollection.unknown')}</td><td>{date(entry.updated_at)}</td><td>{date(entry.last_seen_at)}</td>
            <td>{entry.deleted ? t('orderCollection.deleted') : entry.review_reason !== null ? t(`orderCollection.reviewReasons.${entry.review_reason}`, { defaultValue: t('orderCollection.needsReview') }) : !entry.source_uuid ? t('orderCollection.needsReview') : t('orderCollection.readyForReview')}</td>
            <td>{state(entry.stock_state)}{entry.stock_issued_at && <small className="orders-inbox-subline">{date(entry.stock_issued_at)}</small>}</td>
            <td>{!entry.deleted && entry.review_reason === null && entry.source_uuid && entry.order_number && <Link to={`/orders/stock?shop=${encodeURIComponent(shop)}&order=${encodeURIComponent(entry.order_number)}`}>{t('orderCollection.openStock')}</Link>}</td>
          </tr>)}</tbody></table></div>}
          <div className="opening-stock-pagination"><Button variant="secondary" size="sm" disabled={!!busy || view.inbox.offset === 0} onClick={() => page(Math.max(0, view.inbox.offset - view.inbox.limit))}>{t('orderCollection.previous')}</Button>
            <span>{t('orderCollection.range', { from: view.inbox.total ? view.inbox.offset + 1 : 0, to: view.inbox.offset + view.inbox.entries.length, total: view.inbox.total })}</span>
            <Button variant="secondary" size="sm" disabled={!!busy || view.inbox.offset + view.inbox.limit >= view.inbox.total} onClick={() => page(view.inbox.offset + view.inbox.limit)}>{t('orderCollection.next')}</Button></div>
        </section>
        <section className="opening-stock-panel"><h2>{t('orderCollection.runs')}</h2>{!view.runs.length ? <p>{t('orderCollection.noRuns')}</p> : <div className="opening-stock-table-scroll"><table><thead><tr>{['runMode', 'runStatus', 'startedAt', 'completedAt', 'interval', 'runCount', 'runError'].map(key => <th key={key}>{t(`orderCollection.${key}`)}</th>)}</tr></thead><tbody>{view.runs.map(run => <tr key={run.id}>
          <td>{t(`orderCollection.modes.${run.mode}`)}</td><td>{t(`orderCollection.runStates.${run.status}`)}</td><td>{date(run.started_at)}</td><td>{date(run.completed_at)}</td><td>{date(run.from_at)} → {date(run.until_at)}</td><td>{run.observed_count}</td><td>{run.error ? message(run.error) : '—'}</td>
        </tr>)}</tbody></table></div>}</section>
      </>}
      <section className="opening-stock-panel"><h2>{t('orderCollection.stockTitle')}</h2><p>{t('orderCollection.stockHelp')}</p>
        <label>{t('orderCollection.skus')}<textarea rows={5} maxLength={11000} spellCheck={false} value={skuText} onChange={event => changeSkus(event.target.value)} /></label>
        <Button disabled={!!busy || !skuText.trim()} onClick={stockPreview}>{t('orderCollection.previewStock')}</Button>
        {stock && <div aria-live="polite"><p>{stock.warehouse.name} · {t('orderCollection.capturedAt')}: {date(stock.captured_at)}</p><p className="opening-stock-muted">{t('orderCollection.draftOnly')}</p>
          <div className="opening-stock-table-scroll"><table><thead><tr>{['sku', 'target', 'onHand', 'reserved', 'available', 'rowErrors'].map(key => <th key={key}>{t(`orderCollection.${key}`)}</th>)}</tr></thead><tbody>{stock.rows.map((row, index) => <tr key={`${row.sku}:${index}`}>
            <td><code>{row.sku}</code></td><td>{row.target ? <><code>{row.target.code}</code>{row.target.variant_code && <small className="orders-inbox-subline">{t('orderCollection.parent')}: {row.target.parent_code}</small>}</> : '—'}</td>
            <td>{row.quantity_known ? row.qty_on_hand ?? t('orderCollection.unknown') : t('orderCollection.unknown')}</td><td>{row.quantity_known ? row.qty_reserved ?? t('orderCollection.unknown') : t('orderCollection.unknown')}</td><td>{row.quantity_known ? row.qty_available ?? t('orderCollection.unknown') : t('orderCollection.unknown')}</td>
            <td>{row.errors.map((code, errorIndex) => <div key={errorIndex}>{message(code)}</div>)}</td>
          </tr>)}</tbody></table></div></div>}
      </section>
    </>}{error && <div className="opening-stock-errors" role="alert">{message(error)}</div>}
  </div>;
}
