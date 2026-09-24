import React, { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Button } from '../components/ui/Button.new';
import { ActionScope } from '../components/ui/ActionScope';
import { accessRevision, hubUnlocked, subscribeAccess, unlockHub } from '../api/access';
import {
  FIFO_COST_FIELDS, FifoCostField, FifoCostValues, FifoCostOptions, FifoCostPublication, FifoCostHistory,
  getFifoCostOptions, saveFifoCostWarehouse, saveFifoCostSettings, runFifoCostSync, getFifoCostHistory,
  previewFifoOrderCost, getFifoCostPublication, sendFifoCostPublication, resolveFifoCostPublication,
} from '../api/fifoCostSync';
import './OpeningStockPage.css';
import './AvailabilitySyncPage.css';

type Inputs = Record<FifoCostField, string>;
const inputs = (value: FifoCostValues): Inputs => ({ interval_seconds: String(value.interval_seconds), batch_size: String(value.batch_size) });
const valid = (value: Inputs) => FIFO_COST_FIELDS.every(field => /^\d+$/.test(value[field.key]) && Number.isSafeInteger(Number(value[field.key])) && Number(value[field.key]) >= field.min && Number(value[field.key]) <= field.max);
const numbers = (value: Inputs): FifoCostValues => ({ interval_seconds: Number(value.interval_seconds), batch_size: Number(value.batch_size) });

/** Access changes invalidate all responses. A lost write response always requires a new read. */
function useOperation() {
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [uncertain, setUncertain] = useState(false);
  const running = useRef(false), generation = useRef(0), abort = useRef<AbortController | null>(null), uncertainPublication = useRef<string | null>(null);
  useEffect(() => () => { generation.current++; abort.current?.abort(); }, []);
  async function run<T>(write: boolean, request: (signal: AbortSignal) => Promise<T>, accept: (value: T) => void, publicationId?: string) {
    if (running.current || !hubUnlocked()) return;
    running.current = true; setBusy(true); setError('');
    const id = ++generation.current, credential = accessRevision(), controller = new AbortController(); abort.current = controller;
    const current = () => generation.current === id && accessRevision() === credential && !controller.signal.aborted;
    try { const value = await request(controller.signal); if (current()) accept(value); }
    catch (value) { if (current()) { const code = (value as { code?: string })?.code || 'request_failed'; setError(code); if (write) { setUncertain(true); uncertainPublication.current = publicationId || null; } if (['hub_access_required', 'hub_access_not_configured'].includes(code)) unlockHub(''); } }
    finally { if (current()) { running.current = false; setBusy(false); } }
  }
  return { busy, error, uncertain, run, isRunning: () => running.current, recovered: () => { uncertainPublication.current = null; setUncertain(false); }, recoveredPublication: (id: string) => { if (uncertainPublication.current === id) { uncertainPublication.current = null; setUncertain(false); } } };
}

function CostPanel({ shop, onBusy }: { shop: string; onBusy: (value: boolean) => void }) {
  const { t, i18n } = useTranslation();
  const operation = useOperation();
  useEffect(() => { onBusy(operation.busy); return () => onBusy(false); }, [operation.busy]);
  const [options, setOptions] = useState<FifoCostOptions | null>(null), [warehouseCode, setWarehouseCode] = useState('');
  const [warehouse, setWarehouse] = useState<Inputs>({ interval_seconds: '', batch_size: '' }), [overrides, setOverrides] = useState<Partial<Inputs>>({});
  const [enabled, setEnabled] = useState(false), [products, setProducts] = useState(false), [orders, setOrders] = useState(false);
  const [reloadRequired, setReloadRequired] = useState(false), [message, setMessage] = useState('');
  const [history, setHistory] = useState<FifoCostHistory | null>(null), [publication, setPublication] = useState<FifoCostPublication | null>(null);
  const [pendingPreview, setPendingPreview] = useState<{ id: string; orderNumber: string } | null>(null);
  const [orderNumber, setOrderNumber] = useState(''), [confirmed, setConfirmed] = useState(false), [settled, setSettled] = useState(false), [note, setNote] = useState('');
  const [now, setNow] = useState(Date.now());
  useEffect(() => { const timer = window.setInterval(() => setNow(Date.now()), 1000); return () => window.clearInterval(timer); }, []);
  const date = (stamp: string | null) => stamp ? new Date(stamp).toLocaleString(i18n.language) : '—';
  const errorText = (code: string) => t(`fifoCostSync.errors.${code}`, { defaultValue: t('fifoCostSync.requestFailed') });
  const statusText = (status: string) => t(`fifoCostSync.statuses.${status}`, { defaultValue: status });
  function clearConfirmation() { setConfirmed(false); setSettled(false); setNote(''); }
  function clearSelection() { setPublication(null); clearConfirmation(); }
  function edit() { setMessage(''); clearSelection(); }
  function accept(value: FifoCostOptions) {
    if (!value?.settings || !Array.isArray(value.warehouses) || !Array.isArray(value.publications)) throw new Error('Invalid options response');
    setOptions(value); setWarehouseCode(value.settings.warehouse_code || value.warehouse?.code || '');
    setWarehouse(value.warehouse_settings ? inputs(value.warehouse_settings) : { interval_seconds: '', batch_size: '' });
    setOverrides(Object.fromEntries(FIFO_COST_FIELDS.filter(field => value.settings[field.key] !== null).map(field => [field.key, String(value.settings[field.key])])));
    setEnabled(value.settings.enabled); setProducts(value.settings.product_cost_enabled); setOrders(value.settings.order_cost_enabled);
    setReloadRequired(false); setHistory(null); edit(); operation.recovered();
  }
  function acceptPublication(value: FifoCostPublication) {
    if (!value?.id || !['product', 'order'].includes(value.kind) || !value.source || !['prepared', 'queued', 'sending', 'verified', 'uncertain', 'failed', 'skipped', 'resolved'].includes(value.status)) throw new Error('Invalid publication response');
    if (value.status === 'prepared' && (value.kind !== 'order' || !Array.isArray(value.source.lines) || !value.source.lines.length || !value.remote_costs || typeof value.remote_costs.prices_with_vat_yn !== 'boolean' || !Array.isArray(value.remote_costs.lines) || value.remote_costs.lines.length !== value.source.lines.length || !value.expires_at || !Number.isFinite(Date.parse(value.expires_at)))) throw new Error('Incomplete price preview');
    setPublication(value); clearConfirmation(); setMessage('');
  }
  function readPublication(id: string) {
    operation.run(false, signal => getFifoCostPublication(id, signal), value => {
      if (value.id !== id) throw new Error('Wrong publication response');
      acceptPublication(value); operation.recoveredPublication(id);
      setPendingPreview(previous => previous?.id === id ? null : previous);
    });
  }
  function prepareOrder(request = { id: crypto.randomUUID(), orderNumber: orderNumber.trim() }) {
    if (operation.isRunning()) return;
    setPendingPreview(request);
    operation.run(true, () => previewFifoOrderCost(shop, request.orderNumber, request.id), value => {
      if (value.id !== request.id) throw new Error('Wrong preview response');
      acceptPublication(value); operation.recoveredPublication(request.id); setPendingPreview(null);
    }, request.id);
  }
  const inherited = options?.warehouse_settings ? inputs(options.warehouse_settings) : { interval_seconds: '300', batch_size: '20' };
  const effective = { ...inherited, ...overrides };
  const warehouseDirty = !!options?.warehouse_settings && FIFO_COST_FIELDS.some(field => warehouse[field.key] !== String(options.warehouse_settings![field.key]));
  const shopDirty = !!options && (warehouseCode !== (options.settings.warehouse_code || options.warehouse?.code || '') || enabled !== options.settings.enabled || products !== options.settings.product_cost_enabled || orders !== options.settings.order_cost_enabled || FIFO_COST_FIELDS.some(field => (overrides[field.key] ?? null) !== (options.settings[field.key] === null ? null : String(options.settings[field.key]))));
  const blocked = operation.busy || operation.uncertain;
  const settingsBlocked = blocked || reloadRequired;
  const activityBlocked = settingsBlocked || warehouseDirty || shopDirty;
  const expired = !!publication?.expires_at && new Date(publication.expires_at).getTime() <= now;
  const canSend = !!publication && publication.kind === 'order' && publication.status === 'prepared' && !!publication.source.lines?.length && confirmed && !expired && !!options?.server_write_enabled && options.settings.order_cost_enabled && !activityBlocked;
  const rows = history?.items || options?.publications || [];
  function renderFields(target: 'warehouse' | 'shop') {
    return <div className="availability-fields">{FIFO_COST_FIELDS.map(field => <div key={field.key}>
      <label>{t(`fifoCostSync.fields.${field.key}`)}<input data-testid={`cost-${target}-${field.key}`} type="number" min={field.min} max={field.max} step="1" disabled={settingsBlocked || (target === 'shop' && overrides[field.key] === undefined)} value={target === 'warehouse' ? warehouse[field.key] : effective[field.key]} onChange={event => { edit(); const value = event.target.value; target === 'warehouse' ? setWarehouse(previous => ({ ...previous, [field.key]: value })) : setOverrides(previous => ({ ...previous, [field.key]: value })); }} /><small>{t('fifoCostSync.range', { min: field.min, max: field.max })}</small></label>
      {target === 'shop' && <label className="availability-check"><input data-testid={`cost-inherit-${field.key}`} type="checkbox" checked={overrides[field.key] === undefined} disabled={settingsBlocked} onChange={event => { edit(); setOverrides(previous => { const next = { ...previous }; if (event.target.checked) delete next[field.key]; else next[field.key] = inherited[field.key]; return next; }); }} />{t('fifoCostSync.inherit')}</label>}
    </div>)}</div>;
  }
  function loadHistory(offset = 0) {
    operation.run(false, signal => getFifoCostHistory(shop, offset, signal), value => { setHistory(value); clearSelection(); setMessage(''); });
  }
  return <section className="opening-stock-panel">
    <div className="opening-stock-actions"><h2>{shop === 'xtrek' ? 'xTrek' : 'BIKETREK'}</h2><span className="action-control"><Button data-testid="load-cost-settings" variant="secondary" disabled={operation.busy} onClick={() => operation.run(false, signal => getFifoCostOptions(shop, signal), accept)}>{t('fifoCostSync.load')}</Button><ActionScope effects={['hub-read']} /></span></div>
    {operation.uncertain && <p role="alert" className="opening-stock-notice">{t('fifoCostSync.uncertainResponse')}</p>}
    {operation.error && <p role="alert" className="opening-stock-errors">{errorText(operation.error)} <code>{operation.error}</code></p>}
    {message && <p role="status" className="opening-stock-success">{t(`fifoCostSync.${message}`)}</p>}
    {options && <>
      {!options.server_write_enabled && <p role="status" className="opening-stock-notice">{t('fifoCostSync.serverDisabled')}</p>}
      {!!options.blockers.length && <div className="opening-stock-notice"><strong>{t('fifoCostSync.notReady')}</strong><ul>{options.blockers.map(code => <li key={code}>{errorText(code)} <code>{code}</code></li>)}</ul></div>}
      <label>{t('fifoCostSync.warehouse')}<select data-testid="cost-warehouse" disabled={settingsBlocked} value={warehouseCode} onChange={event => { edit(); setWarehouseCode(event.target.value); }}><option value="">{t('fifoCostSync.selectWarehouse')}</option>{options.warehouses.map(value => <option key={value.code} value={value.code}>{value.name}</option>)}</select></label>
      {warehouseCode && warehouseCode !== options.warehouse?.code && <p className="opening-stock-notice">{t('fifoCostSync.warehouseChanged')}</p>}
      {options.warehouse && options.warehouse_settings && <details className="availability-warehouse"><summary>{t('fifoCostSync.warehouseDefaults', { warehouse: options.warehouse.name })}</summary><p>{t('fifoCostSync.warehouseHelp')}</p>{renderFields('warehouse')}
        <span className="action-control"><Button data-testid="save-cost-warehouse" disabled={settingsBlocked || !warehouseDirty || !valid(warehouse)} onClick={() => operation.run(true, () => saveFifoCostWarehouse({ warehouse_code: options.warehouse!.code, expected_revision: options.warehouse_settings!.revision, ...numbers(warehouse), confirmed: true }), () => { setReloadRequired(true); clearSelection(); setMessage('warehouseSaved'); })}>{t('fifoCostSync.saveWarehouse')}</Button><ActionScope effects={['hub-write']} /></span>
      </details>}
      <h3>{t('fifoCostSync.shopSettings')}</h3><p>{t('fifoCostSync.flagsHelp')}</p>
      {reloadRequired && <p role="status" className="opening-stock-notice">{t('fifoCostSync.reloadRequired')}</p>}
      {renderFields('shop')}
      <label className="availability-check"><input data-testid="cost-products" type="checkbox" checked={products} disabled={settingsBlocked} onChange={event => { edit(); setProducts(event.target.checked); if (!event.target.checked && !orders) setEnabled(false); }} />{t('fifoCostSync.products')}</label>
      <p>{t('fifoCostSync.productHelp')}</p>
      <label className="availability-check"><input data-testid="cost-orders" type="checkbox" checked={orders} disabled={settingsBlocked} onChange={event => { edit(); setOrders(event.target.checked); if (!event.target.checked && !products) setEnabled(false); }} />{t('fifoCostSync.orders')}</label>
      <p>{t('fifoCostSync.orderHelp')}</p>
      <label className="availability-check"><input data-testid="cost-enabled" type="checkbox" checked={enabled} disabled={settingsBlocked || (!products && !orders)} onChange={event => { edit(); setEnabled(event.target.checked); }} />{t('fifoCostSync.automatic')}</label>
      {(warehouseDirty || shopDirty) && <p role="status" className="availability-change">{t('fifoCostSync.unsaved')}</p>}
      <div className="availability-actions"><span className="action-control"><Button data-testid="save-cost-settings" disabled={settingsBlocked || warehouseDirty || !shopDirty || !warehouseCode || !valid(effective)} onClick={() => operation.run(true, () => saveFifoCostSettings({ shop_code: shop, warehouse_code: warehouseCode, expected_revision: options.settings.revision, enabled, product_cost_enabled: products, order_cost_enabled: orders, interval_seconds: overrides.interval_seconds === undefined ? null : Number(overrides.interval_seconds), batch_size: overrides.batch_size === undefined ? null : Number(overrides.batch_size), confirmed: true }), value => { accept(value); setMessage('saved'); })}>{t('fifoCostSync.save')}</Button><ActionScope effects={enabled ? ['hub-write', 'queued-upgates'] : ['hub-write']} shop={shop} calls={{ kind: 'variable' }} /></span>
        <span className="action-control"><Button data-testid="run-cost-sync" variant="secondary" disabled={activityBlocked || !options.server_write_enabled || !!options.blockers.length || (!options.settings.product_cost_enabled && !options.settings.order_cost_enabled) || options.settings.scan_active} onClick={() => operation.run(true, () => runFifoCostSync(shop), value => { accept(value); setMessage('queued'); })}>{t('fifoCostSync.run')}</Button><ActionScope effects={['hub-write', 'queued-upgates']} shop={shop} calls={{ kind: 'variable' }} /></span>
      </div>
      <dl className="availability-status"><div><dt>{t('fifoCostSync.lastCompleted')}</dt><dd>{date(options.settings.last_completed_at)}</dd></div><div><dt>{t('fifoCostSync.nextRun')}</dt><dd>{options.settings.enabled ? date(options.settings.next_run_at) : t('fifoCostSync.automaticOff')}</dd></div><div><dt>{t('fifoCostSync.ordersSince')}</dt><dd>{date(options.settings.orders_since)}</dd></div></dl>
      <p>{t('fifoCostSync.scanCompletionHelp')}</p>
      {options.settings.retry_after_at && Date.parse(options.settings.retry_after_at) > now && <p role="status" className="opening-stock-notice" data-testid="cost-retry-after">{t('fifoCostSync.retryAfter', { date: date(options.settings.retry_after_at) })}</p>}
      {options.settings.scan_active && <p role="status">{t('fifoCostSync.scanActive')}</p>}
      {options.settings.last_error && <p role="alert" className="opening-stock-errors">{errorText(options.settings.last_error)} <code>{options.settings.last_error}</code></p>}
      <section className="availability-run"><h3>{t('fifoCostSync.historicTitle')}</h3><p>{t('fifoCostSync.historicHelp')}</p>
        <label>{t('fifoCostSync.orderNumber')}<input data-testid="cost-order-number" value={orderNumber} maxLength={100} disabled={blocked} onChange={event => { setOrderNumber(event.target.value); edit(); }} /></label>
        <span className="action-control"><Button data-testid="preview-order-cost" variant="secondary" disabled={activityBlocked || !!pendingPreview || !orderNumber.trim() || !options.settings.order_cost_enabled} onClick={() => prepareOrder()}>{t('fifoCostSync.preview')}</Button><ActionScope effects={['hub-write', 'upgates-read']} shop={shop} calls={{ kind: 'variable' }} /></span>
        {pendingPreview && <div className="opening-stock-notice" data-testid="cost-preview-recovery"><p>{t('fifoCostSync.previewRecovery', { order: pendingPreview.orderNumber })}</p><div className="availability-actions">
          <span className="action-control"><Button data-testid="recover-cost-preview" variant="secondary" disabled={operation.busy} onClick={() => readPublication(pendingPreview.id)}>{t('fifoCostSync.recoverPreview')}</Button><ActionScope effects={['hub-read']} /></span>
          <span className="action-control"><Button data-testid="retry-cost-preview" variant="secondary" disabled={operation.busy || reloadRequired || warehouseDirty || shopDirty || !options.settings.order_cost_enabled} onClick={() => prepareOrder(pendingPreview)}>{t('fifoCostSync.retryPreview')}</Button><ActionScope effects={['hub-write', 'upgates-read']} shop={shop} calls={{ kind: 'variable' }} /></span>
        </div></div>}
      </section>
      <section className="availability-run"><div className="opening-stock-actions"><h3>{t('fifoCostSync.history')}</h3><span className="action-control"><Button data-testid="load-cost-history" variant="secondary" disabled={operation.busy} onClick={() => loadHistory()}>{t('fifoCostSync.refreshHistory')}</Button><ActionScope effects={['hub-read']} /></span></div>
        {!rows.length ? <p>{t('fifoCostSync.noHistory')}</p> : <div className="opening-stock-table-scroll"><table><thead><tr><th>{t('fifoCostSync.subject')}</th><th>{t('fifoCostSync.kind')}</th><th>{t('fifoCostSync.status')}</th><th>{t('fifoCostSync.created')}</th><th /></tr></thead><tbody>{rows.map(value => <tr key={value.id}><td>{value.subject}</td><td>{t(`fifoCostSync.kinds.${value.kind}`)}</td><td>{statusText(value.status)}</td><td>{date(value.created_at)}</td><td><Button data-testid={`cost-publication-${value.id}`} variant="secondary" disabled={operation.busy} onClick={() => readPublication(value.id)}>{t('fifoCostSync.detail')}</Button></td></tr>)}</tbody></table></div>}
        {history && history.total > history.limit && <div className="opening-stock-pagination"><Button data-testid="cost-history-previous" variant="secondary" disabled={operation.busy || history.offset === 0} onClick={() => loadHistory(Math.max(0, history.offset - history.limit))}>{t('fifoCostSync.previous')}</Button><span>{t('fifoCostSync.historyPage', { first: history.offset + 1, last: history.offset + history.items.length, total: history.total })}</span><Button data-testid="cost-history-next" variant="secondary" disabled={operation.busy || history.offset + history.items.length >= history.total} onClick={() => loadHistory(history.offset + history.limit)}>{t('fifoCostSync.next')}</Button></div>}
      </section>
    </>}
    {publication && <section className="availability-run" data-testid="cost-publication-detail"><div className="opening-stock-actions"><h3>{t('fifoCostSync.publicationTitle', { subject: publication.subject })}</h3><span className="action-control"><Button data-testid="refresh-cost-publication" variant="secondary" disabled={operation.busy} onClick={() => readPublication(publication.id)}>{t('fifoCostSync.refreshDetail')}</Button><ActionScope effects={['hub-read']} /></span></div>
      <p><strong>{statusText(publication.status)}</strong> · {t(`fifoCostSync.kinds.${publication.kind}`)} · {shop === 'xtrek' ? 'xTrek' : 'BIKETREK'}</p>
      {publication.status === 'verified' && <p role="status" className="opening-stock-success">{t('fifoCostSync.verifiedAt', { date: date(publication.verified_at) })}</p>}
      {['queued', 'sending'].includes(publication.status) && <p role="status" className="opening-stock-notice">{t('fifoCostSync.awaitVerification')}</p>}
      {publication.error && <p role="alert" className="opening-stock-errors">{errorText(publication.error)} <code>{publication.error}</code></p>}
      <p>{t('fifoCostSync.netCurrency', { currency: publication.source.currency || 'EUR' })}</p>
      {publication.source.lines && <div className="opening-stock-table-scroll"><table><thead><tr><th>SKU</th><th>{t('fifoCostSync.quantity')}</th><th>{t('fifoCostSync.unitCost')}</th><th>{t('fifoCostSync.totalCost')}</th><th>{t('fifoCostSync.evidence')}</th></tr></thead><tbody>{publication.source.lines.map((line, index) => <tr key={`${line.line_key || line.product_id || index}-${index}`}><td>{line.sku || line.product_id || '—'}</td><td>{line.quantity}</td><td>{line.unit_cost}</td><td>{line.total_cost}</td><td><details><summary>{t('fifoCostSync.allocations', { count: line.allocations?.length || 0 })}</summary>{line.allocations?.map(slice => <p key={slice.id}>{t('fifoCostSync.layer', { id: slice.layer_id })}: {slice.quantity} × {slice.unit_cost_current ?? '—'} = {slice.total_cost_current ?? '—'} {publication.source.currency || 'EUR'}</p>)}</details></td></tr>)}</tbody></table></div>}
      {publication.kind === 'product' && <p>{t('fifoCostSync.nextUnitCost')}: <strong>{publication.source.unit_cost ?? '—'}</strong></p>}
      {publication.source.total_cost !== undefined && <p>{t('fifoCostSync.totalCost')}: <strong>{publication.source.total_cost}</strong></p>}
      {publication.remote_costs && <section data-testid="cost-remote-values"><h3>{t('fifoCostSync.remoteValues')}</h3><p>{t(publication.remote_costs.prices_with_vat_yn ? 'fifoCostSync.remoteWithVat' : 'fifoCostSync.remoteWithoutVat')}</p><div className="opening-stock-table-scroll"><table><thead><tr><th>SKU</th><th>{t('fifoCostSync.remoteBefore')}</th><th>{t('fifoCostSync.remoteDesired')}</th></tr></thead><tbody>{publication.remote_costs.lines.map(line => <tr key={line.line_key}><td>{line.code}</td><td>{line.before ?? '—'}</td><td>{line.desired}</td></tr>)}{publication.remote_costs.product && <tr><td>{publication.source.sku || publication.subject}</td><td>{publication.remote_costs.product.before ?? '—'}</td><td>{publication.remote_costs.product.desired}</td></tr>}</tbody></table></div></section>}
      {publication.status === 'prepared' && <><p>{t('fifoCostSync.previewOnly')}</p><p>{t(expired ? 'fifoCostSync.expired' : 'fifoCostSync.expires', { date: date(publication.expires_at) })}</p>
        <label className="availability-check"><input data-testid="confirm-order-cost" type="checkbox" disabled={activityBlocked || expired} checked={confirmed} onChange={event => setConfirmed(event.target.checked)} />{t('fifoCostSync.confirmSend', { order: publication.subject, shop: shop === 'xtrek' ? 'xTrek' : 'BIKETREK' })}</label>
        <span className="action-control"><Button data-testid="send-order-cost" disabled={!canSend} onClick={() => { if (!canSend || publication.expires_at && Date.parse(publication.expires_at) <= Date.now()) return; operation.run(true, () => sendFifoCostPublication(publication.id), acceptPublication, publication.id); }}>{t('fifoCostSync.send')}</Button><ActionScope effects={['hub-write', 'queued-upgates']} shop={shop} calls={{ kind: 'variable' }} /></span>
      </>}
      {['uncertain', 'sending'].includes(publication.status) && <div className="availability-recovery"><h3>{t('fifoCostSync.recoveryTitle')}</h3><p>{t('fifoCostSync.recoveryHelp')}</p>
        <label className="availability-check"><input data-testid="cost-request-settled" type="checkbox" disabled={blocked} checked={settled} onChange={event => setSettled(event.target.checked)} />{t('fifoCostSync.requestSettled')}</label>
        <label>{t('fifoCostSync.recoveryNote')}<input data-testid="cost-recovery-note" disabled={blocked} value={note} maxLength={500} onChange={event => setNote(event.target.value)} /></label>
        <span className="action-control"><Button data-testid="resolve-cost-publication" variant="secondary" disabled={blocked || !settled || note.trim().length < 5 || /[\x00-\x1f]/.test(note)} onClick={() => operation.run(true, () => resolveFifoCostPublication(publication.id, note.trim()), acceptPublication, publication.id)}>{t('fifoCostSync.resolve')}</Button><ActionScope effects={['hub-write', 'upgates-read']} shop={shop} calls={{ kind: 'variable' }} /></span>
      </div>}
    </section>}
  </section>;
}

export function FifoCostSyncPage() {
  const { t } = useTranslation();
  const [query] = useSearchParams(), revision = useSyncExternalStore(subscribeAccess, accessRevision);
  const [token, setToken] = useState(''), [shop, setShop] = useState(query.get('shop') === 'xtrek' ? 'xtrek' : 'biketrek'), [busy, setBusy] = useState(false);
  useEffect(() => { setToken(''); }, [revision]);
  return <div className="opening-stock availability-sync"><Link className="opening-stock-back" to="/settings">← {t('fifoCostSync.back')}</Link>
    <header><div><h1>{t('fifoCostSync.title')}</h1><p>{t('fifoCostSync.subtitle')}</p></div>{hubUnlocked() && <Button data-testid="lock-cost-sync" variant="secondary" onClick={() => unlockHub('')}>{t('fifoCostSync.lock')}</Button>}</header>
    <p><Link to="/stock">{t('fifoCostSync.stock')}</Link> · <Link to={`/settings/availability?shop=${shop}`}>{t('fifoCostSync.availability')}</Link></p>
    {!hubUnlocked() ? <form className="opening-stock-panel opening-stock-unlock" onSubmit={event => { event.preventDefault(); if (token.trim()) unlockHub(token.trim()); }}><p>{t('fifoCostSync.tokenHelp')}</p><label>{t('fifoCostSync.token')}<input data-testid="cost-token" type="password" autoComplete="off" value={token} onChange={event => setToken(event.target.value)} /></label><Button data-testid="unlock-cost-sync" type="submit" disabled={!token.trim()}>{t('fifoCostSync.unlock')}</Button></form> : <React.Fragment key={revision}><section className="opening-stock-panel availability-shop-selector"><label>{t('fifoCostSync.shop')}<select data-testid="cost-shop" disabled={busy} value={shop} onChange={event => setShop(event.target.value)}><option value="biketrek">BIKETREK</option><option value="xtrek">xTrek</option></select></label></section><CostPanel key={shop} shop={shop} onBusy={setBusy} /></React.Fragment>}
  </div>;
}
