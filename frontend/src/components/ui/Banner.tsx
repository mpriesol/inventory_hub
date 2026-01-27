import React from "react";

export function DangerBanner({ title, details }: { title: string; details?: string }) {
  return (
    <div className="rounded-xl border border-rose-300/30 bg-rose-900/30 p-3 text-rose-50 flex items-start gap-3">
      <div className="mt-0.5">⚠️</div>
      <div>
        <div className="font-semibold">{title}</div>
        {details && <pre className="mt-1 text-xs text-rose-100/90 whitespace-pre-wrap break-words">{details}</pre>}
      </div>
    </div>
  );
}

export function InfoBanner({ title, details }: { title: string; details?: string }) {
  return (
    <div className="rounded-xl border border-sky-300/20 bg-sky-900/20 p-3 text-sky-50">
      <div className="font-medium">{title}</div>
      {details && <div className="text-xs mt-1 text-sky-100/90">{details}</div>}
    </div>
  );
}
