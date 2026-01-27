import React, { useState } from "react";
import SupplierConfigModal from "./features/configs/SupplierConfigModal";
import ShopConfigModal from "./features/configs/ShopConfigModal";
import InvoicesPanel from "./features/invoices/InvoicesPanel";
import { Modal } from "./components/ui/Modal";
import { ConsoleConfigForm } from "./features/configs/ConsoleConfigForm";

export default function App() {
  // Keep selected supplier/shop in state (used as initial values for modals & InvoicesPanel)
  const [supplier, setSupplier] = useState<string>("paul-lange");
  const [shop, setShop] = useState<string>("biketrek");

  // Modals
  const [supplierCfgOpen, setSupplierCfgOpen] = useState(false);
  const [shopCfgOpen, setShopCfgOpen] = useState(false);
  const [consoleCfgOpen, setConsoleCfgOpen] = useState(false);

  // Unified button style (same for all three — matches our outline style used elsewhere)
  const btn = "inline-flex items-center rounded-2xl px-3 py-1.5 text-sm font-medium border border-lime-500 text-lime-400 hover:bg-lime-500/10 transition";

  return (
    <div className="min-h-screen bg-slate-950 text-white">
      {/* Header: only three buttons that open modals */}
      <header className="sticky top-0 z-40 border-b border-white/10 bg-slate-950/80 backdrop-blur">
        <div className="mx-auto max-w-6xl px-4 py-3 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <button className={btn} onClick={() => setSupplierCfgOpen(true)}>Supplier config</button>
            <button className={btn} onClick={() => setShopCfgOpen(true)}>Shop config</button>
            <button className={btn} onClick={() => setConsoleCfgOpen(true)}>Console config</button>
          </div>
          <div /> {/* spacer/right side empty for now */}
        </div>
      </header>

      {/* Main: ONLY invoices panel */}
      <main className="mx-auto max-w-6xl p-4">
        <div className="rounded-2xl border border-white/10 bg-slate-950/50">
          <div className="px-4 py-2 text-xs uppercase tracking-wider text-white/60 border-b border-white/10">
            Invoices
          </div>
          <div className="p-4">
            {/* Keep the API exactly as requested */}
            <InvoicesPanel supplier={supplier} shop={shop} defaultMonths={3} />
          </div>
        </div>
      </main>

      {/* Modals (ESC to close handled by your Modal components) */}
      <SupplierConfigModal
        open={supplierCfgOpen}
        onClose={() => setSupplierCfgOpen(false)}
        initialSupplier={supplier}
      />
      <ShopConfigModal
        open={shopCfgOpen}
        onClose={() => setShopCfgOpen(false)}
        initialShop={shop}
      />
      <Modal open={consoleCfgOpen} onClose={() => setConsoleCfgOpen(false)} title="Console config">
        <ConsoleConfigForm />
      </Modal>
    </div>
  );
}
