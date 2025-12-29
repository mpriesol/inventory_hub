import React from "react";
import { NiceTable } from "../../components/ui/Table";
import { OutlineButton, Pill } from "../../components/ui/button";
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

export function ImportsTable({ files, onTogglePreview, previews }: {
  files: ListedFile[];
  onTogglePreview: (f: ListedFile) => void;
  previews: Record<string, PreviewState>;
}) {
  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="text-sm text-white/80">Imports / Upgates</h3>
        <Pill>{files.length}</Pill>
      </div>
      {!files.length && <div className="text-sm text-white/60">Zatiaľ nič. Spusti Prepare run.</div>}
      {!!files.length && (
        <NiceTable>
          {files.map((f) => {
            const id = f.path || f.href || f.name;
            const st = previews[id];
            const cols = st?.data?.columns ?? [];
            const rawRows: any[] = Array.isArray(st?.data?.rows) ? (st!.data!.rows as any[]) : [];
            const normRows = rawRows.map((r: any) => Array.isArray(r) ? r : cols.map((_, i) => r?.[i] ?? r?.[String(i)] ?? ""));
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
                    <OutlineButton onClick={() => onTogglePreview(f)}>
                      {shown ? "hide" : "preview"}
                    </OutlineButton>
                  </td>
                </tr>

                {shown && (
                  <tr>
                    <td colSpan={2} className="px-3 pb-3">
                      {st?.loading && <div className="text-xs text-white/70">Načítavam náhľad…</div>}
                      {st?.error && <div className="text-xs text-rose-300">Chyba: {st.error}</div>}
                      {!!st?.data && (
                        <div className="rounded-xl border border-white/10 bg-slate-900/60 p-3 overflow-x-auto">
                          <table className="w-full text-xs whitespace-nowrap">
                            <thead>
                              <tr className="text-white/70">
                                {cols.map((c, i) => (
                                  <th key={i} className="text-left font-medium py-1 pr-4">{c}</th>
                                ))}
                              </tr>
                            </thead>
                            <tbody className="text-white/90">
                              {normRows.slice(0, 20).map((r, ri) => (
                                <tr key={ri} className="border-t border-white/5">
                                  {r.map((cell: any, ci: number) => (
                                    <td key={ci} className="py-1 pr-4 whitespace-nowrap">{String(cell)}</td>
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
    </div>
  );
}
