import React, { useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import { API_BASE, getClientLogs, subscribeClientLogs, type ClientLogEvent } from "../../api/client";
import { fetchRecentLogsGlobal, readLogGlobal, type LogItem, type LogRead } from "../../api/logs";

type Props = {
  open: boolean;
  onClose: () => void;
  supplierFilter?: string[];
};

type TabKey = "server" | "client";

export default function LogsModal({ open, onClose, supplierFilter }: Props) {
  const [tab, setTab] = useState<TabKey>("server");

  // server logs
  const [items, setItems] = useState<LogItem[]>([]);
  const [selected, setSelected] = useState<LogItem | null>(null);
  const [content, setContent] = useState<LogRead | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [autoRefresh, setAutoRefresh] = useState<boolean>(true);
  const [refreshEveryMs] = useState<number>(5000);

  // client logs
  const [clientLogs, setClientLogs] = useState<ClientLogEvent[]>([]);

  // ESC + scroll lock
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", onKey);
    const origOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = origOverflow;
    };
  }, [open, onClose]);

  // subscribe client logs
  useEffect(() => {
    if (!open) return;
    setClientLogs(getClientLogs());
    const unsub = subscribeClientLogs((list) => setClientLogs(list));
    return () => unsub();
  }, [open]);

  // server logs loading
  async function loadServerLogs() {
    setErr(null);
    setLoading(true);
    setContent(null);
    setSelected(null);
    try {
      const it = await fetchRecentLogsGlobal(200, supplierFilter);
      setItems(it);
    } catch (e: any) {
      setErr(e?.message || String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    if (!open || tab !== "server") return;
    void loadServerLogs();
  }, [open, tab, supplierFilter]);

  useEffect(() => {
    if (!open || tab !== "server" || !autoRefresh) return;
    const t = setInterval(() => loadServerLogs(), refreshEveryMs);
    return () => clearInterval(t);
  }, [open, tab, autoRefresh, refreshEveryMs, supplierFilter]);

  async function pick(item: LogItem) {
    setSelected(item);
    setErr(null);
    setContent(null);
    try {
      const c = await readLogGlobal(item.relpath);
      setContent(c);
    } catch (e: any) {
      setErr(e?.message || String(e));
    }
  }

  const iframeSrcDoc = useMemo(() => (content?.is_html ? content?.text : undefined), [content]);

  if (!open) return null;

  return createPortal(
    <div className="fixed inset-0 z-[9999]">
      <div className="absolute inset-0 bg-black/70" onClick={onClose} />
      <div className="absolute inset-0 p-6 overflow-auto">
        <div className="mx-auto max-w-6xl h-[85vh] rounded-2xl border border-white/10 bg-slate-900 shadow-xl flex flex-col">
          {/* Header */}
          <div className="h-12 px-4 border-b border-white/10 flex items-center justify-between">
            <div className="flex items-center gap-2">
              <button
                className={`px-3 py-1.5 rounded-md text-sm ${tab === "server" ? "bg-white/10 text-white" : "text-white/70 hover:text-white"}`}
                onClick={() => setTab("server")}
              >
                Server logs
              </button>
              <button
                className={`px-3 py-1.5 rounded-md text-sm ${tab === "client" ? "bg-white/10 text-white" : "text-white/70 hover:text-white"}`}
                onClick={() => setTab("client")}
              >
                Client activity
              </button>
            </div>
            <div className="flex items-center gap-2">
              {tab === "server" && (
                <>
                  <label className="text-xs text-white/70 flex items-center gap-2">
                    <input
                      type="checkbox"
                      checked={autoRefresh}
                      onChange={(e) => setAutoRefresh(e.currentTarget.checked)}
                    />
                    Auto refresh
                  </label>
                  <button
                    className="text-xs rounded-md border border-white/20 px-2 py-1 text-white/80 hover:bg-white/10"
                    onClick={() => loadServerLogs()}
                  >
                    Refresh now
                  </button>
                </>
              )}
              <button className="text-white/80 hover:text-white text-sm" onClick={onClose}>✕</button>
            </div>
          </div>

          {/* Body */}
          {tab === "server" ? (
            <div className="flex-1 min-h-0 flex">
              <div className="w-80 border-r border-white/10 p-3 flex flex-col bg-slate-850/50">
                <div className="text-xs text-white/60 mb-2">{supplierFilter?.length ? `Filter: ${supplierFilter.join(", ")}` : "All suppliers"}</div>
                {loading && <div className="text-sm text-white/80">Loading…</div>}
                {err && <div className="text-xs text-rose-300">{err}</div>}
                <div className="mt-2 overflow-auto divide-y divide-white/5">
                  {items.map((it) => (
                    <button
                      key={it.relpath}
                      onClick={() => pick(it)}
                      className={`w-full text-left px-2 py-1.5 hover:bg-white/5 ${selected?.relpath === it.relpath ? "bg-white/10" : ""}`}
                      title={it.relpath}
                    >
                      <div className="text-xs font-medium text-white">{it.filename}</div>
                      <div className="text-[10px] text-white/60">{it.supplier} • {it.mtime_iso}</div>
                    </button>
                  ))}
                  {!loading && items.length === 0 && <div className="text-xs text-white/60 p-2">No logs found.</div>}
                </div>
              </div>

              <div className="flex-1 flex flex-col">
                <div className="h-10 border-b border-white/10 px-3 flex items-center justify-between bg-slate-900/60">
                  <div className="text-sm text-white">
                    {selected ? `${selected.filename} — ${selected.supplier}` : "Preview"}
                    {content?.truncated && <span className="ml-2 text-xs text-yellow-300">(truncated)</span>}
                  </div>
                  {selected && (
                    <a
                      className="text-xs text-lime-300 hover:underline"
                      href={`${API_BASE}/files/download?relpath=${encodeURIComponent(selected.relpath)}`}
                      target="_blank"
                      rel="noreferrer"
                    >
                      download
                    </a>
                  )}
                </div>

                <div className="flex-1 overflow-auto bg-slate-950/60">
                  {!selected && <div className="p-4 text-white/70 text-sm">Select a log to preview…</div>}
                  {selected && content && (
                    content.is_html ? (
                      <iframe title="log-html" className="w-full h-full" sandbox="" srcDoc={iframeSrcDoc} />
                    ) : (
                      <pre className="p-4 text-xs whitespace-pre-wrap break-words text-white/90 font-mono">
                        {content.text}
                      </pre>
                    )
                  )}
                </div>
              </div>
            </div>
          ) : (
            // CLIENT ACTIVITY
            <div className="flex-1 min-h-0">
              <div className="h-full overflow-auto p-3 bg-slate-950/60">
                {clientLogs.length === 0 && (
                  <div className="text-white/70 text-sm">No client activity yet. Vykonaj akciu (napr. refresh invoices)…</div>
                )}
                {clientLogs.map((e, i) => (
                  <div key={i} className={`text-xs font-mono py-0.5 ${e.level === "error" ? "text-rose-300" : "text-white/85"}`}>
                    [{new Date(e.ts).toLocaleTimeString()}] {e.dir} {e.method} {e.url}
                    {e.status != null ? ` — ${e.status}` : ""}
                    {e.note ? ` — ${e.note}` : ""}
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>,
    document.body
  );
}
