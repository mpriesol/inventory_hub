import React, { useEffect, useRef, useState } from "react";

const API_BASE: string = (import.meta as any).env?.VITE_API_BASE || "http://127.0.0.1:8000";

type Eff = { using_feed?: { key?: string|null; mode?: "remote"|"local"|null; url?: string|null; path?: string|null } };
type FileItem = { path: string; name?: string; mtime?: string|null };
type SupplierRef = { code: string; name?: string };

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

async function fileMeta(relpath: string): Promise<{mtime: string|null, size: number|null}> {
  try {
    const s = await fetchJSON<any>(`${API_BASE}/files/stat?relpath=${encodeURIComponent(relpath)}`);
    return { mtime: s.mtime_iso || s.mtime || null, size: typeof s.size === "number" ? s.size : null };
  } catch {}
  try {
    const res = await fetch(`${API_BASE}/files/download?relpath=${encodeURIComponent(relpath)}`, { method: "HEAD" });
    return { mtime: res.headers.get("last-modified"), size: res.headers.get("content-length") ? parseInt(res.headers.get("content-length") as string, 10) : null };
  } catch { return { mtime: null, size: null }; }
}

function niceDate(lm: string | null | undefined): string {
  if (!lm) return "—";
  const d = new Date(lm);
  if (!isFinite(d.getTime())) return lm;
  return d.toLocaleString();
}

export default function SupplierConfigModal({
  open,
  onClose,
  initialSupplier = "paul-lange",
  supplierValue,
  onSupplierChange,
  onSaved,
}:{
  open: boolean;
  onClose: () => void;
  initialSupplier?: string;
  supplierValue?: string;
  onSupplierChange?: (s:string)=>void;
  onSaved?: (supplier:string)=>void;
}){
  const [supplier, setSupplier] = useState<string>(supplierValue || initialSupplier);
  const [suppliers, setSuppliers] = useState<SupplierRef[]>([]);
  const [eff, setEff] = useState<Eff| null>(null);

  const [feedsConverted, setFeedsConverted] = useState<FileItem[]>([]);
  const [feedsRaw, setFeedsRaw] = useState<FileItem[]>([]);

  const [status, setStatus] = useState<string>("");
  const [cfgText, setCfgText] = useState<string>("");
  const [saving, setSaving] = useState(false);

  const [previewRelpath, setPreviewRelpath] = useState<string | null>(null);
  const [previewRows, setPreviewRows] = useState<string[][]>([]);
  const [previewErr, setPreviewErr] = useState<string | null>(null);

  const fileRef = useRef<HTMLInputElement>(null);

  const btnGreen = "inline-flex items-center rounded-2xl px-3 py-1.5 text-sm font-medium bg-lime-500 text-slate-950 hover:bg-lime-400 transition";
  const btnOutline = "inline-flex items-center rounded-2xl px-3 py-1.5 text-sm font-medium border border-lime-500 text-lime-400 hover:bg-lime-500/10 transition";

  const latestConverted = feedsConverted[0] || null;
  const latestRaw = feedsRaw[0] || null;

  const rawHref = latestRaw?.path || eff?.using_feed?.path || null;
  const csvHref = latestConverted?.path || null;

  // NEW: close on Esc (keeps your existing UI)
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  useEffect(()=>{
    if (supplierValue && supplierValue !== supplier) setSupplier(supplierValue);
  }, [supplierValue]);

  async function loadSuppliers(){
    try{
      const list = await fetchJSON<any[]>(`${API_BASE}/suppliers`);
      const mapped = Array.isArray(list) ? list.map((x:any)=>({code: x.code || x.supplier_code || x.id || x, name: x.name || x.title || x.code || x})) : [];
      setSuppliers(mapped);
    }catch{}
  }
  async function loadEffective(){
    try{
      const sc = await fetchJSON<Eff>(`${API_BASE}/configs/effective/supplier?supplier=${encodeURIComponent(supplier)}`);
      setEff(sc);
    }catch{
      try{
        const sc = await fetchJSON<Eff>(`${API_BASE}/suppliers/${supplier}/effective`);
        setEff(sc);
      }catch{}
    }
  }
  async function listFiles(area: string): Promise<FileItem[]> {
    try {
      const data = await fetchJSON<any>(`${API_BASE}/suppliers/${supplier}/files?area=${encodeURIComponent(area)}`);
      const arr: string[] = Array.isArray(data) ? data : (data?.files ?? []);
      const items: FileItem[] = [];
      for (const rel of arr) {
        const info = await fileMeta(rel);
        items.push({ path: rel, name: rel.split("/").pop() || rel, mtime: info.mtime || null });
      }
      return items;
    } catch { return []; }
  }
  async function loadFiles(){
    const [conv, raw] = await Promise.all([
      listFiles("feeds/converted"),
      listFiles("feeds/xml"),
    ]);
    setFeedsConverted(conv);
    setFeedsRaw(raw);
  }
  async function loadConfigJsonc(){
    try{
      const res = await fetch(`${API_BASE}/suppliers/${supplier}/config`);
      const txt = await res.text();
      if(!res.ok) throw new Error(`${res.status} ${res.statusText}: ${txt}`);
      const pretty = JSON.stringify(JSON.parse(txt || "{}"), null, 2);
      const header = `//
// BikeTrek Supplier Config (JSON with comments)
// feeds.current_key → active feed key (e.g. "products")
// feeds.sources.<key>.mode → "remote" | "local"
// feeds.sources.<key>.url  → remote URL (if mode="remote")
// feeds.sources.<key>.path → server path (if mode="local")
// Lines starting with // are ignored on save.
//
`;
      setCfgText(header + pretty);
    }catch(e:any){
      setCfgText("// Failed to load config: " + (e?.message || e) + "\n" + JSON.stringify({"feeds":{"current_key":"products","sources":{"products":{"mode":"remote","url":"https://..."}}}}, null, 2));
    }
  }

  useEffect(()=>{ if(open){ loadSuppliers(); }},[open]);
  useEffect(()=>{
    if(!open) return;
    loadEffective();
    loadFiles();
    loadConfigJsonc();
  },[supplier, open]);

  async function downloadAndConvert(){
    setStatus("Downloading and converting…");
    try{
      const out = await fetchJSON<any>(`${API_BASE}/suppliers/${supplier}/feeds/refresh`, { method: "POST", headers: {"Content-Type":"application/json"} });
      setStatus(`OK: ${JSON.stringify(out?.saved || out)}`);
      await loadFiles(); await loadEffective();
    }catch(e:any){ setStatus("ERROR: " + (e?.message || e)); }
  }

  async function uploadLocalAndConvert(file: File){
    setStatus("Uploading local feed…");
    try{
      const fd = new FormData();
      fd.append("file", file);
      const upRes = await fetch(`${API_BASE}/suppliers/${supplier}/feeds/upload`, { method:"POST", body: fd });
      const upTxt = await upRes.text();
      if(!upRes.ok) throw new Error(`${upRes.status} ${upRes.statusText}: ${upTxt}`);
      let savedPath = "";
      try { const up = JSON.parse(upTxt); savedPath = up.saved_path || up.path || up.relpath || ""; } catch {}
      if (!savedPath) throw new Error("Upload OK, ale neprišla cesta (saved_path).");
      setStatus(prev => prev + `\nUploaded as: ${savedPath}\nConverting…`);

      const refRes = await fetch(`${API_BASE}/suppliers/${supplier}/feeds/refresh`, {
        method:"POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify({ source_url: savedPath })
      });
      const refTxt = await refRes.text();
      if(!refRes.ok) throw new Error(`${refRes.status} ${refRes.statusText}: ${refTxt}`);

      setStatus(prev => prev + `\nConvert OK.`);
      await loadFiles(); await loadEffective();
    }catch(e:any){
      const lower = (e?.message || String(e)).toLowerCase();
      if (lower.includes("networkerror") || lower.includes("failed to fetch") || lower.includes("cors")) {
        setStatus("ERROR: Network/CORS — skontroluj CORS pre POST multipart/form-data, VITE_API_BASE a či beží API.");
      } else {
        setStatus("ERROR: " + (e?.message || String(e)));
      }
    }
  }

  async function saveConfig(){
    const raw = stripJsonComments(cfgText);
    let parsed:any;
    try{ parsed = JSON.parse(raw); }catch(e:any){ alert("Config is not valid JSON.\n" + (e?.message||e)); return; }
    if(!confirm("Save supplier config? (server may create a backup)")) return;
    setSaving(true);
    try{
      const res = await fetch(`${API_BASE}/suppliers/${supplier}/config?backup=1`, {
        method: "PUT", headers: {"Content-Type":"application/json"}, body: JSON.stringify(parsed)
      });
      const txt = await res.text();
      if(!res.ok) throw new Error(`${res.status} ${res.statusText}: ${txt}`);
      setStatus("Config saved.");
      onSaved?.(supplier);
      await loadEffective();
    }catch(e:any){ setStatus("ERROR: " + (e?.message || e)); }
    finally{ setSaving(false); }
  }

  async function openPreview(relpath: string){
    setPreviewRelpath(relpath);
    setPreviewErr(null);
    setPreviewRows([]);
    try{
      const res = await fetch(`${API_BASE}/files/preview?relpath=${encodeURIComponent(relpath)}&max_rows=120`);
      const txt = await res.text();
      if(!res.ok) throw new Error(`${res.status} ${res.statusText}: ${txt}`);
      const data = JSON.parse(txt || "[]");
      if (data && Array.isArray(data.rows) && Array.isArray(data.columns)) {
        setPreviewRows([data.columns as string[], ...(data.rows as string[][])]);
        return;
      }
      if(Array.isArray(data) && data.length){
        setPreviewRows(data);
        return;
      }
      setPreviewErr("No data.");
    }catch(e:any){
      setPreviewErr(e?.message || String(e));
    }
  }

  function changeSupplier(next: string){
    setSupplier(next);
    onSupplierChange?.(next);
  }

  function closeSelf(){ onClose(); }

  if(!open) return null;

  return (
    <div className="fixed inset-0 z-50">
      <div className="absolute inset-0 bg-black/60" onClick={closeSelf} />
      <div className="absolute inset-x-0 top-10 mx-auto w-full max-w-5xl overflow-hidden rounded-2xl border border-white/10 bg-slate-950 shadow-2xl">
        <div className="border-b border-white/10 px-6 py-4">
          <div className="flex items-center justify-between">
            <h2 className="text-lg font-semibold">Supplier configuration</h2>
            <button className="text-white/60 hover:text-white" onClick={closeSelf}>✕</button>
          </div>
          <div className="mt-2 flex flex-wrap items-center gap-3 text-sm">
            <label className="flex items-center gap-2">Supplier
              <select className="rounded-lg bg-slate-900 border border-white/10 px-2 py-1" value={supplier} onChange={e=>changeSupplier(e.target.value)}>
                {[supplier, ...suppliers.map(s=>s.code)].filter((v,i,a)=>a.indexOf(v)===i).map(code=> <option key={code} value={code}>{code}</option>)}
              </select>
            </label>
            {eff?.using_feed ? (
              <span className="text-xs text-white/60">
                key: <b>{eff.using_feed.key ?? "—"}</b> • mode: <b>{eff.using_feed.mode ?? "—"}</b> • url: {eff.using_feed.url || "—"} {eff.using_feed.path ? <> • path: <span className="break-all">{eff.using_feed.path}</span></> : null}
              </span>
            ) : null}
          </div>
        </div>

        <div className="flex flex-col gap-4 p-6">
          <Section title="Feed actions">
            <div className="flex flex-col gap-2">
              <button className={btnGreen} onClick={downloadAndConvert}>Download product feed from supplier</button>

              <div className="flex items-center gap-2">
                <input ref={fileRef} type="file" accept=".xml,.csv,.zip,.gz,.bz2,.xz" onChange={e=>{
                  const f = e.target.files?.[0]; if(f) uploadLocalAndConvert(f);
                }} className="text-sm file:mr-3 file:rounded-xl file:border file:border-white/10 file:bg-slate-900 file:px-3 file:py-1.5 file:text-sm file:text-white hover:file:bg-slate-800"/>
                <button className={btnOutline} onClick={()=>fileRef.current?.click()}>Browse… & Upload local product feed</button>
              </div>

              <div className="flex flex-wrap gap-2">
                <a className={btnOutline + (rawHref ? "" : " opacity-50 pointer-events-none")} href={rawHref ? `${API_BASE}/files/download?relpath=${encodeURIComponent(rawHref)}` : "#"} target="_blank" rel="noreferrer">Download product feed (raw)</a>
                <a className={btnOutline + (csvHref ? "" : " opacity-50 pointer-events-none")} href={csvHref ? `${API_BASE}/files/download?relpath=${encodeURIComponent(csvHref)}` : "#"} target="_blank" rel="noreferrer">Download product feed (csv)</a>
                <button className={btnOutline} onClick={()=> csvHref && openPreview(csvHref)} disabled={!csvHref}>Preview product feed (csv)</button>
              </div>

              {status ? <div className="text-xs text-white/70 whitespace-pre-wrap">{status}</div> : null}

              <div className="mt-2 text-xs text-white/60 space-y-1">
                <div>RAW: <span className="break-all">{rawHref || "—"}</span>{latestRaw?.mtime ? <> · <span title="Last-Modified">{niceDate(latestRaw.mtime)}</span></> : null}</div>
                <div>Latest CSV: <span className="break-all">{csvHref || "—"}</span>{latestConverted?.mtime ? <> · <span title="Last-Modified">{niceDate(latestConverted.mtime)}</span></> : null}</div>
              </div>
            </div>
          </Section>

          <Section title="Supplier JSON config (with comments)">
            <textarea className="h-72 w-full rounded-xl border border-white/10 bg-slate-900 p-3 font-mono text-xs leading-5 text-white" value={cfgText} onChange={(e)=>setCfgText(e.target.value)} />
            <div className="mt-2 flex items-center gap-2">
              <button className={btnGreen} onClick={saveConfig} disabled={saving}>{saving ? "Saving…" : "Save config"}</button>
              <button className={btnOutline} onClick={()=>loadConfigJsonc()}>Reload</button>
            </div>
          </Section>

          {previewRelpath && (
            <Section title={`CSV preview — ${previewRelpath.split("/").pop()}`}>
              {previewErr ? (
                <div className="mb-2 rounded-xl border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-rose-200 text-xs">{previewErr}</div>
              ) : null}
              <div className="max-h-72 overflow-auto rounded-xl border border-white/10">
                <table className="min-w-full text-xs">
                  <thead className="sticky top-0 bg-slate-900/90 backdrop-blur">
                    <tr>{(previewRows[0]||[]).map((h,i)=>(<th key={i} className="px-2 py-1 text-left font-semibold border-b border-white/10">{h}</th>))}</tr>
                  </thead>
                  <tbody>
                    {previewRows.slice(1).map((r,ri)=>(
                      <tr key={ri} className="odd:bg-slate-900/40">
                        {r.map((c,ci)=>(<td key={ci} className="px-2 py-1 border-b border-white/5">{c}</td>))}
                      </tr>
                    ))}
                    {previewRows.length===0 ? <tr><td className="px-2 py-2 text-white/60">No data.</td></tr> : null}
                  </tbody>
                </table>
              </div>
              <div className="mt-2">
                <button className="text-white/60 hover:text-white text-xs" onClick={()=>{ setPreviewRelpath(null); setPreviewRows([]); }}>Close preview</button>
              </div>
            </Section>
          )}
        </div>
      </div>
    </div>
  );
}
