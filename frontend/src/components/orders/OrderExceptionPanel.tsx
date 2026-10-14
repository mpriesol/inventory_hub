import React, { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { accessRevision, hubUnlocked } from '../../api/access';
import { getOrderException, OrderExceptionResolution, OrderProcessingJob, previewOrderException, resolveOrderException } from '../../api/orderCollection';
import { Button } from '../ui/Button.new';
import { ActionScope } from '../ui/ActionScope';

const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const storageKey = (shop: string) => `order-exception-preview:${shop}`;
function savedId(shop: string) {
  try { const id = window.sessionStorage.getItem(storageKey(shop)) || ''; return uuidPattern.test(id) ? id : ''; } catch { return ''; }
}
function rememberId(shop: string, id: string) {
  try { if (id) window.sessionStorage.setItem(storageKey(shop), id); else window.sessionStorage.removeItem(storageKey(shop)); } catch { /* The visible ID still supports explicit recovery. */ }
}
function checkedPreview(value: OrderExceptionResolution, shop: string, id: string) {
  if (!value || value.id !== id || value.shop_code !== shop || !['prepared', 'completed'].includes(value.status)
      || typeof value.preview_hash !== 'string' || !value.preview_hash || !value.warehouse || !Array.isArray(value.lines)
      || typeof value.warehouse.name !== 'string' || typeof value.warehouse.code !== 'string'
      || !Number.isFinite(Date.parse(value.expires_at)) || !Number.isFinite(Date.parse(value.issued_at))
      || typeof value.order_number !== 'string' || (value.source_status_name !== null && typeof value.source_status_name !== 'string')
      || value.lines.some(line => !line || typeof line.sku !== 'string' || !Number.isInteger(line.issue_movement_id)
        || typeof line.issued_qty !== 'string' || !Number.isFinite(Number(line.issued_qty)) || Number(line.issued_qty) <= 0
        || typeof line.physical_return_supported !== 'boolean'
        || [line.returned_qty, line.returnable_qty].some(qty => qty !== null && (typeof qty !== 'string' || !qty.trim() || !Number.isFinite(Number(qty)) || Number(qty) < 0)))
      || (value.status === 'completed' && (value.result?.action !== 'resolve_exception' || value.result.movements_created !== 0 || typeof value.result.reason !== 'string'))) {
    throw new Error('Invalid exception preview');
  }
  return value;
}

/** Audits an already-accounted exception. Physical receipts and returns use their own ledger workflow. */
export function OrderExceptionPanel({ shop, job, blocked, onBusy, onActive, onClose }: {
  shop: string; job: OrderProcessingJob | null; blocked: boolean; onBusy: (busy: boolean) => void;
  onActive: (active: boolean) => void; onClose: () => void;
}) {
  const { t, i18n } = useTranslation();
  const [id, setId] = useState(() => savedId(shop));
  const [preview, setPreview] = useState<OrderExceptionResolution | null>(null);
  const [reason, setReason] = useState('');
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [uncertain, setUncertain] = useState<'preview' | 'resolve' | null>(() => savedId(shop) ? 'preview' : null);
  const [missing, setMissing] = useState(false);
  const requestId = useRef(id);
  const busyRef = useRef(false), generation = useRef(0);
  const attemptedReason = useRef<string | null>(null);
  const panel = useRef<HTMLElement | null>(null);
  const active = !!job || !!id;
  useEffect(() => { onActive(active); }, [active, onActive]);
  useEffect(() => { if (active) panel.current?.focus(); }, [active]);
  useEffect(() => () => { generation.current++; onBusy(false); onActive(false); }, []);
  const text = (key: string) => t(`orderCollection.exception.${key}`);
  const date = (value: string) => new Date(value).toLocaleString(i18n.language);
  const locked = busy || blocked;
  const expired = !!preview && Date.parse(preview.expires_at) <= Date.now();

  async function run(kind: 'preview' | 'recover' | 'resolve') {
    if (busyRef.current || blocked || !hubUnlocked()) return;
    if (kind === 'preview' && (!job || (!!requestId.current && !missing))) return;
    if (kind === 'resolve' && (!preview || preview.status !== 'prepared' || uncertain || Date.parse(preview.expires_at) <= Date.now() || !confirmed || !reason.trim()
      || reason.trim().length > 1000 || /[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/.test(reason))) return;
    if (kind === 'recover' && !requestId.current) return;
    const context = generation.current, credential = accessRevision();
    const current = () => context === generation.current && credential === accessRevision();
    busyRef.current = true; setBusy(true); onBusy(true); setError(''); setMissing(false);
    let target = requestId.current;
    if (kind === 'preview' && !target) {
      target = crypto.randomUUID(); requestId.current = target; setId(target); rememberId(shop, target);
    }
    try {
      let result: OrderExceptionResolution;
      if (kind === 'preview') result = await previewOrderException(job!.id, target);
      else if (kind === 'recover') result = await getOrderException(target);
      else {
        const frozenReason = attemptedReason.current ?? reason.trim();
        attemptedReason.current = frozenReason;
        await resolveOrderException(preview!, frozenReason);
        result = await getOrderException(target);
      }
      result = checkedPreview(result, shop, target);
      if (current()) {
        setPreview(result); setUncertain(null); setConfirmed(false);
        if (result.status === 'completed') { rememberId(shop, ''); setReason(result.result?.reason || ''); }
      }
    } catch (value) {
      if (current()) {
        const code = (value as { code?: string }).code || 'request_failed';
        setError(code); setConfirmed(false);
        if (kind !== 'recover') setUncertain(kind);
        if (kind === 'recover' && code === 'order_exception_preview_not_found' && attemptedReason.current === null) { setMissing(true); setUncertain(null); }
      }
    } finally { if (current()) { busyRef.current = false; setBusy(false); onBusy(false); } }
  }
  function close() {
    if (busyRef.current || uncertain) return;
    rememberId(shop, ''); requestId.current = ''; setId(''); setPreview(null); setReason(''); attemptedReason.current = null;
    setConfirmed(false); setError(''); setMissing(false); onClose();
  }
  if (!active) return null;
  const canResolve = preview?.status === 'prepared' && !locked && !uncertain && !expired && confirmed && !!reason.trim()
    && reason.trim().length <= 1000 && !/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/.test(reason);
  return <section className="opening-stock-panel" data-testid="order-exception-panel" tabIndex={-1} ref={panel} aria-labelledby="order-exception-heading">
    <h2 id="order-exception-heading">{text('title')}{(preview?.order_number || job?.order_number) && <> · {preview?.order_number || job?.order_number}</>}</h2>
    <p>{text('help')}</p>
    {!preview && <p>{text('prepareHelp')}</p>}
    {job && (!id || missing) && <span className="action-control"><Button data-testid="prepare-order-exception" disabled={locked} onClick={() => run('preview')}>{text('prepare')}</Button><ActionScope effects={['hub-write', 'upgates-read']} shop={shop} calls={{ kind: 'variable' }} /></span>}
    {id && <><p className="opening-stock-muted">{text('previewId')}: <code data-testid="order-exception-id">{id}</code></p>
      <span className="action-control"><Button data-testid="recover-order-exception" variant="secondary" disabled={locked} onClick={() => run('recover')}>{text('recover')}</Button><ActionScope effects={['hub-read']} /></span></>}
    {uncertain && <p role="alert" className="opening-stock-notice">{text('uncertain')}</p>}
    {missing && <p role="status">{text('missing')}</p>}
    {preview && <>
      <p>{preview.warehouse.name} · {text('sourceStatus')}: {preview.source_status_name || t('orderCollection.unknown')} · {text('issuedAt')}: {date(preview.issued_at)}</p>
      <div className="opening-stock-table-scroll"><table data-testid="order-exception-lines"><thead><tr><th>SKU</th><th>{text('issue')}</th><th>{text('issued')}</th><th>{text('returned')}</th><th>{text('remaining')}</th></tr></thead>
        <tbody>{preview.lines.map(line => <tr key={line.issue_movement_id}><td><code>{line.sku}</code></td><td>{line.issue_movement_id}</td><td>{line.issued_qty}</td><td>{line.returned_qty ?? t('orderCollection.unknown')}</td><td>{line.returnable_qty ?? t('orderCollection.unknown')}{!line.physical_return_supported && <small className="orders-inbox-subline">{text('legacy')}</small>}</td></tr>)}</tbody></table></div>
      <p>{text('returnHelp')} <Link to="/stock" target="_blank" rel="noopener noreferrer">{text('openStock')}</Link></p>
      {preview.status === 'completed' ? <div role="status" className="opening-stock-notice"><strong>{text('completed')}</strong><p>{preview.result?.reason}</p><p>{text('refreshHelp')}</p></div> : <>
        <p className="opening-stock-muted">{expired ? text('expired') : `${text('expires')}: ${date(preview.expires_at)}`}</p>
        <label>{text('reason')}<textarea data-testid="order-exception-reason" rows={3} maxLength={1000} disabled={locked || !!uncertain || attemptedReason.current !== null} value={reason} onChange={event => setReason(event.target.value)} /></label>
        <label className="availability-check"><input data-testid="order-exception-confirm" type="checkbox" checked={confirmed} disabled={locked || !!uncertain || expired} onChange={event => setConfirmed(event.target.checked)} />{text('confirmation')}</label>
        <span className="action-control"><Button data-testid="resolve-order-exception" disabled={!canResolve} onClick={() => run('resolve')}>{text('resolve')}</Button><ActionScope effects={['hub-write', 'upgates-read']} shop={shop} calls={{ kind: 'variable' }} /></span>
      </>}
    </>}
    {error && <p role="alert" className="opening-stock-errors">{t(`orderCollection.exception.errors.${error}`, { defaultValue: t(`orderCollection.processingErrors.${error}`, { defaultValue: t(`orderStock.errors.${error}`, { defaultValue: t('orderCollection.errors.request_failed') }) }) })}</p>}
    <Button data-testid="close-order-exception" variant="secondary" disabled={locked || !!uncertain} onClick={close}>{text('close')}</Button>
  </section>;
}
