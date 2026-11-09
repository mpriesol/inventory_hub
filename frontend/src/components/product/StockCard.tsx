import React, { useCallback, useEffect, useId, useMemo, useRef, useState, useSyncExternalStore } from 'react';
import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';
import { ArrowLeft, Box, Check, ChevronRight, ExternalLink, History, Image as ImageIcon, Layers, LayoutDashboard, Pencil, RefreshCw, Store, X } from 'lucide-react';
import { accessRevision, hubUnlocked, subscribeAccess } from '../../api/access';
import { getEditorProduct, getEditorProductBySku, getEditorSave, getProductEditorOptions, refreshEditorAttributes, saveEditorProducts,
  type ProductEditorDetail, type ProductEditorOptions, type ProductEditorSave, type ProductEditorSaveBody } from '../../api/productEditor';
import { defaultWarehouseCode } from '../../utils/warehouses';
import { refreshProductLinkMetadata } from '../../api/productLinks';
import { ActionScope } from '../ui/ActionScope';
import { FifoPanel } from './FifoPanel';
import { StockAdjustmentPanel } from './StockAdjustmentPanel';
import { ProductSupplierLinksPanel } from './ProductSupplierLinksPanel';
import { ProductShopLinksPanel } from './ProductShopLinksPanel';
import { ProductPublicationPanel } from './ProductPublicationPanel';
import { ProductPricePublicationPanel } from './ProductPricePublicationPanel';
import { SkuMigrationPreviewDialog } from './SkuMigrationPreviewDialog';
import { StockCardEditor } from './StockCardEditor';
import { stockCardAuditChanges, stockCardChange, stockCardDescription, stockCardDraft, stockCardDraftChanged, stockCardPending, stockCardSafeUrl, stockCardSaveKey,
  type StockCardDraft, type StockCardEditorKind } from './stockCardHelpers';
import './StockCard.css';

export type StockCardSection = 'overview' | 'stock' | 'prices' | 'shops' | 'history';
export interface StockCardProps {
  productId?: number; sku?: string; warehouseCode?: string | null; initialSection?: StockCardSection;
  onClose: () => void;
  /** A confirmed change invalidates this canonical card. Callers reload it in their own warehouse scope. */
  onChanged?: (row: ProductEditorDetail) => void; readOnlyReason?: string;
}
type Tool = 'supplier' | 'eans' | 'mapping' | 'sku' | 'publication-biketrek' | 'publication-xtrek' | null;
const sections: { id: StockCardSection; icon: typeof Box }[] = [
  { id: 'overview', icon: LayoutDashboard }, { id: 'stock', icon: Box }, { id: 'prices', icon: Layers },
  { id: 'shops', icon: Store }, { id: 'history', icon: History },
];
const childKeys = ['fifo', 'adjustment', 'supplier', 'mapping', 'sku', 'publication', 'price'] as const;
const decimal = (value: string, digits: number) => new RegExp(`^\\d+(?:[.,]\\d{1,${digits}})?$`).test(value);

export function StockCard({ productId, sku, warehouseCode, initialSection = 'overview', onClose, onChanged, readOnlyReason }: StockCardProps) {
  const { t, i18n } = useTranslation();
  const c = (key: string, vars?: Record<string, unknown>) => t(`stockCard.${key}`, vars);
  const credential = useSyncExternalStore(subscribeAccess, accessRevision);
  const uid = useId();
  const dialog = useRef<HTMLElement>(null), confirmation = useRef<HTMLDivElement>(null), content = useRef<HTMLDivElement>(null);
  const [options, setOptions] = useState<ProductEditorOptions | null>(null);
  const [warehouse, setWarehouse] = useState(warehouseCode || '');
  const [warehouseReady, setWarehouseReady] = useState(warehouseCode !== undefined);
  const warehouseSelectionKnown = useRef(warehouseCode !== undefined);
  const [row, setRow] = useState<ProductEditorDetail | null>(null);
  const [section, setSection] = useState<StockCardSection>(initialSection);
  const [visited, setVisited] = useState<Set<StockCardSection>>(() => new Set([initialSection]));
  const [loading, setLoading] = useState(true), [busy, setBusy] = useState(false);
  const [stockStale, setStockStale] = useState(false);
  const [error, setError] = useState(''), [message, setMessage] = useState('');
  const [refresh, setRefresh] = useState(0), [optionsRetry, setOptionsRetry] = useState(0), [stockRefresh, setStockRefresh] = useState(0);
  const [editor, setEditor] = useState<StockCardDraft | null>(null);
  const [conflict, setConflict] = useState<ProductEditorDetail | null>(null);
  const [tool, setTool] = useState<Tool>(null);
  const [childDirty, setChildDirty] = useState<Record<string, boolean>>({}), [childBusy, setChildBusy] = useState<Record<string, boolean>>({});
  const [childVersion, setChildVersion] = useState(0);
  const [pending, setPending] = useState<ProductEditorSaveBody | null>(null), [saveMissing, setSaveMissing] = useState(false);
  const [guard, setGuard] = useState<{ run: () => void } | null>(null);
  const guardFocus = useRef<HTMLElement | null>(null);
  const [imageFailed, setImageFailed] = useState(false);
  const generation = useRef(0), sending = useRef(false);
  const contextKey = useRef(''); contextKey.current = `${productId || ''}:${sku || ''}:${warehouse}:${credential}`;
  const rowRef = useRef(row); rowRef.current = row;
  const callbacks = useRef({ onClose, onChanged }); callbacks.current = { onClose, onChanged };
  const state = useRef({ busy: false, dirty: false, guard: false, requestClose: () => {} });
  const dirtyCallbacks = useMemo(() => Object.fromEntries(childKeys.map(key => [key, (value: boolean) =>
    setChildDirty(previous => previous[key] === value ? previous : { ...previous, [key]: value })])) as Record<typeof childKeys[number], (value: boolean) => void>, []);
  const busyCallbacks = useMemo(() => Object.fromEntries(childKeys.map(key => [key, (value: boolean) =>
    setChildBusy(previous => previous[key] === value ? previous : { ...previous, [key]: value })])) as Record<typeof childKeys[number], (value: boolean) => void>, []);
  const isBusy = busy || Object.values(childBusy).some(Boolean);
  const draftDirty = stockCardDraftChanged(editor);
  const dirty = draftDirty || !!pending || Object.values(childDirty).some(Boolean);
  const mutationDisabled = !!readOnlyReason || !hubUnlocked() || isBusy || loading || !!pending || !!conflict;
  const request = (run: () => void, needsGuard = dirty) => {
    if (isBusy) return;
    if (needsGuard) { guardFocus.current = document.activeElement as HTMLElement | null; setGuard({ run }); } else run();
  };
  state.current = { busy: isBusy, dirty, guard: !!guard, requestClose: () => request(() => callbacks.current.onClose()) };

  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const overflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    const background = [...document.body.children].filter(element => !element.contains(dialog.current));
    const previousInert = background.map(element => element.hasAttribute('inert'));
    background.forEach(element => element.setAttribute('inert', ''));
    dialog.current?.focus();
    const keyboard = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault(); event.stopPropagation();
        if (state.current.busy) return;
        if (state.current.guard) setGuard(null); else state.current.requestClose();
      }
      if (event.key !== 'Tab') return;
      const scope = state.current.guard ? confirmation.current : dialog.current;
      const nodes = [...(scope?.querySelectorAll<HTMLElement>('a[href],button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled),summary,[tabindex="0"]') || [])]
        .filter(element => {
          if (element.closest('[hidden]') || element.getAttribute('aria-hidden') === 'true') return false;
          for (let parent = element.parentElement; parent && parent !== scope; parent = parent.parentElement) {
            if (parent.tagName === 'DETAILS' && !parent.hasAttribute('open') && !parent.querySelector('summary')?.contains(element)) return false;
          }
          return true;
        });
      const first = nodes[0], last = nodes[nodes.length - 1];
      if (!nodes.length) { event.preventDefault(); dialog.current?.focus(); }
      else if (event.shiftKey && (document.activeElement === first || !scope?.contains(document.activeElement))) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && (document.activeElement === last || !scope?.contains(document.activeElement))) { event.preventDefault(); first?.focus(); }
    };
    const beforeUnload = (event: BeforeUnloadEvent) => { if (state.current.dirty || state.current.busy) { event.preventDefault(); event.returnValue = ''; } };
    document.addEventListener('keydown', keyboard);
    window.addEventListener('beforeunload', beforeUnload);
    return () => {
      document.body.style.overflow = overflow;
      background.forEach((element, index) => { if (!previousInert[index]) element.removeAttribute('inert'); });
      document.removeEventListener('keydown', keyboard); window.removeEventListener('beforeunload', beforeUnload);
      if (previous?.isConnected) previous.focus();
    };
  }, []);
  useEffect(() => {
    if (guard) confirmation.current?.querySelector<HTMLButtonElement>('button')?.focus();
    else if (guardFocus.current) { if (guardFocus.current.isConnected) guardFocus.current.focus(); else dialog.current?.focus(); guardFocus.current = null; }
  }, [guard]);

  useEffect(() => {
    generation.current++; setRow(null); setEditor(null); setConflict(null); setPending(null); setTool(null); setGuard(null);
    setChildDirty({}); setChildBusy({}); setChildVersion(value => value + 1); setError(''); setMessage('');
    setSection(initialSection); setVisited(new Set([initialSection]));
    const explicitWarehouse = warehouseCode !== undefined;
    setWarehouse(explicitWarehouse ? warehouseCode || '' : options ? defaultWarehouseCode(options.warehouses) : '');
    warehouseSelectionKnown.current = explicitWarehouse || !!options;
    setWarehouseReady(warehouseSelectionKnown.current); sending.current = false; setBusy(false);
  }, [productId, sku, warehouseCode, credential]);

  useEffect(() => {
    if (!hubUnlocked()) { setLoading(false); return; }
    const abort = new AbortController();
    getProductEditorOptions(abort.signal).then(value => {
      if (abort.signal.aborted) return;
      setOptions(value);
      if (!warehouseSelectionKnown.current) { setWarehouse(defaultWarehouseCode(value.warehouses)); warehouseSelectionKnown.current = true; }
      setWarehouseReady(true);
    }).catch(failure => { if (!abort.signal.aborted) { setError((failure as { code?: string }).code || 'readFailed'); setLoading(false); } });
    return () => abort.abort();
  }, [credential, optionsRetry]);

  useEffect(() => {
    if (!hubUnlocked() || !warehouseReady) return;
    if (!(productId && Number.isSafeInteger(productId) && productId > 0) && !sku?.trim()) { setLoading(false); setError('missingProduct'); return; }
    const own = ++generation.current, abort = new AbortController(); setLoading(true);
    const load = productId ? getEditorProduct(productId, warehouse, abort.signal) : getEditorProductBySku(sku!, warehouse, abort.signal);
    load.then(value => {
      if (abort.signal.aborted || own !== generation.current) return;
      setRow(value); setStockStale(false); setImageFailed(false);
    }).catch(failure => { if (!abort.signal.aborted && own === generation.current) setError((failure as { code?: string }).code || 'readFailed'); })
      .finally(() => { if (!abort.signal.aborted && own === generation.current) setLoading(false); });
    return () => { abort.abort(); generation.current++; };
  }, [productId, sku, warehouse, warehouseReady, credential, refresh]);

  useEffect(() => {
    if (!row) { setPending(null); setSaveMissing(false); return; }
    setPending(stockCardPending(stockCardSaveKey(row.id, warehouse), row.id, warehouse)); setSaveMissing(false);
  }, [row?.id, warehouse, credential]);

  const changed = useCallback(() => {
    // Notify before our optional detail read: closing the card cannot hide a confirmed change from its caller.
    if (rowRef.current) callbacks.current.onChanged?.(rowRef.current);
    setStockStale(true); setStockRefresh(value => value + 1); setRefresh(value => value + 1);
  }, []);
  const refreshed = useCallback(() => {
    if (rowRef.current) callbacks.current.onChanged?.(rowRef.current);
    setStockRefresh(value => value + 1); setRefresh(value => value + 1);
  }, []);
  const setSectionNow = (next: StockCardSection) => {
    setEditor(null); setConflict(null); setTool(null); setSection(next); setVisited(previous => new Set([...previous, next]));
    setError(''); setMessage(''); if (content.current) content.current.scrollTop = 0;
  };
  const navigate = (next: StockCardSection) => { if (next !== section || editor || tool) request(() => setSectionNow(next)); };
  const discardAndContinue = () => {
    const next = guard; setGuard(null); setEditor(null); setConflict(null); setTool(null); setChildDirty({}); setChildVersion(value => value + 1);
    next?.run();
  };
  const edit = (kind: StockCardEditorKind, shop?: string) => {
    if (!row || mutationDisabled) return;
    request(() => { setTool(null); setEditor(stockCardDraft(row, kind, shop)); setError(''); setMessage(''); if (content.current) content.current.scrollTop = 0; });
  };
  const openTool = (value: Tool) => {
    if (mutationDisabled) return;
    request(() => { setEditor(null); setTool(value); setError(''); setMessage(''); if (content.current) content.current.scrollTop = 0; });
  };
  const closeTool = () => request(() => { setTool(null); setChildDirty({}); setChildVersion(value => value + 1); });
  const explain = (code: string) => t(`stockCard.${code}`, { defaultValue: t(`productEditor.errors.${code}`, { defaultValue: c('requestFailed') }) });

  const acceptSave = (result: ProductEditorSave, body: ProductEditorSaveBody) => {
    const item = result.results?.[0];
    if (result.request_id !== body.request_id || result.status !== 'completed' || result.external_write_enabled !== false ||
      result.results?.length !== 1 || item?.product_id !== body.changes[0].product_id || !['saved', 'conflict', 'invalid', 'missing'].includes(item.status) ||
      item.status === 'saved' && (!item.row || item.row.id !== item.product_id)) throw new Error('Invalid save response');
    const key = stockCardSaveKey(body.changes[0].product_id, body.warehouse_code || '');
    try { if (sessionStorage.getItem(key) === JSON.stringify(body)) sessionStorage.removeItem(key); } catch { /* A completed request can still be read again safely. */ }
    setPending(null); setSaveMissing(false);
    if (item.status === 'saved' && item.row) {
      const updated: ProductEditorDetail = { ...rowRef.current!, ...item.row, audit: rowRef.current?.audit || [] };
      setRow(updated); setEditor(null); setMessage(c('saved')); changed();
    } else {
      if (item.status === 'conflict' && item.row) {
        const latest: ProductEditorDetail = { ...rowRef.current!, ...item.row, audit: rowRef.current?.audit || [] };
        setConflict(latest); setRow(latest);
      }
      setError(item.status === 'conflict' ? 'conflict' : item.errors?.[0]?.code || 'saveRejected');
    }
  };

  async function saveRequest(body: ProductEditorSaveBody, recover = false) {
    if (sending.current || (!recover && readOnlyReason)) return;
    if (!rowRef.current || body.changes[0].product_id !== rowRef.current.id || body.warehouse_code !== (warehouse || null)) { setError('operationScopeChanged'); return; }
    const own = contextKey.current, access = accessRevision();
    const key = stockCardSaveKey(body.changes[0].product_id, body.warehouse_code || '');
    if (!recover) {
      try {
        const serialized = JSON.stringify(body); sessionStorage.setItem(key, serialized);
        if (sessionStorage.getItem(key) !== serialized) throw new Error('storage_unavailable');
      } catch { setError('storageUnavailable'); return; }
    }
    sending.current = true; setBusy(true); setPending(body); setSaveMissing(false); setError(''); setMessage('');
    try {
      const result = recover ? await getEditorSave(body.request_id) : await saveEditorProducts(body);
      if (own === contextKey.current && access === accessRevision()) acceptSave(result, body);
    } catch (failure) {
      if (own !== contextKey.current || access !== accessRevision()) return;
      const failed = failure as { code?: string; status?: number };
      if (recover && failed.code === 'product_editor_save_missing') setSaveMissing(true);
      else {
        const definitive = !recover && !!failed.code && !!failed.status && failed.status >= 400 && failed.status < 500 && failed.status !== 429 && failed.code !== 'product_editor_request_reused';
        if (definitive) { if (sessionStorage.getItem(key) === JSON.stringify(body)) sessionStorage.removeItem(key); setPending(null); }
        setError(failed.code || 'saveUncertain');
      }
    } finally { if (own === contextKey.current && access === accessRevision()) { sending.current = false; setBusy(false); } }
  }
  const save = () => {
    if (!editor || !draftDirty || mutationDisabled || sending.current || guard) return;
    const { values, kind } = editor;
    if (kind === 'product') {
      if (!values.name.trim()) { setError('nameRequired'); return; }
      if (values.image_url && (!stockCardSafeUrl(values.image_url) || !values.image_url.startsWith('https://'))) { setError('invalidImageUrl'); return; }
      if (values.min_quantity && !/^\d+$/.test(values.min_quantity)) { setError('invalidQuantity'); return; }
    }
    if (kind === 'prices') {
      if ([values.sale_price_gross, values.vat_rate, ...editor.base.shops.filter(shop => values[`${shop.shop_code}_inherit`] !== 'true').map(shop => values[`${shop.shop_code}_price`])].some(value => value && !decimal(value, 2))) { setError('invalidPrice'); return; }
    }
    if (kind === 'links' && values.restore_links !== 'true' && [values.shop_url, values.shop_admin_url].some(value => value && !stockCardSafeUrl(value))) { setError('invalidUrl'); return; }
    const body: ProductEditorSaveBody = { request_id: crypto.randomUUID(), warehouse_code: warehouse || null, confirmed: true, changes: [stockCardChange(editor)] };
    void saveRequest(body);
  };
  async function refreshAttributes() {
    if (!row || mutationDisabled || sending.current) return;
    sending.current = true; setBusy(true); setError(''); setMessage('');
    const own = contextKey.current, access = accessRevision();
    try {
      await refreshEditorAttributes(row.id);
      if (own === contextKey.current && access === accessRevision()) { setMessage(c('attributesRefreshed')); changed(); }
    } catch (failure) { if (own === contextKey.current) setError((failure as { code?: string }).code || 'requestFailed'); }
    finally { if (own === contextKey.current) { sending.current = false; setBusy(false); } }
  }
  async function refreshShopLinks(shop: string) {
    if (!row || mutationDisabled || sending.current || dirty) return;
    sending.current = true; setBusy(true); setError(''); setMessage('');
    const own = contextKey.current;
    try {
      await refreshProductLinkMetadata(shop, [row.id]);
      if (own === contextKey.current) { setMessage(c('linksRefreshed')); changed(); }
    } catch (failure) { if (own === contextKey.current) setError((failure as { code?: string }).code || 'requestFailed'); }
    finally { if (own === contextKey.current) { sending.current = false; setBusy(false); } }
  }
  const rebaseDraft = () => {
    if (!conflict || !editor) return;
    const original = stockCardDraft(editor.base, editor.kind, editor.shop).values;
    const latest = stockCardDraft(conflict, editor.kind, editor.shop);
    for (const [key, value] of Object.entries(editor.values)) if (value !== original[key]) latest.values[key] = value;
    setEditor(latest); setConflict(null); setError(''); setMessage(c('reviewRebased'));
  };

  const number = (value: string | null | undefined) => value == null ? c('unknown') : Number.isFinite(Number(value)) ? new Intl.NumberFormat(i18n.language, { maximumFractionDigits: 3 }).format(Number(value)) : value;
  const money = (value: string | null | undefined) => value == null ? c('unknown') : new Intl.NumberFormat(i18n.language, { style: 'currency', currency: 'EUR', maximumFractionDigits: 4 }).format(Number(value));
  const shopName = (value: string) => value === 'biketrek' ? 'BIKETREK' : value === 'xtrek' ? 'xTrek' : value;
  const external = (url: string | null | undefined, label: string, className = '', testId?: string) => {
    const safe = stockCardSafeUrl(url);
    return safe ? <a className={`stock-card__external ${className}`} href={safe} target="_blank" rel="noopener noreferrer" data-testid={testId}>{label}<ExternalLink size={13} aria-hidden="true" /></a>
      : <span className="stock-card__muted">{c('linkUnavailable')}</span>;
  };
  const suppliers = row?.suppliers || [];
  const supplier = suppliers.find(value => value.is_primary && value.is_active) || suppliers.find(value => value.is_active) || suppliers[0];
  const effectiveWarehouse = row?.warehouse_context?.code || warehouse;
  const warehouseName = row?.warehouse_context?.name || options?.warehouses.find(value => value.code === warehouse)?.name || c('noWarehouse');
  const imageUrl = stockCardSafeUrl(row?.image_url);
  const unitLabel = row ? t(`productEditor.units.${row.stock_unit}`) : '';
  const editorTitle = editor?.kind === 'links' ? c('editLinksFor', { shop: shopName(editor.shop || '') }) : c(editor?.kind === 'prices' ? 'editPrice' : 'editProduct');
  const displayValue = (value: unknown) => value == null || value === '' ? c('notSet') : typeof value === 'boolean' ? c(value ? 'yes' : 'no') : typeof value === 'object' ? JSON.stringify(value) : String(value);
  const mutationButton = (label: string, run: () => void, testId?: string) => !readOnlyReason && <button type="button" className="stock-card__button" data-testid={testId} disabled={mutationDisabled || dirty} onClick={run}><Pencil size={14} aria-hidden="true" />{label}</button>;
  const showFifo = !!row && row.item_type !== 'service' && !!effectiveWarehouse && (visited.has('prices') || visited.has('history'));

  return createPortal(<div className="stock-card-backdrop" data-testid="stock-card-backdrop" onMouseDown={event => { if (event.target === event.currentTarget) state.current.requestClose(); }}>
    <section className="stock-card" ref={dialog} role="dialog" aria-modal="true" aria-labelledby={`${uid}-title`} tabIndex={-1} data-testid="stock-card">
      <header className="stock-card__header">
        <div className="stock-card__image" data-testid="stock-card-image">{imageUrl && !imageFailed ? <img src={imageUrl} alt={row?.common.name || ''} onError={() => setImageFailed(true)} /> : <ImageIcon size={32} aria-label={c('noImage')} />}</div>
        <div className="stock-card__identity"><div className="stock-card__eyebrow">{c('title')}{row && <span className={`stock-card__badge ${row.is_active ? 'is-active' : ''}`}>{c(row.is_active ? 'active' : 'inactive')}</span>}</div>
          <h2 id={`${uid}-title`}>{row?.common.name || (loading ? c('loading') : c('title'))}</h2>
          <div className="stock-card__identifiers"><span>{c('hubId')} <strong>{row?.id ?? '—'}</strong></span><span>SKU <strong className="stock-card__mono" data-testid="stock-card-sku">{row?.sku || sku || '—'}</strong></span>
            {row?.shops.filter(shop => ['biketrek', 'xtrek'].includes(shop.shop_code)).map(shop => <span key={shop.shop_code}>{shopName(shop.shop_code)} <strong className="stock-card__mono">{shop.mapping?.code || (shop.shared_stock ? c('multipleMappings') : c('notLinked'))}</strong></span>)}
          </div>
        </div>
        <button type="button" className="stock-card__close" data-testid="stock-card-close" disabled={isBusy} onClick={() => state.current.requestClose()} aria-label={c('close')}><X size={22} /></button>
      </header>
      <div className="stock-card__summary" data-testid="stock-card-summary">
        <label className="stock-card__warehouse"><span>{c('warehouse')}</span><select aria-label={c('warehouse')} data-testid="stock-card-warehouse" value={warehouse} disabled={isBusy || !options || loading}
          onChange={event => { const value = event.target.value; request(() => { warehouseSelectionKnown.current = true; setWarehouse(value); setRow(null); setEditor(null); setConflict(null); setPending(null); setSaveMissing(false); setTool(null); setChildDirty({}); setChildVersion(current => current + 1); setError(''); setMessage(''); }); }}>
          <option value="">{c('noWarehouse')}</option>{options?.warehouses.map(value => <option key={value.code} value={value.code}>{value.name}</option>)}
        </select>{stockStale && <small className="stock-card__muted" role="status">{c('refreshingStock')}</small>}</label>
        {([['physical', 'qty_on_hand'], ['reserved', 'qty_reserved'], ['available', 'qty_available']] as const).map(([label, field]) => <div className={`stock-card__metric ${label === 'available' ? 'stock-card__metric--available' : ''}`} key={label}>
          <span>{c(label)}</span><strong data-testid={`stock-card-${label}`}>{row?.item_type === 'service' ? '—' : row?.stock.known && !stockStale ? number(row.stock[field]) : c('unknown')}<small>{row?.stock.known && !stockStale && row.stock[field] !== null ? unitLabel : ''}</small></strong>
        </div>)}
      </div>
      {readOnlyReason && <p className="stock-card__readonly" role="note">{readOnlyReason}</p>}
      <div className="stock-card__layout">
        <nav className="stock-card__navigation" aria-label={c('sectionsLabel')} role="tablist">
          {sections.map(({ id, icon: Icon }, index) => <button key={id} type="button" role="tab" id={`${uid}-tab-${id}`} aria-selected={section === id} aria-controls={`${uid}-panel`} tabIndex={section === id ? 0 : -1}
            className={section === id ? 'is-selected' : ''} data-testid={`stock-card-tab-${id}`} disabled={isBusy} onClick={() => navigate(id)} onKeyDown={event => {
              const next = event.key === 'Home' ? 0 : event.key === 'End' ? sections.length - 1 : ['ArrowDown', 'ArrowRight'].includes(event.key) ? (index + 1) % sections.length : ['ArrowUp', 'ArrowLeft'].includes(event.key) ? (index + sections.length - 1) % sections.length : -1;
              if (next >= 0) { event.preventDefault(); navigate(sections[next].id); if (!dirty) document.getElementById(`${uid}-tab-${sections[next].id}`)?.focus(); }
            }}><Icon size={18} aria-hidden="true" /><span>{c(`sections.${id}`)}</span><ChevronRight size={14} className="stock-card__nav-arrow" aria-hidden="true" /></button>)}
          <div className="stock-card__nav-note">{c('sharedCard')}</div>
        </nav>
        <div className="stock-card__content" ref={content} id={`${uid}-panel`} role="tabpanel" aria-labelledby={`${uid}-tab-${section}`} aria-busy={loading}>
          {guard && <div className="stock-card__guard-layer"><div className="stock-card__guard" ref={confirmation} role="alertdialog" aria-labelledby={`${uid}-guard-title`} aria-describedby={`${uid}-guard-help`} aria-modal="true" data-testid="stock-card-dirty-guard">
            <h3 id={`${uid}-guard-title`}>{c('unsavedTitle')}</h3><p id={`${uid}-guard-help`}>{c(pending ? 'pendingLeaveHelp' : 'unsavedHelp')}</p>
            <div className="stock-card__actions"><button type="button" className="stock-card__button stock-card__button--primary" data-testid="stock-card-keep-editing" onClick={() => setGuard(null)}>{c('keepEditing')}</button><button type="button" className="stock-card__button" data-testid="stock-card-discard" onClick={discardAndContinue}>{c(pending ? 'leave' : 'discard')}</button></div>
          </div></div>}
          {!hubUnlocked() && <p className="stock-card__note">{c('unlock')}</p>}
          {error && <div className="stock-card__alert" role="alert"><p>{explain(error)}</p>{(!row || stockStale) && <button type="button" className="stock-card__button" data-testid="stock-card-reload" disabled={isBusy || loading} onClick={() => { setError(''); if (!options) setOptionsRetry(value => value + 1); setRefresh(value => value + 1); }}>{c('retry')}</button>}</div>}
          {message && <p className="stock-card__success" role="status"><Check size={16} aria-hidden="true" />{message}</p>}
          {conflict && editor && <div className="stock-card__note" data-testid="stock-card-conflict"><h4>{c('conflictReview')}</h4><p>{c('conflictHelp')}</p><div className="stock-card__conflict-table"><table><thead><tr><th>{c('field')}</th><th>{c('latestValue')}</th><th>{c('yourValue')}</th></tr></thead><tbody>
            {Object.entries(editor.values).filter(([key, value]) => value !== stockCardDraft(editor.base, editor.kind, editor.shop).values[key]).map(([key, value]) => <tr key={key}><th>{t(`stockCard.fields.${key}`, { defaultValue: key })}</th><td>{displayValue(stockCardDraft(conflict, editor.kind, editor.shop).values[key])}</td><td>{displayValue(value)}</td></tr>)}
          </tbody></table></div><div className="stock-card__actions"><button type="button" className="stock-card__button" data-testid="stock-card-rebase" onClick={rebaseDraft}>{c('rebaseDraft')}</button><button type="button" className="stock-card__button" data-testid="stock-card-use-latest" onClick={() => request(() => { setEditor(null); setConflict(null); setError(''); })}>{c('useLatest')}</button></div></div>}
          {pending && <div className="stock-card__note" data-testid="stock-card-save-recovery"><p>{c('saveUncertain')}</p><div className="stock-card__actions"><button className="stock-card__button" type="button" disabled={isBusy || loading || !row} data-testid="stock-card-recover" onClick={() => void saveRequest(pending, true)}>{c('readSaveResult')}</button>{saveMissing && !readOnlyReason && <button className="stock-card__button" type="button" disabled={isBusy || loading || !row} data-testid="stock-card-retry-save" onClick={() => void saveRequest(pending)}>{c('retrySameSave')}</button>}</div></div>}
          {loading && !row && <p className="stock-card__loading" role="status">{c('loading')}</p>}
          {row && <>
            {editor && <><div className="stock-card__section-heading"><h3>{editorTitle}</h3></div><StockCardEditor draft={editor} disabled={isBusy || !!pending || !!readOnlyReason} onChange={(key, value) => { setEditor(previous => previous ? { ...previous, values: { ...previous.values, [key]: value } } : null); setError(''); }} /><p className="stock-card__note">{c('saveLocalHelp')}</p></>}
            <div hidden={!!editor || !!tool || section !== 'overview'}>
              <div className="stock-card__section-heading"><h3>{c('productData')}</h3>{mutationButton(c('edit'), () => edit('product'), 'stock-card-edit-product')}</div>
              <dl className="stock-card__details" data-testid="stock-card-product-data">
                <div><dt>{c('supplier')}</dt><dd data-testid="stock-card-overview-supplier">{supplier?.supplier_name || c('notLinked')}{supplier && !supplier.is_active && <span className="stock-card__badge stock-card__supplier-status">{c('inactiveSupplierSource')}</span>}<span className="stock-card__detail-secondary">{external(supplier?.product_url, c('supplierProduct'), '', 'stock-card-supplier-link')}</span></dd></div>
                <div><dt>{c('brand')}</dt><dd>{row.common.brand || c('notSet')}</dd></div>
                <div><dt>{c('ean')}</dt><dd className="stock-card__mono">{row.eans.length ? row.eans.map(value => <span className="stock-card__line" key={value}>{value}</span>) : c('notSet')}</dd></div>
                <div><dt>{c('supplierCode')}</dt><dd className="stock-card__mono">{supplier?.supplier_sku || row.supplier_codes.map(value => value.code).join(', ') || c('notSet')}</dd></div>
                <div><dt>{c('location')}</dt><dd>{row.warehouse?.location || c('notSet')}</dd></div>
                <div><dt>{c('typeUnit')}</dt><dd>{c(row.item_type === 'service' ? 'service' : 'goods')} / {unitLabel}</dd></div>
              </dl>
              <section className="stock-card__block"><h4>{c('description')}</h4><p className="stock-card__description">{stockCardDescription(row.common.description) || c('noDescription')}</p></section>
              <div className="stock-card__two-columns"><section className="stock-card__block"><h4>{c('internalNote')}</h4><p className="stock-card__description">{row.common.internal_note || c('notSet')}</p></section><section className="stock-card__block"><h4>{c('attributes')}</h4>{row.attributes.length ? <dl className="stock-card__attribute-list">{row.attributes.map((attribute, index) => <React.Fragment key={`${attribute.name}-${index}`}><dt>{attribute.name}</dt><dd>{attribute.value}</dd></React.Fragment>)}</dl> : <p>{c('notSet')}</p>}</section></div>
              {row.supplier_availability && <div className="stock-card__note"><strong>{c('supplierAvailability')}</strong><p>{row.supplier_availability.label || c('unknown')}{!row.supplier_availability.fresh && ` · ${c('availabilityNotFresh')}`}</p></div>}
              {suppliers.length > 1 && <details className="stock-card__block"><summary>{c('allSuppliers', { count: suppliers.length })}</summary><ul className="stock-card__supplier-list">{suppliers.map(value => <li key={`${value.supplier_id}-${value.supplier_product_id}`}><strong>{value.supplier_name}</strong><span className="stock-card__mono">{value.supplier_sku}</span>{external(value.product_url, c('supplierProduct'))}{!value.is_active && <small>{c('inactive')}</small>}</li>)}</ul></details>}
              {!readOnlyReason && <details className="stock-card__block stock-card__advanced"><summary>{c('productActions')}</summary><div className="stock-card__actions">
                <button type="button" className="stock-card__button" disabled={mutationDisabled || dirty} onClick={() => openTool('supplier')}>{c('manageSupplier')}</button>
                <button type="button" className="stock-card__button" data-testid="stock-card-fill-eans" disabled={mutationDisabled || dirty} onClick={() => openTool('eans')}>{c('fillEans')}</button>
                <span className="action-control"><button type="button" className="stock-card__button" data-testid="stock-card-refresh-attributes" disabled={mutationDisabled || dirty} onClick={() => void refreshAttributes()}><RefreshCw size={14} aria-hidden="true" />{c('refreshAttributes')}</button><ActionScope effects={['hub-write']} /></span>
                <button type="button" className="stock-card__button" data-testid="sku-migration-open" disabled={mutationDisabled || dirty} onClick={() => openTool('sku')}>{c('skuMigration')}</button>
              </div></details>}
            </div>
            <div hidden={!!editor || !!tool || section !== 'stock'}>
              <div className="stock-card__section-heading"><h3>{c('stockInWarehouse', { warehouse: warehouseName })}</h3></div>
              <dl className="stock-card__details stock-card__details--compact"><div><dt>{c('location')}</dt><dd>{row.warehouse?.location || c('notSet')}</dd></div><div><dt>{c('minimumStock')}</dt><dd>{row.warehouse?.min_quantity == null ? c('notSet') : `${number(row.warehouse.min_quantity)} ${unitLabel}`}</dd></div><div><dt>{c('quarantined')}</dt><dd>{row.stock.known ? `${number(row.stock.qty_quarantined)} ${unitLabel}` : c('unknown')}</dd></div><div><dt>{c('stockState')}</dt><dd>{c(row.item_type === 'service' ? 'nonstock' : row.stock.known ? 'confirmedStock' : 'unconfirmedStock')}</dd></div></dl>
              {row.item_type === 'service' ? <p className="stock-card__note">{c('nonstockHelp')}</p> : !effectiveWarehouse ? <p className="stock-card__note">{c('chooseWarehouse')}</p> : <>
                <p className="stock-card__note">{c('stockHelp')}</p>
                {!readOnlyReason && visited.has('stock') && <section className="stock-card__block"><h4>{c('adjustStock')}</h4><StockAdjustmentPanel key={`adjustment-${row.id}-${effectiveWarehouse}-${childVersion}`} productId={row.id} sku={row.sku} warehouseCode={effectiveWarehouse} stockUnit={row.stock_unit} embedded refreshKey={stockRefresh} onReadRefresh={refreshed} onChanged={changed} onDirtyChange={dirtyCallbacks.adjustment} onBusyChange={busyCallbacks.adjustment} /></section>}
              </>}
            </div>
            <div hidden={!!editor || !!tool || section !== 'prices'}>
              <div className="stock-card__section-heading"><h3>{c('sellingPrices')}</h3>{mutationButton(c('editPrice'), () => edit('prices'), 'stock-card-edit-price')}</div>
              <div className="stock-card__selling-price"><div><span>{c('sellingPriceGross')}</span><strong>{money(row.variant.sale_price_gross)}</strong><small>{row.variant.vat_rate == null ? c('vatUnknown') : c('vatIncluded', { rate: number(row.variant.vat_rate) })}</small></div><p>{c('priceFlowHelp')}</p></div>
              {!readOnlyReason && visited.has('prices') && <ProductPricePublicationPanel row={row} onChanged={changed} onBusyChange={busyCallbacks.price} />}
              <div className="stock-card__section-heading stock-card__section-heading--spaced"><h3>{c('fifoCosts')}</h3></div><p className="stock-card__muted">{c('fifoHelp')}</p>
              {row.item_type === 'service' && <p className="stock-card__note">{c('nonstockHelp')}</p>}
            </div>
            <div hidden={!!editor || !!tool || section !== 'shops'}>
              <div className="stock-card__section-heading"><h3>{c('shopsTitle')}</h3>{mutationButton(c('manageMappings'), () => openTool('mapping'), 'stock-card-mappings')}</div>
              <p className="stock-card__muted">{c('shopsHelp')}</p>
              <div className="stock-card__shop-grid">{row.shops.filter(shop => ['biketrek', 'xtrek'].includes(shop.shop_code)).map(shop => <article className="stock-card__shop" key={shop.shop_code}>
                <header><h4><Store size={18} aria-hidden="true" />{shopName(shop.shop_code)}</h4><span className={`stock-card__badge ${shop.mapped ? 'is-active' : ''}`}>{c(shop.mapped ? 'linked' : 'notLinked')}</span></header>
                <dl><div><dt>{c('shopCode')}</dt><dd className="stock-card__mono">{shop.mapping?.code || (shop.shared_stock ? c('multipleMappings') : c('notSet'))}</dd></div>
                  {shop.mapping && <><div><dt>{c('parentCode')}</dt><dd className="stock-card__mono" data-testid={`stock-card-parent-${shop.shop_code}`}>{shop.mapping.parent_code || c('notSet')}</dd></div><div><dt>{c('nativeProductId')}</dt><dd className="stock-card__mono" data-testid={`stock-card-native-${shop.shop_code}`}>{shop.mapping.external_product_id || c('notSet')}{shop.mapping.is_variant && <span className="stock-card__line">{c('nativeVariantId')}: {shop.mapping.external_variant_id || c('notSet')}</span>}</dd></div></>}
                  <div><dt>{c('sellingPriceGross')}</dt><dd>{money(shop.effective.sale_price_gross)}</dd></div><div><dt>{c('visibility')}</dt><dd>{shop.effective.visible == null ? c('unknown') : c(shop.effective.visible ? 'visible' : 'hidden')}</dd></div><div><dt>{c('publicationState')}</dt><dd>{t(`productEditor.state.${shop.state}`, { defaultValue: c(`shopState.${shop.state}`) })}</dd></div></dl>
                <div className="stock-card__shop-links">{external(shop.shop_url, c('eshop'))}{external(shop.shop_admin_url, c('administration'))}</div>
                {!shop.mapped && <a className="stock-card__external stock-card__button" data-testid={`stock-card-prepare-listing-${shop.shop_code}`} href="/product-import" target="_blank" rel="noopener noreferrer">{c('prepareListing')}<ExternalLink size={13} aria-hidden="true" /></a>}
                {!!shop.sale_mappings?.length && <details><summary>{c('salesMappings', { count: shop.sale_mappings.length })}</summary><ul className="stock-card__mapping-list">{shop.sale_mappings.map(mapping => <li key={mapping.id}><strong className="stock-card__mono">{mapping.code}</strong><small>{c('parentCode')}: {mapping.parent_code || c('notSet')}</small><small>{c('nativeProductId')}: {mapping.external_product_id || c('notSet')}{mapping.is_variant && ` · ${c('nativeVariantId')}: ${mapping.external_variant_id || c('notSet')}`}</small><small>{c('stockFactor', { factor: mapping.sale_to_stock_factor })}</small>{external(mapping.shop_url, c('eshop'))}{external(mapping.shop_admin_url, c('administration'))}</li>)}</ul></details>}
                {!readOnlyReason && <div className="stock-card__actions"><button type="button" className="stock-card__button" data-testid={`stock-card-edit-links-${shop.shop_code}`} disabled={mutationDisabled || dirty} onClick={() => edit('links', shop.shop_code)}>{c('editLinks')}</button>
                  <button type="button" className="stock-card__button" data-testid={`stock-card-publish-fields-${shop.shop_code}`} disabled={mutationDisabled || dirty || !shop.mapped || shop.shared_stock} onClick={() => openTool(`publication-${shop.shop_code}` as Tool)}>{c('publishFields')}</button>
                  {shop.mapped && <span className="action-control"><button type="button" className="stock-card__button" data-testid={`stock-card-refresh-links-${shop.shop_code}`} disabled={mutationDisabled || dirty} onClick={() => void refreshShopLinks(shop.shop_code)}>{c('refreshShopLinks')}</button><ActionScope effects={['upgates-read', 'hub-write']} shop={shop.shop_code} /></span>}</div>}
              </article>)}</div>
              {!row.shops.length && <p className="stock-card__note">{c('noShops')}</p>}
            </div>
            <div hidden={!!editor || !!tool || section !== 'history'}>
              <div className="stock-card__section-heading"><h3>{c('cardHistory')}</h3></div>
              <div className="stock-card__actions stock-card__history-links">
                <a className="stock-card__external stock-card__button" data-testid="stock-card-all-movements" href={`/stock/movements?sku=${encodeURIComponent(row.sku)}`} target="_blank" rel="noopener noreferrer">{c('allMovements')}<ExternalLink size={13} aria-hidden="true" /></a>
                <a className="stock-card__external stock-card__button" data-testid="stock-card-product-trace" href={`/product-trace?product_id=${row.id}`} target="_blank" rel="noopener noreferrer">{c('productTrace')}<ExternalLink size={13} aria-hidden="true" /></a>
              </div>
              {!row.audit.length ? <p className="stock-card__note">{c('noAudit')}</p> : <ol className="stock-card__audit">{row.audit.map(audit => <li key={audit.id}><details><summary><History size={15} aria-hidden="true" /><time dateTime={audit.created_at}>{new Date(audit.created_at).toLocaleString(i18n.language)}</time><span>{c('cardChanged')}</span></summary><dl>{stockCardAuditChanges(audit.before, audit.after).map(change => <div key={change.field}><dt>{t(`stockCard.fields.${change.field}`, { defaultValue: change.field })}</dt><dd><del>{displayValue(change.before)}</del><span aria-hidden="true"> → </span><ins>{displayValue(change.after)}</ins></dd></div>)}</dl></details></li>)}</ol>}
              <div className="stock-card__section-heading stock-card__section-heading--spaced"><h3>{c('stockHistory')}</h3></div>
              {row.item_type === 'service' && <p className="stock-card__note">{c('nonstockHelp')}</p>}
            </div>
            {showFifo && <div hidden={!!editor || !!tool || !['prices', 'history'].includes(section)}>
              <FifoPanel key={`fifo-${row.id}-${effectiveWarehouse}-${childVersion}`} productId={row.id} warehouseCode={effectiveWarehouse} embedded readOnly={!!readOnlyReason}
                view={section === 'history' ? 'history' : 'costs'} refreshKey={stockRefresh} onReadRefresh={refreshed} onChanged={changed} onDirtyChange={dirtyCallbacks.fifo} onBusyChange={busyCallbacks.fifo} />
            </div>}
            {!readOnlyReason && tool && <div key={`${tool}-${row.id}-${childVersion}`}>
              <button type="button" className="stock-card__back" disabled={isBusy} onClick={closeTool}><ArrowLeft size={16} aria-hidden="true" />{c('backToSection')}</button>
              {(tool === 'supplier' || tool === 'eans') && <ProductSupplierLinksPanel product={row} fillEans={tool === 'eans'} embedded onClose={closeTool} onChanged={changed} onDirtyChange={dirtyCallbacks.supplier} onBusyChange={busyCallbacks.supplier} />}
              {tool === 'mapping' && <ProductShopLinksPanel rows={[row]} embedded onClose={closeTool} onChanged={changed} onDirtyChange={dirtyCallbacks.mapping} onBusyChange={busyCallbacks.mapping} />}
              {tool === 'sku' && <SkuMigrationPreviewDialog product={row} embedded onClose={closeTool} onDirtyChange={dirtyCallbacks.sku} />}
              {(tool === 'publication-biketrek' || tool === 'publication-xtrek') && <ProductPublicationPanel rows={[row]} shop={tool === 'publication-biketrek' ? 'biketrek' : 'xtrek'} embedded onClose={closeTool} onChanged={changed} onBusyChange={busyCallbacks.publication} />}
            </div>}
          </>}
        </div>
      </div>
      <footer className="stock-card__footer"><span className="stock-card__footer-status" role="status">{isBusy ? c('working') : loading ? c('loading') : pending ? c('savePending') : dirty ? c('unsaved') : row ? c('savedInHub') : c('title')}</span><div className="stock-card__actions">
        {editor ? <><button type="button" className="stock-card__button" disabled={isBusy} data-testid="stock-card-cancel-edit" onClick={() => request(() => { setEditor(null); setConflict(null); setError(''); })}>{c('cancel')}</button><span className="action-control"><button type="button" className="stock-card__button stock-card__button--primary" data-testid="stock-card-save" disabled={mutationDisabled || !draftDirty} onClick={save}>{c('saveInHub')}</button><ActionScope effects={['hub-write']} /></span></>
          : <button type="button" className="stock-card__button" disabled={isBusy} onClick={() => state.current.requestClose()}>{c('close')}</button>}
      </div></footer>
    </section>
  </div>, document.body);
}

export default StockCard;
