import { hubRequest } from './access';

export interface CoverageCheck { target: string; rows: number; previous_rows: number; required_rows: number; min_coverage_percent: number }
export interface AttentionItem {
  id: number; kind: string; scope: string; source: string; severity: 'warning' | 'error'; revision: number;
  active: boolean; ignored_at: string | null; first_seen: string; last_seen: string; resolved_at: string | null;
  occurrences: number; episode: number; link: string; settings_link: string | null; log_link: string;
  details: { run_id?: number; total?: number; accepted?: number; rejected?: number; unknown?: number;
    error_code?: string | null; warnings?: string[]; coverage?: CoverageCheck[] };
}
export interface AttentionPage { items: AttentionItem[]; total: number; offset: number; limit: number; sources: string[] }
export interface AttentionRule { kind: string; scope: string; revision: number; enabled: boolean; min_occurrences: number }
export type AttentionState = 'active' | 'ignored' | 'muted' | 'resolved';
export const getAttention = (query: URLSearchParams, signal?: AbortSignal) => hubRequest<AttentionPage>(`/api/attention?${query}`, undefined, signal);
export const getAttentionRules = (signal?: AbortSignal) => hubRequest<{ items: AttentionRule[] }>('/api/attention/rules', undefined, signal);
export const actOnAttention = (item: AttentionItem, action: 'ignore' | 'restore', signal?: AbortSignal) =>
  hubRequest(`/api/attention/${item.id}/action`, { action, expected_revision: item.revision }, signal);
export const saveAttentionRule = (rule: AttentionRule, signal?: AbortSignal) => hubRequest<AttentionRule>(
  `/api/attention/rules/${encodeURIComponent(rule.kind)}?${new URLSearchParams({ scope: rule.scope })}`,
  { expected_revision: rule.revision, enabled: rule.enabled, min_occurrences: rule.min_occurrences }, signal, 'PUT');
