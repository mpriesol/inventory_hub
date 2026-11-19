import { hubRequest, hubUser } from './access';
export interface AvailabilityOption { id: number; label: string; is_active: boolean; sort_order: number; revision: number }
export interface WarehouseOption { id: number; code: string; name: string; address: string | null; is_active: boolean; is_default: boolean; updated_at: string }
export interface ManualAvailability { label?: string; visible?: boolean; source: 'manual'; actor: string; updated_at: string }
const base = '/api/settings/catalogs';
export const listAvailabilityOptions = (signal?: AbortSignal, includeInactive = false) => hubRequest<{ items: AvailabilityOption[] }>(`${base}/availabilities${includeInactive ? '?include_inactive=true' : ''}`, undefined, signal);
export const listWarehouses = (signal?: AbortSignal, includeInactive = false) => hubRequest<{ items: WarehouseOption[] }>(`${base}/warehouses${includeInactive ? '?include_inactive=true' : ''}`, undefined, signal);
export type AvailabilityChange = { request_id: string; id: number | null; expected_revision: number; label: string; is_active: boolean; sort_order: number };
export type WarehouseChange = { request_id: string; code: string; expected_updated_at: string | null; name: string; address: string | null; is_active: boolean; is_default: boolean };
export const saveAvailabilityOption = (body: AvailabilityChange) => hubRequest<AvailabilityOption>(`${base}/availabilities`, body);
export const saveWarehouseOption = (body: WarehouseChange) => hubRequest<WarehouseOption>(`${base}/warehouses`, body);
export const getCatalogRequest = (requestId: string) => hubRequest<AvailabilityOption | WarehouseOption>(`${base}/requests/${encodeURIComponent(requestId)}`);
export interface ProductAvailabilityChange { product_id: number; expected_revision: number; label?: string | null; visible?: boolean | null; clear?: boolean }
export interface ProductAvailabilityInput { request_id: string; changes: ProductAvailabilityChange[] }
export const saveProductAvailability = (body: ProductAvailabilityInput) => hubRequest<{ request_id: string }>(`${base}/product-availability`, body);

// Frozen intent survives a lost response and reopening the same screen.
// It is scoped to the authenticated operator; credentials never enter storage.
export function catalogPendingKey(scope: string) {
  const actor = String(hubUser()?.id ?? hubUser()?.username ?? 'operator-token');
  return `catalog-pending:${encodeURIComponent(actor)}:${scope}`;
}
export function readCatalogPending<T extends { request_id: string }>(key: string): T | null {
  try {
    const value = JSON.parse(window.sessionStorage.getItem(key) || 'null');
    return value && typeof value === 'object' && /^[0-9a-f-]{36}$/i.test(value.request_id) ? value : null;
  } catch { return null; }
}
export function rememberCatalogPending(key: string, body: { request_id: string }) {
  const value = JSON.stringify(body);
  window.sessionStorage.setItem(key, value);
  if (window.sessionStorage.getItem(key) !== value) throw new Error('catalog_storage_unavailable');
}
export function clearCatalogPending(key: string, body: { request_id: string }) {
  try { if (window.sessionStorage.getItem(key) === JSON.stringify(body)) window.sessionStorage.removeItem(key); } catch { /* Completed requests can safely be read again. */ }
}
