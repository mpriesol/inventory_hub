import { hubRequest, hubUpload } from './access';
import { CatalogProduct } from './catalog';

export interface FeedTransform {
  op: 'trim' | 'strip_html' | 'split' | 'join' | 'replace' | 'decimal' | 'multiply' | 'round' | 'truncate' | 'map' | 'prefix' | 'suffix';
  value?: string; with?: string; values?: Record<string, string>;
}
export interface FeedBinding {
  target: string; source?: string; constant?: unknown; default?: unknown; transforms: FeedTransform[];
  param_name_path?: string; param_value_path?: string;
}
export interface FeedDefinition {
  format: 'auto' | 'xml' | 'csv' | 'json'; record_path: string; csv_delimiter: string;
  csv_encoding: 'utf-8-sig' | 'cp1250' | 'iso-8859-2'; bindings: FeedBinding[];
  category_rules: { source: string; target_code: string }[]; seo_fallback: boolean;
}
export interface FeedMappingConfig {
  supplier: string; feed_key: string; shop: string; revision: number; definition: FeedDefinition;
  fields: { key: string; label: string; type: string }[]; native_parser: string | boolean | null; configured: boolean;
  base_revision?: number; inherited_definition?: FeedDefinition;
}
export interface FeedField { path: string; type: string; examples: unknown[]; populated: number; total: number }
export interface FeedInspection {
  sample_id?: string; format: string; record_path: string; total_records: number; fields: FeedField[];
  sample_records: Record<string, unknown>[]; source_categories: string[];
}
export interface FeedPreview { items: CatalogProduct[]; errors: { row: number; message: string }[]; mapping_revision: number }
export const emptyFeedDefinition = (): FeedDefinition => ({ format: 'auto', record_path: '', csv_delimiter: '', csv_encoding: 'utf-8-sig', bindings: [], category_rules: [], seo_fallback: true });
const path = (supplier: string) => `/api/suppliers/${encodeURIComponent(supplier)}/feed-mapping`;
export const getFeedMapping = (supplier: string, feedKey: string, shop: string, signal?: AbortSignal) => hubRequest<FeedMappingConfig>(`${path(supplier)}?${new URLSearchParams({feed_key: feedKey, shop})}`, undefined, signal);
export const saveFeedMapping = (supplier: string, feedKey: string, shop: string, revision: number, definition: FeedDefinition) => hubRequest<FeedMappingConfig>(path(supplier), { feed_key: feedKey, shop, expected_revision: revision, definition }, undefined, 'PUT');
export const inspectFeed = (supplier: string, feedKey: string, definition: FeedDefinition, sampleId?: string, signal?: AbortSignal) => hubRequest<FeedInspection>(`${path(supplier)}/inspection?${new URLSearchParams({ feed_key: feedKey, format: definition.format, record_path: definition.record_path, csv_delimiter: definition.csv_delimiter, csv_encoding: definition.csv_encoding, ...(sampleId ? { sample_id: sampleId } : {}) })}`, undefined, signal);
export function uploadFeedSample(supplier: string, feedKey: string, file: File, definition: FeedDefinition) {
  const form = new FormData(); form.append('file', file); form.append('feed_key', feedKey);
  for (const key of ['format', 'record_path', 'csv_delimiter', 'csv_encoding'] as const) form.append(key, definition[key]);
  return hubUpload<FeedInspection>(`${path(supplier)}/upload`, form);
}
export const previewFeedMapping = (supplier: string, feedKey: string, shop: string, definition: FeedDefinition, sampleId?: string, signal?: AbortSignal) => hubRequest<FeedPreview>(`${path(supplier)}/preview`, {feed_key: feedKey, shop, definition, ...(sampleId ? {sample_id: sampleId} : {}), limit: 5}, signal);
export const applySavedFeedMapping = (supplier: string, feedKey: string, revision: number, sampleId?: string) => hubRequest<{ items?: number; total?: number; status?: string }>(`${path(supplier)}/remap`, {feed_key: feedKey, expected_revision: revision, ...(sampleId ? {sample_id: sampleId} : {})});
