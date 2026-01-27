import React, { useEffect, useState } from "react";
import type { SupplierConfig } from "@/types/config";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/Input";
import { Label } from "@/components/ui/Label";

export function SupplierConfigForm({ supplier }: { supplier: string }) {
  const [cfg, setCfg] = useState<SupplierConfig>({
    feeds: { mode: "remote" },
    downloader: { layout: "flat", auth: {} },
  });
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<null | string>(null);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError(null);
    api
      .get<SupplierConfig>(`/suppliers/${supplier}/config`)
      .then((data) => {
        if (active) setCfg({ ...cfg, ...data });
      })
      .catch(async () => {
        try {
          const eff = await api.get<SupplierConfig>(`/configs/effective/supplier?supplier=${supplier}`);
          if (active) setCfg({ ...cfg, ...eff });
        } catch (e) {
          if (active) setError((e as Error).message);
        }
      })
      .finally(() => active && setLoading(false));
    return () => {
      active = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [supplier]);

  const save = async () => {
    setSaving(true);
    setError(null);
    setSaved(null);
    try {
      await api.put(`/suppliers/${supplier}/config`, cfg);
      setSaved("Saved ✓");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="space-y-6">
      {loading ? <p className="text-sm text-neutral-400">Loading…</p> : null}
      {error ? <p className="text-sm text-red-500">{error}</p> : null}

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div className="space-y-2">
          <Label>Feed mode</Label>
          <select
            className="w-full rounded-xl border border-neutral-700 bg-neutral-900 px-3 py-2 text-sm"
            value={cfg.feeds.mode}
            onChange={(e) => setCfg((c) => ({ ...c, feeds: { ...c.feeds, mode: e.target.value as any } }))}
          >
            <option value="remote">Remote</option>
            <option value="local">Local</option>
          </select>
        </div>

        {cfg.feeds.mode === "local" && (
          <div className="space-y-2 md:col-span-1">
            <Label>Current feed path</Label>
            <Input
              value={cfg.feeds.current_path || ""}
              onChange={(e) => setCfg((c) => ({ ...c, feeds: { ...c.feeds, current_path: e.target.value } }))}
              placeholder="C:/!kafe/BikeTrek/web/inventory-data/suppliers/paul-lange/feeds/converted/export_v2_20251013.csv"
            />
          </div>
        )}

        <div className="space-y-2">
          <Label>Downloader layout</Label>
          <select
            className="w-full rounded-xl border border-neutral-700 bg-neutral-900 px-3 py-2 text-sm"
            value={cfg.downloader?.layout || "flat"}
            onChange={(e) => setCfg((c) => ({ ...c, downloader: { ...c.downloader, layout: e.target.value as any } }))}
          >
            <option value="flat">flat</option>
            <option value="by_number_date">by_number_date</option>
          </select>
        </div>

        <div className="space-y-2">
          <Label>Downloader username</Label>
          <Input
            value={cfg.downloader?.auth?.username || ""}
            onChange={(e) =>
              setCfg((c) => ({
                ...c,
                downloader: { ...c.downloader, auth: { ...c.downloader?.auth, username: e.target.value } },
              }))
            }
            placeholder="paul-lange user"
            autoComplete="username"
          />
        </div>

        <div className="space-y-2">
          <Label>Downloader password</Label>
          <Input
            type="password"
            value={cfg.downloader?.auth?.password || ""}
            onChange={(e) =>
              setCfg((c) => ({
                ...c,
                downloader: { ...c.downloader, auth: { ...c.downloader?.auth, password: e.target.value } },
              }))
            }
            placeholder="••••••••"
            autoComplete="current-password"
          />
        </div>
      </div>

      <div className="flex items-center gap-3">
        <Button onClick={save} disabled={saving}>Save supplier config</Button>
        {saved ? <span className="text-sm text-green-500">{saved}</span> : null}
      </div>
    </div>
  );
}
