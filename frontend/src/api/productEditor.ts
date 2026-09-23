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
  revision: number; snapshot_hash: string; common: { name: string; brand: string | null; internal_note: string | null };
  variant: { sale_price_gross: string | null; vat_rate: string | null; note: string | null };
  warehouse: { code: string; location: string | null; min_quantity: string | null } | null;
  stock: { known: boolean; qty_on_hand: string | null; qty_reserved: string | null; qty_available: string | null; avg_cost: string | null; total_value: string | null };
  shops: { shop_code: string; mapped: boolean; overrides: ProductShopValues; effective: ProductShopValues;
    observed: { name: string | null; price: string | null; price_basis: 'unknown'; visible: boolean | null }; state: 'inherited' | 'saved_unpublished' }[];
  overrides: { common?: Record<string, unknown>; variant?: Record<string, unknown>; warehouses?: Record<string, Record<string, unknown>>; shops?: Record<string, Record<string, unknown>> };
}
export interface ProductEditorFilters {
  q: string; brand: string; shop_code: string; warehouse_code: string; page: number; page_size: number;
  sort: 'group_sku' | 'sku' | 'name'; direction: 'asc' | 'desc';
}
export interface ProductEditorPageData { items: ProductEditorRow[]; total: number; page: number; page_size: number; warehouse: { code: string; name: string } | null; currency: 'EUR'; price_basis: 'incl_vat' }
export interface ProductEditorChange {
  product_id: number; expected_revision: number; snapshot_hash: string;
  common?: Partial<Record<'name' | 'brand' | 'internal_note', string | null>>;
  variant?: Partial<Record<'sale_price_gross' | 'vat_rate' | 'note', string | null>>;
  warehouse?: Partial<Record<'location' | 'min_quantity', string | null>>;
  shops?: Record<string, Partial<ProductShopValues>>;
}
export interface ProductEditorSave {
  request_id: string; status: 'completed'; external_write_enabled: false;
  results: { product_id: number; status: 'saved' | 'conflict' | 'invalid' | 'missing'; row?: ProductEditorRow; errors: { code: string; field?: string }[] }[];
}
export type ProductEditorSnapshot = Pick<ProductEditorRow, 'common' | 'variant' | 'warehouse' | 'shops' | 'overrides'>;
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
