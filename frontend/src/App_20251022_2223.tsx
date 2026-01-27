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
          if (d) detail += `
${typeof d === "string" ? d : JSON.stringify(d)}`;
        } catch {}
        throw new Error(detail);
      }
      const data = ct.includes("application/json") ? await res.json() : {};
      const arr: string[] = Array.isArray(data) ? data : ((data as any)?.files ?? []);
      const mapped = arr.map((relpath: string) => ({
        name: relpath.split('/').pop() || relpath,
        path: relpath,
      }));
      return mapped;
    } catch (e: any) {
      // pri 400/404 skús ďalší variant, inak vyhoď
      if (!/^400|404/.test(String(e.message))) throw e;
    }
  }
  return [];
}


function openPreview(f: ListedFile) {
  const href = f.href || (f.path ? `${API_BASE}/files/preview?relpath=${encodeURIComponent(f.path)}` : undefined);
  if (href) window.open(href, "_blank");
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

  const supplierDisplayName = useMemo(() => {
    const found = suppliers.find(
      (s) => s.supplier_code === supplier || s.name === supplier || String(s.id) === supplier
    );
    return found?.name ?? supplier;
  }, [supplier, suppliers]);

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
      setInvoices(inv);
      setFeedsConverted(feed);
      setImportsUpgates(imp);
    } catch (e: any) {
      setError(`Neviem nacitat subory. ${e.message}`);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadSuppliers();
  }, []);

  useEffect(() => {
    loadFiles();
  }, [supplier]);

  async function handleRefreshFeed() {
    setError(null);
    setInfo(null);
    try {
      const body: any = {};
      if (feedSource?.trim()) body.source_url = feedSource.trim();
      const data = await fetchJSON<{ message?: string }>(`${API_BASE}/suppliers/${supplier}/feeds/refresh`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      setInfo(data?.message || "Feed úspešne aktualizovaný");
      await loadFiles();
    } catch (e: any) {
      setError(
        `Nepodarilo sa obnovit feed. Pravdepodobne 403 Forbidden od dodavatela (nepovolena IP, chyba prihlasenia alebo blokovany port).\n\nDetail: ${e.message}`
      );
    }
  }

  async function handleRunPrepare() {
    if (!selectedInvoice) return;
    setError(null);
    setInfo(null);
    try {
      const payload = { supplier, shop, invoice_csv: selectedInvoice } as any;
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

  return (
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
          <a
            href="https://github.com/"
            className="text-xs text-white/60 hover:text-white"
            target="_blank"
            rel="noreferrer"
          >
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
                onSuccess={(m) => { setInfo(m || "Upload OK"); loadFiles(); }}
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
          <InvoicesTable
            invoices={invoices}
            selectedInvoice={selectedInvoice}
            onSelect={setSelectedInvoice}
            onPreview={openPreview}
            onRunPrepare={handleRunPrepare}
          />
        </Section>

        <Section title="Feeds / Converted">
          <FeedsTable files={feedsConverted} onPreview={openPreview} />
        </Section>

        <Section title="Imports / Upgates">
          <ImportsTable files={importsUpgates} onPreview={openPreview} />
        </Section>

        <Section title="Poznámka k chybe 403 (Paul‑Lange)">
          <div className="text-sm leading-relaxed text-white/80 space-y-2">
            <p>
              Ak pri <span className="font-semibold">Obnoviť feed</span> dostaneš <span className="font-semibold">403 Forbidden</span>, je to odpoveď
              vzdialeného servera dodávateľa (nie CORS). Najčastejšie príčiny: nepovolená IP (whitelist),
              chýbajúce prihlasovacie údaje (HTTP Basic), alebo zablokovaný port <code>8081</code>.
            </p>
            <ul className="list-disc pl-5 space-y-1">
              <li>Skús v <code>/suppliers/{'{supplier}'}/config</code> doplniť <code>config_json</code> s <code>auth</code> (HTTP Basic) a <code>feed_url</code>.</li>
              <li>Požiadaj dodávateľa o whitelist verejnej IP tvojho backendu (miestneho PC/servera).</li>
              <li>Prípadne použi lokálny súbor/URL v poli „Feed source“ a stlač Obnoviť feed.</li>
            </ul>
          </div>
        </Section>
      </main>

      <footer className="mx-auto max-w-6xl px-4 py-8 text-xs text-white/50">
        © {new Date().getFullYear()} BikeTrek · Inventory Hub
      </footer>
    </div>
  );
}
