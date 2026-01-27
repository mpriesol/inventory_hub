
import React, { useEffect, useState } from "react";
import { Modal } from "./components/ui/Modal";
import { SupplierPicker } from "./features/suppliers/SupplierPicker";
import { API_BASE, fetchJSON } from "./api/client";
import { SupplierConfigForm } from "./features/configs/SupplierConfigForm";
import { ShopConfigForm } from "./features/configs/ShopConfigForm";
import { ConsoleConfigForm } from "./features/configs/ConsoleConfigForm";
import { InvoicesTable } from "./features/suppliers/InvoicesTable";
import { RefreshInvoicesButton } from "./features/RefreshInvoicesButton";
import InvoicesPanel from "./features/invoices/InvoicesPanel";
import GlobalLogsButton from "./features/logs/GlobalLogsButton";
import { setClientLogger, type ClientLogEvent } from "./api/client";

type ListedFile = { name?: string; path?: string; href?: string };
type Log = { ts: string; level: "info"|"error"; msg: string };

const [logs, setLogs] = useState<Log[]>([]);
function log(level: "info" | "error", msg: string) {
  setLogs((prev) => [...prev, { ts: new Date().toLocaleTimeString(), level, msg }].slice(-200));
}
const logInfo = (m: string) => log("info", m);
const logError = (m: string) => log("error", m);

useEffect(() => {
  setClientLogger((e: ClientLogEvent) => {
    log(e.level, e.msg);
  });
  return () => setClientLogger(null);
}, []);

async function tryFiles(areaVariants: string[], supplier: string) {
  for (const area of areaVariants) {
    try {
      const res = await fetch(`${API_BASE}/suppliers/${supplier}/files?area=${encodeURIComponent(area)}`);
      const ct = res.headers.get("content-type") || "";
      if (!res.ok) {
        let detail = `${res.status} ${res.statusText}`;
        try {
          const body = ct.includes("application/json") ? await res.json() : await res.text();
          const d = typeof body === "string" ? body : (body as any)?.detail;
          if (d) detail += `\n${typeof d === "string" ? d : JSON.stringify(d)}`;
        } catch {}
        throw new Error(detail);
      }
      const data = ct.includes("application/json") ? await res.json() : {};
      const arr: string[] = Array.isArray(data) ? data : ((data as any)?.files ?? []);
      return arr.map((relpath: string) => ({ name: relpath.split("/").pop() || relpath, path: relpath })) as ListedFile[];
    } catch (e: any) {
      if (!/^400|404/.test(String(e.message))) throw e;
    }
  }
  return [];
}

export default function App() {
  // Selection
  const [supplier, setSupplier] = useState<string>("paul-lange");
  const [shop, setShop] = useState<string>("biketrek");
  const [supplierName, setSupplierName] = useState<string>("paul-lange");
  const [shopName, setShopName] = useState<string>("biketrek");

  // Configs
  const [effSupplierCfg, setEffSupplierCfg] = useState<any>(null);
  const [effShopCfg, setEffShopCfg] = useState<any>(null);
  const [shopCfg, setShopCfg] = useState<any>(null); // direct shop config (for URL enabling)

  // Files
  const [feedsConverted, setFeedsConverted] = useState<ListedFile[]>([]);
  const [feedsRaw, setFeedsRaw] = useState<ListedFile[]>([]);

  // Export status
  const [exportStatus, setExportStatus] = useState<any>(null);

  // Modals
  const [supplierCfgOpen, setSupplierCfgOpen] = useState(false);
  const [shopCfgOpen, setShopCfgOpen] = useState(false);
  const [consoleCfgOpen, setConsoleCfgOpen] = useState(false);

  // Logs
  const [logs, setLogs] = useState<Log[]>([]);
  function log(level: "info"|"error", msg: string) {
    setLogs((prev) => [...prev, { ts: new Date().toLocaleTimeString(), level, msg }].slice(-200));
  }
  const logInfo = (m: string) => log("info", m);
  const logError = (m: string) => log("error", m);

  // Loaders
  async function loadEffectiveConfigs() {
    try { const sc = await fetchJSON<any>(`${API_BASE}/configs/effective/supplier?supplier=${encodeURIComponent(supplier)}`); setEffSupplierCfg(sc); } catch {}
    try { const sh = await fetchJSON<any>(`${API_BASE}/configs/effective/shop?shop=${encodeURIComponent(shop)}`); setEffShopCfg(sh); } catch {}
  }
  async function loadShopConfig() {
    try {
      const res = await fetch(`${API_BASE}/shops/${shop}/config`, { cache: "no-store" });
      const txt = await res.text();
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}: ${txt.slice(0,200)}`);
      setShopCfg(JSON.parse(txt || "{}"));
    } catch (e: any) {
      logError(`GET /shops/${shop}/config failed: ${e?.message || e}`);
    }
  }
  async function loadExportStatus() {
    try {
      const res = await fetch(`${API_BASE}/shops/${shop}/export/status`);
      const txt = await res.text();
      if (!res.ok) { logError(`GET /shops/${shop}/export/status ${res.status} ${res.statusText}: ${txt}`); return; }
      setExportStatus(JSON.parse(txt || "{}"));
    } catch (e: any) {
      logError(`GET /shops/${shop}/export/status failed: ${e?.message || e}`);
    }
  }
  async function loadFiles() {
    const [conv, raw] = await Promise.all([
      tryFiles(["feeds_converted"], supplier),
      tryFiles(["feeds", "feeds_raw"], supplier),
    ]);
    setFeedsConverted(conv || []);
    setFeedsRaw(raw || []);
  }

  useEffect(() => { loadEffectiveConfigs(); loadShopConfig(); loadExportStatus(); loadFiles(); }, [supplier, shop]);

  // Supplier actions
  async function supplierFetchConvert() {
    try {
      const out = await fetchJSON<{ raw_path?: string; converted_csv?: string }>(
        `${API_BASE}/suppliers/${supplier}/feeds/refresh`,
        { method: "POST", headers: { "Content-Type": "application/json" } }
      );
      logInfo(`Supplier feed OK → raw: ${out.raw_path ?? "?"}, converted: ${out.converted_csv ?? "?"}`);
      await loadFiles();
    } catch (e: any) { logError(`Fetch+Convert failed: ${e?.message || e}`); }
  }
  async function supplierUseLocalAndConvert() {
    const path = effSupplierCfg?.feeds?.current_path;
    if (!path) { logError("Local feed path is not set."); return; }
    try {
      const out = await fetchJSON<{ raw_path?: string; converted_csv?: string }>(
        `${API_BASE}/suppliers/${supplier}/feeds/refresh`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          //body: JSON.stringify({ source_url: String(path).replaceAll("\\\\", "/") }),
          body: JSON.stringify({ source_url: String(path)})
        }
      );
      logInfo(`Used local feed → raw: ${out.raw_path ?? "?"}, converted: ${out.converted_csv ?? "?"}`);
      await loadFiles();
    } catch (e: any) { logError(`Local feed failed: ${e?.message || e}`); }
  }

  // Shop actions
  const [shopUploadFile, setShopUploadFile] = useState<File | null>(null);
  async function uploadShopExportLocal() {
    try {
      if (!shopUploadFile) { logError("Choose a file to upload."); return; }
      const fd = new FormData();
      fd.append("file", shopUploadFile);
      const res = await fetch(`${API_BASE}/shops/${shop}/export/upload`, { method: "POST", body: fd });
      const txt = await res.text();
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}: ${txt}`);
      const data = JSON.parse(txt || "{}");
      logInfo(`Export uploaded: ${data?.saved_path || "(unknown)"}`);
      setShopUploadFile(null);
      await loadExportStatus();
    } catch (e: any) {
      logError(`Upload failed: ${e?.message || e}`);
    }
  }
  async function shopFetchExportFromLink() {
    logInfo(`POST /shops/${shop}/export/refresh`);
    try {
      const res = await fetch(`${API_BASE}/shops/${shop}/export/refresh`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
      });
      const txt = await res.text();
      if (!res.ok) { logError(`Download failed: ${res.status} ${res.statusText}: ${txt}`); return; }
      const out = JSON.parse(txt || "{}");
      logInfo(`Downloaded: ${JSON.stringify(out)}`);
      await loadExportStatus();
    } catch (e: any) {
      logError(`Download failed: ${e?.message || e}`);
    }
  }

  // Derived values
  const latestConverted = feedsConverted?.[0];
  const latestRaw = feedsRaw?.[0];
  const exportUrl = shopCfg?.upgates_full_export_url_csv
    || shopCfg?.api_export_url
    || shopCfg?.upgates?.api_export_url
    || null;

  // Styles
  const btnGreen = "inline-flex items-center rounded-2xl px-3 py-1.5 text-sm font-medium bg-lime-500 text-slate-950 hover:bg-lime-400 transition";
  const btnOutline = "inline-flex items-center rounded-2xl px-3 py-1.5 text-sm font-medium border border-lime-500 text-lime-400 hover:bg-lime-500/10 transition";

  // Openers (reload config on open)
  function openShopConfig() {
    loadShopConfig().finally(() => setShopCfgOpen(true));
  }

  return (
    <div className="min-h-screen bg-slate-950 text-white">
      {/* Header */}
      <header className="sticky top-0 z-20 border-b border-white/10 bg-slate-950/80 backdrop-blur">
        <div className="mx-auto max-w-6xl px-4 py-3 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="h-8 w-8 rounded-xl bg-lime-500" />
            <div>
              <h1 className="text-xl font-bold leading-tight">Supplier Console</h1>
              <div className="text-xs text-white/60">API: {API_BASE}</div>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <GlobalLogsButton
              clientLogs={logs.map((l) => ({
                ts: Date.now(), // z tvojho formátu; ak chceš presnejší čas, ulož si epoch pri log() do separátneho poľa
                level: l.level,
                msg: `[${l.ts}] ${l.level.toUpperCase()}: ${l.msg}`
              }))}
            />
            <button className={btnOutline} onClick={() => setConsoleCfgOpen(true)}>Console config</button>
            <a href="#" className="text-xs text-white/60 hover:text-white" target="_blank" rel="noreferrer">
              v0.1 • BikeTrek Inventory Hub
            </a>
          </div>
        </div>
      </header>

      {/* Two-column top area */}
      <div className="mx-auto max-w-6xl px-4 py-4 grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* SUPPLIER */}
        <div className="rounded-2xl border border-white/10 p-4 bg-slate-950/50">
          <div className="text-xs uppercase tracking-wider text-white/60 mb-1">SUPPLIER</div>
          <div className="flex items-center justify-between mb-3">
            <h2 className="font-semibold">{supplierName || supplier}</h2>
            <button className={btnOutline} onClick={() => setSupplierCfgOpen(true)}>Config</button>
          </div>
          <SupplierPicker
            label="SUPPLIER"
            mode="suppliers"
            value={supplier}
            onChange={setSupplier}
            onResolvedValue={(it) => setSupplierName(it?.name || supplier)}
          />
          <div className="mt-3 text-xs text-white/70">
            {effSupplierCfg?.feeds?.mode === "local" && effSupplierCfg?.feeds?.current_path ? (
              <div>Feed: <b>local</b> — <span className="break-all">{effSupplierCfg.feeds.current_path}</span></div>
            ) : (
              <div>Feed: <b>remote</b> (download)</div>
            )}
            {latestConverted ? (
              <div className="mt-1">Latest CSV: <span className="break-all">{latestConverted.path}</span></div>
            ) : null}
            {latestRaw ? (
              <div>RAW: <span className="break-all">{latestRaw.path}</span></div>
            ) : null}
          </div>
          <div className="mt-3 flex flex-wrap gap-2">
            {effSupplierCfg?.feeds?.mode === "local" ? (
              <>
                <button className={btnGreen} onClick={supplierUseLocalAndConvert}>Use local + Convert</button>
                {latestConverted ? <button className={btnOutline} onClick={() => { /* preview */ }}>Preview CSV</button> : null}
              </>
            ) : (
              <>
                <button className={btnGreen} onClick={supplierFetchConvert}>Fetch + Convert</button>
                {latestConverted ? <button className={btnOutline} onClick={() => { /* preview */ }}>Preview CSV</button> : null}
                {latestRaw ? (
                  <a className={btnOutline} href={`${API_BASE}/files/download?relpath=${encodeURIComponent(latestRaw.path)}`} target="_blank" rel="noreferrer">Download RAW</a>
                ) : null}
              </>
            )}
          </div>
        </div>

        {/* SHOP */}
        <div className="rounded-2xl border border-white/10 p-4 bg-slate-950/50">
          <div className="text-xs uppercase tracking-wider text-white/60 mb-1">SHOP</div>
          <div className="flex items-center justify-between mb-3">
            <h2 className="font-semibold">{shopName || shop}</h2>
            <button className={btnOutline} onClick={openShopConfig}>Config</button>
          </div>

          <SupplierPicker
            label="SHOP"
            mode="shops"
            value={shop}
            onChange={setShop}
            onResolvedValue={(it) => setShopName(it?.name || shop)}
            items={[{code:"biketrek",name:"biketrek"},{code:"xtrek",name:"xtrek"}]}
          />

          <div className="mt-3 flex flex-col gap-3">
            <div className="flex items-center gap-2">
              <input
                type="file"
                accept=".csv,text/csv"
                onChange={(e) => setShopUploadFile(e.target.files?.[0] || null)}
                className="text-sm file:mr-3 file:rounded-xl file:border file:border-white/10 file:bg-slate-900 file:px-3 file:py-1.5 file:text-sm file:text-white hover:file:bg-slate-800"
              />
              <button className={btnGreen} onClick={uploadShopExportLocal}>Upload export (local)</button>
              <button
                className={btnOutline}
                onClick={shopFetchExportFromLink}
                disabled={! (shopCfg?.upgates_full_export_url_csv || shopCfg?.api_export_url || shopCfg?.upgates?.api_export_url) }
                title={! (shopCfg?.upgates_full_export_url_csv || shopCfg?.api_export_url || shopCfg?.upgates?.api_export_url)
                  ? "Set Full export CSV URL in Shop config first."
                  : ""}
              >
                Download export (link)
              </button>
            </div>

            {/* Minimal status */}
            <div className="text-xs text-white/70">
              Using: <span className="break-all">{(effShopCfg?.upgates?.export_override_path || `shops/${shop}/latest.csv`)}</span>
              {exportStatus?.last_downloaded_at ? (
                <span> · Last downloaded: {new Date(exportStatus.last_downloaded_at).toLocaleString()}</span>
              ) : null}
            </div>

            {/* Logs */}
            <div className="rounded-xl border border-white/10 bg-slate-950/60">
              <div className="px-3 py-2 text-xs uppercase tracking-wider text-white/60 border-b border-white/10">Logs</div>
              <div className="max-h-40 overflow-auto px-3 py-2 text-xs font-mono">
                {logs.length === 0 ? <div className="text-white/40">No logs yet.</div> : null}
                {logs.map((l, i) => (
                  <div key={i} className={l.level === "error" ? "text-rose-400" : "text-white/70"}>
                    [{l.ts}] {l.level.toUpperCase()}: {l.msg}
                  </div>
                ))}
              </div>
            </div>
          </div>
        </div>
      </div>
      <InvoicesPanel supplier={supplier} defaultMonths={3} />
      <footer className="mx-auto max-w-6xl px-4 py-8 text-xs text-white/50">
        © {new Date().getFullYear()} BikeTrek · Inventory Hub
      </footer>

      {/* Modals */}
      <Modal open={consoleCfgOpen} onClose={() => setConsoleCfgOpen(false)} title="Console config">
        <ConsoleConfigForm />
      </Modal>

      <Modal open={supplierCfgOpen} onClose={() => setSupplierCfgOpen(false)} title="Supplier config">
        <SupplierConfigForm supplier={supplier} />
      </Modal>

      <Modal
        open={shopCfgOpen}
        onClose={() => { setShopCfgOpen(false); loadShopConfig(); }}
        title="Shop config"
      >
        <ShopConfigForm shop={shop} open={shopCfgOpen} initial={shopCfg} onSaved={() => loadShopConfig()} />
      </Modal>
    </div>
  );
}
