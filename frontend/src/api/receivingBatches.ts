import { API_BASE, fetchJSON } from './client';
import type { ReceivingScanCommand, ScanInvoiceChoice } from '../hooks/useReceivingScanner';

export interface ReceivingBatchCandidate {
  supplier: string; supplier_name: string; invoice_no: string; invoice_date: string | null;
  session_id: number | string | null; status: string; blocked?: boolean; blocked_reason?: string | null;
  lines: { id: number | null; line_number: number | null; title: string; ordered_qty: string;
    received_qty: string; remaining_qty: string; unit_price: string | null }[];
}
export interface ReceivingBatchScan {
  id: string; code: string; qty: string; supplier: string; supplier_name: string;
  invoice_no: string; invoice_date: string | null; session_id: number | string; line_id: number;
  line_number: number | null; title: string; product_id: number | null; sku: string | null;
  unit_price: string | null; status: 'active' | 'cancelled'; alternatives: ReceivingBatchCandidate[];
}
export interface ReceivingBatchItem {
  session_id: number | string; supplier: string; invoice_no: string; line_id: number;
  title: string; qty: string; product_id: number | null; sku: string | null;
  card_status: string; can_prepare: boolean; draft_id: string | null; warnings: string[];
  stock_qty_before?: string | null; stock_qty_after?: string | null; stock_quantity?: string;
  purchase_unit?: string | null; purchase_to_stock_factor?: string; unit_price?: string | null; value?: string | null;
  available_before?: string | null; available_after?: string | null;
  stock_unit?: string; stock_confirmed?: boolean;
  shops: { code: string; name: string; linked: boolean; sync_enabled: boolean; status: string; blockers?: string[] }[];
}
export interface ReceivingBatchSummary {
  id: string; status: 'open' | 'completed'; warehouse_code: string; warehouse_name: string;
  created_at: string; completed_at: string | null; scans: ReceivingBatchScan[]; items: ReceivingBatchItem[];
  invoices: { supplier: string; invoice_no: string; session_id: number | string; invoice_date: string | null;
    ordered_qty: string; received_qty: string; scanned_qty: string; unposted_legacy_qty?: string; complete_after: boolean }[];
  total_qty: string; blockers: { code: string; line_id?: number; session_id?: number | string }[];
  preview_hash?: string; result?: Record<string, unknown> | null;
}
export interface ReceivingBatchListItem {
  id: string; status: string; warehouse_code: string; created_at: string; scan_count: number; total_qty: string;
}
export interface ReceivingBatchScanResult { request_id: string; scan_id: string; replayed: boolean; summary: ReceivingBatchSummary }
export interface ReceivingBatchFinalizeResult {
  request_id: string; batch_id: string; status: 'completed'; result: Record<string, unknown>; summary: ReceivingBatchSummary;
}
const base = `${API_BASE}/receiving/batches`;
const path = (id: string) => `${base}/${encodeURIComponent(id)}`;
const post = <T,>(url: string, body: unknown) => fetchJSON<T>(url, {
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
});
const record = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value);
const decimal = (value: unknown) => typeof value === 'string' && /^-?\d+(?:\.\d+)?$/.test(value);
const validSummary = (value: unknown): value is ReceivingBatchSummary => {
  const row = value as ReceivingBatchSummary;
  return !!row && typeof row.id === 'string' && ['open', 'completed'].includes(row.status)
    && Array.isArray(row.scans) && Array.isArray(row.items) && Array.isArray(row.invoices)
    && Array.isArray(row.blockers) && decimal(row.total_qty)
    && row.scans.every(scan => record(scan) && typeof scan.id === 'string' && typeof scan.code === 'string'
      && decimal(scan.qty) && typeof scan.title === 'string' && typeof scan.supplier === 'string'
      && typeof scan.invoice_no === 'string' && ['active', 'cancelled'].includes(scan.status) && Array.isArray(scan.alternatives))
    && row.items.every(item => record(item) && typeof item.title === 'string' && decimal(item.qty)
      && Array.isArray(item.warnings) && Array.isArray(item.shops)
      && item.shops.every(shop => record(shop) && typeof shop.code === 'string' && typeof shop.linked === 'boolean' && typeof shop.status === 'string'))
    && row.invoices.every(invoice => record(invoice) && typeof invoice.invoice_no === 'string'
      && decimal(invoice.ordered_qty) && decimal(invoice.received_qty) && decimal(invoice.scanned_qty));
};
function summaryResult(value: unknown, id?: string): ReceivingBatchSummary {
  if (!validSummary(value) || (id && value.id !== id)) throw new Error('receiving_batch_response_invalid');
  return value;
}
export async function createReceivingBatch(request_id: string, warehouse_code?: string) {
  return summaryResult(await post<unknown>(base, { request_id, ...(warehouse_code ? { warehouse_code } : {}) }));
}
export const listReceivingBatches = () => fetchJSON<{ batches: ReceivingBatchListItem[] }>(`${base}?status=open`);
export async function getReceivingBatch(id: string) { return summaryResult(await fetchJSON<unknown>(path(id)), id); }
export async function previewReceivingBatch(id: string) {
  const value = summaryResult(await post<unknown>(`${path(id)}/preview`, {}), id);
  if (typeof value.preview_hash !== 'string' || !value.preview_hash) throw new Error('receiving_batch_response_invalid');
  return value;
}
function scanResult(value: ReceivingBatchScanResult, id: string, requestId: string, scanId: string) {
  if (value?.request_id !== requestId || typeof value.replayed !== 'boolean' || value.scan_id !== scanId) {
    throw new Error('receiving_batch_response_invalid');
  }
  summaryResult(value.summary, id); return value;
}
export async function scanReceivingBatch(id: string, command: ReceivingScanCommand) {
  return scanResult(await post<ReceivingBatchScanResult>(`${path(id)}/scan`, { ...command, qty: String(command.qty) }), id, command.request_id, command.request_id);
}
export interface ReceivingBatchMutation { request_id: string; action: 'cancel' | 'reassign'; scan_id: string; selection?: ScanInvoiceChoice }
export async function mutateReceivingBatchScan(id: string, command: ReceivingBatchMutation) {
  return scanResult(await post<ReceivingBatchScanResult>(`${path(id)}/scans/${encodeURIComponent(command.scan_id)}/${command.action}`,
    { request_id: command.request_id, ...command.selection }), id, command.request_id, command.scan_id);
}
export interface ReceivingBatchFinalizeCommand { request_id: string; preview_hash: string; confirmed: true }
export async function finalizeReceivingBatch(id: string, command: ReceivingBatchFinalizeCommand) {
  const value = await post<ReceivingBatchFinalizeResult>(`${path(id)}/finalize`, command);
  if (value?.request_id !== command.request_id || value.batch_id !== id || value.status !== 'completed' || value.summary?.status !== 'completed') throw new Error('receiving_batch_response_invalid');
  summaryResult(value.summary, id); return value;
}
