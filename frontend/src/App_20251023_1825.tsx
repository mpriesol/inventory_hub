import React, { useEffect, useMemo, useState } from "react";
import { Section } from "./components/ui/Section";
import { InfoBanner, DangerBanner } from "./components/ui/Banner";
import { SupplierPicker } from "./features/suppliers/SupplierPicker";
import { FeedControls } from "./features/suppliers/FeedControls";
import { InvoicesTable } from "./features/suppliers/InvoicesTable";
import { FeedsTable } from "./features/suppliers/FeedsTable";
import { ImportsTable } from "./features/suppliers/ImportsTable";
import type { Supplier, ListedFile } from "./types";
import { API_BASE, fetchJSON } from "./api/client";
import { FeedUploader } from "./features/suppliers/FeedUploader";





// ---------- Error boundary so stránka nezostane prázdna pri runtime chybe ----------
class Boundary extends React.Component<{children: React.ReactNode}, {error?: any}> {
  constructor(props: any) { super(props); this.state = { error: null }; }
  static getDerivedStateFromError(error: any) { return { error }; }
  componentDidCatch(error: any, info: any) { console.error("UI error:", error, info); }
  render() {
    if (this.state.error) {
      return (
        <div className="min-h-screen bg-slate-950 text-white p-6">
          <div className="max-w-4xl mx-auto">
            <div className="rounded-xl border border-rose-300/30 bg-rose-900/30 p-4">
              <div className="font-semibold mb-2">Runtime chyba v UI</div>
              <pre className="text-xs whitespace-pre-wrap break-words">{String(this.state.error)}</pre>
            </div>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}

type PreviewData = {
  columns?: string[];
  rows?: any[];
  total_columns?: number;
  preview_rows?: number;
};

type PreviewState = {
  open: boolean;
  loading?: boolean;
  error?: string | null;
  data?: PreviewData | null;
};

function idFor(f: ListedFile) {
  return f.path || f.href || f.name;
}

async function tryFiles(areaVariants: string[], supplier: string, base: string) {
  for (const area of areaVariants) {
    try {
      const res = await fetch(`${base}/suppliers/${supplier}/files?area=${encodeURIComponent(area)}`);
      const ct = res.headers.get("content-type") || "";
      if (!res.ok) {
        let detail = `${res.status} ${res.statusText}`;
        try {
          const body = ct.includes("application/json") ? await res.json() : await res.text();
          const d = typeof body === "string" ? body : (body as any)?.detail;
          if (d) detail += `\n${typeof d === "string" ? d : JSON.stringify(d)}`;
        } catch {}
        throw new Error(detail);
      }
      const data = ct.includes("application/json") ? await res.json() : {};
      const arr: string[] = Array.isArray(data) ? data : ((data as any)?.files ?? []);
      const mapped = arr.map((relpath: string) => ({
        name: relpath.split('/').pop() || relpath,
        path: relpath,
      }));
      return mapped as ListedFile[];
    } catch (e: any) {
      if (!/^400|404/.test(String(e.message))) throw e;
    }
  }
  return [];
}

export default function App() {
  const [suppliers, setSuppliers] = useState<Supplier[]>([]);
  const [supplier, setSupplier] = useState<string>("paul-lange");
  const [shop, setShop] = useState<string>("biketrek");
  const [feedSource, setFeedSource] = useState<string>("");

  const [invoices, setInvoices] = useState<ListedFile[]>([]);
  const [feedsConverted, setFeedsConverted] = useState<ListedFile[]>([]);
  const [importsUpgates, setImportsUpgates] = useState<ListedFile[]>([]);

  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);

  const [selectedInvoice, setSelectedInvoice] = useState<string | null>(null);

  const [previews, setPreviews] = useState<Record<string, PreviewState>>({});

  const supplierDisplayName = useMemo(() => {
    const found = suppliers.find(
      (s) => s.supplier_code === supplier || s.name === supplier || String(s.id) === supplier
    );
    return found?.name ?? supplier;
  }, [supplier, suppliers]);

  const [invoiceState, setInvoiceState] = useState<any>(null);

  const processedSet = useMemo(() => {
  const map = invoiceState?.invoices ?? {};
    return new Set(Object.keys(map).filter(k => map[k]?.processed_at));
  }, [invoiceState]);

  function fileKey(f: ListedFile) {
    return (f.path?.split("/").pop() || f.name || "").trim();
  }

  const invoicesNew = invoices.filter(f => !processedSet.has(fileKey(f)));
  const invoicesProcessed = invoices.filter(f => processedSet.has(fileKey(f)));

  const [invTab, setInvTab] = useState<"new"|"processed">("new");

  async function loadSuppliers() {
    try {
      const data = await fetchJSON<Supplier[]>(`${API_BASE}/suppliers`);
      setSuppliers(data);
      if (data.length && !data.some((s) => s.supplier_code === supplier)) {
        setSupplier(data[0].supplier_code || data[0].name);
      }
    } catch (e: any) {
      setError(`Neviem nacitat zoznam dodavatelov. ${e.message}`);
    }
  }

  async function loadInvoiceState() {
  try {
    const s = await fetchJSON<any>(`${API_BASE}/suppliers/${supplier}/invoices/state`);
    setInvoiceState(s);
  } catch {}
  }


  async function loadFiles() {
    if (!supplier) return;
    setLoading(true);
    setError(null);
    try {
      const [inv, feed, imp] = await Promise.all([
        tryFiles(["invoices_csv","invoices"], supplier, API_BASE),
        tryFiles(["feeds_converted","feeds"], supplier, API_BASE),
        tryFiles(["imports_upgates","imports"], supplier, API_BASE),
      ]);
      setInvoices(inv || []);
      setFeedsConverted(feed || []);
      setImportsUpgates(imp || []);
    } catch (e: any) {
      setError(`Neviem nacitat subory. ${e.message}`);
    } finally {
      setLoading(false);
    }
  }

  
  useEffect(() => { loadSuppliers(); }, []);
  useEffect(() => { loadFiles(); loadInvoiceState(); }, [supplier]);
  async function handleRefreshFeed() {
    setError(null);
    setInfo(null);
    try {
      const body: any = {};
      if (feedSource?.trim()) {
        // (len kozmetika) forward-slashes, aby to v logu vyzeralo pekne
        body.source_url = feedSource.trim().replaceAll("\\\\", "/");
      }
      const out = await fetchJSON<{ raw_path?: string; converted_csv?: string }>(
        `${API_BASE}/suppliers/${supplier}/feeds/refresh`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }
      );
      setInfo(
        `Feed OK → raw: ${out.raw_path ?? "?"}, converted: ${out.converted_csv ?? "?"}`
      );
      await loadFiles();   // ← znovu načítaj zoznamy, zobrazí sa nové CSV
    } catch (e: any) {
      setError(`Obnoviť feed zlyhalo: ${e.message}`);
    }
  }

  async function handleRunPrepare() {
    if (!selectedInvoice) return;
    setError(null);
    setInfo(null);
    try {
      const payload = {
        supplier_ref: supplier,
        shop_ref: shop,
        invoice_relpath: selectedInvoice,
      };
      const res = await fetchJSON<{ message?: string }>(`${API_BASE}/runs/prepare`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      setInfo(res?.message || "Prepare run spustený. Pozri sekciu Imports/Upgates.");
      await loadFiles();
    } catch (e: any) {
      setError(`Prepare run zlyhal. ${e.message}`);
    }
  }

  async function togglePreview(f: ListedFile) {
    const id = idFor(f);
    const st = previews[id];
    if (st?.open) {
      setPreviews(prev => ({ ...prev, [id]: { ...(st||{}), open: false } }));
      return;
    }
    if (st && st.data) {
      setPreviews(prev => ({ ...prev, [id]: { ...st, open: true } }));
      return;
    }
    setPreviews(prev => ({ ...prev, [id]: { open: true, loading: true } }));
    try {
      const res = await fetch(`${API_BASE}/files/preview?relpath=${encodeURIComponent(id)}`);
      if (!res.ok) {
        let msg = `${res.status} ${res.statusText}`;
        try { const j = await res.json(); msg = j?.detail || msg } catch {}
        throw new Error(msg);
      }
      const data = await res.json();
      setPreviews(prev => ({ ...prev, [id]: { open: true, loading: false, data } }));
    } catch (e: any) {
      console.error("preview error", e);
      setPreviews(prev => ({ ...prev, [id]: { open: true, loading: false, error: String(e.message || e) } }));
    }
  }

  return (
    <Boundary>
      <div className="min-h-screen bg-slate-950 text-white">
        <header className="sticky top-0 z-20 border-b border-white/10 bg-slate-950/80 backdrop-blur">
          <div className="mx-auto max-w-6xl px-4 py-3 flex items-center justify-between">
            <div className="flex items-center gap-3">
              <div className="h-8 w-8 rounded-xl bg-lime-500" />
              <div>
                <h1 className="text-xl font-bold leading-tight">Supplier Console</h1>
                <div className="text-xs text-white/60">API: {API_BASE}</div>
              </div>
            </div>
            <a href="#" className="text-xs text-white/60 hover:text-white" target="_blank" rel="noreferrer">
              v0.1 • BikeTrek Inventory Hub
            </a>
          </div>
        </header>

        <main className="mx-auto max-w-6xl px-4 py-6 space-y-6">
          <Section title="Ovládanie">
            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
              <SupplierPicker suppliers={suppliers} value={supplier} onChange={setSupplier} />
              <div className="md:col-span-2">
                <FeedControls
                  shop={shop}
                  onShopChange={setShop}
                  feedSource={feedSource}
                  onFeedSourceChange={setFeedSource}
                  onRefresh={handleRefreshFeed}
                />
              <div className="mt-3">
                <FeedUploader
                  supplier={supplier}
                  onSuccess={async (_msg, savedPath) => {
                    // ihneď po uploade spustíme konverziu na CSV
                    if (savedPath) {
                      try {
                        await fetch(`${API_BASE}/suppliers/${supplier}/feeds/refresh`, {
                          method: "POST",
                          headers: { "Content-Type": "application/json" },
                          body: JSON.stringify({ source_url: savedPath }),
                        });
                        setInfo("Feed nahratý a konvertovaný");
                        await loadFiles();
                      } catch (e: any) {
                        setInfo(_msg || "Feed nahratý");
                        setError(`Konverzia po uploade zlyhala: ${e.message}`);
                      }
                    } else {
                      setInfo(_msg || "Feed nahratý");
                    }
                  }}
                  onError={(m) => setError(`Upload zlyhal: ${m}`)}
                />
              </div>                
                <div className="mt-2 text-xs text-white/60">
                  <div>
                    <span className="font-medium text-white">{supplierDisplayName}</span> • Shop:{" "}
                    <span className="font-medium text-white">{shop}</span>
                  </div>
                  {loading && <div className="text-sm text-white/70 mt-1">Načítavam…</div>}
                </div>
              </div>
            </div>
          </Section>

          {error && <DangerBanner title="Chyba" details={error} />}
          {info && <InfoBanner title={info} />}

          <Section title="Invoices (CSV)">
            <div className="flex gap-2 mb-3">
              <button className={invTab==="new" ? "btn-active" : "btn"} onClick={() => setInvTab("new")}>
                New ({invoicesNew.length})
              </button>
              <button className={invTab==="processed" ? "btn-active" : "btn"} onClick={() => setInvTab("processed")}>
                Processed ({invoicesProcessed.length})
              </button>
            </div>            
            <InvoicesTable
              invoices={invTab === "new" ? invoicesNew : invoicesProcessed}
              selectedInvoice={selectedInvoice}
              onSelect={setSelectedInvoice}
              onTogglePreview={togglePreview}
              previews={previews}
              onRunPrepare={handleRunPrepare}
            />
          </Section>

          <Section title="Feeds / Converted">
            <FeedsTable files={feedsConverted} onTogglePreview={togglePreview} previews={previews} />
          </Section>

          <Section title="Imports / Upgates">
            <ImportsTable files={importsUpgates} onTogglePreview={togglePreview} previews={previews} />
          </Section>
        </main>

        <footer className="mx-auto max-w-6xl px-4 py-8 text-xs text-white/50">
          © {new Date().getFullYear()} BikeTrek · Inventory Hub
        </footer>
      </div>
    </Boundary>
  );
}
