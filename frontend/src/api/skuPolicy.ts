import { hubRequest } from './access';

export interface SkuIdentityInput {
  brand: string; source_code: string; code_kind: 'manufacturer' | 'supplier' | 'gtin' | 'service' | 'unbranded';
  source_ref: string; leaf_confirmed: boolean; supplier_id?: number;
}
export interface SkuBrandSupplier { name: string; url: string }
export interface SkuBrand { id: number; code: string; name: string; kind: 'brand' | 'service' | 'unbranded'; aliases: string[]; source_ref?: string; archived?: boolean; reserved_codes?: string[]; suppliers?: SkuBrandSupplier[] }
export interface SkuPreview {
  status: 'new' | 'existing' | 'conflict' | 'blocked'; sku: string | null; product_id: number | null;
  reasons: string[]; candidate_product_ids: number[]; brand_id: number | null;
}
export interface SkuPreviewContext { item_type?: 'stock' | 'service'; barcodes?: string[]; supplier_id?: number; supplier_sku?: string; expected_product_id?: number; sku?: string }
export const emptySkuIdentity = (brand = '', source_code = ''): SkuIdentityInput => ({ brand, source_code, code_kind: 'manufacturer', source_ref: '', leaf_confirmed: false });
export const getSkuBrands = (signal?: AbortSignal, includeArchived = false) =>
  hubRequest<{ brands: SkuBrand[] }>(`/api/sku-policy/brands${includeArchived ? '?include_archived=true' : ''}`, undefined, signal);
type SkuBrandValues = Pick<SkuBrand, 'name' | 'code' | 'kind' | 'aliases' | 'source_ref' | 'suppliers'>;
type SkuBrandWrite = SkuBrandValues & { source_ref: string };
function snapshot({ name, code, kind, aliases, source_ref, archived, suppliers }: SkuBrand) {
  return { name, code, kind, aliases, source_ref, archived, suppliers };
}
export const createSkuBrand = (body: SkuBrandWrite) => hubRequest<SkuBrand>('/api/sku-policy/brands', body);
export const updateSkuBrand = (brand: SkuBrand, body: SkuBrandWrite) =>
  hubRequest<SkuBrand>(`/api/sku-policy/brands/${brand.id}`, { ...body, expected: snapshot(brand) }, undefined, 'PUT');
export const removeSkuBrand = (brand: SkuBrand) =>
  hubRequest<{ id: number; removed: boolean; archived: boolean }>(`/api/sku-policy/brands/${brand.id}/remove`, { expected: snapshot(brand) });
export const restoreSkuBrand = (brand: SkuBrand) =>
  hubRequest<SkuBrand>(`/api/sku-policy/brands/${brand.id}/restore`, { expected: snapshot(brand) });
export const addSkuBrandAlias = (id: number, alias: string, source_ref: string) => hubRequest<SkuBrand>(`/api/sku-policy/brands/${id}/aliases`, { alias, source_ref });
export const previewSku = (sku_identity: SkuIdentityInput | null, context: SkuPreviewContext = {}, signal?: AbortSignal) => hubRequest<SkuPreview>('/api/sku-policy/preview', { sku_identity, ...context }, signal);

export interface SkuMigrationMapping {
  shop_code: string; mapping_id: number; is_variant: boolean; old_code: string; proposed_code: string;
  native_id: string | null; parent_code: string | null; parent_native_id: string | null;
  remote_status: 'not_checked' | 'verified' | 'conflict' | 'unavailable'; reasons: string[];
  remote_identity: Record<string, unknown> | null; remote_hash: string | null;
  remote_checked_at?: string | null; remote_requests?: number | null; remote_connection_fingerprint?: string | null; retry_after_seconds?: number | null;
}
export interface SkuMigrationItem {
  product_id: number; name: string; old_sku: string; proposed_sku: string | null;
  status: 'review' | 'blocked' | 'unchanged'; reasons: string[]; candidate_product_ids: number[];
  identity: SkuIdentityInput & { barcodes: string[]; source_hash: string; group_code?: string | null };
  aliases: { type: string; value: string; supplier_id: number | null; origin: 'product_identifiers' }[];
  mappings: SkuMigrationMapping[];
  impacts: { counts: Record<string, number>; stock_state: 'unconfirmed' | 'confirmed' | 'mixed'; requires_review: string[]; unverified: string[]; refresh_required: string[] };
  remote_verified: boolean;
}
export interface SkuMigrationPreview { created_at: string; preview_hash: string; can_apply: false; items: SkuMigrationItem[]; required_checks: string[] }
export interface SkuMigrationRequest { items: { product_id: number; sku_identity: SkuIdentityInput }[]; verify_remote: boolean }
export const previewSkuMigration = (body: SkuMigrationRequest, signal?: AbortSignal) => hubRequest<SkuMigrationPreview>('/api/sku-policy/migration-preview', body, signal);
