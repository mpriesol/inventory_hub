import React, { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { accessRevision, hubUnlocked, subscribeAccess, unlockHub } from '../api/access';
import { getHistoryOptions, getMovements, type HistoryOptions, type MovementFilters, type MovementPage } from '../api/stockHistory';
import { CatalogApiError } from '../api/catalog';
import { ActionScope } from '../components/ui/ActionScope';
import './StockHistoryPage.css';

const emptyFilters: MovementFilters = { q: '', sku: '', warehouse_code: '', movement_type: '', date_from: '', date_to: '', tracking_scope: 'current' };

export function StockHistoryPage() {
  const { t, i18n } = useTranslation();
  const [params] = useSearchParams();
  const sku = params.get('sku') || '';
  const revision = useSyncExternalStore(subscribeAccess, accessRevision, accessRevision);
  const [token, setToken] = useState('');
  const [filters, setFilters] = useState<MovementFilters>({ ...emptyFilters, sku });
  const [options, setOptions] = useState<HistoryOptions | null>(null);
  const [data, setData] = useState<MovementPage | null>(null);
  const [pageSize, setPageSize] = useState(50);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [accessError, setAccessError] = useState(false);
  const inFlight = useRef(false);
  const request = useRef<AbortController | null>(null);
  const appliedFilters = useRef(filters);
  const readNumber = (value: string | null, digits = 3) => value === null ? t('stockHistory.unknown') :
    Number(value).toLocaleString(i18n.language, { maximumFractionDigits: digits });

  async function load(next: MovementFilters, page = 1, size = pageSize, snapshot?: number) {
    if (inFlight.current) return;
    inFlight.current = true;
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    const currentRevision = accessRevision();
    setBusy(true); setError(''); setData(null);
    try {
      const result = await getMovements(next, page, size, snapshot, controller.signal);
      if (controller.signal.aborted || accessRevision() !== currentRevision) return;
      appliedFilters.current = { ...next };
      setData(result);
    } catch (e) {
      if (!controller.signal.aborted && accessRevision() === currentRevision) {
        if (e instanceof CatalogApiError && ['hub_access_required', 'hub_access_not_configured'].includes(e.code)) { setAccessError(true); unlockHub(''); }
        else setError(t('stockHistory.loadError'));
      }
    } finally {
      if (request.current === controller) { inFlight.current = false; if (!controller.signal.aborted) setBusy(false); }
    }
  }

  useEffect(() => {
    request.current?.abort(); inFlight.current = false; setData(null); setOptions(null); setError(''); setBusy(false);
    const next = { ...emptyFilters, sku };
    setFilters(next); appliedFilters.current = next;
    const controller = new AbortController();
    if (hubUnlocked()) {
      getHistoryOptions(controller.signal).then(value => {
        if (!controller.signal.aborted && accessRevision() === revision) setOptions(value);
      }).catch(e => { if (!controller.signal.aborted && accessRevision() === revision) {
        if (e instanceof CatalogApiError && ['hub_access_required', 'hub_access_not_configured'].includes(e.code)) { setAccessError(true); unlockHub(''); }
        else setError(t('stockHistory.loadError'));
      } });
      void load(next, 1, pageSize);
    }
    return () => { controller.abort(); request.current?.abort(); };
  }, [revision, sku]);

  const field = (key: keyof MovementFilters, value: string) => setFilters(previous => ({ ...previous, [key]: value }));
  return <main className="stock-history">
    <header><h1>{t('stockHistory.title')}</h1><p>{t('stockHistory.subtitle')}</p>
      <ActionScope effects={['hub-read']} calls={{ kind: 'known', count: 0 }}>{t('stockHistory.localOnly')}</ActionScope>
      <Link to="/products">{t('stockHistory.products')}</Link>
    </header>
    {accessError && <p role="alert">{t('stockHistory.accessError')}</p>}
    {!hubUnlocked() ? <form onSubmit={event => { event.preventDefault(); setAccessError(false); unlockHub(token); setToken(''); }} className="stock-history-unlock">
      <label>{t('stockHistory.access')}<input data-testid="history-token" type="password" value={token} autoComplete="off" onChange={event => setToken(event.target.value)} /></label>
      <button data-testid="history-unlock" type="submit" disabled={!token.trim()}>{t('stockHistory.unlock')}</button>
    </form> : <>
      <form className="stock-history-filters" onSubmit={event => { event.preventDefault(); if (!busy) void load(filters); }}>
        <label>{t('stockHistory.search')}<input data-testid="history-query" value={filters.q} maxLength={200} onChange={event => field('q', event.target.value)} /></label>
        <label>SKU<input data-testid="history-sku" value={filters.sku} maxLength={100} onChange={event => field('sku', event.target.value)} /></label>
        <label>{t('stockHistory.warehouse')}<select value={filters.warehouse_code} onChange={event => field('warehouse_code', event.target.value)}><option value="">{t('stockHistory.all')}</option>{options?.warehouses.map(row => <option key={row.code} value={row.code}>{row.name}</option>)}</select></label>
        <label>{t('stockHistory.type')}<select data-testid="history-type" value={filters.movement_type} onChange={event => field('movement_type', event.target.value)}><option value="">{t('stockHistory.all')}</option>{options?.movement_types.map(type => <option key={type} value={type}>{t(`stockHistory.types.${type}`, type)}</option>)}</select></label>
        <label>{t('stockHistory.scope')}<select data-testid="history-scope" value={filters.tracking_scope} onChange={event => field('tracking_scope', event.target.value)}>{['current', 'historical', 'all'].map(scope => <option value={scope} key={scope}>{t(`stockHistory.scopes.${scope}`)}</option>)}</select></label>
        <label>{t('stockHistory.from')}<input type="date" value={filters.date_from} onChange={event => field('date_from', event.target.value)} /></label>
        <label>{t('stockHistory.to')}<input type="date" value={filters.date_to} min={filters.date_from || undefined} onChange={event => field('date_to', event.target.value)} /></label>
        <button data-testid="history-load" type="submit" disabled={busy}>{t(busy ? 'stockHistory.loading' : 'stockHistory.load')}</button>
      </form>
      <p className="stock-history-note">{t('stockHistory.dateHint')}</p>
      {error && <p role="alert">{error}</p>}
      {busy && <p role="status">{t('stockHistory.loading')}</p>}
      {data && <>
        <p role="status">{t('stockHistory.result', { count: data.total })}</p>
        <div className="stock-history-scroll"><table data-testid="history-table"><thead><tr>
          {['time', 'product', 'warehouse', 'type', 'quantity', 'before', 'after', 'reference', 'reason', 'author', 'unitCost'].map(key => <th key={key}>{t(`stockHistory.${key}`)}</th>)}
        </tr></thead><tbody>{data.items.map(row => <tr key={row.id}>
          <td>{new Date(row.created_at).toLocaleString(i18n.language)}<small>#{row.id}</small><small>{row.tracking_scope && t(`stockHistory.scopes.${row.tracking_scope}`)}</small></td>
          <td><Link to={`/products/${encodeURIComponent(row.sku)}`}>{row.sku}</Link><small>{row.product_name}</small></td>
          <td>{row.warehouse_name}</td><td>{t(`stockHistory.types.${row.movement_type}`, row.movement_type)}</td>
          <td className="stock-history-number">{Number(row.quantity) > 0 ? '+' : ''}{readNumber(row.quantity)}</td>
          <td className="stock-history-number">{readNumber(row.balance_before)}</td><td className="stock-history-number">{readNumber(row.balance_after)}</td>
          <td>{row.reference_type === 'receiving_session' && row.document && row.supplier_code ?
            <Link to={`/receiving/${encodeURIComponent(row.document)}`} state={{ sessionId: row.reference_id, supplier: row.supplier_code }}>{row.document}</Link> :
            row.reference_label || row.reference_id || t('stockHistory.notRecorded')}
            <small>{row.shop_code || row.supplier_code || ''}</small></td>
          <td>{row.reason || t('stockHistory.reasonFromOperation')}</td><td>{row.created_by}</td>
          <td className="stock-history-number">{readNumber(row.unit_cost, 4)}</td>
        </tr>)}</tbody></table></div>
        {!data.items.length && <p>{t('stockHistory.empty')}</p>}
        <p className="stock-history-note">{t('stockHistory.costHint')}</p>
        <footer><label>{t('stockHistory.pageSize')}<select data-testid="history-page-size" value={pageSize} disabled={busy} onChange={event => { const size = Number(event.target.value); setPageSize(size); void load(appliedFilters.current, 1, size); }}>{[25, 50, 100].map(size => <option key={size}>{size}</option>)}</select></label>
          <button data-testid="history-previous" disabled={busy || data.page <= 1} onClick={() => void load(appliedFilters.current, data.page - 1, pageSize, data.snapshot_id)}>←</button>
          <span>{data.page} / {Math.max(1, Math.ceil(data.total / data.page_size))}</span>
          <button data-testid="history-next" disabled={busy || data.page * data.page_size >= data.total} onClick={() => void load(appliedFilters.current, data.page + 1, pageSize, data.snapshot_id)}>→</button>
        </footer>
      </>}
    </>}
  </main>;
}
