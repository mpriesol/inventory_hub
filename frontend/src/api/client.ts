// src/api/client.ts
// Unified API client with: global fetch tap (logs), session-persisted ring buffer, and fetchJSON helper.

export type ClientLogEvent = {
  ts: number;                 // epoch ms
  dir: "→" | "←" | "!";       // outbound / inbound / error
  level: "info" | "error";
  method: string;
  url: string;
  status?: number;
  note?: string;
};

const CLIENT_LOG_KEY = "biketrek_client_logs_v1";
const MAX_EVENTS = 300;

let clientLogBuffer: ClientLogEvent[] = (() => {
  try {
    const raw = sessionStorage.getItem(CLIENT_LOG_KEY);
    if (!raw) return [];
    const arr = JSON.parse(raw);
    return Array.isArray(arr) ? arr : [];
  } catch {
    return [];
  }
})();

const clientLogListeners = new Set<(list: ClientLogEvent[]) => void>();

function publishClientLogs() {
  try { sessionStorage.setItem(CLIENT_LOG_KEY, JSON.stringify(clientLogBuffer)); } catch {}
  for (const fn of clientLogListeners) {
    try { fn(clientLogBuffer); } catch {}
  }
}

function pushClientLog(e: ClientLogEvent) {
  clientLogBuffer = [...clientLogBuffer, e].slice(-MAX_EVENTS);
  publishClientLogs();
}

export function getClientLogs(): ClientLogEvent[] {
  return clientLogBuffer;
}

export function subscribeClientLogs(fn: (list: ClientLogEvent[]) => void): () => void {
  clientLogListeners.add(fn);
  try { fn(clientLogBuffer); } catch {}
  return () => clientLogListeners.delete(fn);
}

// Base URL
export const API_BASE = (import.meta.env.VITE_API_BASE || "/api").replace(/\/$/, "");

// Global fetch tap: logs *all* requests targeting API_BASE
let __tapInstalled = false;
export function installFetchTap(base = API_BASE) {
  if (__tapInstalled || typeof window === "undefined" || typeof window.fetch !== "function") return;
  __tapInstalled = true;

  const origFetch = window.fetch.bind(window);
  window.fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = typeof input === "string" ? input : (input as Request).url;
    const method = ((init?.method || (typeof input !== "string" ? (input as Request).method : "GET")) || "GET").toUpperCase();
    const isApi = !!base && typeof url === "string" && url.startsWith(base);

    if (isApi) pushClientLog({ ts: Date.now(), dir: "→", level: "info", method, url });
    try {
      const res = await origFetch(input as any, init);
      if (isApi) {
        pushClientLog({ ts: Date.now(), dir: "←", level: res.ok ? "info" : "error", method, url, status: res.status });
      }
      return res;
    } catch (err: any) {
      if (isApi) pushClientLog({ ts: Date.now(), dir: "!", level: "error", method, url, note: String(err?.message || err) });
      throw err;
    }
  };
}

// Install tap immediately on module import
installFetchTap();

// Helper: fetch and parse JSON with good errors
export async function fetchJSON<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
  const ct = res.headers.get("content-type") || "";
  const text = await res.text().catch(() => "");

  if (!res.ok) {
    // include response text (first 800 chars) for easier debugging
    const snippet = text ? ` — ${text.slice(0, 800)}` : "";
    throw new Error(`${res.status} ${res.statusText}${snippet}`);
  }

  if (ct.includes("application/json")) {
    return (text ? JSON.parse(text) : ({} as any)) as T;
  }
  // Fallback: try parse as JSON; else return an empty object casted
  try { return JSON.parse(text) as T; } catch { return ({} as any) as T; }
}
