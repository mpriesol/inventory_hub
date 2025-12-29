import React, { useState } from "react";
import SupplierConfigModal from "./features/configs/SupplierConfigModal";
import ShopConfigModal from "./features/configs/ShopConfigModal";

export default function App(){
  const [supplierOpen, setSupplierOpen] = useState(false);
  const [shopOpen, setShopOpen] = useState(false);

  return (
    <div className="min-h-screen bg-slate-950 text-white">
      <header className="sticky top-0 z-20 border-b border-white/10 bg-slate-950/80 backdrop-blur">
        <div className="mx-auto max-w-6xl px-4 py-3 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="h-8 w-8 rounded-xl bg-lime-500" />
            <div>
              <h1 className="text-xl font-bold leading-tight">BikeTrek — Inventory Hub</h1>
              <div className="text-xs text-white/60">Supplier Console</div>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <button className="rounded-2xl border border-lime-500 px-3 py-1.5 text-sm text-lime-400 hover:bg-lime-500/10" onClick={()=>setSupplierOpen(true)}>Supplier config</button>
            <button className="rounded-2xl border border-lime-500 px-3 py-1.5 text-sm text-lime-400 hover:bg-lime-500/10" onClick={()=>setShopOpen(true)}>Shop config</button>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-6xl px-4 py-10">
        <p className="text-white/60">Use the buttons above to configure your supplier and shop. Old SUPPLIER/SHOP panels were removed as requested.</p>
      </main>

      <SupplierConfigModal open={supplierOpen} onClose={()=>setSupplierOpen(false)} initialSupplier="paul-lange" onSaved={()=>{}}/>
      <ShopConfigModal open={shopOpen} onClose={()=>setShopOpen(false)} initialShop="biketrek"/>
    </div>
  );
}
