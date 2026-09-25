import { hubRequest } from './access';

export interface SupplierAvailabilitySettings {
  supplier: string; name: string; feed_keys: string[]; feed_key: string;
  revision: number; enabled: boolean; interval_seconds: number; freshness_seconds: number;
  min_coverage_percent: number; last_started_at: string | null; last_success_at: string | null;
  next_run_at: string | null; last_error: string | null; manual_requested_at: string | null;
  last_item_count: number | null; running: boolean; availability: { orderable: string; unknown: string };
}
export interface SupplierAvailabilityUpdate {
  expected_revision: number; enabled: boolean; feed_key: string; interval_seconds: number;
  freshness_seconds: number; min_coverage_percent: number;
}
export interface SupplierLinkIssue { product_id: number; sku: string; supplier_sku: string | null; reason: string }
export interface SupplierLinkReconciliation {
  supplier: string; scanned: number; linked: number; existing: number; skipped: number;
  conflicts: SupplierLinkIssue[]; skipped_details?: SupplierLinkIssue[]; next_after_product_id: number | null;
}
const base = '/api/supplier-availability';
export const getSupplierAvailability = (signal?: AbortSignal) => hubRequest<{ suppliers: SupplierAvailabilitySettings[] }>(base, undefined, signal);
export const saveSupplierAvailability = (supplier: string, body: SupplierAvailabilityUpdate) => hubRequest<SupplierAvailabilitySettings>(`${base}/${encodeURIComponent(supplier)}`, body, undefined, 'PUT');
export const runSupplierAvailability = (supplier: string, expected_revision: number) => hubRequest<{ supplier: string; queued: boolean }>(`${base}/${encodeURIComponent(supplier)}/run`, { expected_revision });
export const reconcileSupplierLinks = (supplier: string, after_product_id: number, signal?: AbortSignal) =>
  hubRequest<SupplierLinkReconciliation>(`${base}/${encodeURIComponent(supplier)}/links/reconcile`, { after_product_id, limit: 500 }, signal);
