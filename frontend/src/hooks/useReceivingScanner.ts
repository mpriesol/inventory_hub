import { useEffect, useRef, useState } from 'react';
import { ApiResponseError } from '../api/client';
import { scanCode, type ScanResult } from '../api/receiving';

export type ReceivingScanCommand = { request_id: string; code: string; qty: number; line_id?: number; supplier?: string; invoice_no?: string };
export type ScanInvoiceChoice = { supplier: string; invoice_no: string; line_id?: number };
type InvoiceChoiceCandidate = { supplier: string; invoice_no: string; lines: { id: number | null }[]; [key: string]: unknown };
type InvoiceChoice = { code: string; errorCode: string; candidates: InvoiceChoiceCandidate[] };
const validCandidate = (value: unknown): value is InvoiceChoiceCandidate => {
  const candidate = value as InvoiceChoiceCandidate;
  return !!candidate && typeof candidate.supplier === 'string' && typeof candidate.invoice_no === 'string'
    && Array.isArray(candidate.lines) && candidate.lines.every(line => !!line && (line.id === null || Number.isSafeInteger(line.id)));
};
type ScanContext = { key: string; queue: ReceivingScanCommand[]; sending: boolean; stopped: boolean; ambiguous?: { code: string; line_ids: number[] } | null; choice?: InvoiceChoice | null };
const restore = (key: string): ReceivingScanCommand[] => {
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
const snapshot = (context: ScanContext) => ({ context, pending: context.queue.length, busy: context.sending, uncertain: context.stopped && !context.ambiguous && !context.choice, ambiguous: context.ambiguous || null, choice: context.choice || null });

/** One UUID per physical scan, sequential delivery, identical payload on retry. */
export function useReceivingScanner<Result = ScanResult>(supplier: string, sessionId: string,
  accept: (result: Result, code: string) => void | Promise<void>,
  reject: (message: string, error?: unknown, command?: ReceivingScanCommand) => void,
  transport?: { key: string; send: (command: ReceivingScanCommand) => Promise<Result> }) {
  const key = transport?.key || `receiving-scans:${supplier}:${sessionId}`;
  const callbacks = useRef({ accept, reject, transport }); callbacks.current = { accept, reject, transport };
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
          const result = callbacks.current.transport
            ? await callbacks.current.transport.send(command)
            : await scanCode(supplier, sessionId, command.code, command.qty, command.request_id, command.line_id) as Result;
          acknowledged = true;
          if (!current(context)) return;
          await callbacks.current.accept(result, command.code);
          if (!current(context)) return;
          context.queue.shift(); persist(context); reflect(context);
        } catch (error) {
          if (!current(context)) return;
          const message = error instanceof Error ? error.message : '';
          const detail = error instanceof ApiResponseError ? error.detail as { code?: string; line_ids?: number[]; candidates?: unknown[] } : null;
          if (!acknowledged && error instanceof ApiResponseError && error.status === 409
            && ['mixed_receiving_invoice_choice', 'mixed_receiving_line_choice'].includes(detail?.code || '')
            && Array.isArray(detail?.candidates) && detail.candidates.length && detail.candidates.every(validCandidate)) {
            context.choice = { code: command.code, errorCode: detail.code!, candidates: detail.candidates };
            context.stopped = true; reflect(context); break;
          }
          if (!acknowledged && error instanceof ApiResponseError && error.status === 409 && detail?.code === 'scan_line_ambiguous' && Array.isArray(detail.line_ids) && detail.line_ids.length > 1 && detail.line_ids.every(id => Number.isSafeInteger(id) && id > 0)) {
            context.ambiguous = { code: command.code, line_ids: detail.line_ids };
            context.stopped = true; reflect(context); break;
          }
          // A known client rejection did not mutate the draft. A malformed 200,
          // unknown outcome or failed replay refresh retains the exact request.
          const rejected = !acknowledged && /^4\d\d\b/.test(message) && !/^(408|429)\b/.test(message);
          if (rejected) { context.queue.shift(); persist(context); }
          else context.stopped = true;
          reflect(context);
          callbacks.current.reject(rejected ? (message.includes('scan_line_ambiguous') ? 'ambiguous' : 'failed') : 'uncertain', error, command);
        }
      }
    } finally {
      context.sending = false; reflect(context);
    }
  }
  function enqueue(code: string, qty: number, requestId?: string) {
    if (!sessionId || !current(context) || !code.trim() || !Number.isFinite(qty) || qty <= 0) return;
    if (requestId && context.queue.some(command => command.request_id === requestId)) return;
    context.queue.push({ request_id: requestId || crypto.randomUUID(), code: code.trim(), qty });
    persist(context); reflect(context); void drain();
  }
  function retry() {
    if (context.sending || context.ambiguous || context.choice || !context.queue.length || !current(context)) return;
    context.stopped = false; reflect(context); void drain();
  }
  function resolveAmbiguity(lineId: number | null) {
    if (!current(context) || context.sending || !context.ambiguous || !context.queue.length) return;
    if (lineId !== null && !context.ambiguous.line_ids.includes(lineId)) return;
    // 409 proves this command was rejected before any write. Only now may its
    // payload gain the explicit line selection; unknown requests stay immutable.
    if (lineId === null) context.queue.shift();
    else context.queue[0] = { ...context.queue[0], line_id: lineId };
    context.ambiguous = null; context.stopped = false; persist(context); reflect(context); void drain();
  }
  function resolveChoice(selection: ScanInvoiceChoice | null) {
    if (!current(context) || context.sending || !context.choice || !context.queue.length) return;
    if (selection && !context.choice.candidates.some(candidate => candidate.supplier === selection.supplier
      && candidate.invoice_no === selection.invoice_no && (selection.line_id === undefined || candidate.lines.some(line => line.id === selection.line_id)))) return;
    // Only an explicit 409 rejection permits changing the original scan payload.
    if (selection === null) context.queue.shift();
    else {
      const { line_id: previousLine, ...command } = context.queue[0];
      context.queue[0] = { ...command, ...selection };
    }
    context.choice = null; context.stopped = false; persist(context); reflect(context); void drain();
  }
  const visible = view.context === context ? view : snapshot(context);
  return { enqueue, retry, resolveAmbiguity, resolveChoice, choice: visible.choice, ambiguous: visible.ambiguous, pending: visible.pending, busy: visible.busy, uncertain: visible.uncertain,
    hasPending: () => context.queue.length > 0 };
}
