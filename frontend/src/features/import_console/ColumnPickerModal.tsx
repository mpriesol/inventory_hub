import React from "react";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Button, OutlineButton } from "@/components/ui/button";
import { Label } from "@/components/ui/Label";
import { getShopConfig, putShopConfig } from "@/api/import_console";

type Props = {
  open: boolean;
  onClose: () => void;
  allColumns: string[];
  selected: string[]; // poradie sa zachová
  onChangeSelected: (cols: string[]) => void;
  tabKey: "updates" | "new" | "unmatched";
  shop: string;
};

const norm = (s: string) => s?.replace(/^\[|\]$/g, "").trim();

export default function ColumnPickerModal({
  open, onClose, allColumns, selected, onChangeSelected, tabKey, shop,
}: Props) {
  const [filter, setFilter] = React.useState("");
  const [localSel, setLocalSel] = React.useState<string[]>(selected);

  React.useEffect(() => {
    setLocalSel(selected);
    setFilter("");
  }, [open, selected]);

  // zoznam dostupných (okrem vybraných), rešpektuje filter
  const selSet = new Set(localSel.map(norm));
  const available = allColumns
    .filter(c => !selSet.has(norm(c)))
    .filter(c => !filter || c.toLowerCase().includes(filter.toLowerCase()));

  // vybrané (v poradí), rešpektuje filter iba na zobrazenie
  const visibleSel = localSel.filter(c =>
    !filter || c.toLowerCase().includes(filter.toLowerCase())
  );

  const add = (c: string) => setLocalSel(s => [...s, c]);
  const remove = (c: string) => setLocalSel(s => s.filter(x => x !== c));
  const move = (idx: number, dir: -1 | 1) => {
    setLocalSel(s => {
      const arr = [...s];
      const j = idx + dir;
      if (idx < 0 || j < 0 || idx >= arr.length || j >= arr.length) return arr;
      [arr[idx], arr[j]] = [arr[j], arr[idx]];
      return arr;
    });
  };

  const addAll = () => setLocalSel(s => {
    const set = new Set(s.map(norm));
    const extra = allColumns.filter(c => !set.has(norm(c)));
    return [...s, ...extra];
  });
  const clearAll = () => setLocalSel([]);

  const saveDefault = async () => {
    const cfg = await getShopConfig(shop);
    const next = { ...(cfg || {}) };
    next.console = next.console || {};
    next.console.import_console = next.console.import_console || {};
    next.console.import_console.columns = next.console.import_console.columns || {};
    next.console.import_console.columns[tabKey] = localSel;
    await putShopConfig(shop, next);
    onChangeSelected(localSel);
    onClose();
  };

  const applyOnce = () => {
    onChangeSelected(localSel);
    onClose();
  };

  return (
    <Dialog open={open} onOpenChange={(v) => (!v ? onClose() : null)}>
      <DialogContent className="max-w-4xl bg-neutral-900 text-neutral-100">
        <DialogHeader>
          <DialogTitle className="text-neutral-100">Columns for {tabKey}</DialogTitle>
        </DialogHeader>

        {/* Filter */}
        <div className="mb-3">
          <Label className="text-neutral-300">Filter</Label>
          <input
            className="w-full mt-1 rounded-xl px-3 py-2 bg-neutral-900 text-neutral-100 border border-neutral-700 placeholder:text-neutral-500 focus:outline-none focus:ring-1 focus:ring-neutral-500"
            placeholder="Search column…"
            value={filter}
            onChange={e=>setFilter(e.target.value)}
          />
        </div>

        {/* Two-pane picker */}
        <div className="grid grid-cols-2 gap-4">
          {/* Left: selected (ordered) */}
          <div className="rounded-xl border border-neutral-800">
            <div className="flex items-center justify-between px-3 py-2 border-b border-neutral-800">
              <div className="text-sm text-neutral-300">Selected (order)</div>
              <div className="flex items-center gap-2">
                <OutlineButton onClick={clearAll} className="px-2 py-1 text-xs">Clear all</OutlineButton>
              </div>
            </div>

            <ul className="max-h-[50vh] overflow-auto p-2">
              {visibleSel.length === 0 && (
                <li className="px-2 py-1 text-sm text-neutral-500">Nothing selected.</li>
              )}
              {visibleSel.map((c) => {
                const idx = localSel.indexOf(c); // index v skutočnom poradi
                return (
                  <li
                    key={c}
                    className="flex items-center gap-2 px-2 py-1 rounded-lg hover:bg-neutral-800"
                    title={c}
                  >
                    <span className="cursor-grab select-none text-neutral-500">⋮⋮</span>
                    <span className="truncate flex-1">{c}</span>
                    <button
                      className="px-2 py-1 rounded border border-neutral-700 hover:bg-neutral-800 text-xs"
                      onClick={() => move(idx, -1)}
                      disabled={idx <= 0}
                      title="Move up"
                    >↑</button>
                    <button
                      className="px-2 py-1 rounded border border-neutral-700 hover:bg-neutral-800 text-xs"
                      onClick={() => move(idx, +1)}
                      disabled={idx >= localSel.length - 1}
                      title="Move down"
                    >↓</button>
                    <button
                      className="px-2 py-1 rounded border border-neutral-700 hover:bg-neutral-800 text-xs"
                      onClick={() => remove(c)}
                      title="Remove"
                    >×</button>
                  </li>
                );
              })}
            </ul>
          </div>

          {/* Right: available */}
          <div className="rounded-xl border border-neutral-800">
            <div className="flex items-center justify-between px-3 py-2 border-b border-neutral-800">
              <div className="text-sm text-neutral-300">Available</div>
              <div className="flex items-center gap-2">
                <OutlineButton onClick={addAll} className="px-2 py-1 text-xs">Add all</OutlineButton>
              </div>
            </div>

            <ul className="max-h-[50vh] overflow-auto p-2">
              {available.length === 0 && (
                <li className="px-2 py-1 text-sm text-neutral-500">No columns match.</li>
              )}
              {available.map(c => (
                <li
                  key={c}
                  className="flex items-center gap-2 px-2 py-1 rounded-lg hover:bg-neutral-800 cursor-pointer"
                  onClick={() => add(c)}
                  title={c}
                >
                  <span className="px-2 py-0.5 text-xs border border-neutral-700 rounded">+</span>
                  <span className="truncate">{c}</span>
                </li>
              ))}
            </ul>
          </div>
        </div>

        {/* Actions */}
        <div className="flex justify-between mt-4">
          <OutlineButton onClick={applyOnce}>Apply</OutlineButton>
          <Button onClick={saveDefault}>Save as default</Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
