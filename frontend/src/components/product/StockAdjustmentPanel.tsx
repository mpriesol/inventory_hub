import React, { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { useTranslation } from 'react-i18next';
import { accessRevision, hubRequest, hubUnlocked, subscribeAccess } from '../../api/access';
import { fifoStock, type FifoStock } from '../../api/fifo';
import { ActionScope } from '../ui/ActionScope';
import './fifoPanel.css';

type Pending = { path: string; body: Record<string, unknown> };
type Preview = { id: string; status: string; preview_hash: string; preview: {
  sku: string; warehouse_code: string; before_quantity: string | null; initial_zero_count?: boolean; counted_quantity: string; delta: string;
  reason: string; source_reference: string; unit_cost: string | null; cost_status: string;
  operator_name: string; counted_at: string;
}; result: unknown };
const readPending = (key: string): Pending | null => {
  try {
    const data = JSON.parse(sessionStorage.getItem(key) || 'null');
    return data && /^\/api\/stock-adjustments\/(preview|[0-9a-f-]{36}\/apply)$/.test(data.path)
      && data.body && typeof data.body === 'object' && !Array.isArray(data.body) ? data : null;
  } catch { return null; }
};
const definitiveRejections = new Set(['fifo_invalid_request', 'fifo_request_conflict', 'fifo_preview_changed',
  'fifo_preview_stale', 'fifo_preview_expired', 'fifo_cutover_required', 'fifo_product_not_found',
  'fifo_warehouse_not_found', 'fifo_product_ambiguous', 'fifo_date_invalid', 'adjustment_not_found',
  'adjustment_no_change', 'adjustment_below_committed', 'adjustment_quantity_invalid',
  'adjustment_issue_cost_derived', 'stock_publication_warehouse_held']);

export function StockAdjustmentPanel({ productId, sku, warehouseCode, onChanged }: {
  productId: number; sku: string; warehouseCode: string; onChanged?: () => void;
}) {
  const { t } = useTranslation();
  const c = (key: string, values?: Record<string, unknown>) => t(`stockAdjustment.${key}`, values);
  const credential = useSyncExternalStore(subscribeAccess, accessRevision);
  const key = `stock-adjustment:${productId}:${warehouseCode}`;
  const [stock, setStock] = useState<FifoStock | null>(null);
  const [count, setCount] = useState(''); const [reason, setReason] = useState('');
  const [source, setSource] = useState(''); const [operator, setOperator] = useState('');
  const [cost, setCost] = useState(''); const [known, setKnown] = useState(false);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [pending, setPending] = useState<Pending | null>(() => readPending(key));
  const [confirmed, setConfirmed] = useState(false); const [busy, setBusy] = useState(false);
  const [error, setError] = useState(''); const [message, setMessage] = useState('');
  const [refresh, setRefresh] = useState(0);
  const generation = useRef(0); const running = useRef(false);
  useEffect(() => { setMessage(''); }, [productId, warehouseCode, credential]);
  useEffect(() => {
    const own = ++generation.current; const controller = new AbortController();
    setStock(null); setPreview(null); setConfirmed(false); setPending(readPending(key));
    setCount(''); setReason(''); setSource(''); setOperator(''); setCost(''); setKnown(false);
    setError(''); running.current = false; setBusy(false);
    if (warehouseCode && hubUnlocked()) fifoStock(productId, warehouseCode, 0, controller.signal)
      .then(value => { if (own === generation.current) { setStock(value); setCount(value.balance?.qty_on_hand == null ? '' : String(Number(value.balance.qty_on_hand))); } })
      .catch(() => { if (!controller.signal.aborted && own === generation.current) setError(c('readFailed')); });
    return () => { generation.current++; controller.abort(); };
  }, [productId, warehouseCode, credential, refresh]);
  const changing = (fn: () => void) => { fn(); setConfirmed(false); };
  const incoming = Number(count) > Number(stock?.balance?.qty_on_hand || 0);
  const valid = /^\d{1,9}$/.test(count) && !!reason.trim() && !!source.trim() && !!operator.trim()
    && (!incoming || !known || /^\d+(\.\d{1,4})?$/.test(cost));
  const immutable = busy || !!pending || !!preview;
  const explainError = (value: any) => {
    const code = value?.code || '';
    return t(`stockAdjustment.errors.${code}`, { defaultValue: c('failed') });
  };
  const command = async (request: Pending) => {
    if (running.current) return;
    const own = generation.current; running.current = true; setBusy(true); setError(''); setMessage('');
    const frozen = JSON.stringify(request);
    const clearOriginal = () => { if (sessionStorage.getItem(key) === frozen) sessionStorage.removeItem(key); };
    try {
      sessionStorage.setItem(key, frozen); setPending(request);
      const result: any = await hubRequest(request.path, request.body);
      if (request.path.endsWith('/preview')) {
        if (result?.id !== request.body.request_id || !result?.preview_hash || !result?.preview) throw new Error('invalid response');
      } else {
        const initialZero = result?.initial_zero_count === true && result?.movement_id === null
          && result?.before_quantity === null && result?.counted_quantity === '0' && result?.delta === '0';
        const postedMovement = result?.initial_zero_count !== true && Number.isSafeInteger(result?.movement_id) && result.movement_id > 0;
        if ((!initialZero && !postedMovement) || result?.adjustment_id !== request.path.split('/')[3]
          || result?.product_id !== productId || !Number.isSafeInteger(result?.warehouse_id) || result.warehouse_id <= 0) throw new Error('invalid response');
      }
      clearOriginal();
      if (own !== generation.current) return;
      setPending(null); setConfirmed(false);
      if (request.path.endsWith('/preview')) {
        setPreview(result); setCount(result.preview.counted_quantity); setReason(result.preview.reason);
        setSource(result.preview.source_reference); setOperator(result.preview.operator_name);
        setKnown(result.preview.cost_status === 'known'); setCost(result.preview.unit_cost ?? '');
      }
      else { setPreview(null); setMessage(c(result.initial_zero_count === true ? 'zeroCompleted' : 'completed')); setRefresh(value => value + 1); onChanged?.(); }
    } catch (e: any) {
      if (own !== generation.current) return;
      // Validation/conflict responses are definitive and contain no committed movement.
      if (definitiveRejections.has(e?.code)) { clearOriginal(); setPending(null); setPreview(null); setConfirmed(false); }
      setError(explainError(e));
    } finally { if (own === generation.current) { running.current = false; setBusy(false); } }
  };
  const submit = () => {
    if (!confirmed || !stock || busy || pending) return;
    if (preview) void command({ path: `/api/stock-adjustments/${preview.id}/apply`, body: {
      preview_hash: preview.preview_hash, confirmed: true, quantities_verified: true, costs_documented: true,
    } });
    else if (valid) void command({ path: '/api/stock-adjustments/preview', body: {
      request_id: crypto.randomUUID(), sku, warehouse_code: warehouseCode, counted_quantity: count,
      counted_at: new Date().toISOString(), source_reference: source.trim(), operator_name: operator.trim(), reason: reason.trim(),
      cost_status: incoming && known ? 'known' : 'unknown', unit_cost: incoming && known ? cost : null,
    } });
  };
  return <section className="fifo-panel" data-testid="stock-adjustment-panel">
    <h3>{c('title')}</h3><p>{c('help')}</p>
    {!hubUnlocked() && <p>{c('unlock')}</p>}
    {error && <p role="alert">{error}</p>}{message && <p role="status">{message}</p>}
    {pending && <div><p>{c('uncertain')}</p><button type="button" data-testid="adjustment-retry" disabled={busy} onClick={() => void command(pending)}>{c('retry')}</button></div>}
    {stock && <><p>{sku} · {stock.warehouse.name} · {c('current')}: {stock.balance?.qty_on_hand ?? c('unknownQuantity')}</p>{!stock.balance && <p data-testid="adjustment-initial-count-help">{c('initialCountHelp')}</p>}
      <fieldset disabled={immutable}><div className="fifo-fields">
        <label>{c('count')}<input data-testid="adjustment-count" inputMode="numeric" value={count} onChange={e => changing(() => setCount(e.target.value))} /></label>
        <label>{c('operator')}<input data-testid="adjustment-operator" value={operator} onChange={e => changing(() => setOperator(e.target.value))} /></label>
        <label>{c('source')}<input data-testid="adjustment-source" value={source} onChange={e => changing(() => setSource(e.target.value))} /></label>
        <label>{c('reason')}<input data-testid="adjustment-reason" value={reason} onChange={e => changing(() => setReason(e.target.value))} /></label>
        {incoming && <><label><input data-testid="adjustment-known" type="checkbox" checked={known} onChange={e => changing(() => setKnown(e.target.checked))} />{c('known')}</label>
          {known && <label>{c('cost')}<input data-testid="adjustment-cost" value={cost} onChange={e => changing(() => setCost(e.target.value))} /></label>}</>}
      </div></fieldset>
      {preview && <div data-testid="adjustment-preview"><p>{preview.preview.sku} · {preview.preview.warehouse_code}</p><p>{preview.preview.initial_zero_count ? c('initialZeroPreview') : c('change', { before: preview.preview.before_quantity ?? c('unknownQuantity'), after: preview.preview.counted_quantity, delta: preview.preview.delta })}</p><p>{preview.preview.reason} · {preview.preview.source_reference} · {preview.preview.operator_name} · {new Date(preview.preview.counted_at).toLocaleString()}</p>
        <p data-testid="adjustment-preview-cost">{preview.preview.initial_zero_count ? c('zeroNoMovement') : Number(preview.preview.delta) > 0 ? `${c('cost')}: ${preview.preview.unit_cost ?? c('unknown')}` : c('derivedCost')}</p></div>}
      <label><input data-testid="adjustment-confirm" type="checkbox" disabled={busy || !!pending} checked={confirmed} onChange={e => setConfirmed(e.target.checked)} />{c(preview?.preview.initial_zero_count ? 'confirmInitialZero' : preview ? 'confirmApply' : 'confirmCount')}</label>
      <div className="fifo-actions"><span className="action-control"><button type="button" data-testid="adjustment-submit" disabled={busy || !!pending || !confirmed || (!preview && !valid)} onClick={submit}>{c(preview?.preview.initial_zero_count ? 'recordInitialZero' : preview ? 'apply' : 'preview')}</button><ActionScope effects={['hub-write']} /></span>
        {preview && !pending && <button type="button" disabled={busy} onClick={() => { setPreview(null); setConfirmed(false); }}>{c('edit')}</button>}
        <button type="button" data-testid="adjustment-reload" disabled={busy || !!pending} onClick={() => setRefresh(value => value + 1)}>{c('reload')}</button></div>
    </>}
  </section>;
}
