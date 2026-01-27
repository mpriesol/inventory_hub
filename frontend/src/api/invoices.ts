import { API_BASE, fetchJSON } from "./client";

export async function refreshSupplierInvoices(
  supplier: string,
  monthsBack: number = 3,
  strategy?: string
) {
  const body: any = { months_back: monthsBack };
  if (strategy) body.strategy = strategy;
  return fetchJSON(`${API_BASE}/suppliers/${encodeURIComponent(supplier)}/invoices/refresh`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}
export const refreshInvoices = refreshSupplierInvoices;

// typy a nové API volania:
export type InvoiceIndexItem = {
  supplier: string;
  invoice_id: string;
  number: string | null;
  issue_date: string | null;
  issue_date_source?: string;
  downloaded_at: string;
  raw_path?: string | null;
  csv_path: string;
  status: "new" | "in_progress" | "processed" | "skipped" | "failed";
  sha1?: string;
  layout_used?: string;
  // For in_progress invoices
  current_session_id?: string;
  paused_at?: string;
  pause_stats?: {
    total_lines: number;
    received_complete: number;
    received_partial: number;
    not_received: number;
    total_scans: number;
  };
  // For processed invoices
  processed_at?: string;
  receiving_session_id?: string;
  receiving_stats?: {
    total_lines: number;
    received_complete: number;
    received_partial: number;
    not_received: number;
  };
};

export async function getInvoicesIndex(supplier: string) {
  const response = await fetchJSON<{ supplier: string; count: number; invoices: InvoiceIndexItem[] }>(
    `${API_BASE}/suppliers/${encodeURIComponent(supplier)}/invoices/index`
  );
  // Normalize: backend returns 'invoices', we use 'items' internally
  return {
    supplier: response.supplier,
    count: response.count,
    items: response.invoices || [],
  };
}

export async function markInvoicesProcessed(supplier: string, invoice_ids: string[]) {
  return fetchJSON(
    `${API_BASE}/suppliers/${encodeURIComponent(supplier)}/invoices/mark_processed`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ invoice_ids }),
    }
  );
}

export type PreviewResponse = {
  columns: string[];
  rows: string[][];
  preview_rows: number;
  total_columns: number;
};

export async function previewCsvByRelpath(relpath: string): Promise<PreviewResponse> {
  return fetchJSON(
    `${API_BASE}/files/preview?relpath=${encodeURIComponent(relpath)}`
  );
}
