import React, { useState } from "react";
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

  const btn = "inline-flex items-center rounded-2xl px-3 py-1.5 text-sm font-medium border border-lime-500 text-lime-400 hover:bg-lime-500/10 transition";

  function openLogs() {
    const url = `${API_BASE}/files/download?relpath=${encodeURIComponent(LOG_REL_PATH)}&disposition=inline&filename=inventory_hub.log&stable=1`;
    window.open(url, "_blank", "noopener");
  }

  return (
    <div className="min-h-screen bg-slate-950 text-white">
      <header className="sticky top-0 z-40 border-b border-white/10 bg-slate-950/80 backdrop-blur">
        <div className="mx-auto max-w-6xl px-4 py-3 flex items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <button className={btn} onClick={() => setSupplierCfgOpen(true)}>Supplier config</button>
            <button className={btn} onClick={() => setShopCfgOpen(true)}>Shop config</button>
            <button className={btn} onClick={() => setConsoleCfgOpen(true)}>Console config</button>
            <button className={btn} onClick={openLogs} title={`Open ${LOG_REL_PATH}`}>View logs</button>
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
