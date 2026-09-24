import { API_BASE } from './client';
import { hubRequest } from './access';

export interface Movement {
  id: number; product_id: number; sku: string; product_name: string;
  warehouse_code: string; warehouse_name: string; movement_type: string;
  quantity: string; balance_before: string; balance_after: string;
  unit_cost: string | null; total_cost: string | null; cost_basis: string;
  reason: string | null; reference_type: string | null; reference_id: string | null;
  reference_source: string | null; reference_label: string | null; document: string | null;
  shop_code: string | null; supplier_code: string | null; created_by: string; created_at: string;
}
export interface MovementPage {
  items: Movement[]; total: number; page: number; page_size: number; snapshot_id: number;
  source: 'hub'; upgates_calls: 0;
}
export interface MovementFilters {
  q: string; sku: string; warehouse_code: string; movement_type: string; date_from: string; date_to: string;
}
export interface HistoryOptions { warehouses: { code: string; name: string }[]; movement_types: string[]; date_timezone: string }
export const getHistoryOptions = (signal?: AbortSignal) => hubRequest<HistoryOptions>(`${API_BASE}/stock-history/options`, undefined, signal);
export function getMovements(filters: MovementFilters, page = 1, pageSize = 50, snapshotId?: number, signal?: AbortSignal) {
  const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
  Object.entries(filters).forEach(([key, value]) => { if (value) params.set(key, value); });
  if (snapshotId !== undefined) params.set('snapshot_id', String(snapshotId));
  return hubRequest<MovementPage>(`${API_BASE}/stock-history/movements?${params}`, undefined, signal);
}
