import { hubRequest } from './access';

export interface IdentityCard { id: number; sku: string; name: string; eans: string[]; attributes: { name: string; value: string }[] }
export interface SupplierLinkSource {
  id: number; supplier_id: number; supplier_code: string; supplier_name: string; supplier_sku: string;
  shop_sku: string; name: string; eans: string[]; attributes: { name: string; value: string }[];
  group_code: string | null; active: boolean; last_seen_at: string | null; match_reasons?: string[]; warnings?: string[];
}
export interface SupplierLinkOptions {
  product: IdentityCard; suppliers: { id: number; code: string; name: string }[];
  current_links: (SupplierLinkSource & { link_id: number; is_active: boolean; orderable: boolean; is_primary: boolean; priority: number })[];
}
export interface SupplierLinkPreviewRequest { request_id: string; product_id: number; supplier_product_id: number }
export interface SupplierLinkApplyRequest { preview_hash: string; confirmed: true }
export interface SupplierLinkPreview {
  id: string; status: 'prepared' | 'completed'; preview_hash: string; expires_at: string;
  product: IdentityCard; source: SupplierLinkSource; link_status: 'ready' | 'already_linked' | 'conflict';
  warnings: string[]; errors: string[]; planned_changes: string[];
  result: null | { status: 'linked' | 'already_linked'; product_id: number; supplier_product_id: number; changes: string[] };
}
const base = '/api/supplier-card-links';
export const supplierLinkOptions = (productId: number) => hubRequest<SupplierLinkOptions>(`${base}/options?product_id=${productId}`);
export const supplierLinkCandidates = (product_id: number, supplier_code: string, query: string) =>
  hubRequest<{ product: IdentityCard; rows: SupplierLinkSource[]; truncated: boolean; warnings?: string[] }>(`${base}/candidates`, { product_id, supplier_code, query });
export const previewSupplierLink = (body: SupplierLinkPreviewRequest) => hubRequest<SupplierLinkPreview>(`${base}/preview`, body);
export const readSupplierLink = (id: string) => hubRequest<SupplierLinkPreview>(`${base}/${encodeURIComponent(id)}`);
export const applySupplierLink = (id: string, body: SupplierLinkApplyRequest) => hubRequest<SupplierLinkPreview>(`${base}/${encodeURIComponent(id)}/apply`, body);

export const SUPPLIER_LINK_PENDING_KEY = 'supplier-card-link-pending-v1';
export interface PendingSupplierLink { version: 1; request: SupplierLinkPreviewRequest; apply?: SupplierLinkApplyRequest }
export function pendingSupplierLink(): PendingSupplierLink | null {
  try {
    const value = JSON.parse(window.sessionStorage.getItem(SUPPLIER_LINK_PENDING_KEY) || 'null');
    if (!value || value.version !== 1 || !/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(value.request?.request_id) ||
        ![value.request.product_id, value.request.supplier_product_id].every(id => Number.isSafeInteger(id) && id > 0) ||
        value.apply && (!/^[0-9a-f]{64}$/.test(value.apply.preview_hash) || value.apply.confirmed !== true)) return null;
    return value;
  } catch { return null; }
}
export function rememberSupplierLink(value: PendingSupplierLink) { window.sessionStorage.setItem(SUPPLIER_LINK_PENDING_KEY, JSON.stringify(value)); }
export function forgetSupplierLink(id: string) {
  if (pendingSupplierLink()?.request.request_id === id) window.sessionStorage.removeItem(SUPPLIER_LINK_PENDING_KEY);
}
