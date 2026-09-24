import { useEffect, useRef, useState } from 'react';
import { scanCode, type ScanResult } from '../api/receiving';

type Scan = { request_id: string; code: string; qty: number };
type ScanContext = { key: string; queue: Scan[]; sending: boolean; stopped: boolean };
const restore = (key: string): Scan[] => {
  try {
    const value = JSON.parse(sessionStorage.getItem(key) || '[]');
    return Array.isArray(value) ? value.filter(row => row && /^[0-9a-f-]{36}$/i.test(row.request_id)
      && typeof row.code === 'string' && row.code.trim() && Number.isFinite(row.qty) && row.qty > 0) : [];
  } catch { return []; }
};
const persist = (context: ScanContext) => {
  try {
    if (context.queue.length) sessionStorage.setItem(context.key, JSON.stringify(context.queue));
    else sessionStorage.removeItem(context.key);
  } catch { /* In-memory retry remains available. */ }
};
const snapshot = (context: ScanContext) => ({ context, pending: context.queue.length, busy: context.sending, uncertain: context.stopped });

/** One UUID per physical scan, sequential delivery, identical payload on retry. */
export function useReceivingScanner(supplier: string, sessionId: string,
  accept: (result: ScanResult, code: string) => void | Promise<void>, reject: (message: string) => void) {
  const key = `receiving-scans:${supplier}:${sessionId}`;
  const callbacks = useRef({ accept, reject }); callbacks.current = { accept, reject };
  const scope = useRef<ScanContext | null>(null);
  // The sender belongs to one context object, not a global busy flag. A late
  // response for receipt A must never block or alter receipt B (or a new A).
  if (!scope.current || scope.current.key !== key) {
    const queue = restore(key);
    scope.current = { key, queue, sending: false, stopped: queue.length > 0 };
  }
  const context = scope.current;
  const mounted = useRef(true);
  const [view, setView] = useState(() => snapshot(context));
  const current = (value: ScanContext) => mounted.current && scope.current === value;
  const reflect = (value: ScanContext) => { if (current(value)) setView(snapshot(value)); };
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => { reflect(context); }, [context]);

  async function drain() {
    if (context.sending || context.stopped || !sessionId || !current(context)) return;
    context.sending = true; reflect(context);
    try {
      while (context.queue.length && !context.stopped && current(context)) {
        const command = context.queue[0];
        let acknowledged = false;
        try {
          const result = await scanCode(supplier, sessionId, command.code, command.qty, command.request_id);
          acknowledged = true;
          if (!current(context)) return;
          await callbacks.current.accept(result, command.code);
          if (!current(context)) return;
          context.queue.shift(); persist(context); reflect(context);
        } catch (error) {
          if (!current(context)) return;
          const message = error instanceof Error ? error.message : '';
          // A known client rejection did not mutate the draft. A malformed 200,
          // unknown outcome or failed replay refresh retains the exact request.
          const rejected = !acknowledged && /^4\d\d\b/.test(message) && !/^(408|429)\b/.test(message);
          if (rejected) { context.queue.shift(); persist(context); }
          else context.stopped = true;
          reflect(context);
          callbacks.current.reject(rejected ? (message.includes('scan_line_ambiguous') ? 'ambiguous' : 'failed') : 'uncertain');
        }
      }
    } finally {
      context.sending = false; reflect(context);
    }
  }
  function enqueue(code: string, qty: number) {
    if (!sessionId || !current(context) || !code.trim() || !Number.isFinite(qty) || qty <= 0) return;
    context.queue.push({ request_id: crypto.randomUUID(), code: code.trim(), qty });
    persist(context); reflect(context); void drain();
  }
  function retry() {
    if (context.sending || !context.queue.length || !current(context)) return;
    context.stopped = false; reflect(context); void drain();
  }
  const visible = view.context === context ? view : snapshot(context);
  return { enqueue, retry, pending: visible.pending, busy: visible.busy, uncertain: visible.uncertain,
    hasPending: () => context.queue.length > 0 };
}
