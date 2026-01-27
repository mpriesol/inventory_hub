import { API_BASE } from "../lib/api"; // uisti sa, že toto je správna cesta u teba

export type LogItem = {
  relpath: string;
  supplier: string;
  filename: string;
  size: number;
  mtime_iso: string;
  ext: string;
  content_type: string;
};

export type LogRead = {
  relpath: string;
  size: number;
  mtime_iso: string;
  is_html: boolean;
  truncated: boolean;
  text: string;
};

async function handle<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const t = await res.text().catch(() => "");
    throw new Error(`${res.status} ${res.statusText}${t ? ` — ${t}` : ""}`);
  }
  return res.json() as Promise<T>;
}

export async function fetchRecentLogsGlobal(limit = 100, supplierFilter?: string[]): Promise<LogItem[]> {
  const params = new URLSearchParams();
  params.set("limit", String(limit));
  if (supplierFilter?.length) params.set("supplier", supplierFilter.join(","));
  const url = `${API_BASE}/logs/recent?${params.toString()}`;
  const data = await handle<{ items: LogItem[] }>(await fetch(url));
  return data.items || [];
}

export async function readLogGlobal(relpath: string): Promise<LogRead> {
  const url = `${API_BASE}/logs/read?relpath=${encodeURIComponent(relpath)}`;
  return handle<LogRead>(await fetch(url));
}