import React, { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { useTranslation } from 'react-i18next';
import { ActionScope } from '../ui/ActionScope';
import { accessRevision, hubUnlocked, subscribeAccess } from '../../api/access';
import { fifoCommand, fifoCostOptions, fifoHistory, fifoOptions, fifoReturnOptions, fifoStock,
  type CostStatus, type FifoAllocation, type FifoCostOptions, type FifoLayer, type FifoMovement,
  type FifoPreview, type FifoReturnOptions, type FifoStock } from '../../api/fifo';
import { fifoCopy, fifoErrors } from './fifoCopy';
import './fifoPanel.css';

type Operation = 'receive' | 'cutover' | 'return' | 'revise' | 'release';
type LayerDraft = { quantity: string; unit_cost: string; cost_status: CostStatus; physical_received_at: string; source_reference: string };
type Pending = { path: string; body: Record<string, unknown> };
const localDate = () => { const d = new Date(); return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16); };
const blankLayer = (): LayerDraft => ({ quantity: '', unit_cost: '', cost_status: 'unknown', physical_received_at: localDate(), source_reference: '' });
const uuid = () => crypto.randomUUID();
const exactQuantity = (value: string) => /^\d+(\.\d{1,3})?$/.test(value) && Number(value) > 0;
const exactCost = (value: string) => /^\d+(\.\d{1,4})?$/.test(value);
const readPending = (key: string): Pending | null => {
  try {
    const value = JSON.parse(sessionStorage.getItem(key) || 'null');
    return value && typeof value.path === 'string' && /^\/api\/fifo\/(?:returns|costs\/revise|quarantine\/release|(?:cutovers|receipts)\/(?:preview|[0-9a-f-]{36}\/apply))$/.test(value.path)
      && value.body && typeof value.body === 'object' && !Array.isArray(value.body) ? value : null;
  } catch { return null; }
};

export function FifoPanel({ productId, warehouseCode, onChanged }: { productId: number; warehouseCode: string; onChanged?: () => void }) {
  const { i18n } = useTranslation();
  const credential = useSyncExternalStore(subscribeAccess, accessRevision);
  const language = i18n.language.startsWith('sk') ? 'sk' : 'en';
  const c = fifoCopy[language];
  const failure = (value: unknown, fallback: string) => fifoErrors[language][(value as { code?: string })?.code || ''] || fallback;
  const [warehouses, setWarehouses] = useState<{ id: number; code: string; name: string }[]>([]);
  const [warehouse, setWarehouse] = useState(warehouseCode);
  const [stock, setStock] = useState<FifoStock | null>(null);
  const [movements, setMovements] = useState<FifoMovement[]>([]);
  const [historyTotal, setHistoryTotal] = useState(0);
  const [layerOffset, setLayerOffset] = useState(0);
  const [historyOffset, setHistoryOffset] = useState(0);
  const [refresh, setRefresh] = useState(0);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [operation, setOperation] = useState<Operation | null>(null);
  const [layers, setLayers] = useState<LayerDraft[]>([blankLayer()]);
  const [source, setSource] = useState('');
  const [operator, setOperator] = useState('');
  const [reason, setReason] = useState('');
  const [quantity, setQuantity] = useState('');
  const [cost, setCost] = useState('');
  const [costStatus, setCostStatus] = useState<CostStatus>('unknown');
  const [date, setDate] = useState(localDate());
  const [condition, setCondition] = useState('good');
  const [targetWarehouse, setTargetWarehouse] = useState('');
  const [confirmed, setConfirmed] = useState(false);
  const [selectedLayer, setSelectedLayer] = useState<FifoLayer | null>(null);
  const [returnData, setReturnData] = useState<FifoReturnOptions | null>(null);
  const [costData, setCostData] = useState<FifoCostOptions | null>(null);
  const [preview, setPreview] = useState<FifoPreview | null>(null);
  const storageKey = `fifo-pending:${productId}:${warehouse}`;
  const [pending, setPending] = useState<Pending | null>(() => readPending(storageKey));
  const context = useRef(0);
  const immutable = busy || !!pending || !!preview;
  const amount = (value: string | null | undefined) => value == null ? c.unknown : `${value} €`;
  const status = (value: CostStatus) => c[value];
  const reset = () => { setOperation(null); setPreview(null); setConfirmed(false); setReturnData(null); setCostData(null); setSelectedLayer(null); };
  useEffect(() => { setWarehouse(warehouseCode); }, [productId, warehouseCode]);
  useEffect(() => {
    context.current += 1; reset(); setLayerOffset(0); setHistoryOffset(0); setError(''); setMessage('');
    busyRef.current = false; setBusy(false); setPending(readPending(storageKey));
    return () => { context.current += 1; };
  }, [productId, warehouse, credential]);
  useEffect(() => {
    if (!hubUnlocked()) { setWarehouses([]); return; }
    const controller = new AbortController();
    fifoOptions(controller.signal).then(data => { if (!controller.signal.aborted) setWarehouses(data.warehouses); }).catch(e => { if (!controller.signal.aborted) setError(failure(e, c.readFailed)); });
    return () => controller.abort();
  }, [productId, credential]);
  useEffect(() => {
    if (!warehouse || !hubUnlocked()) { setStock(null); setMovements([]); setLoading(false); return; }
    const controller = new AbortController(); setLoading(true); setStock(null); setError('');
    Promise.all([fifoStock(productId, warehouse, layerOffset, controller.signal), fifoHistory(productId, warehouse, historyOffset, controller.signal)])
      .then(([data, history]) => { if (!controller.signal.aborted) { setStock(data); setMovements(history.movements); setHistoryTotal(history.total); } })
      .catch(e => { if (!controller.signal.aborted) setError(failure(e, c.readFailed)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [productId, warehouse, layerOffset, historyOffset, refresh, credential]);

  const open = (kind: Operation, layer?: FifoLayer) => {
    if (busyRef.current || pending) return;
    reset(); setOperation(kind); setSelectedLayer(layer || null); setMessage(''); setError('');
    setLayers(kind === 'cutover' && Number(stock?.balance?.qty_on_hand) === 0 ? [] : [blankLayer()]);
    setSource(''); setOperator(''); setReason(''); setQuantity(''); setCost(''); setCostStatus('unknown'); setDate(localDate());
    setCondition('good'); setTargetWarehouse(String(stock?.warehouse.id || ''));
  };
  const inspectIssue = async (id: number) => {
    if (busyRef.current || pending) return;
    open('return'); busyRef.current = true; setBusy(true); const generation = context.current;
    try { const data = await fifoReturnOptions(id); if (generation === context.current) setReturnData(data); }
    catch (e: any) { if (generation === context.current) setError(failure(e, c.readFailed)); }
    finally { if (generation === context.current) { busyRef.current = false; setBusy(false); } }
  };
  const inspectCost = async (layer: FifoLayer) => {
    if (busyRef.current || pending) return;
    open('revise', layer); busyRef.current = true; setBusy(true); const generation = context.current;
    try { const data = await fifoCostOptions(layer.root_cost_layer_id); if (generation === context.current) {
      setCostData(data); setCost(data.unit_cost || ''); setCostStatus(data.cost_status);
    } } catch (e: any) { if (generation === context.current) setError(failure(e, c.readFailed)); }
    finally { if (generation === context.current) { busyRef.current = false; setBusy(false); } }
  };
  const remember = (command: Pending | null) => {
    setPending(command);
    try { if (command) sessionStorage.setItem(storageKey, JSON.stringify(command)); else sessionStorage.removeItem(storageKey); } catch { /* In-memory retry remains available. */ }
  };
  const execute = async (command: Pending) => {
    if (busyRef.current || !hubUnlocked()) return;
    busyRef.current = true; remember(command); setBusy(true); setError(''); setMessage(''); const generation = context.current;
    try {
      const result = await fifoCommand<FifoPreview>(command.path, command.body);
      // A durable local request UUID makes a repeated POST return the same result.
      try { sessionStorage.removeItem(storageKey); } catch { /* No credentials are stored. */ }
      if (generation !== context.current) return;
      remember(null);
      if (command.path.endsWith('/preview')) {
        const data = result.preview_data as any;
        const kind = command.path.includes('/cutovers/') ? 'cutover' : 'receive';
        setOperation(kind); setSource(data.source_reference || ''); setOperator(data.operator_name || '');
        const toLocal = (value: string) => { const d = new Date(value); return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16); };
        const savedLayers = kind === 'cutover' ? data.layers : [data];
        setLayers(savedLayers.map((row: any) => ({ quantity: row.quantity, unit_cost: row.unit_cost || '',
          cost_status: row.cost_status, physical_received_at: toLocal(row.physical_received_at), source_reference: row.source_reference })));
        if (data.counted_at) setDate(toLocal(data.counted_at));
        setPreview(result); setConfirmed(false);
      }
      else { reset(); setMessage(c.saved); setRefresh(v => v + 1); onChanged?.(); }
    } catch (e: any) {
      if (generation === context.current) {
        setError(failure(e, c.failed));
        // Structured API failures rolled back. Unknown network outcomes retain
        // the exact command for idempotent recovery, including after remount.
        if (e.code && e.code !== 'request_failed') remember(null);
      }
    } finally { if (generation === context.current) { busyRef.current = false; setBusy(false); } }
  };
  const submit = async () => {
    if (busyRef.current || pending) return;
    if (!stock || !operation || !confirmed) { setError(c.validation); return; }
    const body: Record<string, unknown> = { request_id: uuid(), confirmed: true, reason: reason.trim() };
    let path = '';
    if (preview) {
      path = `/api/fifo/${operation === 'cutover' ? 'cutovers' : 'receipts'}/${preview.id}/apply`;
      await execute({ path, body: { preview_hash: preview.preview_hash, confirmed: true, quantities_verified: true, costs_documented: true } }); return;
    }
    if (operation === 'receive' || operation === 'cutover') {
      if (!source.trim() || !operator.trim() || !date) { setError(c.validation); return; }
      const valid = layers.every(row => exactQuantity(row.quantity) && row.source_reference.trim() && row.physical_received_at
        && (row.cost_status === 'unknown' || exactCost(row.unit_cost)));
      if (!valid || (operation === 'receive' && layers.length !== 1)) { setError(c.validation); return; }
      const values = layers.map(row => ({ ...row, source_reference: row.source_reference.trim(),
        unit_cost: row.cost_status === 'unknown' ? null : row.unit_cost,
        physical_received_at: new Date(row.physical_received_at).toISOString() }));
      const shared = { request_id: body.request_id, sku: stock.sku, warehouse_code: warehouse,
        source_reference: source.trim(), operator_name: operator.trim() };
      const payload = operation === 'cutover' ? { ...shared, counted_at: new Date(date).toISOString(), layers: values }
        : { ...shared, ...values[0], source_reference: source.trim() };
      await execute({ path: `/api/fifo/${operation === 'cutover' ? 'cutovers' : 'receipts'}/preview`, body: payload }); return;
    }
    if (!reason.trim()) { setError(c.validation); return; }
    if (operation === 'return') {
      if (!returnData || !source.trim() || !/^\d+$/.test(quantity) || !exactQuantity(quantity) || Number(quantity) > Number(returnData.returnable_quantity)) { setError(c.validation); return; }
      Object.assign(body, { issue_movement_id: returnData.issue_movement_id, quantity, case_reference: source.trim(), condition, physical_received: true }); path = '/api/fifo/returns';
    } else if (operation === 'release') {
      if (!selectedLayer || !targetWarehouse || !/^\d+$/.test(quantity) || !exactQuantity(quantity) || Number(quantity) > Number(selectedLayer.quantity_remaining)) { setError(c.validation); return; }
      Object.assign(body, { source_layer_id: selectedLayer.id, target_warehouse_id: Number(targetWarehouse), quantity, condition_verified: true }); path = '/api/fifo/quarantine/release';
    } else {
      if (!costData || !source.trim() || (costStatus !== 'unknown' && !exactCost(cost))) { setError(c.validation); return; }
      Object.assign(body, { root_layer_id: costData.root_layer_id, expected_revision: costData.revision, new_unit_cost: costStatus === 'unknown' ? null : cost,
        cost_status: costStatus, document_reference: source.trim() }); path = '/api/fifo/costs/revise';
    }
    await execute({ path, body });
  };
  const allocationTable = (rows: FifoAllocation[]) => <div className="fifo-scroll"><table><thead><tr><th>{c.layers}</th><th>{c.quantity}</th><th>{c.returned}</th><th>{c.originalCost}</th><th>{c.currentCost}</th></tr></thead><tbody>{rows.map(row => <tr key={row.id}><td>#{row.layer_id}</td><td>{row.quantity}</td><td>{row.returned_quantity}</td><td>{amount(row.total_cost_at_issue)} · {status(row.cost_status_at_issue)}</td><td>{amount(row.total_cost_current)} · {status(row.cost_status_current)}</td></tr>)}</tbody></table></div>;
  return <section className="fifo-panel" data-testid="fifo-panel">
    <div className="fifo-toolbar"><h3>{c.title}</h3><label>{c.warehouse}<select data-testid="fifo-warehouse" value={warehouse} disabled={busy || !!pending} onChange={e => setWarehouse(e.target.value)}><option value="">{c.chooseWarehouse}</option>{warehouses.map(w => <option key={w.id} value={w.code}>{w.name}</option>)}</select></label><span className="action-control"><button type="button" disabled={busy || loading} onClick={() => setRefresh(v => v + 1)}>{c.refresh}</button><ActionScope effects={['hub-read']} /></span></div>
    {error && <p role="alert">{error}</p>}{message && <p role="status">{message}</p>}
    {pending && <div role="alert"><p>{c.pending}</p><span className="action-control"><button type="button" data-testid="fifo-retry" disabled={busy} onClick={() => execute(pending)}>{c.retry}</button><ActionScope effects={['hub-write']} /></span></div>}
    {loading && <p role="status">{c.loading}</p>}
    {stock && <>
      {stock.valuation.mode !== 'fifo' && <p className="fifo-notice">{stock.valuation.mode === 'legacy' ? c.legacy : stock.valuation.mode === 'unconfirmed' ? c.unconfirmed : c.missing}</p>}
      <div className="fifo-metrics">{[[c.physical, stock.balance?.qty_on_hand], [c.reserved, stock.balance?.qty_reserved], [c.quarantine, stock.balance?.qty_quarantined], [c.available, stock.balance?.qty_available], [c.knownValue, amount(stock.valuation.known_value)], [c.provisionalValue, amount(stock.valuation.provisional_value)], [c.unknownQty, stock.valuation.unknown_qty], [c.avg, amount(stock.valuation.avg_cost)], [c.next, stock.valuation.next_layer ? `${amount(stock.valuation.next_layer.unit_cost)} · ${status(stock.valuation.next_layer.cost_status)}` : c.unknown], [c.last, `${amount(stock.balance?.last_purchase_price)} · ${stock.valuation.last_purchase_cost_status === 'legacy' ? c.legacyCost : status(stock.valuation.last_purchase_cost_status || 'unknown')}`]].map(([label, value]) => <div key={label}><small>{label}</small><strong>{value ?? c.unknown}</strong></div>)}</div>
      <p>{stock.valuation.value_complete ? c.complete : c.incomplete}. {c.prices}</p>
      <div className="fifo-toolbar"><button type="button" data-testid="fifo-receive" disabled={busy || !!pending} onClick={() => open('receive')}>{c.receive}</button>{stock.valuation.mode === 'legacy' && <button type="button" data-testid="fifo-cutover" disabled={busy || !!pending} onClick={() => open('cutover')}>{c.cutover}</button>}</div>
      {operation && <div className="fifo-form" data-testid="fifo-form"><h4>{c[operation]}</h4><p>{c[`${operation}Help` as 'receiveHelp']}</p>
        {returnData && <><p>{c.issue} #{returnData.issue_movement_id} · {c.returnable}: {returnData.returnable_quantity}</p>{allocationTable(returnData.allocations)}<p>{c.notMargin}</p></>}
        {costData && <><p>{c.netCost}: {amount(costData.net_consumption.total_cost)} · {costData.net_consumption.value_complete ? c.complete : c.incomplete}</p>{allocationTable(costData.allocations)}
          {costData.revisions.length > 0 && <details><summary>{c.revisions}</summary><ul>{costData.revisions.map(row => <li key={row.revision}>{row.document_reference}: {amount(row.previous_cost)} → {amount(row.new_cost)} · {row.reason}</li>)}</ul></details>}</>}
        <fieldset disabled={immutable} onChange={() => setConfirmed(false)}>
          {(operation === 'receive' || operation === 'cutover') && <>
            <div className="fifo-fields"><label>{c.operator}<input data-testid="fifo-operator" value={operator} onChange={e => setOperator(e.target.value)} /></label><label>{c.source}<input data-testid="fifo-source" value={source} onChange={e => setSource(e.target.value)} /></label>{operation === 'cutover' && <label>{c.counted}<input type="datetime-local" value={date} onChange={e => setDate(e.target.value)} /></label>}</div>
            {layers.map((row, index) => <div className="fifo-layer-draft" key={index}>{(['quantity', 'physical_received_at', 'source_reference'] as const).map(key => <label key={key}>{key === 'quantity' ? c.quantity : key === 'physical_received_at' ? c.date : c.source}<input data-testid={`fifo-layer-${index}-${key}`} type={key === 'physical_received_at' ? 'datetime-local' : 'text'} value={row[key]} onChange={e => setLayers(values => values.map((old, pos) => pos === index ? { ...old, [key]: e.target.value } : old))} /></label>)}
              <label>{c.state}<select data-testid={`fifo-layer-${index}-status`} value={row.cost_status} onChange={e => setLayers(values => values.map((old, pos) => pos === index ? { ...old, cost_status: e.target.value as CostStatus } : old))}>{(['unknown', 'provisional', 'known'] as const).map(value => <option key={value} value={value}>{status(value)}</option>)}</select></label>
              <label>{c.cost}<input data-testid={`fifo-layer-${index}-cost`} disabled={row.cost_status === 'unknown'} value={row.cost_status === 'unknown' ? '' : row.unit_cost} onChange={e => setLayers(values => values.map((old, pos) => pos === index ? { ...old, unit_cost: e.target.value } : old))} /></label>
              {operation === 'cutover' && <button type="button" onClick={() => { setConfirmed(false); setLayers(values => values.filter((_, pos) => pos !== index)); }}>{c.remove}</button>}
            </div>)}
            {operation === 'cutover' && <button type="button" onClick={() => { setConfirmed(false); setLayers(values => [...values, blankLayer()]); }}>{c.addLayer}</button>}
          </>}
          {(operation === 'return' || operation === 'release') && <label>{c.quantity}<input data-testid="fifo-quantity" value={quantity} onChange={e => setQuantity(e.target.value)} /></label>}
          {operation === 'return' && <><label>{c.case}<input data-testid="fifo-source" value={source} onChange={e => setSource(e.target.value)} /></label><label>{c.condition}<select value={condition} onChange={e => setCondition(e.target.value)}><option value="good">{c.good}</option><option value="damaged">{c.damaged}</option></select></label></>}
          {operation === 'release' && <label>{c.target}<select data-testid="fifo-target" value={targetWarehouse} onChange={e => setTargetWarehouse(e.target.value)}><option value="">{c.chooseWarehouse}</option>{warehouses.map(w => <option key={w.id} value={w.id}>{w.name}</option>)}</select></label>}
          {operation === 'revise' && <><label>{c.state}<select data-testid="fifo-cost-status" value={costStatus} onChange={e => setCostStatus(e.target.value as CostStatus)}>{(['unknown', 'provisional', 'known'] as const).map(value => <option key={value} value={value}>{status(value)}</option>)}</select></label><label>{c.cost}<input data-testid="fifo-cost" disabled={costStatus === 'unknown'} value={costStatus === 'unknown' ? '' : cost} onChange={e => setCost(e.target.value)} /></label><label>{c.source}<input data-testid="fifo-source" value={source} onChange={e => setSource(e.target.value)} /></label></>}
          {!['receive', 'cutover'].includes(operation) && <label>{c.reason}<input data-testid="fifo-reason" value={reason} onChange={e => setReason(e.target.value)} /></label>}
        </fieldset>
        {preview && <div data-testid="fifo-preview"><p>{c.previewHelp}</p><p>{stock.sku} · {stock.warehouse.name} · {source}</p><ul>{layers.map((row, i) => <li key={i}>{row.quantity} × {row.cost_status === 'unknown' ? c.unknown : amount(row.unit_cost)} · {status(row.cost_status)} · {row.physical_received_at} · {row.source_reference}</li>)}</ul></div>}
        <label className="fifo-confirm"><input type="checkbox" data-testid="fifo-confirm" disabled={busy || !!pending} checked={confirmed} onChange={e => setConfirmed(e.target.checked)} />{c.confirmed}</label>
        <div className="fifo-toolbar"><span className="action-control"><button type="button" data-testid="fifo-submit" disabled={busy || !!pending || !confirmed || (operation === 'return' && !returnData) || (operation === 'revise' && !costData)} onClick={submit}>{!preview && ['receive', 'cutover'].includes(operation) ? c.preview : c.apply}</button><ActionScope effects={['hub-write']} /></span><button type="button" disabled={busy || !!pending} onClick={reset}>{c.cancel}</button></div>
      </div>}
      <h4>{c.layers}</h4><div className="fifo-scroll"><table><thead><tr><th>{c.date}</th><th>{c.source}</th><th>{c.remaining}</th><th>{c.cost}</th><th>{c.state}</th><th /></tr></thead><tbody>{stock.layers.map(layer => <tr key={layer.id}><td>#{layer.id}<br />{new Date(layer.physical_received_at).toLocaleString()}</td><td>{String(layer.provenance.source_reference || layer.provenance.case_reference || '')}</td><td>{layer.quantity_remaining} / {layer.quantity_original}</td><td>{amount(layer.unit_cost)}</td><td>{status(layer.cost_status)} · {layer.stock_status === 'quarantine' ? c.quarantine : c.available}</td><td><span className="action-control"><button type="button" data-testid={`fifo-revise-${layer.id}`} disabled={busy || !!pending} onClick={() => inspectCost(layer)}>{c.revise}</button><ActionScope effects={['hub-read']} /></span>{layer.stock_status === 'quarantine' && Number(layer.quantity_remaining) > 0 && <button type="button" data-testid={`fifo-release-${layer.id}`} disabled={busy || !!pending} onClick={() => open('release', layer)}>{c.release}</button>}</td></tr>)}</tbody></table></div>
      {stock.layers.length === 0 && <p>{c.noLayers}</p>}<div className="fifo-toolbar"><span className="action-control"><button type="button" disabled={loading || layerOffset === 0} onClick={() => setLayerOffset(v => Math.max(0, v - 50))}>{c.previous}</button><ActionScope effects={['hub-read']} /></span><span>{Math.min(layerOffset + 50, stock.total)} / {stock.total}</span><span className="action-control"><button type="button" disabled={loading || layerOffset + 50 >= stock.total} onClick={() => setLayerOffset(v => v + 50)}>{c.following}</button><ActionScope effects={['hub-read']} /></span></div>
      <h4>{c.history}</h4><div className="fifo-scroll"><table><thead><tr><th>{c.date}</th><th>{c.state}</th><th>{c.quantity}</th><th>{c.originalCost}</th><th /></tr></thead><tbody>{movements.map(row => <tr key={row.id}><td>{new Date(row.created_at).toLocaleString()}</td><td>{row.movement_type} #{row.id}<br />{row.reference_id}</td><td>{row.quantity}</td><td>{amount(row.total_cost)}</td><td>{row.movement_type.toUpperCase() === 'SALE_OUT' && <span className="action-control"><button type="button" data-testid={`fifo-return-${row.id}`} disabled={busy || !!pending} onClick={() => inspectIssue(row.id)}>{c.inspect}</button><ActionScope effects={['hub-read']} /></span>}</td></tr>)}</tbody></table></div>
      {movements.length === 0 && <p>{c.noMovements}</p>}<div className="fifo-toolbar"><span className="action-control"><button type="button" disabled={loading || historyOffset === 0} onClick={() => setHistoryOffset(v => Math.max(0, v - 50))}>{c.previous}</button><ActionScope effects={['hub-read']} /></span><span>{Math.min(historyOffset + 50, historyTotal)} / {historyTotal}</span><span className="action-control"><button type="button" disabled={loading || historyOffset + 50 >= historyTotal} onClick={() => setHistoryOffset(v => v + 50)}>{c.following}</button><ActionScope effects={['hub-read']} /></span></div>
    </>}
  </section>;
}
