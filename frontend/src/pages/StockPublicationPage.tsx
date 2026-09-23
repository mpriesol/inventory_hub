import React, { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Button } from '../components/ui/Button.new';
import { accessRevision, hubUnlocked, subscribeAccess, unlockHub } from '../api/access';
import { cancelPublication, configurePublication, createPublicationHold, getPublicationBatch, getPublicationBatches,
  getPublicationOptions, previewPublication, PublicationBatch, PublicationBatches, PublicationOptions,
  releasePublicationHold, resolvePublication, submitPublication, verifyPublication } from '../api/stockPublication';
import './OpeningStockPage.css';
import './StockPublicationPage.css';

type Operation = 'options' | 'configure' | 'hold' | 'release' | 'preview' | 'batch' | 'batches' | 'submit' | 'cancel' | 'verify' | 'resolve' | null;
const uuid = () => crypto.randomUUID();
const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
interface Scoped<T> { value: T; shop: string; revision: number }

export function StockPublicationPage() {
  const { t, i18n } = useTranslation();
  const [query] = useSearchParams();
  const revision = useSyncExternalStore(subscribeAccess, accessRevision);
  const [shop, setShop] = useState(query.get('shop') === 'xtrek' ? 'xtrek' : 'biketrek');
  const [token, setToken] = useState('');
  const [loaded, setLoaded] = useState<Scoped<PublicationOptions> | null>(null);
  const [batchLoaded, setBatchLoaded] = useState<Scoped<PublicationBatch> | null>(null);
  const [recentLoaded, setRecentLoaded] = useState<Scoped<PublicationBatches> | null>(null);
  const scoped = <T,>(value: Scoped<T> | null): T | null => value?.revision === revision && value.shop === shop ? value.value : null;
  const options = scoped(loaded), batch = scoped(batchLoaded), recent = scoped(recentLoaded);
  const [policyEnabled, setPolicyEnabled] = useState(false);
  const [policyConfirm, setPolicyConfirm] = useState(false);
  const [writersPaused, setWritersPaused] = useState(false);
  const [ordersReconciled, setOrdersReconciled] = useState(false);
  const [maintenanceCompleted, setMaintenanceCompleted] = useState(false);
  const [skuText, setSkuText] = useState('');
  const [batchId, setBatchId] = useState('');
  const [submitConfirm, setSubmitConfirm] = useState(false);
  const [cancelConfirm, setCancelConfirm] = useState(false);
  const [requestsFinished, setRequestsFinished] = useState(false);
  const [busy, setBusy] = useState<Operation>(null);
  const [uncertain, setUncertain] = useState<'options' | 'batch' | null>(null);
  const [error, setError] = useState('');
  const [blockedBatches, setBlockedBatches] = useState<Record<string, string>>({});
  const generation = useRef(0), controller = useRef<AbortController | null>(null), busyRef = useRef<Operation>(null), writeRef = useRef(false);
  const previewRequest = useRef<string | null>(null);
  const writing = !!busy && !['options', 'batch', 'batches', 'preview'].includes(busy);
  const activeHold = options?.hold?.active ? options.hold : null;
  const policyDirty = !!options && policyEnabled !== options.policy.enabled;
  const sameHold = !!batch && !!activeHold && batch.hold_id === activeHold.id;
  const dangerousBatch = (!!batch && (['queued', 'running', 'uncertain'].includes(batch.status) || batch.items.some(item => ['sending', 'uncertain'].includes(item.status))))
    || Object.values(blockedBatches).includes(activeHold?.id || '');
  const expired = !!batch && new Date(batch.expires_at).getTime() <= Date.now();
  function clearConfirmations() { setPolicyConfirm(false); setWritersPaused(false); setOrdersReconciled(false); setMaintenanceCompleted(false); setSubmitConfirm(false); setCancelConfirm(false); setRequestsFinished(false); }
  function reset() {
    generation.current += 1; controller.current?.abort(); controller.current = null; busyRef.current = null; setBusy(null);
    setLoaded(null); setBatchLoaded(null); setRecentLoaded(null); setSkuText(''); setBatchId(''); setPolicyEnabled(false); setBlockedBatches({}); previewRequest.current = null;
    clearConfirmations(); setUncertain(null); setError('');
  }
  useEffect(() => { reset(); setToken(''); return () => { generation.current += 1; controller.current?.abort(); }; }, [revision]);
  function report(value: unknown) {
    const code = (value as Error & { code?: string }).code || 'request_failed';
    if (code === 'hub_access_required' || code === 'hub_access_not_configured') unlockHub('');
    setError(code);
  }
  function acceptOptions(value: PublicationOptions) {
    setLoaded({ value, shop, revision: accessRevision() }); setPolicyEnabled(value.policy.enabled); clearConfirmations();
    if (uncertain === 'options') setUncertain(null);
  }
  function acceptBatch(value: PublicationBatch) {
    if (value.shop_code !== shop) { setError('stock_publication_wrong_shop'); return; }
    setBatchLoaded({ value, shop, revision: accessRevision() }); setBatchId(value.id); setSkuText(value.items.map(item => item.sku).join('\n'));
    if (value.preview_data.rows) setSkuText(value.preview_data.rows.map(row => row.sku).join('\n'));
    setBlockedBatches(previous => { const next = { ...previous }; if (['queued', 'running', 'uncertain'].includes(value.status) || value.items.some(item => ['sending', 'uncertain'].includes(item.status))) next[value.id] = value.hold_id; else delete next[value.id]; return next; });
    previewRequest.current = null; clearConfirmations(); if (uncertain === 'batch') setUncertain(null);
  }
  async function read<T>(operation: Operation, request: (signal: AbortSignal) => Promise<T>, accept: (value: T) => void) {
    if (busyRef.current || !hubUnlocked()) return;
    const id = generation.current, credential = accessRevision(), abort = new AbortController(); controller.current = abort;
    busyRef.current = operation; setBusy(operation); setError(''); clearConfirmations();
    const current = () => id === generation.current && credential === accessRevision() && !abort.signal.aborted;
    try { const value = await request(abort.signal); if (current()) accept(value); }
    catch (value) { if (current()) {
      const code = (value as Error & { code?: string }).code;
      if (operation === 'preview' && (!code || code === 'request_failed')) setUncertain('batch');
      if (operation === 'batch' && code === 'stock_publication_batch_not_found' && uncertain === 'batch' && !batch) setUncertain(null);
      report(value);
    } }
    finally { if (current()) { busyRef.current = null; setBusy(null); } }
  }
  async function mutate<T>(operation: Operation, request: () => Promise<T>, accept: (value: T) => void, recovery: 'options' | 'batch') {
    if (busyRef.current || writeRef.current || uncertain || !hubUnlocked()) return;
    const id = generation.current, credential = accessRevision(); writeRef.current = true; busyRef.current = operation; setBusy(operation); setError('');
    const current = () => id === generation.current && credential === accessRevision();
    try { const value = await request(); if (current()) { accept(value); clearConfirmations(); } }
    catch (value) { if (current()) { setUncertain(recovery); clearConfirmations(); report(value); } }
    finally { writeRef.current = false; if (current()) { busyRef.current = null; setBusy(null); } }
  }
  function changeSkus(value: string) {
    setSkuText(value); setBatchLoaded(null); previewRequest.current = null; clearConfirmations(); setError('');
    if (busyRef.current === 'preview' || busyRef.current === 'batch') { generation.current += 1; controller.current?.abort(); busyRef.current = null; setBusy(null); }
  }
  function prepare() {
    if (!activeHold || activeHold.shop_code !== shop || busyRef.current || uncertain || policyDirty) return;
    const skus = skuText.split(/\r?\n/).map(value => value.trim()).filter(Boolean);
    if (!skus.length || skus.length > 100 || new Set(skus).size !== skus.length || skus.some(value => value.length > 100 || /[\x00-\x1f\x7f]/.test(value))) { setError('invalid_skus'); return; }
    if (options && skus.length > options.values.publication_batch_size) { setError('stock_publication_batch_too_large'); return; }
    const id = previewRequest.current || uuid(); previewRequest.current = id; setBatchId(id); setBatchLoaded(null);
    read('preview', signal => previewPublication({ request_id: id, shop_code: shop, skus }, signal), acceptBatch);
  }
  function recover(id = batchId) {
    if (!uuidPattern.test(id)) { setError('invalid_batch_id'); return; }
    read('batch', signal => getPublicationBatch(id, signal), acceptBatch);
  }
  function loadRecent(offset = 0) { read('batches', signal => getPublicationBatches(shop, offset, signal), value => setRecentLoaded({ value, shop, revision: accessRevision() })); }
  function configure() {
    if (!options || !policyConfirm || dangerousBatch) return;
    mutate('configure', () => configurePublication({ shop_code: shop, expected_revision: options.policy.revision, enabled: policyEnabled, confirmed: true }), value => {
      acceptOptions(value); setBatchLoaded(null); previewRequest.current = null;
    }, 'options');
  }
  function openHold() {
    if (!options || activeHold || !writersPaused || !ordersReconciled) return;
    mutate('hold', () => createPublicationHold(shop), hold => setLoaded(previous => previous ? { ...previous, value: { ...previous.value, hold } } : null), 'options');
  }
  function releaseHold() {
    if (!activeHold || activeHold.shop_code !== shop || !maintenanceCompleted || dangerousBatch) return;
    mutate('release', () => releasePublicationHold(activeHold.id), hold => { setLoaded(previous => previous ? { ...previous, value: { ...previous.value, hold } } : null); setBatchLoaded(null); previewRequest.current = null; }, 'options');
  }
  function submit() {
    if (!batch || !sameHold || !options?.server_write_enabled || !options.external_write_enabled || !submitConfirm || expired || batch.status !== 'prepared' || policyDirty) return;
    mutate('submit', () => submitPublication(batch.id, batch.preview_hash), acceptBatch, 'batch');
  }
  const message = (code: string) => t(`stockPublication.errors.${code}`, { defaultValue: t(`orderCollection.errors.${code}`, { defaultValue: t(`stockSettings.errors.${code}`, { defaultValue: t('stockPublication.errors.request_failed') }) }) });
  const date = (value: string | null | undefined) => value ? new Date(value).toLocaleString(i18n.language) : '—';
  const state = (value: string) => t(`stockPublication.states.${value}`, { defaultValue: t('stockPublication.unknown') });
  const quantity = (value: string | null) => value ?? t('stockPublication.unknown');

  return <div className="opening-stock stock-publication">
    <Link to="/stock" className="opening-stock-back">← {t('stockPublication.back')}</Link>
    <header><div><h1>{t('stockPublication.title')}</h1><p>{t('stockPublication.subtitle')}</p></div>{hubUnlocked() && <Button variant="secondary" onClick={() => unlockHub('')}>{t('stockPublication.lock')}</Button>}</header>
    <div className="opening-stock-notice">{t('stockPublication.scope')}</div>
    {!hubUnlocked() ? <form className="opening-stock-panel opening-stock-unlock" onSubmit={event => { event.preventDefault(); if (token.trim()) unlockHub(token.trim()); }}>
      <p>{t('stockPublication.tokenHelp')}</p><label>{t('stockPublication.token')}<input data-testid="unlock-token" type="password" autoComplete="off" value={token} onChange={event => setToken(event.target.value)} /></label><Button data-testid="unlock" type="submit" disabled={!token.trim()}>{t('stockPublication.unlock')}</Button>
    </form> : <>
      <section className="opening-stock-panel"><div className="opening-stock-actions"><label>{t('stockPublication.shop')}<select data-testid="shop" disabled={writing} value={shop} onChange={event => { reset(); setShop(event.target.value); }}><option value="biketrek">BIKETREK</option><option value="xtrek">xTrek</option></select></label>
        <Button data-testid="load-options" variant="secondary" disabled={!!busy} onClick={() => read('options', signal => getPublicationOptions(shop, signal), acceptOptions)}>{t('stockPublication.loadOptions')}</Button></div>
        <p><Link to={`/settings/stock?shop=${encodeURIComponent(shop)}`}>{t('stockSettings.open')}</Link> · <Link to={`/orders/inbox?shop=${encodeURIComponent(shop)}`}>{t('orderCollection.open')}</Link></p>
      </section>
      {uncertain && <div className="opening-stock-notice" role="alert">{t(uncertain === 'options' ? 'stockPublication.optionsUncertain' : 'stockPublication.batchUncertain')}</div>}
      {options && <>
        <section className="opening-stock-panel"><h2>{t('stockPublication.policyTitle')}</h2><p>{t('stockPublication.warehouse')}: {options.warehouse.name} · {options.warehouse.code}</p>
          {!options.server_write_enabled && <div role="status" className="opening-stock-notice">{t('stockPublication.serverDisabled')}</div>}
          <div className="opening-stock-confirmations"><label><input data-testid="policy-enabled" type="checkbox" disabled={!!busy || !!uncertain || dangerousBatch} checked={policyEnabled} onChange={event => { setPolicyEnabled(event.target.checked); clearConfirmations(); setBatchLoaded(null); previewRequest.current = null; }} />{t('stockPublication.policyEnabled')}</label>
            <label><input data-testid="policy-confirm" type="checkbox" disabled={!!busy || !!uncertain || dangerousBatch} checked={policyConfirm} onChange={event => setPolicyConfirm(event.target.checked)} />{t('stockPublication.policyConfirm')}</label></div>
          <Button data-testid="save-policy" disabled={!!busy || !!uncertain || !policyConfirm || dangerousBatch} onClick={configure}>{t('stockPublication.savePolicy')}</Button>
        </section>
        <section className="opening-stock-panel"><h2>{t('stockPublication.holdTitle')}</h2><p>{t('stockPublication.holdHelp')}</p>
          {activeHold ? <>
            <div role="status" className="opening-stock-warnings">{t('stockPublication.holdActive', { at: date(activeHold.created_at), shop: activeHold.shop_code })}<br />{t('stockPublication.noExpiry')}</div>
            {activeHold.shop_code !== shop && <p className="opening-stock-errors">{t('stockPublication.otherShopHold')}</p>}
            <div className="opening-stock-confirmations"><label><input data-testid="maintenance-completed" type="checkbox" disabled={!!busy || !!uncertain || dangerousBatch || activeHold.shop_code !== shop} checked={maintenanceCompleted} onChange={event => setMaintenanceCompleted(event.target.checked)} />{t('stockPublication.maintenanceCompleted')}</label></div>
            <Button data-testid="release-hold" variant="secondary" disabled={!!busy || !!uncertain || dangerousBatch || !maintenanceCompleted || activeHold.shop_code !== shop} onClick={releaseHold}>{t('stockPublication.releaseHold')}</Button>
            {dangerousBatch && <p className="opening-stock-muted">{t('stockPublication.releaseBlocked')}</p>}
          </> : <>
            <div className="opening-stock-confirmations"><label><input data-testid="writers-paused" type="checkbox" disabled={!!busy || !!uncertain} checked={writersPaused} onChange={event => setWritersPaused(event.target.checked)} />{t('stockPublication.writersPaused')}</label>
              <label><input data-testid="orders-reconciled" type="checkbox" disabled={!!busy || !!uncertain} checked={ordersReconciled} onChange={event => setOrdersReconciled(event.target.checked)} />{t('stockPublication.ordersReconciled')}</label></div>
            <Button data-testid="create-hold" disabled={!!busy || !!uncertain || !writersPaused || !ordersReconciled} onClick={openHold}>{t('stockPublication.createHold')}</Button>
          </>}
        </section>
        <section className="opening-stock-panel"><h2>{t('stockPublication.previewTitle')}</h2><p>{t('stockPublication.previewHelp', { limit: options.values.publication_batch_size, minutes: options.values.publication_preview_minutes })}</p>
          <label>{t('stockPublication.skus')}<textarea data-testid="skus" rows={5} maxLength={11000} spellCheck={false} value={skuText} disabled={writing || !!uncertain || dangerousBatch} onChange={event => changeSkus(event.target.value)} /></label>
          <Button data-testid="preview" disabled={!!busy || !!uncertain || !activeHold || activeHold.shop_code !== shop || !skuText.trim() || dangerousBatch || policyDirty} onClick={prepare}>{t('stockPublication.preview')}</Button>
          {policyDirty && <p className="opening-stock-muted">{t('stockPublication.unsavedPolicy')}</p>}
        </section>
      </>}
      <section className="opening-stock-panel"><h2>{t('stockPublication.recoveryTitle')}</h2><p>{t('stockPublication.recoveryHelp')}</p>
        <div className="opening-stock-actions"><label className="opening-stock-grow">{t('stockPublication.batchId')}<input data-testid="batch-id" value={batchId} disabled={writing || uncertain === 'batch'} onChange={event => {
          if (busyRef.current === 'batch') { generation.current += 1; controller.current?.abort(); busyRef.current = null; setBusy(null); }
          setBatchId(event.target.value.trim()); setBatchLoaded(null); clearConfirmations();
        }} /></label>
          <Button data-testid="recover-batch" variant="secondary" disabled={!!busy || !uuidPattern.test(batchId)} onClick={() => recover()}>{t('stockPublication.recover')}</Button>
          <Button data-testid="load-batches" variant="secondary" disabled={!!busy} onClick={() => loadRecent()}>{t('stockPublication.loadRecent')}</Button></div>
        {recent && <div className="opening-stock-recent">{!recent.batches.length ? <p>{t('stockPublication.noRecent')}</p> : recent.batches.map(item => <div key={item.id}><span><code>{item.id}</code><small>{date(item.created_at)} · {state(item.status)}</small></span><Button size="sm" variant="secondary" disabled={!!busy || (uncertain === 'batch' && item.id !== batchId)} onClick={() => recover(item.id)}>{t('stockPublication.openBatch')}</Button></div>)}
          <div className="opening-stock-pagination"><Button variant="secondary" size="sm" disabled={!!busy || recent.offset === 0} onClick={() => loadRecent(Math.max(0, recent.offset - recent.limit))}>{t('stockPublication.previous')}</Button><span>{t('stockPublication.range', { from: recent.total ? recent.offset + 1 : 0, to: recent.offset + recent.batches.length, total: recent.total })}</span><Button variant="secondary" size="sm" disabled={!!busy || recent.offset + recent.limit >= recent.total} onClick={() => loadRecent(recent.offset + recent.limit)}>{t('stockPublication.next')}</Button></div>
        </div>}
      </section>
      {batch && <section className="opening-stock-panel" aria-live="polite"><h2>{t('stockPublication.batchTitle')}</h2><p><strong>{state(batch.status)}</strong> · <code>{batch.id}</code></p><p className="opening-stock-muted">{t('stockPublication.expiresAt')}: {date(batch.expires_at)}</p>
        {batch.error && <div className="opening-stock-errors">{message(batch.error)}</div>}
        {batch.status === 'blocked' && <p className="opening-stock-errors">{t('stockPublication.blockedHelp')}</p>}
        <h3>{t('stockPublication.comparison')}</h3>
        <div className="opening-stock-table-scroll"><table data-testid="comparison-table"><thead><tr>{['sku', 'target', 'onHand', 'reserved', 'desired', 'before', 'itemError'].map(key => <th key={key}>{t(`stockPublication.${key}`)}</th>)}</tr></thead><tbody>{batch.preview_data.rows.slice(0, 100).map((row, index) => <tr key={`${row.sku}:${index}`}>
          <td><code>{row.sku}</code></td><td><code>{row.remote?.identity.code ?? row.target?.variant_code ?? row.target?.parent_code ?? '—'}</code>{row.remote?.identity.variant_code && <small className="stock-publication-subline">{t('stockPublication.parent')}: {row.remote.identity.parent_code}</small>}</td>
          <td>{row.quantity_known ? quantity(row.qty_on_hand) : t('stockPublication.unknown')}</td><td>{row.quantity_known ? quantity(row.qty_reserved) : t('stockPublication.unknown')}</td><td>{row.quantity_known ? quantity(row.qty_available) : t('stockPublication.unknown')}</td><td>{quantity(row.remote?.quantity ?? null)}</td><td>{row.errors.map((code, index) => <div key={index}>{message(code)}</div>)}</td>
        </tr>)}</tbody></table></div>
        <h3>{t('stockPublication.deliveryResults')}</h3>
        <div className="opening-stock-table-scroll"><table><thead><tr>{['sku', 'target', 'before', 'desired', 'after', 'itemStatus', 'checkedAt', 'itemError'].map(key => <th key={key}>{t(`stockPublication.${key}`)}</th>)}</tr></thead><tbody>{batch.items.slice(0, 100).map(item => <tr key={item.id}>
          <td><code>{item.sku}</code></td><td><code>{String(item.target?.code ?? item.target?.variant_code ?? item.target?.parent_code ?? '—')}</code></td><td>{quantity(item.before_quantity)}</td><td>{quantity(item.quantity)}</td><td>{quantity(item.after_quantity)}</td><td>{state(item.status)}</td><td>{date(item.verified_at)}</td><td>{item.error ? message(item.error) : '—'}</td>
        </tr>)}</tbody></table></div>
        {batch.status === 'prepared' && <>
          {expired && <p className="opening-stock-errors">{t('stockPublication.expired')}</p>}
          <div className="opening-stock-confirmations"><label><input data-testid="submit-confirm" type="checkbox" checked={submitConfirm} disabled={!!busy || !!uncertain || expired || !sameHold || !options?.external_write_enabled || policyDirty} onChange={event => setSubmitConfirm(event.target.checked)} />{t('stockPublication.submitConfirm')}</label></div>
          <Button data-testid="submit" disabled={!!busy || !!uncertain || expired || !sameHold || !options?.server_write_enabled || !options.external_write_enabled || !submitConfirm || policyDirty} onClick={submit}>{t('stockPublication.submit')}</Button>
        </>}
        {['queued', 'running'].includes(batch.status) && <p className="opening-stock-notice">{t('stockPublication.queuedHelp')}</p>}
        {batch.status === 'uncertain' && <div className="opening-stock-warnings">{t('stockPublication.uncertainHelp')}</div>}
        <div className="opening-stock-actions stock-publication-result-actions">
          <Button data-testid="verify" variant="secondary" disabled={!!busy || !!uncertain || !sameHold || ['queued', 'running'].includes(batch.status)} onClick={() => { if (sameHold) mutate('verify', () => verifyPublication(batch.id), acceptBatch, 'batch'); }}>{t('stockPublication.verify')}</Button>
          <Button variant="secondary" disabled={!!busy} onClick={() => recover(batch.id)}>{t('stockPublication.recover')}</Button>
        </div>
        {batch.status === 'uncertain' && <><div className="opening-stock-confirmations"><label><input data-testid="requests-finished" type="checkbox" checked={requestsFinished} disabled={!!busy || !!uncertain || !sameHold} onChange={event => setRequestsFinished(event.target.checked)} />{t('stockPublication.requestsFinished')}</label></div><Button data-testid="resolve" disabled={!!busy || !!uncertain || !requestsFinished || !sameHold} onClick={() => { if (requestsFinished && sameHold) mutate('resolve', () => resolvePublication(batch.id), acceptBatch, 'batch'); }}>{t('stockPublication.resolve')}</Button></>}
        {['prepared', 'queued'].includes(batch.status) && <><div className="opening-stock-confirmations"><label><input data-testid="cancel-confirm" type="checkbox" disabled={!!busy || !!uncertain} checked={cancelConfirm} onChange={event => setCancelConfirm(event.target.checked)} />{t('stockPublication.cancelConfirm')}</label></div><Button data-testid="cancel" variant="secondary" disabled={!!busy || !!uncertain || !cancelConfirm} onClick={() => { if (cancelConfirm) mutate('cancel', () => cancelPublication(batch.id), acceptBatch, 'batch'); }}>{t('stockPublication.cancel')}</Button></>}
      </section>}
    </>}{error && <div role="alert" className="opening-stock-errors">{message(error)}</div>}
  </div>;
}
