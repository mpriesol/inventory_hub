import React, { useEffect, useMemo, useState } from "react";
import { API_BASE, fetchJSON } from "../../api/client";

type InvoiceRow = {
  id: string;           // ← používame na selection
  relpath: string;      // link na download
  filename?: string;
  size?: number;
  issue_date?: string | null;
  processed_at?: string | null;
  downloaded_at?: string | null;
  status?: string;
  number?: string | null;
};

type Props = {
  supplier: string;
  defaultMonths?: number; // napr. 3
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

  useEffect(() => {
    void loadIndex();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [supplier]);

  async function loadIndex() {
    setLoading(true);
    setErr(null);
    try {
      const data = await fetchJSON<any>(`${API_BASE}/suppliers/${encodeURIComponent(supplier)}/invoices/index`);
      const arr: any[] = Array.isArray(data) ? data : (data?.items ?? data?.invoices ?? data?.files ?? []);
      const normalized: InvoiceRow[] = arr.map((it) => {
        const invoiceId = it.invoice_id || it.id || it.number || "";
        const fileRel = it.csv_path || it.raw_path || it.relpath || "";
        const relpath = fileRel.startsWith("suppliers/") ? fileRel : `suppliers/${supplier}/${fileRel}`;
        return {
          id: invoiceId,
          relpath,
          filename: relpath.split("/").pop(),
          size: it.size ?? undefined,
          issue_date: it.issue_date ?? null,
          processed_at: it.processed_at ?? (it.status === "processed" ? (it.processed_at || "✓") : null),
          downloaded_at: it.downloaded_at ?? null,
          status: it.status,
          number: it.number ?? null,
        };
      }).filter(r => r.relpath);

      // najnovšie hore
      normalized.sort((a, b) => (b.relpath > a.relpath ? 1 : -1));
      setRows(normalized);
      setSelected(new Set());
    } catch (e: any) {
      setErr(`Neviem načítať zoznam faktúr: ${e.message || e}`);
    } finally {
      setLoading(false);
    }
  }

  async function refreshInvoices() {
    setErr(null); setMsg(null);
    try {
      await fetchJSON(`${API_BASE}/suppliers/${encodeURIComponent(supplier)}/invoices/refresh`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ months_back: months }),
      });
      setMsg("Invoices refresh spustený/úspešný.");
      await loadIndex();
    } catch (e: any) {
      setErr(`Refresh zlyhal: ${e.message || e}`);
    }
  }

  async function markProcessed() {
    const items = Array.from(selected);
    if (!items.length) return;
    setErr(null); setMsg(null);
    try {
      await fetchJSON(`${API_BASE}/suppliers/${encodeURIComponent(supplier)}/invoices/mark_processed`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ invoice_ids: items }),
      });
      setMsg(`Označené ako spracované: ${items.length} ks`);
      await loadIndex();
    } catch (e: any) {
      setErr(`Mark processed zlyhal: ${e.message || e}`);
    }
  }

  function toggleOne(id: string, checked: boolean) {
    setSelected(prev => {
      const next = new Set(prev);
      if (checked) next.add(id);
      else next.delete(id);
      return next;
    });
  }

  const allSelectable = useMemo(() => rows.filter(r => r.status !== "processed"), [rows]);
  const allChecked = useMemo(() => allSelectable.length > 0 && allSelectable.every(r => selected.has(r.id)), [allSelectable, selected]);

  function toggleAll(checked: boolean) {
    if (!checked) { setSelected(new Set()); return; }
    setSelected(new Set(allSelectable.map(r => r.id)));
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
              title="Koľko mesiacov dozadu kontrolovať nové faktúry"
            />
          </label>
          <button className={btnGreen} onClick={refreshInvoices} disabled={loading}>Refresh</button>
          <button
            className={btnOutline}
            onClick={markProcessed}
            disabled={loading || selected.size === 0}
            title={selected.size ? "" : "Najskôr označ faktúry"}
          >
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
                <input
                  type="checkbox"
                  aria-label="Select all"
                  checked={allChecked}
                  onChange={(e) => toggleAll(e.currentTarget.checked)}
                />
              </th>
              <th className="px-3 py-2">Invoice</th>
              <th className="px-3 py-2">Issue date</th>
              <th className="px-3 py-2">Size</th>
              <th className="px-3 py-2">Status</th>
              <th className="px-3 py-2">Processed at</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const isProcessed = Boolean(r.processed_at);
              const disabled = isProcessed;
              const name = r.filename || r.relpath.split("/").pop() || r.relpath;
              const id = r.id;
              return (
                <tr key={r.relpath} className="border-t border-white/5 hover:bg-white/5">
                  <td className="px-3 py-2 align-middle">
                    <input
                      type="checkbox"
                      disabled={disabled}
                      checked={selected.has(id)}
                      onChange={(e) => toggleOne(id, e.currentTarget.checked)}
                    />
                  </td>
                  <td className="px-3 py-2">
                    <div className="flex items-center gap-2">
                      <span className="font-medium">{name}</span>
                      <a
                        className="text-xs text-lime-300 hover:underline"
                        href={`${API_BASE}/files/download?relpath=${encodeURIComponent(r.relpath)}`}
                        target="_blank"
                        rel="noreferrer"
                      >
                        download
                      </a>
                    </div>
                    <div className="text-xs text-white/50 break-all">{r.relpath}</div>
                  </td>
                  <td className="px-3 py-2">{r.issue_date || "—"}</td>
                  <td className="px-3 py-2">{r.size ? formatBytes(r.size) : "—"}</td>
                  <td className="px-3 py-2">
                    {isProcessed ? (
                      <span className="rounded-full bg-emerald-500/20 px-2 py-0.5 text-emerald-300">Processed</span>
                    ) : (
                      <span className="rounded-full bg-yellow-500/20 px-2 py-0.5 text-yellow-300">New</span>
                    )}
                  </td>
                  <td className="px-3 py-2">{r.processed_at || "—"}</td>
                </tr>
              );
            })}
            {rows.length === 0 && (
              <tr>
                <td colSpan={6} className="px-3 py-8 text-center text-white/50">Žiadne faktúry.</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function formatBytes(n: number): string {
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) { v = v / 1024; i++; }
  return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
}
