import * as React from "react";

type Props = {
  open: boolean;
  onClose: () => void;
  items: any[];
};

export default function HistoryModal({ open, onClose, items }: Props) {
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-[220]">
      <div className="absolute inset-0 bg-black/60" onClick={onClose} />
      <div className="absolute left-1/2 top-10 -translate-x-1/2 w-[820px] max-w-[96vw] rounded-2xl bg-neutral-900 text-neutral-100 border border-neutral-700 shadow-2xl">
        <div className="flex items-center justify-between px-4 py-3 border-b border-neutral-700">
          <div className="font-semibold">Invoice History</div>
          <button className="px-3 py-1.5 rounded-xl border border-neutral-700 hover:bg-neutral-800" onClick={onClose}>
            Close
          </button>
        </div>

        <div className="p-4 max-h-[70vh] overflow-auto">
          {(!items || !items.length) && (
            <div className="text-sm text-neutral-400">No history yet.</div>
          )}
          <ul className="space-y-3">
            {items?.map((it, idx) => (
              <li key={idx} className="rounded-xl border border-neutral-800 p-3">
                <div className="flex items-center gap-2">
                  <span className={"text-xs px-2 py-0.5 rounded-full " + (it?.type === "apply" ? "bg-sky-900/40 text-sky-200" : "bg-emerald-900/40 text-emerald-200")}>
                    {String(it?.type || "").toUpperCase()}
                  </span>
                  <span className="text-sm opacity-80">{it?.timestamp}</span>
                </div>
                <div className="mt-2 grid grid-cols-2 gap-2 text-sm">
                  {it?.source_file && <div><span className="opacity-60">Source: </span><code className="break-all">{it.source_file}</code></div>}
                  {it?.output_file && <div><span className="opacity-60">Output: </span><code className="break-all">{it.output_file}</code></div>}
                  {it?.sent_file && <div><span className="opacity-60">Sent: </span><code className="break-all">{it.sent_file}</code></div>}
                  {it?._path && <div><span className="opacity-60">Log: </span><code className="break-all">{it._path}</code></div>}
                </div>
                {typeof it?.selected_count === "number" && (
                  <div className="mt-2 text-xs opacity-80">Selected rows: {it.selected_count}</div>
                )}
                {Array.isArray(it?.added_columns) && it.added_columns.length > 0 && (
                  <div className="mt-1 text-xs opacity-80">Added columns: {it.added_columns.join(", ")}</div>
                )}
              </li>
            ))}
          </ul>
        </div>
      </div>
    </div>
  );
}
