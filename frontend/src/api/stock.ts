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

export interface ProductDetail {
  sku: string;
  name: string;
  brand: string | null;
  category: string | null;
  weight_g: number | null;
  created_from_source: string | null;
  created_at: string | null;
  updated_at: string | null;
  validation_required: boolean;
  group: { code: string; name: string } | null;
  image_url: string | null;
  attributes: { name: string; value: string }[];
  identifiers: { type: string; value: string; is_primary: boolean }[];
  stock: { on_hand: number; reserved: number; available: number; avg_cost: number | null };
  shops: { shop: string; external_code: string; variant_code: string | null; shop_availability: string | null; shop_stock: number | null; last_pull_at: string | null }[];
}

export async function getProductDetail(sku: string): Promise<ProductDetail> {
  return fetchJSON<ProductDetail>(`${API_BASE}/stock/product/${encodeURIComponent(sku)}`);
}
