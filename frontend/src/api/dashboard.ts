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

// Get dashboard overview stats
// For now, we aggregate from existing endpoints
export async function getDashboardStats(): Promise<DashboardStats> {
  try {
    // Get invoices from paul-lange and count only unprocessed ones
    const invoicesRes = await getInvoicesIndex('paul-lange');
    const pendingInvoices = (invoicesRes?.items || [])
      .filter(inv => inv.status !== 'processed')
      .length;

    // TODO: When we have PostgreSQL, these will come from real endpoints
    // For now, return mock data combined with real pending invoices
    return {
      totalProducts: 1247,
      lowStockCount: 23,
      openOrders: 8,
      inventoryValue: 45320,
      pendingInvoices,
      syncStatus: {
        percentage: 98,
        lastSync: new Date(Date.now() - 5 * 60 * 1000).toISOString(),
        shopsConnected: 2,
      },
    };
  } catch (error) {
    console.error('Failed to fetch dashboard stats:', error);
    // Return defaults on error
    return {
      totalProducts: 0,
      lowStockCount: 0,
      openOrders: 0,
      inventoryValue: 0,
      pendingInvoices: 0,
      syncStatus: {
        percentage: 0,
        lastSync: null,
        shopsConnected: 0,
      },
    };
  }
}

// Get recent activity
export async function getRecentActivity(): Promise<ActivityItem[]> {
  // TODO: Implement real activity log from backend
  // For now return mock data
  return [
    {
      id: '1',
      timestamp: new Date().toISOString(),
      type: 'receiving',
      message: 'Príjem dokončený: F2026010234 (45 položiek)',
    },
    {
      id: '2',
      timestamp: new Date(Date.now() - 2 * 60 * 60 * 1000).toISOString(),
      type: 'sync',
      message: 'Sklad synchronizovaný do BikeTrek (1,247 produktov)',
    },
    {
      id: '3',
      timestamp: new Date(Date.now() - 5 * 60 * 60 * 1000).toISOString(),
      type: 'invoice',
      message: 'Nová faktúra indexovaná: F2026010235',
    },
    {
      id: '4',
      timestamp: new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString(),
      type: 'count',
      message: 'Inventúra dokončená (odchýlka: 3 položky)',
    },
  ];
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
