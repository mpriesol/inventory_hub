import { hubRequest, hubUpload } from './access';

export const progressStates = ['linked','needs_link','needs_card','needs_target','needs_both','review'] as const;
export const bikeStates = ['linked','unlinked','no_card','review'] as const;
export type ProgressRow = {
  source_state?:{product:boolean|null; product_origin:string; variant:boolean|null; variant_origin:string; product_flags:Record<string,string>; variant_flags:Record<string,string>};
  work?:Work; family_key: string; family_name: string; variant_label:string; card_state:'exists'|'missing'|'review';
  shop_url:string|null; shop_admin_url:string|null; code: string; kind: 'variant' | 'standalone'; state: typeof progressStates[number]; name: string;
  parent: string; ean: string; hub_id: number | null; hub_sku: string | null; candidates: number[];
  product_id: string | null; variant_id: string | null; target_code: string | null; target_parent: string | null;
  target_ean: string | null; reasons: string[]; bike_state: typeof bikeStates[number];
  bike: {reason?:string; shop_url?:string|null; shop_admin_url?:string|null; code: string | null; parent: string | null; product_id: string | null; variant_id: string | null} | null;
};
export type ProgressRun = {id: number; checked_at: string; source_sha256: string; source_rows: number; imported_at: string;
  totals: {items: number; review_targets?:number; kinds: Record<string,number>; states: Record<string,number>; bike: Record<string,number>}};
export type ProgressFamily = {key:string; name:string; work?:Work; extra_count?:number; items:ProgressRow[]};
export type ProgressPage = {run: ProgressRun | null; total: number; families:number; groups:ProgressFamily[]};
const base = '/api/migration/atomer-progress';
export function readProgress(params: URLSearchParams, signal?: AbortSignal) {
  return hubRequest<ProgressPage>(base + '?' + params, undefined, signal);
}
export function refreshProgress(expectedRun: number, file?: File | null, staleOnly = false, reviewFile?:File|null, adminFile?:File|null) {
  const form = new FormData();
  form.set('expected_run', String(expectedRun));
  if (adminFile) form.set('admin_file',adminFile);
  if (reviewFile) form.set('review_file',reviewFile);
  if (file) form.set('source_file', file);
  if (staleOnly) form.set('only_if_stale','true');
  return hubUpload<{id: number}>(base + '/refresh', form);
}

export type WorkStatus = 'open'|'queued'|'working'|'blocked'|'done';
export type ReviewKind = 'family'|'item'|'extra';
export type Scope = 'default'|'both'|'xtrek'|'biketrek';
export type Decision = 'undecided'|'yes'|'no';
export type Work = {revision:number; status:WorkStatus; scope:Scope; decision:Decision; note:string; instruction:string; result:string};
export const emptyWork: Work = {revision:0,status:'open',scope:'default',decision:'undecided',note:'',instruction:'',result:''};
export type Extra = {key:string; family_key:string; name:string; code:string; parent:string; source_parent:string; product_id:string;
  variant_id:string; ean:string; axes:Record<string,string[]>; check:string; warnings:string[]; alternatives:string[]; stock:number|null;
  card_ids:number[]; hub_sku:string|null; shop_url:string|null; shop_admin_url:string|null; work:Work};
export type Command = 'save'|'done'|'reopen'|'claim'|'complete'|'block'|'retry';
export type Subject = {kind:ReviewKind; key:string; name:string; work:Work; target?:Extra|null; suggestions?:string[]};
export type ReviewEdit = {kind:ReviewKind; key:string; revision:number; run_id:number; command:Command;
  scope?:Scope; decision?:Decision; note?:string; instruction?:string; result?:string};
export function readTargets(params:URLSearchParams,signal?:AbortSignal) {
  return hubRequest<{total:number;run_id:number;items:Extra[]}>(base+'/targets?'+params,undefined,signal);
}
export function readQueue(params:URLSearchParams,signal?:AbortSignal) {
  return hubRequest<{total:number;run_id:number;items:Subject[]}>(base+'/work?'+params,undefined,signal);
}
export function editWork(edit:ReviewEdit) { return hubRequest<Work>(base+'/work',edit); }
