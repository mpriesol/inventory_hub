// src/api/suppliers.ts
// API client for suppliers management

import { API_BASE, fetchJSON } from './client';

// -----------------------------------------------------------------------------
// Types
// -----------------------------------------------------------------------------
export interface SupplierSummary {
  code: string;
  name: string;
  is_active: boolean;
  product_prefix: string;
  invoice_count: number;
  feed_mode: 'remote' | 'local' | 'none';
  download_strategy: 'web' | 'manual' | 'api' | 'disabled' | 'paul-lange-web' | 'northfinder-web';
  last_invoice_date: string | null;
  last_feed_sync: string | null;
}

export interface SupplierHistoryEntry {
  version: string;
  timestamp: string;
  size_bytes: number;
  changes_summary: string | null;
}

export interface ValidationResult {
  valid: boolean;
  errors: string[];
  warnings: string[];
  feed_url_reachable: boolean | null;
  login_url_reachable: boolean | null;
}

export interface SupplierConfig {
  name?: string;
  is_active?: boolean;
  feeds: {
    current_key: string;
    sources: Record<string, {
      mode: 'remote' | 'local';
      local_path: string | null;
      remote: {
        url: string;
        method: string;
        headers: Record<string, string>;
        params: Record<string, string>;
        auth: {
          mode: 'none' | 'basic' | 'bearer' | 'form';
          login_url?: string;
          user_field?: string;
          pass_field?: string;
          username?: string;
          password?: string;
          cookie?: string;
          basic_user?: string;
          basic_pass?: string;
          token?: string;
          header_name?: string;
          insecure_all?: boolean;
        };
      };
    }>;
  };
  invoices: {
    layout: 'flat' | 'yearly';
    months_back_default: number;
    download: {
      strategy: string;
      web: {
        login: {
          mode: string;
          login_url: string;
          user_field: string;
          pass_field: string;
          username: string;
          password: string;
          cookie: string;
          basic_user: string;
          basic_pass: string;
          token: string;
          header_name: string;
          insecure_all: boolean;
        };
        base_url: string;
        notes: string;
      };
    };
  };
  adapter_settings: {
    currency?: string;
    vat_rate?: number;
    mapping: {
      invoice_to_canon: Record<string, string | null>;
      postprocess: {
        unit_price_source?: string;
        product_code_prefix?: string;
      };
      canon_to_upgates: Record<string, unknown>;
    };
  };
}

export interface CreateSupplierRequest {
  code: string;
  name: string;
  product_prefix?: string;
  download_strategy?: string;
}

// -----------------------------------------------------------------------------
// API Functions
// -----------------------------------------------------------------------------

/**
 * List all suppliers with summary info
 */
export async function listSuppliers(): Promise<SupplierSummary[]> {
  return fetchJSON<SupplierSummary[]>(`${API_BASE}/suppliers`);
}

/**
 * Create a new supplier
 */
export async function createSupplier(data: CreateSupplierRequest): Promise<SupplierConfig> {
  return fetchJSON<SupplierConfig>(`${API_BASE}/suppliers`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
}

/**
 * Get supplier configuration
 */
export async function getSupplierConfig(code: string): Promise<SupplierConfig> {
  return fetchJSON<SupplierConfig>(`${API_BASE}/suppliers/${encodeURIComponent(code)}/config`);
}

/**
 * Update supplier configuration
 */
export async function updateSupplierConfig(code: string, config: SupplierConfig): Promise<SupplierConfig> {
  return fetchJSON<SupplierConfig>(`${API_BASE}/suppliers/${encodeURIComponent(code)}/config`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(config),
  });
}

/**
 * Get supplier config history
 */
export async function getSupplierHistory(code: string): Promise<SupplierHistoryEntry[]> {
  return fetchJSON<SupplierHistoryEntry[]>(`${API_BASE}/suppliers/${encodeURIComponent(code)}/history`);
}

/**
 * Get specific history version
 */
export async function getSupplierHistoryVersion(code: string, version: string): Promise<SupplierConfig> {
  return fetchJSON<SupplierConfig>(`${API_BASE}/suppliers/${encodeURIComponent(code)}/history/${encodeURIComponent(version)}`);
}

/**
 * Restore supplier config from history
 */
export async function restoreSupplierVersion(code: string, version: string): Promise<SupplierConfig> {
  return fetchJSON<SupplierConfig>(`${API_BASE}/suppliers/${encodeURIComponent(code)}/restore/${encodeURIComponent(version)}`, {
    method: 'POST',
  });
}

/**
 * Validate supplier config
 */
export async function validateSupplierConfig(code: string, checkUrls: boolean = false): Promise<ValidationResult> {
  const params = checkUrls ? '?check_urls=true' : '';
  return fetchJSON<ValidationResult>(`${API_BASE}/suppliers/${encodeURIComponent(code)}/validate${params}`, {
    method: 'POST',
  });
}

/**
 * Upload invoice file
 */
export async function uploadInvoice(
  code: string,
  file: File,
  invoiceNumber?: string
): Promise<{ success: boolean; filename: string; path: string; size_bytes: number }> {
  const formData = new FormData();
  formData.append('file', file);
  
  let url = `${API_BASE}/suppliers/${encodeURIComponent(code)}/upload-invoice`;
  if (invoiceNumber) {
    url += `?invoice_number=${encodeURIComponent(invoiceNumber)}`;
  }
  
  const res = await fetch(url, {
    method: 'POST',
    body: formData,
  });
  
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Upload failed: ${res.status} ${text}`);
  }
  
  return res.json();
}

/**
 * Delete supplier (soft delete)
 */
export async function deleteSupplier(code: string): Promise<{ success: boolean; message: string }> {
  return fetchJSON(`${API_BASE}/suppliers/${encodeURIComponent(code)}?confirm=true`, {
    method: 'DELETE',
  });
}
