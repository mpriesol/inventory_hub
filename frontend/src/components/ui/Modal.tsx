import React, { useEffect } from "react";

type ModalProps = {
  open: boolean;
  onClose: () => void;
  title?: string;
  dismissOnEsc?: boolean;
  dismissOnBackdrop?: boolean;
  align?: "top" | "center"; // NEW: control vertical positioning
  children: React.ReactNode;
};

export function Modal({
  open,
  onClose,
  title,
  dismissOnEsc = true,
  dismissOnBackdrop = true,
  align = "top",
  children,
}: ModalProps) {
  useEffect(() => {
    if (!open || !dismissOnEsc) return;
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, dismissOnEsc, onClose]);

  if (!open) return null;

  if (align === "center") {
    return (
      <div className="fixed inset-0 z-50 flex items-center justify-center">
        <div
          className="absolute inset-0 bg-black/60"
          onClick={() => dismissOnBackdrop && onClose()}
          aria-hidden
        />
        <div className="relative z-10 w-full max-w-3xl rounded-2xl border border-white/10 bg-slate-900 shadow-xl">
          <div className="flex items-center justify-between px-4 py-3 border-b border-white/10">
            <h3 className="text-sm font-semibold text-white/90">{title}</h3>
            <button
              onClick={onClose}
              className="rounded px-2 py-1 text-white/60 hover:text-white"
              aria-label="Close modal"
            >
              ✕
            </button>
          </div>
          <div className="p-4">{children}</div>
        </div>
      </div>
    );
  }

  // align === "top" (match Supplier/Shop modals visual placement)
  return (
    <div className="fixed inset-0 z-50">
      <div
        className="absolute inset-0 bg-black/60"
        onClick={() => dismissOnBackdrop && onClose()}
        aria-hidden
      />
      <div className="absolute inset-x-0 top-10 mx-auto w-full max-w-5xl overflow-hidden rounded-2xl border border-white/10 bg-slate-950 shadow-2xl">
        <div className="flex items-center justify-between px-4 py-3 border-b border-white/10">
          <h3 className="text-sm font-semibold text-white/90">{title}</h3>
          <button
            onClick={onClose}
            className="rounded px-2 py-1 text-white/60 hover:text-white"
            aria-label="Close modal"
          >
            ✕
          </button>
        </div>
        <div className="p-4">{children}</div>
      </div>
    </div>
  );
}
