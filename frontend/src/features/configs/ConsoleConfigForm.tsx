
import React, { useEffect, useState } from "react";
import type { ConsoleConfig } from "../../types/config";
import { getConsoleConfig, saveConsoleConfig } from "../../api/config";
import { PrimaryButton, OutlineButton } from "../../components/ui/button";

export function ConsoleConfigForm() {
  const [cfg, setCfg] = useState<ConsoleConfig | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      try {
        const c = await getConsoleConfig();
        setCfg(c);
      } catch (e: any) {
        setErr(String(e.message || e));
      }
    })();
  }, []);

  function update<K extends keyof ConsoleConfig>(k: K, v: ConsoleConfig[K]) {
    if (!cfg) return;
    setCfg({ ...cfg, [k]: v });
  }

  async function onSave() {
    if (!cfg) return;
    setBusy(true); setMsg(null); setErr(null);
    try {
      const saved = await saveConsoleConfig(cfg);
      setCfg(saved);
      setMsg("Console config saved.");
    } catch (e: any) {
      setErr(String(e.message || e));
    } finally {
      setBusy(false);
    }
  }

  if (!cfg) return <div className="text-sm text-white/60">Loading console config…</div>;

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
        <label className="text-sm">
          <div className="text-white/70">Language</div>
          <select className="mt-1 w-full bg-slate-900/60 border border-white/10 rounded-lg p-2"
            value={cfg.language}
            onChange={(e) => update("language", e.target.value as any)}>
            <option value="en">English</option>
          </select>
        </label>
        <label className="text-sm">
          <div className="text-white/70">Default currency</div>
          <select className="mt-1 w-full bg-slate-900/60 border border-white/10 rounded-lg p-2"
            value={cfg.default_currency}
            onChange={(e) => update("default_currency", e.target.value as any)}>
            <option value="EUR">EUR</option>
          </select>
        </label>
        <label className="text-sm">
          <div className="text-white/70">Default months window</div>
          <input type="number" min={1} className="mt-1 w-full bg-slate-900/60 border border-white/10 rounded-lg p-2"
            value={cfg.default_months_window}
            onChange={(e) => update("default_months_window", Number(e.target.value))}/>
        </label>
      </div>

      <label className="text-sm block">
        <div className="text-white/70">Currency rates (JSON)</div>
        <textarea rows={6} className="mt-1 w-full bg-slate-900/60 border border-white/10 rounded-lg p-2 font-mono text-xs"
          value={JSON.stringify(cfg.currency_rates, null, 2)}
          onChange={(e) => {
            try {
              const parsed = JSON.parse(e.target.value || "{}");
              update("currency_rates", parsed);
            } catch {
              // ignore until valid
            }
          }}/>
        <div className="text-xs text-white/50 mt-1">Example: {"{ \"CZK\": { \"EUR\": 0.041 } }"}</div>
      </label>

      <div className="flex items-center gap-2">
        <PrimaryButton onClick={onSave} disabled={busy}>Save console</PrimaryButton>
        {msg && <div className="text-xs text-lime-300">{msg}</div>}
        {err && <div className="text-xs text-rose-300">{err}</div>}
      </div>
    </div>
  );
}
