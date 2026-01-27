import React, { useEffect, useState } from "react";
import { API_BASE } from "../../api/client";

type Props = { shop: string; onSaved?: () => void; open?: boolean; initial?: any };

export function ShopConfigForm({ shop, onSaved, open, initial }: Props) {
  const [cfg, setCfg] = useState<any>(initial ?? {});
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [savedAt, setSavedAt] = useState<string | null>(null);
  const [seededForThisOpen, setSeededForThisOpen] = useState(false);

  async function load() {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`${API_BASE}/shops/${shop}/config`, { cache: "no-store" });
      const text = await res.text();
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}: ${text.slice(0, 200)}`);

      // bezpečne parse – ak je prázdne, nenechaj formulár vyčistiť
      const data = text.trim() ? JSON.parse(text) : {};
      if (data && typeof data === "object" && Object.keys(data).length > 0) {
        setCfg((prev: any) => ({ ...(prev ?? {}), ...(data || {}) }));
      } else {
        // nechaj existujúce hodnoty, ak server vrátil prázdno
        // setCfg((prev) => prev ?? {});
      }
    } catch (e: any) {
      setError(e?.message || String(e));
    } finally {
      setLoading(false);
    }
  }

  // Keď sa modal OTVORÍ: seedni lokálny stav z initial (ak je), a hneď sprav GET
  useEffect(() => {
    if (!open) {
      // reset seeding flagu, aby sa pri ďalšom otvorení znovu seedlo
      setSeededForThisOpen(false);
      return;
    }
    if (!seededForThisOpen) {
      if (initial && typeof initial === "object" && Object.keys(initial).length > 0) {
        setCfg(initial);
      }
      setSeededForThisOpen(true);
      // fetchni zo servera čerstvú verziu
      load();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, shop]);

  // Ak parent po Save/Close pošle nové `initial` a modal je otvorený – prevezmi ju (ale iba vtedy, ak má dáta)
  useEffect(() => {
    if (!open) return;
    if (initial && typeof initial === "object" && Object.keys(initial).length > 0) {
      setCfg(initial);
    }
  }, [initial, open]);

  function set(path: string, val: any) {
    setCfg((prev: any) => ({ ...(prev ?? {}), [path]: val }));
  }

  async function save() {
    setSaving(true);
    setError(null);
    try {
      const res = await fetch(`${API_BASE}/shops/${shop}/config`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(cfg ?? {}),
      });
      const ct = res.headers.get("content-type") || "";
      const txt = await res.text();
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}: ${txt.slice(0,200)}`);

      let saved: any = null;
      if (ct.includes("application/json") && txt.trim()) {
        try { saved = JSON.parse(txt); } catch {}
      }
      // Ak server poslal uložený JSON, rovno ho prevezmi do formulára:
      if (saved && typeof saved === "object" && Object.keys(saved).length) {
        setCfg(saved);
      } else {
        // fallback: nič nečisti — nechaj pôvodné hodnoty
        // (nevolaj setCfg({})!)
      }

      setSavedAt(new Date().toISOString());
      onSaved?.();               // nech si App refreshne, ak chce
      // voliteľne: už nevolaj load() hneď — práve si dostal “source of truth”
    } catch (e: any) {
      setError(e?.message || String(e));
    } finally {
      setSaving(false);
    }
  }

  if (loading && !cfg) return <div className="text-sm text-white/70">Loading…</div>;
  if (error) return <div className="text-sm text-rose-400">Error: {error}</div>;

  return (
    <div className="space-y-4 text-sm">
      <div className="text-white/70">
        Settings are stored under <code>shops/{shop}/config.json</code>.
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div className="md:col-span-2">
          <label className="block text-xs uppercase tracking-wider text-white/60 mb-1">
            Full export CSV URL
          </label>
          <input
            className="w-full rounded-xl border border-white/10 bg-slate-900 px-3 py-2 placeholder:text-white/40"
            type="text"
            placeholder="https://www.biketrek.sk/export-products-XXXX.csv"
            value={cfg?.upgates_full_export_url_csv ?? ""}
            onChange={(e) => set("upgates_full_export_url_csv", e.target.value)}
          />
          <p className="mt-1 text-xs text-white/50">
            Used by the <b>Download export (link)</b> action.
          </p>
        </div>

        <div>
          <label className="block text-xs uppercase tracking-wider text-white/60 mb-1">Verify SSL</label>
          <select
            className="w-full rounded-xl border border-white/10 bg-slate-900 px-3 py-2"
            value={String(cfg?.verify_ssl ?? "true")}
            onChange={(e) => set("verify_ssl", e.target.value === "true")}
          >
            <option value="true">true</option>
            <option value="false">false</option>
          </select>
        </div>

        <div>
          <label className="block text-xs uppercase tracking-wider text-white/60 mb-1">
            CA bundle path (optional)
          </label>
          <input
            className="w-full rounded-xl border border-white/10 bg-slate-900 px-3 py-2 placeholder:text-white/40"
            type="text"
            placeholder="C:\\certs\\corp-rootCA.pem"
            value={cfg?.ca_bundle_path ?? ""}
            onChange={(e) => set("ca_bundle_path", e.target.value)}
          />
        </div>
      </div>

      <div className="flex items-center gap-2">
        <button
          className="inline-flex items-center rounded-2xl px-3 py-1.5 text-sm font-medium bg-lime-500 text-slate-950 hover:bg-lime-400 transition"
          onClick={save}
          disabled={saving}
        >
          {saving ? "Saving…" : "Save"}
        </button>
        {savedAt ? (
          <span className="text-xs text-white/60">Saved: {new Date(savedAt).toLocaleString()}</span>
        ) : null}
      </div>
    </div>
  );
}
