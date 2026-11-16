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
  summary: { total: number; changed: number; blocked: number; unmatched: number };
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
}
export interface AtomerAvailabilityDetail extends AtomerAvailabilityRun {
  rows: AtomerAvailabilityRow[]; total: number; offset: number; limit?: number;
  availability_options?: string[];
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
export const getAtomerAvailabilityRun = async (id: string, offset = 0, signal?: AbortSignal) => {
  const value = await hubRequest<AtomerAvailabilityDetail>(`${base}/runs/${encodeURIComponent(id)}?offset=${offset}&limit=100`, undefined, signal);
  checkedRun(value, id);
  if (!Array.isArray(value.rows) || value.offset !== offset || !Number.isSafeInteger(value.total) || value.total < 0) {
    throw new CatalogApiError('atomer_availability_response_invalid', 'The response does not match this preview page');
  }
  return value;
};
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
