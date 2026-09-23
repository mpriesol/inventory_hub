import React, { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Button } from '../components/ui/Button.new';
import { accessRevision, hubUnlocked, subscribeAccess, unlockHub } from '../api/access';
import { finalizeOpening, getOpeningBatch, getOpeningOptions, listOpeningBatches, OpeningBatch, OpeningBatchInfo,
  OpeningIssue, OpeningOptions, previewOpening } from '../api/openingStock';
import './OpeningStockPage.css';

const csvHeader = 'sku;quantity;unit_cost;unit\n';
const initialForm = () => ({ warehouse: '', source: '', operator: '', countedAt: '', csv: csvHeader });
type Form = ReturnType<typeof initialForm>;
type Operation = 'options' | 'preview' | 'recover' | 'recent' | 'finalize' | 'file' | null;
const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
function localDate(value: string) {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return '';
  return new Date(date.getTime() - date.getTimezoneOffset() * 60_000).toISOString().slice(0, 19);
}

export function OpeningStockPage() {
  const { t, i18n } = useTranslation();
  const revision = useSyncExternalStore(subscribeAccess, accessRevision);
  const unlocked = hubUnlocked();
  const [token, setToken] = useState('');
  const [options, setOptions] = useState<OpeningOptions | null>(null);
  const [form, setForm] = useState(initialForm);
  const [loaded, setLoaded] = useState<{ batch: OpeningBatch; revision: number } | null>(null);
  const batch = loaded?.revision === revision ? loaded.batch : null;
  const [recent, setRecent] = useState<OpeningBatchInfo[] | null>(null);
  const [recoverId, setRecoverId] = useState('');
  const [issues, setIssues] = useState<OpeningIssue[]>([]);
  const [warnings, setWarnings] = useState<OpeningIssue[]>([]);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState<Operation>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [reconciled, setReconciled] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const [tablePage, setTablePage] = useState(0);
  const generation = useRef(0);
  const readController = useRef<AbortController | null>(null);
  const busyRef = useRef<Operation>(null);
  const writeInFlight = useRef(false);
  const previewId = useRef<string | null>(null);
  const fileGeneration = useRef(0);
  const lockedForm = busy === 'finalize' || uncertain;
  const expired = !!batch && new Date(batch.expires_at).getTime() <= Date.now();

  function clearDraft() {
    generation.current += 1;
    fileGeneration.current += 1;
    readController.current?.abort(); readController.current = null;
    previewId.current = null;
    setLoaded(null); setIssues([]); setWarnings([]); setConfirmed(false); setReconciled(false);
    setUncertain(false); setTablePage(0); setBusy(null); busyRef.current = null;
  }
  useEffect(() => {
    clearDraft(); setOptions(null); setRecent(null); setRecoverId(''); setForm(initialForm()); setToken('');
    return () => { generation.current += 1; fileGeneration.current += 1; readController.current?.abort(); };
  }, [revision]);

  function update(field: keyof Form, value: string) {
    if (lockedForm) return;
    clearDraft(); setError(''); setRecoverId(''); setForm(previous => ({ ...previous, [field]: value }));
  }
  function reportError(value: unknown) {
    const code = (value as Error & { code?: string }).code || 'request_failed';
    if (code === 'hub_access_required' || code === 'hub_access_not_configured') unlockHub('');
    setError(code);
  }
  async function read<T>(operation: Operation, request: (signal: AbortSignal) => Promise<T>, accept: (data: T) => void) {
    if (busyRef.current || !hubUnlocked()) return;
    const id = ++generation.current;
    const credential = accessRevision();
    readController.current?.abort();
    const controller = new AbortController(); readController.current = controller;
    busyRef.current = operation; setBusy(operation); setError('');
    const current = () => generation.current === id && credential === accessRevision() && !controller.signal.aborted;
    try { const data = await request(controller.signal); if (current()) accept(data); }
    catch (value) { if (current()) reportError(value); }
    finally { if (current()) { busyRef.current = null; setBusy(null); } }
  }
  function acceptBatch(value: OpeningBatch) {
    setLoaded({ batch: value, revision: accessRevision() }); setRecoverId(value.id);
    setIssues([]); setWarnings(value.warnings); setConfirmed(false); setReconciled(false); setUncertain(false); setTablePage(0);
    // Recovery keeps the frozen batch for finalization. Reconstructed CSV is
    // new input and must not reuse the original raw-input idempotency key.
    previewId.current = null;
    setForm({ warehouse: value.warehouse.code, source: value.source_reference, operator: value.operator_name,
      countedAt: localDate(value.counted_at), csv: csvHeader + value.lines.map(line => `"${line.sku.replace(/"/g, '""')}";${line.quantity};${line.unit_cost};ks`).join('\n') });
  }
  function recover(id = recoverId) {
    if (!uuidPattern.test(id.trim())) { setError('invalid_batch_id'); return; }
    read('recover', signal => getOpeningBatch(id.trim(), signal), acceptBatch);
  }
  function preview() {
    if (!options || busyRef.current || lockedForm) return;
    if (!options.warehouses.some(warehouse => warehouse.code === form.warehouse) || !form.source.trim() || !form.operator.trim() || !form.countedAt) {
      setError('required_fields'); return;
    }
    const counted = new Date(form.countedAt);
    if (!Number.isFinite(counted.getTime())) { setError('invalid_counted_at'); return; }
    if (new TextEncoder().encode(form.csv).length > options.limits.max_bytes) { setError('file_too_large'); return; }
    if (batch && new Date(batch.expires_at).getTime() <= Date.now()) previewId.current = null;
    previewId.current ||= crypto.randomUUID();
    setRecoverId(previewId.current); setLoaded(null); setIssues([]); setWarnings([]); setConfirmed(false); setReconciled(false);
    const input = { request_id: previewId.current, warehouse_code: form.warehouse, source_reference: form.source.trim(),
      operator_name: form.operator.trim(), counted_at: counted.toISOString(), csv_text: form.csv };
    read('preview', signal => previewOpening(input, signal), data => {
      setIssues(data.errors); setWarnings(data.warnings);
      if (data.ready && data.batch) {
        setLoaded({ batch: data.batch, revision: accessRevision() }); setRecoverId(data.batch.id); setTablePage(0);
      }
    });
  }
  async function loadFile(file?: File) {
    if (!file || lockedForm) return;
    clearDraft(); setError(''); setRecoverId('');
    const id = fileGeneration.current;
    const credential = accessRevision();
    if (file.size > (options?.limits.max_bytes ?? 1048576)) { setError('file_too_large'); return; }
    busyRef.current = 'file'; setBusy('file');
    try {
      const text = new TextDecoder('utf-8', { fatal: true }).decode(await file.arrayBuffer());
      if (id === fileGeneration.current && credential === accessRevision()) setForm(previous => ({ ...previous, csv: text }));
    } catch {
      if (id === fileGeneration.current && credential === accessRevision()) setError('invalid_utf8');
    } finally {
      if (id === fileGeneration.current && credential === accessRevision()) { busyRef.current = null; setBusy(null); }
    }
  }
  async function finalize() {
    if (!batch || batch.status !== 'prepared' || !confirmed || !reconciled || uncertain || busyRef.current || writeInFlight.current) return;
    if (new Date(batch.expires_at).getTime() <= Date.now()) { setError('opening_preview_expired'); return; }
    const selected = batch;
    const id = generation.current;
    const credential = accessRevision();
    writeInFlight.current = true; busyRef.current = 'finalize'; setBusy('finalize'); setError('');
    const current = () => id === generation.current && credential === accessRevision();
    try {
      const result = await finalizeOpening(selected);
      if (current()) {
        setLoaded({ batch: { ...selected, status: 'completed', completed_at: result.completed_at, result }, revision: credential });
        setConfirmed(false); setReconciled(false); setUncertain(false);
      }
    } catch (value) {
      if (current()) { setUncertain(true); setConfirmed(false); setReconciled(false); reportError(value); }
    } finally {
      writeInFlight.current = false;
      if (current()) { busyRef.current = null; setBusy(null); }
    }
  }
  const textDate = (value: string) => new Date(value).toLocaleString(i18n.language);
  const message = (code: string) => t(`openingStock.errors.${code}`, { defaultValue: t('openingStock.errors.request_failed') });
  function issueList(rows: OpeningIssue[], kind: 'errors' | 'warnings') {
    return !!rows.length && <div className={`opening-stock-${kind}`} role={kind === 'errors' ? 'alert' : undefined}>
      <strong>{t(`openingStock.${kind === 'errors' ? 'errorsTitle' : kind}`)}</strong><ul>{rows.slice(0, 100).map((row, index) => <li key={index}>
        {row.row !== null && `${t('openingStock.row')} ${row.row}: `}{message(row.code)}{row.field && <code> ({row.field})</code>}
      </li>)}</ul>{rows.length > 100 && <p>{t('openingStock.moreIssues', { count: rows.length - 100 })}</p>}
    </div>;
  }

  return <div className="opening-stock">
    <Link to="/stock" className="opening-stock-back">← {t('openingStock.back')}</Link>
    <header><div><h1>{t('openingStock.title')}</h1><p>{t('openingStock.subtitle')}</p></div>
      {unlocked && <Button variant="secondary" onClick={() => { unlockHub(''); setError(''); }}>{t('openingStock.lock')}</Button>}
    </header>
    <div className="opening-stock-notice">{t('openingStock.scope')}</div>
    {!unlocked ? <form className="opening-stock-panel opening-stock-unlock" onSubmit={event => { event.preventDefault(); if (token.trim()) { setError(''); unlockHub(token.trim()); setToken(''); } }}>
      <p>{t('openingStock.tokenHelp')}</p><label>{t('openingStock.token')}<input type="password" autoComplete="off" value={token} onChange={event => setToken(event.target.value)} /></label>
      <Button type="submit" disabled={!token.trim()}>{t('openingStock.unlock')}</Button>
    </form> : <>
      <section className="opening-stock-panel">
        <div className="opening-stock-actions"><h2>{t('openingStock.recovery')}</h2><Button variant="secondary" disabled={!!busy} onClick={() => read('recent', listOpeningBatches, data => setRecent(data.batches))}>{t('openingStock.loadRecent')}</Button></div>
        <p className="opening-stock-muted">{t('openingStock.recoveryHelp')}</p>
        <div className="opening-stock-actions"><label className="opening-stock-grow">{t('openingStock.batchId')}<input value={recoverId} disabled={busy === 'finalize' || uncertain} onChange={event => { clearDraft(); setError(''); setRecoverId(event.target.value); }} /></label>
          <Button variant="secondary" disabled={!!busy || !recoverId.trim()} onClick={() => recover()}>{t('openingStock.recover')}</Button></div>
        {recent && <div className="opening-stock-recent">{!recent.length && <p>{t('openingStock.noRecent')}</p>}{recent.map(item => <div key={item.id}>
          <div><strong>{item.source_reference}</strong><small>{item.warehouse.name} · {textDate(item.created_at)} · {t(`openingStock.status.${item.status}`)} · {item.summary.total_value} EUR</small></div>
          <Button size="sm" variant="secondary" disabled={!!busy || uncertain} onClick={() => recover(item.id)}>{t('openingStock.openBatch')}</Button>
        </div>)}</div>}
      </section>
      {batch?.status !== 'completed' && <section className="opening-stock-panel">
        <div className="opening-stock-actions"><h2>{t('openingStock.inputTitle')}</h2><Button variant="secondary" disabled={!!busy || uncertain} onClick={() => read('options', getOpeningOptions, setOptions)}>{t('openingStock.loadWarehouses')}</Button></div>
        <fieldset disabled={lockedForm}>
          <div className="opening-stock-fields">
            <label>{t('openingStock.warehouse')}<select value={form.warehouse} onChange={event => update('warehouse', event.target.value)} disabled={!options || lockedForm}>
              <option value="">{t('openingStock.chooseWarehouse')}</option>
              {options?.warehouses.map(warehouse => <option key={warehouse.id} value={warehouse.code}>{warehouse.name}</option>)}
              {!options && batch && <option value={batch.warehouse.code}>{batch.warehouse.name}</option>}
            </select></label>
            <label>{t('openingStock.source')}<input maxLength={255} value={form.source} onChange={event => update('source', event.target.value)} /></label>
            <label>{t('openingStock.operator')}<input maxLength={100} value={form.operator} onChange={event => update('operator', event.target.value)} /></label>
            <label>{t('openingStock.countedAt')}<input type="datetime-local" step="1" value={form.countedAt} onChange={event => update('countedAt', event.target.value)} /></label>
          </div>
          <p className="opening-stock-muted">{t('openingStock.csvHelp')}</p>
          <label>{t('openingStock.csvFile')}<input type="file" accept=".csv,text/csv" onChange={event => { loadFile(event.target.files?.[0]); event.target.value = ''; }} /></label>
          <label className="opening-stock-csv-label">{t('openingStock.csv')}<textarea rows={8} spellCheck={false} value={form.csv} onChange={event => update('csv', event.target.value)} /></label>
          <div className="opening-stock-actions"><Button disabled={!options || !!busy || uncertain} onClick={preview}>{t(busy === 'preview' ? 'common.loading' : 'openingStock.preview')}</Button>
            {options && <small>{t('openingStock.limits', { rows: options.limits.max_rows })}</small>}</div>
        </fieldset>
      </section>}
      {issueList(issues, 'errors')}{issueList(warnings, 'warnings')}
      {batch && <section className="opening-stock-panel" aria-live="polite">
        <div className="opening-stock-actions"><h2>{t(batch.status === 'completed' ? 'openingStock.completed' : 'openingStock.previewTitle')}</h2><span className="opening-stock-badge">{t(`openingStock.status.${batch.status}`)}</span></div>
        <dl className="opening-stock-summary">
          <div><dt>{t('openingStock.lines')}</dt><dd>{batch.summary.lines}</dd></div><div><dt>{t('openingStock.quantity')}</dt><dd>{batch.summary.quantity} ks</dd></div>
          <div><dt>{t('openingStock.total')}</dt><dd>{batch.summary.total_value} EUR</dd></div>
        </dl>
        <p>{batch.warehouse.name} · {batch.source_reference} · {batch.operator_name}</p>
        <p className="opening-stock-muted">{t('openingStock.countedAt')}: {textDate(batch.counted_at)} · {t('openingStock.batchId')}: <code>{batch.id}</code></p>
        {batch.status === 'prepared' && <p className="opening-stock-muted">{t('openingStock.expiresAt')}: {textDate(batch.expires_at)}</p>}
        <div className="opening-stock-table-scroll"><table><thead><tr>
          {['row', 'sku', 'name', 'quantity', 'unitCost', 'value'].map(key => <th scope="col" key={key}>{t(`openingStock.${key}`)}</th>)}
        </tr></thead><tbody>{batch.lines.slice(tablePage * 100, (tablePage + 1) * 100).map(line => <tr key={line.line_number}>
          <td>{line.line_number}</td><td><code>{line.sku}</code></td><td>{line.name}{line.warnings.map((code, index) => <small className="opening-stock-line-warning" key={index}>{message(code)}</small>)}</td>
          <td>{line.quantity} ks</td><td>{line.unit_cost} EUR</td><td>{line.value} EUR</td>
        </tr>)}</tbody></table></div>
        {batch.lines.length > 100 && <div className="opening-stock-pagination"><Button size="sm" variant="secondary" disabled={tablePage === 0} onClick={() => setTablePage(value => value - 1)}>{t('openingStock.previous')}</Button>
          <span>{t('openingStock.page', { page: tablePage + 1, pages: Math.ceil(batch.lines.length / 100) })}</span><Button size="sm" variant="secondary" disabled={(tablePage + 1) * 100 >= batch.lines.length} onClick={() => setTablePage(value => value + 1)}>{t('openingStock.next')}</Button></div>}
        {batch.status === 'completed' ? <div className="opening-stock-success"><strong>{t('openingStock.completedHelp')}</strong><p>{t('openingStock.movements', { count: batch.result?.movements_created ?? 0 })} · {textDate(batch.completed_at || batch.result?.completed_at || batch.created_at)}</p></div> : <>
          {expired && <div className="opening-stock-notice">{t('openingStock.expired')}</div>}
          {uncertain ? <div className="opening-stock-notice" role="alert"><p>{t('openingStock.uncertain')}</p><Button variant="secondary" disabled={!!busy} onClick={() => recover(batch.id)}>{t('openingStock.recover')}</Button></div> : <div className="opening-stock-confirmations">
            <label><input type="checkbox" checked={confirmed} disabled={!!busy || expired} onChange={event => setConfirmed(event.target.checked)} />{t('openingStock.confirmPhysical')}</label>
            <label><input type="checkbox" checked={reconciled} disabled={!!busy || expired} onChange={event => setReconciled(event.target.checked)} />{t('openingStock.confirmReceipts')}</label>
            <Button disabled={!confirmed || !reconciled || !!busy || expired} loading={busy === 'finalize'} onClick={finalize}>{t('openingStock.finalize')}</Button>
          </div>}
        </>}
        <Button variant="secondary" disabled={!!busy || uncertain} onClick={() => { clearDraft(); setForm(initialForm()); setRecoverId(''); setError(''); }}>{t('openingStock.newBatch')}</Button>
      </section>}
    </>}
    {error && <div className="opening-stock-errors" role="alert">{message(error)}</div>}
  </div>;
}
