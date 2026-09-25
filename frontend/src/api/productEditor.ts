import { hubRequest } from './access';

export type EditorMode = 'common' | 'biketrek' | 'xtrek';
export interface ProductEditorOptions {
  shops: { id: number; code: string; name: string }[]; warehouses: { id: number; code: string; name: string }[];
  brands: string[]; page_sizes: number[]; currency: 'EUR'; price_basis: 'incl_vat';
}
export interface ProductShopValues { name: string | null; sale_price_gross: string | null; visible: boolean | null }
export interface ProductEditorRow {
  id: number; sku: string; group: { id: number; code: string; name: string } | null;
  attributes: { name: string; value: string }[]; eans: string[]; supplier_codes: { supplier_code: string; code: string }[]; image_url: string | null;
  supplier_availability?: { available: boolean | null; fresh: boolean; label: string; source: string | null; quantity: string | null; quantity_kind: 'exact' | 'minimum' | 'boolean' | 'unknown'; observed_at: string | null; expires_at: string | null; orderable: true; status?: 'missing_link' | 'unavailable_source' | 'missing_observation' | 'stale' | 'fresh' | 'conflict'; reason?: string | null; supplier?: string; supplier_sku?: string };
  revision: number; snapshot_hash: string; common: { name: string; brand: string | null; internal_note: string | null; image_url?: string | null };
  variant: { sale_price_gross: string | null; vat_rate: string | null; note: string | null; attributes?: { name: string; value: string }[] | null; eans?: string[] | null };
  warehouse: { code: string; location: string | null; min_quantity: string | null } | null;
  stock: { known: boolean; qty_on_hand: string | null; qty_reserved: string | null; qty_quarantined: string | null; qty_available: string | null; avg_cost: string | null; total_value: string | null };
  shops: { shop_code: string; mapped: boolean; shop_url?: string | null; shop_admin_url?: string | null; overrides: ProductShopValues; effective: ProductShopValues;
    observed: { name: string | null; price: string | null; price_basis: 'unknown'; visible: boolean | null }; published_fields?: ProductPublicationField[]; state: 'inherited' | 'saved_unpublished' | 'published' }[];
  overrides: { common?: Record<string, unknown>; variant?: Record<string, unknown>; warehouses?: Record<string, Record<string, unknown>>; shops?: Record<string, Record<string, unknown>> };
}
export interface ProductEditorFilters {
  q: string; brand: string; shop_code: string; warehouse_code: string; page: number; page_size: number;
  sort: 'group_sku' | 'sku' | 'name'; direction: 'asc' | 'desc';
}
export interface ProductEditorPageData { items: ProductEditorRow[]; total: number; page: number; page_size: number; warehouse: { code: string; name: string } | null; currency: 'EUR'; price_basis: 'incl_vat' }
export interface ProductEditorChange {
  product_id: number; expected_revision: number; snapshot_hash: string;
  common?: Partial<Record<'name' | 'brand' | 'internal_note' | 'image_url', string | null>>;
  variant?: Partial<Record<'sale_price_gross' | 'vat_rate' | 'note', string | null>> & { attributes?: { name: string; value: string }[] | null; eans?: string[] | null; sku?: string | null };
  warehouse?: Partial<Record<'location' | 'min_quantity', string | null>>;
  shops?: Record<string, Partial<ProductShopValues>>;
}
export interface ProductEditorSave {
  request_id: string; status: 'completed'; external_write_enabled: false;
  results: { product_id: number; status: 'saved' | 'conflict' | 'invalid' | 'missing'; row?: ProductEditorRow; errors: { code: string; field?: string }[] }[];
}
export type ProductEditorSnapshot = Pick<ProductEditorRow, 'common' | 'variant' | 'warehouse' | 'shops' | 'overrides'> & Partial<Pick<ProductEditorRow, 'sku' | 'eans' | 'attributes' | 'image_url'>>;
export interface ProductEditorDetail extends ProductEditorRow { audit: { id: number; created_at: string; before: ProductEditorSnapshot; after: ProductEditorSnapshot }[] }
export interface ProductEditorSaveBody { request_id: string; warehouse_code: string | null; confirmed: true; changes: ProductEditorChange[] }
const base = '/api/product-editor';
export const getProductEditorOptions = (signal?: AbortSignal) => hubRequest<ProductEditorOptions>(`${base}/options`, undefined, signal);
export const getEditorProducts = (filters: ProductEditorFilters, signal?: AbortSignal) => {
  const params = new URLSearchParams(); Object.entries(filters).forEach(([key, value]) => { if (value !== '') params.set(key, String(value)); });
  return hubRequest<ProductEditorPageData>(`${base}/products?${params}`, undefined, signal);
};
export const getEditorProduct = (id: number, warehouse: string, signal?: AbortSignal) => hubRequest<ProductEditorDetail>(`${base}/products/${id}${warehouse ? `?warehouse_code=${encodeURIComponent(warehouse)}` : ''}`, undefined, signal);
export const saveEditorProducts = (body: ProductEditorSaveBody) => hubRequest<ProductEditorSave>(`${base}/save`, body);
export const getEditorSave = (id: string, signal?: AbortSignal) => hubRequest<ProductEditorSave>(`${base}/saves/${encodeURIComponent(id)}`, undefined, signal);

export type ProductPublicationField = 'name' | 'sale_price_gross' | 'visible' | 'ean' | 'attributes' | 'image_url';
export interface ProductPublication {
  id: string; product_id: number; shop_code: string; state: string; revision: number; fields: ProductPublicationField[];
  before: Record<string, unknown>; after: Record<string, unknown>; payload?: Record<string, unknown>;
  expires_at: string; created_at?: string; error: string | { code?: string } | null;
}
export const previewProductPublication = (row: ProductEditorRow, shop: string, fields: ProductPublicationField[]) => hubRequest<ProductPublication>(`${base}/products/${row.id}/publication/preview`, { shop_code: shop, expected_revision: row.revision, fields });
export const sendProductPublication = (id: string) => hubRequest<ProductPublication>(`${base}/publications/${encodeURIComponent(id)}/send`, { confirmed: true });
export const readProductPublication = (id: string, signal?: AbortSignal) => hubRequest<ProductPublication>(`${base}/publications/${encodeURIComponent(id)}`, undefined, signal);
export const productPublicationHistory = (id: number, signal?: AbortSignal) => hubRequest<{ items: ProductPublication[] }>(`${base}/products/${id}/publications`, undefined, signal);
export const resolveProductPublication = (id: string, note: string) => hubRequest<ProductPublication>(`${base}/publications/${encodeURIComponent(id)}/resolve`, { confirmed: true, original_request_settled: true, note });

export const refreshEditorAttributes = (id: number) => hubRequest<ProductEditorDetail>(`${base}/products/${id}/refresh-attributes`, {});
