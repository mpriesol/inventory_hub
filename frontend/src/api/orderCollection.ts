import { hubRequest } from './access';
import type { EffectiveStockSettings } from './stockSettings';

export interface OrderCollectionStatus {
  shop: { code: string; name: string };
  policy: { starts_at: string; warehouse_code: string } | null;
  collector: { enabled: boolean; revision: number; cursor_at: string | null; last_reconciled_at: string | null;
    next_poll_at: string | null; last_started_at: string | null; last_completed_at: string | null; last_error: string | null; entries_seen: number; manual_pending?: boolean } | null;
  connection_configured: boolean; connection_matches?: boolean; external_write_enabled: false;
  effective?: EffectiveStockSettings | null;
  configuration_error?: string | null;
}
export interface OrderInboxEntry {
  id: number; order_number: string; source_uuid: string | null; created_at: string | null; updated_at: string | null;
  deleted: boolean; origin: string; status_id: number | null; observed_at: string; last_seen_at: string;
  review_reason: string | null; stock_state: string | null; stock_issued_at: string | null; stock_updated_at: string | null;
}
export interface OrderInbox { entries: OrderInboxEntry[]; total: number; limit: number; offset: number }
export interface OrderProcessingJob {
  id: number; source_uuid: string; order_number: string; generation: number;
  status: 'pending' | 'running' | 'completed' | 'retry' | 'review'; next_attempt_at: string | null;
  last_started_at: string | null; last_completed_at: string | null; last_checked_at: string | null;
  attempts: number; error: string | null; result: { action?: string; stock_state?: string } | null; audit_preview_id: string | null;
}
export interface OrderProcessingJobs { jobs: OrderProcessingJob[]; total: number; limit: number; offset: number }
export interface CollectionRun {
  id: number; mode: 'delta' | 'reconcile'; status: 'running' | 'completed' | 'failed'; started_at: string; completed_at: string | null;
  from_at: string; until_at: string; pages: number; observed_count: number; error: string | null;
  trigger?: 'manual' | 'automatic';
}
export interface CollectionStockPreview {
  shop_code: string; warehouse: { id: number; code: string; name: string }; captured_at: string; external_write_enabled: false;
  rows: { sku: string; product_id: number | null; target: { parent_code: string; variant_code: string | null; code: string } | null;
    quantity_known: boolean; qty_on_hand: string | null; qty_reserved: string | null; qty_available: string | null; errors: string[] }[];
}
const base = '/api/order-collection';
export const getCollectionStatus = (shop: string, signal?: AbortSignal) => hubRequest<OrderCollectionStatus>(`${base}/status?shop_code=${encodeURIComponent(shop)}`, undefined, signal);
export const configureCollection = (body: { shop_code: string; enabled: boolean; expected_revision: number | null; confirmed: true }) => hubRequest<OrderCollectionStatus>(`${base}/configure`, body);
export const refreshCollection = (body: { shop_code: string; expected_revision: number | null; confirmed: true }) => hubRequest<OrderCollectionStatus>(`${base}/refresh`, body);
export const getOrderInbox = (shop: string, offset = 0, signal?: AbortSignal) => hubRequest<OrderInbox>(`${base}/inbox?shop_code=${encodeURIComponent(shop)}&limit=50&offset=${offset}`, undefined, signal);
export const getCollectionRuns = (shop: string, signal?: AbortSignal) => hubRequest<{ runs: CollectionRun[] }>(`${base}/runs?shop_code=${encodeURIComponent(shop)}`, undefined, signal);
export const getOrderProcessingJobs = (shop: string, offset = 0, signal?: AbortSignal) => hubRequest<OrderProcessingJobs>(`/api/order-processing/jobs?shop_code=${encodeURIComponent(shop)}&limit=50&offset=${offset}`, undefined, signal);
export const previewCollectionStock = (shop: string, skus: string[], signal?: AbortSignal) => hubRequest<CollectionStockPreview>(`${base}/stock-preview`, { shop_code: shop, skus }, signal);
