import React, { useEffect, useRef, useState } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { CheckCircle, FileText, Plus, RefreshCw, Upload, X } from 'lucide-react';
import { Button } from '../components/ui/Button.new';
import { BarcodeCamera } from '../components/BarcodeCamera';
import { StockCardLink } from '../components/product/StockCardLink';
import { ApiResponseError } from '../api/client';
import { listSuppliers, uploadInvoice, type SupplierSummary } from '../api/suppliers';
import { prepareReceivingProducts } from '../api/receiving';
import { useReceivingScanner, type ReceivingScanCommand, type ScanInvoiceChoice } from '../hooks/useReceivingScanner';
import {
  createReceivingBatch, finalizeReceivingBatch, getReceivingBatch, listReceivingBatches,
  mutateReceivingBatchScan, previewReceivingBatch, scanReceivingBatch,
  type ReceivingBatchCandidate, type ReceivingBatchFinalizeCommand, type ReceivingBatchItem,
  type ReceivingBatchListItem, type ReceivingBatchMutation, type ReceivingBatchScan,
  type ReceivingBatchScanResult, type ReceivingBatchSummary,
} from '../api/receivingBatches';
import { formatNumber } from '../utils/format';
import { receivingSourceError } from '../features/receiving/sourceErrors';
import './MixedReceivingPage.css';

type PendingCommand = { kind: 'finalize'; body: ReceivingBatchFinalizeCommand }
  | { kind: 'scan'; body: ReceivingBatchMutation };
const storageKey = (id: string) => `receiving-batch-command:${id}`;
const readPending = (id: string): PendingCommand | null => {
  try {
    const value = JSON.parse(sessionStorage.getItem(storageKey(id)) || 'null');
    return value && ['finalize', 'scan'].includes(value.kind) && typeof value.body?.request_id === 'string' ? value : null;
  } catch { return null; }
};
const savePending = (id: string, command: PendingCommand | null) => {
  try {
    if (command) sessionStorage.setItem(storageKey(id), JSON.stringify(command));
    else sessionStorage.removeItem(storageKey(id));
  } catch { /* In-memory recovery remains available. */ }
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
  const createRequestId = useRef<string | null>(null);
  const [batch, setBatch] = useState<ReceivingBatchSummary | null>(null);
  const [openBatches, setOpenBatches] = useState<ReceivingBatchListItem[]>([]);
  const [suppliers, setSuppliers] = useState<SupplierSummary[]>([]);
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
  const fileInput = useRef<HTMLInputElement>(null);
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
      setMissing(command); setShowUpload(true);
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
  useEffect(() => { let active = true; listSuppliers().then(value => { if (active) setSuppliers(value); }).catch(() => {}); return () => { active = false; }; }, []);

  const ready = !!batch && batch.id === id;
  const completed = ready && batch.status === 'completed';
  const blocked = !!busy || !!pending || scanner.pending > 0 || !!scanner.choice;
  const activeScans = batch?.scans.filter(scan => scan.status === 'active') || [];
  const lastScan = [...activeScans].reverse().find(scan => scan.code === lastCode) || activeScans[activeScans.length - 1];

  async function startBatch() {
    if (lock.current || busy) return;
    lock.current = true; setBusy('create'); setError('');
    let requestId: string;
    try { requestId = createRequestId.current || sessionStorage.getItem('receiving-new-batch') || crypto.randomUUID(); createRequestId.current = requestId; sessionStorage.setItem('receiving-new-batch', requestId); }
    catch { requestId = createRequestId.current || crypto.randomUUID(); createRequestId.current = requestId; }
    try {
      const value = await createReceivingBatch(requestId);
      try { sessionStorage.removeItem('receiving-new-batch'); } catch { /* Optional storage. */ }
      createRequestId.current = null; setQuery({ batch: value.id });
    } catch (failure) { setError(errorMessage(failure)); }
    finally { lock.current = false; setBusy(''); }
  }
  function enqueue(value: string, requestId?: string) {
    if (!ready || completed || lock.current || pending || scanner.uncertain || scanner.choice || preview) return;
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
    if (!ready || blocked || lock.current || !activeScans.length) return;
    lock.current = true; setBusy('preview'); setError('');
    try {
      const result = await previewReceivingBatch(id);
      if (scope.current === id) { setBatch(result); setPreview(true); }
    } catch (failure) { setError(errorMessage(failure)); }
    finally { lock.current = false; setBusy(''); }
  }
  async function execute(command: PendingCommand) {
    if (!id || lock.current || scanner.hasPending()) return;
    lock.current = true; setBusy(command.kind); setError('');
    savePending(id, command); setPending(command);
    try {
      const result = command.kind === 'finalize'
        ? await finalizeReceivingBatch(id, command.body)
        : await mutateReceivingBatchScan(id, command.body);
      if (scope.current !== id) return;
      updateBatch(result.summary); savePending(id, null); setPending(null); setReassign(null);
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
    if (blocked || lock.current) return;
    lock.current = true; setBusy('prepare'); setError('');
    try {
      const result = await prepareReceivingProducts(item.supplier, String(item.session_id), [item.line_id]);
      if (scope.current === id) navigate(`/product-import?${new URLSearchParams({ draft: result.draft_id, receiving_batch: id })}`);
    } catch (failure) { setError(errorMessage(failure)); }
    finally { lock.current = false; setBusy(''); }
  }
  async function upload(event: React.ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (!file || !uploadSupplier || lock.current) return;
    lock.current = true; setBusy('upload'); setError('');
    try {
      await uploadInvoice(uploadSupplier, file);
      setNotice(c('uploaded', { name: file.name })); setShowUpload(false);
      // This scan was explicitly rejected; preserve its UUID when retrying after upload.
      if (missing && id) scanner.enqueue(missing.code, missing.qty, missing.request_id);
    } catch (failure) { setError(errorMessage(failure)); }
    finally { lock.current = false; setBusy(''); if (fileInput.current) fileInput.current.value = ''; }
  }

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
    return <div className="receiving-item-list">{items.map(item => <article className="receiving-item" key={`${item.session_id}:${item.line_id}`}>
      <div className="receiving-item-heading"><div><strong>{item.title}</strong><p className="receiving-muted">
        {item.sku && <StockCardLink productId={item.product_id} sku={item.sku} warehouseCode={batch?.warehouse_code}>{item.sku}</StockCardLink>}
        {' · '}{item.invoice_no} · {item.supplier}
      </p></div><strong>+{number(item.stock_quantity ?? item.qty)} {item.stock_unit || c('pieces')}</strong></div>
      {item.purchase_to_stock_factor && item.purchase_to_stock_factor !== '1' && <p className="receiving-muted">{c('purchaseQuantity', { qty: number(item.qty), unit: c(`units.${item.purchase_unit || 'pcs'}`), stock: number(item.stock_quantity), stockUnit: c(`units.${item.stock_unit || 'pcs'}`) })}</p>}
      <p>{c('stockChange', { before: number(item.stock_qty_before), after: number(item.stock_qty_after) })}</p>
      {!item.product_id && <p className="receiving-warning">{t(`receiving.products.statuses.${item.card_status}`, { defaultValue: c('missingCard') })}</p>}
      {item.warnings.length > 0 && <p className="receiving-warning">{item.warnings.map(value => c(`errors.${value}`, { defaultValue: t(`receiving.products.statuses.${value}`, { defaultValue: c('warningReview') }) })).join(' · ')}</p>}
      {!completed && (item.can_prepare || item.draft_id) && <Button type="button" variant="secondary" disabled={blocked} data-testid="mixed-prepare-card" onClick={() => void prepare(item)}>{c(item.draft_id ? 'continueCard' : 'prepareCard')}</Button>}
      <div className="receiving-shop-list">{item.shops.map(shop => <span key={shop.code}>
        <strong>{shop.name || (shop.code === 'biketrek' ? 'BIKETREK' : shop.code === 'xtrek' ? 'xTrek' : shop.code)}</strong>{': '}
        {c(`shopStatus.${!shop.linked ? 'unlinked' : completed && shop.status === 'ready' ? 'linked' : !completed && shop.status === 'verified' ? 'ready' : shop.status}`, { defaultValue: c(shop.sync_enabled ? 'shopStatus.pending' : 'shopStatus.disabled') })}
      </span>)}</div>
    </article>)}</div>;
  }

  return <main className="mixed-receiving">
    <header className="receiving-heading"><div><h1>{c('title')}</h1><p className="receiving-muted">{c('subtitle')}</p></div>
      <Link to="/receiving/invoices" className="receiving-text-link"><FileText size={17} />{c('invoices')}</Link>
    </header>
    {error && <div role="alert" className="receiving-banner is-error">{error}</div>}
    {legacyCandidates.length > 0 && <div className="receiving-panel">{legacyCandidates.filter(candidate => candidate.session_id).map(candidate =>
      <p key={`${candidate.supplier}:${candidate.invoice_no}`}><Link className="receiving-text-link" to={`/receiving/${encodeURIComponent(candidate.invoice_no)}?${new URLSearchParams({ supplier: candidate.supplier, session: String(candidate.session_id) })}`}>{c('openOriginal', { invoice: candidate.invoice_no })}</Link></p>)}</div>}
    {notice && <div role="status" className="receiving-banner is-success">{notice}</div>}
    {!id && <section className="receiving-panel">
      <Button type="button" data-testid="mixed-start" disabled={!!busy} onClick={() => void startBatch()}><Plus size={18} />{c('start')}</Button>
      {openBatches.length > 0 && <div className="receiving-open-list"><h2>{c('openBatches')}</h2>{openBatches.map(value =>
        <Link key={value.id} to={`/receiving?batch=${encodeURIComponent(value.id)}`} className="receiving-open-batch">
          <span>{date(value.created_at)} · {value.warehouse_code}</span><strong>{number(value.total_qty)} {c('pieces')}</strong>
        </Link>)}</div>}
    </section>}
    {busy === 'load' && <p role="status">{t('common.loading')}</p>}
    {id && !ready && !busy && <Button type="button" onClick={() => void refresh()}>{c('reload')}</Button>}
    {ready && <>
      <div className="receiving-batch-header"><div><strong>{batch.warehouse_name}</strong><span className="receiving-muted">{date(batch.created_at)}</span></div>
        <Button type="button" variant="secondary" disabled={!!busy || scanner.busy} onClick={() => void refresh()}><RefreshCw size={16} />{c('refresh')}</Button>
      </div>
      {pending && <div className="receiving-banner is-warning"><p>{c('pendingOperation')}</p><Button type="button" data-testid="mixed-retry-operation" disabled={!!busy} onClick={() => void execute(pending)}>{c('retryOperation')}</Button></div>}
      {scanner.pending > 0 && <p role="status" data-testid="mixed-pending-scans">{c('pendingScans', { count: scanner.pending })}</p>}
      {scanner.uncertain && <Button type="button" data-testid="mixed-retry-scan" disabled={!!busy || scanner.busy} onClick={() => scanner.retry()}>{c('retryScan')}</Button>}
      {scanner.choice && <section className="receiving-panel" role="dialog" aria-label={c('chooseInvoice')} data-testid="mixed-scan-choice">
        <h2>{c('chooseInvoice')}</h2><p>{c('chooseHelp', { code: scanner.choice.code })}</p>
        {invoiceChoices(scanner.choice.candidates as unknown as ReceivingBatchCandidate[], selection => scanner.resolveChoice(selection))}
        <Button type="button" variant="ghost" onClick={() => scanner.resolveChoice(null)}>{t('common.cancel')}</Button>
      </section>}
      {!completed && !preview && <section className="receiving-panel">
        <BarcodeCamera disabled={!ready || completed || !!pending || !!scanner.choice || scanner.uncertain || !!busy} onScan={enqueue} />
        <form className="receiving-code-form" onSubmit={event => { event.preventDefault(); enqueue(codeRef.current); }}>
          <label htmlFor="mixed-receiving-code">{c('code')}</label><div>
            <input ref={input} id="mixed-receiving-code" data-testid="mixed-code" value={code} maxLength={100} autoComplete="off" autoCapitalize="none"
              disabled={!!busy || !!pending || scanner.uncertain || !!scanner.choice}
              onChange={event => { codeRef.current = event.target.value; setCode(event.target.value); }} placeholder={c('codePlaceholder')} />
            <Button type="submit" data-testid="mixed-scan" disabled={!code.trim() || !!busy || !!pending || scanner.uncertain || !!scanner.choice}>{c('add')}</Button>
          </div>
        </form>
        {lastScan && <div className="receiving-last-scan" role="status" data-testid="mixed-last-scan"><CheckCircle size={20} /><div>
          <strong>{lastScan.title}</strong><p>{lastScan.supplier_name} · {lastScan.invoice_no}</p>
          {alternatives(lastScan).length > 0 && <button type="button" className="receiving-text-link" disabled={blocked} onClick={() => setReassign(lastScan)}>{c('otherInvoices', { count: alternatives(lastScan).length })}</button>}
        </div></div>}
        <p className="receiving-muted">{c('stagingHelp')}</p>
      </section>}
      {!completed && <div className="receiving-upload-controls"><Button type="button" variant="secondary" disabled={blocked} onClick={() => setShowUpload(value => !value)}><Upload size={16} />{c('upload')}</Button></div>}
      {!completed && showUpload && <section className="receiving-panel" data-testid="mixed-upload">
        <h2>{c('upload')}</h2>{missing && <p>{c('missingInvoice', { code: missing.code })}</p>}
        <label>{c('supplier')}<select data-testid="mixed-upload-supplier" value={uploadSupplier} disabled={!!busy} onChange={event => setUploadSupplier(event.target.value)}>
          <option value="">{c('chooseSupplier')}</option>{suppliers.map(supplier => <option key={supplier.code} value={supplier.code}>{supplier.name}</option>)}
        </select></label>
        <input type="file" ref={fileInput} data-testid="mixed-upload-file" disabled={!uploadSupplier || !!busy || !!pending} accept={uploadSupplier === 'northfinder' ? '.xlsx,.xls,.pdf' : '.pdf'} onChange={event => void upload(event)} />
      </section>}
      {reassign && <section role="dialog" aria-label={c('changeInvoice')} className="receiving-panel" data-testid="mixed-reassign">
        <h2>{c('changeInvoice')}</h2><p>{reassign.title} · {reassign.invoice_no}</p>
        {invoiceChoices(alternatives(reassign), selection => mutate(reassign, 'reassign', selection))}
        <Button type="button" variant="ghost" disabled={!!busy} onClick={() => setReassign(null)}>{t('common.cancel')}</Button>
      </section>}
      {!completed && !preview && activeScans.length > 0 && <section className="receiving-panel">
        <h2>{c('scanned')}</h2><ol className="receiving-scan-list">{[...activeScans].reverse().map(scan => <li key={scan.id}>
          <div><strong>{scan.title}</strong><p className="receiving-muted">{scan.code} · {scan.invoice_no} · {scan.supplier_name}</p>
            {alternatives(scan).length > 0 && <button type="button" disabled={blocked} className="receiving-text-link" onClick={() => setReassign(scan)}>{c('changeInvoice')}</button>}
          </div><strong>{number(scan.qty)}</strong><button type="button" data-testid="mixed-cancel-scan" disabled={blocked} aria-label={c('removeScan', { name: scan.title })} onClick={() => mutate(scan, 'cancel')}><X size={18} /></button>
        </li>)}</ol>
      </section>}
      {(preview || completed) && <section className="receiving-panel" data-testid="mixed-summary">
        <h2>{c(completed ? 'completed' : 'summary')}</h2><p>{c(completed ? 'completedHelp' : 'summaryHelp')}</p>
        {itemCards(batch.items)}
        <h3>{c('invoiceProgress')}</h3><ul className="receiving-invoice-progress">{batch.invoices.map(invoice => <li key={`${invoice.supplier}:${invoice.session_id}`}>
          <Link to={`/receiving/${encodeURIComponent(invoice.invoice_no)}?${new URLSearchParams({ supplier: invoice.supplier, session: String(invoice.session_id) })}`}>{invoice.invoice_no}</Link>
          <span>{number(invoice.received_qty)}{!completed && ` + ${number(invoice.scanned_qty)}`} / {number(invoice.ordered_qty)}</span>
          {Number(invoice.unposted_legacy_qty || 0) > 0 && <span className="receiving-warning">{c('legacyPending', { count: number(invoice.unposted_legacy_qty) })}</span>}
          <strong>{c(invoice.complete_after ? completed ? 'invoiceClosed' : 'willClose' : 'remainsOpen')}</strong>
        </li>)}</ul>
        {batch.blockers.length > 0 && <div role="alert" className="receiving-banner is-warning"><h3>{c('needsReview')}</h3><ul>{batch.blockers.map((blocker, index) => <li key={index}>{c(`errors.${blocker.code}`, { defaultValue: c('blocked') })}{blocker.line_id ? ` · ${c('line')} ${blocker.line_id}` : ''}</li>)}</ul></div>}
        {completed && <div className="receiving-result-actions"><Link className="receiving-text-link" data-testid="mixed-atomer" to={`/sync/atomer?receiving_batch=${encodeURIComponent(id)}`}>{c('atomerExport')}</Link>
          <Button type="button" onClick={() => setQuery({})}>{c('newReceipt')}</Button></div>}
      </section>}
      {!completed && <footer className="receiving-bottom-bar"><div><strong>{number(batch.total_qty)} {c('pieces')}</strong><small>{c('invoiceCount', { count: batch.invoices.length })}</small></div>
        {preview ? <div className="receiving-bottom-actions"><Button type="button" variant="secondary" disabled={blocked} onClick={() => setPreview(false)}>{c('continueScanning')}</Button>
          <Button type="button" data-testid="mixed-confirm" disabled={blocked || !activeScans.length || batch.blockers.length > 0 || !batch.preview_hash}
            onClick={() => void execute({ kind: 'finalize', body: { request_id: crypto.randomUUID(), preview_hash: batch.preview_hash!, confirmed: true } })}>{c('confirm')}</Button></div>
          : <Button type="button" data-testid="mixed-preview" disabled={blocked || !activeScans.length} onClick={() => void summarize()}>{c('review')}</Button>}
      </footer>}
    </>}
  </main>;
}
