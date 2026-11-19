import React, { useEffect, useRef, useState } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { CheckCircle, FileText, Plus, RefreshCw, Upload, X } from 'lucide-react';
import { Button } from '../components/ui/Button.new';
import { BarcodeCamera } from '../components/BarcodeCamera';
import { StockCardLink } from '../components/product/StockCardLink';
import { ApiResponseError } from '../api/client';
import { InvoiceUploadModal } from '../components/InvoiceUploadModal';
import { getProductEditorOptions, type ProductEditorOptions } from '../api/productEditor';
import { defaultWarehouseCode } from '../utils/warehouses';
import { prepareReceivingProducts } from '../api/receiving';
import { useReceivingScanner, type ReceivingScanCommand, type ScanInvoiceChoice } from '../hooks/useReceivingScanner';
import {
  archiveReceivingBatch, createReceivingBatch, finalizeReceivingBatch, getReceivingBatch, listReceivingBatches,
  getReceivingManualProduct, manualReceivingBatchScan,
  mutateReceivingBatchScan, previewReceivingBatch, scanReceivingBatch,
  type ReceivingBatchCandidate, type ReceivingBatchFinalizeCommand, type ReceivingBatchItem,
  type ReceivingBatchListItem, type ReceivingBatchMutation, type ReceivingBatchScan,
  type ReceivingBatchScanResult, type ReceivingBatchSummary, type ReceivingBatchArchiveCommand,
  type ReceivingManualProduct, type ReceivingManualScanCommand,
} from '../api/receivingBatches';
import { formatNumber } from '../utils/format';
import { receivingSourceError } from '../features/receiving/sourceErrors';
import './MixedReceivingPage.css';

type PendingCommand = { kind: 'finalize'; body: ReceivingBatchFinalizeCommand }
  | { kind: 'scan'; body: ReceivingBatchMutation }
  | { kind: 'manual'; body: ReceivingManualScanCommand }
  | { kind: 'archive'; batchId: string; body: ReceivingBatchArchiveCommand };
const storageKey = (id: string) => `receiving-batch-command:${id}`;
const readPending = (id: string): PendingCommand | null => {
  try {
    const value = JSON.parse(sessionStorage.getItem(storageKey(id)) || 'null');
    return value && ['finalize', 'scan', 'manual', 'archive'].includes(value.kind) && typeof value.body?.request_id === 'string' ? value : null;
  } catch { return null; }
};
const savePending = (id: string, command: PendingCommand | null) => {
  try {
    if (command) {
      const encoded = JSON.stringify(command); sessionStorage.setItem(storageKey(id), encoded);
      return sessionStorage.getItem(storageKey(id)) === encoded;
    }
    sessionStorage.removeItem(storageKey(id)); return true;
  } catch { return false; }
};
const readCreateRequest = (): { request_id: string; warehouse_code: string } | null => {
  try {
    const value = JSON.parse(sessionStorage.getItem('receiving-new-batch-v2') || 'null');
    return value && typeof value.request_id === 'string' && typeof value.warehouse_code === 'string' && value.warehouse_code ? value : null;
  } catch { return null; }
};
const knownRejection = (error: unknown) => error instanceof ApiResponseError
  && error.status >= 400 && error.status < 500 && ![408, 429].includes(error.status);
const detailOf = (error: unknown) => error instanceof ApiResponseError && error.detail && typeof error.detail === 'object'
  ? error.detail as { code?: string; message?: string; candidates?: ReceivingBatchCandidate[] } : null;

export function MixedReceivingPage() {
  const [query] = useSearchParams();
  return <ReceivingBatchWorkspace key={query.get('batch') || 'new'} />;
}

function ReceivingBatchWorkspace() {
  const { t, i18n } = useTranslation();
  const c = (key: string, values: Record<string, unknown> = {}) => t(`receiving.mixed.${key}`, values);
  const [query, setQuery] = useSearchParams();
  const navigate = useNavigate();
  const id = query.get('batch') || '';
  const scope = useRef(id); scope.current = id;
  const createRequest = useRef(readCreateRequest());
  const [batch, setBatch] = useState<ReceivingBatchSummary | null>(null);
  const [openBatches, setOpenBatches] = useState<ReceivingBatchListItem[]>([]);
  const [warehouses, setWarehouses] = useState<ProductEditorOptions['warehouses']>([]);
  const [warehouse, setWarehouse] = useState('');
  const [warehouseReady, setWarehouseReady] = useState(false);
  const [history, setHistory] = useState<ReceivingBatchListItem[] | null>(null);
  const [busy, setBusy] = useState('');
  const lock = useRef(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [code, setCode] = useState('');
  const codeRef = useRef('');
  const input = useRef<HTMLInputElement>(null);
  const [preview, setPreview] = useState(false);
  const [pending, setPending] = useState<PendingCommand | null>(() => readPending(id));
  const [missing, setMissing] = useState<ReceivingScanCommand | null>(null);
  const [uploadSupplier, setUploadSupplier] = useState('');
  const [showUpload, setShowUpload] = useState(false);
  const [manual, setManual] = useState<ReceivingManualProduct | null>(null);
  const [manualValues, setManualValues] = useState({ qty: '1', reason: '', cost_status: 'unknown' as 'unknown' | 'known' | 'provisional', unit_cost: '' });
  const focusNeeded = useRef(true);
  const [reassign, setReassign] = useState<ReceivingBatchScan | null>(null);
  const [lastCode, setLastCode] = useState('');
  const [legacyCandidates, setLegacyCandidates] = useState<ReceivingBatchCandidate[]>([]);
  const date = (value: string | null) => value ? new Date(value).toLocaleDateString(i18n.language) : c('dateUnknown');
  const number = (value: string | number | null | undefined) => value === null || value === undefined ? c('unknown') : formatNumber(value, i18n.language, 3);
  const errorMessage = (failure: unknown) => {
    const detail = detailOf(failure);
    return receivingSourceError(failure, t) || (detail?.code
      ? c(`errors.${detail.code}`, { defaultValue: detail.message || c('failed') }) : c('failed'));
  };
  const updateBatch = (value: ReceivingBatchSummary, expectedId = id) => {
    if (scope.current !== expectedId) return;
    setBatch(value); setPreview(false);
  };

  const scanner = useReceivingScanner<ReceivingBatchScanResult>('batch', id, async (result, scanned) => {
    updateBatch(result.replayed ? await getReceivingBatch(id) : result.summary); setLastCode(scanned); setMissing(null); setLegacyCandidates([]); setError(''); setNotice('');
  }, (kind, failure, command) => {
    setError(kind === 'uncertain' ? c('scanUncertain') : errorMessage(failure));
    const detail = detailOf(failure);
    setLegacyCandidates(detail?.code === 'mixed_receiving_legacy_pending' ? detail.candidates || [] : []);
    if (detail?.code === 'mixed_receiving_invoice_missing' && command) {
      setMissing(command); setManual(null);
      const choices = new Set(detail.candidates?.map(candidate => candidate.supplier));
      if (choices.size === 1) setUploadSupplier([...choices][0]);
    }
  }, { key: `receiving-batch-scans:${id}`, send: command => scanReceivingBatch(id, command) });

  useEffect(() => {
    let active = true;
    setBatch(null); setPreview(false); setError(''); setNotice(''); setMissing(null); setReassign(null); setLegacyCandidates([]);
    setPending(readPending(id)); setBusy('load'); lock.current = false;
    const request = id ? getReceivingBatch(id).then(value => { if (active) setBatch(value); })
      : listReceivingBatches().then(value => { if (active) setOpenBatches(value.batches); });
    request.catch(failure => { if (active) setError(errorMessage(failure)); })
      .finally(() => { if (active) setBusy(''); });
    return () => { active = false; };
  }, [id]);
  useEffect(() => {
    let active = true;
    getProductEditorOptions().then(value => { if (active) { setWarehouses(value.warehouses); setWarehouse(createRequest.current?.warehouse_code || defaultWarehouseCode(value.warehouses)); setWarehouseReady(true); } })
      .catch(() => { if (active) setError(c('warehousesFailed')); });
    return () => { active = false; };
  }, []);

  const ready = !!batch && batch.id === id;
  const completed = ready && batch.status === 'completed';
  const archived = ready && batch.archived === true;
  const closed = completed || archived;
  const blocked = !!busy || !!pending || scanner.pending > 0 || !!scanner.choice;
  const activeScans = batch?.scans.filter(scan => scan.status === 'active') || [];
  const lastScan = [...activeScans].reverse().find(scan => scan.code === lastCode) || activeScans[activeScans.length - 1];

  async function startBatch() {
    if (lock.current || busy || pending || !warehouseReady || !warehouse) return;
    lock.current = true; setBusy('create'); setError('');
    try {
      if (!createRequest.current) createRequest.current = { request_id: crypto.randomUUID(), warehouse_code: warehouse };
      const encoded = JSON.stringify(createRequest.current); sessionStorage.setItem('receiving-new-batch-v2', encoded);
      if (sessionStorage.getItem('receiving-new-batch-v2') !== encoded) throw new Error('storage_unavailable');
      const command = createRequest.current;
      const value = await createReceivingBatch(command.request_id, command.warehouse_code);
      sessionStorage.removeItem('receiving-new-batch-v2');
      createRequest.current = null; setQuery({ batch: value.id });
    } catch (failure) {
      if (knownRejection(failure)) { createRequest.current = null; try { sessionStorage.removeItem('receiving-new-batch-v2'); } catch { /* No server write occurred. */ } }
      setError(errorMessage(failure));
    }
    finally { lock.current = false; setBusy(''); }
  }
  function enqueue(value: string, requestId?: string) {
    if (!ready || closed || lock.current || pending || scanner.uncertain || scanner.choice || preview || manual || reassign || showUpload) return;
    const next = value.trim(); if (!next) return;
    codeRef.current = ''; setCode(''); setError(''); scanner.enqueue(next, 1, requestId);
    input.current?.focus();
  }
  async function refresh() {
    if (!id || lock.current || scanner.busy) return;
    lock.current = true; setBusy('refresh'); setError('');
    try { updateBatch(await getReceivingBatch(id)); }
    catch (failure) { setError(errorMessage(failure)); }
    finally { lock.current = false; setBusy(''); }
  }
  async function summarize() {
    if (!ready || blocked || lock.current || reassign || !activeScans.length) return;
    lock.current = true; setBusy('preview'); setError('');
    try {
      const result = await previewReceivingBatch(id);
      if (scope.current === id) { setBatch(result); setPreview(true); }
    } catch (failure) { setError(errorMessage(failure)); }
    finally { lock.current = false; setBusy(''); }
  }
  async function execute(command: PendingCommand) {
    if ((!id && command.kind !== 'archive') || lock.current || scanner.hasPending()) return;
    lock.current = true; setBusy(command.kind); setError('');
    if (!savePending(id, command)) { setError(c('recoveryStorageRequired')); lock.current = false; setBusy(''); return; }
    setPending(command);
    try {
      if (command.kind === 'archive') {
        await archiveReceivingBatch(command.batchId, command.body);
        if (scope.current !== id) return;
        savePending(id, null); setPending(null);
        if (id) setQuery({});
        else { setOpenBatches(value => value.filter(row => row.id !== command.batchId)); setNotice(c('removedBatch')); }
        return;
      }
      const result = command.kind === 'finalize' ? await finalizeReceivingBatch(id, command.body)
        : command.kind === 'manual' ? await manualReceivingBatchScan(id, command.body) : await mutateReceivingBatchScan(id, command.body);
      if (scope.current !== id) return;
      updateBatch(result.summary); savePending(id, null); setPending(null); setReassign(null); focusNeeded.current = true;
      if (command.kind === 'manual') { setManual(null); setMissing(null); setLastCode(command.body.code); }
      setNotice(command.kind === 'finalize' ? c('posted') : c('changed'));
    } catch (failure) {
      if (scope.current !== id) return;
      if (knownRejection(failure)) {
        savePending(id, null); setPending(null); setPreview(false);
        setError(errorMessage(failure));
      } else setError(c('operationUncertain'));
    } finally { lock.current = false; if (scope.current === id) setBusy(''); }
  }
  function mutate(scan: ReceivingBatchScan, action: 'cancel' | 'reassign', selection?: ScanInvoiceChoice) {
    if (blocked || lock.current) return;
    void execute({ kind: 'scan', body: { request_id: crypto.randomUUID(), action, scan_id: scan.id, selection } });
  }
  async function prepare(item: ReceivingBatchItem) {
    if (blocked || lock.current || !item.supplier || !item.session_id || !item.line_id) return;
    lock.current = true; setBusy('prepare'); setError('');
    try {
      const result = await prepareReceivingProducts(item.supplier, String(item.session_id), [item.line_id]);
      if (scope.current === id) navigate(`/product-import?${new URLSearchParams({ draft: result.draft_id, receiving_batch: id })}`);
    } catch (failure) { setError(errorMessage(failure)); }
    finally { lock.current = false; setBusy(''); }
  }
  async function openManual() {
    if (!missing || !id || blocked || lock.current) return;
    lock.current = true; setBusy('manual-product'); setError('');
    try {
      const product = await getReceivingManualProduct(id, missing.code);
      if (scope.current === id) { setManual(product); setManualValues({ qty: String(missing.qty), reason: '', cost_status: 'unknown', unit_cost: '' }); }
    } catch (failure) { setError(errorMessage(failure)); }
    finally { lock.current = false; setBusy(''); }
  }
  async function showHistory() {
    if (busy || lock.current) return;
    if (history) { setHistory(null); return; }
    setBusy('history');
    try { setHistory((await listReceivingBatches('completed')).batches); }
    catch (failure) { setError(errorMessage(failure)); }
    finally { setBusy(''); }
  }
  const removeBatch = (batchId: string) => {
    if (blocked || lock.current) return;
    void execute({ kind: 'archive', batchId, body: { request_id: crypto.randomUUID(), reason: c('emptyArchiveReason'), empty_only: true } });
  };
  useEffect(() => {
    if (!ready || closed || busy || pending || showUpload || scanner.choice || reassign || manual || preview || !focusNeeded.current) return;
    input.current?.focus(); focusNeeded.current = false;
  }, [ready, closed, busy, pending, showUpload, scanner.choice, reassign, manual, preview]);

  function alternatives(scan: ReceivingBatchScan) {
    return scan.alternatives.filter(candidate => !candidate.blocked).map(candidate => ({ ...candidate,
      lines: candidate.lines.filter(line => Number(line.remaining_qty) > 0 && !(candidate.supplier === scan.supplier
        && candidate.invoice_no === scan.invoice_no && (line.id === scan.line_id || line.line_number === scan.line_number))),
    })).filter(candidate => candidate.lines.length > 0);
  }
  function invoiceChoices(candidates: ReceivingBatchCandidate[], choose: (selection: ScanInvoiceChoice) => void) {
    return <div className="receiving-invoice-choices">{candidates.filter(candidate => !candidate.blocked).flatMap(candidate => candidate.lines.map((line, index) =>
      <Button type="button" variant="secondary" key={`${candidate.supplier}:${candidate.invoice_no}:${line.id ?? index}`}
        data-testid="mixed-invoice-choice" disabled={!!busy}
        onClick={() => choose({ supplier: candidate.supplier, invoice_no: candidate.invoice_no, ...(line.id ? { line_id: line.id } : {}) })}>
        <span><strong>{candidate.supplier_name} · {candidate.invoice_no}</strong><br />
          {date(candidate.invoice_date)} · {line.title}<br />
          {c('remaining', { count: number(line.remaining_qty) })} · {number(line.unit_price)} EUR
        </span>
      </Button>))}</div>;
  }
  function itemCards(items: ReceivingBatchItem[]) {
    return <div className="receiving-item-list">{items.map(item => <article className="receiving-item" key={item.source_kind === 'manual' ? `manual:${item.scan_id}` : `${item.session_id}:${item.line_id}`}>
      <div className="receiving-item-heading"><div><strong>{item.title}</strong><p className="receiving-muted">
        {item.sku && <StockCardLink productId={item.product_id} sku={item.sku} warehouseCode={batch?.warehouse_code}>{item.sku}</StockCardLink>}
        {' · '}{item.source_kind === 'manual' ? c('withoutInvoice') : `${item.invoice_no} · ${item.supplier}`}
        {item.source_kind === 'manual' && <span> · {c(`costStates.${item.cost_status || 'unknown'}`)} · {item.manual_reason}</span>}
      </p></div><strong>+{number(item.stock_quantity ?? item.qty)} {item.stock_unit || c('pieces')}</strong></div>
      {item.purchase_to_stock_factor && item.purchase_to_stock_factor !== '1' && <p className="receiving-muted">{c('purchaseQuantity', { qty: number(item.qty), unit: c(`units.${item.purchase_unit || 'pcs'}`), stock: number(item.stock_quantity), stockUnit: c(`units.${item.stock_unit || 'pcs'}`) })}</p>}
      <p>{c('stockChange', { before: number(item.stock_qty_before), after: number(item.stock_qty_after) })}</p>
      {!item.product_id && <p className="receiving-warning">{t(`receiving.products.statuses.${item.card_status}`, { defaultValue: c('missingCard') })}</p>}
      {item.warnings.length > 0 && <p className="receiving-warning">{item.warnings.map(value => c(`errors.${value}`, { defaultValue: t(`receiving.products.statuses.${value}`, { defaultValue: c('warningReview') }) })).join(' · ')}</p>}
      {!closed && (item.can_prepare || item.draft_id) && <Button type="button" variant="secondary" disabled={blocked} data-testid="mixed-prepare-card" onClick={() => void prepare(item)}>{c(item.draft_id ? 'continueCard' : 'prepareCard')}</Button>}
      <div className="receiving-shop-list">{item.shops.map(shop => <span key={shop.code}>
        <strong>{shop.name || (shop.code === 'biketrek' ? 'BIKETREK' : shop.code === 'xtrek' ? 'xTrek' : shop.code)}</strong>{': '}
        {c(`shopStatus.${!shop.linked ? 'unlinked' : completed && shop.status === 'ready' ? 'linked' : !completed && shop.status === 'verified' ? 'ready' : shop.status}`, { defaultValue: c(shop.sync_enabled ? 'shopStatus.pending' : 'shopStatus.disabled') })}
      </span>)}</div>
    </article>)}</div>;
  }

  return <main className="mixed-receiving">
    <header className="receiving-heading"><div><h1>{c('title')}</h1><p className="receiving-muted">{c('subtitle')}</p></div>
      <div className="receiving-header-actions">
        <Button type="button" variant="secondary" data-testid="mixed-upload-open" disabled={blocked} onClick={() => setShowUpload(true)}><Upload size={16} />{c('upload')}</Button>
        <Button type="button" variant="ghost" data-testid="mixed-history" disabled={!!busy} onClick={() => void showHistory()}><FileText size={17} />{c('history')}</Button>
      </div>
    </header>
    <InvoiceUploadModal open={showUpload} initialSupplier={uploadSupplier} context={missing ? c('missingInvoice', { code: missing.code }) : undefined}
      onClose={() => { setShowUpload(false); focusNeeded.current = true; }} onUploaded={async result => {
        if (result.processing?.receiving_ready && missing && id) scanner.enqueue(missing.code, missing.qty, missing.request_id);
      }} />
    {history && <section className="receiving-panel" data-testid="mixed-history-list"><h2>{c('history')}</h2>
      {!history.length && <p>{c('historyEmpty')}</p>}{history.map(value => <Link key={value.id} className="receiving-open-batch" to={`/receiving?batch=${encodeURIComponent(value.id)}`}><span>{date(value.created_at)} · {value.warehouse_code}</span><strong>{number(value.total_qty)} {c('pieces')}</strong></Link>)}
    </section>}
    {pending && <div className="receiving-banner is-warning"><p>{c('pendingOperation')}</p><Button type="button" data-testid="mixed-retry-operation" disabled={!!busy} onClick={() => void execute(pending)}>{c('retryOperation')}</Button></div>}
    {error && <div role="alert" className="receiving-banner is-error">{error}</div>}
    {legacyCandidates.length > 0 && <div className="receiving-panel">{legacyCandidates.filter(candidate => candidate.session_id).map(candidate =>
      <p key={`${candidate.supplier}:${candidate.invoice_no}`}><Link className="receiving-text-link" to={`/receiving/${encodeURIComponent(candidate.invoice_no)}?${new URLSearchParams({ supplier: candidate.supplier, session: String(candidate.session_id) })}`}>{c('openOriginal', { invoice: candidate.invoice_no })}</Link></p>)}</div>}
    {notice && <div role="status" className="receiving-banner is-success">{notice}</div>}
    {!id && <section className="receiving-panel">
      <label>{c('warehouse')}<select data-testid="mixed-warehouse" value={warehouse} disabled={!!busy || !!pending || !!createRequest.current} onChange={event => setWarehouse(event.target.value)}>
        {!warehouse && <option value="">{c('chooseWarehouse')}</option>}{warehouses.map(row => <option key={row.code} value={row.code}>{row.name}</option>)}
      </select></label>
      <Button type="button" data-testid="mixed-start" disabled={!!busy || !!pending || !warehouseReady || !warehouse} onClick={() => void startBatch()}><Plus size={18} />{c('start')}</Button>
      {openBatches.length > 0 && <div className="receiving-open-list"><h2>{c('openBatches')}</h2>{openBatches.map(value =>
        <div key={value.id} className="receiving-open-row"><Link to={`/receiving?batch=${encodeURIComponent(value.id)}`} className="receiving-open-batch">
          <span>{date(value.created_at)} · {value.warehouse_code}</span><strong>{number(value.total_qty)} {c('pieces')}</strong>
        </Link>{Number(value.total_qty) === 0 && <Button type="button" variant="ghost" data-testid="mixed-remove-batch" disabled={blocked} onClick={() => removeBatch(value.id)}>{c('removeEmptyBatch')}</Button>}</div>)}</div>}
    </section>}
    {busy === 'load' && <p role="status">{t('common.loading')}</p>}
    {id && !ready && !busy && <Button type="button" onClick={() => void refresh()}>{c('reload')}</Button>}
    {ready && <>
      <div className="receiving-batch-header"><div><strong>{batch.warehouse_name}</strong><span className="receiving-muted">{date(batch.created_at)}</span></div>
        <div>{!closed && !activeScans.length && <Button type="button" variant="ghost" data-testid="mixed-remove-current" disabled={blocked} onClick={() => removeBatch(id)}>{c('removeEmptyBatch')}</Button>}
        <Button type="button" variant="secondary" disabled={!!busy || scanner.busy} onClick={() => void refresh()}><RefreshCw size={16} />{c('refresh')}</Button></div>
      </div>
      {scanner.pending > 0 && <p role="status" data-testid="mixed-pending-scans">{c('pendingScans', { count: scanner.pending })}</p>}
      {scanner.uncertain && <Button type="button" data-testid="mixed-retry-scan" disabled={!!busy || scanner.busy} onClick={() => scanner.retry()}>{c('retryScan')}</Button>}
      {scanner.choice && <section className="receiving-panel" role="dialog" aria-label={c('chooseInvoice')} data-testid="mixed-scan-choice">
        <h2>{c('chooseInvoice')}</h2><p>{c('chooseHelp', { code: scanner.choice.code })}</p>
        {invoiceChoices(scanner.choice.candidates as unknown as ReceivingBatchCandidate[], selection => { focusNeeded.current = true; scanner.resolveChoice(selection); })}
        <Button type="button" variant="ghost" onClick={() => { focusNeeded.current = true; scanner.resolveChoice(null); }}>{t('common.cancel')}</Button>
      </section>}
      {!closed && !preview && <section className="receiving-panel">
        <BarcodeCamera disabled={!ready || closed || !!pending || !!scanner.choice || scanner.uncertain || !!busy || !!manual || !!reassign || showUpload} onScan={enqueue} />
        <form className="receiving-code-form" onSubmit={event => { event.preventDefault(); enqueue(codeRef.current); }}>
          <label htmlFor="mixed-receiving-code">{c('code')}</label><div>
            <input type="text" ref={input} id="mixed-receiving-code" data-testid="mixed-code" value={code} maxLength={100} autoComplete="off" autoCapitalize="none"
              disabled={!!busy || !!pending || scanner.uncertain || !!scanner.choice || !!manual || !!reassign || showUpload}
              onChange={event => { codeRef.current = event.target.value; setCode(event.target.value); }} placeholder={c('codePlaceholder')} />
            <Button type="submit" data-testid="mixed-scan" disabled={!code.trim() || !!busy || !!pending || scanner.uncertain || !!scanner.choice || !!manual || !!reassign || showUpload}>{c('add')}</Button>
          </div>
        </form>
        {lastScan && <div className="receiving-last-scan" role="status" data-testid="mixed-last-scan"><CheckCircle size={20} /><div>
          <strong>{lastScan.title}</strong><p>{lastScan.source_kind === 'manual' ? c('withoutInvoice') : `${lastScan.supplier_name} · ${lastScan.invoice_no}`}</p>
          {alternatives(lastScan).length > 0 && <button type="button" className="receiving-text-link" disabled={blocked} onClick={() => setReassign(lastScan)}>{c('otherInvoices', { count: alternatives(lastScan).length })}</button>}
        </div></div>}
        <p className="receiving-muted">{c('stagingHelp')}</p>
      </section>}
      {!closed && !preview && missing && <section className="receiving-panel" data-testid="mixed-missing-invoice"><h2>{c('missingInvoice', { code: missing.code })}</h2>
        <div className="receiving-header-actions"><Button type="button" disabled={blocked} onClick={() => setShowUpload(true)}>{c('upload')}</Button>
          <Button type="button" variant="secondary" data-testid="mixed-manual-open" disabled={blocked} onClick={() => void openManual()}>{c('receiveWithoutInvoice')}</Button></div>
        <p className="receiving-muted">{c('manualHelp')}</p>
      </section>}
      {!closed && !preview && manual && missing && <section className="receiving-panel" data-testid="mixed-manual-form"><h2>{c('receiveWithoutInvoice')}</h2>
        <p><strong>{manual.title}</strong> · {manual.sku}</p><p className="receiving-warning">{c('manualWarning')}</p>
        <p className="receiving-muted">{c('manualCostHelp')}</p>
        <form onSubmit={event => { event.preventDefault(); if (blocked || manualValues.reason.trim().length < 3 || !Number.isFinite(Number(manualValues.qty)) || Number(manualValues.qty) <= 0) return;
          void execute({ kind: 'manual', body: { request_id: crypto.randomUUID(), code: missing.code, qty: manualValues.qty, confirmed_without_invoice: true,
            reason: manualValues.reason.trim(), cost_status: manualValues.cost_status, unit_cost: manualValues.cost_status === 'unknown' ? null : manualValues.unit_cost } }); }}>
          <div className="receiving-manual-fields"><label>{c('manualQuantity', { unit: c(`units.${manual.stock_unit}`, { defaultValue: manual.stock_unit }) })}<input type="number" step="any" min="0.001" required data-testid="mixed-manual-qty" value={manualValues.qty} onChange={event => setManualValues({ ...manualValues, qty: event.target.value })} disabled={blocked} /></label>
          <label>{c('manualReason')}<input type="text" required minLength={3} maxLength={255} data-testid="mixed-manual-reason" value={manualValues.reason} onChange={event => setManualValues({ ...manualValues, reason: event.target.value })} disabled={blocked} /></label>
          <label>{c('manualCostState')}<select data-testid="mixed-manual-cost-status" value={manualValues.cost_status} onChange={event => setManualValues({ ...manualValues, cost_status: event.target.value as typeof manualValues.cost_status })} disabled={blocked}>{(['unknown', 'known', 'provisional'] as const).map(value => <option key={value} value={value}>{c(`costStates.${value}`)}</option>)}</select></label>
          {manualValues.cost_status !== 'unknown' && <label>{c('manualCost')}<input type="number" min="0" step="any" required data-testid="mixed-manual-cost" value={manualValues.unit_cost} onChange={event => setManualValues({ ...manualValues, unit_cost: event.target.value })} disabled={blocked} /></label>}</div>
          <div className="receiving-header-actions"><Button type="submit" data-testid="mixed-manual-confirm" disabled={blocked || manualValues.reason.trim().length < 3}>{c('confirmManual')}</Button><Button type="button" variant="ghost" disabled={blocked} onClick={() => { setManual(null); focusNeeded.current = true; }}>{t('common.cancel')}</Button></div>
        </form>
      </section>}
      {reassign && <section role="dialog" aria-label={c('changeInvoice')} className="receiving-panel" data-testid="mixed-reassign">
        <h2>{c('changeInvoice')}</h2><p>{reassign.title} · {reassign.invoice_no}</p>
        {invoiceChoices(alternatives(reassign), selection => mutate(reassign, 'reassign', selection))}
        <Button type="button" variant="ghost" disabled={!!busy} onClick={() => { setReassign(null); focusNeeded.current = true; }}>{t('common.cancel')}</Button>
      </section>}
      {!closed && !preview && activeScans.length > 0 && <section className="receiving-panel">
        <h2>{c('scanned')}</h2><ol className="receiving-scan-list">{[...activeScans].reverse().map(scan => <li key={scan.id}>
          <div><strong>{scan.title}</strong><p className="receiving-muted">{scan.code} · {scan.source_kind === 'manual' ? c('withoutInvoice') : `${scan.invoice_no} · ${scan.supplier_name}`}</p>
            {alternatives(scan).length > 0 && <button type="button" disabled={blocked} className="receiving-text-link" onClick={() => setReassign(scan)}>{c('changeInvoice')}</button>}
          </div><strong>{number(scan.qty)}</strong><button type="button" data-testid="mixed-cancel-scan" disabled={blocked} aria-label={c('removeScan', { name: scan.title })} onClick={() => mutate(scan, 'cancel')}><X size={18} /></button>
        </li>)}</ol>
      </section>}
      {(preview || closed) && <section className="receiving-panel" data-testid="mixed-summary">
        <h2>{c(archived ? 'archivedReceipt' : completed ? 'completed' : 'summary')}</h2><p>{c(archived ? 'archivedHelp' : completed ? 'completedHelp' : 'summaryHelp')}</p>
        {itemCards(batch.items)}
        <h3>{c('invoiceProgress')}</h3><ul className="receiving-invoice-progress">{batch.invoices.map(invoice => <li key={`${invoice.supplier}:${invoice.session_id}`}>
          <Link to={`/receiving/${encodeURIComponent(invoice.invoice_no)}?${new URLSearchParams({ supplier: invoice.supplier, session: String(invoice.session_id) })}`}>{invoice.invoice_no}</Link>
          <span>{number(invoice.received_qty)}{!closed && ` + ${number(invoice.scanned_qty)}`} / {number(invoice.ordered_qty)}</span>
          {Number(invoice.unposted_legacy_qty || 0) > 0 && <span className="receiving-warning">{c('legacyPending', { count: number(invoice.unposted_legacy_qty) })}</span>}
          <strong>{c(invoice.complete_after ? completed ? 'invoiceClosed' : 'willClose' : 'remainsOpen')}</strong>
        </li>)}</ul>
        {batch.blockers.length > 0 && <div role="alert" className="receiving-banner is-warning"><h3>{c('needsReview')}</h3><ul>{batch.blockers.map((blocker, index) => <li key={index}>{c(`errors.${blocker.code}`, { defaultValue: c('blocked') })}{blocker.line_id ? ` · ${c('line')} ${blocker.line_id}` : ''}</li>)}</ul></div>}
        {closed && <div className="receiving-result-actions">{completed && !archived && <Link className="receiving-text-link" data-testid="mixed-atomer" to={`/sync/atomer?receiving_batch=${encodeURIComponent(id)}`}>{c('atomerExport')}</Link>}
          <Button type="button" onClick={() => setQuery({})}>{c('newReceipt')}</Button></div>}
      </section>}
      {!closed && <footer className="receiving-bottom-bar"><div><strong>{number(batch.total_qty)} {c('pieces')}</strong><small>{c('invoiceCount', { count: batch.invoices.length })}</small></div>
        {preview ? <div className="receiving-bottom-actions"><Button type="button" variant="secondary" disabled={blocked} onClick={() => { setPreview(false); focusNeeded.current = true; }}>{c('continueScanning')}</Button>
          <Button type="button" data-testid="mixed-confirm" disabled={blocked || !activeScans.length || batch.blockers.length > 0 || !batch.preview_hash}
            onClick={() => void execute({ kind: 'finalize', body: { request_id: crypto.randomUUID(), preview_hash: batch.preview_hash!, confirmed: true } })}>{c('confirm')}</Button></div>
          : <Button type="button" data-testid="mixed-preview" disabled={blocked || !!reassign || !activeScans.length} onClick={() => void summarize()}>{c('review')}</Button>}
      </footer>}
    </>}
  </main>;
}
