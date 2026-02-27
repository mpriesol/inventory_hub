// frontend/src/components/ReceivingResultsModal.tsx
//
// Editovateľná tabuľka pre 3 výstupné CSV súbory po spracovaní faktúry.
// Stĺpce sú konfigurovateľné – ukladajú sa do localStorage per-tab.
// Použitie:
//   <ReceivingResultsModal
//     supplier="paul-lange"
//     invoiceId="paul-lange:F2025060682"
//     onClose={() => setOpen(false)}
//   />

import { useState, useEffect, useCallback, useRef } from "react";

const API = import.meta.env.VITE_API_BASE ?? "/api";

// ── Typy ────────────────────────────────────────────────────────────────────

interface CsvData {
  columns: string[];
  rows: string[][];
}

type Tab = "updates" | "new" | "unmatched";

interface ColVisibility {
  [col: string]: boolean;
}

interface EditCell {
  rowIdx: number;
  col: string;
}

interface PrepareResult {
  run_id: string;
  stats: { existing_rows: number; new_rows: number; unmatched_rows: number };
  outputs: {
    existing: string | null;
    new: string | null;
    unmatched: string | null;
  };
}

interface Props {
  supplier: string;
  invoiceId: string;        // napr. "paul-lange:F2025060682"
  shop?: string;
  onClose: () => void;
}

// ── Helpers ──────────────────────────────────────────────────────────────────

const tabLabel: Record<Tab, string> = {
  updates: "Aktualizácia skladu",
  new: "Nové produkty",
  unmatched: "Nenájdené",
};

const tabOutputKey: Record<Tab, keyof PrepareResult["outputs"]> = {
  updates: "existing",
  new: "new",
  unmatched: "unmatched",
};

function storageKey(supplier: string, tab: Tab) {
  return `ih_col_vis__${supplier}__${tab}`;
}

function loadColVis(supplier: string, tab: Tab, cols: string[]): ColVisibility {
  try {
    const raw = localStorage.getItem(storageKey(supplier, tab));
    if (raw) {
      const saved: ColVisibility = JSON.parse(raw);
      // merge so that new columns default to true
      const merged: ColVisibility = {};
      cols.forEach((c) => { merged[c] = saved[c] !== undefined ? saved[c] : true; });
      return merged;
    }
  } catch { /* ignore */ }
  // default: first 8 cols visible, rest hidden
  const vis: ColVisibility = {};
  cols.forEach((c, i) => { vis[c] = i < 8; });
  return vis;
}

function saveColVis(supplier: string, tab: Tab, vis: ColVisibility) {
  try { localStorage.setItem(storageKey(supplier, tab), JSON.stringify(vis)); } catch { /* ignore */ }
}

// Odstráni Upgates [] závorky pre zobrazenie
function displayCol(col: string) {
  return col.replace(/^\[/, "").replace(/\]$/, "").replace(/^PARAMETER „/, "").replace(/"$/, "");
}

// ── Komponent ────────────────────────────────────────────────────────────────

export function ReceivingResultsModal({ supplier, invoiceId, shop = "biketrek", onClose }: Props) {
  const [preparing, setPreparing] = useState(false);
  const [prepResult, setPrepResult] = useState<PrepareResult | null>(null);
  const [prepError, setPrepError] = useState<string | null>(null);

  const [activeTab, setActiveTab] = useState<Tab>("updates");
  const [csvData, setCsvData] = useState<Partial<Record<Tab, CsvData>>>({});
  const [loadingCsv, setLoadingCsv] = useState(false);

  const [colVis, setColVis] = useState<ColVisibility>({});
  const [showColPicker, setShowColPicker] = useState(false);

  // editácia bunky
  const [editCell, setEditCell] = useState<EditCell | null>(null);
  const [editValue, setEditValue] = useState("");
  const editInputRef = useRef<HTMLInputElement>(null);

  // filter
  const [filterText, setFilterText] = useState("");

  // ── Spusti prípravu (prepare_legacy) ─────────────────────────────────────

  const runPrepare = useCallback(async () => {
    setPreparing(true);
    setPrepError(null);
    setPrepResult(null);
    setCsvData({});

    try {
      const body = {
        supplier_ref: supplier,
        shop_ref: shop,
        invoice_relpath: `suppliers/${supplier}/invoices/csv/${invoiceId.split(":").pop()}.csv`,
      };
      const r = await fetch(`${API}/runs/prepare_legacy`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(err.detail ?? `HTTP ${r.status}`);
      }
      const result: PrepareResult = await r.json();
      setPrepResult(result);
    } catch (e: unknown) {
      setPrepError(e instanceof Error ? e.message : String(e));
    } finally {
      setPreparing(false);
    }
  }, [supplier, shop, invoiceId]);

  // Spusti hneď po otvorení
  useEffect(() => { runPrepare(); }, [runPrepare]);

  // ── Načítaj CSV dáta pre aktívny tab ─────────────────────────────────────

  useEffect(() => {
    if (!prepResult) return;
    const outputPath = prepResult.outputs[tabOutputKey[activeTab]];
    if (!outputPath) return;
    if (csvData[activeTab]) return; // už načítané

    setLoadingCsv(true);
    const relpath = outputPath.replace(/^data\//, "");
    fetch(`${API}/files/preview?relpath=${encodeURIComponent(relpath)}&max_rows=500&strip_upgates_brackets=false`)
      .then((r) => r.ok ? r.json() : Promise.reject(`HTTP ${r.status}`))
      .then((data: { columns: string[]; rows: string[][] }) => {
        setCsvData((prev) => ({ ...prev, [activeTab]: data }));
      })
      .catch((e) => console.error("CSV preview error:", e))
      .finally(() => setLoadingCsv(false));
  }, [prepResult, activeTab, csvData]);

  // Nastavenie viditeľnosti stĺpcov pri načítaní nových dát
  useEffect(() => {
    const data = csvData[activeTab];
    if (!data) return;
    setColVis(loadColVis(supplier, activeTab, data.columns));
  }, [csvData, activeTab, supplier]);

  // Focus na input pri editácii
  useEffect(() => {
    if (editCell) editInputRef.current?.focus();
  }, [editCell]);

  // ── Editácia ──────────────────────────────────────────────────────────────

  const startEdit = (rowIdx: number, col: string, current: string) => {
    setEditCell({ rowIdx, col });
    setEditValue(current);
  };

  const commitEdit = () => {
    if (!editCell) return;
    const { rowIdx, col } = editCell;
    const data = csvData[activeTab];
    if (!data) return;
    const colIdx = data.columns.indexOf(col);
    if (colIdx < 0) return;

    const newRows = data.rows.map((r, i) => {
      if (i !== rowIdx) return r;
      const copy = [...r];
      while (copy.length <= colIdx) copy.push("");
      copy[colIdx] = editValue;
      return copy;
    });

    setCsvData((prev) => ({
      ...prev,
      [activeTab]: { ...data, rows: newRows },
    }));
    setEditCell(null);
  };

  // ── Viditeľnosť stĺpcov ───────────────────────────────────────────────────

  const toggleCol = (col: string) => {
    setColVis((prev) => {
      const next = { ...prev, [col]: !prev[col] };
      saveColVis(supplier, activeTab, next);
      return next;
    });
  };

  // ── Filter riadkov ────────────────────────────────────────────────────────

  const filteredRows = useCallback(() => {
    const data = csvData[activeTab];
    if (!data) return [];
    const q = filterText.toLowerCase().trim();
    if (!q) return data.rows;
    return data.rows.filter((row) =>
      row.some((cell) => (cell ?? "").toLowerCase().includes(q))
    );
  }, [csvData, activeTab, filterText]);

  const visibleCols = csvData[activeTab]?.columns.filter((c) => colVis[c]) ?? [];

  // ── Download upraveného CSV ───────────────────────────────────────────────

  const downloadCsv = () => {
    const data = csvData[activeTab];
    if (!data) return;

    const escape = (v: string) => {
      if (v.includes(";") || v.includes('"') || v.includes("\n"))
        return `"${v.replace(/"/g, '""')}"`;
      return v;
    };

    const lines = [
      data.columns.map(escape).join(";"),
      ...data.rows.map((r) =>
        data.columns.map((_, i) => escape(r[i] ?? "")).join(";")
      ),
    ];

    const blob = new Blob(["\uFEFF" + lines.join("\r\n")], {
      type: "text/csv;charset=utf-8",
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${invoiceId.split(":").pop()}_${activeTab}_edited.csv`;
    a.click();
    URL.revokeObjectURL(url);
  };

  // ── Štatistiky ────────────────────────────────────────────────────────────

  const stats = prepResult?.stats;
  const tabCounts: Record<Tab, number> = {
    updates: stats?.existing_rows ?? 0,
    new: stats?.new_rows ?? 0,
    unmatched: stats?.unmatched_rows ?? 0,
  };

  // ── Render ────────────────────────────────────────────────────────────────

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70">
      <div
        className="relative flex flex-col bg-[#1a1a1a] border border-[#333] rounded-xl shadow-2xl"
        style={{ width: "96vw", height: "92vh", maxWidth: 1600 }}
      >
        {/* ── Header ── */}
        <div className="flex items-center justify-between px-5 py-3 border-b border-[#2a2a2a]">
          <div>
            <span className="text-white font-semibold text-base">Výsledky príjmu</span>
            <span className="ml-3 text-[#888] text-sm font-mono">{invoiceId}</span>
          </div>
          <button
            onClick={onClose}
            className="text-[#888] hover:text-white text-xl leading-none px-2"
          >
            ✕
          </button>
        </div>

        {/* ── Stav prípravy ── */}
        {preparing && (
          <div className="flex-1 flex items-center justify-center text-[#888]">
            <span className="animate-pulse">Spracúvam faktúru…</span>
          </div>
        )}

        {prepError && (
          <div className="flex-1 flex flex-col items-center justify-center gap-4">
            <div className="text-red-400 text-sm max-w-lg text-center">{prepError}</div>
            <button
              onClick={runPrepare}
              className="px-4 py-2 bg-amber-600 hover:bg-amber-500 text-white text-sm rounded-lg"
            >
              Skúsiť znovu
            </button>
          </div>
        )}

        {prepResult && (
          <>
            {/* ── Tabs ── */}
            <div className="flex items-center gap-1 px-5 pt-3 border-b border-[#2a2a2a]">
              {(["updates", "new", "unmatched"] as Tab[]).map((tab) => (
                <button
                  key={tab}
                  onClick={() => { setActiveTab(tab); setFilterText(""); }}
                  className={`px-4 py-2 text-sm rounded-t-lg transition-colors ${
                    activeTab === tab
                      ? "bg-[#252525] text-amber-400 border border-b-0 border-[#333]"
                      : "text-[#888] hover:text-white"
                  }`}
                >
                  {tabLabel[tab]}
                  <span
                    className={`ml-2 text-xs px-1.5 py-0.5 rounded-full ${
                      tab === "unmatched"
                        ? "bg-red-900/50 text-red-400"
                        : "bg-[#333] text-[#aaa]"
                    }`}
                  >
                    {tabCounts[tab]}
                  </span>
                </button>
              ))}

              {/* Toolbar napravo */}
              <div className="ml-auto flex items-center gap-2 pb-1">
                {/* Filter */}
                <input
                  value={filterText}
                  onChange={(e) => setFilterText(e.target.value)}
                  placeholder="Filtrovať…"
                  className="px-3 py-1 bg-[#252525] border border-[#333] rounded-lg text-sm text-white placeholder-[#555] w-44 focus:outline-none focus:border-amber-500"
                />

                {/* Stĺpce */}
                <div className="relative">
                  <button
                    onClick={() => setShowColPicker((v) => !v)}
                    className="px-3 py-1 bg-[#252525] border border-[#333] rounded-lg text-sm text-[#aaa] hover:text-white"
                  >
                    Stĺpce
                  </button>
                  {showColPicker && csvData[activeTab] && (
                    <div
                      className="absolute right-0 top-8 z-10 bg-[#1e1e1e] border border-[#333] rounded-xl shadow-xl p-3 w-72 max-h-96 overflow-y-auto"
                      onClick={(e) => e.stopPropagation()}
                    >
                      <div className="text-xs text-[#666] mb-2 font-semibold uppercase tracking-wider">
                        Viditeľné stĺpce
                      </div>
                      {csvData[activeTab]!.columns.map((col) => (
                        <label
                          key={col}
                          className="flex items-center gap-2 py-1 cursor-pointer hover:text-white text-[#aaa] text-xs"
                        >
                          <input
                            type="checkbox"
                            checked={colVis[col] ?? true}
                            onChange={() => toggleCol(col)}
                            className="accent-amber-500"
                          />
                          <span className="truncate">{displayCol(col)}</span>
                        </label>
                      ))}
                    </div>
                  )}
                </div>

                {/* Download */}
                <button
                  onClick={downloadCsv}
                  disabled={!csvData[activeTab]}
                  className="px-3 py-1 bg-amber-600 hover:bg-amber-500 disabled:opacity-40 text-white text-sm rounded-lg"
                >
                  ↓ Stiahnuť CSV
                </button>
              </div>
            </div>

            {/* ── Tabuľka ── */}
            <div
              className="flex-1 overflow-auto"
              onClick={() => { if (showColPicker) setShowColPicker(false); }}
            >
              {loadingCsv ? (
                <div className="flex items-center justify-center h-full text-[#888] animate-pulse">
                  Načítavam…
                </div>
              ) : !prepResult.outputs[tabOutputKey[activeTab]] ? (
                <div className="flex items-center justify-center h-full text-[#555] text-sm">
                  {activeTab === "unmatched" ? "Žiadne nenájdené produkty 🎉" : "Žiadne záznamy"}
                </div>
              ) : !csvData[activeTab] ? (
                <div className="flex items-center justify-center h-full text-[#888] animate-pulse">
                  Načítavam CSV…
                </div>
              ) : (
                <table className="w-full text-xs border-collapse" style={{ tableLayout: "fixed" }}>
                  <colgroup>
                    {visibleCols.map((col) => (
                      <col
                        key={col}
                        style={{
                          width:
                            col.includes("DESCRIPTION") || col.includes("TITLE")
                              ? 260
                              : col.includes("PRODUCT_CODE")
                              ? 130
                              : 100,
                        }}
                      />
                    ))}
                  </colgroup>
                  <thead className="sticky top-0 z-10">
                    <tr className="bg-[#1e1e1e] border-b border-[#2a2a2a]">
                      {visibleCols.map((col) => (
                        <th
                          key={col}
                          className="text-left px-2 py-2 text-[#666] font-medium whitespace-nowrap overflow-hidden text-ellipsis select-none"
                          title={col}
                        >
                          {displayCol(col)}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {filteredRows().map((row, rowIdx) => (
                      <tr
                        key={rowIdx}
                        className="border-b border-[#222] hover:bg-[#222]/50 group"
                      >
                        {visibleCols.map((col) => {
                          const colIdx = csvData[activeTab]!.columns.indexOf(col);
                          const cellVal = row[colIdx] ?? "";
                          const isEditing =
                            editCell?.rowIdx === rowIdx && editCell?.col === col;

                          return (
                            <td
                              key={col}
                              className="px-2 py-1.5 text-[#ccc] overflow-hidden"
                              style={{ maxWidth: 260 }}
                            >
                              {isEditing ? (
                                <input
                                  ref={editInputRef}
                                  value={editValue}
                                  onChange={(e) => setEditValue(e.target.value)}
                                  onBlur={commitEdit}
                                  onKeyDown={(e) => {
                                    if (e.key === "Enter") commitEdit();
                                    if (e.key === "Escape") setEditCell(null);
                                  }}
                                  className="w-full bg-[#2a2a2a] border border-amber-500 rounded px-1 py-0.5 text-white focus:outline-none"
                                />
                              ) : (
                                <span
                                  className="block truncate cursor-pointer hover:text-white"
                                  title={cellVal}
                                  onDoubleClick={() => startEdit(rowIdx, col, cellVal)}
                                >
                                  {cellVal || (
                                    <span className="text-[#444] italic">–</span>
                                  )}
                                </span>
                              )}
                            </td>
                          );
                        })}
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>

            {/* ── Footer ── */}
            <div className="px-5 py-2 border-t border-[#2a2a2a] flex items-center justify-between text-xs text-[#555]">
              <span>
                {filteredRows().length} riadkov
                {filterText && ` (filtrované z ${csvData[activeTab]?.rows.length ?? 0})`}
              </span>
              <span className="text-[#444]">
                Dvakrát kliknite na bunku pre editáciu · Enter = potvrdiť · Esc = zrušiť
              </span>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
