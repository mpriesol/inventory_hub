import React, { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Button } from '../components/ui/Button.new';
import { ActionScope } from '../components/ui/ActionScope';
import { getUpgatesCapacity, UpgatesCapacity } from '../api/upgatesCapacity';
import { accessRevision, hubUnlocked, subscribeAccess, unlockHub } from '../api/access';
import { getSupplierAvailability, getSupplierFeedFailures, SupplierFeedRun, SupplierFeedFailures, reconcileSupplierLinks, runSupplierAvailability, saveSupplierAvailability, SupplierAvailabilitySettings, SupplierLinkIssue } from '../api/supplierAvailability';
import { getStockSyncOptions, getStockSyncRun, resolveStockSyncItem, runStockSync, saveStockSyncSettings, saveStockSyncWarehouse,
  STOCK_SYNC_FIELDS, StockSyncField, StockSyncOptions, StockSyncRunDetail, StockSyncValues, StockSyncResolution, StockSyncItem } from '../api/stockSync';
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
  return { busy, error, uncertain, run, recovered: () => setUncertain(false), cancel: () => { generation.current++; abort.current?.abort(); running.current = false; setBusy(false); } };
}

function Feedback({ error, uncertain }: { error: string; uncertain: boolean }) {
  const { t } = useTranslation();
  return <>{uncertain && <p role="alert" className="opening-stock-notice">{t('availabilitySync.uncertain')}</p>}
    {error && <p role="alert" className="opening-stock-errors">{t(`availabilitySync.errors.${error}`, { defaultValue: t('availabilitySync.requestFailed') })} <code>{error}</code></p>}</>;
}

function SupplierRunResult({ supplier, run }: { supplier: string; run: SupplierFeedRun }) {
  const { t, i18n } = useTranslation();
  const operation = useOperation();
  const [failures, setFailures] = useState<SupplierFeedFailures | null>(null), [opened, setOpened] = useState(false);
  const report = run.report, issueCount = report ? report.rejected + report.unknown : 0;
  function load(offset = 0) { operation.run(false, signal => getSupplierFeedFailures(supplier, run.id, offset, signal), setFailures); }
  return <section className="availability-result" data-testid={`supplier-${supplier}-result`}>
    <h4>{t('availabilitySync.resultTitle')}</h4>
    <p>{t(`availabilitySync.runStatus.${run.status}`)} · {new Date(run.finished_at || run.started_at).toLocaleString(i18n.language)}</p>
    {report && <>
      <dl className="availability-status availability-counts">{(['total', 'accepted', 'rejected', 'unknown'] as const).map(key => <div key={key}><dt>{t(`availabilitySync.counts.${key}`)}</dt><dd data-testid={`supplier-${supplier}-count-${key}`}>{report[key]}</dd></div>)}</dl>
      {report.warnings.map(code => <p key={code} className="opening-stock-notice">{t(`availabilitySync.errors.${code}`, { defaultValue: t('availabilitySync.requestFailed') })}</p>)}
      {issueCount > 0 && <>
        <p>{t('availabilitySync.preservedRows')}</p>
        <Button data-testid={`supplier-${supplier}-failures`} variant="secondary" disabled={operation.busy} onClick={() => { setOpened(!opened); if (!opened && !failures) load(); }}>{t(opened ? 'availabilitySync.hideFailures' : 'availabilitySync.showFailures', { count: issueCount })}</Button>
        {opened && <div>
          <Feedback error={operation.error} uncertain={false} />
          {operation.error && <Button variant="secondary" onClick={() => load(failures?.offset || 0)}>{t('availabilitySync.reloadFailures')}</Button>}
          {failures && <>
            <ul className="availability-failures" data-testid={`supplier-${supplier}-failure-list`}>{failures.items.map(item => <li key={item.row}>
              <strong>{item.sku || t('availabilitySync.noSku')}</strong> <small>{t('availabilitySync.sourceRow', { row: item.row })}</small>
              <p>{t(`availabilitySync.errors.${item.code}`, { defaultValue: t('availabilitySync.requestFailed') })}</p>
              <small>{t('availabilitySync.rawQuantity')}: <code>{item.stock ?? '—'}</code>{item.external_stock != null && <> · {t('availabilitySync.externalQuantity')}: <code>{item.external_stock}</code></>}</small>
            </li>)}</ul>
            <div className="availability-actions"><Button data-testid={`supplier-${supplier}-failures-prev`} variant="secondary" disabled={operation.busy || failures.offset === 0} onClick={() => load(Math.max(0, failures.offset - failures.limit))}>{t('availabilitySync.previousPage')}</Button>
              <span>{t('availabilitySync.failurePage', { from: failures.total ? failures.offset + 1 : 0, to: failures.offset + failures.items.length, total: failures.total })}</span>
              <Button data-testid={`supplier-${supplier}-failures-next`} variant="secondary" disabled={operation.busy || failures.offset + failures.items.length >= failures.total} onClick={() => load(failures.offset + failures.limit)}>{t('availabilitySync.nextPage')}</Button></div>
          </>}
        </div>}
      </>}
    </>}
  </section>;
}

function SupplierCard({ value, replace, onBusy, onLeaveBlocked, refreshVersion }: { value: SupplierAvailabilitySettings; replace: (value: SupplierAvailabilitySettings) => void; onBusy: (supplier: string, busy: boolean) => void; onLeaveBlocked: (supplier: string, blocked: boolean) => void; refreshVersion: number }) {
  const { t, i18n } = useTranslation();
  const operation = useOperation();
  const manualOnly = value.manual_only === true || value.supplier === 'sloger';
  const [baseline, setBaseline] = useState(value);
  const [enabled, setEnabled] = useState(value.enabled), [feed, setFeed] = useState(value.feed_key);
  const [interval, setInterval] = useState(String(value.interval_seconds)), [freshness, setFreshness] = useState(String(value.freshness_seconds)), [coverage, setCoverage] = useState(String(value.min_coverage_percent));
  const [saved, setSaved] = useState(false);
  const [linkResult, setLinkResult] = useState<{ scanned: number; linked: number; existing: number; skipped: number; conflicts: number; issues: SupplierLinkIssue[]; complete: boolean } | null>(null);
  function resetForm(next: SupplierAvailabilitySettings) { setBaseline(next); setEnabled(next.enabled); setFeed(next.feed_key); setInterval(String(next.interval_seconds)); setFreshness(String(next.freshness_seconds)); setCoverage(String(next.min_coverage_percent)); operation.recovered(); }
  useEffect(() => { resetForm(value); }, [refreshVersion]);
  useEffect(() => { onBusy(value.supplier, operation.busy); return () => onBusy(value.supplier, false); }, [operation.busy, value.supplier]);
  const dirty = freshness !== String(baseline.freshness_seconds) || (!manualOnly && (enabled !== baseline.enabled || feed !== baseline.feed_key || interval !== String(baseline.interval_seconds) || coverage !== String(baseline.min_coverage_percent)));
  const minInterval = enabled ? 86400 : 300;
  const valid = integer(freshness, 300, 2592000) && (manualOnly || (value.feed_keys.includes(feed) && integer(interval, minInterval, 604800) && Number(freshness) >= Number(interval) + (enabled ? 3600 : 0) && integer(coverage, 1, 100)));
  const date = (stamp: string | null) => stamp ? new Date(stamp).toLocaleString(i18n.language) : '—';
  const stale = value.last_success_at ? Date.now() - new Date(value.last_success_at).getTime() > value.freshness_seconds * 1000 : true;
  const configChanged = baseline.revision !== value.revision;
  const blocked = operation.busy || operation.uncertain || configChanged;
  useEffect(() => { onLeaveBlocked(value.supplier, dirty || blocked); return () => onLeaveBlocked(value.supplier, false); }, [dirty, blocked, value.supplier]);
  const status = value.status || (value.revision === 0 ? 'unconfigured' : !value.last_success_at ? 'not_loaded' : stale ? 'stale' : 'fresh');
  const id = (suffix: string) => `supplier-${value.supplier}-${suffix}`;
  function discard() { resetForm(value); setSaved(false); }
  function save() {
    if (!valid || blocked) return;
    setSaved(false);
    operation.run(true, () => saveSupplierAvailability(value.supplier, { expected_revision: value.revision, enabled: manualOnly ? false : enabled, feed_key: manualOnly ? value.feed_key : feed, interval_seconds: manualOnly ? Math.min(value.interval_seconds, Number(freshness)) : Number(interval), freshness_seconds: Number(freshness), min_coverage_percent: manualOnly ? value.min_coverage_percent : Number(coverage) }), result => { resetForm(result); replace({ ...result, latest_run: result.feed_key === value.feed_key ? value.latest_run : null }); setSaved(true); });
  }
  function repairLinks() {
    if (blocked || dirty) return;
    setLinkResult(null);
    operation.run(true, async signal => {
      const credential = accessRevision();
      const totals = { scanned: 0, linked: 0, existing: 0, skipped: 0, conflicts: 0, issues: [] as SupplierLinkIssue[], complete: false };
      let after = 0;
      while (true) {
        if (signal.aborted || credential !== accessRevision()) throw new DOMException('Aborted', 'AbortError');
        const batch = await reconcileSupplierLinks(value.supplier, after, signal);
        if (signal.aborted || credential !== accessRevision()) throw new DOMException('Aborted', 'AbortError');
        totals.scanned += batch.scanned; totals.linked += batch.linked; totals.existing += batch.existing; totals.skipped += batch.skipped;
        totals.conflicts += batch.conflicts.length;
        totals.issues = [...totals.issues, ...batch.conflicts, ...(batch.skipped_details || [])].slice(0, 50);
        totals.complete = batch.next_after_product_id === null;
        setLinkResult({ ...totals });
        if (totals.complete) return totals;
        if (!Number.isSafeInteger(batch.next_after_product_id) || batch.next_after_product_id! <= after) throw new Error('Invalid reconciliation cursor');
        after = batch.next_after_product_id!;
      }
    }, result => setLinkResult(result));
  }
  return <article className={`availability-supplier${dirty ? ' availability-dirty' : ''}`} data-testid={id('card')}>
    <header><h3>{value.name}</h3><span className="opening-stock-badge">{value.running || value.manual_requested_at ? t(`availabilitySync.${value.running ? 'running' : 'queued'}`) : t(`productEditor.supplierStatus.${status}`, { defaultValue: t(`availabilitySync.${stale ? 'stale' : 'fresh'}`) })}</span></header>
    {status === 'unconfigured' && <p data-testid={id('setup-help')}>{t('availabilitySync.setupFirst')}</p>}
    {status === 'not_loaded' && !value.last_error && <p data-testid={id('load-help')}>{t('availabilitySync.loadFirst')}</p>}
    {status === 'configuration_error' && <p role="alert">{t('availabilitySync.configurationHelp')}</p>}
    {value.publication?.enabled && <p data-testid={id('publication')}>{t('availabilitySync.scopedPublication', { shop: value.publication.shop_code, brands: value.publication.brands.join(', '), completed: value.publication.counts?.completed || 0, uncertain: value.publication.counts?.uncertain || 0, rejected: value.publication.counts?.rejected || 0 })}</p>}
    {!manualOnly && <><p data-testid={id('automation')}>{t(value.enabled ? 'availabilitySync.autoEnabled' : 'availabilitySync.automaticOff', { hours: value.interval_seconds / 3600 })}</p><p className="opening-stock-muted">{t('availabilitySync.dailyChangesOnly')}</p></>}
    {value.latest_run && <SupplierRunResult key={value.latest_run.id} supplier={value.supplier} run={value.latest_run} />}
    <p><Link to={`/suppliers?edit=${encodeURIComponent(value.supplier)}`}>{t('availabilitySync.supplierConfig')}</Link></p>
    {manualOnly && <p data-testid={id('manual-help')}>{t('availabilitySync.manualB2bHelp')}</p>}
    <div className="availability-fields">
      {!manualOnly && <label>{t('availabilitySync.feed')}<select data-testid={id('feed')} value={feed} disabled={blocked} onChange={event => { setFeed(event.target.value); setSaved(false); }}>{value.feed_keys.length === 0 && <option value="">{t('availabilitySync.noFeeds')}</option>}{value.feed_keys.map(key => <option key={key} value={key}>{t(`availabilitySync.feedNames.${key}`, { defaultValue: key })}</option>)}</select></label>}
      {!manualOnly && <label>{t('availabilitySync.interval')}<input data-testid={id('interval')} type="number" min={minInterval} max="604800" step="1" value={interval} disabled={blocked} onChange={event => { setInterval(event.target.value); setSaved(false); }} /><small>{t('availabilitySync.rangeSeconds', { min: minInterval, max: 604800 })}</small></label>}
    </div><details className="availability-advanced"><summary>{t('availabilitySync.advancedSettings')}</summary><div className="availability-fields">
      <label>{t('availabilitySync.freshness')}<input data-testid={id('freshness')} type="number" min="300" max="2592000" step="1" value={freshness} disabled={blocked} onChange={event => { setFreshness(event.target.value); setSaved(false); }} /><small>{t('availabilitySync.freshnessHelp')}</small></label>
      {!manualOnly && <label>{t('availabilitySync.coverage')}<input data-testid={id('coverage')} type="number" min="1" max="100" step="1" value={coverage} disabled={blocked} onChange={event => { setCoverage(event.target.value); setSaved(false); }} /><small>{t('availabilitySync.coverageHelp')}</small></label>}
    </div>
    </details>
    {!manualOnly && <label className="availability-check"><input data-testid={id('enabled')} type="checkbox" checked={enabled} disabled={blocked} onChange={event => {
      setEnabled(event.target.checked); setSaved(false);
      if (event.target.checked) {
        const dailyInterval = Math.max(86400, Number(interval) || 86400);
        setInterval(String(dailyInterval)); setFreshness(String(Math.max(dailyInterval + 3600, Number(freshness) || 172800)));
      }
    }} />{t('availabilitySync.supplierAuto')}</label>}
    <dl className="availability-status"><div><dt>{t('availabilitySync.lastSuccess')}</dt><dd>{date(value.last_success_at)}</dd></div>{!manualOnly && <div><dt>{t('availabilitySync.nextRun')}</dt><dd>{value.enabled ? date(value.next_run_at) : t('availabilitySync.automaticOff')}</dd></div>}{manualOnly && <div><dt>{t('availabilitySync.itemCount')}</dt><dd>{value.last_item_count ?? '—'}</dd></div>}</dl>
    {value.refresh_progress && <div data-testid={id('refresh-progress')} role="status">
      <p>{t('availabilitySync.b2bProgress', value.refresh_progress)}</p>
      {!!value.refresh_progress.failures.length && <details><summary>{t('availabilitySync.b2bFailures')}</summary>{value.refresh_progress.failures.map((item, index) => <p key={`${item.sku}-${index}`}><code>{item.sku}</code>: {t(`availabilitySync.errors.${item.code}`, { defaultValue: t('availabilitySync.requestFailed') })}</p>)}</details>}
    </div>}
    {value.last_error && <p className="opening-stock-errors" role="alert">{t('availabilitySync.lastError')}: {t(`availabilitySync.errors.${value.last_error}`, { defaultValue: t('availabilitySync.requestFailed') })}</p>}
    {configChanged && <p role="alert">{t('availabilitySync.configChanged')}</p>}
    {dirty && <p className="availability-change" role="status">{t('availabilitySync.unsaved')} <Button data-testid={id('discard')} variant="secondary" disabled={blocked} onClick={discard}>{t('availabilitySync.discardChanges')}</Button></p>}
    {saved && !dirty && <p role="status" className="opening-stock-success">{t('availabilitySync.saved')}</p>}
    {linkResult && <div data-testid={id('link-result')} role="status"><p>{t(linkResult.complete ? 'availabilitySync.linksComplete' : operation.busy ? 'availabilitySync.linksProgress' : 'availabilitySync.linksIncomplete', linkResult)}</p>
      <p>{t('availabilitySync.linksCounts', linkResult)}</p>
      {linkResult.issues.length > 0 && <details><summary>{t('availabilitySync.linksIssues')}</summary><ul>{linkResult.issues.map((issue, index) => <li key={`${issue.product_id}-${index}`}><code>{issue.sku}</code>: {t(`availabilitySync.linkReasons.${issue.reason}`, { defaultValue: t('availabilitySync.linkNeedsReview') })}</li>)}</ul></details>}
    </div>}
    <Feedback error={operation.error} uncertain={operation.uncertain} />
    <div className="availability-actions"><span className="action-control"><Button data-testid={id('save')} disabled={blocked || !valid || (!dirty && value.revision > 0)} onClick={save}>{t('availabilitySync.save')}</Button><ActionScope effects={!manualOnly && enabled ? ['hub-write', 'queued-supplier'] : ['hub-write']} calls={{ kind: 'variable' }} /></span>
      <span className="action-control"><Button data-testid={id('run')} variant="secondary" disabled={blocked || dirty || value.revision === 0 || value.configuration_error != null || !valid || value.running || !!value.manual_requested_at} onClick={() => operation.run(true, () => runSupplierAvailability(value.supplier, value.revision), () => replace({ ...value, manual_requested_at: new Date().toISOString() }))}>{t('availabilitySync.refreshSupplier')}</Button><ActionScope effects={['hub-write', 'queued-supplier']} calls={{ kind: 'variable' }} /></span>
      <span className="action-control"><Button data-testid={id('reconcile')} variant="secondary" disabled={blocked || dirty} onClick={repairLinks}>{t('availabilitySync.reconcileLinks')}</Button><ActionScope effects={['hub-write']} /></span></div>
    <p className="text-sm">{t(manualOnly ? 'availabilitySync.manualB2bHelp' : 'availabilitySync.refreshScopeHelp')}</p>
    <p className="text-sm">{t('availabilitySync.linksHelp')}</p>
  </article>;
}

function SuppliersPanel({ selectedSupplier }: { selectedSupplier: string }) {
  const { t } = useTranslation();
  const operation = useOperation();
  const [suppliers, setSuppliers] = useState<SupplierAvailabilitySettings[] | null>(null), [writing, setWriting] = useState<Set<string>>(new Set());
  const [refreshVersion, setRefreshVersion] = useState(0);
  const [leaveBlocked, setLeaveBlocked] = useState<Set<string>>(new Set());
  function onLeaveBlocked(supplier: string, blocked: boolean) { setLeaveBlocked(previous => { const next = new Set(previous); blocked ? next.add(supplier) : next.delete(supplier); return next; }); }
  function onBusy(supplier: string, busy: boolean) { if (busy) operation.cancel(); setWriting(previous => { const next = new Set(previous); busy ? next.add(supplier) : next.delete(supplier); return next; }); }
  function load() { operation.run(false, signal => getSupplierAvailability(signal), result => { setSuppliers(result.suppliers); setRefreshVersion(v => v + 1); operation.recovered(); }); }
  useEffect(() => { if (selectedSupplier) load(); }, [selectedSupplier]);
  const pending = suppliers?.some(value => value.running || value.manual_requested_at);
  useEffect(() => {
    if (!pending || writing.size || operation.busy || operation.error) return;
    const timer = window.setTimeout(() => operation.run(false, signal => getSupplierAvailability(signal), result => setSuppliers(result.suppliers)), 5000);
    return () => window.clearTimeout(timer);
  }, [pending, suppliers, writing.size, operation.busy, operation.error]);
  const visible = selectedSupplier ? suppliers?.filter(value => value.supplier === selectedSupplier) : suppliers;
  return <section className="opening-stock-panel"><div className="opening-stock-actions"><h2>{t('availabilitySync.suppliersTitle')}</h2><span className="action-control"><Button data-testid="load-suppliers" variant="secondary" disabled={operation.busy || writing.size > 0} onClick={load}>{t('availabilitySync.loadSuppliers')}</Button><ActionScope effects={['hub-read']} /></span></div>
    <p>{t(selectedSupplier ? 'availabilitySync.focusedSupplierHelp' : 'availabilitySync.suppliersHelp')}</p>
    {selectedSupplier && <p>{leaveBlocked.size > 0 ? <><Button data-testid="all-suppliers-blocked" variant="secondary" disabled>{t('availabilitySync.showAllSuppliers')}</Button> <span role="status">{t('availabilitySync.leaveBlocked')}</span></> : <Link data-testid="all-suppliers" to="/settings/availability">{t('availabilitySync.showAllSuppliers')}</Link>}</p>}
    <Feedback error={operation.error} uncertain={false} />
    {visible?.length === 0 && <p>{t(selectedSupplier ? 'availabilitySync.supplierNotFound' : 'availabilitySync.noSuppliers')}</p>}
    <div className="availability-suppliers">{visible?.map(value => <SupplierCard key={value.supplier} value={value} refreshVersion={refreshVersion} replace={result => setSuppliers(previous => previous!.map(item => item.supplier === result.supplier ? result : item))} onBusy={onBusy} onLeaveBlocked={onLeaveBlocked} />)}</div>
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
  const [nativeOrders, setNativeOrders] = useState(false), [authority, setAuthority] = useState(false), [writersDisabled, setWritersDisabled] = useState(false), [ordersReconciled, setOrdersReconciled] = useState(false);
  const [saved, setSaved] = useState(''), [reloadRequired, setReloadRequired] = useState(false), [run, setRun] = useState<StockSyncRunDetail | null>(null), [finished, setFinished] = useState<Record<number, boolean>>({});
  const [resolutions, setResolutions] = useState<Record<number, { outcome: '' | StockSyncResolution['outcome']; evidence: string }>>({});
  function acceptRun(value: StockSyncRunDetail) { setRun(value); setFinished({}); setResolutions({}); }
  const date = (stamp: string | null) => stamp ? new Date(stamp).toLocaleString(i18n.language) : '—';
  const blocked = operation.busy || operation.uncertain;
  function clear() { setSaved(''); setNativeOrders(false); setAuthority(false); setWritersDisabled(false); setOrdersReconciled(false); }
  function accept(value: StockSyncOptions) { setOptions(value); if (value.warehouse_settings) setWarehouse(valueInputs(value.warehouse_settings));
    setOverrides(Object.fromEntries(STOCK_SYNC_FIELDS.filter(field => value.settings[field.key] !== null).map(field => [field.key, String(value.settings[field.key])])));
    setEnabled(value.settings.enabled); setAuthorized(value.settings.authorized); setReloadRequired(false); clear(); operation.recovered();
  }
  const effective = options?.warehouse_settings ? { ...valueInputs(options.warehouse_settings), ...overrides } : warehouse;
  const needsAuthority = authorized && (!options?.settings.authorized || options.settings.protocol !== 'delta_v1' || options.blockers.some(code => ['stock_sync_authority_changed', 'stock_sync_target_changed', 'stock_sync_protocol_confirmation_required'].includes(code)));
  const canAuthorize = !needsAuthority || (authority && nativeOrders && writersDisabled && ordersReconciled);
  const warehouseDirty = !!options?.warehouse_settings && STOCK_SYNC_FIELDS.some(({ key }) => warehouse[key] !== String(options.warehouse_settings![key]));
  const stockDirty = !!options && (enabled !== options.settings.enabled || authorized !== options.settings.authorized || STOCK_SYNC_FIELDS.some(({ key }) => (overrides[key] ?? null) !== (options.settings[key] === null ? null : String(options.settings[key]))));
  function fields(target: 'warehouse' | 'shop') { return <div className="availability-fields">{STOCK_SYNC_FIELDS.map(field => {
    const inherited = overrides[field.key] === undefined;
    return <div key={field.key}><label>{t(`availabilitySync.fields.${field.key}`)}<input data-testid={`sync-${target}-${field.key}`} type="number" min={field.min} max={field.max} step="1" disabled={blocked || (target === 'shop' && inherited)} value={target === 'warehouse' ? warehouse[field.key] : effective[field.key]} onChange={event => { clear(); const value = event.target.value; target === 'warehouse' ? setWarehouse(previous => ({ ...previous, [field.key]: value })) : setOverrides(previous => ({ ...previous, [field.key]: value })); }} /><small>{t('availabilitySync.range', { min: field.min, max: field.max })}</small></label>
      {target === 'shop' && <label className="availability-check"><input data-testid={`sync-inherit-${field.key}`} type="checkbox" checked={inherited} disabled={blocked} onChange={event => { clear(); setOverrides(previous => { const next = { ...previous }; if (event.target.checked) delete next[field.key]; else next[field.key] = String(options?.warehouse_settings?.[field.key] ?? ''); return next; }); }} />{t('availabilitySync.inherit')}</label>}</div>;
  })}</div>; }
  function recovery(item: StockSyncItem) {
    const delta = (item.protocol ?? run?.protocol) === 'delta_v1';
    const resolution = resolutions[item.id];
    const evidence = resolution?.evidence.trim() || '';
    const validResolution = !delta || (!!resolution?.outcome && evidence.length >= 8 && evidence.length <= 1000);
    function resolve() {
      if (blocked || !finished[item.id] || !validResolution) return;
      operation.run(true, () => resolveStockSyncItem(item.id, delta ? { outcome: resolution!.outcome as StockSyncResolution['outcome'], evidence } : undefined), acceptRun);
    }
    return <div className="availability-recovery">
      <p>{t(delta ? 'availabilitySync.deltaRecoveryHelp' : 'availabilitySync.recoveryHelp')}</p>
      {delta && <>
        <label>{t('availabilitySync.resolutionOutcome')}<select data-testid={`sync-outcome-${item.id}`} value={resolution?.outcome || ''} disabled={blocked} onChange={event => setResolutions(previous => ({ ...previous, [item.id]: { evidence: previous[item.id]?.evidence || '', outcome: event.target.value as StockSyncResolution['outcome'] | '' } }))}>
          <option value="">{t('availabilitySync.chooseOutcome')}</option><option value="applied">{t('availabilitySync.outcomeApplied')}</option><option value="not_applied">{t('availabilitySync.outcomeNotApplied')}</option>
        </select></label>
        <label>{t('availabilitySync.resolutionEvidence')}<textarea data-testid={`sync-evidence-${item.id}`} rows={3} minLength={8} maxLength={1000} disabled={blocked} value={resolution?.evidence || ''} onChange={event => setResolutions(previous => ({ ...previous, [item.id]: { outcome: previous[item.id]?.outcome || '', evidence: event.target.value } }))} /></label>
      </>}
      <label className="availability-check"><input data-testid={`sync-finished-${item.id}`} type="checkbox" disabled={blocked} checked={!!finished[item.id]} onChange={event => setFinished(previous => ({ ...previous, [item.id]: event.target.checked }))} />{t('availabilitySync.requestFinished')}</label>
      <span className="action-control"><Button data-testid={`resolve-sync-${item.id}`} variant="secondary" disabled={blocked || !finished[item.id] || !validResolution} onClick={resolve}>{t(delta ? 'availabilitySync.resolveDelta' : 'availabilitySync.resolve')}</Button><ActionScope effects={delta ? ['hub-write'] : ['hub-write', 'upgates-read']} shop={shop} calls={delta ? undefined : { kind: 'variable' }} /></span>
    </div>;
  }
  return <section className="opening-stock-panel"><div className="opening-stock-actions"><h2>{t('availabilitySync.stockTitle', { shop: shop === 'xtrek' ? 'xTrek' : 'BIKETREK' })}</h2><span className="action-control"><Button data-testid="load-stock-sync" variant="secondary" disabled={operation.busy} onClick={() => operation.run(false, signal => getStockSyncOptions(shop, signal), accept)}>{t('availabilitySync.loadStock')}</Button><ActionScope effects={['hub-read']} /></span></div>
    <p>{t('availabilitySync.stockHelp')}</p><p><Link to={`/settings/stock?shop=${shop}`}>{t('availabilitySync.orderSettings')}</Link> · <Link to={`/orders/inbox?shop=${shop}`}>{t('availabilitySync.orderInbox')}</Link></p>
    <Feedback error={operation.error} uncertain={operation.uncertain} />
    {saved && <p role="status" className="opening-stock-success">{t(`availabilitySync.${saved}`)}</p>}
    {options && <>
      <p data-testid="sync-baseline-count">{t('availabilitySync.baselineCount', { count: options.baseline_count ?? 0 })} · <Link to={`/stock/publication?shop=${shop}`}>{t('availabilitySync.prepareBaseline')}</Link></p>
      {options.settings.authorized && options.settings.protocol !== 'delta_v1' && <p role="status" className="opening-stock-notice">{t('availabilitySync.legacyAuthorization')}</p>}
      {!!options.blockers.length && <div role="status" className="opening-stock-notice"><strong>{t('availabilitySync.notReady')}</strong><ul>{options.blockers.map(code => <li key={code}>{t(`availabilitySync.errors.${code}`, { defaultValue: code })}</li>)}</ul></div>}
      {!options.server_write_enabled && <p className="opening-stock-notice">{t('availabilitySync.serverDisabled')}</p>}
      {options.warehouse && options.warehouse_settings && <>
        <details className="availability-warehouse"><summary>{t('availabilitySync.warehouseTitle', { name: options.warehouse.name })}</summary><p>{t('availabilitySync.warehouseHelp')}</p>{fields('warehouse')}{warehouseDirty && <p role="status" className="availability-change">{t('availabilitySync.unsaved')}</p>}
          <span className="action-control"><Button data-testid="save-sync-warehouse" disabled={blocked || reloadRequired || !warehouseDirty || !validValues(warehouse)} onClick={() => operation.run(true, () => saveStockSyncWarehouse({ warehouse_code: options.warehouse!.code, expected_revision: options.warehouse_settings!.revision, ...numberValues(warehouse), confirmed: true }), () => { setReloadRequired(true); clear(); setSaved('warehouseSaved'); })}>{t('availabilitySync.saveWarehouse')}</Button><ActionScope effects={['hub-write']} /></span></details>
        <h3>{t('availabilitySync.shopSettings')}</h3>{reloadRequired && <p role="status" className="opening-stock-notice">{t('availabilitySync.reloadRequired')}</p>}{fields('shop')}
        <label className="availability-check"><input data-testid="sync-authorized" type="checkbox" checked={authorized} disabled={blocked || reloadRequired} onChange={event => { clear(); setAuthorized(event.target.checked); if (!event.target.checked) setEnabled(false); }} />{t('availabilitySync.authorized')}</label>
        <label className="availability-check"><input data-testid="sync-enabled" type="checkbox" checked={enabled} disabled={blocked || reloadRequired || !authorized} onChange={event => { clear(); setEnabled(event.target.checked); }} />{t('availabilitySync.stockAuto')}</label>
        {needsAuthority && <div className="opening-stock-notice availability-authority"><p>{t('availabilitySync.authorityHelp')}</p><label className="availability-check"><input data-testid="sync-native-orders" type="checkbox" checked={nativeOrders} disabled={blocked} onChange={event => setNativeOrders(event.target.checked)} />{t('availabilitySync.nativeOrders')}</label><label className="availability-check"><input data-testid="sync-authority" type="checkbox" checked={authority} disabled={blocked} onChange={event => setAuthority(event.target.checked)} />{t('availabilitySync.authority')}</label><label className="availability-check"><input data-testid="sync-writers-disabled" type="checkbox" checked={writersDisabled} disabled={blocked} onChange={event => setWritersDisabled(event.target.checked)} />{t('availabilitySync.writersDisabled')}</label><label className="availability-check"><input data-testid="sync-orders-reconciled" type="checkbox" checked={ordersReconciled} disabled={blocked} onChange={event => setOrdersReconciled(event.target.checked)} />{t('availabilitySync.ordersReconciled')}</label></div>}
        {stockDirty && <p role="status" className="availability-change">{t('availabilitySync.unsaved')}</p>}
        <div className="availability-actions"><span className="action-control"><Button data-testid="save-stock-sync" disabled={blocked || reloadRequired || warehouseDirty || (!stockDirty && !needsAuthority) || !validValues(effective) || !canAuthorize || (enabled && !authorized)} onClick={() => operation.run(true, () => saveStockSyncSettings({ shop_code: shop, expected_revision: options.settings.revision, enabled, authorized, interval_seconds: overrides.interval_seconds === undefined ? null : Number(overrides.interval_seconds), batch_size: overrides.batch_size === undefined ? null : Number(overrides.batch_size), max_order_age_seconds: overrides.max_order_age_seconds === undefined ? null : Number(overrides.max_order_age_seconds), hub_is_stock_authority: authority, native_order_stock_enabled: nativeOrders, external_stock_writers_disabled: writersDisabled, orders_reconciled: ordersReconciled, confirmed: true }), result => { accept(result); setSaved('saved'); })}>{t('availabilitySync.saveShop')}</Button><ActionScope effects={enabled ? ['hub-write', 'queued-upgates'] : ['hub-write']} shop={shop} calls={{ kind: 'variable' }} /></span>
          <span className="action-control"><Button data-testid="run-stock-sync" variant="secondary" disabled={blocked || reloadRequired || warehouseDirty || stockDirty || needsAuthority || !options.settings.authorized || !options.server_write_enabled || !!options.blockers.length || !!run && ['queued', 'pending', 'running', 'preparing'].includes(run.status)} onClick={() => operation.run(true, () => runStockSync(shop), result => { acceptRun(result); setSaved('stockQueued'); })}>{t('availabilitySync.runStock')}</Button><ActionScope effects={['hub-write', 'queued-upgates']} shop={shop} calls={{ kind: 'variable' }} /></span></div>
      </>}
      <dl className="availability-status"><div><dt>{t('availabilitySync.lastCompleted')}</dt><dd>{date(options.settings.last_completed_at)}</dd></div><div><dt>{t('availabilitySync.nextRun')}</dt><dd>{options.settings.enabled ? date(options.settings.next_run_at) : t('availabilitySync.automaticOff')}</dd></div></dl>
      {options.settings.last_error && <p role="alert" className="opening-stock-errors">{t('availabilitySync.lastError')}: {t(`availabilitySync.errors.${options.settings.last_error}`, { defaultValue: options.settings.last_error })}</p>}
      <h3>{t('availabilitySync.recentRuns')}</h3>{options.runs.length === 0 && <p>{t('availabilitySync.noRuns')}</p>}
      {options.runs.length > 0 && <div className="opening-stock-table-scroll"><table><thead><tr><th>{t('availabilitySync.run')}</th><th>{t('availabilitySync.status')}</th><th>{t('availabilitySync.verified')}</th><th>{t('availabilitySync.failed')}</th><th>{t('availabilitySync.started')}</th><th /></tr></thead><tbody>{options.runs.map(item => <tr key={item.id}><td>#{item.id}</td><td>{t(`availabilitySync.statuses.${item.status}`, { defaultValue: item.status })}</td><td>{item.counts.verified}</td><td>{item.counts.failed + item.counts.uncertain}</td><td>{date(item.started_at)}</td><td><Button data-testid={`sync-run-${item.id}`} variant="secondary" disabled={operation.busy} onClick={() => operation.run(false, signal => getStockSyncRun(item.id, signal), acceptRun)}>{t('availabilitySync.detail')}</Button></td></tr>)}</tbody></table></div>}
    </>}
    {run && <section className="availability-run" data-testid="sync-run-detail"><div className="opening-stock-actions"><h3>{t('availabilitySync.runDetail', { id: run.id })}</h3><span className="action-control"><Button data-testid="refresh-sync-run" variant="secondary" disabled={operation.busy} onClick={() => operation.run(false, signal => getStockSyncRun(run.id, signal, run.items_offset ?? 0), acceptRun)}>{t('availabilitySync.refreshRun')}</Button><ActionScope effects={['hub-read']} /></span></div>
      <p>{t('availabilitySync.runSummary', { status: t(`availabilitySync.statuses.${run.status}`, { defaultValue: run.status }), ...run.counts })}</p>{run.more_pending && <p>{t('availabilitySync.morePending')}</p>}{run.items_truncated && <p>{t('availabilitySync.itemsTruncated')}</p>}
      {run.error && <p className="opening-stock-errors">{t('availabilitySync.lastError')}: {t(`availabilitySync.errors.${run.error}`, { defaultValue: run.error })}</p>}
      <div className="opening-stock-table-scroll"><table><thead><tr><th>SKU</th><th>{t('availabilitySync.status')}</th><th>{t('availabilitySync.stock')}</th><th>{t('availabilitySync.availability')}</th><th>{t('availabilitySync.recovery')}</th></tr></thead><tbody>{run.items.map(item => <tr key={item.id}><td><code>{item.sku}</code></td><td>{t(`availabilitySync.statuses.${item.status}`, { defaultValue: item.status })}{item.error && <small className="availability-error-code">{t(`availabilitySync.errors.${item.error}`, { defaultValue: item.error })}</small>}</td><td>{item.desired?.stock_increment !== undefined ? t('availabilitySync.quantityChange', { quantity: item.desired.stock_increment }) : item.desired?.stock ?? '—'}</td><td>{item.desired?.availability ?? '—'}</td><td>{item.status === 'uncertain' && recovery(item)}</td></tr>)}</tbody></table></div>
      {(run.items_total ?? run.items.length) > (run.items_limit ?? 100) && <div className="opening-stock-pagination"><Button data-testid="sync-items-previous" variant="secondary" disabled={operation.busy || !(run.items_offset ?? 0)} onClick={() => operation.run(false, signal => getStockSyncRun(run.id, signal, Math.max(0, (run.items_offset ?? 0) - (run.items_limit ?? 100)), run.items_limit ?? 100), acceptRun)}>{t('availabilitySync.previous')}</Button><span>{t('availabilitySync.itemPage', { first: (run.items_offset ?? 0) + 1, last: (run.items_offset ?? 0) + run.items.length, total: run.items_total ?? run.items.length })}</span><Button data-testid="sync-items-next" variant="secondary" disabled={operation.busy || (run.items_offset ?? 0) + run.items.length >= (run.items_total ?? run.items.length)} onClick={() => operation.run(false, signal => getStockSyncRun(run.id, signal, (run.items_offset ?? 0) + (run.items_limit ?? 100), run.items_limit ?? 100), acceptRun)}>{t('availabilitySync.next')}</Button></div>}
    </section>}
  </section>;
}

function CapacityPanel({ shop }: { shop: string }) {
  const { t, i18n } = useTranslation();
  const operation = useOperation();
  const [value, setValue] = useState<UpgatesCapacity | null>(null);
  const [includeProductSample, setIncludeProductSample] = useState(false);
  const copy = (key: string) => t(`availabilitySync.capacity.${key}`);
  const amount = (number: number | null | undefined) => typeof number === 'number' && Number.isFinite(number)
    ? number.toLocaleString(i18n.language) : copy('unknown');
  function probe() {
    if (operation.busy) return;
    setValue(null);
    operation.run(false, signal => getUpgatesCapacity(shop, signal, includeProductSample), result => {
      if (!result || result.shop_code !== shop || !result.probe?.permissions || !result.quota || !result.projection?.stock || !result.projection.orders || !Array.isArray(result.projection.eligibility_errors)) throw new Error('Invalid capacity result');
      setValue(result);
    });
  }
  const error = value?.probe.error || operation.error;
  const sample = value?.catalog_probe;
  const sampleCalls = sample?.requests_attempted;
  const sampleError = sample?.error ? t([`availabilitySync.capacity.errors.${sample.error}`,
    `availabilitySync.errors.${sample.error}`, `stockPublication.errors.${sample.error}`], { defaultValue: '' }) : '';
  const actualCalls = !value ? undefined : sample?.requested || includeProductSample
    ? typeof sampleCalls === 'number' && Number.isInteger(sampleCalls) && sampleCalls >= 0
      ? value.probe.requests_attempted + sampleCalls : undefined
    : value.probe.requests_attempted;
  const stock = value?.projection.stock, orders = value?.projection.orders;
  const deltaModel = stock?.call_model === 'origin_aware_delta';
  const baselineCalls = stock?.singleton_pass_calls, batchCalls = stock?.initial_batch_calls;
  const savedInitialShare = typeof baselineCalls === 'number' && Number.isSafeInteger(baselineCalls) && baselineCalls > 0
    && typeof batchCalls === 'number' && Number.isSafeInteger(batchCalls) && batchCalls >= 0 && batchCalls <= baselineCalls
    ? 1 - batchCalls / baselineCalls : null;
  return <section className="opening-stock-panel" data-testid="upgates-capacity-panel">
    <div className="opening-stock-actions"><h2>{t('availabilitySync.capacity.title', { shop: shop === 'xtrek' ? 'xTrek' : 'BIKETREK' })}</h2>
      <span className="action-control"><Button data-testid="probe-upgates-capacity" variant="secondary" disabled={operation.busy} onClick={probe}>{copy('probe')}</Button>
        <ActionScope effects={['hub-read', 'upgates-read']} shop={shop} calls={{ kind: 'known', count: includeProductSample ? 3 : 1 }} actualCalls={actualCalls}>{includeProductSample && copy('sampleCallLimit')}</ActionScope></span></div>
    <p>{copy('help')}</p><label className="availability-check"><input data-testid="capacity-product-sample" type="checkbox" checked={includeProductSample} disabled={operation.busy} onChange={event => { setValue(null); setIncludeProductSample(event.target.checked); }} />{copy('sampleOption')}</label><p className="opening-stock-notice">{copy('cooperation')}</p>
    {error && <p role="alert" className="opening-stock-errors">{t(`availabilitySync.capacity.errors.${error}`, { defaultValue: copy('failed') })}</p>}
    {value && <div data-testid="upgates-capacity-result">
      <dl className="availability-status"><div><dt>{copy('checked')}</dt><dd>{new Date(value.observed_at).toLocaleString(i18n.language)}</dd></div>
        <div><dt>{copy('connection')}</dt><dd>{copy(value.probe.ok ? 'connected' : 'unavailable')}</dd></div>
        <div><dt>{copy('latency')}</dt><dd>{amount(value.probe.latency_ms)}{value.probe.latency_ms !== null && ' ms'}</dd></div></dl>
      <h3>{copy('quotaTitle')}</h3><p>{copy('quotaHelp')}</p>
      <div className="opening-stock-table-scroll"><table data-testid="upgates-capacity-quota"><thead><tr><th>{copy('window')}</th><th>{copy('limit')}</th><th>{copy('remaining')}</th></tr></thead><tbody>
        <tr><td>{copy('hour')}</td><td>{amount(value.quota.hour_limit)}</td><td>{amount(value.quota.hour_remaining)}</td></tr>
        <tr><td>{copy('day')}</td><td>{amount(value.quota.day_limit)}</td><td>{amount(value.quota.day_remaining)}</td></tr>
      </tbody></table></div>
      {!value.quota.headers_complete && <p className="opening-stock-notice" data-testid="upgates-capacity-missing-quota">{copy('missingQuota')}</p>}
      <dl className="availability-status"><div><dt>{copy('totalRemaining')}</dt><dd data-testid="upgates-capacity-remaining">{amount(value.quota.total_remaining)}</dd></div>
        <div><dt>{copy('dailyMaximum')}</dt><dd data-testid="upgates-capacity-daily-max">{amount(value.quota.theoretical_daily_max)}</dd></div></dl>
      {value.quota.retry_after_seconds !== null && <p className="opening-stock-notice">{t('availabilitySync.capacity.retryAfter', { seconds: value.quota.retry_after_seconds })}</p>}
      <details><summary>{copy('permissionsTitle')}</summary><dl className="availability-status">{(['products', 'orders', 'order_statuses'] as const).map(key => <div key={key}><dt>{copy(key)}</dt><dd>{t(`availabilitySync.capacity.permissions.${value.probe.permissions[key]}`, { defaultValue: copy('unknown') })}</dd></div>)}</dl><p>{copy('writeUntested')}</p></details>
      {(sample?.requested || includeProductSample) && <section data-testid="upgates-capacity-sample"><h3>{copy('sampleTitle')}</h3>
        <p role={sample?.ok ? 'status' : 'alert'}>{copy(sample?.ok ? 'sampleOk' : 'sampleFailed')}</p>
        {sample?.requested && <p>{t('availabilitySync.capacity.sampleCounts', { products: amount(sample.selected_products), variants: amount(sample.selected_variants) })}</p>}
        {sampleError && sampleError !== copy('sampleFailed') && <p className="opening-stock-errors">{sampleError}</p>}
      </section>}
      {stock && orders && <>
        <h3>{copy('estimateTitle')}</h3><p>{copy('estimateHelp')}</p>
        <dl className="availability-status"><div><dt>{copy('listed')}</dt><dd>{amount(value.projection.listed_skus)}</dd></div><div><dt>{copy('confirmed')}</dt><dd>{amount(value.projection.confirmed_skus)}</dd></div>
          <div><dt>{copy('eligible')}</dt><dd>{amount(value.projection.eligible_skus)}</dd></div><div><dt>{copy('unpublishable')}</dt><dd>{amount(value.projection.unpublishable_skus)}</dd></div></dl>
        <p>{copy('stockSchedule')}: <strong>{copy(stock.enabled ? 'enabled' : 'disabled')}</strong> · {copy('stockPermission')}: <strong>{copy(stock.authorized ? 'enabled' : 'disabled')}</strong> · {copy('serverPermission')}: <strong>{copy(stock.server_enabled ? 'enabled' : 'disabled')}</strong></p>
        <p>{copy('orderCollection')}: <strong>{copy(orders.collector_enabled ? 'enabled' : 'disabled')}</strong> · {copy('orderProcessing')}: <strong>{t(`stockSettings.modes.${orders.processing_mode}`)}</strong>{!orders.processing_ready && <> · {copy('processingNotReady')}</>}</p>
        <p className="opening-stock-muted">{t('availabilitySync.capacity.intervals', { stock: amount(stock.interval_seconds), orders: amount(orders.poll_interval_seconds) })}</p>
        {deltaModel && <><dl className="availability-status" data-testid="upgates-capacity-batch-comparison">
          <div><dt>{copy('formerInitialReads')}</dt><dd data-testid="upgates-capacity-singleton-calls">{amount(baselineCalls)}</dd></div>
          <div><dt>{copy('batchedInitialReads')}</dt><dd data-testid="upgates-capacity-batch-calls">{amount(batchCalls)}</dd></div>
          <div><dt>{copy('initialReadSavings')}</dt><dd data-testid="upgates-capacity-read-savings">{savedInitialShare === null ? copy('unknown') : savedInitialShare.toLocaleString(i18n.language, { style: 'percent', maximumFractionDigits: 1 })}</dd></div>
        </dl><p>{t('availabilitySync.capacity.batchReadsHelp', { count: stock.batch_size })}</p></>}
        <div className="opening-stock-table-scroll"><table data-testid="upgates-capacity-estimate"><thead><tr><th>{copy('operation')}</th><th>{copy('perPass')}</th><th>{copy('dailyIfEnabled')}</th></tr></thead><tbody>
          <tr><td>{copy('unchangedStock')}</td><td>{amount(stock.unchanged_pass_calls)}</td><td>{amount(stock.daily_unchanged_if_enabled)}</td></tr>
          <tr><td>{copy('changedStock')}</td><td>{amount(stock.all_changed_pass_calls)}</td><td>{amount(stock.daily_all_changed_if_enabled)}</td></tr>
          <tr><td>{copy('emptyOrderPoll')}</td><td>{amount(orders.collector_empty_poll_calls)}</td><td>{amount(orders.collector_daily_if_enabled)}</td></tr>
          <tr><td>{copy('orderRetries')}</td><td>—</td><td>{amount(orders.retry_daily_if_processing)}</td></tr>
          <tr><td>{copy('orderHistory')}</td><td>—</td><td>{amount(orders.history_daily_if_processing)}</td></tr>
        </tbody></table></div>
        {deltaModel && <><p data-testid="upgates-capacity-audits">{t('availabilitySync.capacity.auditHelp', { hours: amount(typeof stock.audit_interval_seconds === 'number' ? stock.audit_interval_seconds / 3600 : null), calls: amount(stock.audit_pass_calls) })}</p><p>{copy('changedReadsHelp')}</p></>}
        <p className="opening-stock-muted">{copy('alternatives')}</p><p>{copy('excluded')}</p>
        {!!value.projection.eligibility_errors.length && <details><summary>{copy('eligibilityErrors')}</summary><ul>{value.projection.eligibility_errors.map(item => <li key={item.code}>{t(`availabilitySync.errors.${item.code}`, { defaultValue: copy('eligibilityUnknown') })} ({amount(item.count)})</li>)}</ul></details>}
      </>}
    </div>}
  </section>;
}

export function AvailabilitySyncPage() {
  const { t } = useTranslation();
  const [query] = useSearchParams();
  const selectedSupplier = /^[a-zA-Z0-9_-]{1,50}$/.test(query.get('supplier') || '') ? query.get('supplier')! : '';
  const revision = useSyncExternalStore(subscribeAccess, accessRevision);
  const [token, setToken] = useState(''), [shop, setShop] = useState(query.get('shop') === 'xtrek' ? 'xtrek' : 'biketrek'), [stockBusy, setStockBusy] = useState(false);
  useEffect(() => { setToken(''); }, [revision]);
  return <div className="opening-stock availability-sync"><Link className="opening-stock-back" to="/settings">← {t('availabilitySync.back')}</Link><header><div><h1>{t('availabilitySync.title')}</h1><p>{t('availabilitySync.subtitle')}</p></div>{hubUnlocked() && <Button variant="secondary" data-testid="lock-sync" onClick={() => unlockHub('')}>{t('availabilitySync.lock')}</Button>}</header>
    <p><Link to={`/settings/purchase-costs?shop=${shop}`}>{t('fifoCostSync.title')}</Link></p>
    {!hubUnlocked() ? <form className="opening-stock-panel opening-stock-unlock" onSubmit={event => { event.preventDefault(); if (token.trim()) unlockHub(token.trim()); }}><p>{t('availabilitySync.tokenHelp')}</p><label>{t('availabilitySync.token')}<input data-testid="sync-token" type="password" autoComplete="off" value={token} onChange={event => setToken(event.target.value)} /></label><Button data-testid="unlock-sync" type="submit" disabled={!token.trim()}>{t('availabilitySync.unlock')}</Button></form> : <React.Fragment key={revision}><SuppliersPanel key={selectedSupplier} selectedSupplier={selectedSupplier} />{!selectedSupplier && <><section className="opening-stock-panel availability-shop-selector"><label>{t('availabilitySync.shop')}<select data-testid="sync-shop" value={shop} disabled={stockBusy} onChange={event => setShop(event.target.value)}><option value="biketrek">BIKETREK</option><option value="xtrek">xTrek</option></select></label></section><CapacityPanel key={`capacity:${shop}`} shop={shop} /><StockPanel key={shop} shop={shop} onBusy={setStockBusy} /></>}</React.Fragment>}
  </div>;
}
