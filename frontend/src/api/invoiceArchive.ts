import { hubAccessToken, hubRequest, hubUpload } from './access';
import { hubFetchInit } from './transport';
import { CatalogApiError } from './catalog';

export type InvoiceAmount = string | number;
export type InvoiceDirection = 'purchase' | 'sale';
export type InvoicePaymentStatus = 'unknown' | 'unpaid' | 'partial' | 'paid';
export interface InvoicePayment {
  id: number; kind: 'opening' | 'payment' | 'refund' | 'reversal'; amount: string; currency: string;
  paid_at: string; note: string | null; actor: string | null;
  reverses_payment_id: number | null; reversed_by_id: number | null;
}
export interface InvoiceFinance {
  payment_revision: number; currency: string | null; gross_amount: string | null;
  paid_amount: string | null; outstanding_amount: string | null; status: InvoicePaymentStatus;
  is_overdue: boolean; opening_required: boolean; payments: InvoicePayment[];
}
export interface ArchivedInvoice {
  id: number; direction: InvoiceDirection; document_type?: string; source_kind?: 'upload' | 'supplier' | 'shop';
  supplier_id: number | null; supplier_code: string | null; supplier_name: string | null;
  shop_code?: string | null; shop_name?: string | null; customer_name?: string | null;
  original_filename: string; file_type: string; invoice_number: string | null;
  invoice_date: string | null; due_date: string | null; currency: string | null;
  total_amount: InvoiceAmount | null; total_with_vat: InvoiceAmount | null;
  total_without_vat?: InvoiceAmount | null; vat_amount: InvoiceAmount | null;
  vat_rate: InvoiceAmount | null; vat_included: boolean; items_count: number;
  payment_status: InvoicePaymentStatus; receiving_status: string; is_overdue: boolean;
  days_until_due: number | null; is_parsed: boolean; created_at: string;
  finance?: InvoiceFinance;
}
export interface ArchivedInvoiceLine {
  id: number; line_number: number; ean: string | null; supplier_sku: string | null;
  product_name: string | null; quantity: InvoiceAmount | null; unit: string | null;
  unit_price: InvoiceAmount | null; total_price: InvoiceAmount | null; vat_rate: InvoiceAmount | null;
  unit_price_with_vat: InvoiceAmount | null; total_price_with_vat: InvoiceAmount | null;
  matched_product_id: number | null; product_sku: string | null; matched_product_name: string | null;
  is_new_product: boolean;
}
export interface ArchivedInvoiceDetail extends ArchivedInvoice {
  payment_revision: number;
  paid_at: string | null; paid_amount: InvoiceAmount | null; parse_error: string | null;
  receiving_session_id: number | null; receiving_invoice_number: string | null;
  notes: string | null; updated_at: string | null; lines: ArchivedInvoiceLine[];
}
export interface InvoiceArchiveFilters {
  direction: InvoiceDirection; f_invoice_number: string; f_supplier: string;
  date_from: string; date_to: string; due_from: string; due_to: string;
  f_amount_min: string; f_amount_max: string; f_currency: string;
  payment_status: string; receiving_status: string; is_overdue: string;
  sort_by: string; sort_order: 'asc' | 'desc';
}
export interface InvoiceArchivePage {
  items: ArchivedInvoice[]; total: number; page: number; page_size: number; total_pages: number;
}
export interface InvoiceCurrencyTotals {
  currency: string | null; invoice_count: number; total_amount: string | null;
  outstanding_amount: string | null; unpaid_amount: string | null; overdue_amount: string | null;
  unpaid_count: number; overdue_count: number; unknown_amount_count: number; unknown_outstanding_count?: number;
}
export interface InvoiceArchiveStats {
  direction: InvoiceDirection; total_invoices: number; unpaid_count: number; overdue_count: number;
  totals_by_currency: InvoiceCurrencyTotals[];
  monthly: { month: string; currency: string | null; total_amount: string | null; invoice_count: number; unknown_amount_count: number }[];
  aging: { bucket: 'not_due' | '1_30' | '31_60' | '61_90' | 'over_90' | 'unknown_due'; currency: string | null; outstanding_amount: string; invoice_count: number }[];
}
export const invoiceArchiveDefaults: InvoiceArchiveFilters = {
  direction: 'purchase', f_invoice_number: '', f_supplier: '', date_from: '', date_to: '',
  due_from: '', due_to: '', f_amount_min: '', f_amount_max: '', f_currency: '',
  payment_status: '', receiving_status: '', is_overdue: '', sort_by: 'invoice_date', sort_order: 'desc',
};
const base = '/api/invoices';
export function invoiceArchiveQuery(values: Partial<InvoiceArchiveFilters> & { page?: number; page_size?: number }) {
  const query = new URLSearchParams();
  Object.entries(values).forEach(([key, value]) => { if (value !== '' && value !== undefined) query.set(key, String(value)); });
  return query.toString();
}
export const getInvoiceArchive = (filters: InvoiceArchiveFilters, page: number, signal?: AbortSignal) =>
  hubRequest<InvoiceArchivePage>(`${base}?${invoiceArchiveQuery({ ...filters, page, page_size: 25 })}`, undefined, signal);
export const getArchivedInvoice = (id: number, signal?: AbortSignal) => hubRequest<ArchivedInvoiceDetail>(`${base}/${id}`, undefined, signal);
export const getInvoiceArchiveStats = (direction: InvoiceDirection, signal?: AbortSignal) => hubRequest<InvoiceArchiveStats>(`${base}/stats?direction=${direction}`, undefined, signal);
export const getArchiveShops = (signal?: AbortSignal) => hubRequest<{ shops: { code: string; name: string }[] }>('/api/product-editor/options', undefined, signal);
export const uploadArchivedInvoice = (body: FormData) => hubUpload<{ id: number; invoice_number: string | null }>(`${base}/upload`, body);
export async function getArchiveSuppliers(signal?: AbortSignal): Promise<{ code: string; name: string }[]> {
  const value = await hubRequest<{ code: string; name: string }[] | { suppliers?: { code: string; name: string }[]; items?: { code: string; name: string }[] }>('/api/suppliers', undefined, signal);
  return Array.isArray(value) ? value : value.suppliers || value.items || [];
}
export interface InvoiceMetadataChange {
  expected_revision: number; invoice_number?: string | null; invoice_date?: string | null; due_date?: string | null;
  total_amount?: string | null; vat_rate?: string | null; vat_included?: boolean; notes?: string | null; currency?: string | null;
}
async function archiveMutation(path: string, method: string, body?: unknown) {
  const response = await fetch(path, hubFetchInit(path, {
    method, headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  }, hubAccessToken()));
  const result = await response.json();
  if (!response.ok) throw Object.assign(new CatalogApiError(result?.detail?.code || 'request_failed', typeof result?.detail === 'string' ? result.detail : result?.detail?.message || String(response.status)), { status: response.status, detail: result?.detail, knownUnapplied: result?.detail?.known_unapplied === true });
  return result;
}
export const updateArchivedInvoice = (id: number, body: InvoiceMetadataChange) => archiveMutation(`${base}/${id}`, 'PATCH', body);
export const deleteArchivedInvoice = (id: number) => archiveMutation(`${base}/${id}?delete_file=true`, 'DELETE');
export interface InvoicePaymentRequest {
  request_id: string; expected_revision: number; kind: 'payment' | 'refund'; amount: string;
  paid_at: string; note: string; opening_paid_amount?: string;
}
export interface InvoiceReversalRequest { request_id: string; expected_revision: number; note: string }
export interface InvoicePaymentResult { invoice_id: number; request_id: string; payment_revision: number; finance: InvoiceFinance }
export const addInvoicePayment = (id: number, body: InvoicePaymentRequest) => hubRequest<InvoicePaymentResult>(`${base}/${id}/payments`, body);
export const reverseInvoicePayment = (id: number, paymentId: number, body: InvoiceReversalRequest) => hubRequest<InvoicePaymentResult>(`${base}/${id}/payments/${paymentId}/reverse`, body);
export const getInvoicePaymentOperation = (id: number, requestId: string) => hubRequest<InvoicePaymentResult>(`${base}/${id}/payment-operations/${encodeURIComponent(requestId)}`);

/** Download through the same credential/session boundary as JSON calls. */
export async function downloadInvoiceArchive(ids: number[] | number): Promise<void> {
  const batch = Array.isArray(ids), path = batch ? `${base}/export` : `${base}/${ids}/download`;
  const response = await fetch(path, hubFetchInit(path, batch ? {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ invoice_ids: ids }),
  } : undefined, hubAccessToken()));
  if (!response.ok) {
    const value = await response.json().catch(() => null);
    throw Object.assign(new CatalogApiError(value?.detail?.code || 'invoice_download_failed', value?.detail?.message || String(response.status)), { status: response.status });
  }
  const url = URL.createObjectURL(await response.blob());
  const disposition = response.headers.get('Content-Disposition') || '';
  const name = /filename="([^"\r\n]+)"/.exec(disposition)?.[1];
  const link = document.createElement('a'); link.href = url;
  link.download = name?.replace(/[\\/]/g, '_') || (batch ? 'invoice-archive.zip' : `invoice-${ids}`);
  document.body.append(link); link.click(); link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export interface ShopInvoiceSyncRequest { date_from: string; date_to: string; page: number; offset: number; page_token?: string | null }
export interface ShopInvoiceSyncResult {
  imported: number; unchanged: number; failed: number;
  items: { invoice_number: string; status: string; id?: number; error?: string }[];
  next: { page: number; offset: number; page_token?: string | null } | null; total_in_source: number; source_pages: number;
  stopped_error?: string | null; retry_after?: number | null;
}
export const syncShopInvoiceArchive = (shop: string, body: ShopInvoiceSyncRequest) =>
  hubRequest<ShopInvoiceSyncResult>(`${base}/sync/shop/${encodeURIComponent(shop)}`, body);
export interface SupplierInvoiceReconcileResult {
  supplier_code: string; scanned: number; created: number; existing: number;
  conflicts: { source_key: string; code: string }[]; next_after_key: string | null;
}
export const reconcileSupplierInvoiceArchive = (supplier: string, after: string | null = null) =>
  hubRequest<SupplierInvoiceReconcileResult>(`${base}/reconcile`, { supplier_code: supplier, after_key: after, limit: 100 });
