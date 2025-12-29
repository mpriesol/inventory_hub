import React, { useEffect, useState } from "react";
import type { ShopConfig } from "@/types/config";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/Input";
import { Label } from "@/components/ui/Label";

export function ShopConfigForm({ shop }: { shop: string }) {
  const [cfg, setCfg] = useState<ShopConfig>({ shop_code: shop, upgates: { export_override_path: "" } });
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<null | string>(null);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError(null);
    api
      .get<ShopConfig>(`/configs/effective/shop?shop=${shop}`)
      .then((data) => active && setCfg({ ...data, shop_code: shop }))
      .catch((e) => active && setError((e as Error).message))
      .finally(() => active && setLoading(false));
    return () => {
      active = false;
    };
  }, [shop]);

  const save = async () => {
    setSaving(true);
    setError(null);
    setSaved(null);
    try {
      await api.put(`/shops/${shop}/config`, cfg);
      setSaved("Saved ✓");
    } catch (e) {
      setError((e as Error).message + " — Tip: implement PUT /shops/{shop}/config on backend.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="space-y-6">
      {loading ? <p className="text-sm text-neutral-400">Loading…</p> : null}
      {error ? <p className="text-sm text-red-500">{error}</p> : null}

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div className="space-y-2 md:col-span-2">
          <Label>Upgates export override (local file or URL)</Label>
          <Input
            value={cfg.upgates?.export_override_path || ""}
            onChange={(e) => setCfg((c) => ({ ...c, upgates: { ...c.upgates, export_override_path: e.target.value } }))}
            placeholder="C:/!kafe/BikeTrek/web/inventory-data/shops/xtrek/export-products-20251024.csv"
          />
          <p className="text-xs text-neutral-400">If empty, backend uses shops/{shop}/latest.csv as usual.</p>
        </div>
      </div>

      <div className="flex items-center gap-3">
        <Button onClick={save} disabled={saving}>Save shop config</Button>
        {saved ? <span className="text-sm text-green-500">{saved}</span> : null}
      </div>
    </div>
  );
}
