import React, { useEffect, useState } from "react";
import type { SupplierConfig } from "../../types/config";
import { toForm, fromForm, SupplierForm } from "./supplierShape";
import { getSupplierConfig, saveSupplierConfig } from "../../api/config";

type Props = {
  supplier: string;
  onClose?: () => void;
};

export default SupplierConfigForm;

export function SupplierConfigForm({ supplier, onClose }: Props) {
  const [form, setForm] = useState<SupplierForm | null>(null);
  const [raw, setRaw] = useState<SupplierConfig | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        setErr(null);
        const rawCfg = await getSupplierConfig(supplier);
        if (cancelled) return;
        setRaw(rawCfg);
        setForm(toForm(rawCfg));
      } catch (e: any) {
        if (cancelled) return;
        setErr(String(e?.message || e));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [supplier]);

  async function onSave() {
    if (!form || !raw) return;
    setBusy(true);
    setErr(null);
    setMsg(null);
    try {
      const patch = fromForm(form, raw);
      const savedRaw = await saveSupplierConfig(supplier, patch);
      setRaw(savedRaw);
      setForm(toForm(savedRaw));
      setMsg("Saved.");
    } catch (e: any) {
      setErr(String(e?.message || e));
    } finally {
      setBusy(false);
    }
  }

  // --- UI helpers ---
  const inputCls =
    "w-full rounded-xl border border-white/10 bg-slate-900 px-3 py-2 text-sm text-white placeholder-white/40";
  const labelCls = "text-sm text-white/80";
  const btnPrimary =
    "inline-flex items-center rounded-2xl px-3 py-1.5 text-sm font-medium bg-lime-500 text-slate-950 hover:bg-lime-400 transition";
  const btnGhost =
    "inline-flex items-center rounded-2xl px-3 py-1.5 text-sm font-medium border border-white/15 hover:bg-white/5 transition";

  return (
    <div className="p-4">
      <h3 className="mb-3 text-lg font-semibold">Supplier config — {supplier}</h3>

      {err && (
        <div className="mb-3 rounded-xl border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-200">
          {err}
        </div>
      )}
      {msg && (
        <div className="mb-3 rounded-xl border border-lime-500/40 bg-lime-500/10 px-3 py-2 text-sm text-lime-200">
          {msg}
        </div>
      )}

      {!form ? (
        <div className="text-white/70 text-sm">Loading…</div>
      ) : (
        <div className="space-y-4">
          {/* Feed URL / Local path */}
          <div>
            <label className={labelCls}>Feed URL alebo lokálna cesta</label>
            <input
              className={inputCls}
              value={form.feed_url ?? ""}
              onChange={(e) => setForm({ ...form, feed_url: e.target.value })}
              placeholder="http(s)://… alebo C:\…\file.xml"
            />
          </div>

          {/* Strategy + months */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            <div>
              <label className={labelCls}>Invoices download strategy</label>
              <select
                className={inputCls}
                value={form.invoice_download_strategy ?? "manual"}
                onChange={(e) =>
                  setForm({ ...form, invoice_download_strategy: e.target.value })
                }
              >
                <option value="manual">manual</option>
                <option value="paul-lange-web">paul-lange-web</option>
                <option value="api">api</option>
                <option value="email">email</option>
              </select>
            </div>
            <div>
              <label className={labelCls}>Months back (invoices)</label>
              <input
                type="number"
                min={1}
                max={24}
                className={inputCls}
                value={form.default_months_window ?? 3}
                onChange={(e) =>
                  setForm({
                    ...form,
                    default_months_window: Math.max(
                      1,
                      Math.min(24, Number(e.target.value) || 3)
                    ),
                  })
                }
              />
            </div>
          </div>

          {/* Auth block */}
          <div className="rounded-xl border border-white/10 p-3">
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              <div>
                <label className={labelCls}>Auth mode</label>
                <select
                  className={inputCls}
                  value={form.auth?.mode ?? "none"}
                  onChange={(e) =>
                    setForm({ ...form, auth: { ...form.auth, mode: e.target.value as any } })
                  }
                >
                  <option value="none">none</option>
                  <option value="form">form</option>
                  <option value="cookie">cookie</option>
                  <option value="basic">basic</option>
                  <option value="token">token</option>
                  <option value="header">header</option>
                </select>
              </div>
              <div>
                <label className={labelCls}>Login URL (form)</label>
                <input
                  className={inputCls}
                  value={form.auth?.login_url ?? ""}
                  onChange={(e) =>
                    setForm({ ...form, auth: { ...form.auth, login_url: e.target.value } })
                  }
                  placeholder="https://…/login"
                />
              </div>
              <div>
                <label className={labelCls}>Username</label>
                <input
                  className={inputCls}
                  value={form.auth?.username ?? ""}
                  onChange={(e) =>
                    setForm({ ...form, auth: { ...form.auth, username: e.target.value } })
                  }
                />
              </div>
              <div>
                <label className={labelCls}>Password</label>
                <input
                  className={inputCls}
                  type="password"
                  value={form.auth?.password ?? ""}
                  onChange={(e) =>
                    setForm({ ...form, auth: { ...form.auth, password: e.target.value } })
                  }
                />
              </div>
              <div>
                <label className={labelCls}>User field (form)</label>
                <input
                  className={inputCls}
                  value={form.auth?.user_field ?? "login"}
                  onChange={(e) =>
                    setForm({ ...form, auth: { ...form.auth, user_field: e.target.value } })
                  }
                />
              </div>
              <div>
                <label className={labelCls}>Pass field (form)</label>
                <input
                  className={inputCls}
                  value={form.auth?.pass_field ?? "password"}
                  onChange={(e) =>
                    setForm({ ...form, auth: { ...form.auth, pass_field: e.target.value } })
                  }
                />
              </div>
              <div>
                <label className={labelCls}>Cookie</label>
                <input
                  className={inputCls}
                  value={form.auth?.cookie ?? ""}
                  onChange={(e) =>
                    setForm({ ...form, auth: { ...form.auth, cookie: e.target.value } })
                  }
                />
              </div>
              <div>
                <label className={labelCls}>Basic user</label>
                <input
                  className={inputCls}
                  value={form.auth?.basic_user ?? ""}
                  onChange={(e) =>
                    setForm({ ...form, auth: { ...form.auth, basic_user: e.target.value } })
                  }
                />
              </div>
              <div>
                <label className={labelCls}>Basic pass</label>
                <input
                  className={inputCls}
                  type="password"
                  value={form.auth?.basic_pass ?? ""}
                  onChange={(e) =>
                    setForm({ ...form, auth: { ...form.auth, basic_pass: e.target.value } })
                  }
                />
              </div>
              <div className="flex items-center gap-2">
                <input
                  id="insecure"
                  type="checkbox"
                  checked={!!form.auth?.insecure_all}
                  onChange={(e) =>
                    setForm({ ...form, auth: { ...form.auth, insecure_all: e.target.checked } })
                  }
                />
                <label htmlFor="insecure" className={labelCls}>
                  Allow insecure (no SSL verify)
                </label>
              </div>
            </div>
          </div>

          {/* Actions */}
          <div className="mt-4 flex items-center gap-2">
            <button className={btnPrimary} onClick={onSave} disabled={busy || !form}>
              Save
            </button>
            <button className={btnGhost} onClick={onClose} type="button">
              Close
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
