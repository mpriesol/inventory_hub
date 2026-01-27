
import React, { useEffect, useState } from "react";
import type { EffectiveConfig } from "../../types/config";
import { getEffective } from "../../api/config";

export function EffectiveConfigPanel({ shop, supplier }: { shop: string; supplier: string }) {
  const [data, setData] = useState<EffectiveConfig | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      try {
        const d = await getEffective(shop, supplier);
        setData(d);
      } catch (e: any) {
        setErr(String(e.message || e));
      }
    })();
  }, [shop, supplier]);

  if (err) return <div className="text-xs text-rose-300">Failed to load effective config: {err}</div>;
  if (!data) return <div className="text-sm text-white/60">Loading effective config…</div>;

  return (
    <div className="rounded-xl border border-white/10 bg-slate-900/60 p-3">
      <div className="text-xs text-white/70 mb-2">Effective (merged) values</div>
      <pre className="text-xs whitespace-pre-wrap">{JSON.stringify(data.effective, null, 2)}</pre>
    </div>
  );
}
