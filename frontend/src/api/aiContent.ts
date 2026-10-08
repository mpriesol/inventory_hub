import { ImportOptions, ImportPreview, ImportResult } from './catalog';
import { hubRequest } from './access';
export { unlockHub as unlockAi, hubUnlocked as aiUnlocked } from './access';

export const policyKeys = ['review_required', 'active_after_import', 'show_cost_estimate', 'confirm_import'] as const;
export type PolicyKey = typeof policyKeys[number];
export type AiPolicy = Partial<Record<PolicyKey, boolean | null>>;
export interface AiScope { shop: string; supplier: string; category: string; brand: string; product: string }
export interface AiRule { category_profiles?: string[]; id: string; name: string; scope: AiScope; instructions: string; policy: AiPolicy; enabled: boolean; official_domains: string[]; import_policy?: { orderable?: string | null; unknown?: string | null; hide_zero_stock?: boolean | null; supplier_name?: string | null } }
export interface AiParameter { name: string; required: boolean; scope: 'parent' | 'variant' | 'choice'; approved?: boolean; values: string[]; unit: string; instructions: string }
export interface AiCategory { registry_status?: "approved" | "draft" | "mixed" | "missing"; shop_category_matches?: Record<string, string[]>; id: string; name: string; instructions: string; parameters: AiParameter[]; shop_categories: Record<string, string>; policy: AiPolicy; automatic_import_ready: boolean }
export interface AiBook { rules: AiRule[]; categories: AiCategory[] }
export interface AiRules { published_id: number; book: AiBook; used_usd: string; versions: { id: number; note: string; origin: string; created_at: string }[] }
export interface AiStatus { enabled: boolean; key_configured: boolean; access_configured: boolean; model: string; monthly_limit_usd: string; job_limit_usd: string }
export interface AiModelOption { id: string; label: string; input_usd_per_million: string; cached_input_usd_per_million: string; cache_write_usd_per_million: string; output_usd_per_million: string; web_search_usd: string; context_tokens: number; long_context_threshold: number; long_context_input_usd_per_million: string; long_context_cached_input_usd_per_million: string; long_context_cache_write_usd_per_million: string; long_context_output_usd_per_million: string }
export interface AiSettings { revision: number; model: string; source: 'server' | 'hub'; models: AiModelOption[]; updated_at: string | null }
export interface AiComposition { version: string; category_profile: string | null; source_characters: number; selected_characters: number; omitted_characters: number; added_characters?: number; included_rules: number; omitted_rules: number; entries: { id: string; name: string; status: 'included' | 'filtered' | 'omitted'; source_characters: number; selected_characters: number; reasons: string[]; source_sha256: string; selected_sha256: string }[] }
export const readAiSettings = () => hubRequest<AiSettings>('/api/ai-content/settings');
export const saveAiSettings = (settings: AiSettings, model: string) => hubRequest<AiSettings>('/api/ai-content/settings', { expected_revision: settings.revision, model }, undefined, 'PUT');
export interface AiContent { title: string; short_description: string; long_description: string; seo_title: string; meta_description: string; h1_descriptor: string; future_name: string; h1_descr_suffix: string; parameters: { name: string; values: string[]; product_id: number | null }[]; evidence: { claim: string; source: string; quote: string }[]; warnings: string[]; missing_facts: string[] }
export interface AiUpdatePreview { availability_basis?: string; sending_at?: string; id: string; state: string; fields: string[]; before: Record<string, unknown>; after: Record<string, unknown>; expires_at: string }
export interface AiImportStatus { draft_id: string; revision: number; linked: boolean; profile_matches?: boolean; auto_apply?: boolean; auto_publish?: boolean; automation_paused?: boolean; ai_applied: boolean; publication_finished: boolean; publication_status: string | null }
interface AiJobBase { model?: string; category_profile_source?: string; category_profile_name?: string; composition?: AiComposition; category_selection?: {category_code: string; profile_id: string; path: string; reason: string}; applied_rules?: {id:string; name:string; text:string}[]; resolved_import_policy?: AiRule['import_policy']; parameter_registry?: AiParameter[]; archived?: boolean; update_only?: boolean; source_kind?: 'catalog' | 'shop' | 'import_draft'; staging_id?: string | null; staging?: AiImportStatus | null; update_state?: string | null; update_preview?: AiUpdatePreview | null; update_result?: { status: string; fields: string[]; error?: string; mismatched_fields?: string[]; observed?: Record<string, unknown> }; id: string; batch_id: string; status: string; revision: number; shop: string; supplier: string; code: string; name: string; image: string | null; product_ids: number[]; use_ai: boolean; rules_version: number; category_profile: string; policy: AiPolicy; origins: Record<string, string>; estimate_usd: string; actual_usd: string | null; reserved_usd: string; checks: { manual_overrides?: string[]; errors?: string[]; warnings?: string[]; evidence_errors?: { index: number; source: string; reason: string }[] }; usage?: { opened_sources?: string[] }; error: string | null; preview_id: string | null; created_at: string; updated_at: string; facts?: { id: number; name: string; description: string; variant_attributes: { name: string; value: string }[] }[]; events?: { at: string; status: string; note: string }[]; preview?: ImportPreview; import_result?: ImportResult; options?: ImportOptions; research?: string }

export interface AiProposal { instructions: string; reason: string; questions: string[]; parameters: AiParameter[] | null }
export type AiJob = AiJobBase & ({ kind: 'product'; output?: AiContent } | { kind: 'rules'; output?: AiProposal });

export async function aiRequest<T>(path: string, body?: unknown): Promise<T> {
  return hubRequest<T>(`/api/ai-content${path}`, body);
}

// AI readiness and shop delivery are separate states for an import draft.
export function aiContentStatusKey(job: AiJob, status = job.status): string {
  return job.staging_id && ['preparing_import', 'ready', 'completed'].includes(status)
    ? `ai.staging.states.${status}` : `ai.states.${status}`;
}
export function aiJobStatusKey(job: AiJob): string {
  if (job.staging_id) {
    if (job.staging?.linked === false) return 'ai.staging.replaced';
    if (job.staging?.publication_status) return `productImport.delivery.${job.staging.publication_status}`;
    return aiContentStatusKey(job);
  }
  const update = job.update_preview?.state || job.update_state;
  if (['sending', 'uncertain', 'rejected'].includes(update || '')) return `ai.updateStates.${update}`;
  if (job.update_only && job.status === 'exists') return update === 'completed' ? 'ai.updatedExisting' : 'ai.readyForUpdate';
  return `ai.states.${job.status}`;
}
export function aiJobNextKey(job: AiJob): string {
  if (job.staging_id) {
    if (job.staging?.linked === false) return 'ai.staging.replacedHelp';
    if (job.staging?.publication_status === 'completed') return job.staging.ai_applied ? 'ai.staging.delivered' : 'ai.staging.deliveredWithoutAi';
    if (job.staging?.publication_status) return 'ai.staging.checkDelivery';
    if (job.staging?.profile_matches === false) return 'ai.staging.profileChanged';
    if (job.staging?.automation_paused) return 'ai.staging.automationPaused';
    if (job.staging?.auto_apply && ['ready', 'preparing_import'].includes(job.status)) return 'ai.staging.autoApplying';
    if (job.staging?.auto_publish && ['completed', 'import_queued', 'importing'].includes(job.status)) return 'ai.staging.autoPublishing';
    if (['preparing_import', 'ready', 'completed', 'review', 'blocked'].includes(job.status)) return `ai.staging.next.${job.status}`;
  }
  const update = job.update_preview?.state || job.update_state;
  if (['sending', 'uncertain', 'rejected'].includes(update || '')) return `ai.updateStates.${update}`;
  return job.update_only ? `ai.updateNext.${update === 'completed' ? 'completed' : job.status}` : `ai.next.${job.status}`;
}
