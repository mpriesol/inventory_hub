import { hubRequest } from './access';

export interface ConsolidationBlocker {
  code: string; product_id?: number; mapping_id?: number; shop_code?: string; table?: string; count?: number;
}
export interface ConsolidationCard {
  id: number; sku: string; name: string; is_active: boolean; item_type: string; stock_unit: string;
  eans: string[]; attributes: { name: string; value: string }[]; manual_eans: string[] | null;
  canonical_product_id: number | null;
}
export interface ConsolidationMapping {
  id: number; product_id: number; shop_code: string; external_code: string | null; parent_code: string | null;
  is_variant: boolean; external_product_id: string | null; external_variant_id: string | null;
  shop_name?: string; shop_available?: boolean; is_listed?: boolean; shared_stock?: boolean;
  sale_to_stock_factor?: string | number; push_pending?: boolean;
}
export interface ConsolidationSelection {
  products: ConsolidationCard[]; mappings: ConsolidationMapping[]; blockers: ConsolidationBlocker[];
}
export interface ConsolidationTarget {
  shop_code: string; code: string; parent_code: string; is_variant: boolean;
  native_product_id: number; native_variant_id?: number | null; sale_to_stock_factor: 1;
}
export type ConsolidationActionName = 'retain' | 'bind' | 'retire_absent' | 'retire_unverified_code_absent';
export interface ConsolidationMappingAction {
  mapping_id: number; action: ConsolidationActionName; target: ConsolidationTarget | null;
}
export interface ProductConsolidationPreviewBody {
  request_id: string; keeper_product_id: number; retired_product_ids: number[];
  mode: 'identity_only_preserve_unconfirmed_history';
  same_physical_retail_unit_confirmed: true; legacy_initial_history_preserved: true;
  preserve_keeper_invoice_receiving?: boolean; use_keeper_supplier_gtin?: boolean;
  identity_evidence: { review_note: string; physical_unit: string; source_urls: string[] };
  mapping_actions: ConsolidationMappingAction[]; additional_sales: ConsolidationTarget[];
}
export interface ProductConsolidationApplyBody { preview_hash: string; confirmed: true }
export interface ConsolidationReviewCard {
  id: number; sku: string; name: string; is_active: boolean; item_type: string; stock_unit: string;
  identity: { eans: string[]; attributes: { name: string; value: string }[]; manual_eans?: string[] | null };
}
export interface ConsolidationNativeLeaf {
  code: string; parent_code: string; is_variant: boolean; external_id: string; parent_id: string | null;
  name?: string; eans: string[]; attributes: { name: string; value: string }[];
}
export interface ProductConsolidationReview {
  id: string; status: 'prepared' | 'expired' | 'completed'; preview_hash: string; expires_at: string; eligible: boolean;
  keeper_product_id: number; retired_product_ids: number[];
  identity_evidence: ProductConsolidationPreviewBody['identity_evidence'];
  products: ConsolidationReviewCard[]; existing_mappings: ConsolidationMapping[];
  plan: { actions: ConsolidationMappingAction[]; additional_sales: ConsolidationTarget[] };
  native: Record<string, { leaves: ConsolidationNativeLeaf[] }>;
  dependencies: { blockers: ConsolidationBlocker[] };
  supplier_gtin_proof?: { verified_gtin_key: string; physical_axes?: unknown };
  result: null | { keeper_product_id: number; retired_product_ids: number[]; retired_mapping_ids: number[];
    current_mappings: ConsolidationMapping[]; physical_stock_changed: false; remote_write_performed: false };
}

const base = '/api/product-consolidations';
export const readConsolidationSelection = (ids: number[], signal?: AbortSignal) => {
  const query = new URLSearchParams(); ids.forEach(id => query.append('product_ids', String(id)));
  return hubRequest<ConsolidationSelection>(`${base}/selection?${query}`, undefined, signal);
};
export const previewProductConsolidation = (body: ProductConsolidationPreviewBody) =>
  hubRequest<ProductConsolidationReview>(`${base}/preview`, body);
export const readProductConsolidation = (id: string) =>
  hubRequest<ProductConsolidationReview>(`${base}/${encodeURIComponent(id)}`);
export const applyProductConsolidation = (id: string, body: ProductConsolidationApplyBody) =>
  hubRequest<ProductConsolidationReview>(`${base}/${encodeURIComponent(id)}/apply`, body);
export const discardProductConsolidation = (id: string, body: ProductConsolidationApplyBody) =>
  hubRequest<ProductConsolidationReview>(`${base}/${encodeURIComponent(id)}/discard`, body);

// One exact operation per tab. Credentials, fetched catalog data, stock and
// prices never enter this recovery record. Keep the evidence the operator sent.
export const PRODUCT_CONSOLIDATION_PENDING_KEY = 'product-consolidation-pending-v1';
export interface PendingProductConsolidation {
  version: 1; request: ProductConsolidationPreviewBody;
  apply?: ProductConsolidationApplyBody; discard?: ProductConsolidationApplyBody;
}
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const hash = /^[0-9a-f]{64}$/;
const positive = (value: unknown): value is number => Number.isSafeInteger(value) && Number(value) > 0;
const code = (value: unknown): value is string => typeof value === 'string' && value.length > 0 &&
  value.length <= 100 && value === value.trim() && !/[;\u0000-\u001f\u007f]/.test(value);
const plain = (value: unknown, minimum: number, maximum: number): value is string => typeof value === 'string' &&
  value.length >= minimum && value.length <= maximum && value === value.trim() && !/[\u0000-\u001f]/.test(value);
export function consolidationSourceUrl(value: string): boolean {
  try {
    const url = new URL(value);
    return value.length <= 2000 && !/[\u0000-\u0020]/.test(value) && url.protocol === 'https:' &&
      !!url.hostname && !url.username && !url.password;
  } catch { return false; }
}
function validTarget(value: any): value is ConsolidationTarget {
  return value && typeof value.shop_code === 'string' && /^[a-z0-9][a-z0-9_-]{0,49}$/.test(value.shop_code) &&
    code(value.code) && code(value.parent_code) && typeof value.is_variant === 'boolean' &&
    positive(value.native_product_id) && value.sale_to_stock_factor === 1 &&
    (value.is_variant ? positive(value.native_variant_id) : value.native_variant_id == null && value.parent_code === value.code);
}
function validRequest(value: any): value is ProductConsolidationPreviewBody {
  if (!value || !uuid.test(value.request_id) || !positive(value.keeper_product_id) ||
      !Array.isArray(value.retired_product_ids) || !value.retired_product_ids.length || value.retired_product_ids.length > 9 ||
      !value.retired_product_ids.every(positive) || new Set(value.retired_product_ids).size !== value.retired_product_ids.length ||
      value.retired_product_ids.includes(value.keeper_product_id) || value.mode !== 'identity_only_preserve_unconfirmed_history' ||
      value.same_physical_retail_unit_confirmed !== true || value.legacy_initial_history_preserved !== true ||
      (value.preserve_keeper_invoice_receiving !== undefined && typeof value.preserve_keeper_invoice_receiving !== 'boolean') ||
      (value.use_keeper_supplier_gtin !== undefined && typeof value.use_keeper_supplier_gtin !== 'boolean')) return false;
  const evidence = value.identity_evidence;
  if (!evidence || !plain(evidence.review_note, 20, 3000) || !plain(evidence.physical_unit, 5, 500) ||
      !Array.isArray(evidence.source_urls) || !evidence.source_urls.length || evidence.source_urls.length > 10 ||
      !evidence.source_urls.every((url: unknown) => typeof url === 'string' && consolidationSourceUrl(url))) return false;
  if (!Array.isArray(value.mapping_actions) || value.mapping_actions.length > 50 ||
      !value.mapping_actions.every((entry: any) => entry && positive(entry.mapping_id) &&
        (['retain', 'bind'].includes(entry.action) ? validTarget(entry.target) :
          ['retire_absent', 'retire_unverified_code_absent'].includes(entry.action) && entry.target === null)) ||
      new Set(value.mapping_actions.map((entry: ConsolidationMappingAction) => entry.mapping_id)).size !== value.mapping_actions.length ||
      !Array.isArray(value.additional_sales) || value.additional_sales.length > 20 || !value.additional_sales.every(validTarget)) return false;
  return true;
}
export function pendingProductConsolidation(): PendingProductConsolidation | null {
  try {
    const item = JSON.parse(window.sessionStorage.getItem(PRODUCT_CONSOLIDATION_PENDING_KEY) || 'null');
    if (!item || item.version !== 1 || !validRequest(item.request) ||
        (item.apply && item.discard && item.apply.preview_hash !== item.discard.preview_hash) ||
        ['apply', 'discard'].some(key => item[key] !== undefined &&
          (!item[key] || !hash.test(item[key].preview_hash) || item[key].confirmed !== true))) return null;
    return item;
  } catch { return null; }
}
export function rememberProductConsolidation(item: PendingProductConsolidation) {
  const serialized = JSON.stringify(item);
  window.sessionStorage.setItem(PRODUCT_CONSOLIDATION_PENDING_KEY, serialized);
  if (window.sessionStorage.getItem(PRODUCT_CONSOLIDATION_PENDING_KEY) !== serialized) throw new Error('storage_unavailable');
}
export function forgetProductConsolidation(id: string) {
  const stored = window.sessionStorage.getItem(PRODUCT_CONSOLIDATION_PENDING_KEY);
  if (stored && JSON.parse(stored)?.request?.request_id === id) {
    window.sessionStorage.removeItem(PRODUCT_CONSOLIDATION_PENDING_KEY);
    if (window.sessionStorage.getItem(PRODUCT_CONSOLIDATION_PENDING_KEY) === stored) throw new Error('storage_unavailable');
  }
}
