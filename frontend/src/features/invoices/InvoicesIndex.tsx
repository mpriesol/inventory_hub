import React, { useEffect, useMemo, useState } from "react";
import { API_BASE } from "../../api/client";
import {
  getInvoicesIndex,
  refreshInvoices,
  type InvoiceIndexItem,
} from "../../api/invoices";

type Props = {
  supplier: string;
  defaultMonths?: number;
  className?: string;
  onLog?: (msg: string, level?: "info" | "error") => void;
};

function InvoicesIndex({ supplier, defaultMonths = 3, className, onLog }: Props) {
  const [items, setItems] = useState<InvoiceIndexItem[]>([]);
  const [count, setCount] = useState<number>(0);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState<string>("");

  function logInfo(m: string) {
    onLog?.(m, "info");
  }
  function logError(m: string) {
    onLog?.(m, "error");
  }

  function norm(it: any): InvoiceIndexItem {
    const rel = String(it?.rel_path || it?.csv_path || "");
    return {
      invoice_id: String(it?.invoice_id || ""),
      number: it?.number ? String(it.number) : "",
      issue_date: it?.issue_date ?? null,
      issue_date_source: it?.issue_date_source ?? null,
      downloaded_at: it?.downloaded_at ?? null,
      csv_path: rel,
      rel_path: rel,
      status: (it?.status as any) || "new",
      sha1: it?.sha1 || "",
    };
  }

  async function loadIndex() {
    if (!supplier) return;
    setLoading(true);
    setError(null);
    try {
      const r = await getInvoicesIndex(supplier);
      setCount(r.count ?? r.items.length);
      setItems(r.items.map(norm));
    } catch (e: any) {
      const msg = String(e?.message || e);
      setError(msg);
      setCount(0);
      setItems([]);
      logError(`Neviem načítať zoznam faktúr: ${msg}`);
    } finally {
      setLoading(false);
    }
  }

  async function handleRefresh() {
    if (!supplier) return;
    setLoading(true);
    setError(null);
    try {
      const r = await refreshInvoices(supplier, defaultMonths);
      await loadIndex(); // kľúčové: hneď načítať stav po refreši
      logInfo(`Hotovo: stiahnuté ${r.downloaded}, tabulka aktualizovaná.`);
    } catch (e: any) {
      const msg = String(e?.message || e);
      setError(msg);
      logError(`Refresh faktúr zlyhal: ${msg}`);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadIndex();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [supplier]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return items;
    return items.filter((it) => {
      return (
        it.invoice_id.toLowerCase().includes(q) ||
        (it.number || "").toLowerCase().includes(q) ||
        (it.csv_path || "").toLowerCase().includes(q) ||
        (it.status || "").toLowerCase().includes(q)
      );
    });
  }, [items, query]);

  const sorted = useMemo(() => {
    // pokus o číselno-lexikografické zoradenie podľa čísla faktúry zostupne
    return [...filtered].sort((a, b) => {
      const an = a.number || "";
      const bn = b.number || "";
      if (an === bn) return 0;
      return an > bn ? -1 : 1;
    });
  }, [filtered]);

  function downloadUrl(relpath: string) {
    return `${API_BASE}/files/download?relpath=${encodeURIComponent(relpath)}`;
  }

  return (
    <div className={className}>
      {/* Header strip */}
      <div className="mb-3 flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <div className="text-sm text-white/70">
          <span className="font-semibold">Supplier:</span>{" "}
          <span className="text-white">{supplier}</span>{" "}
          <span className="mx-2 text-white/30">•</span>
          <span className="font-semibold">V indexe:</span>{" "}
          <span className="text-white">{count}</span>
        </div>
        <div className="flex items-center gap-2">
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Hľadať v tabuľke…"
            className="rounded-xl bg-slate-950/60 border border-white/10 px-3 py-1.5 text-sm outline-none focus:ring-1 focus:ring-lime-500"
          />
          <button
            onClick={handleRefresh}
            disabled={loading}
            className="inline-flex items-center rounded-2xl px-3 py-1.5 text-sm font-medium border border-lime-500 text-lime-400 hover:bg-lime-500/10 disabled:opacity-50"
            title={loading ? "Prebieha..." : `Stiahnuť nové faktúry (posledné ${defaultMonths} mesiace) a obnoviť index`}
          >
            {loading ? "Refreshing…" : "Refresh"}
          </button>
        </div>
      </div>

      {/* Error */}
      {error && (
        <div className="mb-3 text-sm text-rose-400">
          {error}
        </div>
      )}

      {/* Table */}
      <div className="rounded-2xl border border-white/10 bg-slate-950/50 overflow-auto">
        <table className="min-w-full text-sm">
          <thead className="bg-slate-900 sticky top-0 z-10">
            <tr className="text-left text-white/70">
              <th className="px-3 py-2">Invoice #</th>
              <th className="px-3 py-2 hidden md:table-cell">Invoice ID</th>
              <th className="px-3 py-2">Status</th>
              <th className="px-3 py-2 hidden lg:table-cell">CSV</th>
              <th className="px-3 py-2 hidden lg:table-cell">Downloaded at</th>
              <th className="px-3 py-2 text-right">Akcie</th>
            </tr>
          </thead>
          <tbody>
            {loading && sorted.length === 0 ? (
              <tr>
                <td className="px-3 py-3 text-white/60" colSpan={6}>Loading…</td>
              </tr>
            ) : null}
            {!loading && sorted.length === 0 ? (
              <tr>
                <td className="px-3 py-3 text-white/40" colSpan={6}>Žiadne faktúry.</td>
              </tr>
            ) : null}
            {sorted.map((it) => (
              <tr key={it.invoice_id} className="border-t border-white/5 odd:bg-slate-950/50 even:bg-slate-950/30">
                <td className="px-3 py-2 font-mono">{it.number || "—"}</td>
                <td className="px-3 py-2 font-mono hidden md:table-cell">{it.invoice_id}</td>
                <td className="px-3 py-2">
                  <span className="inline-flex items-center rounded-full border border-white/10 px-2 py-0.5 text-xs text-white/80">
                    {it.status || "new"}
                  </span>
                </td>
                <td className="px-3 py-2 hidden lg:table-cell">
                  <div className="max-w-[32rem] truncate text-white/70">{it.csv_path}</div>
                </td>
                <td className="px-3 py-2 hidden lg:table-cell">
                  <div className="text-white/70">
                    {it.downloaded_at ? new Date(it.downloaded_at).toLocaleString() : "—"}
                  </div>
                </td>
                <td className="px-3 py-2">
                  <div className="flex items-center justify-end gap-2">
                    <a
                      href={downloadUrl(it.rel_path || it.csv_path)}
                      target="_blank"
                      rel="noreferrer"
                      className="rounded-xl border border-white/10 px-2 py-1 text-xs text-white/80 hover:bg-white/5"
                      title="Stiahnuť CSV"
                    >
                      Download
                    </a>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export default InvoicesIndex;
