import { hubRequest, hubUpload } from './access';

export const ATOMER_CONTENT_FIELDS = ['long_description', 'seo_title', 'seo_description'] as const;
export type AtomerContentField = typeof ATOMER_CONTENT_FIELDS[number];
export interface AtomerContentItem {
  request_id: string; source_code: string; hub_sku: string; fields: AtomerContentField[];
}
export interface AtomerContentPublication {
  id: string; product_id: number; shop_code: string; state: string; workflow: 'atomer_content';
  source_code: string; hub_sku: string; source_sha256: string; no_op?: boolean; rollback_of?: string;
  fields: AtomerContentField[]; before: Record<string, unknown>; after: Record<string, unknown>;
  payload?: Record<string, unknown>; observed?: Record<string, unknown> | null;
  protected_changes?: Record<string, unknown> | null; expires_at: string; created_at?: string;
  error: string | { code?: string } | null;
}
export interface AtomerContentPreview {
  source: { sha256: string; size_bytes: number; total_rows: number | null };
  items: { source_code: string; hub_sku: string; publication?: AtomerContentPublication; error?: { code: string } }[];
}
const base = '/api/migration/atomer-content';
export function previewAtomerContent(file: File, items: AtomerContentItem[]) {
  const body = new FormData();
  body.append('source_file', file);
  body.append('selection', JSON.stringify({ items }));
  return hubUpload<AtomerContentPreview>(`${base}/preview`, body);
}
export const atomerContentHistory = (signal?: AbortSignal) => hubRequest<{ items: AtomerContentPublication[] }>(`${base}/publications`, undefined, signal);
export const previewAtomerRollback = (id: string, requestId: string) => hubRequest<AtomerContentPublication>(`${base}/publications/${encodeURIComponent(id)}/rollback-preview`, { request_id: requestId });
export const readAtomerPublication = (id: string, signal?: AbortSignal) => hubRequest<AtomerContentPublication>(`/api/product-editor/publications/${encodeURIComponent(id)}`, undefined, signal);
export const sendAtomerPublication = (id: string) => hubRequest<AtomerContentPublication>(`/api/product-editor/publications/${encodeURIComponent(id)}/send`, { confirmed: true });
export const resolveAtomerPublication = (id: string, note: string) => hubRequest<AtomerContentPublication>(`/api/product-editor/publications/${encodeURIComponent(id)}/resolve`, { confirmed: true, original_request_settled: true, note });
