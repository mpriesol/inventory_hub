import { accessRevision, hubAccessToken, hubRequest, hubUpload } from './access';
import { CatalogApiError } from './catalog';
import { hubFetchInit } from './transport';

export interface AtomerAvailabilityOptions {
  revision: number; enabled: boolean; warehouse_code: string | null; source_configured: boolean;
  source_format: string | null; interval_seconds: number; last_started_at: string | null;
  last_completed_at: string | null; last_error: string | null; next_run_at: string | null;
  warehouses: { code: string; name: string; is_default?: boolean }[];
  admin_source_configured?: boolean; admin_complete?: boolean;
  admin_upload?: { imported_at: string; complete: boolean; fresh: boolean } | null;
  latest_run?: AtomerAvailabilityRun | null;
}
export interface AtomerAvailabilityRun {
  id: string; status: 'queued' | 'fetching' | 'ready' | 'exported' | 'failed'; revision: number;
  trigger: string; created_at: string; source_fetched_at: string | null; source_sha256: string | null;
  completed_at: string | null; error: string | null; download_available: boolean;
  receiving_batch_id?: string | null; warehouse_code?: string;
  summary: { total: number; changed: number; blocked: number; unmatched: number; exportable?: number };
  warnings?: string[];
  coverage?: { kind: 'buyers_only' | 'admin_partial' | 'admin_complete'; hidden_rows: number; missing_parents: number; source_blocked_rows?: number; admin_source_at?: string | null };
  missing_products?: { id: number; sku: string; name: string }[];
}
export interface AtomerAvailabilityRow {
  code: string; parent_code: string | null; name: string; image: string | null;
  kind: 'parent' | 'variant' | 'standalone'; product_id: number | null; sku: string | null;
  parent_name?: string | null; parent_image?: string | null; parent_visibility?: boolean | null;
  current_visibility: boolean | null; current_availability: string;
  proposed_visibility: boolean | null; proposed_availability: string; qty_available: string | null;
  status: 'changed' | 'unchanged' | 'blocked' | 'unmatched' | 'context'; reasons: string[];
  editable?: boolean; matches_filter?: boolean; include_in_export?: boolean; manual_override?: boolean; apply_to_every_export?: boolean;
  supplier_status?: string | null; supplier_reason?: string | null; supplier_source?: string | null; warnings?: string[];
  availability_override?: { label?: string; visible?: boolean; source?: string; updated_at?: string; actor?: string } | null;
}
export type AtomerAvailabilityView = 'all' | 'changes' | 'issues';
export interface AtomerAvailabilityFilter {
  view: AtomerAvailabilityView; search?: string; kind?: 'all' | 'parent' | 'variant' | 'standalone';
  status?: 'all' | AtomerAvailabilityRow['status']; visibility?: 'all' | 'visible' | 'hidden' | 'unknown';
  availability?: string | null; current_availability?: string | null; current_visibility?: 'all' | 'visible' | 'hidden' | 'unknown';
  include_in_export?: boolean | null; manual_override?: boolean | null;
}
export type AtomerAvailabilitySort = 'code' | 'name' | 'sku' | 'ean' | 'current_availability' | 'proposed_availability'
  | 'current_visibility' | 'proposed_visibility' | 'qty_available' | 'status' | 'include_in_export' | 'manual_override';
export interface AtomerAvailabilityQuery extends AtomerAvailabilityFilter { sort?: AtomerAvailabilitySort; direction?: 'asc' | 'desc' }
export interface AtomerAvailabilitySelection { codes?: string[]; all_filtered?: boolean; filter?: AtomerAvailabilityFilter; excluded_codes?: string[] }
export interface AtomerAvailabilityChanges { visibility?: boolean; availability?: string; include_in_export?: boolean; apply_to_every_export?: boolean }
export interface AtomerAvailabilityEdit {
  request_id: string; expected_revision: number; selection: AtomerAvailabilitySelection; changes: AtomerAvailabilityChanges;
}
export interface AtomerAvailabilityEditResult { id: string; request_id: string; revision: number; updated: number; skipped: number; skipped_persistent: number }
export interface AtomerAvailabilityDetail extends AtomerAvailabilityRun {
  rows: AtomerAvailabilityRow[]; total: number; offset: number; limit?: number; view: AtomerAvailabilityView;
  availability_options?: string[]; matched_total?: number; selectable_total?: number; filter_options?: { availability: string[] };
}
export interface AtomerAvailabilityOverride { code: string; visibility?: boolean; availability?: string }
export interface AtomerAvailabilityExport { expected_revision: number; overrides: AtomerAvailabilityOverride[] }
export interface AtomerAvailabilityCreate { request_id: string; warehouse_code?: string; receiving_batch_id?: string }
export interface AtomerAvailabilityConfigure {
  request_id: string; expected_revision: number; enabled: boolean; warehouse_code: string;
  feed_url?: string; admin_export_url?: string; admin_complete?: boolean;
}

const base = '/api/atomer-availability';
function checkedRun(value: AtomerAvailabilityRun, id: string): AtomerAvailabilityRun {
  if (!value || value.id !== id || !['queued', 'fetching', 'ready', 'exported', 'failed'].includes(value.status)
      || !Number.isSafeInteger(value.revision) || value.revision < 1 || typeof value.download_available !== 'boolean') {
    throw new CatalogApiError('atomer_availability_response_invalid', 'The response does not match this export');
  }
  return value;
}
export const getAtomerAvailabilityOptions = (signal?: AbortSignal) => hubRequest<AtomerAvailabilityOptions>(`${base}/options`, undefined, signal);
export const saveAtomerAvailabilityOptions = (body: AtomerAvailabilityConfigure) => hubRequest<AtomerAvailabilityOptions>(`${base}/configure`, body);
export function uploadAtomerAvailabilityAdmin(file: File, complete: boolean) {
  const body = new FormData(); body.append('file', file); body.append('complete', String(complete));
  return hubUpload<{ source_sha256: string; imported_at: string; complete: boolean }>(`${base}/source/admin`, body);
}
export const listAtomerAvailabilityRuns = (signal?: AbortSignal) => hubRequest<{ items: AtomerAvailabilityRun[] }>(`${base}/runs`, undefined, signal);
export const createAtomerAvailabilityRun = async (body: AtomerAvailabilityCreate) => checkedRun(await hubRequest<AtomerAvailabilityRun>(`${base}/runs`, body), body.request_id);
export const getAtomerAvailabilityRun = async (id: string, offset = 0, signal?: AbortSignal, query: AtomerAvailabilityQuery | AtomerAvailabilityView = 'all') => {
  const filter = typeof query === 'string' ? { view: query } : query;
  const params = new URLSearchParams({ offset: String(offset), limit: '100', view: filter.view });
  for (const [key, item] of Object.entries(filter)) if (item !== undefined && item !== null && item !== '') params.set(key, String(item));
  const value = await hubRequest<AtomerAvailabilityDetail>(`${base}/runs/${encodeURIComponent(id)}?${params}`, undefined, signal);
  checkedRun(value, id);
  if (!Array.isArray(value.rows) || value.offset !== offset || value.view !== filter.view || !Number.isSafeInteger(value.total) || value.total < 0) {
    throw new CatalogApiError('atomer_availability_response_invalid', 'The response does not match this preview page');
  }
  return value;
};
function checkedEdit(value: AtomerAvailabilityEditResult, id: string, requestId: string) {
  if (!value || value.id !== id || value.request_id !== requestId || !Number.isSafeInteger(value.revision) || value.revision < 1) {
    throw new CatalogApiError('atomer_availability_response_invalid', 'The response does not match this edit');
  }
  return value;
}
export const editAtomerAvailabilityRun = async (id: string, body: AtomerAvailabilityEdit) =>
  checkedEdit(await hubRequest<AtomerAvailabilityEditResult>(`${base}/runs/${encodeURIComponent(id)}/edit`, body), id, body.request_id);
export const getAtomerAvailabilityEdit = async (id: string, requestId: string, signal?: AbortSignal) =>
  checkedEdit(await hubRequest<AtomerAvailabilityEditResult>(`${base}/runs/${encodeURIComponent(id)}/edits/${encodeURIComponent(requestId)}`, undefined, signal), id, requestId);
export const exportAtomerAvailabilityRun = async (id: string, body: AtomerAvailabilityExport) => {
  const value = checkedRun(await hubRequest<AtomerAvailabilityRun>(`${base}/runs/${encodeURIComponent(id)}/export`, body), id);
  if (value.status !== 'exported') throw new CatalogApiError('atomer_availability_response_invalid', 'CSV creation was not confirmed');
  return value;
};

/** Downloads stay inside the existing same-origin authenticated transport. */
export async function downloadAtomerAvailabilityRun(id: string): Promise<void> {
  const path = `${base}/runs/${encodeURIComponent(id)}/download`, revision = accessRevision();
  const response = await fetch(path, hubFetchInit(path, undefined, hubAccessToken()));
  if (!response.ok) {
    const value = await response.json().catch(() => null);
    throw new CatalogApiError(value?.detail?.code || 'atomer_availability_download_failed', String(response.status));
  }
  const blob = await response.blob();
  if (revision !== accessRevision()) return;
  const url = URL.createObjectURL(blob), link = document.createElement('a');
  link.href = url; link.download = `atomer-availability-${id}.csv`;
  document.body.append(link); link.click(); link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}
