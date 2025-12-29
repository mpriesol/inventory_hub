
import React from "react";
import { ConsoleConfigForm } from "./ConsoleConfigForm";
import { ShopConfigForm } from "./ShopConfigForm";
import { SupplierConfigForm } from "./SupplierConfigForm";
import { EffectiveConfigPanel } from "./EffectiveConfigPanel";

export function ConfigPage({ shop, supplier }: { shop: string; supplier: string }) {
  return (
    <div className="space-y-6">
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <div>
          <h3 className="text-sm text-white/70 mb-2">Console</h3>
          <ConsoleConfigForm />
        </div>
        <div>
          <h3 className="text-sm text-white/70 mb-2">Shop: {shop}</h3>
          <ShopConfigForm shop={shop} />
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <div>
          <h3 className="text-sm text-white/70 mb-2">Supplier: {supplier}</h3>
          <SupplierConfigForm supplier={supplier} />
        </div>
        <div>
          <h3 className="text-sm text-white/70 mb-2">Effective</h3>
          <EffectiveConfigPanel shop={shop} supplier={supplier} />
        </div>
      </div>
    </div>
  );
}
