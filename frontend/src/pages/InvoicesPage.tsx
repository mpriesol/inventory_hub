import React, { useEffect, useMemo, useState, useSyncExternalStore } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Download, RefreshCw, Upload, ChevronLeft, ChevronRight, FileText } from 'lucide-react';
import { accessRevision, subscribeAccess } from '../api/access';
import {
  ArchivedInvoice, InvoiceArchiveFilters, InvoiceArchivePage, InvoiceArchiveStats, ShopInvoiceSyncResult, SupplierInvoiceReconcileResult,
  downloadInvoiceArchive, getArchiveShops, getArchiveSuppliers, getInvoiceArchive, getInvoiceArchiveStats,
  invoiceArchiveDefaults, invoiceArchiveQuery, reconcileSupplierInvoiceArchive, syncShopInvoiceArchive, uploadArchivedInvoice,
} from '../api/invoiceArchive';
import { invoiceDate, invoiceError, invoiceMoney, invoiceSourceDiagnostic, InvoicePaymentBadge } from './invoiceArchiveUi';
import './invoiceArchive.css';

function filtersFromUrl(params: URLSearchParams): InvoiceArchiveFilters {
  const result = { ...invoiceArchiveDefaults };
  (Object.keys(result) as (keyof InvoiceArchiveFilters)[]).forEach(key => {
    if (params.has(key)) (result as Record<string, string>)[key] = params.get(key) || '';
  });
  if (!['purchase', 'sale'].includes(result.direction)) result.direction = 'purchase';
  if (!['asc', 'desc'].includes(result.sort_order)) result.sort_order = 'desc';
  return result;
}

function ArchiveDashboard({ stats }: { stats: InvoiceArchiveStats }) {
  const { t, i18n } = useTranslation();
  const [chosen, setChosen] = useState('EUR');
  const group = stats.totals_by_currency.find(row => (row.currency || '') === chosen) || stats.totals_by_currency[0];
  const currency = group?.currency ?? null;
  const monthly = stats.monthly.filter(row => row.currency === currency).slice(-6);
  const aging = stats.aging.filter(row => row.currency === currency);
  const bars = (rows: { label: string; value: string | null }[], overdue = false) => {
    const maximum = Math.max(0, ...rows.map(row => Math.abs(Number(row.value || 0))));
    return rows.length ? rows.map((row, index) => <div className="chart-row" key={index}>
      <span>{row.label}</span><span className="chart-track" aria-hidden="true"><span className={`chart-fill${overdue ? ' overdue' : ''}`} style={{ display: 'block', width: `${maximum ? Math.abs(Number(row.value || 0)) / maximum * 100 : 0}%` }} /></span>
      <span className="amount">{invoiceMoney(row.value, currency, i18n.language)}</span>
    </div>) : <p className="muted small">{t('invoiceArchive.noChartData')}</p>;
  };
  return <section className="panel" data-testid="invoice-dashboard">
    <header><div><h2>{t(stats.direction === 'sale' ? 'invoiceArchive.salesOverview' : 'invoiceArchive.purchaseOverview')}</h2><p className="muted small">{t('invoiceArchive.dashboardScope', { count: stats.total_invoices })}</p></div>
      {stats.totals_by_currency.length > 1 && <label>{t('invoiceArchive.currency')}<select data-testid="dashboard-currency" value={currency || ''} onChange={event => setChosen(event.target.value)}>
        {stats.totals_by_currency.map(row => <option key={row.currency || 'unknown'} value={row.currency || ''}>{row.currency || t('invoiceArchive.unknownCurrency')}</option>)}
      </select></label>}
    </header>
    {group ? <><div className="metric-grid" style={{ marginTop: '.9rem' }}>
      <div className="metric"><span className="muted">{t('invoiceArchive.invoiceValue')}</span><strong>{invoiceMoney(group.total_amount, currency, i18n.language)}</strong><small>{t('invoiceArchive.documentsCount', { count: group.invoice_count })}</small></div>
      <div className="metric"><span className="muted">{t('invoiceArchive.outstanding')}</span><strong>{invoiceMoney(group.outstanding_amount, currency, i18n.language)}</strong><small>{t('invoiceArchive.documentsCount', { count: group.unpaid_count })}</small></div>
      <div className="metric"><span className="muted">{t('invoiceArchive.status.overdue')}</span><strong style={{ color: 'var(--color-error)' }}>{invoiceMoney(group.overdue_amount, currency, i18n.language)}</strong><small>{t('invoiceArchive.documentsCount', { count: group.overdue_count })}</small></div>
    </div>
    {group.unknown_amount_count > 0 && <p className="notice warning small">{t('invoiceArchive.unknownAmounts', { count: group.unknown_amount_count })}</p>}
    {!!group.unknown_outstanding_count && <p className="notice warning small" data-testid="invoice-unknown-payments">{t('invoiceArchive.unknownPayments', { count: group.unknown_outstanding_count })}</p>}
    <div className="charts"><div><h2>{t('invoiceArchive.monthly')}</h2>{bars(monthly.map(row => ({ label: row.month, value: row.total_amount })))}</div>
      <div><h2>{t('invoiceArchive.agingTitle')}</h2>{bars(aging.map(row => ({ label: t(`invoiceArchive.aging.${row.bucket}`), value: row.outstanding_amount })), true)}</div></div>
    </> : <p className="muted">{t('invoiceArchive.empty')}</p>}
  </section>;
}

export function InvoicesPage() {
  const { t, i18n } = useTranslation();
  const access = useSyncExternalStore(subscribeAccess, accessRevision);
  const [params, setParams] = useSearchParams();
  const query = params.toString();
  const applied = useMemo(() => filtersFromUrl(new URLSearchParams(query)), [query]);
  const page = Math.max(1, Number.parseInt(params.get('page') || '1', 10) || 1);
  const [filters, setFilters] = useState(applied);
  const [data, setData] = useState<InvoiceArchivePage | null>(null);
  const [stats, setStats] = useState<InvoiceArchiveStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [statsError, setStatsError] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [refresh, setRefresh] = useState(0);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [working, setWorking] = useState(false);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [reconcileOpen, setReconcileOpen] = useState(false);
  const [reconcileSupplier, setReconcileSupplier] = useState('');
  const [reconcileResult, setReconcileResult] = useState<SupplierInvoiceReconcileResult | null>(null);
  const [suppliers, setSuppliers] = useState<{ code: string; name: string }[]>([]);
  const [upload, setUpload] = useState({ supplier_code: '', invoice_number: '', invoice_date: '', due_date: '', vat_included: true });
  const [file, setFile] = useState<File | null>(null);
  const [syncOpen, setSyncOpen] = useState(false);
  const [shops, setShops] = useState<{ code: string; name: string }[]>([]);
  const [sync, setSync] = useState({ shop: '', date_from: '', date_to: '' });
  const [syncResult, setSyncResult] = useState<ShopInvoiceSyncResult | null>(null);
  const [syncUncertain, setSyncUncertain] = useState(false);
  const [retryAt, setRetryAt] = useState(0);
  const [now, setNow] = useState(Date.now());
  useEffect(() => { if (retryAt > Date.now()) { const timer = window.setInterval(() => setNow(Date.now()), 1000); return () => window.clearInterval(timer); } }, [retryAt]);

  useEffect(() => { setFilters(applied); setSelected(new Set()); }, [applied]);
  useEffect(() => {
    const controller = new AbortController(); setLoading(true); setError(''); setData(null); setSelected(new Set());
    getInvoiceArchive(applied, page, controller.signal).then(value => { if (!controller.signal.aborted) setData(value); })
      .catch(reason => { if (!controller.signal.aborted) setError(invoiceError(reason, t('invoiceArchive.loadFailed'))); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [query, refresh, access, t]);
  useEffect(() => {
    const controller = new AbortController(); setStats(null); setStatsError(false);
    getInvoiceArchiveStats(applied.direction, controller.signal).then(value => { if (!controller.signal.aborted) setStats(value); })
      .catch(() => { if (!controller.signal.aborted) setStatsError(true); });
    return () => controller.abort();
  }, [applied.direction, refresh, access]);
  useEffect(() => {
    const controller = new AbortController();
    if (uploadOpen || reconcileOpen) getArchiveSuppliers(controller.signal).then(setSuppliers).catch(() => { if (!controller.signal.aborted) setError(t('invoiceArchive.suppliersFailed')); });
    if (syncOpen) getArchiveShops(controller.signal).then(value => setShops(value.shops)).catch(() => { if (!controller.signal.aborted) setError(t('invoiceArchive.shopsFailed')); });
    return () => controller.abort();
  }, [uploadOpen, reconcileOpen, syncOpen, access]);

  const update = (key: keyof InvoiceArchiveFilters, value: string) => { setFilters(old => ({ ...old, [key]: value })); setSelected(new Set()); };
  const apply = (values: InvoiceArchiveFilters = filters, nextPage = 1) => setParams(invoiceArchiveQuery({ ...values, page: nextPage }));
  const status = applied.is_overdue === 'true' ? 'overdue' : applied.payment_status || 'all';
  const statusFilter = (value: string) => apply({ ...applied, payment_status: ['all', 'overdue'].includes(value) ? '' : value, is_overdue: value === 'overdue' ? 'true' : '' });
  const download = async (ids: number | number[]) => {
    if (working) return; setWorking(true); setError('');
    try { await downloadInvoiceArchive(ids); } catch (reason) { setError(invoiceError(reason, t('invoiceArchive.downloadFailed'))); }
    finally { setWorking(false); }
  };
  const handleUpload = async (event: React.FormEvent) => {
    event.preventDefault(); if (!file || !upload.supplier_code || working) return;
    setWorking(true); setError(''); setMessage('');
    const body = new FormData(); body.append('file', file);
    Object.entries(upload).forEach(([key, value]) => { if (value !== '') body.append(key, String(value)); });
    try {
      await uploadArchivedInvoice(body); setFile(null); setUploadOpen(false); setMessage(t('invoiceArchive.uploaded'));
      setRefresh(value => value + 1);
    } catch (reason) { setError(invoiceError(reason, t('invoiceArchive.uploadFailed'))); }
    finally { setWorking(false); }
  };
  const syncBatch = async (continuation = false) => {
    if (working || !sync.shop || !sync.date_from || !sync.date_to) return;
    setWorking(true); setError(''); setSyncUncertain(false);
    try {
      const value = await syncShopInvoiceArchive(sync.shop, { date_from: sync.date_from, date_to: sync.date_to, ...(continuation && syncResult?.next ? syncResult.next : { page: 1, offset: 0 }) });
      setSyncResult(value); setRetryAt(value.retry_after ? Date.now() + value.retry_after * 1000 : 0); setNow(Date.now()); setRefresh(value => value + 1);
    } catch (reason) {
      setSyncUncertain(true);
      setError([invoiceError(reason, t('invoiceArchive.syncFailed')),
        invoiceSourceDiagnostic((reason as { detail?: unknown })?.detail, t)].filter(Boolean).join(' '));
    }
    finally { setWorking(false); }
  };
  const reconcile = async (continuation = false) => {
    if (working || !reconcileSupplier) return; setWorking(true); setError('');
    try {
      setReconcileResult(await reconcileSupplierInvoiceArchive(reconcileSupplier, continuation ? reconcileResult?.next_after_key || null : null));
      setRefresh(value => value + 1);
    } catch (reason) { setError(invoiceError(reason, t('invoiceArchive.reconcileFailed'))); }
    finally { setWorking(false); }
  };
  const invoiceTitle = (invoice: ArchivedInvoice) => invoice.invoice_number || invoice.original_filename;
  const rows = data?.items || [];

  return <div className="invoice-archive">
    <header><div><h1>{t('invoiceArchive.title')}</h1><p className="muted">{t('invoiceArchive.subtitle')}</p></div>
      <div className="actions"><button data-testid="invoice-refresh" disabled={working || loading} onClick={() => setRefresh(value => value + 1)}><RefreshCw size={16} />{t('common.refresh')}</button>
        {applied.direction === 'purchase' ? <><button data-testid="invoice-reconcile-open" disabled={working} onClick={() => setReconcileOpen(value => !value)}>{t('invoiceArchive.reconcile')}</button><button className="primary" data-testid="invoice-upload-open" disabled={working} onClick={() => setUploadOpen(value => !value)}><Upload size={16} />{t('receiving.uploadArchive')}</button></>
          : <button className="primary" data-testid="invoice-sync-open" disabled={working} onClick={() => setSyncOpen(value => !value)}><Download size={16} />{t('invoiceArchive.syncShop')}</button>}</div>
    </header>
    <div className="status-tabs" aria-label={t('invoiceArchive.direction')}>
      {(['purchase', 'sale'] as const).map(direction => <button key={direction} data-testid={`invoice-direction-${direction}`} disabled={working} aria-pressed={applied.direction === direction} onClick={() => { apply({ ...invoiceArchiveDefaults, direction }); setUploadOpen(false); setSyncOpen(false); setReconcileOpen(false); }}>
        {t(`invoiceArchive.directionLabels.${direction}`)}</button>)}
    </div>
    {error && <div role="alert" className="notice error" data-testid="invoice-error">{error}</div>}
    {message && <div role="status" className="notice success">{message}</div>}
    {reconcileOpen && <section className="panel"><h2>{t('invoiceArchive.reconcile')}</h2><p className="muted small">{t('invoiceArchive.reconcileHelp')}</p>
      <div className="fields" style={{ marginTop: '.8rem' }}><label>{t('invoiceArchive.supplier')}<select data-testid="invoice-reconcile-supplier" disabled={working} value={reconcileSupplier} onChange={event => { setReconcileSupplier(event.target.value); setReconcileResult(null); }}><option value="">{t('invoiceArchive.chooseSupplier')}</option>{suppliers.map(supplier => <option key={supplier.code} value={supplier.code}>{supplier.name}</option>)}</select></label></div>
      <footer><button disabled={working} onClick={() => setReconcileOpen(false)}>{t('common.close')}</button><button className="primary" data-testid="invoice-reconcile-start" disabled={working || !reconcileSupplier} onClick={() => reconcile()}>{t('invoiceArchive.reconcileStart')}</button></footer>
      {reconcileResult && <><p role="status">{t('invoiceArchive.reconcileResult', { scanned: reconcileResult.scanned, created: reconcileResult.created, existing: reconcileResult.existing, conflicts: reconcileResult.conflicts.length })}</p>
        {reconcileResult.conflicts.length > 0 && <ul>{reconcileResult.conflicts.map((row, index) => <li key={index}>{row.source_key}: {row.code}</li>)}</ul>}
        {reconcileResult.next_after_key ? <button disabled={working} data-testid="invoice-reconcile-next" onClick={() => reconcile(true)}>{t('invoiceArchive.reconcileNext')}</button> : <p className="muted small">{t('invoiceArchive.reconcileDone')}</p>}
      </>}
    </section>}
    {uploadOpen && <form className="panel" onSubmit={handleUpload} data-testid="invoice-upload-form"><h2>{t('receiving.uploadArchive')}</h2>
      <p className="muted small">{t('receiving.archiveHelp')} <Link to="/receiving">{t('receiving.goToReceipt')}</Link></p>
      <fieldset disabled={working}><div className="fields" style={{ marginTop: '.9rem' }}>
        <label>{t('invoiceArchive.supplier')}<select required data-testid="upload-supplier" value={upload.supplier_code} onChange={event => setUpload({ ...upload, supplier_code: event.target.value })}><option value="">{t('invoiceArchive.chooseSupplier')}</option>{suppliers.map(row => <option key={row.code} value={row.code}>{row.name}</option>)}</select></label>
        <label>{t('invoiceArchive.file')}<input required type="file" accept=".pdf,.csv,.xlsx,.xls,.doc,.docx,.xml,.txt,.jpg,.jpeg,.png" onChange={event => setFile(event.target.files?.[0] || null)} /></label>
        {(['invoice_number', 'invoice_date', 'due_date'] as const).map(key => <label key={key}>{t(`invoiceArchive.${key}`)}<input type={key.endsWith('date') ? 'date' : 'text'} value={upload[key]} onChange={event => setUpload({ ...upload, [key]: event.target.value })} /></label>)}
        <label className="check"><input type="checkbox" checked={upload.vat_included} onChange={event => setUpload({ ...upload, vat_included: event.target.checked })} />{t('invoiceArchive.vatIncluded')}</label>
      </div><footer><button type="button" onClick={() => setUploadOpen(false)}>{t('common.cancel')}</button><button className="primary" disabled={!file || !upload.supplier_code}>{t('receiving.uploadArchive')}</button></footer></fieldset>
    </form>}
    {syncOpen && <section className="panel"><h2>{t('invoiceArchive.syncShop')}</h2><p className="muted small">{t('invoiceArchive.syncHelp')}</p>
      <fieldset disabled={working}><div className="fields" style={{ marginTop: '.9rem' }}>
        <label>{t('invoiceArchive.shop')}<select data-testid="sync-shop" value={sync.shop} onChange={event => { setSync({ ...sync, shop: event.target.value }); setSyncResult(null); }}><option value="">{t('invoiceArchive.chooseShop')}</option>{shops.map(shop => <option key={shop.code} value={shop.code}>{shop.name}</option>)}</select></label>
        {(['date_from', 'date_to'] as const).map(key => <label key={key}>{t(`invoiceArchive.${key}`)}<input data-testid={`sync-${key}`} type="date" value={sync[key]} onChange={event => { setSync({ ...sync, [key]: event.target.value }); setSyncResult(null); }} /></label>)}
      </div><footer><button onClick={() => setSyncOpen(false)}>{t('common.close')}</button><button className="primary" data-testid="invoice-sync-start" disabled={!sync.shop || !sync.date_from || !sync.date_to || sync.date_to < sync.date_from} onClick={() => syncBatch()}>{t('invoiceArchive.syncStart')}</button></footer></fieldset>
      {syncUncertain && <p className="notice warning">{t('invoiceArchive.syncUncertain')}</p>}
      {syncResult && <div style={{ marginTop: '.9rem' }}><p role="status">{t('invoiceArchive.syncResult', { imported: syncResult.imported, unchanged: syncResult.unchanged, failed: syncResult.failed })}</p>
        {syncResult.stopped_error && <p className="notice warning" data-testid="invoice-sync-stopped">{t('invoiceArchive.syncStopped')} ({syncResult.stopped_error}) {invoiceSourceDiagnostic(syncResult, t)}</p>}
        {syncResult.items.some(row => row.error) && <ul>{syncResult.items.filter(row => row.error).map((row, index) => <li key={index}>{row.invoice_number}: {row.error}</li>)}</ul>}
        {syncResult.next ? <button data-testid="invoice-sync-next" disabled={working || retryAt > now} onClick={() => syncBatch(true)}>{t('invoiceArchive.syncNext')}{retryAt > now ? ` (${Math.ceil((retryAt - now) / 1000)} s)` : ''}</button> : <p className="muted small">{t('invoiceArchive.syncDone')}</p>}
      </div>}
    </section>}
    {stats && <ArchiveDashboard stats={stats} />}
    {statsError && <p className="notice warning">{t('invoiceArchive.statsFailed')}</p>}
    <section className="panel">
      <div className="status-tabs" aria-label={t('invoiceArchive.paymentStatus')}>
        {['all', 'unpaid', 'partial', 'paid', 'overdue', 'unknown'].map(value => <button key={value} data-testid={`invoice-status-${value}`} disabled={working} aria-pressed={status === value} onClick={() => statusFilter(value)}>{t(`invoiceArchive.status.${value}`)}</button>)}
      </div>
      <form onSubmit={event => { event.preventDefault(); apply(); }} style={{ marginTop: '1rem' }}><fieldset disabled={working}>
        <div className="fields">
          <label>{t('invoiceArchive.invoice_number')}<input data-testid="invoice-search" value={filters.f_invoice_number} onChange={event => update('f_invoice_number', event.target.value)} /></label>
          <label>{t(applied.direction === 'purchase' ? 'invoiceArchive.supplier' : 'invoiceArchive.source')}<input value={filters.f_supplier} onChange={event => update('f_supplier', event.target.value)} /></label>
          <label>{t('invoiceArchive.date_from')}<input type="date" data-testid="invoice-date-from" value={filters.date_from} onChange={event => update('date_from', event.target.value)} /></label>
          <label>{t('invoiceArchive.date_to')}<input type="date" value={filters.date_to} onChange={event => update('date_to', event.target.value)} /></label>
        </div>
        <details style={{ marginTop: '.8rem' }}><summary>{t('invoiceArchive.moreFilters')}</summary><div className="fields">
          {(['due_from', 'due_to', 'f_amount_min', 'f_amount_max', 'f_currency'] as const).map(key => <label key={key}>{t(`invoiceArchive.${key}`)}<input type={key.startsWith('due') ? 'date' : 'text'} inputMode={key.startsWith('f_amount') ? 'decimal' : undefined} value={filters[key]} onChange={event => update(key, event.target.value)} /></label>)}
          {applied.direction === 'purchase' && <label>{t('invoiceArchive.receiving')}<select value={filters.receiving_status} onChange={event => update('receiving_status', event.target.value)}>{['', 'not_started', 'in_progress', 'completed'].map(value => <option key={value} value={value}>{t(value ? `invoiceArchive.receivingStatus.${value}` : 'invoiceArchive.status.all')}</option>)}</select></label>}
          <label>{t('invoiceArchive.sort')}<select value={filters.sort_by} onChange={event => update('sort_by', event.target.value)}>{['invoice_date', 'due_date', 'created_at', 'invoice_number', 'total_amount'].map(value => <option key={value} value={value}>{t(`invoiceArchive.${value}`)}</option>)}</select></label>
          <label>{t('invoiceArchive.sortOrder')}<select value={filters.sort_order} onChange={event => update('sort_order', event.target.value)}><option value="desc">{t('invoiceArchive.desc')}</option><option value="asc">{t('invoiceArchive.asc')}</option></select></label>
        </div></details>
        <div className="actions"><button type="submit" data-testid="invoice-apply-filters">{t('invoiceArchive.applyFilters')}</button><button type="button" className="quiet" onClick={() => apply({ ...invoiceArchiveDefaults, direction: applied.direction })}>{t('invoiceArchive.clearFilters')}</button></div>
      </fieldset></form>
    </section>
    <section className="panel">
      <header><div><h2>{t('invoiceArchive.archive')}</h2><p className="muted small">{t('invoiceArchive.documentsCount', { count: data?.total || 0 })}</p></div><div className="actions">
        <span className="muted small">{t('invoiceArchive.selectedCount', { count: selected.size })}</span><button className="primary" data-testid="invoice-batch-download" disabled={working || loading || !selected.size} onClick={() => download([...selected])}><Download size={15} />{t('invoiceArchive.accountantExport')}</button>
      </div></header><p className="muted small">{t('invoiceArchive.exportHelp')}</p>
      <div className="table-scroll"><table data-testid="invoice-table"><thead><tr>
        <th><input aria-label={t('invoiceArchive.selectPage')} data-testid="invoice-select-page" type="checkbox" disabled={working || loading || !rows.length} checked={rows.length > 0 && rows.every(row => selected.has(row.id))} onChange={event => setSelected(event.target.checked ? new Set(rows.map(row => row.id)) : new Set())} /></th>
        {['invoice_number', applied.direction === 'purchase' ? 'supplier' : 'source', 'invoice_date', 'due_date'].map(key => <th key={key}>{t(`invoiceArchive.${key}`)}</th>)}
        <th className="amount">{t('invoiceArchive.total_amount')}</th><th className="amount">{t('invoiceArchive.outstanding')}</th><th>{t('invoiceArchive.paymentStatus')}</th>{applied.direction === 'purchase' && <th>{t('invoiceArchive.receiving')}</th>}<th>{t('invoiceArchive.file')}</th>
      </tr></thead><tbody>
        {loading ? <tr><td colSpan={10} className="empty" role="status">{t('common.loading')}</td></tr> : !rows.length ? <tr><td colSpan={10} className="empty"><FileText size={22} style={{ display: 'inline', marginRight: '.5rem' }} />{t('invoiceArchive.empty')}</td></tr> : rows.map(invoice => <tr key={invoice.id} data-testid={`invoice-row-${invoice.id}`}>
          <td><input type="checkbox" disabled={working} aria-label={t('invoiceArchive.selectInvoice', { number: invoiceTitle(invoice) })} checked={selected.has(invoice.id)} onChange={event => setSelected(old => { const next = new Set(old); event.target.checked ? next.add(invoice.id) : next.delete(invoice.id); return next; })} /></td>
          <td><Link to={`/invoices/${invoice.id}`}>{invoiceTitle(invoice)}</Link><span className="secondary file-name">{invoice.original_filename}</span>{invoice.document_type && invoice.document_type !== 'invoice' && <span className="badge">{t(`invoiceArchive.documentType.${invoice.document_type}`, { defaultValue: invoice.document_type })}</span>}</td>
          <td>{invoice.supplier_name || invoice.shop_name || invoice.shop_code || '—'}{invoice.customer_name && <span className="secondary">{invoice.customer_name}</span>}</td>
          <td>{invoiceDate(invoice.invoice_date, i18n.language)}</td><td>{invoiceDate(invoice.due_date, i18n.language)}</td>
          <td className="amount">{invoiceMoney(invoice.finance ? invoice.finance.gross_amount : invoice.total_with_vat ?? invoice.total_amount, invoice.currency, i18n.language)}</td>
          <td className="amount">{invoiceMoney(invoice.finance?.outstanding_amount, invoice.currency, i18n.language)}</td>
          <td><InvoicePaymentBadge invoice={invoice} /></td>{applied.direction === 'purchase' && <td><span className="badge">{t(`invoiceArchive.receivingStatus.${invoice.receiving_status}`, { defaultValue: invoice.receiving_status })}</span></td>}
          <td><button className="quiet" disabled={working} data-testid={`invoice-download-${invoice.id}`} aria-label={t('invoiceArchive.downloadInvoice', { number: invoiceTitle(invoice) })} onClick={() => download(invoice.id)}><Download size={15} /></button></td>
        </tr>)}
      </tbody></table></div>
      <footer><span className="muted small">{t('invoiceArchive.pagination', { page, pages: Math.max(1, data?.total_pages || 1) })}</span><div className="actions"><button data-testid="invoice-prev" aria-label={t('invoiceArchive.previousPage')} disabled={loading || working || page <= 1} onClick={() => apply(applied, page - 1)}><ChevronLeft size={17} /></button><button data-testid="invoice-next" aria-label={t('invoiceArchive.nextPage')} disabled={loading || working || page >= (data?.total_pages || 1)} onClick={() => apply(applied, page + 1)}><ChevronRight size={17} /></button></div></footer>
    </section>
  </div>;
}
export default InvoicesPage;
