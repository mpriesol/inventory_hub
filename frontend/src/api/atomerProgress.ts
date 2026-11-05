import { hubRequest, hubUpload } from './access';

export const progressStates = ['linked','needs_link','needs_card','needs_target','needs_both','review'] as const;
export const bikeStates = ['linked','unlinked','no_card','review'] as const;
export type ProgressRow = {
  family_key: string; family_name: string; variant_label:string; card_state:'exists'|'missing'|'review';
  shop_url:string|null; shop_admin_url:string|null; code: string; kind: 'variant' | 'standalone'; state: typeof progressStates[number]; name: string;
  parent: string; ean: string; hub_id: number | null; hub_sku: string | null; candidates: number[];
  product_id: string | null; variant_id: string | null; target_code: string | null; target_parent: string | null;
  target_ean: string | null; reasons: string[]; bike_state: typeof bikeStates[number];
  bike: {reason?:string; shop_url?:string|null; shop_admin_url?:string|null; code: string | null; parent: string | null; product_id: string | null; variant_id: string | null} | null;
};
export type ProgressRun = {id: number; checked_at: string; source_sha256: string; source_rows: number; imported_at: string;
  totals: {items: number; kinds: Record<string,number>; states: Record<string,number>; bike: Record<string,number>}};
export type ProgressFamily = {key:string; name:string; items:ProgressRow[]};
export type ProgressPage = {run: ProgressRun | null; total: number; families:number; groups:ProgressFamily[]};
const base = '/api/migration/atomer-progress';
export function readProgress(params: URLSearchParams, signal?: AbortSignal) {
  return hubRequest<ProgressPage>(base + '?' + params, undefined, signal);
}
export function refreshProgress(expectedRun: number, file?: File | null, staleOnly = false) {
  const form = new FormData();
  form.set('expected_run', String(expectedRun));
  if (file) form.set('source_file', file);
  if (staleOnly) form.set('only_if_stale','true');
  return hubUpload<{id: number}>(base + '/refresh', form);
}
