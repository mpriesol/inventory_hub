import React, { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Download, RefreshCw } from 'lucide-react';
import { accessRevision, hubUnlocked, hubUser, subscribeAccess, unlockHub } from '../api/access';
import { createAtomerAvailabilityRun, downloadAtomerAvailabilityRun, exportAtomerAvailabilityRun,
  getAtomerAvailabilityOptions, getAtomerAvailabilityRun, listAtomerAvailabilityRuns, saveAtomerAvailabilityOptions, uploadAtomerAvailabilityAdmin,
  type AtomerAvailabilityConfigure, type AtomerAvailabilityCreate, type AtomerAvailabilityDetail, type AtomerAvailabilityExport, type AtomerAvailabilityOptions,
  type AtomerAvailabilityOverride, type AtomerAvailabilityRow, type AtomerAvailabilityRun, type AtomerAvailabilityView } from '../api/atomerAvailability';
import { Button } from '../components/ui/Button.new';
import { ProductThumb } from '../components/product/ProductDisplay';
import { StockCardLink } from '../components/product/StockCardLink';
import './AtomerAvailabilityPage.css';

const active = (run: AtomerAvailabilityRun | null) => !!run && ['queued', 'fetching'].includes(run.status);
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const errorCode = (error: unknown) => (error as { code?: string })?.code || 'request_failed';
const rejected = (error: unknown) => (error as { knownUnapplied?: boolean }).knownUnapplied === true || (error as { status?: number }).status === 422;
const draftKey = (id: string) => `hub.atomer-availability.draft.${id}`;
function stored<T>(key: string): T | null { try { return JSON.parse(sessionStorage.getItem(key) || 'null'); } catch { return null; } }
function remember(key: string, value: unknown) { try { value === null ? sessionStorage.removeItem(key) : sessionStorage.setItem(key, JSON.stringify(value)); } catch { /* A browser storage limit must not prevent a safe server operation. */ } }
interface SavedDraft { revision: number; overrides: Record<string, AtomerAvailabilityOverride>; pending?: AtomerAvailabilityExport }

/** A page slice retains the parent context even when some siblings are on another page. */
function families(rows: AtomerAvailabilityRow[]) {
  const result = new Map<string, { parent?: AtomerAvailabilityRow; name: string; image?: string | null; children: AtomerAvailabilityRow[] }>();
  for (const row of rows) {
    const key = row.parent_code || row.code;
    let family = result.get(key);
    if (!family) { family = { name: row.parent_code ? row.parent_name || row.parent_code : row.name, image: row.parent_code ? row.parent_image : row.image, children: [] }; result.set(key, family); }
    if (row.kind === 'parent') { family.parent = row; family.name = row.name; family.image = row.image; }
    else family.children.push(row);
  }
  return [...result.entries()];
}

export function AtomerAvailabilityPage() {
  const { t, i18n } = useTranslation(), [params, setParams] = useSearchParams();
  const access = useSyncExternalStore(subscribeAccess, accessRevision, accessRevision);
  const batch = params.get('receiving_batch') || '', selectedId = params.get('run') || '';
  const invalidBatch = !!batch && !uuid.test(batch);
  const pendingKey = `hub.atomer-availability.pending.${batch || 'all'}`;
  const [options, setOptions] = useState<AtomerAvailabilityOptions | null>(null), [runs, setRuns] = useState<AtomerAvailabilityRun[]>([]);
  const [run, setRun] = useState<AtomerAvailabilityDetail | null>(null), [overrides, setOverrides] = useState<Record<string, AtomerAvailabilityOverride>>({});
  const [view, setView] = useState<AtomerAvailabilityView>('changes');
  const viewRef = useRef<AtomerAvailabilityView>('changes'), readingRows = useRef(false);
  const [pendingExport, setPendingExport] = useState<AtomerAvailabilityExport | null>(null), [pendingRun, setPendingRun] = useState('');
  const [exportRecovered, setExportRecovered] = useState(false), [creationMissing, setCreationMissing] = useState(false);
  const [warehouse, setWarehouse] = useState(''), [enabled, setEnabled] = useState(false), [sourceUrl, setSourceUrl] = useState('');
  const [adminUrl, setAdminUrl] = useState(''), [adminComplete, setAdminComplete] = useState(false), [adminFile, setAdminFile] = useState<File | null>(null);
  const [pendingSettings, setPendingSettings] = useState<AtomerAvailabilityConfigure | null>(null);
  const [token, setToken] = useState(''), [busy, setBusy] = useState(false), [error, setError] = useState(''), [notice, setNotice] = useState('');
  const request = useRef<AbortController | null>(null), inFlight = useRef(false), generation = useRef(0);
  const adminFileInput = useRef<HTMLInputElement | null>(null);
  const currentRun = useRef(run); currentRun.current = run;
  const visibleRuns = batch ? runs.filter(item => item.receiving_batch_id === batch) : runs;
  const canConfigure = !hubUser() || hubUser()?.role === 'admin';
  const dirty = Object.keys(overrides).length > 0;
  const settingsDirty = !!options && (enabled !== options.enabled || warehouse !== (options.warehouse_code || '') || !!sourceUrl || !!adminUrl || adminComplete !== !!options.admin_complete);
  const date = (value?: string | null) => value ? new Date(value).toLocaleString(i18n.language) : t('atomerAvailability.notYet');
  const message = (code: string) => t(`atomerAvailability.errors.${code}`, { defaultValue: t('atomerAvailability.requestFailed') });

  async function operation<T>(action: (signal: AbortSignal) => Promise<T>, accept: (value: T) => void, failed?: (value: unknown) => void) {
    if (inFlight.current || !hubUnlocked()) return;
    inFlight.current = true; setBusy(true); setError('');
    const controller = new AbortController(), credential = accessRevision(), number = ++generation.current;
    request.current = controller;
    const isCurrent = () => number === generation.current && credential === accessRevision() && !controller.signal.aborted;
    try { const value = await action(controller.signal); if (isCurrent()) accept(value); }
    catch (value) { if (isCurrent()) { setError(errorCode(value)); failed?.(value); } }
    finally { if (request.current === controller) { inFlight.current = false; if (isCurrent()) setBusy(false); } }
  }
  function resetOptions(value: AtomerAvailabilityOptions) { setOptions(value); setWarehouse(value.warehouse_code || ''); setEnabled(value.enabled); setSourceUrl(''); setAdminUrl(''); setAdminComplete(!!value.admin_complete); setPendingSettings(null); }
  function acceptRun(value: AtomerAvailabilityDetail) {
    if (value.view !== viewRef.current) return;
    if (batch && value.receiving_batch_id !== batch) throw Object.assign(new Error('Receipt export scope changed'), { code: 'atomer_availability_receipt_scope_invalid' });
    setRun(value);
    if (value.id === pendingRun || value.id === stored<AtomerAvailabilityCreate>(pendingKey)?.request_id) {
      setPendingRun(''); remember(pendingKey, null);
    }
    const saved = stored<SavedDraft>(draftKey(value.id));
    if (value.status === 'exported') { setOverrides({}); setPendingExport(null); remember(draftKey(value.id), null); }
    else if (saved?.revision === value.revision) { setOverrides(saved.overrides || {}); setPendingExport(saved.pending || null); }
    else { setOverrides({}); setPendingExport(null); }
    setRuns(old => [value, ...old.filter(item => item.id !== value.id)]);
  }
  function selectRun(id: string) {
    const next = new URLSearchParams(params); next.set('run', id); setParams(next, { replace: true });
  }
  function loadRows(nextView: AtomerAvailabilityView, offset = 0) {
    if (!run || (inFlight.current && !readingRows.current)) return;
    // A later filter/page request supersedes only an earlier read, never a write.
    generation.current++; request.current?.abort(); inFlight.current = false;
    viewRef.current = nextView; setView(nextView); readingRows.current = true;
    void operation(signal => getAtomerAvailabilityRun(run.id, offset, signal, nextView), acceptRun)
      .finally(() => { if (viewRef.current === nextView && !inFlight.current) readingRows.current = false; });
  }
  function refresh() {
    operation(async signal => {
      const [nextOptions, history] = await Promise.all([getAtomerAvailabilityOptions(signal), listAtomerAvailabilityRuns(signal)]);
      const id = stored<AtomerAvailabilityCreate>(pendingKey)?.request_id || selectedId || (!batch ? history.items[0]?.id : '');
      const detail = id && uuid.test(id) ? await getAtomerAvailabilityRun(id, currentRun.current?.id === id && currentRun.current.view === viewRef.current ? currentRun.current.offset : 0, signal, viewRef.current) : null;
      return { nextOptions, history, detail };
    }, ({ nextOptions, history, detail }) => {
      resetOptions(nextOptions); setRuns(history.items);
      if (detail) { acceptRun(detail); if (selectedId && detail.id !== selectedId) selectRun(detail.id); }
    });
  }
  useEffect(() => {
    generation.current++; request.current?.abort(); inFlight.current = false; readingRows.current = false;
    setOptions(null); setRun(null); setRuns([]); setOverrides({}); setPendingExport(null); setBusy(false); setError(''); setNotice(''); setSourceUrl(''); setAdminUrl(''); setAdminFile(null); setPendingSettings(null);
    const saved = stored<AtomerAvailabilityCreate>(pendingKey); setPendingRun(saved && uuid.test(saved.request_id) ? saved.request_id : '');
    setExportRecovered(false); setCreationMissing(false);
    if (hubUnlocked() && !invalidBatch) refresh();
    return () => { generation.current++; request.current?.abort(); inFlight.current = false; };
  }, [access, batch, selectedId]);
  useEffect(() => {
    if (!active(run) || busy || !hubUnlocked()) return;
    const id = run!.id;
    const timer = window.setTimeout(() => operation(signal => getAtomerAvailabilityRun(id, 0, signal, viewRef.current), acceptRun), 3000);
    return () => window.clearTimeout(timer);
  }, [run, busy, access]);
  useEffect(() => {
    if (!dirty && !pendingExport && !pendingRun) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ''; };
    window.addEventListener('beforeunload', warn); return () => window.removeEventListener('beforeunload', warn);
  }, [dirty, pendingExport, pendingRun]);

  function saveSettings() {
    if (!options || !warehouse || !canConfigure || inFlight.current) return;
    const body = pendingSettings || { request_id: crypto.randomUUID(), expected_revision: options.revision, enabled, warehouse_code: warehouse,
      admin_complete: adminComplete, ...(sourceUrl.trim() ? { feed_url: sourceUrl.trim() } : {}), ...(adminUrl.trim() ? { admin_export_url: adminUrl.trim() } : {}) };
    setPendingSettings(body);
    operation(() => saveAtomerAvailabilityOptions(body), value => { resetOptions(value); setNotice('settingsSaved'); },
      value => { if (rejected(value)) setPendingSettings(null); });
  }
  function uploadAdmin() {
    if (!adminFile || !canConfigure || inFlight.current) return;
    operation(async signal => { await uploadAtomerAvailabilityAdmin(adminFile, adminComplete); return getAtomerAvailabilityOptions(signal); }, value => { resetOptions(value); setAdminFile(null); if (adminFileInput.current) adminFileInput.current.value = ''; setNotice('adminUploaded'); });
  }
  function createPreview() {
    if (inFlight.current || (!batch && !warehouse) || dirty || pendingExport || pendingRun || invalidBatch || settingsDirty) return;
    const id = crypto.randomUUID(), body = { request_id: id, ...(batch ? { receiving_batch_id: batch } : { warehouse_code: warehouse }) };
    setPendingRun(id); remember(pendingKey, body); setNotice(''); setCreationMissing(false);
    operation(() => createAtomerAvailabilityRun(body),
      value => { setPendingRun(''); remember(pendingKey, null); selectRun(value.id); },
      value => { if (rejected(value)) { setPendingRun(''); remember(pendingKey, null); } });
  }
  function recoverRun() {
    const id = pendingRun || run?.id; if (!id) return;
    operation(signal => getAtomerAvailabilityRun(id, run?.view === viewRef.current ? run.offset : 0, signal, viewRef.current), value => { acceptRun(value); setExportRecovered(true); if (selectedId !== id) selectRun(id); },
      value => { if (pendingRun && (value as { status?: number }).status === 404) setCreationMissing(true); });
  }
  function retryCreation() {
    const body = stored<AtomerAvailabilityCreate>(pendingKey);
    if (!creationMissing || !body || body.request_id !== pendingRun || inFlight.current) return;
    setCreationMissing(false);
    operation(() => createAtomerAvailabilityRun(body), value => { setPendingRun(''); remember(pendingKey, null); selectRun(value.id); });
  }
  function change(row: AtomerAvailabilityRow, patch: Partial<AtomerAvailabilityOverride>) {
    if (!run || run.status !== 'ready' || pendingExport || busy) return;
    const next = { ...overrides }, item = { code: row.code, ...next[row.code], ...patch };
    if (item.visibility === row.proposed_visibility) delete item.visibility;
    if (item.availability === row.proposed_availability) delete item.availability;
    if (item.visibility === undefined && item.availability === undefined) delete next[row.code]; else next[row.code] = item;
    setOverrides(next); remember(draftKey(run.id), { revision: run.revision, overrides: next });
  }
  function discard() { if (!run || pendingExport) return; setOverrides({}); remember(draftKey(run.id), null); }
  function exportPreview() {
    if (!run || run.status !== 'ready' || busy || inFlight.current || (pendingExport && !exportRecovered)) return;
    const payload = pendingExport || { expected_revision: run.revision, overrides: Object.values(overrides).sort((a, b) => a.code.localeCompare(b.code)) };
    setPendingExport(payload); setExportRecovered(false); remember(draftKey(run.id), { revision: run.revision, overrides, pending: payload });
    operation(async signal => { await exportAtomerAvailabilityRun(run.id, payload); return getAtomerAvailabilityRun(run.id, 0, signal, viewRef.current); },
      value => { acceptRun(value); setNotice('exportReady'); },
      value => { if (rejected(value)) { setPendingExport(null); remember(draftKey(run.id), { revision: run.revision, overrides }); } });
  }
  const rowView = (row: AtomerAvailabilityRow) => {
    const edit = overrides[row.code], visible = edit?.visibility ?? row.proposed_visibility;
    const availability = edit?.availability ?? row.proposed_availability;
    const blocked = ['blocked', 'unmatched'].includes(row.status) || row.reasons.length > 0 || row.current_visibility === null;
    const editable = run?.status === 'ready' && !blocked && !pendingExport && !busy;
    return <tr key={row.code} className={row.kind === 'parent' ? 'atomer-family-row' : row.kind === 'variant' ? 'atomer-variant' : ''} data-testid={`atomer-row-${row.code}`}>
      <td><div className="atomer-product"><ProductThumb key={row.image || row.code} url={row.image} name={row.name} size={48} /><div className="atomer-product-text">
        <strong>{row.product_id ? <StockCardLink productId={row.product_id} sku={row.sku} warehouseCode={run?.warehouse_code || warehouse}>{row.name}</StockCardLink> : row.name}</strong>
        <code>{row.code}</code>{row.sku && row.sku !== row.code && <small>Hub: {row.sku}</small>}
      </div></div></td>
      <td>{row.qty_available === null ? t('atomerAvailability.unknown') : row.qty_available}</td>
      <td><span className="atomer-before">{t('atomerAvailability.current')}: {row.current_visibility === null ? t('atomerAvailability.unknown') : t(`atomerAvailability.${row.current_visibility ? 'visible' : 'hidden'}`)}</span>
        <label className="atomer-visibility"><input type="checkbox" data-testid={`atomer-visible-${row.code}`} checked={visible === true} disabled={!editable || visible === null}
          aria-label={t('atomerAvailability.visibilityFor', { code: row.code })} onChange={event => change(row, { visibility: event.target.checked })} />
          {visible === null ? t('atomerAvailability.unknown') : t(`atomerAvailability.${visible ? 'visible' : 'hidden'}`)}</label></td>
      <td><span className="atomer-before">{t('atomerAvailability.current')}: {row.current_availability || t('atomerAvailability.unknown')}</span>
        <input list="atomer-availability-values" data-testid={`atomer-availability-${row.code}`} aria-label={t('atomerAvailability.availabilityFor', { code: row.code })}
          value={availability || ''} maxLength={100} disabled={!editable} onChange={event => change(row, { availability: event.target.value })} /></td>
      <td>{edit && <span className="atomer-status">{t('atomerAvailability.edited')}</span>}{!edit && <span className="atomer-status">{t(`atomerAvailability.rowStatus.${row.status}`)}</span>}
        {!!row.reasons.length && <div className="atomer-row-warning">{row.reasons.map(reason => <span key={reason}>{message(reason)}</span>)}</div>}</td>
    </tr>;
  };

  return <main className="atomer-availability">
    <header><h1>{t('atomerAvailability.title')}</h1><p>{t('atomerAvailability.subtitle')}</p>{batch && <p><Link to={`/receiving?batch=${encodeURIComponent(batch)}`}>{t('atomerAvailability.backToReceiving')}</Link></p>}</header>
    {invalidBatch && <p role="alert" className="atomer-error">{t('atomerAvailability.invalidBatch')}</p>}
    {!hubUnlocked() ? <form className="atomer-fields" onSubmit={event => { event.preventDefault(); unlockHub(token); setToken(''); }}>
      <label>{t('atomerAvailability.access')}<input type="password" value={token} data-testid="atomer-token" autoComplete="off" onChange={event => setToken(event.target.value)} /></label>
      <Button type="submit" disabled={!token.trim()} data-testid="atomer-unlock">{t('atomerAvailability.unlock')}</Button>
    </form> : !invalidBatch && <>
      {error && <p role="alert" className="atomer-error">{message(error)}</p>}
      {notice && <p role="status">{t(`atomerAvailability.${notice}`)}</p>}
      <section className="atomer-panel">
        <div className="atomer-heading"><h2>{t('atomerAvailability.previewTitle')}</h2><Button variant="secondary" icon={<RefreshCw size={16} />} disabled={busy || settingsDirty} onClick={refresh} data-testid="atomer-refresh">{t('atomerAvailability.refresh')}</Button></div>
        <p className="atomer-muted">{t(batch ? 'atomerAvailability.receivingScope' : 'atomerAvailability.allScope')}</p>
        {options && <>
          <p>{t(options.enabled ? 'atomerAvailability.scheduleOn' : 'atomerAvailability.scheduleOff')} {options.enabled && options.next_run_at && t('atomerAvailability.nextRun', { date: date(options.next_run_at) })}</p>
          {!options.source_configured && <p className="atomer-notice">{t('atomerAvailability.sourceMissing')}</p>}
          {options.last_error && <p role="alert" className="atomer-error">{message(options.last_error)}</p>}
          <details><summary>{t('atomerAvailability.settings')}</summary>
            <div className="atomer-fields">
              <label>{t('atomerAvailability.warehouse')}<select value={warehouse} disabled={busy || !canConfigure || !!pendingSettings} data-testid="atomer-warehouse" onChange={event => setWarehouse(event.target.value)}><option value="">{t('atomerAvailability.chooseWarehouse')}</option>{options.warehouses.map(item => <option key={item.code} value={item.code}>{item.name}</option>)}</select></label>
              <label className="atomer-feed-field">{t('atomerAvailability.feedUrl')}<input type="password" autoComplete="new-password" value={sourceUrl} disabled={busy || !canConfigure || !!pendingSettings} data-testid="atomer-feed-url"
                placeholder={t(options.source_configured ? 'atomerAvailability.sourceSaved' : 'atomerAvailability.sourcePlaceholder')} onChange={event => setSourceUrl(event.target.value)} /></label>
              <label className="atomer-check-label"><input type="checkbox" checked={enabled} disabled={busy || !canConfigure || !!pendingSettings} data-testid="atomer-enabled" onChange={event => setEnabled(event.target.checked)} />{t('atomerAvailability.daily')}</label>
              <label className="atomer-feed-field">{t('atomerAvailability.adminUrl')}<input type="password" autoComplete="new-password" value={adminUrl} disabled={busy || !canConfigure || !!pendingSettings} data-testid="atomer-admin-url"
                placeholder={t(options.admin_source_configured ? 'atomerAvailability.sourceSaved' : 'atomerAvailability.sourcePlaceholder')} onChange={event => setAdminUrl(event.target.value)} /></label>
              <label className="atomer-check-label"><input type="checkbox" checked={adminComplete} disabled={busy || !canConfigure || !!pendingSettings} data-testid="atomer-admin-complete" onChange={event => setAdminComplete(event.target.checked)} />{t('atomerAvailability.adminComplete')}</label>
              <Button variant="secondary" disabled={busy || !canConfigure || !warehouse || !settingsDirty} onClick={saveSettings} data-testid="atomer-save-settings">{t('atomerAvailability.saveSettings')}</Button>
              {(settingsDirty || pendingSettings) && <Button variant="secondary" disabled={busy} onClick={() => operation(getAtomerAvailabilityOptions, resetOptions)} data-testid="atomer-reset-settings">{t('atomerAvailability.resetSettings')}</Button>}
            </div>{pendingSettings && !busy && <p className="atomer-notice">{t('atomerAvailability.settingsUncertain')}</p>}
            {canConfigure && <><p className="atomer-muted">{t('atomerAvailability.adminSourceHelp')}</p>{options.admin_upload && <p className={options.admin_upload.fresh ? 'atomer-muted' : 'atomer-notice'}>{t('atomerAvailability.adminUploadDate', { date: date(options.admin_upload.imported_at) })} {!options.admin_upload.fresh && t('atomerAvailability.adminUploadStale')}</p>}<div className="atomer-fields"><label>{t('atomerAvailability.adminFile')}<input ref={adminFileInput} type="file" accept=".csv,text/csv" disabled={busy || !!pendingSettings} data-testid="atomer-admin-file" onChange={event => setAdminFile(event.target.files?.[0] || null)} /></label><Button variant="secondary" disabled={busy || !!pendingSettings || !adminFile} onClick={uploadAdmin} data-testid="atomer-upload-admin">{t('atomerAvailability.uploadAdmin')}</Button></div></>}
            {!canConfigure && <p className="atomer-muted">{t('atomerAvailability.adminRequired')}</p>}
          </details>
          <div className="atomer-actions"><Button disabled={busy || !options.source_configured || (!batch && !warehouse) || active(run) || dirty || !!pendingExport || !!pendingRun || settingsDirty} onClick={createPreview} data-testid="atomer-create">{t(batch ? 'atomerAvailability.createReceivingPreview' : 'atomerAvailability.createPreview')}</Button>
            {dirty && !pendingExport && <Button variant="secondary" disabled={busy} onClick={discard} data-testid="atomer-discard">{t('atomerAvailability.discard')}</Button>}
          </div>
        </>}
        {pendingRun && <div className="atomer-notice" role="alert"><p>{t('atomerAvailability.creationUncertain')}</p><Button variant="secondary" disabled={busy} onClick={recoverRun} data-testid="atomer-recover-run">{t('atomerAvailability.recover')}</Button>
          {creationMissing && <Button variant="secondary" disabled={busy} onClick={retryCreation} data-testid="atomer-retry-create">{t('atomerAvailability.retryCreation')}</Button>}</div>}
        {!options && !busy && <Button variant="secondary" onClick={refresh}>{t('atomerAvailability.refresh')}</Button>}
        {busy && <p role="status">{t('atomerAvailability.loading')}</p>}
      </section>
      {run && <section className="atomer-panel" data-testid="atomer-preview">
        <div className="atomer-heading"><h2>{t('atomerAvailability.resultTitle')}</h2><span className="atomer-status" data-testid="atomer-run-status">{t(`atomerAvailability.runStatus.${run.status}`)}</span></div>
        <p className="atomer-muted">{t('atomerAvailability.sourceDate', { date: date(run.source_fetched_at) })}</p>
        {run.coverage?.kind && <p className={run.coverage.kind === 'admin_complete' ? 'atomer-muted' : 'atomer-notice'} data-testid="atomer-coverage">{t(`atomerAvailability.coverage.${run.coverage.kind}`, { hidden: run.coverage.hidden_rows, parents: run.coverage.missing_parents })}</p>}
        {run.warnings?.map(code => <p className="atomer-notice" key={code}>{message(code)}{code === 'source_rows_blocked' && run.coverage?.source_blocked_rows !== undefined && ` (${run.coverage.source_blocked_rows})`}</p>)}
        {!!run.missing_products?.length && <div className="atomer-notice" data-testid="atomer-missing-products"><strong>{t('atomerAvailability.missingProducts')}</strong><ul>{run.missing_products.map(product => <li key={product.id}><StockCardLink productId={product.id} sku={product.sku}>{product.sku} - {product.name}</StockCardLink></li>)}</ul></div>}
        {run.error && <p role="alert" className="atomer-error">{message(run.error)}</p>}
        {run.summary && <dl className="atomer-counts">{(['total', 'changed', 'blocked', 'unmatched'] as const).map(key => <div key={key}><dt>{t(`atomerAvailability.counts.${key}`)}</dt><dd>{run.summary[key] ?? 0}</dd></div>)}</dl>}
        {['ready', 'exported'].includes(run.status) && <>
          {run.status === 'ready' && <p className="atomer-muted">{t('atomerAvailability.editHelp')}</p>}
          {!!run.summary?.blocked && <p className="atomer-notice">{t('atomerAvailability.blockedHelp')}</p>}
          {pendingExport && <div className="atomer-notice" role="alert"><p>{t('atomerAvailability.exportUncertain')}</p><Button variant="secondary" disabled={busy} onClick={recoverRun} data-testid="atomer-recover-export">{t('atomerAvailability.recover')}</Button></div>}
          <div className="atomer-fields"><label>{t('atomerAvailability.viewLabel')}<select value={view} disabled={busy && !readingRows.current} data-testid="atomer-view" onChange={event => loadRows(event.target.value as AtomerAvailabilityView)}>
            {(['changes', 'all', 'issues'] as const).map(value => <option key={value} value={value}>{t(`atomerAvailability.views.${value}`)}</option>)}
          </select></label><span className="atomer-muted">{t('atomerAvailability.viewHelp')}</span></div>
          <datalist id="atomer-availability-values">{run.availability_options?.map(value => <option key={value} value={value} />)}</datalist>
          {run.view === view && <>
          <div className="atomer-table-scroll"><table className="atomer-table"><thead><tr>{['product', 'availableQty', 'visibility', 'availability', 'result'].map(key => <th key={key}>{t(`atomerAvailability.${key}`)}</th>)}</tr></thead>
            {families(run.rows || []).map(([key, family]) => <tbody key={key}>
              {family.parent ? rowView(family.parent) : family.children[0]?.parent_code && <tr className="atomer-family-row"><td colSpan={5}><div className="atomer-product"><ProductThumb url={family.image} name={family.name} size={48} /><div className="atomer-product-text"><strong>{family.name}</strong><code>{key}</code></div></div></td></tr>}
              {family.children.map(rowView)}
            </tbody>)}
          </table></div>
          {!run.rows?.length && <p>{t('atomerAvailability.noRows')}</p>}
          <div className="atomer-pagination"><Button variant="secondary" size="sm" disabled={busy || run.offset === 0} onClick={() => loadRows(view, Math.max(0, run.offset - 100))} data-testid="atomer-previous">{t('atomerAvailability.previous')}</Button>
            <span>{t('atomerAvailability.page', { from: run.total ? run.offset + 1 : 0, to: Math.min(run.offset + (run.rows?.length || 0), run.total), total: run.total })}</span>
            <Button variant="secondary" size="sm" disabled={busy || run.offset + (run.rows?.length || 0) >= run.total} onClick={() => loadRows(view, run.offset + 100)} data-testid="atomer-next">{t('atomerAvailability.next')}</Button></div>
          </>}
          <div className="atomer-actions">
            {run.status === 'ready' && <Button disabled={busy || run.view !== view || (!!pendingExport && !exportRecovered) || !run.summary?.total || Object.values(overrides).some(value => value.availability !== undefined && !value.availability.trim())} onClick={exportPreview} data-testid="atomer-export">{t(pendingExport ? 'atomerAvailability.retryExport' : 'atomerAvailability.exportCsv')}</Button>}
            {run.download_available && <Button icon={<Download size={16} />} disabled={busy} onClick={() => operation(() => downloadAtomerAvailabilityRun(run.id), () => {})} data-testid="atomer-download">{t('atomerAvailability.download')}</Button>}
          </div>
          {run.status === 'exported' && <p>{t('atomerAvailability.manualImport')}</p>}
        </>}
      </section>}
      <section className="atomer-panel"><h2>{t('atomerAvailability.history')}</h2><p className="atomer-muted">{t('atomerAvailability.retention')}</p>
        <div className="atomer-downloads">{visibleRuns.map(item => <div className="atomer-download" key={item.id}><div><strong>{date(item.created_at)}</strong><small>{t(`atomerAvailability.runStatus.${item.status}`)} · {t('atomerAvailability.changedCount', { count: item.summary?.changed || 0 })}</small></div>
          <div className="atomer-actions"><Button variant="secondary" size="sm" disabled={busy || dirty || !!pendingExport || !!pendingRun || item.id === run?.id} onClick={() => selectRun(item.id)} data-testid={`atomer-open-${item.id}`}>{t('atomerAvailability.open')}</Button>
            {item.download_available && <Button variant="secondary" size="sm" icon={<Download size={14} />} disabled={busy} onClick={() => operation(() => downloadAtomerAvailabilityRun(item.id), () => {})} data-testid={`atomer-download-${item.id}`}>CSV</Button>}</div></div>)}</div>
        {!visibleRuns.length && <p>{t('atomerAvailability.noHistory')}</p>}
      </section>
    </>}
  </main>;
}
