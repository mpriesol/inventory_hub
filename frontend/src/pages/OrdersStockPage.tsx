import React, { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Button } from '../components/ui/Button.new';
import { accessRevision, hubUnlocked, subscribeAccess, unlockHub } from '../api/access';
import { applyOrderStock, configureOrderStock, getOrderStockOptions, OrderStockAction, OrderStockError,
  OrderStockOptions, OrderStockPreview, OrderStockPreviewInfo, previewOrderStock, recentOrderStock, recoverOrderStock } from '../api/orderStock';
import './OpeningStockPage.css';
import './OrdersStockPage.css';

type Operation = 'options' | 'configure' | 'preview' | 'recent' | 'recover' | 'apply' | null;
const actions: OrderStockAction[] = ['review', 'reserve', 'issue', 'cancel'];
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function OrdersStockPage() {
  const { t, i18n } = useTranslation();
  const [query] = useSearchParams();
  const revision = useSyncExternalStore(subscribeAccess, accessRevision);
  const [token, setToken] = useState('');
  const [shop, setShop] = useState(query.get('shop') === 'xtrek' ? 'xtrek' : 'biketrek');
  const [order, setOrder] = useState((query.get('order') || '').slice(0, 100));
  const [options, setOptions] = useState<OrderStockOptions | null>(null);
  const [warehouse, setWarehouse] = useState('');
  const [statusActions, setStatusActions] = useState<Record<string, OrderStockAction>>({});
  const [configDirty, setConfigDirty] = useState(false);
  const [configConfirmed, setConfigConfirmed] = useState(false);
  const [loaded, setLoaded] = useState<{ value: OrderStockPreview; revision: number } | null>(null);
  const preview = loaded?.revision === revision ? loaded.value : null;
  const [recent, setRecent] = useState<OrderStockPreviewInfo[] | null>(null);
  const [recoverId, setRecoverId] = useState('');
  const [errors, setErrors] = useState<OrderStockError[]>([]);
  const [error, setError] = useState('');
  const [confirmed, setConfirmed] = useState(false);
  const [physical, setPhysical] = useState(false);
  const [uncertain, setUncertain] = useState<'configure' | 'apply' | null>(null);
  const [busy, setBusy] = useState<Operation>(null);
  const [effectPage, setEffectPage] = useState(0);
  const [linePage, setLinePage] = useState(0);
  const [excludedPage, setExcludedPage] = useState(0);
  const generation = useRef(0);
  const controller = useRef<AbortController | null>(null);
  const busyRef = useRef<Operation>(null);
  const writeInFlight = useRef(false);
  const locked = busy === 'configure' || busy === 'apply' || !!uncertain;

  function invalidate() {
    generation.current += 1; controller.current?.abort(); controller.current = null;
    setLoaded(null); setConfirmed(false); setPhysical(false); setErrors([]); setError(''); setRecoverId('');
    setEffectPage(0); setLinePage(0); setExcludedPage(0); setBusy(null); busyRef.current = null;
  }
  useEffect(() => {
    invalidate(); setOptions(null); setWarehouse(''); setStatusActions({}); setRecent(null);
    setConfigConfirmed(false); setConfigDirty(false); setUncertain(null); setToken('');
    return () => { generation.current += 1; controller.current?.abort(); };
  }, [revision]);
  function report(value: unknown) {
    const code = (value as Error & { code?: string }).code || 'request_failed';
    if (code === 'hub_access_required' || code === 'hub_access_not_configured') unlockHub('');
    setError(code);
  }
  async function read<T>(operation: Operation, request: (signal: AbortSignal) => Promise<T>, accept: (data: T) => void) {
    if (!hubUnlocked() || busyRef.current) return;
    const id = ++generation.current, credential = accessRevision();
    controller.current?.abort(); const abort = new AbortController(); controller.current = abort;
    busyRef.current = operation; setBusy(operation); setError('');
    const current = () => generation.current === id && credential === accessRevision() && !abort.signal.aborted;
    try { const result = await request(abort.signal); if (current()) accept(result); }
    catch (value) { if (current()) report(value); }
    finally { if (current()) { busyRef.current = null; setBusy(null); } }
  }
  function acceptOptions(value: OrderStockOptions) {
    setOptions(value); setWarehouse(value.policy?.warehouse_code || '');
    const next = Object.fromEntries(value.statuses.map(status => {
      const saved = value.policy?.status_actions[String(status.id)];
      const allowed = value.allowed_actions[String(status.id)] || ['review'];
      return [String(status.id), saved && allowed.includes(saved) ? saved : 'review'];
    })) as Record<string, OrderStockAction>;
    const changed = !!value.policy && (Object.keys(value.policy.status_actions).length !== value.statuses.length
      || value.statuses.some(status => value.policy!.status_actions[String(status.id)] !== next[String(status.id)]));
    setStatusActions(next); setConfigConfirmed(false); setConfigDirty(changed); setUncertain(null);
  }
  function loadOptions() {
    if (busyRef.current || uncertain === 'apply') return;
    invalidate(); read('options', signal => getOrderStockOptions(shop, signal), acceptOptions);
  }
  function changeShop(value: string) {
    if (locked) return;
    invalidate(); setShop(value); setOptions(null); setWarehouse(''); setStatusActions({}); setRecent(null);
    setConfigConfirmed(false); setConfigDirty(false);
  }
  function changeConfig(nextWarehouse: string, nextActions: Record<string, OrderStockAction>) {
    if (locked) return;
    invalidate(); setWarehouse(nextWarehouse); setStatusActions(nextActions); setConfigConfirmed(false); setConfigDirty(true);
  }
  async function configure() {
    if (!options || !warehouse || !configConfirmed || busyRef.current || writeInFlight.current || uncertain) return;
    invalidate(); const id = generation.current, credential = accessRevision();
    writeInFlight.current = true; busyRef.current = 'configure'; setBusy('configure');
    const current = () => id === generation.current && credential === accessRevision();
    try {
      const result = await configureOrderStock({ shop_code: shop, warehouse_code: warehouse,
        status_hash: options.status_hash, status_actions: statusActions, confirmed: true });
      if (current()) acceptOptions(result);
    } catch (value) { if (current()) { setUncertain('configure'); setConfigConfirmed(false); report(value); } }
    finally { writeInFlight.current = false; if (current()) { busyRef.current = null; setBusy(null); } }
  }
  function acceptPreview(value: OrderStockPreview) {
    if (value.shop_code !== shop) { setError('preview_shop_mismatch'); return; }
    setLoaded({ value, revision: accessRevision() }); setRecoverId(value.id); setOrder(value.source.order_number);
    setConfirmed(false); setPhysical(false); setErrors(value.plan.errors || []); setUncertain(null);
    setEffectPage(0); setLinePage(0); setExcludedPage(0);
  }
  function prepare() {
    if (!options?.policy || configDirty || !order.trim() || busyRef.current || uncertain) return;
    invalidate(); const id = crypto.randomUUID(); setRecoverId(id);
    read('preview', signal => previewOrderStock({ request_id: id, shop_code: shop, order_number: order.trim() }, signal), result => {
      setErrors(result.errors || []);
      if (result.preview) acceptPreview(result.preview);
    });
  }
  function recover(id = recoverId) {
    if (!uuid.test(id.trim())) { setError('invalid_preview_id'); return; }
    read('recover', signal => recoverOrderStock(id.trim(), signal), acceptPreview);
  }
  async function apply() {
    if (!preview || preview.status !== 'prepared' || !preview.plan.ready || !confirmed || (preview.action === 'issue' && !physical)
        || configDirty || uncertain || busyRef.current || writeInFlight.current) return;
    if (new Date(preview.expires_at).getTime() <= Date.now()) { setError('order_stock_preview_expired'); return; }
    const selected = preview, id = generation.current, credential = accessRevision();
    writeInFlight.current = true; busyRef.current = 'apply'; setBusy('apply'); setError('');
    const current = () => id === generation.current && credential === accessRevision();
    try {
      const result = await applyOrderStock(selected, selected.action === 'issue' && physical);
      if (current()) { setLoaded({ value: { ...selected, status: 'completed', result }, revision: credential }); setConfirmed(false); setPhysical(false); }
    } catch (value) { if (current()) { setUncertain('apply'); setConfirmed(false); setPhysical(false); report(value); } }
    finally { writeInFlight.current = false; if (current()) { busyRef.current = null; setBusy(null); } }
  }
  const date = (value: string) => new Date(value).toLocaleString(i18n.language);
  const message = (code: string) => t(`orderStock.errors.${code}`, { defaultValue: t('orderStock.errors.request_failed') });
  function errorIdentity(item: OrderStockError) {
    const line = item.line_key ? preview?.source.lines.find(line => line.line_key === item.line_key) : undefined;
    const effect = item.product_id ? preview?.plan.effects.find(effect => effect.product_id === item.product_id) : undefined;
    return [...new Set([item.sku || line?.sku || line?.code || effect?.sku, line?.title].filter(Boolean))].join(' · ');
  }
  function pagination(count: number, page: number, change: (page: number) => void) {
    return count > 100 && <div className="opening-stock-pagination"><Button size="sm" variant="secondary" disabled={!page} onClick={() => change(page - 1)}>{t('orderStock.previous')}</Button>
      <span>{t('orderStock.page', { page: page + 1, pages: Math.ceil(count / 100) })}</span><Button size="sm" variant="secondary" disabled={(page + 1) * 100 >= count} onClick={() => change(page + 1)}>{t('orderStock.next')}</Button></div>;
  }
  const expired = preview && new Date(preview.expires_at).getTime() <= Date.now();

  return <div className="opening-stock order-stock">
    <Link to="/orders" className="opening-stock-back">← {t('orderStock.back')}</Link>
    <header><div><h1>{t('orderStock.title')}</h1><p>{t('orderStock.subtitle')}</p></div>{hubUnlocked() && <Button variant="secondary" onClick={() => { unlockHub(''); setError(''); }}>{t('orderStock.lock')}</Button>}</header>
    <div className="opening-stock-notice">{t('orderStock.scope')}</div>
    <p><Link to={`/orders/inbox?shop=${encodeURIComponent(shop)}`}>{t('orderCollection.open')}</Link> · <Link to={`/settings/stock?shop=${encodeURIComponent(shop)}`}>{t('stockSettings.open')}</Link></p>
    {!hubUnlocked() ? <form className="opening-stock-panel opening-stock-unlock" onSubmit={event => { event.preventDefault(); if (token.trim()) { setError(''); unlockHub(token.trim()); setToken(''); } }}>
      <p>{t('orderStock.tokenHelp')}</p><label>{t('orderStock.token')}<input type="password" autoComplete="off" value={token} onChange={event => setToken(event.target.value)} /></label>
      <Button type="submit" disabled={!token.trim()}>{t('orderStock.unlock')}</Button>
    </form> : <>
      <section className="opening-stock-panel">
        <div className="opening-stock-actions"><label>{t('orderStock.shop')}<select disabled={locked} value={shop} onChange={event => changeShop(event.target.value)}><option value="biketrek">BIKETREK</option><option value="xtrek">xTrek</option></select></label>
          <Button variant="secondary" disabled={!!busy || uncertain === 'apply'} onClick={loadOptions}>{t('orderStock.loadOptions')}</Button></div>
        {uncertain === 'configure' && <div className="opening-stock-notice" role="alert">{t('orderStock.configUncertain')}</div>}
        {options && <details className="order-stock-config" open={!options.policy || configDirty}>
          <summary>{t('orderStock.configTitle')}</summary>
          {options.policy ? <p>{t('orderStock.activeSince', { at: date(options.policy.starts_at), revision: options.policy.revision })}</p> : <p>{t('orderStock.cutoverHelp')}</p>}
          <label>{t('orderStock.warehouse')}<select value={warehouse} disabled={locked || !!options.policy} onChange={event => changeConfig(event.target.value, statusActions)}><option value="">{t('orderStock.chooseWarehouse')}</option>
            {options.warehouses.map(item => <option key={item.id} value={item.code}>{item.name}</option>)}
          </select></label>
          <div className="opening-stock-table-scroll"><table><thead><tr><th>{t('orderStock.status')}</th><th>{t('orderStock.operation')}</th><th>{t('orderStock.suggestion')}</th></tr></thead><tbody>
            {options.statuses.map(status => <tr key={status.id}><td>{status.name} <small>#{status.id}</small></td><td><select aria-label={`${t('orderStock.statusAction')} ${status.id} ${status.name}`} value={statusActions[String(status.id)] || 'review'} disabled={locked} onChange={event => changeConfig(warehouse, { ...statusActions, [String(status.id)]: event.target.value as OrderStockAction })}>
              {actions.filter(action => (options.allowed_actions[String(status.id)] || ['review']).includes(action)).map(action => <option key={action} value={action}>{t(`orderStock.actions.${action}`)}</option>)}</select></td><td>{t(`orderStock.actions.${options.suggested_actions[String(status.id)] || 'review'}`)}</td></tr>)}
          </tbody></table></div>
          <div className="opening-stock-confirmations"><label><input type="checkbox" checked={configConfirmed} disabled={locked} onChange={event => setConfigConfirmed(event.target.checked)} />{t('orderStock.configConfirm')}</label>
            <Button disabled={!!busy || !!uncertain || !warehouse || !configConfirmed} onClick={configure}>{t(options.policy ? 'orderStock.saveConfig' : 'orderStock.activate')}</Button></div>
        </details>}
      </section>
      <section className="opening-stock-panel">
        <h2>{t('orderStock.orderTitle')}</h2><div className="opening-stock-actions order-stock-order-input"><label className="opening-stock-grow">{t('orderStock.orderNumber')}<input maxLength={100} value={order} disabled={locked} onChange={event => { invalidate(); setOrder(event.target.value); }} /></label>
          <Button disabled={!options?.policy || configDirty || !!busy || !!uncertain || !order.trim()} onClick={prepare}>{t('orderStock.preview')}</Button></div>
        {configDirty && <p className="opening-stock-muted">{t('orderStock.saveBeforePreview')}</p>}
        <p className="opening-stock-muted">{t('orderStock.previewHelp')}</p>
        <p className="opening-stock-muted">{t('orderStock.posHelp')}</p>
      </section>
      <section className="opening-stock-panel">
        <div className="opening-stock-actions"><h2>{t('orderStock.recovery')}</h2><Button variant="secondary" disabled={!!busy} onClick={() => read('recent', signal => recentOrderStock(shop, signal), result => setRecent(result.previews))}>{t('orderStock.loadRecent')}</Button></div>
        <div className="opening-stock-actions order-stock-order-input"><label className="opening-stock-grow">{t('orderStock.previewId')}<input value={recoverId} disabled={locked} onChange={event => { invalidate(); setRecoverId(event.target.value); }} /></label>
          <Button variant="secondary" disabled={!!busy || !recoverId.trim() || uncertain === 'configure'} onClick={() => recover()}>{t('orderStock.recover')}</Button></div>
        {recent && <div className="opening-stock-recent">{!recent.length && <p>{t('orderStock.noRecent')}</p>}{recent.map(item => <div key={item.id}><div><strong>{item.order_number || item.source?.order_number}</strong><small>{date(item.created_at)} · {t(`orderStock.actions.${item.action}`)} · {t(`orderStock.previewStatus.${item.status}`)}</small></div><Button size="sm" variant="secondary" disabled={!!busy || !!uncertain} onClick={() => recover(item.id)}>{t('orderStock.openPreview')}</Button></div>)}</div>}
      </section>
      {!!errors.length && <div className="opening-stock-errors" role="alert"><strong>{t('orderStock.planErrors')}</strong><ul>{errors.slice(0, 100).map((item, index) => <li key={index}>{message(item.code)}{errorIdentity(item) && <strong> · {errorIdentity(item)}</strong>}</li>)}</ul>{errors.length > 100 && <p>{t('orderStock.moreErrors', { count: errors.length - 100 })}</p>}</div>}
      {preview && <section className="opening-stock-panel" aria-live="polite">
        <div className="opening-stock-actions"><h2>{preview.source.order_number} · {t(`orderStock.actions.${preview.action}`)}</h2><span className="opening-stock-badge">{t(`orderStock.previewStatus.${preview.status}`)}</span></div>
        <p>{preview.shop_code === 'biketrek' ? 'BIKETREK' : 'xTrek'} · {preview.warehouse.name}</p><p className="opening-stock-muted">{t('orderStock.expiresAt')}: {date(preview.expires_at)} · {t('orderStock.policyRevision')}: {preview.policy_revision}</p>
        <p>{t('orderStock.noPartialIssue')}</p>
        <div className="opening-stock-table-scroll"><table><thead><tr>{['sku', 'onHand', 'reserved', 'shortage', 'issueQuantity', 'avgCost', 'issueCost', 'stockValue'].map(key => <th key={key}>{t(`orderStock.${key}`)}</th>)}</tr></thead><tbody>
          {preview.plan.effects.slice(effectPage * 100, (effectPage + 1) * 100).map(item => <tr key={item.product_id}><td><code>{item.sku}</code>{item.value_complete === false && <small className="block">{t('stock.unknownValue')}</small>}</td><td>{item.qty_on_hand} → {item.qty_on_hand_after}</td><td>{item.qty_reserved} → {item.qty_reserved_after}</td><td>{item.shortage}</td><td>{item.issue_quantity}</td><td>{item.avg_cost == null ? t('stock.unknownValue') : `${item.avg_cost} EUR`}</td><td>{item.issue_cost == null ? t('stock.unknownValue') : `${item.issue_cost} EUR`}</td><td>{item.total_value == null ? t('stock.unknownValue') : `${item.total_value} EUR`} → {item.total_value_after == null ? t('stock.unknownValue') : `${item.total_value_after} EUR`}</td></tr>)}
        </tbody></table></div>{pagination(preview.plan.effects.length, effectPage, setEffectPage)}
        <details className="order-stock-lines"><summary>{t('orderStock.lineDetails')}</summary><div className="opening-stock-table-scroll"><table><thead><tr>{['sku', 'quantity', 'allocation', 'shortage'].map(key => <th key={key}>{t(`orderStock.${key}`)}</th>)}</tr></thead><tbody>
          {preview.plan.lines.slice(linePage * 100, (linePage + 1) * 100).map(item => <tr key={item.line_key}><td><code>{item.sku}</code></td><td>{item.quantity}</td><td>{item.old_allocation} → {item.allocation}</td><td>{item.shortage}</td></tr>)}
        </tbody></table></div>{pagination(preview.plan.lines.length, linePage, setLinePage)}</details>
        {!!preview.plan.excluded_lines.length && <div className="order-stock-excluded"><h3>{t('orderStock.excluded')}</h3><p>{t('orderStock.excludedHelp')}</p><ul>{preview.plan.excluded_lines.slice(excludedPage * 100, (excludedPage + 1) * 100).map((item, index) => <li key={`${item.line_key}:${index}`}><strong>{item.title || item.name || item.code || item.line_key}</strong> {item.code && <code>{item.code}</code>} · {t(`orderStock.excludedTypes.${item.classification || 'manual'}`, { defaultValue: t('orderStock.excludedTypes.manual') })}</li>)}</ul>{pagination(preview.plan.excluded_lines.length, excludedPage, setExcludedPage)}</div>}
        {preview.status === 'completed' ? <div className="opening-stock-success"><strong>{t('orderStock.completed')}</strong><p>{t('orderStock.completedHelp')}</p>{preview.result && <p>{t('orderStock.movements', { count: preview.result.movements_created })}</p>}</div> : uncertain === 'apply' ? <div className="opening-stock-notice" role="alert"><p>{t('orderStock.applyUncertain')}</p><Button variant="secondary" disabled={!!busy} onClick={() => recover(preview.id)}>{t('orderStock.recover')}</Button></div> : <>
          {(!preview.plan.ready || expired) && <div className="opening-stock-notice">{t(expired ? 'orderStock.expired' : 'orderStock.blocked')}</div>}
          <div className="opening-stock-confirmations"><label><input type="checkbox" checked={confirmed} disabled={!!busy || !preview.plan.ready || !!expired || configDirty} onChange={event => setConfirmed(event.target.checked)} />{t('orderStock.applyConfirm')}</label>
            {preview.action === 'issue' && <label><input type="checkbox" checked={physical} disabled={!!busy || !preview.plan.ready || !!expired || configDirty} onChange={event => setPhysical(event.target.checked)} />{t('orderStock.physicalConfirm')}</label>}
            <Button disabled={!!busy || !preview.plan.ready || !!expired || !confirmed || (preview.action === 'issue' && !physical) || configDirty} loading={busy === 'apply'} onClick={apply}>{t('orderStock.apply')}</Button></div>
        </>}
      </section>}
    </>}{error && <div className="opening-stock-errors" role="alert">{message(error)}</div>}
  </div>;
}
