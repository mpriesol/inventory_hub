import { hubRequest } from './access';

export interface OrderWebhookMutation {
  event: 'create' | 'update' | 'delete'; method: 'POST' | 'PUT'; intent_id: string; started_at: string;
}
export interface OrderWebhookSettings {
  enabled: boolean; revision: number; public_base_url: string; catchup_interval_seconds: number;
  registration_status: 'pending' | 'running' | 'registered' | 'failed' | 'disabled';
  registered_at: string | null; registration_checked_at: string | null; last_received_at: string | null; last_completed_at: string | null;
  next_registration_at: string | null; registration_attempts: number; last_error: string | null;
  registered_events: Partial<Record<'create' | 'update' | 'delete', number>>; secret_configured: boolean;
  pending_mutations?: OrderWebhookMutation[];
}
export interface OrderWebhookEvent {
  order_number: string; event: 'create' | 'update' | 'delete'; notification_at: string | null;
  last_received_at: string; last_completed_at: string | null; status: string; attempts: number; last_error: string | null;
  generation: number; notification_hash: string;
  resolution?: { request_id: string; action: NotificationResolutionAction; note: string; actor: string;
    applied_at: string; event_generation: number };
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
export const resolveOrderWebhookRegistration = (shop: string, expected_revision: number, mutation: OrderWebhookMutation, note: string) =>
  hubRequest<OrderWebhookStatus>(`${base}/resolve-registration`, { shop_code: shop, expected_revision, confirmed: true,
    event: mutation.event, intent_id: mutation.intent_id, remote_request_finished_confirmed: true, note });

export type NotificationResolutionAction = 'acknowledge_absent_delete' | 'reopen';
export interface NotificationResolutionOperation {
  request_id: string; shop_code: string; order_number: string; action: NotificationResolutionAction;
  note: string; actor: string; phase: 'prepared' | 'completed'; preview_hash: string; created_at: string; expires_at: string;
  event: Omit<OrderWebhookEvent, 'order_number' | 'resolution'> & { event: 'delete'; status: 'review' | 'completed' };
  after_status: 'completed' | 'review'; stock_effect: 'none';
  evidence: { kind: 'absent_delete' | 'reopen_manual_ack'; checked_at: string;
    active_absent: true | null; deleted_absent: true | null; no_local_history: true | null };
  result: null | { applied_at: string; event_status: 'completed' | 'review'; event_generation: number };
}
/** Persisted before preview; the returned hash is retained before an apply can be sent. */
export interface NotificationResolutionBinding {
  request_id: string; shop_code: string; order_number: string; action: NotificationResolutionAction; actor: string;
  note: string; preview_hash: string | null; event_generation: number; notification_hash: string;
}
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const hash = /^[0-9a-f]{64}$/;
const date = (value: unknown): value is string => typeof value === 'string' && Number.isFinite(Date.parse(value));
const nullableDate = (value: unknown) => value === null || date(value);
const invalidNotification = () => Object.assign(new Error('Invalid notification resolution response'), { code: 'order_webhook_resolution_invalid_response' });

export function isNotificationResolutionBinding(value: unknown): value is NotificationResolutionBinding {
  const item = value as NotificationResolutionBinding | null;
  return !!item && typeof item === 'object' && typeof item.request_id === 'string' && uuid.test(item.request_id)
    && typeof item.shop_code === 'string' && /^[a-z0-9][a-z0-9_-]{0,49}$/.test(item.shop_code)
    && typeof item.order_number === 'string' && item.order_number.length > 0 && item.order_number.length <= 100
    && ['acknowledge_absent_delete', 'reopen'].includes(item.action) && typeof item.actor === 'string' && !!item.actor
    && typeof item.note === 'string' && item.note.trim().length >= 10 && item.note.length <= 2000
    && (item.preview_hash === null || typeof item.preview_hash === 'string' && hash.test(item.preview_hash))
    && Number.isSafeInteger(item.event_generation) && item.event_generation > 0
    && typeof item.notification_hash === 'string' && hash.test(item.notification_hash);
}

export function checkedNotificationOperation(value: unknown, expected?: NotificationResolutionBinding,
  completed = false): NotificationResolutionOperation {
  const item = value as NotificationResolutionOperation | null;
  if (!item || typeof item !== 'object' || !uuid.test(item.request_id || '')
      || typeof item.shop_code !== 'string' || !/^[a-z0-9][a-z0-9_-]{0,49}$/.test(item.shop_code)
      || typeof item.order_number !== 'string' || !item.order_number || item.order_number.length > 100
      || !['acknowledge_absent_delete', 'reopen'].includes(item.action)
      || typeof item.note !== 'string' || item.note.trim().length < 10 || item.note.length > 2000
      || typeof item.actor !== 'string' || !item.actor || !hash.test(item.preview_hash || '')
      || !['prepared', 'completed'].includes(item.phase) || (completed && item.phase !== 'completed')
      || !date(item.created_at) || !date(item.expires_at) || Date.parse(item.expires_at) <= Date.parse(item.created_at)
      || item.stock_effect !== 'none' || item.after_status !== (item.action === 'reopen' ? 'review' : 'completed')
      || !item.event || item.event.event !== 'delete' || !Number.isSafeInteger(item.event.generation) || item.event.generation < 1
      || !hash.test(item.event.notification_hash || '') || !nullableDate(item.event.notification_at)
      || !date(item.event.last_received_at) || !nullableDate(item.event.last_completed_at)
      || !Number.isSafeInteger(item.event.attempts) || item.event.attempts < 0
      || !(item.event.last_error === null || typeof item.event.last_error === 'string')
      || item.event.status !== (item.action === 'reopen' ? 'completed' : 'review')
      || (item.action === 'acknowledge_absent_delete' && item.event.last_error !== 'order_webhook_order_not_found')
      || !item.evidence || !date(item.evidence.checked_at)
      || item.evidence.kind !== (item.action === 'reopen' ? 'reopen_manual_ack' : 'absent_delete')
      || [item.evidence.active_absent, item.evidence.deleted_absent, item.evidence.no_local_history]
        .some(proof => proof !== (item.action === 'reopen' ? null : true))
      || (item.phase === 'prepared' ? item.result !== null : (!item.result || !date(item.result.applied_at)
        || item.result.event_status !== item.after_status || item.result.event_generation !== item.event.generation))) throw invalidNotification();
  if (expected && (item.request_id !== expected.request_id || item.shop_code !== expected.shop_code
      || item.order_number !== expected.order_number || item.action !== expected.action || item.actor !== expected.actor
      || item.note !== expected.note || (expected.preview_hash !== null && item.preview_hash !== expected.preview_hash)
      || item.event.generation !== expected.event_generation || item.event.notification_hash !== expected.notification_hash)) throw invalidNotification();
  return item;
}

export const previewNotificationResolution = async (binding: NotificationResolutionBinding) =>
  checkedNotificationOperation(await hubRequest<unknown>(`${base}/notifications/preview`, {
    request_id: binding.request_id, shop_code: binding.shop_code, order_number: binding.order_number,
    action: binding.action, note: binding.note,
  }), binding);
export const applyNotificationResolution = async (binding: NotificationResolutionBinding) => {
  if (!binding.preview_hash) throw invalidNotification();
  return checkedNotificationOperation(await hubRequest<unknown>(`${base}/notifications/operations/${encodeURIComponent(binding.request_id)}/apply`, {
    shop_code: binding.shop_code, preview_hash: binding.preview_hash, confirmed: true,
  }), binding, true);
};
export const getNotificationResolution = async (binding: NotificationResolutionBinding, signal?: AbortSignal) =>
  checkedNotificationOperation(await hubRequest<unknown>(`${base}/notifications/operations/${encodeURIComponent(binding.request_id)}?shop_code=${encodeURIComponent(binding.shop_code)}`,
    undefined, signal), binding);
export async function getNotificationResolutionHistory(shop: string, order: string, signal?: AbortSignal) {
  const value = await hubRequest<{ shop_code: string; order_number: string; limit: number; operations: unknown[] }>(
    `${base}/notifications/history?shop_code=${encodeURIComponent(shop)}&order_number=${encodeURIComponent(order)}`, undefined, signal);
  if (!value || value.shop_code !== shop || value.order_number !== order || value.limit !== 50
      || !Array.isArray(value.operations) || value.operations.length > 50) throw invalidNotification();
  const operations = value.operations.map(item => checkedNotificationOperation(item, undefined, true));
  if (operations.some(item => item.shop_code !== shop || item.order_number !== order)
      || new Set(operations.map(item => item.request_id)).size !== operations.length) throw invalidNotification();
  return operations;
}
