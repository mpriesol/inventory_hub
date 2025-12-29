import React from "react";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { Button, OutlineButton } from "@/components/ui/button";
import { Section } from "@/components/ui/Section";
import {
  fetchCsvOutputs,
  previewCsv,
  getInvoicesIndex,
  enrichedPreview,
  getShopConfig,
  applyImports,
  sendImports,
  getInvoiceHistory
} from "@/api/import_console";

import ColumnPickerModal from "./ColumnPickerModal";
import ImageGalleryModal from "./ImageGalleryModal";
import HistoryModal from "./HistoryModal";

const API_BASE: string = (import.meta as any).env?.VITE_API_BASE || "http://127.0.0.1:8000";

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
    "[PRICE_WITH_VAT „Predvolené“]",
    "PRICE_BUY",
    "INVOICE_UNIT_PRICE_EUR",
    "BUY_DELTA_EUR",
    "PRICE_DELTA_EUR",
    "PROFIT_VS_INVOICE_EUR",
    "PROFIT_VS_INVOICE_PCT",
    "AVAILABILITY",
    "IMAGES"
  ],
  new: ["PRODUCT_CODE","TITLE","EAN","[PRICE_WITH_VAT „Predvolené“]","IMAGES"],
  unmatched:["SCM","PRODUCT_CODE","QTY","REASON","IMAGES"]
};

// ktoré stĺpce možno editovať (podľa tabu)
const EDITABLE_PRESET: Record<"updates"|"new"|"unmatched", string[]> = {
  updates: [
    "AVAILABILITY",
    "PRICE_BUY",
    "[PRICE_WITH_VAT „Predvolené“]",
    "INVOICE_QTY",
    "TITLE"
  ],
  new: [
    "TITLE",
    "EAN",
    "[PRICE_WITH_VAT „Predvolené“]",
    "AVAILABILITY",
    "IMAGES"
  ],
  unmatched: []
};

// vypočítavané stĺpce (len na čítanie)
const COMPUTED_COLS = new Set([
  "INVOICE_UNIT_PRICE_EUR",
  "BUY_DELTA_EUR",
  "PRICE_DELTA_EUR",
  "PROFIT_VS_INVOICE_EUR",
  "PROFIT_VS_INVOICE_PCT",
  "SHOP_STOCK_CURRENT",
  "STOCK_DELTA",
  "STOCK_AFTER"
]);

// normalizátor hlavičiek: zjednotí [], úvodzovky (rovné aj „“), zredukuje medzery
const norm = (h: string) =>
  (h ?? "")
    .replace(/^\[|\]$/g, "")
    .replace(/[„”“"']/g, "")
    .replace(/\s+/g, " ")
    .trim();

// jednoduchý parser čísla (znesie €, CZK, medzery ap.)
const toNumber = (v: any): number => {
  if (v === null || v === undefined) return 0;
  let s = String(v).trim();
  if (!s) return 0;
  s = s.replace(/\s|\u00A0/g, "").replace(",", ".").replace(/€|EUR|eur|Kč|CZK|czk/gi, "");
  const n = Number(s);
  return Number.isFinite(n) ? n : 0;
};

// formát „12.34 €“
const euro = (n: number): string => `${n.toFixed(2)} €`;
// --- Download helpers for prepared file ---
const guessRelpath = (absPath: string | undefined | null): string | null => {
  if (!absPath) return null;
  const s = String(absPath).replace(/\\/g, "/");
  const low = s.toLowerCase();
  let idx = low.indexOf("/suppliers/");
  if (idx < 0) idx = low.indexOf("/shops/");
  if (idx < 0) return null;
  return s.substring(idx + 1); // drop leading slash
};

const baseName = (p: string) => {
  const s = String(p).replace(/\\/g, "/");
  const i = s.lastIndexOf("/");
  return i >= 0 ? s.substring(i + 1) : s;
};

const downloadUrlForPrepared = (absPath: string | undefined | null): string | null => {
  const rel = guessRelpath(absPath);
  if (!rel) return null;
  const name = baseName(String(absPath));
  const url = `${API_BASE}/files/download?relpath=${encodeURIComponent(rel)}&filename=${encodeURIComponent(name)}&disposition=attachment`;
  return url;
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

  // EDIT MODE
  const [editMode, setEditMode] = React.useState(false);
  // edits: PRODUCT_CODE -> (normColumn -> value)
  const [edits, setEdits] = React.useState<Record<string, Record<string, string>>>({});

  // výber podľa tabu (držím PRODUCT_CODE)
  const [selectedByTab, setSelectedByTab] = React.useState<Record<"updates"|"new"|"unmatched", Set<string>>>({
    updates: new Set(), new: new Set(), unmatched: new Set(),
  });
  const sel = selectedByTab[tab];

  // --- APPLY/SEND/HISTORY ---
  const [applyInfo, setApplyInfo] = React.useState<{ file?: string; hist?: string } | null>(null);
  const [historyOpen, setHistoryOpen] = React.useState(false);
  const [historyItems, setHistoryItems] = React.useState<any[]>([]);

  // helpers pre hlavičky / indexy
  const colIndex = (name: string) => {
    const cols = preview?.columns || [];
    const iExact = cols.indexOf(name);
    if (iExact >= 0) return iExact;
    const nName = norm(name);
    return cols.findIndex(h => norm(h) === nName);
  };
  const pcIdx = colIndex("PRODUCT_CODE");

  // --- 1) načítaj zoznam faktúr
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

  // --- 2) načítaj uložený preset stĺpcov zo shop configu
  React.useEffect(() => {
    let alive = true;
    getShopConfig(shop)
      .then(cfg => {
        if (!alive) return;
        const saved = cfg?.console?.import_console?.columns;
        if (!saved) return;
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

  // --- 3) načítaj CSV (a Columns All) podľa tabu/detaily/invoice
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
      // pri zmene tabu/faktúry resetni edit draft a prepared info
      setEdits({});
      setApplyInfo(null);
    }).catch(()=>{ setOutputs(null); setPreview(null); });
  }, [supplier, invoice, tab, details, shop]);

  // --- 4) sanitizácia vybraných stĺpcov
  React.useEffect(() => {
    if (!columnsAll.length) return;
    setColumnsSel(prev => {
      const inAll = new Set(columnsAll.map(norm));
      const fix = (arr: string[], key: "updates"|"new"|"unmatched") => {
        const cleaned = arr.filter(c => inAll.has(norm(c)));
        // fallback: ak nič, skús default pre daný tab
        const base = cleaned.length ? cleaned : DEFAULT_COLS[key].filter(c => inAll.has(norm(c)));
        return base;
      };
      return {
        updates: fix(prev.updates || DEFAULT_COLS.updates, "updates"),
        new: fix(prev.new || DEFAULT_COLS.new, "new"),
        unmatched: fix(prev.unmatched || DEFAULT_COLS.unmatched, "unmatched"),
      };
    });
  }, [columnsAll.map(norm).join("|")]);

  const openColumns = () => setPickerOpen(true);

  // finálny zoznam stĺpcov (po sanitizácii)
  const visibleCols = (columnsSel[tab] || DEFAULT_COLS[tab]).filter(c => {
    const inExact = columnsAll.includes(c);
    const inNorm = columnsAll.map(norm).includes(norm(c));
    return inExact || inNorm;
  });

  const rows = preview?.rows || [];
  const pageSize = 100;
  const pageRows = rows.slice(page*pageSize, page*pageSize + pageSize);

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

  const getCellOriginal = (row: any[], colName: string): string => {
    const cols = preview?.columns || [];
    const exactIdx = cols.indexOf(colName);
    const idx = exactIdx >= 0 ? exactIdx : cols.findIndex(h => norm(h) === norm(colName));
    return idx >= 0 ? String(row[idx] ?? "") : "";
  };

  // edity: ak mám edit pre daný code+col, vrátim edit; inak originál
  const getCellValue = (code: string, row: any[], colName: string): string => {
    const ncol = norm(colName);
    const v = edits[code]?.[ncol];
    return v !== undefined ? v : getCellOriginal(row, colName);
  };

  // nakonfigurované stĺpce, ktoré sú editovateľné v danom tabe
  const editableSet = React.useMemo(() => new Set(EDITABLE_PRESET[tab].map(norm)), [tab]);

  // odvodené hodnoty pri zmenách (live prepočet)
  const fmtSigned = (x: number): string => {
    if (!Number.isFinite(x)) return "";
    const sign = x > 0 ? "+" : "";
    return `${sign}${x.toFixed(2)} €`;
  };

  const computeValue = (code: string, row: any[], colName: string): string => {
    const n = norm(colName);

    // pomocné hodnoty
    const invUnit = toNumber(getCellValue(code, row, "INVOICE_UNIT_PRICE_EUR"));
    const shopBuy = toNumber(getCellValue(code, row, "PRICE_BUY"));
    const shopRetail = toNumber(getCellValue(code, row, "[PRICE_WITH_VAT „Predvolené“]"));
    const shopStock = toNumber(getCellValue(code, row, "SHOP_STOCK_CURRENT"));
    const invQty = toNumber(getCellValue(code, row, "INVOICE_QTY"));

    if (n === "BUY_DELTA_EUR") {
      if (invUnit || shopBuy) return fmtSigned(invUnit - shopBuy);
      return "";
    }
    if (n === "PRICE_DELTA_EUR") {
      if (invUnit || shopRetail) return fmtSigned(invUnit - shopRetail);
      return "";
    }
    if (n === "PROFIT_VS_INVOICE_EUR") {
      if (shopRetail && invUnit) return euro(Math.max(shopRetail - invUnit, 0));
      return "";
    }
    if (n === "PROFIT_VS_INVOICE_PCT") {
      if (shopRetail) {
        const pct = ((shopRetail - invUnit) / shopRetail) * 100;
        return `${pct.toFixed(1)} %`;
      }
      return "";
    }
    if (n === "STOCK_DELTA") {
      return Number.isFinite(invQty) ? String(Number.isInteger(invQty) ? invQty : invQty.toFixed(2)) : "";
    }
    if (n === "STOCK_AFTER") {
      const after = shopStock + invQty;
      return Number.isFinite(after) ? String(Number.isInteger(after) ? after : after.toFixed(2)) : "";
    }

    // default – ak nie je computed, len vráť edit/originál
    return getCellValue(code, row, colName);
  };

  const setEdit = (code: string, colName: string, value: string) => {
    const ncol = norm(colName);
    setEdits(prev => {
      const rowMap = {...(prev[code] || {})};
      rowMap[ncol] = value;
      return {...prev, [code]: rowMap};
    });
  };

  const resetEdits = () => setEdits({});

  const getImgUrls = (row: any[]) => {
    const val = getCellOriginal(row, "IMAGES");
    if (!val) return [];
    return String(val).split(";").map(s => s.trim()).filter(Boolean);
  };

  const toggleCode = (code: string, checked: boolean) => {
    setSelectedByTab(prev => {
      const copy = new Set(prev[tab]);
      if (checked) copy.add(code); else copy.delete(code);
      return { ...prev, [tab]: copy };
    });
  };

  const selectAllPage = (checked: boolean) => {
    const visibleCodes: string[] = pageRows
      .map(row => String(pcIdx >= 0 ? row[pcIdx] ?? "" : ""))
      .filter(Boolean);
    setSelectedByTab(prev => {
      const copy = new Set(prev[tab]);
      if (checked) visibleCodes.forEach(c => copy.add(c));
      else visibleCodes.forEach(c => copy.delete(c));
      return { ...prev, [tab]: copy };
    });
  };

  // --- APPLY ---
  const doApply = async () => {
    if (!invoice) return;
    const invParam = invoice.includes(":") ? invoice : `${supplier}:${invoice}`;
    const selectedCodes = Array.from(selectedByTab[tab] || []);
    if (!selectedCodes.length) return;

    // pre edity pošleme len edited polia pre vybrané kódy
    const editsPayload: Record<string, Record<string,string>> = {};
    for (const code of selectedCodes) {
      const rowEdits = edits[code];
      if (!rowEdits) continue;
      editsPayload[code] = {...rowEdits}; // keys = normalized headers; backend si poradí
    }

    const body = {
      invoice_id: invParam,
      shop,
      tab,
      selected_product_codes: selectedCodes,
      edits: editsPayload,
      meta: { append_invoice_ref: true },
      send_now: false
    };

    const r = await applyImports(supplier, body);
    const file = r?.selected_files?.[tab];
    setApplyInfo({ file, hist: r?.history_entry });
  };

  // --- SEND ---
  const doSend = async () => {
    if (!invoice || !applyInfo?.file) return;
    const invParam = invoice.includes(":") ? invoice : `${supplier}:${invoice}`;
    const r = await sendImports(supplier, {
      invoice_id: invParam,
      tab,
      selected_files: [applyInfo.file],
      mode: "upgates-csv"
    });
    if (r?.status === "ok") {
      // refresh history
      openHistory();
    }
  };

  // --- HISTORY ---
  const openHistory = async () => {
    if (!invoice) return;
    const invParam = invoice.includes(":") ? invoice : `${supplier}:${invoice}`;
    const h = await getInvoiceHistory(supplier, invParam);
    setHistoryItems(h?.items || []);
    setHistoryOpen(true);
  };

  // render input podľa typu
  const renderEditor = (code: string, row: any[], col: string) => {
    const n = norm(col);

    // computed = readonly
    if (COMPUTED_COLS.has(n)) {
      return <span className="opacity-80">{computeValue(code, row, col)}</span>;
    }

    if (!editableSet.has(n)) {
      return <span>{getCellValue(code, row, col)}</span>;
    }

    // SPECIAL: AVAILABILITY ako select
    if (n === "AVAILABILITY") {
      const v = getCellValue(code, row, col) || "";
      const opts = ["Na sklade","Na predajni","Na objednávku","Na ceste","Nedostupné"];
      return (
        <select
          className="px-2 py-1 rounded border border-neutral-700 bg-neutral-900 text-neutral-100 focus:outline-none focus:ring-1 focus:ring-neutral-500"
          value={v}
          onChange={e=>setEdit(code, col, e.target.value)}
        >
          <option value=""></option>
          {opts.map(o => <option key={o} value={o}>{o}</option>)}
        </select>
      );
    }

    // čísla (jednoduchá heuristika)
    const isNumeric = /\b(PRICE|QTY|STOCK|DELTA)\b/i.test(n);
    const v = getCellValue(code, row, col) || "";

    return (
      <input
        type={isNumeric ? "number" : "text"}
        step={isNumeric ? "0.01" : undefined}
        className="w-full px-2 py-1 rounded border border-neutral-700 bg-neutral-900 text-neutral-100 focus:outline-none focus:ring-1 focus:ring-neutral-500"
        value={v}
        onChange={e=>setEdit(code, col, e.target.value)}
        placeholder={isNumeric ? "0.00" : ""}
      />
    );
  };

  // je bunka upravená?
  const isEdited = (code: string, colName: string, row: any[]) => {
    const n = norm(colName);
    const cur = getCellValue(code, row, colName);
    const orig = getCellOriginal(row, colName);
    return cur !== orig && editableSet.has(n);
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
                    checked={visibleCodes.length > 0 && visibleCodes.every(c => sel.has(c))}
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
                    const edited = isEdited(code, c, row);
                    return (
                      <td key={c} className={"p-2 text-neutral-900 dark:text-neutral-100 " + (edited ? "bg-amber-50 dark:bg-amber-900/30 ring-1 ring-amber-300/50" : "")}>
                        {editMode ? renderEditor(code, row, c) : computeValue(code, row, c)}
                      </td>
                    );
                  })}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {/* Pagination + selection count + prepared info */}
      <div className="flex flex-wrap justify-between items-center gap-2 mt-3">
        <div className="text-xs opacity-70">
          File: {relForTab()} · Selected: <strong>{sel.size}</strong>
          {editMode && (
            <span className="ml-2 px-2 py-0.5 text-xs rounded-full bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-200">
              Edit mode
            </span>
          )}
          {applyInfo?.file && (
            (() => {
              const url = downloadUrlForPrepared(applyInfo.file);
              const label = baseName(applyInfo.file);
              return url ? (
                <a
                  href={url}
                  target="_blank"
                  rel="noreferrer"
                  className="ml-2 px-2 py-0.5 text-xs rounded-full bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-200 underline decoration-dotted"
                  title="Download prepared *_selected.csv"
                >
                  Prepared: {label}
                </a>
              ) : (
                <span className="ml-2 px-2 py-0.5 text-xs rounded-full bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-200">
                  Prepared: {label}
                </span>
              );
            })()
          )}
        </div>
        <div className="flex items-center gap-2">
          <OutlineButton disabled={page===0} onClick={()=>setPage(p=>Math.max(0,p-1))}>Prev</OutlineButton>
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

        <div className="ml-auto flex flex-wrap items-center gap-2">
          <OutlineButton onClick={()=>setPickerOpen(true)}>Columns…</OutlineButton>

          <label className="flex items-center gap-2 text-sm text-neutral-300 ml-2">
            <input type="checkbox" checked={details} onChange={e=>setDetails(e.target.checked)} />
            Details
          </label>

          <label className="flex items-center gap-2 text-sm text-neutral-300 ml-2">
            <input type="checkbox" checked={editMode} onChange={e=>setEditMode(e.target.checked)} />
            Edit mode
          </label>

          {editMode && (
            <OutlineButton onClick={resetEdits} className="ml-1">Reset edits</OutlineButton>
          )}

          {/* NEW: Apply & Send & History */}
          <Button
            onClick={doApply}
            disabled={(tab === "unmatched") || (selectedByTab[tab]?.size ?? 0) === 0}
            className="bg-emerald-600 hover:bg-emerald-500"
            title="Pripraví *_selected.csv a zapíše históriu (bez odoslania do shopu)."
          >
            Apply selection
          </Button>
          <OutlineButton
            onClick={doSend}
            disabled={!applyInfo?.file || tab === "unmatched"}
            title="Odošle posledný pripravený *_selected.csv (loguje sa do histórie)."
          >
            Send to shop
          </OutlineButton>
          <OutlineButton onClick={openHistory}>History</OutlineButton>

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

      <HistoryModal
        open={historyOpen}
        onClose={()=>setHistoryOpen(false)}
        items={historyItems}
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
