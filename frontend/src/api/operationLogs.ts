import { hubRequest } from './access';

export interface OperationEvent {
  id: string; timestamp: string; component: string; event: string; level: 'info' | 'warning' | 'error';
  supplier?: string; run_id?: number; request_id?: string; direction?: 'inbound' | 'outbound' | 'internal';
  method?: string; target?: string; status_code?: number; duration_ms?: number;
  phase?: string; page?: number; observed_page?: number; rows?: number; error_code?: string; reason?: string;
}
export interface OperationLogPage { items: OperationEvent[]; next_cursor: string | null; max_bytes: number }
export function getOperationLogs(params: URLSearchParams, signal: AbortSignal) {
  return hubRequest<OperationLogPage>(`/api/logs/events?${params}`, undefined, signal);
}
