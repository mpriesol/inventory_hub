import { hubRequest } from './access';

export interface OrderWebhookSettings {
  enabled: boolean; revision: number; public_base_url: string; catchup_interval_seconds: number;
  registration_status: 'pending' | 'running' | 'registered' | 'failed' | 'disabled';
  registered_at: string | null; registration_checked_at: string | null; last_received_at: string | null; last_completed_at: string | null;
  next_registration_at: string | null; registration_attempts: number; last_error: string | null;
  registered_events: Partial<Record<'create' | 'update' | 'delete', number>>; secret_configured: boolean;
}
export interface OrderWebhookEvent {
  order_number: string; event: 'create' | 'update' | 'delete'; notification_at: string | null;
  last_received_at: string; last_completed_at: string | null; status: string; attempts: number; last_error: string | null;
}
export interface OrderWebhookStatus {
  shop: { code: string; name: string }; webhook: OrderWebhookSettings | null;
  pending_count: number; review_count: number; collector_enabled: boolean; connection_matches: boolean;
  events: OrderWebhookEvent[]; external_write_enabled: false;
}
export interface OrderWebhookConfigure {
  shop_code: string; expected_revision: number | null; confirmed: true; enabled: boolean;
  public_base_url: string; catchup_interval_seconds: number;
}
const base = '/api/order-webhooks';
export const getOrderWebhookStatus = (shop: string, signal?: AbortSignal) =>
  hubRequest<OrderWebhookStatus>(`${base}/status?shop_code=${encodeURIComponent(shop)}`, undefined, signal);
export const configureOrderWebhooks = (body: OrderWebhookConfigure) => hubRequest<OrderWebhookStatus>(`${base}/configure`, body);
export const registerOrderWebhooks = (shop: string, expected_revision: number) =>
  hubRequest<OrderWebhookStatus>(`${base}/register`, { shop_code: shop, expected_revision, confirmed: true });
export const retryOrderWebhooks = (shop: string, expected_revision: number) =>
  hubRequest<OrderWebhookStatus>(`${base}/retry`, { shop_code: shop, expected_revision, confirmed: true });
