import React, { useEffect, useState } from "react";
import SupplierConfigModal from "./features/configs/SupplierConfigModal";
import ShopConfigModal from "./features/configs/ShopConfigModal";
import InvoicesPanel from "./features/invoices/InvoicesPanel";
import { Modal } from "./components/ui/Modal";
import { ConsoleConfigForm } from "./features/configs/ConsoleConfigForm";
import ImportConsolePanel from "@/features/import_console/ImportConsolePanel";

const API_BASE: string = (import.meta as any).env?.VITE_API_BASE || "http://127.0.0.1:8000";
const LOG_REL_PATH = "logs/inventory_hub.log";

export default function App() {
  const [supplier, setSupplier] = useState<string>("paul-lange");
  const [shop, setShop] = useState<string>("biketrek");

  const [supplierCfgOpen, setSupplierCfgOpen] = useState(false);
  const [shopCfgOpen, setShopCfgOpen] = useState(false);
  const [consoleCfgOpen, setConsoleCfgOpen] = useState(false);

  const btn =
    "inline-flex items-center rounded-2xl px-3 py-1.5 text-sm font-medium border border-lime-500 text-lime-400 hover:bg-lime-500/10 transition";

  function openLogs() {
    const url = `${API_BASE}/files/download?relpath=${encodeURIComponent(
      LOG_REL_PATH
    )}&disposition=inline&filename=inventory_hub.log&stable=1`;
    window.open(url, "_blank", "noopener");
  }

  // --- Config Admin (unified JSON editor) ---
  const [configAdminOpen, setConfigAdminOpen] = useState(false);
  const [adminTab, setAdminTab] = useState<"console" | "shop" | "supplier">("console");
  const [jsonText, setJsonText] = useState<string>("");
  const [loadingCfg, setLoadingCfg] = useState(false);
  const [savingCfg, setSavingCfg] = useState(false);
  const [cfgError, setCfgError] = useState<string | null>(null);

  async function loadConfig(tab: "console" | "shop" | "supplier") {
    setLoadingCfg(true);
    setCfgError(null);
    try {
      let url = "";
      if (tab === "console") url = `${API_BASE}/configs/console`;
      if (tab === "shop")    url = `${API_BASE}/shops/${encodeURIComponent(shop)}/config`;
      if (tab === "supplier")url = `${API_BASE}/suppliers/${encodeURIComponent(supplier)}/config`;

      const res = await fetch(url);
      if (!res.ok) {
        let detail = ""; try { detail = await res.text(); } catch {}
        throw new Error(`GET ${url} → ${res.status}${detail ? ` · ${detail}` : ""}`);
      }
      const data = await res.json();
      setJsonText(JSON.stringify(data, null, 2));
    } catch (e: any) {
      setCfgError(String(e?.message || e)); // ← zobrazí chybu, editor nechá nedotknutý
    } finally {
      setLoadingCfg(false);
    }
  }


  async function saveConfig() {
    setSavingCfg(true);
    setCfgError(null);
    try {
      const parsed = JSON.parse(jsonText || "{}");
      let url = "";
      let method = "POST";
      if (adminTab === "console") url = `${API_BASE}/configs/console`;
      if (adminTab === "shop") {
        url = `${API_BASE}/shops/${encodeURIComponent(shop)}/config`;
        method = "PUT";
      }
      if (adminTab === "supplier") {
        url = `${API_BASE}/suppliers/${encodeURIComponent(supplier)}/config`;
        method = "PUT";
      }
      const res = await fetch(url, {
        method,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(parsed),
      });
      if (!res.ok) {
        let t = "";
        try {
          t = await res.text();
        } catch {}
        throw new Error(`${method} ${url} → ${res.status} ${t}`);
      }
      await loadConfig(adminTab);
    } catch (e: any) {
      setCfgError(String(e?.message || e));
    } finally {
      setSavingCfg(false);
    }
  }

  useEffect(() => {
    if (configAdminOpen) loadConfig(adminTab);
  }, [configAdminOpen]);

  useEffect(() => {
    if (configAdminOpen) loadConfig(adminTab);
  }, [adminTab, configAdminOpen]);

  return (
    <div className="min-h-screen bg-slate-950 text-white">
      <header className="sticky top-0 z-40 border-b border-white/10 bg-slate-950/80 backdrop-blur">
        <div className="mx-auto max-w-6xl px-4 py-3 flex items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <button className={btn} onClick={() => setSupplierCfgOpen(true)}>
              Supplier config
            </button>
            <button className={btn} onClick={() => setShopCfgOpen(true)}>
              Shop config
            </button>
            <button className={btn} onClick={() => setConsoleCfgOpen(true)}>
              Console config
            </button>
            <button className={btn} onClick={() => setConfigAdminOpen(true)}>
              Config admin
            </button>
            <button className={btn} onClick={openLogs} title={`Open ${LOG_REL_PATH}`}>
              View logs
            </button>
          </div>
          <div className="flex items-center gap-3 text-sm text-white/70">
            <div className="flex items-center gap-1">
              <span className="text-white/40">Supplier:</span>
              <span className="font-medium text-white/80">{supplier}</span>
            </div>
            <span className="text-white/25">•</span>
            <div className="flex items-center gap-1">
              <span className="text-white/40">Shop:</span>
              <span className="font-medium text-white/80">{shop}</span>
            </div>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-6xl p-4">
        <div className="rounded-2xl border border-white/10 bg-slate-950/50">
          <div className="px-4 py-2 text-xs uppercase tracking-wider text-white/60 border-b border-white/10">
            Invoices
          </div>
          <div className="p-4">
            <InvoicesPanel supplier={supplier} shop={shop} defaultMonths={3} />
          </div>
          <div className="mt-8">
            <ImportConsolePanel supplier={supplier} shop={shop} />
          </div>
        </div>
      </main>

      <Modal open={configAdminOpen} onClose={() => setConfigAdminOpen(false)} title="Config admin" align="top">
        <div className="w-[88vw] md:w-[84vw] max-w-[1200px]">
          {/* Sticky toolbar – Save nikdy nezmizne */}
          <div className="sticky top-0 z-10 bg-slate-950/95 backdrop-blur px-2 pt-1 pb-2 border-b border-white/10">
            <div className="flex flex-wrap items-center gap-2">
              <button
                className={`px-3 py-1.5 rounded-xl text-sm border ${adminTab==='console' ? 'bg-lime-600/20 border-lime-500 text-lime-300' : 'border-white/10 text-white/70 hover:bg-white/5'}`}
                onClick={() => setAdminTab('console')}
              >
                Console
              </button>
              <button
                className={`px-3 py-1.5 rounded-xl text-sm border ${adminTab==='shop' ? 'bg-lime-600/20 border-lime-500 text-lime-300' : 'border-white/10 text-white/70 hover:bg-white/5'}`}
                onClick={() => setAdminTab('shop')}
              >
                Shop: <span className="opacity-80">{shop}</span>
              </button>
              <button
                className={`px-3 py-1.5 rounded-xl text-sm border ${adminTab==='supplier' ? 'bg-lime-600/20 border-lime-500 text-lime-300' : 'border-white/10 text-white/70 hover:bg-white/5'}`}
                onClick={() => setAdminTab('supplier')}
              >
                Supplier: <span className="opacity-80">{supplier}</span>
              </button>

              <div className="ml-auto flex flex-wrap items-center gap-2">
                <button
                  className="px-3 py-1.5 rounded-xl text-sm border border-white/10 text-white/80 hover:bg-white/5"
                  onClick={() => { try { setJsonText(JSON.stringify(JSON.parse(jsonText||"{}"), null, 2)); } catch {} }}
                >
                  Format
                </button>
                <button
                  className="px-3 py-1.5 rounded-xl text-sm border border-lime-500 text-lime-300 hover:bg-lime-600/20 disabled:opacity-50"
                  disabled={savingCfg}
                  onClick={saveConfig}
                  title="Save configuration"
                >
                  {savingCfg ? 'Saving…' : 'Save'}
                </button>
              </div>
            </div>
          </div>

          {/* Scrollovateľné telo */}
          {cfgError && (
            <div className="mt-2 mb-3 text-sm rounded-lg border border-red-500/30 bg-red-500/10 text-red-300 px-3 py-2">
              {cfgError}
            </div>
          )}
          <div className="relative max-h-[70vh] overflow-auto">
            {loadingCfg && (
              <div className="absolute inset-0 bg-black/30 flex items-center justify-center text-white/70 text-sm">Loading…</div>
            )}
            <textarea
              value={jsonText}
              onChange={(e) => setJsonText(e.target.value)}
              className="w-full min-h-[56vh] font-mono text-sm bg-slate-950 border border-white/10 rounded-2xl p-3 outline-none focus:ring-1 focus:ring-lime-500"
              spellCheck={false}
            />
          </div>

          <div className="mt-3 text-xs text-white/40">
            Endpoints: {adminTab==='console' && <code>/configs/console (GET/POST)</code>}
            {adminTab==='shop' && <code className="ml-2">/shops/{shop}/config (GET/PUT)</code>}
            {adminTab==='supplier' && <code className="ml-2">/suppliers/{supplier}/config (GET/PUT)</code>}
          </div>
        </div>
      </Modal>



      <SupplierConfigModal
        open={supplierCfgOpen}
        onClose={() => setSupplierCfgOpen(false)}
        initialSupplier={supplier}
        onSupplierChange={(s: string) => setSupplier(s)}
      />

      <ShopConfigModal
        open={shopCfgOpen}
        onClose={() => setShopCfgOpen(false)}
        initialShop={shop}
        onShopChange={(s: string) => setShop(s)}
      />

      <Modal open={consoleCfgOpen} onClose={() => setConsoleCfgOpen(false)} title="Console config" align="top">
        <ConsoleConfigForm />
      </Modal>
    </div>
  );
}
