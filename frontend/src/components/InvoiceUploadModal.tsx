import React, { useEffect, useId, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Upload, X } from 'lucide-react';
import { getArchiveSuppliers, getInvoiceSupplierOptions, uploadArchivedInvoice, type InvoiceUploadResult } from '../api/invoiceArchive';
import { invoiceError } from '../pages/invoiceArchiveUi';
import '../pages/invoiceArchive.css';
import './InvoiceUploadModal.css';

const extensions = new Set(['pdf', 'csv', 'tsv', 'xlsx', 'xls', 'doc', 'docx', 'xml', 'txt', 'jpg', 'jpeg', 'png']);
const accept = [...extensions].map(value => `.${value}`).join(',');
type Props = { open: boolean; initialSupplier?: string; context?: string; onClose: () => void;
  onUploaded?: (result: InvoiceUploadResult) => void | Promise<void> };

/** All entry points retain their context while sharing upload and processing feedback. */
export function InvoiceUploadModal({ open, ...props }: Props) {
  return open ? <UploadDialog {...props} /> : null;
}
function UploadDialog({ initialSupplier = '', context, onClose, onUploaded }: Omit<Props, 'open'>) {
  const { t, i18n } = useTranslation();
  const titleId = useId();
  const dialog = useRef<HTMLDivElement>(null);
  const metadata = useRef<HTMLDetailsElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const close = useRef(onClose); close.current = onClose;
  const [suppliers, setSuppliers] = useState<{ code: string; name: string }[]>([]);
  const [upload, setUpload] = useState({ supplier_code: initialSupplier, invoice_number: '', invoice_date: '', due_date: '', vat_included: true });
  const [file, setFile] = useState<File | null>(null);
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState('');
  const [working, setWorking] = useState(false);
  const [refreshWarning, setRefreshWarning] = useState(false);
  const [result, setResult] = useState<InvoiceUploadResult | null>(null);
  const lock = useRef(false), mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    const previous = document.activeElement as HTMLElement | null;
    dialog.current?.querySelector<HTMLElement>('select')?.focus();
    return () => { mounted.current = false; if (previous?.isConnected) previous.focus(); };
  }, []);
  useEffect(() => {
    if (upload.supplier_code === 'northfinder' && /\.xlsx?$/i.test(file?.name || '') && metadata.current) metadata.current.open = true;
  }, [upload.supplier_code, file]);
  useEffect(() => {
    const controller = new AbortController();
    Promise.allSettled([getArchiveSuppliers(controller.signal), getInvoiceSupplierOptions(controller.signal)]).then(values => {
      if (controller.signal.aborted) return;
      const options = new Map<string, { code: string; name: string }>();
      values.forEach(value => { if (value.status === 'fulfilled') value.value.forEach(row => options.set(row.code, row)); });
      setSuppliers([...options.values()].sort((a, b) => a.name.localeCompare(b.name, i18n.language)));
      if (values.every(value => value.status === 'rejected')) setError(t('invoiceArchive.suppliersFailed'));
    });
    return () => controller.abort();
  }, [i18n.language, t]);
  const dismiss = () => { if (!lock.current) close.current(); };
  function keyboard(event: React.KeyboardEvent) {
    if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); dismiss(); }
    if (event.key !== 'Tab') return;
    const controls = Array.from(dialog.current?.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled):not([hidden]), select:not(:disabled), a[href], summary, [tabindex="0"]') || []).filter(control => {
      const collapsed = control.closest('details:not([open])');
      return !control.closest('[hidden]') && (!collapsed || control.tagName === 'SUMMARY' && control.parentElement === collapsed);
    });
    if (!controls.length) { event.preventDefault(); return; }
    const first = controls[0], last = controls[controls.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }
  function choose(files: FileList | File[] | null) {
    if (lock.current || !files?.length) return;
    const chosen = files[0];
    const failure = files.length !== 1 ? 'dropOneFile' : !extensions.has(chosen.name.split('.').pop()?.toLowerCase() || '')
      ? 'unsupportedFile' : !chosen.size || chosen.size > 50 * 1024 * 1024 ? 'fileSizeLimit' : '';
    setResult(null); setRefreshWarning(false); setError(failure ? t(`invoiceArchive.${failure}`) : ''); setFile(failure ? null : chosen);
    if (fileInput.current) fileInput.current.value = '';
  }
  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (lock.current || !file || !upload.supplier_code) return;
    lock.current = true; setWorking(true); setError(''); setResult(null); setRefreshWarning(false);
    const body = new FormData(); body.append('file', file);
    Object.entries(upload).forEach(([key, value]) => { if (value !== '') body.append(key, String(value)); });
    try {
      const value = await uploadArchivedInvoice(body);
      if (!mounted.current) return;
      setResult(value); setFile(null);
      try { await onUploaded?.(value); } catch { if (mounted.current) setRefreshWarning(true); }
    } catch (reason) {
      if (!mounted.current) return;
      const code = (reason as { code?: string })?.code || 'request_failed';
      setError(t(`invoiceArchive.errors.${code}`, { defaultValue: invoiceError(reason, t('invoiceArchive.uploadFailed')) }));
      if (code === 'invoice_number_required_for_receiving' && metadata.current) {
        metadata.current.open = true;
        // The field is enabled once the in-flight request has settled.
        window.setTimeout(() => { if (mounted.current) dialog.current?.querySelector<HTMLInputElement>('[data-testid="upload-invoice-number"]')?.focus(); }, 0);
      }
    } finally { lock.current = false; if (mounted.current) setWorking(false); }
  }
  return <div className="invoice-upload-overlay invoice-archive" onKeyDown={keyboard}>
    <div className="invoice-upload-backdrop" aria-hidden="true" onClick={dismiss} />
    <div className="invoice-upload-dialog panel" role="dialog" aria-modal="true" aria-labelledby={titleId} ref={dialog}>
      <header><h2 id={titleId}>{t('invoiceArchive.addInvoice')}</h2><button type="button" data-testid="invoice-upload-close" disabled={working} aria-label={t('common.close')} onClick={dismiss}><X size={18} /></button></header>
      {context && <p>{context}</p>}<p className="muted small">{t('invoiceUpload.help')}</p>
      {error && <div role="alert" className="notice error" data-testid="invoice-upload-error">{error}<p><Link to="/settings/logs" target="_blank" rel="noreferrer">{t('invoiceUpload.analyze')}</Link></p></div>}
      {result && <div role="status" className={`notice ${result.processing?.status === 'complete' ? 'success' : 'warning'}`} data-testid="invoice-upload-result">
        <strong>{t(`invoiceUpload.results.${result.processing?.status || 'saved'}`)}</strong>
        {result.processing && <p>{t('invoiceUpload.lineCount', { parsed: result.processing.parsed_lines, stored: result.processing.stored_lines })}</p>}
        <p>{t(result.processing?.receiving_ready ? 'invoiceUpload.ready' : 'invoiceUpload.notReady')}</p>
        {!!result.processing?.missing_fields?.length && <p>{t('invoiceUpload.missingFields', { fields: result.processing.missing_fields.map(field => t(`invoiceUpload.fields.${field}`, { defaultValue: field })).join(', ') })}</p>}
        {!!result.processing?.issues?.length && <ul>{result.processing.issues.map((issue, index) => <li key={index}>{t(`invoiceUpload.issues.${issue}`, { defaultValue: issue })}</li>)}</ul>}
        {result.processing?.status !== 'complete' && <p><Link to="/settings/logs" target="_blank" rel="noreferrer">{t('invoiceUpload.analyze')}</Link></p>}
      </div>}
      {refreshWarning && <p role="status" className="notice warning">{t('invoiceUpload.refreshFailed')}</p>}
      <form onSubmit={submit} data-testid="invoice-upload-form"><fieldset disabled={working}>
        <label>{t('invoiceArchive.supplier')}<select required data-testid="upload-supplier" value={upload.supplier_code} onChange={event => { setError(''); setUpload({ ...upload, supplier_code: event.target.value }); }}>
          <option value="">{t('invoiceArchive.chooseSupplier')}</option>{initialSupplier && !suppliers.some(row => row.code === initialSupplier) && <option value={initialSupplier}>{initialSupplier}</option>}
          {suppliers.map(row => <option key={row.code} value={row.code}>{row.name}</option>)}
        </select></label>
        <section className={`invoice-dropzone${dragging ? ' dragging' : ''}${working ? ' disabled' : ''}`} data-testid="invoice-dropzone"
          onDragEnter={event => { if (event.dataTransfer.types.includes('Files')) { event.preventDefault(); if (!working) setDragging(true); } }}
          onDragOver={event => { if (event.dataTransfer.types.includes('Files')) { event.preventDefault(); event.dataTransfer.dropEffect = working ? 'none' : 'copy'; } }}
          onDragLeave={event => { if (!(event.relatedTarget as Node | null)?.nodeType || !event.currentTarget.contains(event.relatedTarget as Node)) setDragging(false); }}
          onDrop={event => { event.preventDefault(); setDragging(false); choose(event.dataTransfer.files); }}>
          <Upload size={24} aria-hidden="true" /><div><strong>{t('invoiceArchive.dropFile')}</strong><p className="muted small">{t('invoiceArchive.scanHelp')}</p></div>
          <button type="button" onClick={() => fileInput.current?.click()}>{t('invoiceArchive.chooseFile')}</button>
          <input ref={fileInput} hidden type="file" accept={accept} data-testid="invoice-upload-file" onChange={event => choose(event.target.files)} />
        </section>
        <div className="upload-selected"><span className="muted small">{t('invoiceArchive.file')}</span><strong data-testid="invoice-selected-file">{file?.name || t('invoiceArchive.noFile')}</strong></div>
        <details ref={metadata} data-testid="invoice-upload-metadata"><summary>{t('invoiceUpload.metadata')}</summary><div className="fields">
          {(['invoice_number', 'invoice_date', 'due_date'] as const).map(key => <label key={key}>{t(`invoiceArchive.${key}`)}<input type={key.endsWith('date') ? 'date' : 'text'} data-testid={`upload-${key.replace(/_/g, '-')}`} value={upload[key]} onChange={event => { setError(''); setUpload({ ...upload, [key]: event.target.value }); }} /></label>)}
          <label className="check"><input type="checkbox" checked={upload.vat_included} onChange={event => { setError(''); setUpload({ ...upload, vat_included: event.target.checked }); }} />{t('invoiceArchive.vatIncluded')}</label>
        </div></details>
        <footer><button type="button" onClick={dismiss}>{t(result ? 'invoiceUpload.continue' : 'common.cancel')}</button><button className="primary" disabled={!file || !upload.supplier_code} data-testid="invoice-upload-save">{t(working ? 'invoiceUpload.processing' : 'invoiceArchive.addInvoice')}</button></footer>
      </fieldset>{working && <p role="status">{t('invoiceUpload.processing')}</p>}</form>
    </div>
  </div>;
}
