import React, { useEffect, useRef, useState } from "react";
import { Modal } from "../../components/ui/Modal";

const API_BASE: string = (import.meta.env.VITE_API_BASE || "/api").replace(/\/$/, "");

type EffShop = { using_export?: { url?: string|null; path?: string|null } };
type FileMeta = { mtime?: string|null; size?: number|null };
type ShopRef = { code: string; name?: string };

async function fetchJSON<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
  const text = await res.text();
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}${text? `: ${text}`:""}`);
  try { return JSON.parse(text) as T; } catch { return {} as T; }
}

function stripJsonComments(input: string): string {
  return input
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .split(/\r?\n/)
    .map((l) => l.replace(/(^|\s)\/\/.*$/, ""))
    .join("\n");
}

function Section({title, children}:{title:string; children:React.ReactNode}){
  return (
    <div className="rounded-2xl border border-white/10 bg-slate-950/50">
      <div className="px-4 py-2 text-xs uppercase tracking-wider text-white/60 border-b border-white/10">{title}</div>
      <div className="p-4">{children}</div>
    </div>
  );
}

async function fileMeta(relpath: string): Promise<FileMeta> {
  try {
    const s = await fetchJSON<any>(`${API_BASE}/files/stat?relpath=${encodeURIComponent(relpath)}`);
    return { mtime: s.mtime_iso || s.mtime || null, size: typeof s.size === "number" ? s.size : null };
  } catch {}
  try {
    const res = await fetch(`${API_BASE}/files/download?relpath=${encodeURIComponent(relpath)}`, { method: "HEAD" });
    if (res.ok) {
      const lm = res.headers.get("last-modified");
      const cl = res.headers.get("content-length");
      return { mtime: lm, size: cl ? parseInt(cl, 10) : null };
    }
  } catch {}
  return { mtime: null, size: null };
}

function niceDate(lm: string | null | undefined): string {
  if (!lm) return "—";
  const d = new Date(lm);
  if (!isFinite(d.getTime())) return lm;
  return d.toLocaleString();
}

export default function ShopConfigModal({
  open,
  onClose,
  initialShop = "biketrek",
  shopValue,
  onShopChange,
  onSaved,
}:{
  open: boolean;
  onClose: () => void;
  initialShop?: string;
  shopValue?: string;
  onShopChange?: (s:string)=>void;
  onSaved?: (shop:string)=>void;
}){
  const [shop, setShop] = useState<string>(shopValue || initialShop);
  const [shops, setShops] = useState<ShopRef[]>([]);
  const [eff, setEff] = useState<EffShop| null>(null);

  const [status, setStatus] = useState<string>("");
  const [cfgText, setCfgText] = useState<string>("");
  const [saving, setSaving] = useState(false);

  const [previewRows, setPreviewRows] = useState<string[][]>([]);
  const [previewErr, setPreviewErr] = useState<string | null>(null);

  const fileRef = useRef<HTMLInputElement>(null);

  const latestCsvRel = `shops/${shop}/latest.csv`;
  const [csvMeta, setCsvMeta] = useState<FileMeta>({});

  const btnGreen = "inline-flex items-center rounded-2xl px-3 py-1.5 text-sm font-medium bg-lime-500 text-slate-950 hover:bg-lime-400 transition";
  const btnOutline = "inline-flex items-center rounded-2xl px-3 py-1.5 text-sm font-medium border border-lime-500 text-lime-400 hover:bg-lime-500/10 transition";

  // NEW: close on Esc
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  useEffect(()=>{
    if (shopValue && shopValue !== shop) setShop(shopValue);
  }, [shopValue]);

  async function loadShops(){
    try{
      const list = await fetchJSON<any[]>(`${API_BASE}/shops`);
      const mapped: ShopRef[] = Array.isArray(list)
        ? list.map((x:any)=>({ code: x.code || x.shop_code || x.id || String(x), name: x.name || x.title || x.code || x.shop_code || String(x) }))
        : [{ code: initialShop, name: initialShop }];
      const seen = new Set<string>();
      const unique = mapped.filter(s => (s.code && !seen.has(s.code)) ? (seen.add(s.code), true) : false);
      setShops(unique.length ? unique : [{ code: initialShop, name: initialShop }]);
    }catch{
      setShops([{ code: initialShop, name: initialShop }]);
    }
  }

  async function loadEffective(){
    try{
      const sc = await fetchJSON<EffShop>(`${API_BASE}/configs/effective/shop?shop=${encodeURIComponent(shop)}`);
      setEff(sc);
    }catch{
      try{
        const sc = await fetchJSON<EffShop>(`${API_BASE}/shops/${shop}/effective`);
        setEff(sc);
      }catch{}
    }
  }

  async function loadConfigJsonc(){
    try{
      const res = await fetch(`${API_BASE}/shops/${shop}/config`);
      const txt = await res.text();
      if(!res.ok) throw new Error(`${res.status} ${res.statusText}: ${txt}`);
      const pretty = JSON.stringify(JSON.parse(txt || "{}"), null, 2);
      const header = `//
// BikeTrek Shop Config (JSON with comments)
// upgates_full_export_url_csv → URL pre stiahnutie CSV exportu
// verify_ssl / ca_bundle_path  → TLS detaily (ak potrebuješ)
// Ostatné shop políčka nechávame bez zmeny.
// Lines starting with // are ignored on save.
//
`;
      setCfgText(header + pretty);
    }catch(e:any){
      setCfgText("// Failed to load config: " + (e?.message || e) + "\n" + JSON.stringify({"upgates_full_export_url_csv":"https://..."}, null, 2));
    }
  }

  async function refreshCsvMeta(){
    const meta = await fileMeta(latestCsvRel);
    setCsvMeta(meta);
  }

  useEffect(()=>{ if(open){ loadShops(); }},[open]);
  useEffect(()=>{
    if(!open) return;
    loadEffective();
    loadConfigJsonc();
    setPreviewRows([]);
    setPreviewErr(null);
    refreshCsvMeta();
  },[shop, open]);

  async function saveConfig(){
    const raw = stripJsonComments(cfgText);
    let parsed:any;
    try{ parsed = JSON.parse(raw); }catch(e:any){ alert("Config is not valid JSON.\n" + (e?.message||e)); return; }
    if(!confirm("Save shop config? (server may create a backup)")) return;
    setSaving(true);
    try{
      const res = await fetch(`${API_BASE}/shops/${shop}/config?backup=1`, {
        method: "PUT", headers: {"Content-Type":"application/json"}, body: JSON.stringify(parsed)
      });
      const txt = await res.text();
      if(!res.ok) throw new Error(`${res.status} ${res.statusText}: ${txt}`);
      setStatus("Config saved.");
      onSaved?.(shop);
      await loadEffective();
    }catch(e:any){ setStatus("ERROR: " + (e?.message || e)); }
    finally{ setSaving(false); }
  }

  async function refreshExportFromEshop(){
    setStatus("Downloading export from e-shop…");
    try{
      let ok = false
      for (const u of [
        `${API_BASE}/shops/${shop}/export/refresh`,
        `${API_BASE}/shops/${shop}/register-export`,
      ]){
        const res = await fetch(u, { method:"POST", headers: {"Content-Type":"application/json"} });
        if (res.ok) { ok = true; await res.text(); break; }
      }
      if (!ok) throw new Error("No refresh endpoint responded 200. Please implement /shops/{shop}/export/refresh or /shops/{shop}/register-export.");
      setStatus("Export updated.");
      await refreshCsvMeta();
    }catch(e:any){
      setStatus("ERROR: " + (e?.message || String(e)));
    }
  }

  async function uploadLocalExport(file: File){
    setStatus("Uploading local export…");
    try{
      const fd = new FormData();
      fd.append("file", file);
      const res = await fetch(`${API_BASE}/shops/${shop}/export/upload`, { method:"POST", body: fd });
      const txt = await res.text();
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}: ${txt}`);
      setStatus("Local export uploaded.");
      await refreshCsvMeta();
    }catch(e:any){
      const lower = (e?.message || String(e)).toLowerCase();
      if (lower.includes("networkerror") || lower.includes("failed to fetch") || lower.includes("cors")) {
        setStatus("ERROR: Network/CORS — skontroluj CORS pre POST multipart/form-data, VITE_API_BASE a či beží API.");
      } else if (lower.includes("404")) {
        setStatus("ERROR: Missing /shops/{shop}/export/upload endpoint on backend.");
      } else {
        setStatus("ERROR: " + (e?.message || String(e)));
      }
    }
  }

  function closeSelf(){ onClose(); }
  function changeShop(next: string){ setShop(next); onShopChange?.(next); }

  if (!open) return null;
return (
  <Modal open={open} onClose={closeSelf} title="Shop configuration" align="top">
    <div className="border-b border-white/10 px-6 py-4">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold">Shop configuration</h2>
        <button className="text-white/60 hover:text-white" onClick={closeSelf}>✕</button>
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-3 text-sm">
        <label className="flex items-center gap-2">Shop
          <select
            className="rounded-lg bg-slate-900 border border-white/10 px-2 py-1"
            value={shop}
            onChange={(e)=>changeShop(e.target.value)}>
            {[{code: shop, name: shop}, ...shops]
              .filter((s,i,arr)=> arr.findIndex(t=>t.code===s.code)===i)
              .map((s)=> <option key={s.code} value={s.code}>{s.name || s.code}</option>
            )}
          </select>
        </label>
        {eff?.using_export ? (
          <span className="text-xs text-white/60">
            url: {eff.using_export.url || "—"} {eff.using_export.path ? <> • path: <span className="break-all">{eff.using_export.path}</span></> : null}
          </span>
        ) : null}
      </div>
    </div>

    {/* SINGLE-COLUMN LAYOUT */}
    <div className="flex flex-col gap-4 p-6">
      <Section title="Export actions">
        <div className="flex flex-col gap-2">
          <button className="inline-flex items-center rounded-2xl px-3 py-1.5 text-sm font-medium bg-lime-500 text-slate-950 hover:bg-lime-400 transition" onClick={refreshExportFromEshop}>Download product export from e-shop</button>

          <div className="flex items-center gap-2">
            <input ref={fileRef} type="file" accept=".csv" onChange={e=>{
              const f = e.target.files?.[0]; if(f) uploadLocalExport(f);
            }} className="text-sm file:mr-3 file:rounded-xl file:border file:border-white/10 file:bg-slate-900 file:px-3 file:py-1.5 file:text-sm file:text-white hover:file:bg-slate-800"/>
            <button className="inline-flex items-center rounded-2xl px-3 py-1.5 text-sm font-medium border border-lime-500 text-lime-400 hover:bg-lime-500/10 transition" onClick={()=>fileRef.current?.click()}>Browse… & Upload local export</button>
          </div>

          <div className="flex flex-wrap gap-2">
            <a className="inline-flex items-center rounded-2xl px-3 py-1.5 text-sm font-medium border border-lime-500 text-lime-400 hover:bg-lime-500/10 transition" href={`${API_BASE}/files/download?relpath=${encodeURIComponent(latestCsvRel)}`} target="_blank" rel="noreferrer">Download product export (csv)</a>
            <button className="inline-flex items-center rounded-2xl px-3 py-1.5 text-sm font-medium border border-lime-500 text-lime-400 hover:bg-lime-500/10 transition" onClick={()=> {/* your preview logic here or keep as is */}}>Preview product export (csv)</button>
          </div>

          {status ? <div className="text-xs text-white/70 whitespace-pre-wrap">{status}</div> : null}

          <div className="mt-2 text-xs text-white/60 space-y-1">
            <div>Using: <span className="break-all">{latestCsvRel}</span> · Last downloaded: {niceDate(csvMeta.mtime || null)}</div>
          </div>
        </div>
      </Section>

      <Section title="Shop JSON config (with comments)">
        <textarea className="h-72 w-full rounded-xl border border-white/10 bg-slate-900 p-3 font-mono text-xs leading-5 text-white" value={cfgText} onChange={(e)=>setCfgText(e.target.value)} />
        <div className="mt-2 flex items-center gap-2">
          <button className="inline-flex items-center rounded-2xl px-3 py-1.5 text-sm font-medium bg-lime-500 text-slate-950 hover:bg-lime-400 transition" onClick={saveConfig} disabled={saving}>{saving ? "Saving…" : "Save config"}</button>
          <button className="inline-flex items-center rounded-2xl px-3 py-1.5 text-sm font-medium border border-lime-500 text-lime-400 hover:bg-lime-500/10 transition" onClick={()=>loadConfigJsonc()}>Reload</button>
        </div>
      </Section>
    </div>
  </Modal>
);}
