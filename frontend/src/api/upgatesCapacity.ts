import { hubRequest } from './access';

export type UpgatesPermission = 'deny' | 'read' | 'write' | 'all' | 'unknown';
export interface UpgatesCapacity {
  shop_code: string; observed_at: string;
  probe: { ok: boolean; http_status: number | null; error: string | null; requests_attempted: 0 | 1;
    latency_ms: number | null; target_fingerprint: string | null; write_tested: false;
    permissions: Record<'products' | 'orders' | 'order_statuses', UpgatesPermission> };
  catalog_probe?: { requested: true; ok: boolean; selected_products: number; selected_variants: number;
    planned_requests: number; requests_attempted: number | null; error: string | null };
  quota: { hour_limit: number | null; day_limit: number | null; hour_remaining: number | null; day_remaining: number | null;
    total_remaining: number | null; retry_after_seconds: number | null; theoretical_daily_max: number | null; headers_complete: boolean };
  projection: { captured_at: string; warehouse_code: string | null; listed_skus: number; confirmed_skus: number | null;
    eligible_skus: number | null; unpublishable_skus: number | null; eligibility_errors: { code: string; count: number }[];
    stock: { configured: boolean; enabled: boolean; authorized: boolean; server_enabled: boolean; interval_seconds: number | null;
      batch_size: number; call_model: 'batched_unchanged_fresh_reads_before_write';
      initial_batch_calls: number | null; singleton_pass_calls: number | null;
      unchanged_pass_calls: number | null; all_changed_pass_calls: number | null;
      daily_unchanged_if_enabled: number | null; daily_all_changed_if_enabled: number | null };
    orders: { collector_enabled: boolean; processing_mode: 'manual' | 'reserve' | 'fulfill'; processing_ready: boolean;
      processing_error: string | null; poll_interval_seconds: number | null; retry_minutes: number | null; full_check_hours: number | null;
      inbox_count: number; jobs_by_status: Record<'pending' | 'running' | 'completed' | 'retry' | 'review', number>;
      unprocessed_count: number; collector_empty_poll_calls: 2; collector_daily_if_enabled: number;
      retry_daily_if_processing: number; history_daily_if_processing: number };
    estimate_kind: 'theoretical_no_runtime_delay'; not_included: string[];
  };
}
export const getUpgatesCapacity = (shop: string, signal?: AbortSignal, includeProductSample = false) => hubRequest<UpgatesCapacity>(
  `/api/shops/${encodeURIComponent(shop)}/upgates/capacity${includeProductSample ? '?include_product_sample=true' : ''}`, undefined, signal);
