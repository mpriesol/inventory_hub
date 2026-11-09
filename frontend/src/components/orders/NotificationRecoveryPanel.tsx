import React, { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { accessRevision, hubAccessToken, hubUnlocked, hubUser } from '../../api/access';
import {
  applyNotificationResolution, getNotificationResolution, getNotificationResolutionHistory,
  isNotificationResolutionBinding, NotificationResolutionAction, NotificationResolutionBinding,
  NotificationResolutionOperation, OrderWebhookEvent, previewNotificationResolution,
} from '../../api/orderWebhooks';
import { ActionScope } from '../ui/ActionScope';
import { Button } from '../ui/Button.new';

const actor = () => !hubAccessToken() && hubUser()?.id != null ? `user:${hubUser()!.id}` : 'operator-token';
const storageKey = (shop: string, order: string) => `hub.order-webhook-notification.${shop}.${encodeURIComponent(order)}`;
function readPending(shop: string, order: string) {
  try {
    const raw = window.sessionStorage.getItem(storageKey(shop, order));
    if (raw === null) return { pending: null, storageError: false };
    const value: unknown = JSON.parse(raw);
    if (!isNotificationResolutionBinding(value) || value.shop_code !== shop || value.order_number !== order) throw new Error('Invalid retained request');
    return { pending: value, storageError: false };
  } catch { return { pending: null, storageError: true }; }
}
function availableAction(event: OrderWebhookEvent): NotificationResolutionAction | null {
  if (event.event !== 'delete' || !Number.isSafeInteger(event.generation) || event.generation < 1
      || !/^[0-9a-f]{64}$/.test(event.notification_hash || '')) return null;
  if (event.status === 'review' && event.last_error === 'order_webhook_order_not_found') return 'acknowledge_absent_delete';
  if (event.status === 'completed' && event.resolution?.action === 'acknowledge_absent_delete'
      && event.resolution.event_generation === event.generation) return 'reopen';
  return null;
}

/** An audited notification decision only. The server owns absence/history checks and every state transition. */
export function NotificationRecoveryPanel({ shop, event, disabled, onLockChange, onChange }: {
  shop: string; event: OrderWebhookEvent; disabled: boolean;
  onLockChange: (order: string, locked: boolean) => void; onChange: () => Promise<void>;
}) {
  const { t, i18n } = useTranslation();
  const [retained] = useState(() => readPending(shop, event.order_number));
  const [pending, setPending] = useState<NotificationResolutionBinding | null>(retained.pending);
  const [storageError, setStorageError] = useState(retained.storageError);
  const [operation, setOperation] = useState<NotificationResolutionOperation | null>(null);
  const [history, setHistory] = useState<NotificationResolutionOperation[] | null>(null);
  const [note, setNote] = useState(''), [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [saved, setSaved] = useState(false);
  const working = useRef(false), generation = useRef(0), controller = useRef<AbortController | null>(null);
  const mounted = useRef(true);
  const text = (key: string) => t(`orderWebhooks.notifications.${key}`);
  const date = (value: string) => new Date(value).toLocaleString(i18n.language);
  const action = availableAction(event);
  const actorChanged = !!pending && pending.actor !== actor();
  const locked = disabled || busy || storageError || actorChanged;
  const validNote = note.trim().length >= 10 && note.length <= 2000 && !/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/.test(note);
  const eventMatches = !pending || (pending.event_generation === event.generation && pending.notification_hash === event.notification_hash);

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; generation.current++; controller.current?.abort(); onLockChange(event.order_number, false); };
  }, [shop, event.order_number, onLockChange]);
  useEffect(() => { onLockChange(event.order_number, busy || !!pending || storageError); }, [busy, pending, storageError, event.order_number, onLockChange]);

  function retain(value: NotificationResolutionBinding | null) {
    if (value) {
      const serialized = JSON.stringify(value);
      window.sessionStorage.setItem(storageKey(shop, event.order_number), serialized);
      if (window.sessionStorage.getItem(storageKey(shop, event.order_number)) !== serialized) throw new Error('Storage unavailable');
    } else {
      try { window.sessionStorage.removeItem(storageKey(shop, event.order_number)); }
      catch { /* A completed or discarded prepared UUID can still be read safely after reload. */ }
    }
    setPending(value);
  }
  function keep(value: NotificationResolutionBinding) {
    try { retain(value); return true; }
    catch { setStorageError(true); setError(text('storageUnavailable')); return false; }
  }
  function explain(reason: unknown) {
    const code = (reason as { code?: string })?.code;
    return code ? t(`orderWebhooks.errors.${code}`, { defaultValue: text('failed') }) : text('failed');
  }
  async function accept(value: NotificationResolutionOperation, binding: NotificationResolutionBinding) {
    if (value.phase === 'completed') {
      retain(null); setOperation(null); setConfirmed(false); setNote(''); setSaved(true); setError('');
      setHistory(previous => previous ? [value, ...previous.filter(item => item.request_id !== value.request_id)].slice(0, 50) : null);
      // The decision is already verified. Refreshing the list cannot send it a second time.
      await onChange();
    } else if (keep({ ...binding, preview_hash: value.preview_hash })) {
      setOperation(value); setConfirmed(false);
    }
  }
  async function run(kind: 'preview' | 'apply' | 'recover' | 'history') {
    if (working.current || locked || !hubUnlocked() || hubUser()?.role === 'operator') return;
    let binding = pending;
    if (kind === 'preview') {
      if (pending || !action || !validNote) return;
      binding = { request_id: crypto.randomUUID(), shop_code: shop, order_number: event.order_number,
        action, actor: actor(), note: note.trim(), preview_hash: null,
        event_generation: event.generation, notification_hash: event.notification_hash };
      if (!keep(binding)) return;
    } else if (kind !== 'history') {
      if (!binding || binding.actor !== actor() || binding.shop_code !== shop || binding.order_number !== event.order_number) return;
      if (kind === 'apply' && (!operation || operation.phase !== 'prepared' || !binding.preview_hash || !confirmed || !eventMatches)) return;
      if (!keep(binding)) return;
    }
    working.current = true; setBusy(true); setError(''); setSaved(false);
    const context = generation.current, access = accessRevision(), abort = new AbortController(); controller.current = abort;
    const current = () => mounted.current && context === generation.current && access === accessRevision() && !abort.signal.aborted;
    try {
      if (kind === 'history') {
        const result = await getNotificationResolutionHistory(shop, event.order_number, abort.signal);
        if (current()) setHistory(result);
      } else {
        const result = await (kind === 'preview' ? previewNotificationResolution(binding!)
          : kind === 'apply' ? applyNotificationResolution(binding!) : getNotificationResolution(binding!, abort.signal));
        if (current()) await accept(result, binding!);
      }
    } catch (reason) {
      if (current()) {
        setError(explain(reason));
        if (kind !== 'history') {
          setOperation(null); setConfirmed(false);
          // A rejected preview never applies a decision. Unknown/malformed replies keep the original UUID.
          const failure = reason as { status?: number };
          if (kind === 'preview' && [400, 401, 403, 404, 409, 422].includes(failure.status || 0)) retain(null);
          if (kind === 'recover' && failure.status === 404 && binding?.preview_hash === null) retain(null);
        }
      }
    } finally {
      working.current = false;
      if (current()) setBusy(false);
    }
  }
  const discard = () => {
    if (locked || !operation || operation.phase !== 'prepared') return;
    retain(null); setOperation(null); setConfirmed(false); setError('');
  };

  // A newer create/update must not hide recovery of an older retained DELETE decision.
  if (event.event !== 'delete' && !pending && !storageError) return null;
  return <tr><td colSpan={5}><details className="opening-stock-notice" data-testid="order-notification-panel" data-order-number={event.order_number}>
    <summary>{text('title')}</summary><p>{text('scope')}</p>
    {storageError && <p role="alert">{text('storageUnavailable')}</p>}
    {actorChanged && <p role="alert">{text('actorChanged')}</p>}
    {error && <p role="alert" data-testid="order-notification-error">{error}</p>}
    {saved && <p role="status" data-testid="order-notification-saved">{text('saved')}</p>}
    {pending && !operation && <div data-testid="order-notification-pending"><p>{text('pending')}</p>
      <p>{text('requestId')}: {pending.request_id}</p>
      <span className="action-control"><Button data-testid="order-notification-recover" variant="secondary" disabled={locked}
        onClick={() => run('recover')}>{text('recover')}</Button><ActionScope effects={['hub-read']} /></span>
    </div>}
    {action && !pending && <form onSubmit={submit => { submit.preventDefault(); void run('preview'); }}>
      <p><strong>{text(`actions.${action}`)}</strong></p><p>{text(`help.${action}`)}</p>
      <label>{text('note')}<textarea data-testid="order-notification-note" rows={2} minLength={10} maxLength={2000} required
        disabled={locked} value={note} onChange={change => { setNote(change.target.value); setSaved(false); }} /></label>
      <span className="action-control"><Button type="submit" data-testid="order-notification-preview" disabled={locked || !validNote}>{text('preview')}</Button>
        <ActionScope effects={action === 'reopen' ? ['hub-write'] : ['hub-write', 'upgates-read']} shop={shop} calls={{ kind: 'variable' }} /></span>
    </form>}
    {operation && <div data-testid="order-notification-preview-result">
      <h4>{text('evidenceTitle')}</h4><p><strong>{text(`actions.${operation.action}`)}</strong></p>
      <p>{text('state')}: {t(`orderWebhooks.states.${operation.event.status}`)} → {t(`orderWebhooks.states.${operation.after_status}`)}</p>
      <p>{text('note')}: {operation.note}</p><p>{text('checkedAt')}: {date(operation.evidence.checked_at)}</p>
      {operation.action === 'acknowledge_absent_delete' && <ul><li>{text('activeAbsent')}</li><li>{text('deletedAbsent')}</li><li>{text('noLocalHistory')}</li></ul>}
      <p>{text('requestId')}: {operation.request_id}</p>
      <div className="opening-stock-confirmations"><label><input data-testid="order-notification-confirmation" type="checkbox"
        disabled={locked || !eventMatches} checked={confirmed} onChange={change => setConfirmed(change.target.checked)} />{text('confirmation')}</label></div>
      <div className="opening-stock-actions"><Button data-testid="order-notification-cancel" variant="secondary" disabled={locked} onClick={discard}>{text('cancel')}</Button>
        <span className="action-control"><Button data-testid="order-notification-confirm" disabled={locked || !confirmed || !eventMatches} onClick={() => run('apply')}>{text('confirm')}</Button>
          <ActionScope effects={operation.action === 'reopen' ? ['hub-write'] : ['hub-write', 'upgates-read']} shop={shop} calls={{ kind: 'variable' }} /></span>
      </div>
    </div>}
    <div className="opening-stock-actions"><span className="action-control"><Button data-testid="order-notification-history" variant="secondary"
      disabled={locked} onClick={() => run('history')}>{text('loadHistory')}</Button><ActionScope effects={['hub-read']} /></span></div>
    {history && <section data-testid="order-notification-history-result"><h4>{text('history')}</h4>
      {history.length === 0 ? <p>{text('historyEmpty')}</p> : <div className="opening-stock-table-scroll"><table>
        <thead><tr>{['createdAt', 'action', 'note', 'actor', 'state'].map(key => <th key={key}>{text(key)}</th>)}</tr></thead>
        <tbody>{history.map(item => <tr key={item.request_id}><td>{date(item.result!.applied_at)}</td><td>{text(`actions.${item.action}`)}</td>
          <td>{item.note}</td><td>{item.actor}</td><td>{text(`phases.${item.phase}`)}</td></tr>)}</tbody>
      </table></div>}
    </section>}
  </details></td></tr>;
}
