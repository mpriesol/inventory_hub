// src/api/dashboard.ts
// API calls for dashboard KPIs and stats

import { API_BASE, fetchJSON } from "./client";
import { getInvoicesIndex, type InvoiceIndexItem } from "./invoices";

export interface DashboardStats {
  totalProducts: number;
  lowStockCount: number;
  openOrders: number;
  inventoryValue: number;
  pendingInvoices: number;
  syncStatus: {
    percentage: number;
    lastSync: string | null;
    shopsConnected: number;
  };
}

export interface ActivityItem {
  id: string;
  timestamp: string;
  type: 'receiving' | 'sync' | 'invoice' | 'count' | 'other';
  message: string;
  details?: Record<string, any>;
}

// Get dashboard overview stats — real numbers from DB (/stock/summary)
// plus real pending invoices from the invoice index.
export async function getDashboardStats(): Promise<DashboardStats> {
  const empty: DashboardStats = {
    totalProducts: 0,
    lowStockCount: 0,
    openOrders: 0,
    inventoryValue: 0,
    pendingInvoices: 0,
    syncStatus: { percentage: 0, lastSync: null, shopsConnected: 0 },
  };

  let pendingInvoices = 0;
  try {
    const invoicesRes = await getInvoicesIndex('paul-lange');
    pendingInvoices = (invoicesRes?.items || [])
      .filter(inv => inv.status !== 'processed')
      .length;
  } catch (error) {
    console.error('Failed to fetch invoices index:', error);
  }

  try {
    const summary = await fetchJSON<{
      products_total: number;
      inventory_value: number;
      low_stock_count: number;
    }>(`${API_BASE}/stock/summary`);
    return {
      ...empty,
      totalProducts: summary.products_total,
      lowStockCount: summary.low_stock_count,
      inventoryValue: summary.inventory_value,
      pendingInvoices,
      // openOrders / syncStatus: no orders module and no Upgates API sync
      // exist yet — keep honest zeros instead of fake numbers.
    };
  } catch (error) {
    console.error('Failed to fetch stock summary:', error);
    return { ...empty, pendingInvoices };
  }
}

// Get recent activity — no backend activity log exists yet.
// Returns empty list (UI shows "no activity") instead of fake entries.
// TODO: implement real audit feed (sync_log / receiving sessions / uploads).
export async function getRecentActivity(): Promise<ActivityItem[]> {
  return [];
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
