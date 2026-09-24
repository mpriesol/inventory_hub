import { hubRequest } from './access';

export const FIFO_COST_FIELDS = [
  { key: 'interval_seconds', min: 60, max: 86400 },
  { key: 'batch_size', min: 1, max: 100 },
] as const;
export type FifoCostField = typeof FIFO_COST_FIELDS[number]['key'];
export type FifoCostValues = Record<FifoCostField, number>;
export interface FifoCostWarehouse { code: string; name: string }
export interface FifoCostWarehouseSettings extends FifoCostValues { warehouse_code: string; revision: number }
export interface FifoCostAllocation {
  id: number; layer_id: number; quantity: string; unit_cost_at_issue: string | null; total_cost_at_issue: string | null; unit_cost_current: string | null; total_cost_current: string | null;
}
export interface FifoCostLine {
  sku: string; product_id: number; line_key: string; quantity: string; unit_cost: string; total_cost: string; allocations: FifoCostAllocation[];
}
export interface FifoCostSource {
  lines?: FifoCostLine[]; sku?: string; unit_cost?: string; total_cost?: string; currency?: string; warehouse_code?: string;
  order_number?: string; [key: string]: unknown;
}
export interface FifoCostPublication {
  id: string; kind: 'product' | 'order'; status: 'prepared' | 'queued' | 'sending' | 'verified' | 'uncertain' | 'failed' | 'skipped' | 'resolved';
  remote_costs: { prices_with_vat_yn: boolean; lines: { line_key: string; code: string; before: string | null; desired: string }[]; product?: { before: string | null; desired: string } } | null;
  subject: string; source: FifoCostSource; before: unknown; after: unknown; error: string | null;
  created_at: string; expires_at: string | null; attempt_started_at: string | null; verified_at: string | null; resolution: unknown;
}
export interface FifoCostOptions {
  shop: { code: string; name: string }; warehouse: FifoCostWarehouse | null; warehouses: FifoCostWarehouse[];
  warehouse_settings: FifoCostWarehouseSettings | null;
  settings: Record<FifoCostField, number | null> & {
    revision: number; enabled: boolean; product_cost_enabled: boolean; order_cost_enabled: boolean; warehouse_code: string | null;
    orders_since: string | null; next_run_at: string | null; retry_after_at: string | null; last_completed_at: string | null; last_error: string | null; scan_active: boolean;
  };
  effective: FifoCostValues | null; server_write_enabled: boolean; blockers: string[]; publications: FifoCostPublication[];
}
export interface FifoCostConfiguration extends Record<FifoCostField, number | null> {
  shop_code: string; warehouse_code: string; expected_revision: number; enabled: boolean; product_cost_enabled: boolean; order_cost_enabled: boolean; confirmed: true;
}
export interface FifoCostHistory { items: FifoCostPublication[]; total: number; offset: number; limit: number }
const base = '/api/fifo-cost-sync';
export const getFifoCostOptions = (shop: string, signal?: AbortSignal) => hubRequest<FifoCostOptions>(`${base}/options?shop_code=${encodeURIComponent(shop)}`, undefined, signal);
export const saveFifoCostWarehouse = (body: FifoCostValues & { warehouse_code: string; expected_revision: number; confirmed: true }) => hubRequest<FifoCostWarehouseSettings>(`${base}/warehouse`, body);
export const saveFifoCostSettings = (body: FifoCostConfiguration) => hubRequest<FifoCostOptions>(`${base}/configure`, body);
export const runFifoCostSync = (shop_code: string) => hubRequest<FifoCostOptions>(`${base}/run`, { shop_code, confirmed: true });
export const getFifoCostHistory = (shop: string, offset = 0, signal?: AbortSignal) => hubRequest<FifoCostHistory>(`${base}/history?shop_code=${encodeURIComponent(shop)}&offset=${offset}&limit=50`, undefined, signal);
export const previewFifoOrderCost = (shop_code: string, order_number: string, request_id: string) => hubRequest<FifoCostPublication>(`${base}/orders/preview`, { shop_code, order_number, request_id, confirmed: true });
export const getFifoCostPublication = (id: string, signal?: AbortSignal) => hubRequest<FifoCostPublication>(`${base}/publications/${encodeURIComponent(id)}`, undefined, signal);
export const sendFifoCostPublication = (id: string) => hubRequest<FifoCostPublication>(`${base}/publications/${encodeURIComponent(id)}/send`, { confirmed: true });
export const resolveFifoCostPublication = (id: string, note: string) => hubRequest<FifoCostPublication>(`${base}/publications/${encodeURIComponent(id)}/resolve`, { confirmed: true, original_request_settled: true, note });
