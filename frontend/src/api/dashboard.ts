// Dashboard reads local Hub data only; no Upgates request is made here.
import { API_BASE, fetchJSON } from "./client";
import { hubUnlocked } from './access';
import { getMovements } from './stockHistory';

export interface DashboardStats {
  totalProducts: number;
  lowStockCount: number;
  openOrders: number;
  inventoryValue: number | null;
  onHand: number;
  reserved: number;
  available: number;
  quarantined: number;
}
export interface ActivityItem {
  id: string; timestamp: string; sku: string; name: string; quantity: string;
  movementType: string; reference: string | null;
}
export async function getDashboardStats(): Promise<DashboardStats> {
  const value = await fetchJSON<{
    products_total: number; inventory_value: number | null; low_stock_count: number;
    open_managed_orders: number; on_hand_total: number; reserved_total: number;
    available_total: number; quarantined_total: number;
  }>(`${API_BASE}/stock/summary`);
  return {
    totalProducts: value.products_total, inventoryValue: value.inventory_value,
    lowStockCount: value.low_stock_count, openOrders: value.open_managed_orders,
    onHand: value.on_hand_total, reserved: value.reserved_total,
    available: value.available_total, quarantined: value.quarantined_total,
  };
}
export async function getRecentActivity(): Promise<ActivityItem[]> {
  if (!hubUnlocked()) return [];
  const value = await getMovements({q: '', sku: '', warehouse_code: '', movement_type: '', date_from: '', date_to: ''}, 1, 25);
  return value.items.slice(0, 6).map(row => ({ id: String(row.id), timestamp: row.created_at,
    sku: row.sku, name: row.product_name, quantity: row.quantity, movementType: row.movement_type,
    reference: row.reference_label || row.reference_id,
  }));
}

// Suppliers list
export interface Supplier {
  name: string;
  supplier_code: string;
  adapter: string;
}

export async function getSuppliers(): Promise<Supplier[]> {
  return fetchJSON<Supplier[]>(`${API_BASE}/suppliers`);
}
