import { hubRequest } from './access';

export interface SkuIdentityInput {
  brand: string; source_code: string; code_kind: 'manufacturer' | 'supplier' | 'gtin' | 'service' | 'unbranded';
  source_ref: string; leaf_confirmed: boolean; supplier_id?: number;
}
export interface SkuBrand { id: number; code: string; name: string; kind: 'brand' | 'service' | 'unbranded'; aliases: string[] }
export interface SkuPreview {
  status: 'new' | 'existing' | 'conflict' | 'blocked'; sku: string | null; product_id: number | null;
  reasons: string[]; candidate_product_ids: number[]; brand_id: number | null;
}
export interface SkuPreviewContext { barcodes?: string[]; supplier_id?: number; supplier_sku?: string; expected_product_id?: number; sku?: string }
export const emptySkuIdentity = (brand = '', source_code = ''): SkuIdentityInput => ({ brand, source_code, code_kind: 'manufacturer', source_ref: '', leaf_confirmed: false });
export const getSkuBrands = (signal?: AbortSignal) => hubRequest<{ brands: SkuBrand[] }>('/api/sku-policy/brands', undefined, signal);
export const createSkuBrand = (body: Omit<SkuBrand, 'id'> & { source_ref: string }) => hubRequest<SkuBrand>('/api/sku-policy/brands', body);
export const addSkuBrandAlias = (id: number, alias: string, source_ref: string) => hubRequest<SkuBrand>(`/api/sku-policy/brands/${id}/aliases`, { alias, source_ref });
export const previewSku = (sku_identity: SkuIdentityInput | null, context: SkuPreviewContext = {}, signal?: AbortSignal) => hubRequest<SkuPreview>('/api/sku-policy/preview', { sku_identity, ...context }, signal);
