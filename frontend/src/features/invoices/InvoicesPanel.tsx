import React, { useEffect, useState } from "react";
import { Modal } from "../../components/ui/Modal";

const API_BASE: string = (import.meta.env.VITE_API_BASE || "/api").replace(/\/$/, "");

type Stats = { existing?: number; new?: number; unmatched?: number; invoice_items?: number };
type Outputs = { updates_existing?: string; new_products?: string; unmatched?: string };

type InvoiceRow = {
  id: string;
  rel_path: string;
  filename: string;
  issued_at?: string | null;
  processed?: boolean;
  processed_at?: string | null;
  size?: number;
  stats?: Stats;
  outputs?: Outputs;
  history_count?: number;
  last_processed_at?: string | null;
};

type Props = { supplier: string; shop?: string; defaultMonths?: number };
type AnyObj = Record<string, any>;

async function fetchJSON<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
  const text = await res.text();
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}${text ? ` — ${text}` : ""}`);
  try { return JSON.parse(text) as T; } catch { return {} as T; }
}

function normDate(v: any): string | null {
  const s = String(v ?? "").trim();
  if (!s) return null;
  const d = new Date(s);
  return isFinite(d.getTime()) ? d.toISOString().slice(0, 10) : s;
}

function dateFromPaulLangeNumber(token?: string | null): string | null {
  const s = String(token ?? "").trim();
  const m = s.match(/F(\d{8})/i);
  if (!m) return null;
  const y = m[1].slice(0, 4);
  const mo = m[1].slice(4, 6);
  const d = m[1].slice(6, 8);
  return `${y}-${mo}-${d}`;
}

function coalesceRelPath(supplier: string, v: AnyObj): string | null {
  const raw = v?.rel_path ?? v?.csv_path ?? v?.raw_path ?? v?.relpath ?? v?.path ?? "";
  const p = String(raw || "").replace(/\\/g, "/").trim();
  if (!p) return null;
  if (p.startsWith("suppliers/")) return p;
  return `suppliers/${supplier}/${p}`;
}

function ensureFilename(relpath: string): string {
  try { return relpath.split("/").pop() || relpath; } catch { return relpath; }
}

function coalesceId(v: AnyObj, rel?: string): string | null {
  const direct = v?.invoice_id ?? v?.number ?? v?.id ?? "";
  const s = String(direct || "").trim();
  if (s) return s;
  if (rel) {
    const base = ensureFilename(rel).replace(/\.csv$/i, "");
    if (base) return base;
  }
  return null;
}
function coalesceIssuedAt(v: AnyObj): string | null {
  const primary = v?.issue_date ?? v?.issued_at ?? v?.date ?? v?.created_at ?? null;
  if (primary) return normDate(primary);
  if (v?.downloaded_at) return normDate(String(v.downloaded_at).slice(0, 10));
  return null;
}

function coalesceProcessed(v: AnyObj): boolean {
  const st = String(v?.status ?? "").toLowerCase();
  if (st) return st !== "new";
  const raw = v?.processed ?? v?.is_processed ?? v?.done ?? false;
  return !!raw;
}

function coalesceSize(v: AnyObj): number | undefined {
  const n = v?.size_bytes ?? v?.size ?? v?.bytes ?? null;
  return typeof n === "number" ? n : undefined;
}

function normalizeRows(supplier: string, payload: any) {
  const list: AnyObj[] =
    Array.isArray(payload?.invoices)
      ? payload.invoices
      : Array.isArray(payload?.rows)
        ? payload.rows
        : Array.isArray(payload?.items)
          ? payload.items
          : Array.isArray(payload)
            ? payload
            : [];

  return (list || [])
    .map((v: AnyObj) => {
      const rel = coalesceRelPath(supplier, v);
      const id  = coalesceId(v, rel || undefined);
      if (!rel || !id) return null;

      let issued = coalesceIssuedAt(v);
      if (supplier === "paul-lange") {
        const fromNo = dateFromPaulLangeNumber(v?.number || v?.invoice_id || id);
        if (fromNo) issued = fromNo;
      }

      const processedAt = v?.processed_at || null;
      const stats: Stats | undefined = v?.stats || (typeof v?.existing === "number" || typeof v?.new === "number" || typeof v?.unmatched === "number"
        ? { existing: v?.existing, new: v?.new, unmatched: v?.unmatched, invoice_items: v?.invoice_items }
        : undefined);
      const outputs: Outputs | undefined = v?.outputs || v?.output;

      return {
        id,
        rel_path: rel,
        filename: ensureFilename(rel),
        issued_at: issued,
        processed: coalesceProcessed(v),
        processed_at: processedAt,
        size: coalesceSize(v),
        stats,
        outputs,
        history_count: typeof v?.history_count === 'number' ? v.history_count : undefined,
        last_processed_at: v?.last_processed_at || null,
      } as InvoiceRow;
    })
    .filter((x): x is InvoiceRow => Boolean(x));
}

export default function InvoicesPanel({ supplier, shop = "biketrek", defaultMonths = 3 }: Props) {
  const [rows, setRows] = useState<InvoiceRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [processing, setProcessing] = useState(false);
  const [processResults, setProcessResults] = useState<Record<string, any>>({});
  const [historyOpen, setHistoryOpen] = useState(false);
  const [historyFor, setHistoryFor] = useState<string | null>(null);
  const [historyItems, setHistoryItems] = useState<any[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyErr, setHistoryErr] = useState<string | null>(null);

  async function openHistory(id: string) {
    setHistoryFor(id);
    setHistoryOpen(true);
    setHistoryLoading(true);
    setHistoryErr(null);
    try {
      const url = `${API_BASE}/suppliers/${supplier}/invoices/history?invoice_id=${encodeURIComponent(id)}`;
      const data = await fetchJSON<any>(url);
      const items = Array.isArray(data?.items) ? data.items : [];
      setHistoryItems(items);
    } catch (e:any) {
      setHistoryErr(e?.message || String(e));
    } finally {
      setHistoryLoading(false);
    }
  }


  const btnOutline = "inline-flex items-center rounded-2xl px-3 py-1.5 text-sm font-medium border border-lime-500 text-lime-400 hover:bg-lime-500/10 transition";

  async function loadIndex() {
    setLoading(true);
    setErr(null);
    try {
      const data = await fetchJSON<any>(`${API_BASE}/suppliers/${supplier}/invoices/index`);
      const norm = normalizeRows(supplier, data);
      setRows(norm);
    } catch (e: any) {
      setErr(e?.message || String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { loadIndex(); }, [supplier]);

  function toggle(id: string) {
    setSelected(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }

  async function doRefresh() {
    setIsRefreshing(true);
    setErr(null);
    setMsg(null);
    try {
      const body = { months_back: defaultMonths };
      const res = await fetch(`${API_BASE}/suppliers/${supplier}/invoices/refresh`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const txt = await res.text();
      if (!res.ok) throw new Error(`${res.status} ${res.statusText} — ${txt}`);
      setMsg("Invoices refreshed.");
      await loadIndex();
    } catch (e: any) {
      setErr(e?.message || String(e));
    } finally {
      setIsRefreshing(false);
    }
  }

  function outLink(rel: string): string {
    if (!rel) return "#";
    const path = rel.startsWith("suppliers/") ? rel : `suppliers/${supplier}/${rel}`;
    return `${API_BASE}/files/download?relpath=${encodeURIComponent(path)}`;
  }

  async function processSelected() {
    const ids = Array.from(selected);
    if (!ids.length) return;
    setErr(null);
    setMsg(null);
    setProcessing(true);
    try {
      const acc: Record<string, any> = {};
      const succeeded: string[] = [];

      for (const id of ids) {
        const row = rows.find(r => r.id === id);
        if (!row) continue;
        const invoice_relpath = row.rel_path.startsWith(`suppliers/${supplier}/`)
          ? row.rel_path.slice(`suppliers/${supplier}/`.length)
          : row.rel_path;

        const body = {
          supplier_ref: supplier,
          shop_ref: shop,
          invoice_relpath,
          use_invoice_qty: true
        };

        const res = await fetchJSON<any>(`${API_BASE}/runs/prepare`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        acc[id] = res;
        const hasOut = !!(res?.outputs?.updates_existing || res?.outputs?.new_products || res?.outputs?.unmatched);
        const hasStats = (res?.stats?.existing ?? 0) + (res?.stats?.new ?? 0) + (res?.stats?.unmatched ?? 0) > 0;
        if (hasOut || hasStats) {
          succeeded.push(id);
        }
      }

      if (succeeded.length) {
        setRows(prev => prev.map(r => (succeeded.includes(r.id) ? { ...r, processed: true } : r)));
      }

      setProcessResults(prev => ({ ...prev, ...acc }));
      setMsg(`Processed ${ids.length} invoice${ids.length > 1 ? "s" : ""}.`);
    } catch (e: any) {
      setErr(`Processing failed: ${e?.message || e}`);
    } finally {
      setProcessing(false);
    }
  }

  const totalSelected = selected.size;

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <button className={btnOutline} onClick={doRefresh} disabled={loading || isRefreshing}>
          {isRefreshing ? "Refreshing…" : "Refresh"}
        </button>
        <button
          className={btnOutline}
          onClick={processSelected}
          disabled={loading || isRefreshing || processing || totalSelected === 0}
          title={totalSelected ? "" : "Najskôr označ faktúry"}
        >
          {processing ? "Processing…" : "Process selected"}
        </button>
      </div>

      {err ? <div className="rounded-2xl border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-rose-200 text-xs">{err}</div> : null}
      {msg ? <div className="rounded-2xl border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-emerald-200 text-xs">{msg}</div> : null}

      <div className="overflow-auto rounded-2xl border border-white/10">
        <table className="min-w-full text-sm">
          <thead className="sticky top-0 bg-slate-900/90 backdrop-blur text-left">
            <tr>
              <th className="px-3 py-2 w-10">Sel</th>
              <th className="px-3 py-2">Date</th>
              <th className="px-3 py-2">Invoice</th>
              <th className="px-3 py-2">Relpath</th>
              <th className="px-3 py-2">Status</th>
              <th className="px-3 py-2">Results</th>
              <th className="px-3 py-2 w-20"> </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const persisted = rows.find(x => x.id === r.id);
              const pr = (processResults as any)[r.id] || { stats: persisted?.stats, outputs: persisted?.outputs };
              const st = pr?.stats || {};
              const out = pr?.outputs || {};
              return (
              <tr key={r.id} className="odd:bg-slate-900/40">
                <td className="px-3 py-2">
                  <input type="checkbox" checked={selected.has(r.id)} onChange={() => toggle(r.id)} />
                </td>
                <td className="px-3 py-2">{r.issued_at || "—"}</td>
                <td className="px-3 py-2">{r.id}</td>
                <td className="px-3 py-2">
                  <a
                    className="text-lime-300 hover:underline"
                    href={`${API_BASE}/files/download?relpath=${encodeURIComponent(r.rel_path)}`}
                    target="_blank"
                    rel="noreferrer"
                  >
                    {r.rel_path}
                  </a>
                </td>
                <td className="px-3 py-2">
                  {r.processed ? (
                    <div className="flex flex-col">
                      <span className="rounded bg-emerald-500/20 px-2 py-0.5 text-emerald-300 text-xs self-start">processed</span>
                      {r.processed_at && <span className="text-[10px] text-white/40 mt-0.5">{new Date(r.processed_at).toLocaleString()}</span>}
                      {(r.history_count > 0 || r.last_processed_at) && (
                        <div className="text-[11px] text-white/50 mt-0.5">
                          {r.history_count ? <>history: {r.history_count}</> : null}
                          {r.history_count && r.last_processed_at ? <> · </> : null}
                          {r.last_processed_at ? <>last: {r.last_processed_at}</> : null}
                        </div>
                      )}                      
                    </div>
                  ) : (
                    <span className="rounded bg-yellow-500/20 px-2 py-0.5 text-yellow-300 text-xs">new</span>
                  )}
                </td>
                <td className="px-3 py-2 text-xs text-white/80">
                  {(st.existing ?? st.new ?? st.unmatched ?? out.updates_existing ?? out.new_products ?? out.unmatched) ? (
                    <div className="space-y-1">
                      <div>
                        <span className="mr-2">existing: <b>{st.existing ?? 0}</b></span>
                        <span className="mr-2">new: <b>{st.new ?? 0}</b></span>
                        <span className="mr-2">unmatched: <b>{st.unmatched ?? 0}</b></span>
                      </div>
                      <div className="flex flex-wrap gap-2">
                        {out.updates_existing && <a className="text-lime-300 hover:underline" target="_blank" rel="noreferrer" href={outLink(out.updates_existing)}>updates</a>}
                        {out.new_products && <a className="text-lime-300 hover:underline" target="_blank" rel="noreferrer" href={outLink(out.new_products)}>new</a>}
                        {out.unmatched && <a className="text-lime-300 hover:underline" target="_blank" rel="noreferrer" href={outLink(out.unmatched)}>unmatched</a>}
                      </div>
                    </div>
                  ) : (
                    <span className="text-white/40">—</span>
                  )}
                </td>
                <td className="px-3 py-2"> </td>
              </tr>
            )})}
            {rows.length === 0 && (
              <tr>
                <td colSpan={7} className="px-3 py-8 text-center text-white/50">Žiadne faktúry.</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    
      {/* HISTORY MODAL */}
      <Modal open={historyOpen} onClose={()=>setHistoryOpen(false)} title={historyFor ? `History — ${historyFor}` : "History"} align="top">
        {historyLoading ? (
          <div className="text-sm text-white/70">Loading…</div>
        ) : historyErr ? (
          <div className="text-sm text-rose-300">{historyErr}</div>
        ) : (
          <div className="text-sm text-white/80">
            {historyItems.length === 0 ? (
              <div className="text-white/50">No history.</div>
            ) : (
              <ul className="space-y-2">
                {historyItems.map((it, idx) => (
                  <li key={idx} className="flex items-center justify-between gap-3">
                    <div>
                      <div className="text-white">{it.processed_at || it.name}</div>
                      {it.relpath ? <div className="text-white/50 text-xs break-all">{it.relpath}</div> : null}
                    </div>
                    {it.relpath ? (
                      <a
                        className="text-lime-300 hover:underline"
                        href={`${API_BASE}/files/download?relpath=${encodeURIComponent(it.relpath)}&disposition=inline&filename=${encodeURIComponent(it.name || "history.json")}`}
                        target="_blank"
                        rel="noreferrer"
                      >
                        open
                      </a>
                    ) : null}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </Modal>

    </div>
  );
}
