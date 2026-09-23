import { hubRequest } from './access';

export interface PublicationHold {
  id: string; warehouse_id: number; shop_code: string; active: boolean;
  assertions: Record<string, boolean | string>; created_at: string; closed_at: string | null;
}
export interface PublicationOptions {
  shop: { code: string; name: string }; warehouse: { id: number; code: string; name: string };
  policy: { revision: number; enabled: boolean; target_fingerprint: string | null };
  hold: PublicationHold | null; server_write_enabled: boolean; external_write_enabled: boolean;
  values: { publication_batch_size: number; publication_preview_minutes: number };
}
export interface PublicationItem {
  id: number; position: number; sku: string; target: Record<string, unknown> | null;
  quantity: string | null; before_quantity: string | null; after_quantity: string | null; status: string;
  attempt_id: string | null; attempt_started_at: string | null; attempt_completed_at: string | null;
  verified_at: string | null; error: string | null; observation: unknown; acknowledgement: unknown; resolution: unknown;
}
export interface PublicationRow {
  sku: string; product_id: number | null; target: { code?: string; parent_code: string; variant_code: string | null } | null;
  quantity_known: boolean; qty_on_hand: string | null; qty_reserved: string | null; qty_available: string | null; errors: string[];
  remote: { identity: { code: string; parent_code: string; variant_code: string | null; product_id: number; variant_id: number | null }; quantity: string | null } | null;
}
export interface PublicationBatch {
  id: string; shop_code: string; hold_id: string; status: string; preview_hash: string;
  created_at: string; expires_at: string; queued_at: string | null; started_at: string | null; completed_at: string | null;
  error: string | null; result: unknown; preview_data: { rows: PublicationRow[]; warehouse: { id: number; code: string; name: string }; [key: string]: unknown }; items: PublicationItem[];
}
export interface PublicationBatches {
  batches: Omit<PublicationBatch, 'items' | 'preview_data'>[]; total: number; limit: number; offset: number;
}
const base = '/api/stock-publication';
export const getPublicationOptions = (shop: string, signal?: AbortSignal) => hubRequest<PublicationOptions>(`${base}/options?shop_code=${encodeURIComponent(shop)}`, undefined, signal);
export const configurePublication = (body: { shop_code: string; expected_revision: number; enabled: boolean; confirmed: true }) => hubRequest<PublicationOptions>(`${base}/configure`, body);
export const createPublicationHold = (shop: string) => hubRequest<PublicationHold>(`${base}/holds`, { shop_code: shop, confirmed: true, external_writers_paused: true, orders_reconciled: true });
export const releasePublicationHold = (id: string) => hubRequest<PublicationHold>(`${base}/holds/${encodeURIComponent(id)}/release`, { confirmed: true, maintenance_completed: true });
export const previewPublication = (body: { request_id: string; shop_code: string; skus: string[] }, signal?: AbortSignal) => hubRequest<PublicationBatch>(`${base}/preview`, body, signal);
export const getPublicationBatch = (id: string, signal?: AbortSignal) => hubRequest<PublicationBatch>(`${base}/batches/${encodeURIComponent(id)}`, undefined, signal);
export const getPublicationBatches = (shop: string, offset = 0, signal?: AbortSignal) => hubRequest<PublicationBatches>(`${base}/batches?shop_code=${encodeURIComponent(shop)}&limit=50&offset=${offset}`, undefined, signal);
export const submitPublication = (id: string, previewHash: string) => hubRequest<PublicationBatch>(`${base}/batches/${encodeURIComponent(id)}/submit`, { preview_hash: previewHash, confirmed: true });
export const cancelPublication = (id: string) => hubRequest<PublicationBatch>(`${base}/batches/${encodeURIComponent(id)}/cancel`, { confirmed: true });
export const verifyPublication = (id: string) => hubRequest<PublicationBatch>(`${base}/batches/${encodeURIComponent(id)}/verify`, { confirmed: true });
export const resolvePublication = (id: string) => hubRequest<PublicationBatch>(`${base}/batches/${encodeURIComponent(id)}/resolve`, { confirmed: true, external_requests_finished: true });
