import React, { useEffect, useMemo, useState } from "react";
import { API_BASE, fetchJSON } from "../../api/client";

type InvoiceRow = {
  id: string;
  rel_path: string;
  filename: string;
  issued_at?: string | null;
  processed?: boolean;
  size?: number;
};

type Props = {
  supplier: string;
  defaultMonths?: number;
};

export default function InvoicesPanel({ supplier, defaultMonths = 3 }: Props) {
  const [rows, setRows] = useState<InvoiceRow[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [months, setMonths] = useState<number>(defaultMonths);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  const btnGreen =
    "inline-flex items-center rounded-2xl px-3 py-1.5 text-sm font-medium bg-lime-500 text-slate-950 hover:bg-lime-400 transition";
  const btnOutline =
    "inline-flex items-center rounded-2xl px-3 py-1.5 text-sm font-medium border border-lime-500 text-lime-400 hover:bg-lime-500/10 transition";

  useEffect(() => { void loadIndex(); /* eslint-disable-next-line */ }, [supplier]);

  function coalesceRelPath(v: any): string | null {
    // Tvoj backend: "csv_path": "invoices/csv/20251024_F2025072207.csv"
    // Chceme relpath od rootu data: "suppliers/{supplier}/invoices/csv/20251024_F2025..."
    const p = v?.csv_path || v?.raw_path || v?.rel_path || v?.relpath || "";
    if (!p) return null;
    if (p.startsWith("suppliers/")) return p;
    return `suppliers/${supplier}/${p}`;
  }

  function normalizeFromDict(obj: Record<string, any>): InvoiceRow[] {
    return Object.values(obj || {}).map((v: any) => {
      const id = String(v.invoice_id || v.id || "");
      const rel_path = coalesceRelPath(v);
      if (!id || !rel_path) return null;

      const filename = v.filename || (String(rel_path).split("/").pop() ?? rel_path);
      // Preferuj issue_date; fallback na date part z downloaded_at
      let issued_at: string | null = v.issue_date || null;
      if (!issued_at && v.downloaded_at) {
        const d = String(v.downloaded_at).slice(0, 10);
        if (/^\d{4}-\d{2}-\d{2}$/.test(d)) issued_at = d;
      }
      const processed = String(v.status || "").toLowerCase() !== "new";

      return { id, rel_path, filename, issued_at, processed } as InvoiceRow;
    }).filter(Boolean) as InvoiceRow[];
  }

  function normalizeFromList(list: any[]): InvoiceRow[] {
    return (list || []).map((it) => {
      const id = String(it.id ?? it.invoice_id ?? "");
      const rel_path = coalesceRelPath(it);
      if (!id || !rel_path) return null;

      const filename = it.filename ? String(it.filename) : (String(rel_path).split("/").pop() || rel_path);
      const issued_at = it.issued_at ?? it.issue_date ?? null;
      const processed = Boolean(it.processed ?? (String(it.status || "").toLowerCase() !== "new"));

      return { id, rel_path, filename, issued_at, processed, size: typeof it.size === "number" ? it.size : undefined };
    }).filter(Boolean) as InvoiceRow[];
  }

  async function loadIndex() {
    setLoading(true);
    setErr(null);
    try {
      const url = `${API_BASE}/suppliers/${encodeURIComponent(supplier)}/invoices/index`;
      const data = await fetchJSON<any>(url);

      let normalized: InvoiceRow[] = [];
      if (Array.isArray(data?.items)) {
        normalized = normalizeFromList(data.items);
      } else if (data && typeof data === "object") {
        // podpora starého map formátu
        normalized = normalizeFromDict(data);
      }

      normalized.sort((a, b) => {
        const ad = a.issued_at || "";
        const bd = b.issued_at || "";
        if (ad !== bd) return bd.localeCompare(ad);
        return (b.filename || "").localeCompare(a.filename || "");
      });

      setRows(normalized);
      setSelected(new Set());
    } catch (e: any) {
      setErr(`Neviem načítať zoznam faktúr: ${e?.message || e}`);
    } finally {
      setLoading(false);
    }
  }

  async function refreshInvoices() {
    setErr(null);
    setMsg(null);
    try {
      const url = `${API_BASE}/suppliers/${encodeURIComponent(supplier)}/invoices/refresh`;
      // pošleme obe formy parametra pre kompatibilitu
      await fetchJSON(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ monthsBack: months, months_back: months, strategy: "paul-lange-web" })
      });
      setMsg("Invoices refresh spustený.");
      await loadIndex();
    } catch (e: any) {
      setErr(`Refresh zlyhal: ${e?.message || e}`);
    }
  }

  async function markProcessed() {
    const ids = Array.from(selected);
    if (!ids.length) return;
    setErr(null);
    setMsg(null);
    try {
      const url = `${API_BASE}/suppliers/${encodeURIComponent(supplier)}/invoices/mark_processed`;
      await fetchJSON(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ invoice_ids: ids }) // backend očakáva pole
      });
      setMsg(`Označené ako spracované: ${ids.length} ks`);
      await loadIndex();
    } catch (e: any) {
      setErr(`Mark processed zlyhal: ${e?.message || e}`);
    }
  }

  function toggleOne(id: string, checked: boolean) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (checked) next.add(id); else next.delete(id);
      return next;
    });
  }

  const allSelectable = useMemo(() => rows.filter((r) => !r.processed), [rows]);
  const allChecked = useMemo(
    () => allSelectable.length > 0 && allSelectable.every((r) => selected.has(r.id)),
    [allSelectable, selected]
  );

  function toggleAll(checked: boolean) {
    if (!checked) { setSelected(new Set()); return; }
    setSelected(new Set(allSelectable.map((r) => r.id)));
  }

  return (
    <div className="rounded-2xl border border-white/10 bg-slate-950/50 p-4">
      <div className="flex flex-wrap items-center justify-between gap-3 mb-4">
        <h3 className="text-lg font-semibold">Invoices</h3>
        <div className="flex items-center gap-2">
          <label className="text-sm text-white/70 flex items-center gap-2">
            Months back
            <input
              type="number"
              min={1}
              max={24}
              value={months}
              onChange={(e) => setMonths(Math.max(1, Math.min(24, Number(e.target.value) || defaultMonths)))}
              className="w-20 rounded-xl border border-white/10 bg-slate-900 px-2 py-1 text-sm text-white"
            />
          </label>
          <button className={btnGreen} onClick={refreshInvoices} disabled={loading}>Refresh</button>
          <button className={btnOutline} onClick={markProcessed} disabled={loading || selected.size === 0}>
            Mark processed
          </button>
        </div>
      </div>

      {err && <div className="mb-3 rounded-xl border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-200">{err}</div>}
      {msg && <div className="mb-3 rounded-xl border border-lime-500/40 bg-lime-500/10 px-3 py-2 text-sm text-lime-200">{msg}</div>}

      <div className="overflow-auto rounded-xl border border-white/10">
        <table className="min-w-full text-sm">
          <thead className="bg-slate-900/70">
            <tr className="text-left text-white/70">
              <th className="px-3 py-2 w-10">
                <input type="checkbox" aria-label="Select all" checked={allChecked} onChange={(e) => toggleAll(e.currentTarget.checked)} />
              </th>
              <th className="px-3 py-2">Invoice</th>
              <th className="px-3 py-2">Issued at</th>
              <th className="px-3 py-2">Status</th>
              <th className="px-3 py-2 w-20"> </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const disabled = Boolean(r.processed);
              return (
                <tr key={r.id} className="border-t border-white/5 hover:bg-white/5">
                  <td className="px-3 py-2 align-middle">
                    <input type="checkbox" disabled={disabled} checked={selected.has(r.id)} onChange={(e) => toggleOne(r.id, e.currentTarget.checked)} />
                  </td>
                  <td className="px-3 py-2">
                    <div className="font-medium">{r.filename}</div>
                    <div className="text-xs text-white/50 break-all">{r.rel_path}</div>
                  </td>
                  <td className="px-3 py-2">{r.issued_at || "—"}</td>
                  <td className="px-3 py-2">
                    {r.processed
                      ? <span className="rounded-full bg-emerald-500/20 px-2 py-0.5 text-emerald-300">Processed</span>
                      : <span className="rounded-full bg-yellow-500/20 px-2 py-0.5 text-yellow-300">New</span>}
                  </td>
                  <td className="px-3 py-2">
                    <a
                      className="text-xs text-lime-300 hover:underline"
                      href={`${API_BASE}/files/download?relpath=${encodeURIComponent(r.rel_path)}`}
                      target="_blank"
                      rel="noreferrer"
                    >
                      download
                    </a>
                  </td>
                </tr>
              );
            })}
            {rows.length === 0 && (
              <tr>
                <td colSpan={5} className="px-3 py-8 text-center text-white/50">Žiadne faktúry.</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
