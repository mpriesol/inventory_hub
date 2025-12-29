import React, { useEffect, useState } from "react";
import ReactDOM from "react-dom";
import { Badge } from "./ui/Badge";

type Props = {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  supplier: string;
  shop: string;
  children: React.ReactNode;
  title?: string;
};

export function ConfigModal({ open, onOpenChange, supplier, shop, children, title = "Configuration" }: Props) {
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onOpenChange(false);
    };
    if (open) window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onOpenChange]);

  if (!mounted) return null;
  return ReactDOM.createPortal(
    <div
      aria-hidden={!open}
      className={`fixed inset-0 z-[999] ${open ? "pointer-events-auto" : "pointer-events-none"} `}
      onMouseDown={() => onOpenChange(false)}
    >
      <div className={`absolute inset-0 bg-black/60 backdrop-blur-sm ${open ? "opacity-100" : "opacity-0"} transition`} />
      <div
        role="dialog"
        aria-modal="true"
        onMouseDown={(e) => e.stopPropagation()}
        className={`absolute left-1/2 top-8 w-[95vw] max-w-5xl -translate-x-1/2 rounded-2xl border border-neutral-800 bg-neutral-950 shadow-2xl transition ${
          open ? "opacity-100 translate-y-0" : "opacity-0 -translate-y-2"
        }`}
      >
        <div className="px-6 pt-5 pb-3 border-b border-neutral-900">
          <div className="flex items-center justify-between">
            <h2 className="text-lg font-semibold">{title}</h2>
            <span className="text-xs text-neutral-400">Floating over the app — ESC to close</span>
          </div>
          <div className="mt-2 flex gap-2">
            <Badge>Supplier: {supplier}</Badge>
            <Badge>Shop: {shop}</Badge>
          </div>
        </div>
        <div className="p-6">{children}</div>
      </div>
    </div>,
    document.body
  );
}
