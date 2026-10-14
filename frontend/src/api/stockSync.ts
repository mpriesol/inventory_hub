import { hubRequest } from './access';

export const STOCK_SYNC_FIELDS = [
  { key: 'interval_seconds', min: 60, max: 86400 },
  { key: 'batch_size', min: 1, max: 100 },
  { key: 'max_order_age_seconds', min: 60, max: 86400 },
] as const;
export type StockSyncField = typeof STOCK_SYNC_FIELDS[number]['key'];
export type StockSyncValues = Record<StockSyncField, number>;
export interface StockSyncWarehouseSettings extends StockSyncValues { revision: number }
export type StockSyncProtocol = 'delta_v1' | 'legacy_absolute';
export type StockSyncResolution = { outcome: 'applied' | 'not_applied'; evidence: string };
export interface StockSyncRun {
  id: string; shop_code: string; protocol?: StockSyncProtocol; trigger: string; status: string; started_at: string | null;
  completed_at: string | null; error: string | null;
  counts: { verified: number; skipped: number; failed: number; uncertain: number; pending: number };
  more_pending?: boolean;
}
export interface StockSyncItem {
  id: number; sku: string; protocol?: StockSyncProtocol; status: string; error: string | null;
  desired: { stock?: number | string; stock_increment?: string; availability: string; can_add_to_basket_yn: boolean };
  before: unknown; after: unknown; attempt_started_at: string | null; verified_at: string | null;
}
export interface StockSyncRunDetail extends StockSyncRun { items: StockSyncItem[]; items_truncated?: boolean; items_total?: number; items_offset?: number; items_limit?: number }
export interface StockSyncOptions {
  shop: { code: string; name: string }; warehouse: { code: string; name: string } | null;
  warehouse_settings: StockSyncWarehouseSettings | null;
  settings: Record<StockSyncField, number | null> & {
    revision: number; enabled: boolean; authorized: boolean; protocol: StockSyncProtocol; authority_confirmed_at: string | null;
    next_run_at: string | null; last_started_at: string | null; last_completed_at: string | null; last_error: string | null;
  };
  baseline_count: number; effective: StockSyncValues | null; blockers: string[]; runs: StockSyncRun[]; server_write_enabled: boolean;
}
export interface StockSyncUpdate extends Record<StockSyncField, number | null> {
  shop_code: string; expected_revision: number; enabled: boolean; authorized: boolean;
  hub_is_stock_authority: boolean; native_order_stock_enabled: boolean; external_stock_writers_disabled: boolean; orders_reconciled: boolean; confirmed: true;
}
const base = '/api/stock-sync';
export const getStockSyncOptions = (shop: string, signal?: AbortSignal) => hubRequest<StockSyncOptions>(`${base}/options?shop_code=${encodeURIComponent(shop)}`, undefined, signal);
export const saveStockSyncWarehouse = (body: StockSyncValues & { warehouse_code: string; expected_revision: number; confirmed: true }) => hubRequest<StockSyncWarehouseSettings>(`${base}/warehouse`, body);
export const saveStockSyncSettings = (body: StockSyncUpdate) => hubRequest<StockSyncOptions>(`${base}/configure`, body);
export const runStockSync = (shop_code: string) => hubRequest<StockSyncRunDetail>(`${base}/run`, { shop_code, confirmed: true });
export const getStockSyncRun = (id: string, signal?: AbortSignal, offset = 0, limit = 100) => hubRequest<StockSyncRunDetail>(`${base}/runs/${encodeURIComponent(id)}?offset=${offset}&limit=${limit}`, undefined, signal);
export const resolveStockSyncItem = (id: number, resolution?: StockSyncResolution) => hubRequest<StockSyncRunDetail>(`${base}/items/${id}/resolve`, { confirmed: true, external_requests_finished: true, ...resolution });
export interface StockSyncBaseline { shop_code: string; baseline_count: number; skus: string[] }
export const establishStockSyncBaseline = (body: { shop_code: string; hold_id: string; skus: string[]; confirmed: true; external_writers_paused: true; orders_reconciled: true }) => hubRequest<StockSyncBaseline>(`${base}/baseline`, body);
