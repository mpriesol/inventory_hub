export interface CatalogProduct {
  id: number; supplier: string; feed_key: string; run_id: number | null;
  code: string; shop_code: string; manufacturer_code: string | null; eans: string[];
  name: string; brand: string | null; category: string | null; category_code: string | null;
  description: string; manufacturer_description: string; safety_information: string;
  images: string[]; url: string | null; parameters: { name: string; value: string }[];
  static_parameters: Record<string, unknown>;
  prices: { currency: string; vat_percent: string | null; vat_source: 'configured' | 'feed'; purchase_net: string | null; purchase_gross: string | null;
    retail_net: string | null; retail_gross: string | null; discount_net: string | null; discount_gross: string | null };
  supplier_stock: string | null; supplier_stock_min: string | null; supplier_stock_raw: string | null;
  supplier_stock_external: string | null; supplier_stock_external_raw: string | null; supplier_external_available: boolean | null;
  availability: string | null; delivery_date: string | null;
  group_code: string | null; group_name: string | null; variant_relationship: 'explicit' | 'not_provided';
  variant_attributes: { name: string; value: string }[]; warnings: string[]; listed: boolean;
  fetched_at: string | null; source_hash: string | null;
}
export interface CatalogRow { key: string; product: CatalogProduct; is_group: boolean; variants_count: number; matching_ids: number[]; variants: CatalogProduct[] }
export interface CatalogPage {
  supplier: string; feed_key: string; run_id: number | null; fetched_at: string | null;
  total: number; total_items: number; page: number; page_size: number; pages: number;
  manufacturers: string[]; items: CatalogRow[];
}
export interface CatalogStatus {
  supplier: string; name: string; feed_key: string; run_id: number | null; last_successful_at: string | null; status: string; items: number;
  sources: { key: string; name: string; supported: boolean; configured: boolean }[];
  shops: { code: string; name: string; ready: boolean }[];
  defaults: { category_code: string; currency: string; vat_percent: string };
}
export interface CatalogDetail { product: CatalogProduct; variants: CatalogProduct[]; source_fields: Record<string, unknown>; source_xml: string; description_html: string; active: boolean }
export interface ImportOptions {
  language: string; currency: string; pricelist: string; category_code: string | null;
  pricing: 'configured' | 'retail'; include_images: boolean; include_description: boolean; include_parameters: boolean;
}
export interface TargetOptions {
  shop: string; prices_with_vat: boolean; create_validation_field: boolean;
  languages: { code: string; currency: string; default: boolean }[];
  pricelists: { name: string; default: boolean }[];
  categories: { code: string; names: Record<string, string> }[];
}
export interface ImportItem {
  code: string; name: string; product_ids: number[]; variants_count: number;
  status: 'ready' | 'exists' | 'invalid' | 'created' | 'failed' | 'uncertain';
  warnings: string[]; errors: string[]; payload: Record<string, any>;
}
export interface ImportPreview {
  preview_id: string; shop: string; supplier: string; created_at: string; expires_at: string;
  options: ImportOptions; items: ImportItem[]; errors: string[]; warnings: string[]; create_validation_field: boolean;
}
export interface ImportResult {
  preview_id: string; shop: string; status: 'queued' | 'running' | 'completed' | 'failed';
  items: ImportItem[]; errors: string[]; updated_at: string;
}
export type CatalogFilters = { feed_key: string; q: string; code: string; ean: string; manufacturer: string; sort: string; listing: string; shop: string };

export class CatalogApiError extends Error {
  constructor(public code: string, message: string) { super(message); }
}
export async function catalogRequest<T>(path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`/api${path}`, { method: body === undefined ? 'GET' : 'POST', signal,
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body) });
  let data: any;
  try { data = await response.json(); } catch { throw new CatalogApiError('request_failed', String(response.status)); }
  if (!response.ok) throw new CatalogApiError(data?.detail?.code || 'request_failed', data?.detail?.message || String(response.status));
  return data;
}
export function catalogQuery(filters: Partial<CatalogFilters> & { page?: number; page_size?: number; grouped?: boolean }) {
  const query = new URLSearchParams();
  Object.entries(filters).forEach(([key, value]) => { if (value !== '' && value !== undefined) query.set(key, String(value)); });
  return query.toString();
}
