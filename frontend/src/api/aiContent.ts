import { CatalogApiError, ImportOptions, ImportPreview, ImportResult } from './catalog';

export const policyKeys = ['review_required', 'active_after_import', 'show_cost_estimate', 'confirm_import'] as const;
export type PolicyKey = typeof policyKeys[number];
export type AiPolicy = Partial<Record<PolicyKey, boolean | null>>;
export interface AiScope { shop: string; supplier: string; category: string; brand: string; product: string }
export interface AiRule { id: string; name: string; scope: AiScope; instructions: string; policy: AiPolicy; enabled: boolean; official_domains: string[]; import_policy?: { orderable?: string | null; unknown?: string | null; hide_zero_stock?: boolean | null; supplier_name?: string | null } }
export interface AiParameter { name: string; required: boolean; scope: 'parent' | 'variant'; values: string[]; unit: string; instructions: string }
export interface AiCategory { id: string; name: string; instructions: string; parameters: AiParameter[]; shop_categories: Record<string, string>; policy: AiPolicy; automatic_import_ready: boolean }
export interface AiBook { rules: AiRule[]; categories: AiCategory[] }
export interface AiRules { published_id: number; book: AiBook; used_usd: string; versions: { id: number; note: string; origin: string; created_at: string }[] }
export interface AiStatus { enabled: boolean; key_configured: boolean; access_configured: boolean; model: string; monthly_limit_usd: string; job_limit_usd: string }
export interface AiContent { title: string; short_description: string; long_description: string; seo_title: string; meta_description: string; h1_descriptor: string; future_name: string; h1_descr_suffix: string; parameters: { name: string; values: string[]; product_id: number | null }[]; evidence: { claim: string; source: string; quote: string }[]; warnings: string[]; missing_facts: string[] }
export interface AiUpdatePreview { availability_basis?: string; id: string; state: string; fields: string[]; before: Record<string, unknown>; after: Record<string, unknown>; expires_at: string }
interface AiJobBase { applied_rules?: {id:string; name:string; text:string}[]; resolved_import_policy?: AiRule['import_policy']; parameter_registry?: AiParameter[]; archived?: boolean; update_only?: boolean; source_kind?: 'catalog' | 'shop'; update_state?: string | null; update_preview?: AiUpdatePreview | null; update_result?: { status: string; fields: string[]; error?: string; mismatched_fields?: string[]; observed?: Record<string, unknown> }; id: string; batch_id: string; status: string; revision: number; shop: string; supplier: string; code: string; name: string; image: string | null; product_ids: number[]; use_ai: boolean; rules_version: number; category_profile: string; policy: AiPolicy; origins: Record<string, string>; estimate_usd: string; actual_usd: string | null; reserved_usd: string; checks: { errors?: string[]; warnings?: string[] }; error: string | null; preview_id: string | null; created_at: string; updated_at: string; facts?: { id: number; name: string; description: string; variant_attributes: { name: string; value: string }[] }[]; events?: { at: string; status: string; note: string }[]; preview?: ImportPreview; import_result?: ImportResult; options?: ImportOptions; research?: string }

export interface AiProposal { instructions: string; reason: string; questions: string[]; parameters: AiParameter[] | null }
export type AiJob = AiJobBase & ({ kind: 'product'; output?: AiContent } | { kind: 'rules'; output?: AiProposal });

// Deliberately memory-only: the OpenAI key is never sent to the frontend, and
// the Hub operator token does not persist in localStorage or URLs.
let accessToken = '';
export function unlockAi(value: string) { accessToken = value; }
export function aiUnlocked() { return !!accessToken; }
export async function aiRequest<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(`/api/ai-content${path}`, { method: body === undefined ? 'GET' : 'POST',
    headers: { Authorization: `Bearer ${accessToken}`, ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
    body: body === undefined ? undefined : JSON.stringify(body) });
  const data = await response.json();
  if (!response.ok) throw new CatalogApiError(data?.detail?.code || 'request_failed', typeof data?.detail?.message === 'string' ? data.detail.message : JSON.stringify(data?.detail || response.status));
  return data;
}
