import { hubRequest } from './access';

export type SupplierObservationStatus = 'unconfigured' | 'not_loaded' | 'missing_observation' | 'feed_mismatch' | 'configuration_error' | 'stale' | 'fresh' | 'unavailable_source' | 'missing_link' | 'conflict';
export interface SupplierObservation {
  available: boolean | null; fresh: boolean; quantity: string | null; quantity_kind: 'exact' | 'minimum' | 'boolean' | 'unknown';
  availability_state: 'available' | 'unavailable' | 'unknown'; label: string; source: string | null;
  supplier: string; supplier_name: string; supplier_sku: string; status: SupplierObservationStatus; reason: string | null;
  feed_key: string | null; observation_feed_key: string | null; observed_at: string | null; expires_at: string | null;
  last_success_at: string | null; last_error: string | null; refresh_state: 'idle' | 'queued' | 'running';
  action: 'configure' | 'refresh' | 'check_source' | 'wait' | 'link';
}
export interface SupplierFeedIssue { row: number; sku: string | null; stock: string | null; external_stock: string | null; code: string; kind: 'rejected' | 'unknown' }
export interface SupplierFeedRun {
  id: number; feed_key: string; status: 'running' | 'completed' | 'partial' | 'failed';
  started_at: string; finished_at: string | null; error: string | null;
  report: { total: number; accepted: number; rejected: number; unknown: number; reasons: Record<string, number>; warnings: string[] } | null;
}
export interface SupplierFeedFailures { run_id: number; total: number; offset: number; limit: number; items: SupplierFeedIssue[] }
export interface SupplierAvailabilitySettings {
  latest_run?: SupplierFeedRun | null;
  publication?: { enabled: boolean; shop_code: string; brands: string[]; counts?: Record<string, number> } | null;
  supplier: string; name: string; feed_keys: string[]; feed_key: string;
  revision: number; enabled: boolean; interval_seconds: number; freshness_seconds: number;
  min_coverage_percent: number; last_started_at: string | null; last_success_at: string | null;
  next_run_at: string | null; last_error: string | null; manual_requested_at: string | null;
  last_item_count: number | null; running: boolean; availability: { orderable: string; unknown: string };
  status?: SupplierObservationStatus; configuration_error?: string | null;
  manual_only?: boolean; source_kind?: 'feed' | 'b2b'; refresh_scope?: 'supplier_catalog' | 'hub_products';
  refresh_progress?: { total: number; processed: number; succeeded: number; failed: number; failures: { sku: string; code: string }[]; status: 'running' | 'completed' | 'failed' } | null;
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

export const getSupplierObservation = (supplier: string, sku: string, signal?: AbortSignal) =>
  hubRequest<SupplierObservation>(`${base}/${encodeURIComponent(supplier)}/observation?${new URLSearchParams({ sku })}`, undefined, signal);

export const getSupplierFeedFailures = (supplier: string, runId: number, offset = 0, signal?: AbortSignal) =>
  hubRequest<SupplierFeedFailures>(`${base}/${encodeURIComponent(supplier)}/runs/${runId}/failures?offset=${offset}&limit=50`, undefined, signal);
