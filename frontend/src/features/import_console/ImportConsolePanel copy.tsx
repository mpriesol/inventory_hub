import React from "react";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { Button, OutlineButton } from "@/components/ui/button";
import { Section } from "@/components/ui/Section";
import { fetchCsvOutputs, previewCsv, getInvoicesIndex, enrichedPreview, getShopConfig } from "@/api/import_console";
import ColumnPickerModal from "./ColumnPickerModal";
import ImageGalleryModal from "./ImageGalleryModal";

type Props = {
  supplier: string;         // napr. "paul-lange"
  shop: string;             // napr. "biketrek"
  defaultInvoiceId?: string;// "supplier:INVNO" alebo "INVNO"
};

// default preset, ak v shope nie je nič uložené
const DEFAULT_COLS: Record<"updates"|"new"|"unmatched", string[]> = {
  updates: [
    "PRODUCT_CODE",
    "TITLE",
    "INVOICE_QTY",
    "SHOP_STOCK_CURRENT",
    "STOCK_DELTA",
    "STOCK_AFTER",
    "PRICE_WITH_VAT 'Predvolené'",
    "PRICE_BUY",
    "INVOICE_UNIT_PRICE_EUR",
    "BUY_DELTA_EUR",
    "PRICE_DELTA_EUR",
    "PROFIT_VS_INVOICE_EUR",
    "PROFIT_VS_INVOICE_PCT",
    "AVAILABILITY",
    "IMAGES"
  ],
  new: ["PRODUCT_CODE","TITLE","EAN","PRICE_WITH_VAT 'Predvolené'","IMAGES"],
  unmatched:["SCM","PRODUCT_CODE","QTY","REASON","IMAGES"]
};

export default function ImportConsolePanel({ supplier, shop, defaultInvoiceId }: Props) {
  const [invoice, setInvoice] = React.useState<string | null>(defaultInvoiceId || null);
  const [invList, setInvList] = React.useState<any[]>([]);
  const [tab, setTab] = React.useState<"updates" | "new" | "unmatched">("updates");
  const [outputs, setOutputs] = React.useState<any | null>(null);
  const [columnsAll, setColumnsAll] = React.useState<string[]>([]);
  const [columnsSel, setColumnsSel] = React.useState<Record<"updates"|"new"|"unmatched", string[]>>({...DEFAULT_COLS});
  const [pickerOpen, setPickerOpen] = React.useState(false);
  const [gallery, setGallery] = React.useState<{open:boolean; title?:string; images:string[]}>({open:false, images:[]});
  const [preview, setPreview] = React.useState<{columns:string[]; rows:any[]} | null>(null);
  const [page, setPage] = React.useState(0);
  const [details, setDetails] = React.useState(true); // default ON
  const [fullscreen, setFullscreen] = React.useState(false);

  // výber podľa tabu (držím PRODUCT_CODE)
  const [selectedByTab, setSelectedByTab] = React.useState<Record<"updates"|"new"|"unmatched", Set<string>>>({
    updates: new Set(), new: new Set(), unmatched: new Set(),
  });
  const sel = selectedByTab[tab];

  // helpers pre hlavičky / indexy
  const norm = (h: string) => h?.replace(/^\[|\]$/g, "").trim();
  const colIndex = (name: string) => {
    const cols = preview?.columns || [];
    let i = cols.indexOf(name);
    if (i >= 0) return i;
    return cols.findIndex(h => norm(h) === name);
  };
  const pcIdx = colIndex("PRODUCT_CODE");

  // --- 1) načítaj a priprav zoznam faktúr
  React.useEffect(() => {
    getInvoicesIndex(supplier)
      .then((d) => {
        const list = Array.isArray(d?.invoices) ? d.invoices : [];
        list.sort((a: any, b: any) =>
          String(a?.number || "").localeCompare(String(b?.number || ""))
        );
        setInvList(list);
        if (!invoice && list.length) {
          const last = list[list.length - 1];
          const id = last?.invoice_id || `${supplier}:${last?.number}`;
          setInvoice(id);
        }
      })
      .catch((err) => {
        console.error("getInvoicesIndex failed", err);
        setInvList([]);
      });
  }, [supplier]);

  // --- 2) načítaj uložený preset z shops/{shop}/config.json -> console.import_console.columns
  React.useEffect(() => {
    let alive = true;
    getShopConfig(shop)
      .then(cfg => {
        if (!alive) return;
        const saved = cfg?.console?.import_console?.columns;
        if (!saved) return;

        // merge, ale iba platné polia (sanitácia urobíme ešte aj po načítaní datasetu)
        setColumnsSel(prev => {
          const next = {...prev};
          (["updates","new","unmatched"] as const).forEach(k => {
            if (Array.isArray(saved[k]) && saved[k].length) next[k] = [...saved[k]];
          });
          return next;
        });
      })
      .catch(e => console.warn("Shop config load failed:", e));
    return () => { alive = false; };
  }, [shop]);

  // --- 3) načítaj CSV podklady (a Columns All) podľa tabu/detaily/invoice
  React.useEffect(() => {
    if (!invoice) return;
    const invParam = invoice.includes(":") ? invoice : `${supplier}:${invoice}`;

    fetchCsvOutputs(supplier, invParam).then(async (d) => {
      setOutputs(d);
      const entry = (d as any)[tab];
      if (entry?.relpath) {
        let pv;
        if (details && tab === "updates") {
          pv = await enrichedPreview(supplier, invParam, shop, tab, 0, 250);
        } else {
          pv = await previewCsv(entry.relpath, 250);
        }
        setColumnsAll(pv.columns || []);
        setPreview(pv);
      } else {
        setColumnsAll([]);
        setPreview(null);
      }
      setPage(0);
    }).catch(()=>{ setOutputs(null); setPreview(null); });
  }, [supplier, invoice, tab, details, shop]);

  // --- 4) sanitizuj vybrané stĺpce podľa aktuálne dostupných hlavičiek
  React.useEffect(() => {
    if (!columnsAll.length) return;
    setColumnsSel(prev => {
      const inAll = new Set(columnsAll.map(norm));
      const fix = (arr: string[]) => {
        const cleaned = arr.filter(c => inAll.has(norm(c)));
        return cleaned.length ? cleaned : DEFAULT_COLS[tab].filter(c => inAll.has(norm(c)));
      };
      return {
        updates: fix(prev.updates || DEFAULT_COLS.updates),
        new: fix(prev.new || DEFAULT_COLS.new),
        unmatched: fix(prev.unmatched || DEFAULT_COLS.unmatched),
      };
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [columnsAll.join("|")]); // zámerne string, aby sa efekt trigerol len pri zmene sady

  const openColumns = () => setPickerOpen(true);

  // finálny zoznam stĺpcov (už po sanitizácii)
  const visibleCols = (columnsSel[tab] || DEFAULT_COLS[tab]).filter(c => {
    const inExact = columnsAll.includes(c);
    const inNorm = columnsAll.map(norm).includes(norm(c));
    return inExact || inNorm;
  });

  const rows = preview?.rows || [];
  const pageSize = 100;
  const pageRows = rows.slice(page*pageSize, page*pageSize + pageSize);

  const getImgUrls = (row: any[]) => {
    const idx = colIndex("IMAGES");
    if (idx < 0) return [];
    const val = String(row[idx] ?? "").trim();
    if (!val) return [];
    return val.split(";").map(s => s.trim()).filter(Boolean);
  };

  const relForTab = () => (outputs?.[tab]?.relpath || null);

  // výber (per-page master, indeterminate)
  const visibleCodes: string[] = pageRows
    .map(row => String(pcIdx >= 0 ? row[pcIdx] ?? "" : ""))
    .filter(Boolean);

  const allSelected = visibleCodes.length > 0 && visibleCodes.every(c => sel.has(c));
  const someSelected = visibleCodes.some(c => sel.has(c)) && !allSelected;

  const masterRef = React.useRef<HTMLInputElement>(null);
  React.useEffect(() => {
    if (masterRef.current) masterRef.current.indeterminate = someSelected;
  }, [someSelected, page, preview]);

  const toggleCode = (code: string, checked: boolean) => {
    setSelectedByTab(prev => {
      const copy = new Set(prev[tab]);
      if (checked) copy.add(code); else copy.delete(code);
      return { ...prev, [tab]: copy };
    });
  };

  const selectAllPage = (checked: boolean) => {
    setSelectedByTab(prev => {
      const copy = new Set(prev[tab]);
      if (checked) visibleCodes.forEach(c => copy.add(c));
      else visibleCodes.forEach(c => copy.delete(c));
      return { ...prev, [tab]: copy };
    });
  };

  // render table (zdieľané aj pre fullscreen)
  const renderTable = () => (
    <>
      <div className="overflow-auto border border-neutral-800 rounded-2xl">
        <table className="min-w-full text-sm">
          <thead className="sticky top-0 bg-white dark:bg-neutral-900 text-neutral-900 dark:text-neutral-100">
            <tr>
              {/* Sel master */}
              <th className="p-2 w-16 sticky left-0 bg-white dark:bg-neutral-900">
                <div className="flex items-center gap-2">
                  <input
                    ref={masterRef}
                    type="checkbox"
                    checked={allSelected}
                    onChange={e=>selectAllPage(e.target.checked)}
                  />
                  <span className="text-xs opacity-70">Sel</span>
                </div>
              </th>
              {/* image col */}
              <th className="p-2 w-16 sticky left-16 bg-white dark:bg-neutral-900">Img</th>
              {visibleCols.map(c => <th key={c} className="p-2 text-left">{c}</th>)}
            </tr>
          </thead>
          <tbody className="bg-white dark:bg-neutral-900">
            {pageRows.map((row, i) => {
              const imgUrls = getImgUrls(row);
              const code = String(pcIdx >= 0 ? row[pcIdx] ?? "" : "");
              const isChecked = code && sel.has(code);
              return (
                <tr key={i} className="border-t border-neutral-200 dark:border-neutral-800 hover:bg-neutral-50 dark:hover:bg-neutral-800/50">
                  {/* checkbox cell */}
                  <td className="p-2 w-16 sticky left-0 bg-white dark:bg-neutral-900">
                    <input
                      type="checkbox"
                      disabled={!code}
                      checked={!!code && isChecked}
                      onChange={e=>toggleCode(code, e.target.checked)}
                      title={code ? `Select ${code}` : "No PRODUCT_CODE"}
                    />
                  </td>

                  {/* image cell */}
                  <td className="p-2 w-16 sticky left-16 bg-white dark:bg-neutral-900">
                    {imgUrls[0] ? (
                      <img
                        src={imgUrls[0]}
                        onClick={()=>setGallery({open:true, images:imgUrls})}
                        className="h-12 w-12 object-contain cursor-zoom-in rounded bg-neutral-100 dark:bg-neutral-800"
                      />
                    ) : <div className="h-12 w-12 rounded bg-neutral-100 dark:bg-neutral-800" />}
                  </td>

                  {/* dynamic cells */}
                  {visibleCols.map(c => {
                    const exactIdx = (preview?.columns || []).indexOf(c);
                    const idx = exactIdx >= 0 ? exactIdx : (preview?.columns || []).findIndex(h => norm(h) === norm(c));
                    const v = idx >= 0 ? row[idx] : "";
                    return <td key={c} className="p-2 text-neutral-900 dark:text-neutral-100">{String(v ?? "")}</td>;
                  })}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {/* Pagination + selection count */}
      <div className="flex justify-between items-center mt-3">
        <div className="text-xs opacity-70">
          File: {relForTab()} · Selected: <strong>{sel.size}</strong>
        </div>
        <div className="flex items-center gap-2">
          <OutlineButton disabled={page===0} onClick={()=>setPage(p=>Math.max(0, p-1))}>Prev</OutlineButton>
          <div className="text-sm">Page {page+1}</div>
          <OutlineButton disabled={(page+1)*pageSize >= rows.length} onClick={()=>setPage(p=>p+1)}>Next</OutlineButton>
        </div>
      </div>
    </>
  );

  // lock body scroll v fullscreen
  React.useEffect(() => {
    if (fullscreen) {
      const prev = document.body.style.overflow;
      document.body.style.overflow = "hidden";
      return () => { document.body.style.overflow = prev; };
    }
  }, [fullscreen]);

  // ESC na zatvorenie fullscreen
  React.useEffect(() => {
    if (!fullscreen) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setFullscreen(false); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [fullscreen]);

  return (
    <Section title="Import Console">
      <div className="flex flex-wrap items-center gap-2 mb-3">
        <label className="text-sm">Invoice:</label>
        <select
          className="border border-neutral-700 rounded-xl px-3 py-2 bg-neutral-900 text-neutral-100
                      focus:outline-none focus:ring-1 focus:ring-neutral-500"
          value={invoice || ""}
          onChange={e=>setInvoice(e.target.value)}
        >
          {invList.map((it:any) => {
            const id = it.invoice_id || `${supplier}:${it.number}`;
            return (
              <option key={id} value={id} className="bg-neutral-900 text-neutral-100">
                {it.number}
              </option>
            );
          })}
        </select>

        <div className="ml-auto flex items-center gap-2">
          <OutlineButton onClick={()=>setPickerOpen(true)}>Columns…</OutlineButton>
          <label className="flex items-center gap-2 text-sm text-neutral-300 ml-2">
            <input type="checkbox" checked={details} onChange={e=>setDetails(e.target.checked)} />
            Details
          </label>
          <OutlineButton onClick={()=>setFullscreen(true)}>Fullscreen</OutlineButton>
        </div>
      </div>

      <Tabs value={tab} onValueChange={(v)=>setTab(v as any)}>
        <TabsList className="inline-flex items-center gap-1 border border-neutral-800 rounded-xl bg-neutral-900 p-1">
          <TabsTrigger value="updates" className="px-3 py-1.5 rounded-lg text-sm text-neutral-300 data-[state=active]:bg-neutral-800 data-[state=active]:text-white data-[state=active]:shadow focus:outline-none">
            Updates
          </TabsTrigger>
          <TabsTrigger value="new" className="px-3 py-1.5 rounded-lg text-sm text-neutral-300 data-[state=active]:bg-neutral-800 data-[state=active]:text-white data-[state=active]:shadow focus:outline-none">
            New
          </TabsTrigger>
          <TabsTrigger value="unmatched" className="px-3 py-1.5 rounded-lg text-sm text-neutral-300 data-[state=active]:bg-neutral-800 data-[state=active]:text-white data-[state=active]:shadow focus:outline-none">
            Unmatched
          </TabsTrigger>
        </TabsList>

        {(["updates","new","unmatched"] as const).map(tk => (
          <TabsContent key={tk} value={tk} className="mt-4">
            {tab === tk ? (
              (outputs?.[tab]?.relpath ? renderTable() : <div className="text-sm text-gray-500 border rounded-xl p-4">No CSV for this tab.</div>)
            ) : null}
          </TabsContent>
        ))}
      </Tabs>

      <ColumnPickerModal
        open={pickerOpen}
        onClose={()=>setPickerOpen(false)}
        allColumns={columnsAll}
        selected={columnsSel[tab] || []}
        onChangeSelected={(cols)=>setColumnsSel(s=>({...s,[tab]:cols}))}
        tabKey={tab}
        shop={shop}
      />

      <ImageGalleryModal
        open={gallery.open}
        onClose={()=>setGallery({open:false, images:[]})}
        images={gallery.images}
      />

      {/* Fullscreen overlay */}
      {fullscreen && (
        <div className="fixed inset-0 z-[200]">
          <div className="absolute inset-0 bg-black/70" onClick={() => setFullscreen(false)} />
          <div
            className="relative mx-auto mt-[3vh] w-[98vw] h-[94vh] rounded-2xl
                       bg-neutral-900 text-neutral-100 shadow-2xl border border-neutral-800
                       overflow-hidden"
          >
            <div className="flex items-center justify-between px-4 py-3 border-b border-neutral-800">
              <div className="font-semibold">Import Console — {invoice}</div>
              <button
                className="px-3 py-1.5 rounded-xl border border-neutral-700 hover:bg-neutral-800"
                onClick={() => setFullscreen(false)}
              >
                Close
              </button>
            </div>
            <div className="p-4 h-[calc(94vh-56px)] overflow-auto">
              {renderTable()}
            </div>
          </div>
        </div>
      )}
    </Section>
  );
}
