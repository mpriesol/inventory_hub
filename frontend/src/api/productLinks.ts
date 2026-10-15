import { hubRequest } from './access';

export type ProductLinkShop = 'biketrek' | 'xtrek';
export interface ProductLinkManualMatch {
  product_id: number; code: string; parent_code: string; is_variant: boolean;
}
export interface ProductLinkPreviewBody {
  request_id: string; shop_code: ProductLinkShop; product_ids: number[];
  manual_matches?: ProductLinkManualMatch[];
}
export interface ProductLinkRemote {
  code: string; parent_code: string; is_variant: boolean; external_id: string; parent_id: string | null;
  name: string; eans: string[]; attributes: { name: string; value: string }[];
  raw_barcodes?: string[]; invalid_barcodes?: string[];
}
export interface ProductLinkRow {
  product_id: number; sku: string; name: string; local_eans?: string[];
  status: 'ready' | 'already_linked' | 'not_found' | 'conflict'; match_by: string | null;
  remote: ProductLinkRemote | null; warnings: string[]; errors: string[];
}
export interface ProductLinkBatch {
  id: string; shop_code: ProductLinkShop; status: 'prepared' | 'completed'; preview_hash: string;
  expires_at: string; rows: ProductLinkRow[];
  result: null | { product_ids: number[]; linked_product_ids: number[]; already_linked_product_ids: number[] };
}
export interface ProductLinkApplyBody { preview_hash: string; product_ids: number[]; confirmed: true }
export interface ProductLinkMetadataResult {
  shop_code: ProductLinkShop; remote_requests: number;
  rows: { product_id: number; status: 'refreshed' | 'incomplete'; shop_url: string | null; shop_admin_url: string | null }[];
}
const base = '/api/product-links';
export const previewProductLinks = (body: ProductLinkPreviewBody) => hubRequest<ProductLinkBatch>(`${base}/preview`, body);
export const applyProductLinks = (id: string, body: ProductLinkApplyBody) => hubRequest<ProductLinkBatch>(`${base}/${encodeURIComponent(id)}/apply`, body);
export const readProductLinks = (id: string, signal?: AbortSignal) => hubRequest<ProductLinkBatch>(`${base}/${encodeURIComponent(id)}`, undefined, signal);
export const refreshProductLinkMetadata = (shop_code: string, product_ids: number[]) =>
  hubRequest<ProductLinkMetadataResult>(`${base}/refresh`, { shop_code, product_ids });

// A single pending batch per tab. Store only its request/identity and chosen IDs,
// never credentials, fetched product contents, prices or customer data.
export const PRODUCT_LINK_PENDING_KEY = 'product-links-pending-v1';
export interface PendingProductLink {
  version: 1; request: ProductLinkPreviewBody; apply?: ProductLinkApplyBody;
}
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
export function pendingProductLink(): PendingProductLink | null {
  try {
    const item = JSON.parse(window.sessionStorage.getItem(PRODUCT_LINK_PENDING_KEY) || 'null');
    if (!item || item.version !== 1 || !uuid.test(item.request?.request_id) ||
        !['biketrek', 'xtrek'].includes(item.request.shop_code) || !Array.isArray(item.request.product_ids) ||
        !item.request.product_ids.length || item.request.product_ids.length > 100 ||
        !item.request.product_ids.every((id: unknown) => Number.isSafeInteger(id) && Number(id) > 0)) return null;
    if (item.request.manual_matches !== undefined && (!Array.isArray(item.request.manual_matches) ||
        item.request.manual_matches.length > 100 || !item.request.manual_matches.every((match: ProductLinkManualMatch) =>
          match && item.request.product_ids.includes(match.product_id) && typeof match.code === 'string' &&
          typeof match.parent_code === 'string' && typeof match.is_variant === 'boolean'))) return null;
    if (item.apply && (typeof item.apply.preview_hash !== 'string' || item.apply.confirmed !== true ||
        !Array.isArray(item.apply.product_ids) || !item.apply.product_ids.length ||
        !item.apply.product_ids.every((id: number) => item.request.product_ids.includes(id)))) return null;
    return item;
  } catch { return null; }
}
export function rememberProductLink(item: PendingProductLink) {
  window.sessionStorage.setItem(PRODUCT_LINK_PENDING_KEY, JSON.stringify(item));
}
export function forgetProductLink(id: string) {
  if (pendingProductLink()?.request.request_id === id) window.sessionStorage.removeItem(PRODUCT_LINK_PENDING_KEY);
}
