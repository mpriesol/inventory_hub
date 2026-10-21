import { hubRequest } from './access';
import { SkuIdentityInput } from './skuPolicy';

export type EditorMode = 'common' | 'biketrek' | 'xtrek';
export interface ProductEditorOptions {
  shops: { id: number; code: string; name: string }[]; warehouses: { id: number; code: string; name: string; is_default?: boolean }[];
  brands: string[]; page_sizes: number[]; currency: 'EUR'; price_basis: 'incl_vat';
}
export interface ProductShopValues { name: string | null; sale_price_gross: string | null; visible: boolean | null }
export interface ProductEditorRow {
  is_active: boolean; supplier_linked: boolean;
  id: number; sku: string; group: { id: number; code: string; name: string } | null;
  attributes: { name: string; value: string }[]; eans: string[]; supplier_codes: { supplier_code: string; code: string }[]; image_url: string | null;
  supplier_availability?: {
    available: boolean | null; fresh: boolean; label: string; source: string | null;
    quantity: string | null; quantity_kind: 'exact' | 'minimum' | 'boolean' | 'unknown';
    observed_at: string | null; expires_at: string | null; orderable: true;
    availability_state?: 'available' | 'unavailable' | 'unknown';
    status?: 'missing_link' | 'unavailable_source' | 'unconfigured' | 'not_loaded' | 'missing_observation' | 'feed_mismatch' | 'configuration_error' | 'stale' | 'fresh' | 'conflict';
    reason?: string | null; supplier?: string; supplier_name?: string; supplier_sku?: string;
    feed_key?: string | null; observation_feed_key?: string | null; last_success_at?: string | null;
    last_error?: string | null; refresh_state?: 'idle' | 'queued' | 'running';
    action?: 'configure' | 'refresh' | 'check_source' | 'wait' | 'link';
  };
  revision: number; snapshot_hash: string; common: { name: string; brand: string | null; internal_note: string | null; image_url?: string | null };
  variant: { sale_price_gross: string | null; vat_rate: string | null; note: string | null; attributes?: { name: string; value: string }[] | null; eans?: string[] | null };
  warehouse: { code: string; location: string | null; min_quantity: string | null } | null;
  stock: { known: boolean; tracking_status?: 'confirmed' | 'unconfirmed' | 'no_warehouse'; started_at?: string | null; qty_on_hand: string | null; qty_reserved: string | null; qty_quarantined: string | null; qty_available: string | null; avg_cost: string | null; total_value: string | null };
  shops: { shop_code: string; mapped: boolean; shop_url?: string | null; shop_admin_url?: string | null; overrides: ProductShopValues; effective: ProductShopValues;
    observed: { name: string | null; price: string | null; price_basis: 'unknown'; visible: boolean | null }; published_fields?: ProductPublicationField[]; state: 'inherited' | 'saved_unpublished' | 'published' }[];
  overrides: { common?: Record<string, unknown>; variant?: Record<string, unknown>; warehouses?: Record<string, Record<string, unknown>>; shops?: Record<string, Record<string, unknown>> };
}
export interface ProductEditorFilters {
  activity: 'all' | 'active' | 'inactive'; supplier_link: 'all' | 'linked' | 'unlinked';
  stock_scope: 'all' | 'confirmed' | 'unconfirmed' | 'in_stock';
  q: string; brand: string; shop_code: string; warehouse_code: string; page: number; page_size: number;
  sort: 'group_sku' | 'sku' | 'name'; direction: 'asc' | 'desc';
}
export interface ProductEditorPageData { items: ProductEditorRow[]; total: number; page: number; page_size: number; warehouse: { code: string; name: string } | null; currency: 'EUR'; price_basis: 'incl_vat' }
export interface ProductEditorChange {
  is_active?: boolean;
  product_id: number; expected_revision: number; snapshot_hash: string;
  common?: Partial<Record<'name' | 'brand' | 'internal_note' | 'image_url', string | null>>;
  variant?: Partial<Record<'sale_price_gross' | 'vat_rate' | 'note', string | null>> & { attributes?: { name: string; value: string }[] | null; eans?: string[] | null; sku?: string | null };
  warehouse?: Partial<Record<'location' | 'min_quantity', string | null>>;
  shops?: Record<string, Partial<ProductShopValues>>;
}
export interface ProductEditorSave {
  created?: boolean; reused?: boolean;
  request_id: string; status: 'completed'; external_write_enabled: false;
  results: { product_id: number; status: 'saved' | 'conflict' | 'invalid' | 'missing'; row?: ProductEditorRow; errors: { code: string; field?: string }[] }[];
}
export type ProductEditorSnapshot = Pick<ProductEditorRow, 'common' | 'variant' | 'warehouse' | 'shops' | 'overrides'> & Partial<Pick<ProductEditorRow, 'is_active' | 'sku' | 'eans' | 'attributes' | 'image_url'>>;
export interface ProductEditorDetail extends ProductEditorRow { audit: { id: number; created_at: string; before: ProductEditorSnapshot; after: ProductEditorSnapshot }[] }
export interface ProductEditorSaveBody { request_id: string; warehouse_code: string | null; confirmed: true; changes: ProductEditorChange[] }
const base = '/api/product-editor';
export interface ProductCardCreate { request_id: string; sku?: string; name: string; is_active: boolean; sku_identity?: SkuIdentityInput; eans?: string[]; supplier_id?: number; supplier_sku?: string }
export const createProductCard = (body: ProductCardCreate) => hubRequest<ProductEditorSave>(`${base}/products`, body);
export const getProductEditorOptions = (signal?: AbortSignal) => hubRequest<ProductEditorOptions>(`${base}/options`, undefined, signal);
export const getEditorProducts = async (filters: ProductEditorFilters, signal?: AbortSignal): Promise<ProductEditorPageData> => {
  const readPage = (page: number, page_size: number) => {
    const params = new URLSearchParams(); Object.entries({ ...filters, page, page_size }).forEach(([key, value]) => { if (value !== '') params.set(key, String(value)); });
    return hubRequest<ProductEditorPageData>(`${base}/products?${params}`, undefined, signal);
  };
  if (filters.page_size <= 100) return readPage(filters.page, filters.page_size);
  // The existing API accepts at most 100 rows. Assemble a bounded display page
  // without changing save/publication limits or exposing partial results.
  if (![250, 500].includes(filters.page_size)) throw new Error('Invalid display page size');
  const offset = (filters.page - 1) * filters.page_size, firstPage = Math.floor(offset / 100) + 1;
  const first = await readPage(firstPage, 100), items = [...first.items];
  const count = Math.min(filters.page_size, Math.max(0, first.total - offset));
  const pages = Math.ceil((offset % 100 + count) / 100);
  for (let index = 1; index < pages; index++) {
    const next = await readPage(firstPage + index, 100);
    if (next.total !== first.total) throw new Error('Product list changed during loading');
    items.push(...next.items);
  }
  const visible = items.slice(offset % 100, offset % 100 + count);
  if (visible.length !== count || new Set(visible.map(row => row.id)).size !== count) throw new Error('Incomplete product list');
  return { ...first, items: visible, page: filters.page, page_size: filters.page_size };
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
