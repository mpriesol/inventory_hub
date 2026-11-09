import React, { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { accessRevision, hubUser } from '../api/access';
import type { ReceivingStatus } from '../api/receiving';
import { applyReceivingOperation, previewReceivingOperation, getReceivingOperationHistory, type ReceivingOperationAction,
  type ReceivingOperationRequest, type ReceivingOperationConfirmation, type ReceivingOperationReview,
  type ReceivingOperationResult, type ReceivingOperationHistoryEntry } from '../api/receivingOperations';

type Pending = { supplier: string; session: string; actor: string; id: string } & (
  { phase: 'preview'; body: ReceivingOperationRequest } | { phase: 'apply'; body: ReceivingOperationConfirmation });
const actor = () => String(hubUser()?.id ?? hubUser()?.username ?? 'operator-token');
const storageKey = (supplier: string, session: string) => `hub.receiving-operation.${supplier}.${session}`;
function readPending(supplier: string, session: string): Pending | null {
  try {
    const value = JSON.parse(sessionStorage.getItem(storageKey(supplier, session)) || 'null');
    return value?.supplier === supplier && value.session === session && value.actor === actor()
      && /^[0-9a-f-]{36}$/i.test(value.id || '') && ['preview', 'apply'].includes(value.phase) && value.body ? value : null;
  } catch { return null; }
}

export function ReceivingRecoveryPanel({ supplier, sessionId, status, archived, reversed, disabled, lastOperation,
  onChanged, onLockChange }: { supplier: string; sessionId: string; status: ReceivingStatus;
  archived: boolean; reversed: boolean; disabled: boolean; onChanged: (result: ReceivingOperationResult) => Promise<void>;
  lastOperation?: { id: string; action: string; reason: string; actor: string; at: string };
  onLockChange: (locked: boolean) => void }) {
  const { t, i18n } = useTranslation();
  const actions: ReceivingOperationAction[] = archived ? ['restore'] : status === 'completed'
    ? ['reopen', 'reverse', 'archive'] : status === 'cancelled' && reversed ? ['reopen', 'archive'] : ['archive'];
  const [action, setAction] = useState(actions[0]);
  const [reason, setReason] = useState('');
  const [review, setReview] = useState<ReceivingOperationReview | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [pending, setPending] = useState<Pending | null>(() => readPending(supplier, sessionId));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [history, setHistory] = useState<ReceivingOperationHistoryEntry[]>([]);
  const [historyFailed, setHistoryFailed] = useState(false), [historyVersion, setHistoryVersion] = useState(0);
  const working = useRef(false), mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; onLockChange(false); }; }, [onLockChange]);
  useEffect(() => { onLockChange(busy || !!pending); }, [busy, pending, onLockChange]);
  useEffect(() => {
    const controller = new AbortController(), access = accessRevision();
    setHistoryFailed(false);
    getReceivingOperationHistory(supplier, sessionId, controller.signal).then(value => {
      if (!controller.signal.aborted && access === accessRevision()) setHistory(value.operations);
    }).catch(() => { if (!controller.signal.aborted && access === accessRevision()) setHistoryFailed(true); });
    return () => controller.abort();
  }, [supplier, sessionId, historyVersion, lastOperation?.id]);
  useEffect(() => {
    if (!actions.includes(action)) setAction(actions[0]);
    setReview(null); setConfirmed(false);
  }, [status, archived, reversed]);
  useEffect(() => {
    if (!pending && !busy) return;
    const guard = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ''; };
    window.addEventListener('beforeunload', guard);
    return () => window.removeEventListener('beforeunload', guard);
  }, [pending, busy]);
  const remember = (value: Pending) => {
    const serialized = JSON.stringify(value), key = storageKey(supplier, sessionId);
    sessionStorage.setItem(key, serialized);
    if (sessionStorage.getItem(key) !== serialized) throw new Error('storage_unavailable');
    setPending(value);
  };
  const clear = () => {
    setPending(null);
    try { sessionStorage.removeItem(storageKey(supplier, sessionId)); } catch { /* Replay remains safe if persistence cannot be cleared. */ }
  };
  const explain = (failure: unknown) => {
    const code = (failure as { code?: string })?.code;
    return code ? t(`receiving.recovery.errors.${code}`, { defaultValue: `${t('receiving.recovery.failed')} (${code})` }) : t('receiving.recovery.failed');
  };
  const finish = async (value: ReceivingOperationResult) => {
    clear(); setReview(null); setReason(''); setConfirmed(false); setMessage(t('receiving.recovery.applied'));
    setHistoryVersion(version => version + 1);
    try { await onChanged(value); } catch { if (mounted.current) setError(t('receiving.recovery.failed')); }
  };
  const send = async (command: Pending) => {
    if (working.current || disabled || command.actor !== actor()) return;
    working.current = true; setBusy(true); setError(''); setMessage('');
    const access = accessRevision();
    try {
      // Retain the exact UUID and body before sending; an unknown outcome cannot create a second operation.
      remember(command);
      if (command.phase === 'preview') {
        const value = await previewReceivingOperation(supplier, sessionId, command.body);
        if (!mounted.current || access !== accessRevision()) return;
        if (value.status === 'completed' && value.result) await finish(value.result);
        else { clear(); setReview(value); setConfirmed(false); }
      } else {
        const value = await applyReceivingOperation(supplier, sessionId, command.id, command.body);
        if (mounted.current && access === accessRevision()) await finish(value);
      }
    } catch (failure) {
      if (!mounted.current || access !== accessRevision()) return;
      const statusCode = (failure as { status?: number })?.status;
      if (statusCode && [400, 401, 403, 404, 409, 422].includes(statusCode)) { clear(); setReview(null); setConfirmed(false); }
      setError(explain(failure));
    } finally { working.current = false; if (mounted.current) setBusy(false); }
  };
  const startPreview = (event: React.FormEvent) => {
    event.preventDefault(); if (pending || !actions.includes(action) || reason.trim().length < 3) return;
    const id = crypto.randomUUID();
    void send({ supplier, session: sessionId, actor: actor(), id, phase: 'preview', body: { request_id: id, action, reason: reason.trim() } });
  };
  const locked = disabled || busy || !!pending;
  return <section className="rounded-xl border p-4 space-y-3" data-testid="receiving-recovery" style={{ borderColor: 'var(--color-border-subtle)' }}>
    <h2 className="font-semibold">{t('receiving.recovery.title')}</h2>
    {lastOperation && <p className="text-sm" data-testid="receiving-recovery-last-operation">{t('receiving.recovery.lastOperation')}: {t(`receiving.recovery.${lastOperation.action}`)} · {lastOperation.reason} · {lastOperation.actor} · {new Date(lastOperation.at).toLocaleString(i18n.language)}</p>}
    {archived && <p role="status" data-testid="receiving-archived">{t('receiving.recovery.archived')}</p>}
    {error && <p role="alert" data-testid="receiving-recovery-error">{error}</p>}
    {message && <p role="status">{message}</p>}
    {pending && <div role="status" data-testid="receiving-recovery-pending"><p>{t('receiving.recovery.pending')}</p>
      <button type="button" disabled={disabled || busy} onClick={() => void send(pending)} data-testid="receiving-recovery-retry">{t('receiving.recovery.retry')}</button></div>}
    <form onSubmit={startPreview}><fieldset disabled={locked || !!review} className="space-y-3">
      <label className="block">{t('receiving.recovery.action')} <select data-testid="receiving-recovery-action" value={action} onChange={event => setAction(event.target.value as ReceivingOperationAction)}>
        {actions.map(value => <option key={value} value={value}>{t(`receiving.recovery.${value}`)}</option>)}</select></label>
      <label className="block">{t('receiving.recovery.reason')}<textarea className="block w-full" required minLength={3} maxLength={1000} value={reason}
        placeholder={t('receiving.recovery.reasonPlaceholder')} data-testid="receiving-recovery-reason" onChange={event => setReason(event.target.value)} /></label>
      <p className="text-sm">{t(action === 'archive' || action === 'restore' ? 'receiving.recovery.noStockChange' : 'receiving.recovery.undoHelp')}</p>
      <button type="submit" disabled={reason.trim().length < 3} data-testid="receiving-recovery-preview">{t(busy ? 'receiving.recovery.checking' : 'receiving.recovery.preview')}</button>
    </fieldset></form>
    <details data-testid="receiving-recovery-history"><summary>{t('receiving.recovery.history')}</summary>
      <p className="text-sm">{t('receiving.recovery.historyLimit')}</p>
      {historyFailed ? <p role="alert">{t('receiving.recovery.historyLoadFailed')} <button type="button" onClick={() => setHistoryVersion(value => value + 1)}>{t('receiving.recovery.retry')}</button></p>
        : !history.length ? <p>{t('receiving.recovery.historyEmpty')}</p>
        : <div className="overflow-x-auto"><table className="w-full text-sm"><thead><tr>
          {['date', 'action', 'reason', 'actor', 'stockEffect'].map(label => <th key={label}>{t(`receiving.recovery.${label}`)}</th>)}
        </tr></thead><tbody>{history.map(item => <tr key={item.id} data-testid="receiving-recovery-history-row">
          <td>{new Date(item.at).toLocaleString(i18n.language)}</td><td>{t(`receiving.recovery.${item.action}`)}</td><td>{item.reason}</td><td>{item.actor}</td>
          <td>{item.stock_effect === 'unchanged' ? t('receiving.recovery.noStockChange') : item.impacts.map(impact => `${impact.sku}: ${impact.delta} ${impact.stock_unit}`).join('; ')}</td>
        </tr>)}</tbody></table></div>}
    </details>
    {review && <div className="space-y-3" data-testid="receiving-recovery-review">
      <p>{t(`receiving.recovery.${review.action}`)} · {review.invoice_number} · {review.warehouse.name}</p><p>{review.reason}</p>
      <p>{t(`receiving.sessionStatus.${review.receipt_status}`)} → {t(`receiving.sessionStatus.${review.after_status || review.receipt_status}`)}</p>
      {review.stock_effect === 'unchanged' && <p data-testid="receiving-recovery-no-stock">{t('receiving.recovery.noStockChange')}</p>}
      {!!review.impacts.length && <div className="overflow-x-auto"><table className="w-full text-sm"><thead><tr>
        <th>{t('receiving.recovery.product')}</th>{['before', 'change', 'after', 'availableAfter'].map(label => <th key={label}>{t(`receiving.recovery.${label}`)}</th>)}
      </tr></thead><tbody>{review.impacts.map(item => <tr key={item.product_id}><td>{item.sku} · {item.name}</td>
        {[item.before_quantity, item.delta, item.after_quantity, item.available_after].map((value, index) => <td key={index}>{value} {item.stock_unit}</td>)}
      </tr>)}</tbody></table></div>}
      {review.errors.map((item, index) => <p role="alert" key={index}>{t(`receiving.recovery.errors.${item.code}`, { defaultValue: `${t('receiving.recovery.blocked')} (${item.code})` })}{item.product_id ? ` · #${item.product_id}` : ''}</p>)}
      <div className="flex gap-3"><button disabled={locked} type="button" data-testid="receiving-recovery-new" onClick={() => { setReview(null); setConfirmed(false); }}>{t('receiving.recovery.newPreview')}</button>
        {review.ready && <><label><input type="checkbox" checked={confirmed} disabled={locked} onChange={event => setConfirmed(event.target.checked)} data-testid="receiving-recovery-confirm" /> {t('receiving.recovery.confirm')}</label>
          <button type="button" disabled={locked || !confirmed} data-testid="receiving-recovery-apply" onClick={() => void send({ supplier, session: sessionId, actor: actor(), id: review.id,
            phase: 'apply', body: { preview_hash: review.preview_hash, confirmed: true } })}>{t('receiving.recovery.apply')}</button></>}
      </div>
    </div>}
  </section>;
}
