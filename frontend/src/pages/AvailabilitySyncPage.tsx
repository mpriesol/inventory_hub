import React, { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Button } from '../components/ui/Button.new';
import { ActionScope } from '../components/ui/ActionScope';
import { accessRevision, hubUnlocked, subscribeAccess, unlockHub } from '../api/access';
import { getSupplierAvailability, runSupplierAvailability, saveSupplierAvailability, SupplierAvailabilitySettings } from '../api/supplierAvailability';
import { getStockSyncOptions, getStockSyncRun, resolveStockSyncItem, runStockSync, saveStockSyncSettings, saveStockSyncWarehouse,
  STOCK_SYNC_FIELDS, StockSyncField, StockSyncOptions, StockSyncRunDetail, StockSyncValues } from '../api/stockSync';
import './OpeningStockPage.css';
import './AvailabilitySyncPage.css';

const integer = (value: string, min: number, max: number) => /^\d+$/.test(value) && Number.isSafeInteger(Number(value)) && Number(value) >= min && Number(value) <= max;
const errorCode = (error: unknown) => (error as { code?: string })?.code || 'request_failed';

/** Reject stale responses after access changes and suppress duplicate mutations. No automatic write retries. */
function useOperation() {
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [uncertain, setUncertain] = useState(false);
  const running = useRef(false), generation = useRef(0), abort = useRef<AbortController | null>(null);
  useEffect(() => () => { generation.current++; abort.current?.abort(); }, []);
  async function run<T>(write: boolean, request: (signal: AbortSignal) => Promise<T>, accept: (value: T) => void) {
    if (running.current || !hubUnlocked()) return;
    running.current = true; setBusy(true); setError('');
    const id = ++generation.current, credential = accessRevision(), controller = new AbortController(); abort.current = controller;
    const current = () => id === generation.current && credential === accessRevision() && !controller.signal.aborted;
    try { const value = await request(controller.signal); if (current()) accept(value); }
    catch (value) { if (current()) { if (write) setUncertain(true); setError(errorCode(value));
      if (['hub_access_required', 'hub_access_not_configured'].includes(errorCode(value))) unlockHub('');
    } }
    finally { if (current()) { running.current = false; setBusy(false); } }
  }
  return { busy, error, uncertain, run, recovered: () => setUncertain(false) };
}

function Feedback({ error, uncertain }: { error: string; uncertain: boolean }) {
  const { t } = useTranslation();
  return <>{uncertain && <p role="alert" className="opening-stock-notice">{t('availabilitySync.uncertain')}</p>}
    {error && <p role="alert" className="opening-stock-errors">{t(`availabilitySync.errors.${error}`, { defaultValue: t('availabilitySync.requestFailed') })} <code>{error}</code></p>}</>;
}

function SupplierCard({ value, replace, onBusy }: { value: SupplierAvailabilitySettings; replace: (value: SupplierAvailabilitySettings) => void; onBusy: (supplier: string, busy: boolean) => void }) {
  const { t, i18n } = useTranslation();
  const operation = useOperation();
  const [enabled, setEnabled] = useState(value.enabled), [feed, setFeed] = useState(value.feed_key);
  const [interval, setInterval] = useState(String(value.interval_seconds)), [freshness, setFreshness] = useState(String(value.freshness_seconds)), [coverage, setCoverage] = useState(String(value.min_coverage_percent));
  const [saved, setSaved] = useState(false), [queued, setQueued] = useState(false);
  useEffect(() => { setEnabled(value.enabled); setFeed(value.feed_key); setInterval(String(value.interval_seconds)); setFreshness(String(value.freshness_seconds)); setCoverage(String(value.min_coverage_percent)); operation.recovered(); setQueued(false); }, [value]);
  useEffect(() => { onBusy(value.supplier, operation.busy); return () => onBusy(value.supplier, false); }, [operation.busy, value.supplier]);
  const dirty = enabled !== value.enabled || feed !== value.feed_key || interval !== String(value.interval_seconds) || freshness !== String(value.freshness_seconds) || coverage !== String(value.min_coverage_percent);
  const valid = value.feed_keys.includes(feed) && integer(interval, 300, 604800) && integer(freshness, 300, 2592000) && Number(freshness) >= Number(interval) && integer(coverage, 1, 100);
  const date = (stamp: string | null) => stamp ? new Date(stamp).toLocaleString(i18n.language) : '—';
  const stale = value.last_success_at ? Date.now() - new Date(value.last_success_at).getTime() > value.freshness_seconds * 1000 : true;
  const blocked = operation.busy || operation.uncertain;
  const id = (suffix: string) => `supplier-${value.supplier}-${suffix}`;
  function save() {
    if (!valid || blocked) return;
    setSaved(false);
    operation.run(true, () => saveSupplierAvailability(value.supplier, { expected_revision: value.revision, enabled, feed_key: feed, interval_seconds: Number(interval), freshness_seconds: Number(freshness), min_coverage_percent: Number(coverage) }), result => { replace(result); setSaved(true); });
  }
  return <article className={`availability-supplier${dirty ? ' availability-dirty' : ''}`} data-testid={id('card')}>
    <header><h3>{value.name}</h3><span className="opening-stock-badge">{t(`availabilitySync.${value.running ? 'running' : value.manual_requested_at || queued ? 'queued' : stale ? 'stale' : 'fresh'}`)}</span></header>
    <p>{t('availabilitySync.supplierLabels', { available: value.availability?.orderable || 'do 5 dní', unknown: value.availability?.unknown || 'overíme' })}</p>
    <p><Link to="/suppliers">{t('availabilitySync.supplierConfig')}</Link></p>
    <div className="availability-fields">
      <label>{t('availabilitySync.feed')}<select data-testid={id('feed')} value={feed} disabled={blocked} onChange={event => { setFeed(event.target.value); setSaved(false); }}>{value.feed_keys.length === 0 && <option value="">{t('availabilitySync.noFeeds')}</option>}{value.feed_keys.map(key => <option key={key} value={key}>{key}</option>)}</select></label>
      <label>{t('availabilitySync.interval')}<input data-testid={id('interval')} type="number" min="300" max="604800" step="1" value={interval} disabled={blocked} onChange={event => { setInterval(event.target.value); setSaved(false); }} /><small>{t('availabilitySync.rangeSeconds', { min: 300, max: 604800 })}</small></label>
      <label>{t('availabilitySync.freshness')}<input data-testid={id('freshness')} type="number" min="300" max="2592000" step="1" value={freshness} disabled={blocked} onChange={event => { setFreshness(event.target.value); setSaved(false); }} /><small>{t('availabilitySync.freshnessHelp')}</small></label>
      <label>{t('availabilitySync.coverage')}<input data-testid={id('coverage')} type="number" min="1" max="100" step="1" value={coverage} disabled={blocked} onChange={event => { setCoverage(event.target.value); setSaved(false); }} /><small>{t('availabilitySync.coverageHelp')}</small></label>
    </div>
    <label className="availability-check"><input data-testid={id('enabled')} type="checkbox" checked={enabled} disabled={blocked} onChange={event => { setEnabled(event.target.checked); setSaved(false); }} />{t('availabilitySync.supplierAuto')}</label>
    <dl className="availability-status"><div><dt>{t('availabilitySync.lastSuccess')}</dt><dd>{date(value.last_success_at)}</dd></div><div><dt>{t('availabilitySync.nextRun')}</dt><dd>{value.enabled ? date(value.next_run_at) : t('availabilitySync.automaticOff')}</dd></div><div><dt>{t('availabilitySync.itemCount')}</dt><dd>{value.last_item_count ?? '—'}</dd></div></dl>
    {value.last_error && <p className="opening-stock-errors" role="alert">{t('availabilitySync.lastError')}: {t(`availabilitySync.errors.${value.last_error}`, { defaultValue: value.last_error })}</p>}
    {dirty && <p className="availability-change" role="status">{t('availabilitySync.unsaved')}</p>}
    {saved && !dirty && <p role="status" className="opening-stock-success">{t('availabilitySync.saved')}</p>}
    {queued && <p role="status">{t('availabilitySync.supplierQueued')}</p>}
    <Feedback error={operation.error} uncertain={operation.uncertain} />
    <div className="availability-actions"><span className="action-control"><Button data-testid={id('save')} disabled={blocked || !valid || (!dirty && value.revision > 0)} onClick={save}>{t('availabilitySync.save')}</Button><ActionScope effects={enabled ? ['hub-write', 'queued-supplier'] : ['hub-write']} calls={{ kind: 'variable' }} /></span>
      <span className="action-control"><Button data-testid={id('run')} variant="secondary" disabled={blocked || dirty || value.revision === 0 || value.running || !!value.manual_requested_at || queued} onClick={() => operation.run(true, () => runSupplierAvailability(value.supplier, value.revision), () => setQueued(true))}>{t('availabilitySync.refreshSupplier')}</Button><ActionScope effects={['hub-write', 'queued-supplier']} calls={{ kind: 'variable' }} /></span></div>
  </article>;
}

function SuppliersPanel() {
  const { t } = useTranslation();
  const operation = useOperation();
  const [suppliers, setSuppliers] = useState<SupplierAvailabilitySettings[] | null>(null), [writing, setWriting] = useState<Set<string>>(new Set());
  function onBusy(supplier: string, busy: boolean) { setWriting(previous => { const next = new Set(previous); busy ? next.add(supplier) : next.delete(supplier); return next; }); }
  return <section className="opening-stock-panel"><div className="opening-stock-actions"><h2>{t('availabilitySync.suppliersTitle')}</h2><span className="action-control"><Button data-testid="load-suppliers" variant="secondary" disabled={operation.busy || writing.size > 0} onClick={() => operation.run(false, signal => getSupplierAvailability(signal), result => { setSuppliers(result.suppliers); operation.recovered(); })}>{t('availabilitySync.loadSuppliers')}</Button><ActionScope effects={['hub-read']} /></span></div>
    <p>{t('availabilitySync.suppliersHelp')}</p><Feedback error={operation.error} uncertain={false} />
    {suppliers?.length === 0 && <p>{t('availabilitySync.noSuppliers')}</p>}
    <div className="availability-suppliers">{suppliers?.map(value => <SupplierCard key={value.supplier} value={value} replace={result => setSuppliers(previous => previous!.map(item => item.supplier === result.supplier ? result : item))} onBusy={onBusy} />)}</div>
  </section>;
}

type Values = Record<StockSyncField, string>;
const valueInputs = (value: StockSyncValues): Values => ({ interval_seconds: String(value.interval_seconds), batch_size: String(value.batch_size), max_order_age_seconds: String(value.max_order_age_seconds) });
const validValues = (values: Values) => STOCK_SYNC_FIELDS.every(field => integer(values[field.key], field.min, field.max));
const numberValues = (values: Values): StockSyncValues => ({ interval_seconds: Number(values.interval_seconds), batch_size: Number(values.batch_size), max_order_age_seconds: Number(values.max_order_age_seconds) });

function StockPanel({ shop, onBusy }: { shop: string; onBusy: (busy: boolean) => void }) {
  const { t, i18n } = useTranslation();
  const operation = useOperation();
  useEffect(() => { onBusy(operation.busy); return () => onBusy(false); }, [operation.busy]);
  const [options, setOptions] = useState<StockSyncOptions | null>(null), [warehouse, setWarehouse] = useState<Values>({ interval_seconds: '', batch_size: '', max_order_age_seconds: '' });
  const [overrides, setOverrides] = useState<Partial<Values>>({}), [enabled, setEnabled] = useState(false), [authorized, setAuthorized] = useState(false);
  const [authority, setAuthority] = useState(false), [writersDisabled, setWritersDisabled] = useState(false), [ordersReconciled, setOrdersReconciled] = useState(false);
  const [saved, setSaved] = useState(''), [reloadRequired, setReloadRequired] = useState(false), [run, setRun] = useState<StockSyncRunDetail | null>(null), [finished, setFinished] = useState<Record<number, boolean>>({});
  const date = (stamp: string | null) => stamp ? new Date(stamp).toLocaleString(i18n.language) : '—';
  const blocked = operation.busy || operation.uncertain;
  function clear() { setSaved(''); setAuthority(false); setWritersDisabled(false); setOrdersReconciled(false); }
  function accept(value: StockSyncOptions) { setOptions(value); if (value.warehouse_settings) setWarehouse(valueInputs(value.warehouse_settings));
    setOverrides(Object.fromEntries(STOCK_SYNC_FIELDS.filter(field => value.settings[field.key] !== null).map(field => [field.key, String(value.settings[field.key])])));
    setEnabled(value.settings.enabled); setAuthorized(value.settings.authorized); setReloadRequired(false); clear(); operation.recovered();
  }
  const effective = options?.warehouse_settings ? { ...valueInputs(options.warehouse_settings), ...overrides } : warehouse;
  const needsAuthority = authorized && (!options?.settings.authorized || options.blockers.some(code => ['stock_sync_authority_changed', 'stock_sync_target_changed'].includes(code)));
  const canAuthorize = !needsAuthority || (authority && writersDisabled && ordersReconciled);
  const warehouseDirty = !!options?.warehouse_settings && STOCK_SYNC_FIELDS.some(({ key }) => warehouse[key] !== String(options.warehouse_settings![key]));
  const stockDirty = !!options && (enabled !== options.settings.enabled || authorized !== options.settings.authorized || STOCK_SYNC_FIELDS.some(({ key }) => (overrides[key] ?? null) !== (options.settings[key] === null ? null : String(options.settings[key]))));
  function fields(target: 'warehouse' | 'shop') { return <div className="availability-fields">{STOCK_SYNC_FIELDS.map(field => {
    const inherited = overrides[field.key] === undefined;
    return <div key={field.key}><label>{t(`availabilitySync.fields.${field.key}`)}<input data-testid={`sync-${target}-${field.key}`} type="number" min={field.min} max={field.max} step="1" disabled={blocked || (target === 'shop' && inherited)} value={target === 'warehouse' ? warehouse[field.key] : effective[field.key]} onChange={event => { clear(); const value = event.target.value; target === 'warehouse' ? setWarehouse(previous => ({ ...previous, [field.key]: value })) : setOverrides(previous => ({ ...previous, [field.key]: value })); }} /><small>{t('availabilitySync.range', { min: field.min, max: field.max })}</small></label>
      {target === 'shop' && <label className="availability-check"><input data-testid={`sync-inherit-${field.key}`} type="checkbox" checked={inherited} disabled={blocked} onChange={event => { clear(); setOverrides(previous => { const next = { ...previous }; if (event.target.checked) delete next[field.key]; else next[field.key] = String(options?.warehouse_settings?.[field.key] ?? ''); return next; }); }} />{t('availabilitySync.inherit')}</label>}</div>;
  })}</div>; }
  return <section className="opening-stock-panel"><div className="opening-stock-actions"><h2>{t('availabilitySync.stockTitle', { shop: shop === 'xtrek' ? 'xTrek' : 'BIKETREK' })}</h2><span className="action-control"><Button data-testid="load-stock-sync" variant="secondary" disabled={operation.busy} onClick={() => operation.run(false, signal => getStockSyncOptions(shop, signal), accept)}>{t('availabilitySync.loadStock')}</Button><ActionScope effects={['hub-read']} /></span></div>
    <p>{t('availabilitySync.stockHelp')}</p><p><Link to={`/settings/stock?shop=${shop}`}>{t('availabilitySync.orderSettings')}</Link> · <Link to={`/orders/inbox?shop=${shop}`}>{t('availabilitySync.orderInbox')}</Link></p>
    <Feedback error={operation.error} uncertain={operation.uncertain} />
    {saved && <p role="status" className="opening-stock-success">{t(`availabilitySync.${saved}`)}</p>}
    {options && <>
      {!!options.blockers.length && <div role="status" className="opening-stock-notice"><strong>{t('availabilitySync.notReady')}</strong><ul>{options.blockers.map(code => <li key={code}>{t(`availabilitySync.errors.${code}`, { defaultValue: code })}</li>)}</ul></div>}
      {!options.server_write_enabled && <p className="opening-stock-notice">{t('availabilitySync.serverDisabled')}</p>}
      {options.warehouse && options.warehouse_settings && <>
        <details className="availability-warehouse"><summary>{t('availabilitySync.warehouseTitle', { name: options.warehouse.name })}</summary><p>{t('availabilitySync.warehouseHelp')}</p>{fields('warehouse')}{warehouseDirty && <p role="status" className="availability-change">{t('availabilitySync.unsaved')}</p>}
          <span className="action-control"><Button data-testid="save-sync-warehouse" disabled={blocked || reloadRequired || !warehouseDirty || !validValues(warehouse)} onClick={() => operation.run(true, () => saveStockSyncWarehouse({ warehouse_code: options.warehouse!.code, expected_revision: options.warehouse_settings!.revision, ...numberValues(warehouse), confirmed: true }), () => { setReloadRequired(true); clear(); setSaved('warehouseSaved'); })}>{t('availabilitySync.saveWarehouse')}</Button><ActionScope effects={['hub-write']} /></span></details>
        <h3>{t('availabilitySync.shopSettings')}</h3>{reloadRequired && <p role="status" className="opening-stock-notice">{t('availabilitySync.reloadRequired')}</p>}{fields('shop')}
        <label className="availability-check"><input data-testid="sync-authorized" type="checkbox" checked={authorized} disabled={blocked || reloadRequired} onChange={event => { clear(); setAuthorized(event.target.checked); if (!event.target.checked) setEnabled(false); }} />{t('availabilitySync.authorized')}</label>
        <label className="availability-check"><input data-testid="sync-enabled" type="checkbox" checked={enabled} disabled={blocked || reloadRequired || !authorized} onChange={event => { clear(); setEnabled(event.target.checked); }} />{t('availabilitySync.stockAuto')}</label>
        {needsAuthority && <div className="opening-stock-notice availability-authority"><p>{t('availabilitySync.authorityHelp')}</p><label className="availability-check"><input data-testid="sync-authority" type="checkbox" checked={authority} disabled={blocked} onChange={event => setAuthority(event.target.checked)} />{t('availabilitySync.authority')}</label><label className="availability-check"><input data-testid="sync-writers-disabled" type="checkbox" checked={writersDisabled} disabled={blocked} onChange={event => setWritersDisabled(event.target.checked)} />{t('availabilitySync.writersDisabled')}</label><label className="availability-check"><input data-testid="sync-orders-reconciled" type="checkbox" checked={ordersReconciled} disabled={blocked} onChange={event => setOrdersReconciled(event.target.checked)} />{t('availabilitySync.ordersReconciled')}</label></div>}
        {stockDirty && <p role="status" className="availability-change">{t('availabilitySync.unsaved')}</p>}
        <div className="availability-actions"><span className="action-control"><Button data-testid="save-stock-sync" disabled={blocked || reloadRequired || warehouseDirty || (!stockDirty && !needsAuthority) || !validValues(effective) || !canAuthorize || (enabled && !authorized)} onClick={() => operation.run(true, () => saveStockSyncSettings({ shop_code: shop, expected_revision: options.settings.revision, enabled, authorized, interval_seconds: overrides.interval_seconds === undefined ? null : Number(overrides.interval_seconds), batch_size: overrides.batch_size === undefined ? null : Number(overrides.batch_size), max_order_age_seconds: overrides.max_order_age_seconds === undefined ? null : Number(overrides.max_order_age_seconds), hub_is_stock_authority: authority, external_stock_writers_disabled: writersDisabled, orders_reconciled: ordersReconciled, confirmed: true }), result => { accept(result); setSaved('saved'); })}>{t('availabilitySync.saveShop')}</Button><ActionScope effects={enabled ? ['hub-write', 'queued-upgates'] : ['hub-write']} shop={shop} calls={{ kind: 'variable' }} /></span>
          <span className="action-control"><Button data-testid="run-stock-sync" variant="secondary" disabled={blocked || reloadRequired || warehouseDirty || stockDirty || !options.settings.authorized || !options.server_write_enabled || !!options.blockers.length || !!run && ['queued', 'pending', 'running', 'preparing'].includes(run.status)} onClick={() => operation.run(true, () => runStockSync(shop), result => { setRun(result); setFinished({}); setSaved('stockQueued'); })}>{t('availabilitySync.runStock')}</Button><ActionScope effects={['hub-write', 'queued-upgates']} shop={shop} calls={{ kind: 'variable' }} /></span></div>
      </>}
      <dl className="availability-status"><div><dt>{t('availabilitySync.lastCompleted')}</dt><dd>{date(options.settings.last_completed_at)}</dd></div><div><dt>{t('availabilitySync.nextRun')}</dt><dd>{options.settings.enabled ? date(options.settings.next_run_at) : t('availabilitySync.automaticOff')}</dd></div></dl>
      {options.settings.last_error && <p role="alert" className="opening-stock-errors">{t('availabilitySync.lastError')}: {t(`availabilitySync.errors.${options.settings.last_error}`, { defaultValue: options.settings.last_error })}</p>}
      <h3>{t('availabilitySync.recentRuns')}</h3>{options.runs.length === 0 && <p>{t('availabilitySync.noRuns')}</p>}
      {options.runs.length > 0 && <div className="opening-stock-table-scroll"><table><thead><tr><th>{t('availabilitySync.run')}</th><th>{t('availabilitySync.status')}</th><th>{t('availabilitySync.verified')}</th><th>{t('availabilitySync.failed')}</th><th>{t('availabilitySync.started')}</th><th /></tr></thead><tbody>{options.runs.map(item => <tr key={item.id}><td>#{item.id}</td><td>{t(`availabilitySync.statuses.${item.status}`, { defaultValue: item.status })}</td><td>{item.counts.verified}</td><td>{item.counts.failed + item.counts.uncertain}</td><td>{date(item.started_at)}</td><td><Button data-testid={`sync-run-${item.id}`} variant="secondary" disabled={operation.busy} onClick={() => operation.run(false, signal => getStockSyncRun(item.id, signal), value => { setRun(value); setFinished({}); })}>{t('availabilitySync.detail')}</Button></td></tr>)}</tbody></table></div>}
    </>}
    {run && <section className="availability-run" data-testid="sync-run-detail"><div className="opening-stock-actions"><h3>{t('availabilitySync.runDetail', { id: run.id })}</h3><span className="action-control"><Button data-testid="refresh-sync-run" variant="secondary" disabled={operation.busy} onClick={() => operation.run(false, signal => getStockSyncRun(run.id, signal, run.items_offset ?? 0), value => { setRun(value); setFinished({}); })}>{t('availabilitySync.refreshRun')}</Button><ActionScope effects={['hub-read']} /></span></div>
      <p>{t('availabilitySync.runSummary', { status: t(`availabilitySync.statuses.${run.status}`, { defaultValue: run.status }), ...run.counts })}</p>{run.more_pending && <p>{t('availabilitySync.morePending')}</p>}{run.items_truncated && <p>{t('availabilitySync.itemsTruncated')}</p>}
      {run.error && <p className="opening-stock-errors">{t('availabilitySync.lastError')}: {t(`availabilitySync.errors.${run.error}`, { defaultValue: run.error })}</p>}
      <div className="opening-stock-table-scroll"><table><thead><tr><th>SKU</th><th>{t('availabilitySync.status')}</th><th>{t('availabilitySync.stock')}</th><th>{t('availabilitySync.availability')}</th><th>{t('availabilitySync.recovery')}</th></tr></thead><tbody>{run.items.map(item => <tr key={item.id}><td><code>{item.sku}</code></td><td>{t(`availabilitySync.statuses.${item.status}`, { defaultValue: item.status })}{item.error && <small className="availability-error-code">{t(`availabilitySync.errors.${item.error}`, { defaultValue: item.error })}</small>}</td><td>{item.desired?.stock ?? '—'}</td><td>{item.desired?.availability ?? '—'}</td><td>{item.status === 'uncertain' && <div className="availability-recovery"><p>{t('availabilitySync.recoveryHelp')}</p><label className="availability-check"><input data-testid={`sync-finished-${item.id}`} type="checkbox" disabled={blocked} checked={!!finished[item.id]} onChange={event => setFinished(previous => ({ ...previous, [item.id]: event.target.checked }))} />{t('availabilitySync.requestFinished')}</label><span className="action-control"><Button data-testid={`resolve-sync-${item.id}`} variant="secondary" disabled={blocked || !finished[item.id]} onClick={() => operation.run(true, () => resolveStockSyncItem(item.id), value => { setRun(value); setFinished({}); })}>{t('availabilitySync.resolve')}</Button><ActionScope effects={['hub-write', 'upgates-read']} shop={shop} calls={{ kind: 'variable' }} /></span></div>}</td></tr>)}</tbody></table></div>
      {(run.items_total ?? run.items.length) > (run.items_limit ?? 100) && <div className="opening-stock-pagination"><Button data-testid="sync-items-previous" variant="secondary" disabled={operation.busy || !(run.items_offset ?? 0)} onClick={() => operation.run(false, signal => getStockSyncRun(run.id, signal, Math.max(0, (run.items_offset ?? 0) - (run.items_limit ?? 100)), run.items_limit ?? 100), value => { setRun(value); setFinished({}); })}>{t('availabilitySync.previous')}</Button><span>{t('availabilitySync.itemPage', { first: (run.items_offset ?? 0) + 1, last: (run.items_offset ?? 0) + run.items.length, total: run.items_total ?? run.items.length })}</span><Button data-testid="sync-items-next" variant="secondary" disabled={operation.busy || (run.items_offset ?? 0) + run.items.length >= (run.items_total ?? run.items.length)} onClick={() => operation.run(false, signal => getStockSyncRun(run.id, signal, (run.items_offset ?? 0) + (run.items_limit ?? 100), run.items_limit ?? 100), value => { setRun(value); setFinished({}); })}>{t('availabilitySync.next')}</Button></div>}
    </section>}
  </section>;
}

export function AvailabilitySyncPage() {
  const { t } = useTranslation();
  const [query] = useSearchParams();
  const revision = useSyncExternalStore(subscribeAccess, accessRevision);
  const [token, setToken] = useState(''), [shop, setShop] = useState(query.get('shop') === 'xtrek' ? 'xtrek' : 'biketrek'), [stockBusy, setStockBusy] = useState(false);
  useEffect(() => { setToken(''); }, [revision]);
  return <div className="opening-stock availability-sync"><Link className="opening-stock-back" to="/settings">← {t('availabilitySync.back')}</Link><header><div><h1>{t('availabilitySync.title')}</h1><p>{t('availabilitySync.subtitle')}</p></div>{hubUnlocked() && <Button variant="secondary" data-testid="lock-sync" onClick={() => unlockHub('')}>{t('availabilitySync.lock')}</Button>}</header>
    <p><Link to={`/settings/purchase-costs?shop=${shop}`}>{t('fifoCostSync.title')}</Link></p>
    {!hubUnlocked() ? <form className="opening-stock-panel opening-stock-unlock" onSubmit={event => { event.preventDefault(); if (token.trim()) unlockHub(token.trim()); }}><p>{t('availabilitySync.tokenHelp')}</p><label>{t('availabilitySync.token')}<input data-testid="sync-token" type="password" autoComplete="off" value={token} onChange={event => setToken(event.target.value)} /></label><Button data-testid="unlock-sync" type="submit" disabled={!token.trim()}>{t('availabilitySync.unlock')}</Button></form> : <React.Fragment key={revision}><SuppliersPanel /><section className="opening-stock-panel availability-shop-selector"><label>{t('availabilitySync.shop')}<select data-testid="sync-shop" value={shop} disabled={stockBusy} onChange={event => setShop(event.target.value)}><option value="biketrek">BIKETREK</option><option value="xtrek">xTrek</option></select></label></section><StockPanel key={shop} shop={shop} onBusy={setStockBusy} /></React.Fragment>}
  </div>;
}
