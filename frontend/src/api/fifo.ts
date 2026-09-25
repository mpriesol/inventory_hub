import { hubRequest } from './access';
export type CostStatus = 'known' | 'provisional' | 'unknown';
export interface FifoLayer {
  id: number; root_cost_layer_id: number; receipt_movement_id: number | null;
  physical_received_at: string; quantity_original: string; quantity_remaining: string;
  unit_cost: string | null; cost_status: CostStatus; stock_status: string; cost_revision: number;
  provenance: Record<string, unknown>;
}
export interface FifoStock {
  product_id: number; sku: string; warehouse: { id: number; code: string; name: string };
  balance: { qty_on_hand: string; qty_reserved: string; qty_quarantined: string; qty_available: string; last_purchase_price?: string | null; last_purchase_at?: string | null } | null;
  tracking_confirmed?: boolean;
  valuation: { mode: 'fifo' | 'legacy' | 'missing' | 'unconfirmed'; revision: number; known_value: string | null;
    provisional_value: string | null; unknown_qty: string | null; provisional_qty: string | null;
    quarantined_qty: string | null; value_complete: boolean; avg_cost: string | null; total_value: string | null;
    next_layer?: { id: number; unit_cost: string | null; cost_status: CostStatus; physical_received_at: string } | null;
    last_purchase_cost_status?: CostStatus | 'legacy' };
  snapshot_hash: string; layers: FifoLayer[]; total: number; limit: number; offset: number;
}
export interface FifoMovement {
  id: number; movement_type: string; quantity: string; unit_cost: string | null; total_cost: string | null;
  balance_after: string; reference_id: string | null; reference_type: string | null; created_at: string;
}
export interface FifoAllocation {
  id: number; layer_id: number; sequence: number; quantity: string; returned_quantity: string;
  unit_cost_at_issue: string | null; total_cost_at_issue: string | null; cost_status_at_issue: CostStatus;
  unit_cost_current: string | null; total_cost_current: string | null; cost_status_current: CostStatus;
  net_cost_current?: string | null;
}
export interface FifoReturnOptions {
  issue_movement_id: number; issued_quantity: string; returned_quantity: string; returnable_quantity: string;
  allocations: FifoAllocation[];
}
export interface FifoCostOptions {
  root_layer_id: number; revision: number; unit_cost: string | null; cost_status: CostStatus;
  allocations: FifoAllocation[];
  net_consumption: { quantity: string; known_cost: string; provisional_cost: string; total_cost: string | null; value_complete: boolean };
  revisions: { revision: number; previous_cost: string | null; new_cost: string | null; new_status: string;
    document_reference: string; reason: string; created_at: string }[];
}
export interface FifoPreview { id: string; status: string; preview_hash: string; preview_data: Record<string, unknown>; result: unknown }
const scope = (product: number, warehouse: string, offset = 0) => `product_id=${product}&warehouse_code=${encodeURIComponent(warehouse)}&limit=50&offset=${offset}`;
export const fifoOptions = (signal?: AbortSignal) => hubRequest<{ warehouses: { id: number; code: string; name: string }[] }>('/api/fifo/options', undefined, signal);
export const fifoStock = (product: number, warehouse: string, offset = 0, signal?: AbortSignal) => hubRequest<FifoStock>(`/api/fifo/stock?${scope(product, warehouse, offset)}`, undefined, signal);
export const fifoHistory = (product: number, warehouse: string, offset = 0, signal?: AbortSignal) => hubRequest<{ movements: FifoMovement[]; total: number }>(`/api/fifo/history?${scope(product, warehouse, offset)}`, undefined, signal);
export const fifoReturnOptions = (movement: number) => hubRequest<FifoReturnOptions>(`/api/fifo/issues/${movement}/return-options`);
export const fifoCostOptions = (layer: number) => hubRequest<FifoCostOptions>(`/api/fifo/costs/${layer}`);
export const fifoCommand = <T,>(path: string, body: unknown) => hubRequest<T>(path, body);
