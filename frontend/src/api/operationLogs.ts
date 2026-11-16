import { hubRequest } from './access';

export interface OperationEvent {
  id: string; timestamp: string; component: string; event: string; level: 'info' | 'warning' | 'error';
  supplier?: string; run_id?: number; attention_id?: number; rule_code?: string; scope?: string; source?: string; occurrences?: number; request_id?: string; direction?: 'inbound' | 'outbound' | 'internal';
  method?: string; target?: string; status_code?: number; duration_ms?: number;
  phase?: string; page?: number; observed_page?: number; rows?: number; accepted?: number; rejected?: number; error_code?: string; reason?: string;
  previous_rows?: number; required_rows?: number; min_coverage_percent?: number;
  catalog_rows?: number; stock_rows?: number; b2b_rows?: number; matched_rows?: number; unknown?: number;
}
export interface OperationLogPage { items: OperationEvent[]; next_cursor: string | null; max_bytes: number }
export function getOperationLogs(params: URLSearchParams, signal: AbortSignal) {
  return hubRequest<OperationLogPage>(`/api/logs/events?${params}`, undefined, signal);
}
