import { hubRequest } from './access';

export type OrderStockAction = 'reserve' | 'issue' | 'cancel' | 'review';
export interface OrderStockPolicy {
  warehouse_id: number; warehouse_code: string; starts_at: string; revision: number;
  status_actions: Record<string, OrderStockAction>;
}
export interface OrderStockOptions {
  shop: { id: number; code: string; name: string };
  warehouses: { id: number; code: string; name: string }[];
  statuses: { id: number; name: string; type: string }[];
  status_hash: string; policy: OrderStockPolicy | null; suggested_actions: Record<string, OrderStockAction>;
  allowed_actions: Record<string, OrderStockAction[]>;
}
export interface OrderStockError { code: string; sku?: string; line_key?: string; product_id?: number; [key: string]: unknown }
export interface OrderStockLine {
  line_key: string; product_id: number; sku: string; quantity: string;
  old_allocation: string; allocation: string; shortage: string;
}
export interface OrderStockEffect {
  product_id: number; sku: string; qty_on_hand: string; qty_reserved: string; avg_cost: string | null; total_value: string | null;
  old_allocation: string; allocation: string; shortage: string; issue_quantity: string; issue_cost: string | null;
  qty_on_hand_after: string; qty_reserved_after: string; total_value_after: string | null;
  valuation_mode?: string; value_complete?: boolean; qty_quarantined?: string;
}
export interface OrderStockExcluded {
  line_key: string; code?: string; title?: string; name?: string; quantity?: string; classification?: string; reason?: string; reasons?: string[];
}
export interface OrderStockResult {
  order_id: number; order_number: string; action: string; stock_state: string; revision: number;
  movements_created: number; movement_ids: number[]; lines: OrderStockLine[]; effects: OrderStockEffect[]; excluded_lines: OrderStockExcluded[];
}
export interface OrderStockPreview {
  id: string; preview_hash: string; status: 'prepared' | 'completed'; created_at: string; expires_at: string;
  shop_code: string; warehouse: { id: number; code: string; name: string };
  source: { order_number: string; uuid: string; created_at: string; updated_at: string; origin: string; status_id: number; paid: boolean; resolved: boolean; lines: { line_key: string; code: string; title: string; sku?: string | null; product_id?: number | null }[] };
  action: 'reserve' | 'cancel' | 'issue' | 'noop'; order_revision: number; policy_revision: number;
  plan: { ready: boolean; errors: OrderStockError[]; lines: OrderStockLine[]; effects: OrderStockEffect[]; excluded_lines: OrderStockExcluded[] };
  result: OrderStockResult | null;
}
export interface OrderStockPreviewResponse { ready: boolean; errors: OrderStockError[]; preview: OrderStockPreview | null }
export interface OrderStockPreviewInfo { id: string; status: string; shop_code: string; order_number?: string; source?: { order_number: string }; action: string; created_at: string; expires_at: string }
const base = '/api/order-stock';
export const getOrderStockOptions = (shop: string, signal?: AbortSignal) => hubRequest<OrderStockOptions>(`${base}/options?shop_code=${encodeURIComponent(shop)}`, undefined, signal);
export const configureOrderStock = (body: { shop_code: string; warehouse_code: string; status_hash: string; status_actions: Record<string, OrderStockAction>; confirmed: true }) => hubRequest<OrderStockOptions>(`${base}/configure`, body);
export const previewOrderStock = (body: { request_id: string; shop_code: string; order_number: string }, signal?: AbortSignal) => hubRequest<OrderStockPreviewResponse>(`${base}/preview`, body, signal);
export const recentOrderStock = (shop: string, signal?: AbortSignal) => hubRequest<{ previews: OrderStockPreviewInfo[] }>(`${base}/previews?shop_code=${encodeURIComponent(shop)}`, undefined, signal);
export const recoverOrderStock = (id: string, signal?: AbortSignal) => hubRequest<OrderStockPreview>(`${base}/previews/${encodeURIComponent(id)}`, undefined, signal);
export const applyOrderStock = (preview: OrderStockPreview, physical: boolean) => hubRequest<OrderStockResult>(`${base}/previews/${encodeURIComponent(preview.id)}/apply`, {
  preview_hash: preview.preview_hash, confirmed: true, physical_confirmed: physical,
});
