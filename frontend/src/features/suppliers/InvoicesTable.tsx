import React from "react";
import { NiceTable } from "../../components/ui/Table";
import { OutlineButton, PrimaryButton, Pill } from "../../components/ui/button";
import type { ListedFile } from "../../types";

type PreviewState = {
  open: boolean;
  loading?: boolean;
  error?: string | null;
  data?: {
    columns?: string[];
    rows?: any[];
    total_columns?: number;
    preview_rows?: number;
  } | null;
};

export function InvoicesTable({
  invoices = [],
  selectedInvoice = null,
  onSelect,
  onTogglePreview,
  previews = {},
  onRunPrepare,
}: {
  invoices?: ListedFile[];
  selectedInvoice?: string | null;
  onSelect: (id: string | null) => void;
  onTogglePreview: (f: ListedFile) => void;
  previews?: Record<string, PreviewState>;
  onRunPrepare: () => void;
}) {
  // Defenzívne stráže
  const list = Array.isArray(invoices) ? invoices : [];
  const pv: Record<string, PreviewState> = previews || {};

  const header = (
    <div className="flex items-center justify-between mb-2">
      <h3 className="text-sm text-white/80">Invoices (CSV)</h3>
      <Pill>{list.length}</Pill>
    </div>
  );

  return (
    <div className="space-y-3">
      {header}

      {!list.length && (
        <div className="text-sm text-white/60">Žiadne súbory.</div>
      )}

      {!!list.length && (
        <NiceTable>
          {list.map((f, idx) => {
            const id = f.path || f.href || f.name || String(idx);
            const st = pv[id];
            const cols = st?.data?.columns ?? [];
            const rawRows: any[] = Array.isArray(st?.data?.rows) ? (st!.data!.rows as any[]) : [];
            const normRows = rawRows.map((r: any) =>
              Array.isArray(r) ? r : cols.map((_, i) => r?.[i] ?? r?.[String(i)] ?? "")
            );
            const shown = st?.open;

            return (
              <React.Fragment key={id}>
                <tr className="hover:bg-white/5">
                  <td className="px-3 py-2 align-middle text-sm text-white/90">
                    <div className="flex items-center gap-3">
                      <span className="w-2.5 h-2.5 rounded-full bg-lime-400" />
                      <div className="leading-tight">
                        <div className="font-medium text-white">{f.name}</div>
                      </div>
                    </div>
                  </td>
                  <td className="px-3 py-2 align-middle text-right">
                    <div className="flex items-center justify-end gap-3">
                      <label className="inline-flex items-center gap-2 text-sm">
                        <input
                          type="radio"
                          name="invoice"
                          className="scale-110 accent-lime-500"
                          checked={selectedInvoice === id}
                          onChange={() => onSelect(id)}
                        />
                        <span className="text-white/70">vybrať</span>
                      </label>
                      <OutlineButton onClick={() => onTogglePreview(f)}>
                        {shown ? "hide" : "preview"}
                      </OutlineButton>
                    </div>
                  </td>
                </tr>

                {shown && (
                  <tr>
                    <td colSpan={2} className="px-3 pb-3">
                      {st?.loading && (
                        <div className="text-xs text-white/70">Načítavam náhľad…</div>
                      )}
                      {st?.error && (
                        <div className="text-xs text-rose-300">Chyba: {st.error}</div>
                      )}
                      {!!st?.data && (
                        <div className="rounded-xl border border-white/10 bg-slate-900/60 p-3 overflow-x-auto">
                          <table className="w-full text-xs whitespace-nowrap">
                            <thead>
                              <tr className="text-white/70">
                                {cols.map((c, i) => (
                                  <th key={i} className="text-left font-medium py-1 pr-4">
                                    {c}
                                  </th>
                                ))}
                              </tr>
                            </thead>
                            <tbody className="text-white/90">
                              {normRows.slice(0, 20).map((r, ri) => (
                                <tr key={ri} className="border-t border-white/5">
                                  {r.map((cell: any, ci: number) => (
                                    <td key={ci} className="py-1 pr-4 whitespace-nowrap">
                                      {String(cell)}
                                    </td>
                                  ))}
                                </tr>
                              ))}
                            </tbody>
                          </table>
                          <div className="text-[10px] text-white/40 mt-2">
                            Zobrazených {Math.min(20, normRows.length)} z {normRows.length} riadkov.
                          </div>
                        </div>
                      )}
                    </td>
                  </tr>
                )}
              </React.Fragment>
            );
          })}
        </NiceTable>
      )}

      <div>
        <PrimaryButton disabled={!selectedInvoice} onClick={onRunPrepare}>
          Run / Prepare
        </PrimaryButton>
      </div>
    </div>
  );
}
