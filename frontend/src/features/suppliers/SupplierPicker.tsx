import React, { useEffect, useMemo, useState } from "react";
import { API_BASE } from "../../api/client";

type Item = { code: string; name: string };
type Mode = "suppliers" | "shops";

type Props = {
  label?: string;
  mode?: Mode;
  endpoint?: string;
  items?: Item[];
  value: string;
  onChange: (code: string) => void;
  onResolvedValue?: (item: Item | null) => void;
  disabled?: boolean;
};

async function fetchList(url: string): Promise<Item[]> {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const data = await res.json();
  if (!Array.isArray(data)) return [];
  return data.map((raw: any) => {
    const code =
      raw?.supplier_code ?? raw?.shop_code ?? raw?.code ?? raw?.id ?? raw?.name ?? "";
    const name = raw?.name ?? raw?.title ?? raw?.supplier_code ?? raw?.shop_code ?? String(code);
    return { code: String(code), name: String(name) };
  });
}

export function SupplierPicker({
  label = "Dodávateľ",
  mode,
  endpoint,
  items,
  value,
  onChange,
  onResolvedValue,
  disabled,
}: Props) {
  const [list, setList] = useState<Item[]>(items ?? []);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

useEffect(() => {
  let active = true;

  async function run() {
    if (!endpoint && !mode) return;

    setLoading(true);
    setError(null);

    try {
      const url =
        endpoint || (mode === "suppliers" ? `${API_BASE}/suppliers` : `${API_BASE}/shops`);
      const fetched = await fetchList(url);
      if (!active) return;
      setList(fetched);
    } catch (e: any) {
      if (!active) return;
      setError(e?.message || String(e));
      if (items && items.length) setList(items);
    } finally {
      if (active) setLoading(false);
    }
  }

  run();
  return () => {
    active = false;
  };
// eslint-disable-next-line react-hooks/exhaustive-deps
}, [endpoint, mode]);


  useEffect(() => {
    if (!endpoint && !mode && items) setList(items);
  }, [items])

  const current = useMemo(() => list.find((i) => i.code === value) || null, [list, value]);
  useEffect(() => {
    onResolvedValue?.(current ?? null);
  }, [current?.code, current?.name, list.length]);

  return (
    <div className="w-full">
      <div className="text-xs uppercase tracking-wider text-white/60 mb-1">{label}</div>
      <div className="flex items-center gap-2">
        <select
          className="w-full rounded-xl border border-white/10 bg-slate-900 px-3 py-2 text-sm disabled:opacity-60"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          disabled={disabled}
        >
          {list.map((s) => (
            <option key={s.code} value={s.code}>{s.name}</option>
          ))}
        </select>
        {loading ? <span className="text-xs text-white/50">Načítavam…</span> : null}
        {error ? <span className="text-xs text-rose-400" title={error}>Chyba</span> : null}
      </div>
    </div>
  );
}
