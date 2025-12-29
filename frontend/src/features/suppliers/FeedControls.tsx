import React from "react";
import { PrimaryButton } from "../../components/ui/button";

export function FeedControls({
  shop,
  onShopChange,
  feedSource,
  onFeedSourceChange,
  onRefresh,
}: {
  shop: string;
  onShopChange: (v: string) => void;
  feedSource: string;
  onFeedSourceChange: (v: string) => void;
  onRefresh: () => void;
}) {
  return (
    <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
      <div className="space-y-2">
        <label className="text-xs uppercase tracking-wide text-white/60">Shop</label>
        <div className="flex items-center gap-2">
          {["biketrek", "xtrek"].map((s) => (
            <button
              key={s}
              onClick={() => onShopChange(s)}
              className={
                "px-3 py-2 rounded-xl border text-sm transition " +
                (shop === s ? "bg-lime-500 text-black border-lime-400" : "bg-white/5 text-white/80 border-white/10 hover:bg-white/10")
              }
            >
              {s}
            </button>
          ))}
        </div>
      </div>

      <div className="space-y-2 md:col-span-2">
        <label className="text-xs uppercase tracking-wide text-white/60">Feed source (voliteľne – lokálny XML/URL)</label>
        <div className="flex gap-2">
          <input
            value={feedSource}
            onChange={(e) => onFeedSourceChange(e.target.value)}
            placeholder="napr. C:\\tmp\\export_v2.xml alebo http://..."
            className="w-full rounded-xl bg-white/5 border border-white/10 px-3 py-2 text-sm placeholder:text-white/40"
          />
          <PrimaryButton onClick={onRefresh}>Obnoviť feed</PrimaryButton>
        </div>
      </div>
    </div>
  );
}
