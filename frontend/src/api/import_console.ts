import { API_BASE, fetchJSON } from "./client";

export type CsvOutputInfo = {
  relpath: string;
  headers: string[];
  rows: number;
};

export type CsvOutputsResponse = {
  invoice: string;
  updates: CsvOutputInfo | null;
  new: CsvOutputInfo | null;
  unmatched: CsvOutputInfo | null;
};

export async function fetchCsvOutputs(supplier: string, invoice: string): Promise<CsvOutputsResponse> {
  const res = await fetch(`${API_BASE}/suppliers/${encodeURIComponent(supplier)}/invoices/${encodeURIComponent(invoice)}/csv-outputs`);
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function previewCsv(relpath: string, maxRows = 100) {
  const u = new URL(`${API_BASE}/files/preview`);
  u.searchParams.set("relpath", relpath);
  u.searchParams.set("max_rows", String(maxRows));
  u.searchParams.set("strip_upgates_brackets", "true");
  const res = await fetch(u.toString());
  if (!res.ok) throw new Error(await res.text());
  return res.json() as Promise<{columns: string[]; rows: any[][]}>;
}

export async function getInvoicesIndex(supplier: string) {
  const res = await fetch(`${API_BASE}/suppliers/${encodeURIComponent(supplier)}/invoices/index`);
  if (!res.ok) throw new Error(await res.text());
  return res.json() as Promise<{invoices: any[]}>;
}

export async function getShopConfig(shop: string) {
  const res = await fetch(`${API_BASE}/shops/${encodeURIComponent(shop)}/config`);
  if (!res.ok) throw new Error(await res.text());
  return res.json() as Promise<any>;
}

export async function putShopConfig(shop: string, cfg: any) {
  const res = await fetch(`${API_BASE}/shops/${encodeURIComponent(shop)}/config`, {
    method: "PUT",
    headers: {"Content-Type":"application/json"},
    body: JSON.stringify(cfg),
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function enrichedPreview(
  supplier: string,
  invoice: string,
  shop: string,
  tab: "updates" | "new" | "unmatched",
  offset = 0,
  limit = 200
) {
  const u = new URL(`${API_BASE}/suppliers/${encodeURIComponent(supplier)}/invoices/${encodeURIComponent(invoice)}/enriched-preview`);
  u.searchParams.set("shop", shop);
  u.searchParams.set("tab", tab);
  u.searchParams.set("offset", String(offset));
  u.searchParams.set("limit", String(limit));
  const res = await fetch(u.toString());
  if (!res.ok) throw new Error(await res.text());
  return res.json() as Promise<{columns: string[]; rows: any[]}>;
}


/* imports */
async function handle<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`${res.status} ${res.statusText}${text ? ` — ${text}` : ""}`);
  }
  return res.json() as Promise<T>;
}

export async function applyImports(supplier: string, body: any) {
  const res = await fetch(`${API_BASE}/suppliers/${encodeURIComponent(supplier)}/imports/apply`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  });
  return handle<any>(res);
}

export async function sendImports(supplier: string, body: any) {
  const res = await fetch(`${API_BASE}/suppliers/${encodeURIComponent(supplier)}/imports/send`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  });
  return handle<any>(res);
}

export async function getInvoiceHistory(supplier: string, invoiceId: string) {
  const url = `${API_BASE}/suppliers/${encodeURIComponent(supplier)}/invoices/${encodeURIComponent(invoiceId)}/history`;
  const res = await fetch(url);
  return handle<any>(res);
}