import React from "react";
import { useLocalStorage } from "@/hooks/useLocalStorage";
import type { ConsoleConfigLocal } from "@/types/config";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/Input";
import { Label } from "@/components/ui/Label";

export function ConsoleConfigForm() {
  const [cfg, setCfg] = useLocalStorage<ConsoleConfigLocal>("console_config", {
    inventoryDataRoot: "C:/!kafe/BikeTrek/web/inventory-data",
    darkSidebar: true,
  });
  const [, setSaved] = useLocalStorage<string | null>("console_config_saved", null);

  const save = () => {
    setSaved("Saved ✓ (local only)");
    setTimeout(() => setSaved(null), 1500);
  };

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div className="space-y-2 md:col-span-2">
          <Label>INVENTORY_DATA_ROOT (UI hint)</Label>
          <Input
            value={cfg.inventoryDataRoot || ""}
            onChange={(e) => setCfg({ ...cfg, inventoryDataRoot: e.target.value })}
            placeholder="C:/!kafe/BikeTrek/web/inventory-data"
          />
          <p className="text-xs text-neutral-400">Stored in browser only; backend source of truth remains .env.</p>
        </div>
        <div className="space-y-2">
          <Label className="inline-flex items-center gap-2">
            <input
              type="checkbox"
              className="h-4 w-4 rounded border"
              checked={!!cfg.darkSidebar}
              onChange={(e) => setCfg({ ...cfg, darkSidebar: e.target.checked })}
            />
            <span>Dark sidebar (UI theme)</span>
          </Label>
        </div>
      </div>

      <div className="flex items-center gap-3">
        <Button onClick={save}>Save UI settings</Button>
      </div>
    </div>
  );
}
