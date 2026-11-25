import React, { useEffect, useId, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Upload, X } from 'lucide-react';
import { accessRevision } from '../api/access';
import { getArchiveSuppliers, getInvoiceSupplierOptions, uploadArchivedInvoice, type InvoiceUploadResult } from '../api/invoiceArchive';
import { invoiceError } from '../pages/invoiceArchiveUi';
import '../pages/invoiceArchive.css';
import './InvoiceUploadModal.css';

const extensions = new Set(['pdf', 'csv', 'tsv', 'xlsx', 'xls', 'doc', 'docx', 'xml', 'isdoc', 'txt', 'jpg', 'jpeg', 'png']);
const accept = [...extensions].map(value => '.' + value).join(',');
const supplierErrors = new Set(['invoice_supplier_required', 'invoice_supplier_ambiguous']);
type SupplierOption = { code: string; name: string };
type Fields = { supplier_code: string; invoice_number: string; invoice_date: string; due_date: string; vat_included: boolean; service_issuer: string; service_total: string; service_currency: string };
type SelectedFile = {
  id: number;
  file: File;
  pdf: boolean;
  isdoc: boolean;
  service: boolean;
  fields: Fields;
  validation?: string;
  error?: { code: string; message: string };
  needsSupplier?: boolean;
  result?: InvoiceUploadResult;
};
type Props = { open: boolean; allowService?: boolean; initialSupplier?: string; context?: string; onClose: () => void;
  onUploaded?: (results: readonly InvoiceUploadResult[]) => void | Promise<void> };

function canUpload(item: SelectedFile) {
  return !item.validation && !item.result && (item.service ? !!item.fields.service_issuer.trim() : !!item.fields.supplier_code || item.isdoc || (item.pdf && !item.needsSupplier));
}

/** All entry points retain their context while sharing upload and processing feedback. */
export function InvoiceUploadModal({ open, ...props }: Props) {
  return open ? <UploadDialog {...props} /> : null;
}
function UploadDialog({ initialSupplier = '', context, onClose, onUploaded, allowService = false }: Omit<Props, 'open'>) {
  const { t, i18n } = useTranslation();
  const titleId = useId();
  const dialog = useRef<HTMLDivElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const nextId = useRef(0);
  const close = useRef(onClose); close.current = onClose;
  const [suppliers, setSuppliers] = useState<SupplierOption[]>([]);
  const [defaultSupplier, setDefaultSupplier] = useState(initialSupplier);
  const [files, setFiles] = useState<SelectedFile[]>([]);
  const [dragging, setDragging] = useState(false);
  const [supplierError, setSupplierError] = useState(false);
  const [working, setWorking] = useState(false);
  const [refreshWarning, setRefreshWarning] = useState(false);
  const lock = useRef(false), mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    const previous = document.activeElement as HTMLElement | null;
    dialog.current?.querySelector<HTMLElement>('select')?.focus();
    return () => { mounted.current = false; if (previous?.isConnected) previous.focus(); };
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    Promise.allSettled([getArchiveSuppliers(controller.signal), getInvoiceSupplierOptions(controller.signal)]).then(values => {
      if (controller.signal.aborted) return;
      const options = new Map<string, SupplierOption>();
      if (initialSupplier) options.set(initialSupplier, { code: initialSupplier, name: initialSupplier });
      values.forEach(value => { if (value.status === 'fulfilled') value.value.forEach(row => options.set(row.code, row)); });
      setSuppliers([...options.values()].sort((a, b) => a.name.localeCompare(b.name, i18n.language)));
      setSupplierError(values.every(value => value.status === 'rejected'));
    });
    return () => controller.abort();
  }, [initialSupplier, i18n.language]);
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
  function choose(selection: FileList | File[] | null) {
    if (lock.current || !selection?.length) return;
    setFiles(Array.from(selection, file => {
      const extension = file.name.split('.').pop()?.toLowerCase() || '';
      const validation = !extensions.has(extension) ? 'unsupportedFile'
        : !file.size || file.size > 50 * 1024 * 1024 ? 'fileSizeLimit' : undefined;
      const pdf = extension === 'pdf';
      return { id: ++nextId.current, file, pdf, isdoc: extension === 'isdoc', service: false, validation, fields: {
        supplier_code: pdf && selection.length > 1 ? '' : defaultSupplier,
        invoice_number: '', invoice_date: '', due_date: '', vat_included: true, service_issuer: '', service_total: '', service_currency: '',
      } };
    }));
    setRefreshWarning(false);
    if (fileInput.current) fileInput.current.value = '';
  }
  const updateFile = (id: number, update: (item: SelectedFile) => SelectedFile) =>
    setFiles(items => items.map(item => item.id === id ? update(item) : item));

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    const pending = files.filter(canUpload).sort((left, right) => Number(left.isdoc) - Number(right.isdoc));
    if (lock.current || !pending.length) return;
    lock.current = true; setWorking(true); setRefreshWarning(false);
    const startedAccess = accessRevision();
    const current = () => mounted.current && startedAccess === accessRevision();
    const saved: InvoiceUploadResult[] = [];
    try {
      for (const item of pending) {
        if (!current()) break;
        const body = new FormData(); body.append('file', item.file);
        Object.entries(item.fields).forEach(([key, value]) => {
          if (value !== '' && (item.service || !key.startsWith('service_')) && (!item.isdoc || key === 'supplier_code')) {
            body.append(key, String(value));
          }
        });
        try {
          const result = await uploadArchivedInvoice(body);
          if (!current()) break;
          saved.push(result);
          updateFile(item.id, row => ({ ...row, result, error: undefined }));
        } catch (reason) {
          if (!current()) break;
          const code = (reason as { code?: string })?.code || 'request_failed';
          const message = t('invoiceArchive.errors.' + code, { defaultValue: invoiceError(reason, t('invoiceArchive.uploadFailed')) });
          updateFile(item.id, row => ({ ...row, error: { code, message }, needsSupplier: supplierErrors.has(code) }));
        }
      }
      if (current() && saved.length) {
        try { await onUploaded?.(saved); } catch { if (current()) setRefreshWarning(true); }
      }
    } finally { lock.current = false; if (mounted.current) setWorking(false); }
  }

  const single = files.length === 1;
  return <div className="invoice-upload-overlay invoice-archive" onKeyDown={keyboard}>
    <div className="invoice-upload-backdrop" aria-hidden="true" onClick={dismiss} />
    <div className="invoice-upload-dialog panel" role="dialog" aria-modal="true" aria-labelledby={titleId} ref={dialog}>
      <header><h2 id={titleId}>{t('invoiceArchive.addInvoice')}</h2><button type="button" data-testid="invoice-upload-close" disabled={working} aria-label={t('common.close')} onClick={dismiss}><X size={18} /></button></header>
      {context && <p>{context}</p>}<p className="muted small">{t('invoiceUpload.help')}</p>
      {supplierError && <p role="status" className="notice warning">{t('invoiceArchive.suppliersFailed')}</p>}
      {refreshWarning && <p role="status" className="notice warning">{t('invoiceUpload.refreshFailed')}</p>}
      <form onSubmit={submit} data-testid="invoice-upload-form"><fieldset disabled={working}>
        {!files.length && <SupplierSelect suppliers={suppliers} value={defaultSupplier} auto testId="upload-supplier" onChange={setDefaultSupplier} />}
        <section className={'invoice-dropzone' + (dragging ? ' dragging' : '') + (working ? ' disabled' : '')} data-testid="invoice-dropzone"
          onDragEnter={event => { if (event.dataTransfer.types.includes('Files')) { event.preventDefault(); if (!working) setDragging(true); } }}
          onDragOver={event => { if (event.dataTransfer.types.includes('Files')) { event.preventDefault(); event.dataTransfer.dropEffect = working ? 'none' : 'copy'; } }}
          onDragLeave={event => { if (!(event.relatedTarget as Node | null)?.nodeType || !event.currentTarget.contains(event.relatedTarget as Node)) setDragging(false); }}
          onDrop={event => { event.preventDefault(); setDragging(false); choose(event.dataTransfer.files); }}>
          <Upload size={24} aria-hidden="true" /><div><strong>{t('invoiceUpload.dropFiles')}</strong><p className="muted small">{t('invoiceArchive.scanHelp')}</p></div>
          <button type="button" onClick={() => fileInput.current?.click()}>{t('invoiceUpload.chooseFiles')}</button>
          <input ref={fileInput} hidden multiple type="file" accept={accept} data-testid="invoice-upload-file" onChange={event => choose(event.target.files)} />
        </section>
        {files.map(item => <FileEntry key={item.id} item={item} single={single} suppliers={suppliers} working={working} allowService={allowService}
          onService={service => updateFile(item.id, row => ({ ...row, service, error: undefined }))} onChange={fields => {
          if (single) setDefaultSupplier(fields.supplier_code);
          updateFile(item.id, row => ({ ...row, fields, error: undefined, needsSupplier: row.needsSupplier && !fields.supplier_code }));
        }} />)}
        <footer><button type="button" onClick={dismiss}>{t(files.some(file => file.result) ? 'invoiceUpload.continue' : 'common.cancel')}</button><button className="primary" disabled={!files.some(canUpload)} data-testid="invoice-upload-save">{t(working ? 'invoiceUpload.processing' : single ? 'invoiceArchive.addInvoice' : 'invoiceUpload.processFiles')}</button></footer>
      </fieldset>{working && <p role="status">{t('invoiceUpload.processing')}</p>}</form>
    </div>
  </div>;
}

function SupplierSelect({ suppliers, value, auto, testId, onChange }: {
  suppliers: SupplierOption[]; value: string; auto: boolean; testId: string; onChange: (value: string) => void;
}) {
  const { t } = useTranslation();
  return <label>{t('invoiceArchive.supplier')}<select data-testid={testId} value={value} onChange={event => onChange(event.target.value)}>
    <option value="">{t(auto ? 'invoiceUpload.autoSupplier' : 'invoiceArchive.chooseSupplier')}</option>
    {value && !suppliers.some(row => row.code === value) && <option value={value}>{value}</option>}
    {suppliers.map(row => <option key={row.code} value={row.code}>{row.name}</option>)}
  </select></label>;
}

function FileEntry({ item, single, suppliers, working, allowService, onService, onChange }: {
  allowService: boolean; onService: (service: boolean) => void;
  item: SelectedFile; single: boolean; suppliers: SupplierOption[]; working: boolean; onChange: (fields: Fields) => void;
}) {
  const { t } = useTranslation();
  const metadata = useRef<HTMLDetailsElement>(null);
  const number = useRef<HTMLInputElement>(null);
  const requiredNumber = item.error?.code === 'invoice_number_required_for_receiving';
  const northfinderSpreadsheet = item.fields.supplier_code === 'northfinder' && /\.xlsx?$/i.test(item.file.name);
  useEffect(() => {
    if ((northfinderSpreadsheet || requiredNumber || item.service) && metadata.current) metadata.current.open = true;
    if (requiredNumber && single && !working) number.current?.focus();
  }, [northfinderSpreadsheet, requiredNumber, single, working, item.service]);
  const testId = (name: string) => single ? name : name + '-' + item.id;
  const result = item.result;
  const supplierName = (item.service && item.fields.service_issuer) || suppliers.find(supplier => supplier.code === result?.supplier_code)?.name || result?.supplier_code;
  return <section className="invoice-upload-file-entry" data-testid={testId('invoice-upload-entry')}>
    <div className="upload-selected"><strong data-testid={testId('invoice-selected-file')}>{item.file.name}</strong></div>
    {(item.validation || item.error) && <div role="alert" className="notice error" data-testid={testId('invoice-upload-error')}>
      {item.validation ? t('invoiceArchive.' + item.validation) : item.error?.message}
      {item.error && !supplierErrors.has(item.error.code) && <p><Link to="/settings/logs" target="_blank" rel="noreferrer">{t('invoiceUpload.analyze')}</Link></p>}
    </div>}
    {!item.validation && !result && <>
      {allowService && !item.isdoc && <label className="check"><input type="checkbox" checked={item.service}
        data-testid={testId('upload-service-toggle')} onChange={event => onService(event.target.checked)} />{t('invoiceArchive.serviceDocument')}</label>}
      {item.isdoc && <p className="muted small">{t('invoiceArchive.isdocHelp')}</p>}
      {item.service ? <label>{t('invoiceArchive.issuer')}<input required maxLength={255} value={item.fields.service_issuer}
        onChange={event => onChange({ ...item.fields, service_issuer: event.target.value })} /></label>
        : !item.isdoc && <SupplierSelect suppliers={suppliers} value={item.fields.supplier_code} auto={item.pdf && !item.needsSupplier} testId={testId('upload-supplier')}
          onChange={supplier_code => onChange({ ...item.fields, supplier_code })} />}
      {!item.service && !item.isdoc && !item.pdf && !item.fields.supplier_code && <p className="muted small">{t('invoiceUpload.manualSupplier')}</p>}
      {!item.isdoc && <details ref={metadata} data-testid={testId('invoice-upload-metadata')}><summary>{t('invoiceUpload.metadata')}</summary><div className="fields">
        {(['invoice_number', 'invoice_date', 'due_date'] as const).map(key => <label key={key}>{t('invoiceArchive.' + key)}<input
          ref={key === 'invoice_number' ? number : undefined} type={key.endsWith('date') ? 'date' : 'text'} data-testid={testId('upload-' + key.replace(/_/g, '-'))}
          value={item.fields[key]} onChange={event => onChange({ ...item.fields, [key]: event.target.value })} /></label>)}
        {item.service ? <>
          <label>{t('invoiceArchive.total_amount')}<input inputMode="decimal" value={item.fields.service_total}
            onChange={event => onChange({ ...item.fields, service_total: event.target.value })} /></label>
          <label>{t('invoiceArchive.currency')}<input maxLength={3} pattern="[A-Z]{3}" value={item.fields.service_currency}
            onChange={event => onChange({ ...item.fields, service_currency: event.target.value.toUpperCase() })} /></label>
        </> : <label className="check"><input type="checkbox" checked={item.fields.vat_included} onChange={event => onChange({ ...item.fields, vat_included: event.target.checked })} />{t('invoiceArchive.vatIncluded')}</label>}
      </div></details>}
    </>}
    {result && <div role="status" className={'notice ' + (result.processing?.status === 'complete' ? 'success' : 'warning')} data-testid={testId('invoice-upload-result')}>
      <strong>{t('invoiceUpload.results.' + (result.processing?.status || 'saved'))}</strong>
      {supplierName && <p>{supplierName}{result.invoice_number ? ' · ' + result.invoice_number : ''}</p>}
      {result.processing && <p>{t('invoiceUpload.lineCount', { parsed: result.processing.parsed_lines, stored: result.processing.stored_lines })}</p>}
      <p>{t(item.service ? 'invoiceArchive.serviceDocument' : item.isdoc ? 'invoiceArchive.isdocAccepted' : result.processing?.receiving_ready ? 'invoiceUpload.ready' : 'invoiceUpload.notReady')}</p>
      {!!result.processing?.missing_fields?.length && <p>{t('invoiceUpload.missingFields', { fields: result.processing.missing_fields.map(field => t('invoiceUpload.fields.' + field, { defaultValue: field })).join(', ') })}</p>}
      {!!result.processing?.issues?.length && <ul>{result.processing.issues.map((issue, index) => <li key={index}>{t('invoiceUpload.issues.' + issue, { defaultValue: issue })}</li>)}</ul>}
      {result.processing?.status !== 'complete' && <p><Link to="/settings/logs" target="_blank" rel="noreferrer">{t('invoiceUpload.analyze')}</Link></p>}
    </div>}
  </section>;
}
