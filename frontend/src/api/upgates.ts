// src/api/upgates.ts
// "Stiahnuť z Upgates" — preview and import of products from an Upgates shop

import { API_BASE, fetchJSON } from "./client";

export interface UpgatesNewProduct {
  code: string;
  title: string;
  manufacturer: string;
  variants_count: number;
  availability: string;
  stock: number | null;
}

export interface UpgatesPreview {
  shop: string;
  total_in_upgates: number;
  already_in_db: number;
  new_count: number;
  new_products: UpgatesNewProduct[];
}

export interface UpgatesImportResult {
  shop: string;
  created_products: number;
  created_variants: number;
  updated_products: number;
  content_saved: number;
  stock_initialized: number;
  skipped: { code: string; reason: string }[];
  message: string;
}

export interface UpgatesImportOptions {
  updateExisting?: boolean;
  includeStock?: boolean;
}

export async function getUpgatesPreview(shop: string): Promise<UpgatesPreview> {
  return fetchJSON<UpgatesPreview>(`${API_BASE}/shops/${encodeURIComponent(shop)}/upgates/products/preview`);
}

export async function importUpgatesProducts(
  shop: string,
  codes: string[],
  opts: UpgatesImportOptions = {},
): Promise<UpgatesImportResult> {
  return fetchJSON<UpgatesImportResult>(`${API_BASE}/shops/${encodeURIComponent(shop)}/upgates/products/import`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      codes,
      update_existing: !!opts.updateExisting,
      include_stock: opts.includeStock !== false,
    }),
  });
}
