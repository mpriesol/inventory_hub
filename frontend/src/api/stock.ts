// src/api/stock.ts
// Real stock data from /stock endpoints (stock_balances + products in DB)

import { API_BASE, fetchJSON } from "./client";

export interface StockItem {
  sku: string;
  name: string;
  brand: string;
  on_hand: number;
  reserved: number;
  available: number;
  avg_cost: number;
  total_value: number;
  low_stock: boolean;
}

export interface StockSummary {
  products_total: number;
  products_with_stock: number;
  inventory_value: number;
  reserved_total: number;
  low_stock_count: number;
}

export async function getStockItems(): Promise<StockItem[]> {
  return fetchJSON<StockItem[]>(`${API_BASE}/stock/items`);
}

export async function getStockSummary(): Promise<StockSummary> {
  return fetchJSON<StockSummary>(`${API_BASE}/stock/summary`);
}
