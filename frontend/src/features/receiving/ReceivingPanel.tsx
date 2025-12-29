import React from "react";
import { Button, OutlineButton } from "@/components/ui/button";
import { createReceivingSession, scanCode, getReceivingSummary, finalizeReceiving } from "@/api/receiving";

type Props = {
  supplier: string;
  invoiceId: string; // "supplier:INVNO" alebo "INVNO"
  onUseSelection?: (sel: { selected_product_codes: string[], edits: Record<string, Record<string,string>> }) => void;
};

export default function ReceivingPanel({ supplier, invoiceId, onUseSelection }: Props) {
  const [sessionId, setSessionId] = React.useState<string | null>(null);
  const [input, setInput] = React.useState("");
  const [qty, setQty] = React.useState<number>(1);
  const [lines, setLines] = React.useState<any[]>([]);
  const [summary, setSummary] = React.useState<any | null>(null);
  const [busy, setBusy] = React.useState(false);
  const scanRef = React.useRef<HTMLInputElement>(null);

  React.useEffect(() => {
    (async () => {
      setBusy(true);
      try {
        const r = await createReceivingSession(supplier, invoiceId);
        setSessionId(r.session_id);
        setLines(r.lines || []);
      } finally {
        setBusy(false);
      }
    })();
  }, [supplier, invoiceId]);

  const doScan = async () => {
    if (!sessionId || !input.trim()) return;
    setBusy(true);
    try {
      const r = await scanCode(supplier, sessionId, input.trim(), qty || 1);
      const s = await getReceivingSummary(supplier, sessionId);
      setLines(s.lines || []);
      setSummary(s.summary || null);
      setInput("");
      setQty(1);
      scanRef.current?.focus();
    } catch (e) {
      console.error(e);
    } finally {
      setBusy(false);
    }
  };

  const finalize = async () => {
    if (!sessionId) return;
    setBusy(true);
    try {
      const r = await finalizeReceiving(supplier, sessionId);
      if (onUseSelection) onUseSelection({ selected_product_codes: r.selected_product_codes, edits: r.edits });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="p-3 space-y-3">
      <div className="flex flex-wrap items-end gap-2">
        <div className="flex flex-col">
          <label className="text-xs opacity-70">Scan / EAN / SKU / Product code</label>
          <input
            ref={scanRef}
            value={input}
            onChange={(e)=>setInput(e.target.value)}
            onKeyDown={(e)=>{ if (e.key === "Enter") doScan(); }}
            className="w-72 px-3 py-2 rounded-xl border border-neutral-700 bg-neutral-900 text-neutral-100 focus:outline-none focus:ring-1 focus:ring-neutral-500"
            placeholder="Scan here…"
            autoFocus
          />
        </div>
        <div className="flex flex-col">
          <label className="text-xs opacity-70">Qty</label>
          <input
            type="number"
            value={qty}
            onChange={(e)=>setQty(Number(e.target.value || 1))}
            className="w-24 px-3 py-2 rounded-xl border border-neutral-700 bg-neutral-900 text-neutral-100 focus:outline-none focus:ring-1 focus:ring-neutral-500"
            min={-999}
            step={1}
          />
        </div>
        <Button onClick={doScan} disabled={busy || !input.trim()}>Scan</Button>
        <OutlineButton onClick={finalize} disabled={busy || !sessionId}>Use receiving selection</OutlineButton>

        {summary && (
          <div className="ml-auto text-xs opacity-70">
            Pending: {summary.pending} · Partial: {summary.partial} · Matched: {summary.matched} · Overage: {summary.overage} · Unexpected: {summary.unexpected}
          </div>
        )}
      </div>

      <div className="overflow-auto border border-neutral-800 rounded-2xl">
        <table className="min-w-full text-sm">
          <thead className="sticky top-0 bg-white dark:bg-neutral-900">
            <tr>
              <th className="p-2 text-left">EAN</th>
              <th className="p-2 text-left">SCM</th>
              <th className="p-2 text-left">Product code</th>
              <th className="p-2 text-left">Title</th>
              <th className="p-2 text-right">Ordered</th>
              <th className="p-2 text-right">Received</th>
              <th className="p-2 text-left">Status</th>
            </tr>
          </thead>
          <tbody>
            {lines.map((ln, i) => (
              <tr key={i} className="border-t border-neutral-200 dark:border-neutral-800">
                <td className="p-2">{ln.ean}</td>
                <td className="p-2">{ln.scm}</td>
                <td className="p-2">{ln.product_code}</td>
                <td className="p-2">{ln.title}</td>
                <td className="p-2 text-right">{ln.ordered_qty}</td>
                <td className="p-2 text-right">{ln.received_qty}</td>
                <td className="p-2">
                  <span className={
                    "px-2 py-0.5 rounded text-xs " + (
                      ln.status === "matched" ? "bg-emerald-900/40 text-emerald-200" :
                      ln.status === "partial" ? "bg-amber-900/40 text-amber-200" :
                      ln.status === "overage" ? "bg-rose-900/40 text-rose-200" :
                      "bg-neutral-800 text-neutral-200"
                    )
                  }>
                    {ln.status}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
