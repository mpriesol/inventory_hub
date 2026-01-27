import React, { useState } from "react";
import { refreshSupplierInvoices } from "../api/invoices";
import { PrimaryButton } from "../components/ui/button";

export function RefreshInvoicesButton({ supplier, shop, defaultMonths = 3 }:
  { supplier: string; shop: string; defaultMonths?: number }) {
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  async function onClick() {
    setBusy(true); setMsg(null); setErr(null);
    try {
      const res = await refreshSupplierInvoices(supplier, defaultMonths, shop);
      setMsg(`Found: ${res.found}, downloaded: ${res.downloaded.length}, skipped: ${res.skipped.length}, failed: ${res.failed.length}`);
    } catch (e: any) {
      setErr(String(e.message || e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex items-center gap-2">
      <PrimaryButton onClick={onClick} disabled={busy}>
        {busy ? "Refreshing…" : "Refresh invoices"}
      </PrimaryButton>
      {msg && <div className="text-xs text-lime-300">{msg}</div>}
      {err && <div className="text-xs text-rose-300">{err}</div>}
    </div>
  );
}
