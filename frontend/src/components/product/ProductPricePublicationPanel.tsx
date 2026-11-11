import React, { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { useTranslation } from 'react-i18next';
import { Check, ChevronLeft, RefreshCw, Send } from 'lucide-react';
import { Button } from '../ui/Button.new';
import { ActionScope } from '../ui/ActionScope';
import { accessRevision, subscribeAccess } from '../../api/access';
import { ProductEditorDetail, ProductEditorRow, ProductPublication, previewProductPublication,
  productPublicationHistory, readProductPublication, resolveProductPublication, sendProductPublication } from '../../api/productEditor';
import './ProductPricePublicationPanel.css';

type ShopCode = 'biketrek' | 'xtrek';
const shops: ShopCode[] = ['biketrek', 'xtrek'];
const shopName = (shop: ShopCode) => shop === 'biketrek' ? 'BIKETREK' : 'xTrek';
const pendingStates = new Set(['sending', 'uncertain']);
// A lost response can precede the durable sending state. Keep only operation IDs
// in this tab so a remount or account change cannot replay that same request.
// Authorized history reads remain the source of all product/operation data.
const unconfirmedSendIds = new Set<string>();
// These structured 4xx results are raised before the durable send intent in the
// existing publisher. A lost/unknown response is never treated as a rejection.
const preflightRejections = new Set(['product_publication_expired', 'product_publication_target_changed',
  'product_publication_remote_changed', 'product_editor_changed', 'product_publication_identity_changed',
  'product_publication_mapping_missing', 'product_publication_shared_sales', 'product_publication_connection_missing']);
const priceOnly = (operation: ProductPublication) => !operation.workflow &&
  operation.fields.length === 1 && operation.fields[0] === 'sale_price_gross';
const decimal = (value: unknown) => typeof value === 'string' && /^\d+(?:\.\d+)?$/.test(value) && Number.isFinite(Number(value));
type TargetState = {
  operation: ProductPublication | null; error: string; uncertain: boolean;
  settled: boolean; note: string; completedSource?: string; rejected?: boolean;
};
const emptyTarget = (): TargetState => ({ operation: null, error: '', uncertain: false, settled: false, note: '' });
const emptyTargets = (): Record<ShopCode, TargetState> => ({ biketrek: emptyTarget(), xtrek: emptyTarget() });
type Prepared = { source: string; targets: { shop: ShopCode; id: string | null }[]; attempted: boolean };

export interface ProductPricePublicationPanelProps {
  row: ProductEditorDetail | ProductEditorRow;
  showSavedPrice?: boolean;
  onClose?: () => void;
  onChanged?: () => void;
  onBusyChange?: (busy: boolean) => void;
}

/** Embedded price-only publishing. Saving a Hub price remains the card's job. */
export function ProductPricePublicationPanel({ row, showSavedPrice = true, onClose, onChanged, onBusyChange }: ProductPricePublicationPanelProps) {
  const { t, i18n } = useTranslation();
  const credential = useSyncExternalStore(subscribeAccess, accessRevision);
  const source = JSON.stringify([row.id, row.revision, row.snapshot_hash, row.sku, row.variant,
    row.shops.map(shop => [shop.shop_code, shop.effective, shop.mapping, shop.shared_stock])]);
  const currentSource = useRef(source); currentSource.current = source;
  const context = useRef(0), busyRef = useRef(true);
  const busyCallback = useRef(onBusyChange); busyCallback.current = onBusyChange;
  const [activity, setActivity] = useState<string | null>('history');
  const [historyReady, setHistoryReady] = useState(false);
  const [history, setHistory] = useState<ProductPublication[]>([]);
  const [targets, setTargets] = useState<Record<ShopCode, TargetState>>(emptyTargets);
  const [selected, setSelected] = useState<ShopCode[]>([]);
  const [prepared, setPrepared] = useState<Prepared | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [loadError, setLoadError] = useState('');
  const [reload, setReload] = useState(0);
  const busy = activity !== null;
  const unresolved = (target: TargetState) => target.uncertain || !!target.operation && pendingStates.has(target.operation.state);
  const updateTarget = (shop: ShopCode, changes: Partial<TargetState>) => setTargets(previous => ({ ...previous, [shop]: { ...previous[shop], ...changes } }));
  const setBusy = (value: string | null) => { busyRef.current = value !== null; setActivity(value); };
  const matchesCurrentPrice = (operation: ProductPublication, shop: ShopCode) => {
    const price = row.shops.find(item => item.shop_code === shop)?.effective.sale_price_gross;
    return priceOnly(operation) && operation.revision === row.revision && decimal(price) &&
      decimal(operation.after.sale_price_gross) && Number(price) === Number(operation.after.sale_price_gross);
  };
  const record = (shop: ShopCode, operation: ProductPublication, completedSource?: string) => {
    if (!pendingStates.has(operation.state) && operation.state !== 'ready') unconfirmedSendIds.delete(operation.id);
    const successfulSource = operation.state === 'completed' ? completedSource || (matchesCurrentPrice(operation, shop) ? source : undefined) : undefined;
    updateTarget(shop, { operation, error: '', uncertain: unconfirmedSendIds.has(operation.id), settled: false, note: '', rejected: false,
      ...(successfulSource ? { completedSource: successfulSource } : {}) });
    setHistory(previous => [operation, ...previous.filter(item => item.id !== operation.id)]);
  };
  const errorText = (code: string) => t(`productPublication.errors.${code}`, { defaultValue: t('stockCardPrice.failed') });
  const money = (value: unknown) => decimal(value)
    ? new Intl.NumberFormat(i18n.language, { style: 'currency', currency: 'EUR' }).format(Number(value))
    : t('stockCardPrice.unknown');

  useEffect(() => { busyCallback.current?.(busy); }, [busy]);
  useEffect(() => () => { busyCallback.current?.(false); }, []);
  useEffect(() => {
    const generation = ++context.current, controller = new AbortController();
    setHistory([]); setTargets(emptyTargets()); setSelected([]); setPrepared(null); setConfirmed(false);
    setHistoryReady(false); setLoadError(''); setBusy('history');
    productPublicationHistory(row.id, controller.signal).then(result => {
      if (controller.signal.aborted || generation !== context.current || credential !== accessRevision()) return;
      const items = result.items.filter(item => item.product_id === row.id && shops.includes(item.shop_code as ShopCode));
      const next = emptyTargets();
      for (const shop of shops) {
        const pending = items.find(item => item.shop_code === shop && (pendingStates.has(item.state) || unconfirmedSendIds.has(item.id)));
        const latest = pending || items.find(item => item.shop_code === shop && priceOnly(item) && item.state !== 'ready');
        if (latest) {
          if (!pendingStates.has(latest.state) && latest.state !== 'ready') unconfirmedSendIds.delete(latest.id);
          next[shop] = { ...emptyTarget(), operation: latest, uncertain: unconfirmedSendIds.has(latest.id),
            completedSource: latest.state === 'completed' && matchesCurrentPrice(latest, shop) ? source : undefined };
        }
      }
      setHistory(items); setTargets(next); setHistoryReady(true);
    }).catch((reason: any) => {
      if (!controller.signal.aborted && generation === context.current) setLoadError(reason.code || 'request_failed');
    }).finally(() => {
      if (!controller.signal.aborted && generation === context.current) setBusy(null);
    });
    return () => { context.current++; controller.abort(); };
  }, [source, credential, reload]);

  const unavailable = (shop: ShopCode): string | null => {
    if (unresolved(targets[shop])) return 'pendingTarget';
    if (targets[shop].completedSource === source) return 'alreadySent';
    const target = row.shops.find(item => item.shop_code === shop);
    if (target?.shared_stock) return 'sharedTarget';
    if (!target?.mapped || !target.mapping) return 'missingTarget';
    if (!decimal(target.effective.sale_price_gross)) return 'missingPrice';
    return null;
  };
  const isCurrent = (generation: number, access: number, savedSource: string) =>
    generation === context.current && access === accessRevision() && savedSource === currentSource.current;
  const validResult = (operation: ProductPublication, shop: ShopCode, id?: string) =>
    operation.product_id === row.id && operation.shop_code === shop && (!id || operation.id === id);
  const ready = !!prepared && prepared.source === source && !prepared.attempted && prepared.targets.length > 0 &&
    prepared.targets.every(({ shop, id }) => {
      const target = targets[shop], operation = target.operation;
      return !!id && operation?.id === id && operation.state === 'ready' && priceOnly(operation) &&
        operation.revision === row.revision && Date.parse(operation.expires_at) > Date.now() &&
        !unresolved(target) && !unconfirmedSendIds.has(id) && !unavailable(shop);
    });
  const attemptedResults = prepared?.attempted ? prepared.targets.map(({ shop, id }) =>
    ({ shop, operation: targets[shop].operation?.id === id ? targets[shop].operation : history.find(item => item.id === id) })) : [];
  const completedCount = attemptedResults.filter(item => item.operation?.state === 'completed').length;
  const partial = completedCount > 0 && completedCount < attemptedResults.length;

  async function preview() {
    if (busyRef.current || !historyReady || !selected.length || selected.some(shop => unavailable(shop))) return;
    const generation = context.current, access = accessRevision(), savedSource = source, requested = [...selected];
    const results: Prepared['targets'] = [];
    setBusy('preview'); setPrepared(null); setConfirmed(false);
    try {
      for (const shop of requested) {
        if (!isCurrent(generation, access, savedSource)) return;
        updateTarget(shop, { error: '', operation: null, uncertain: false, settled: false, note: '' });
        try {
          const operation = await previewProductPublication(row, shop, ['sale_price_gross']);
          if (!isCurrent(generation, access, savedSource)) return;
          if (!validResult(operation, shop) || !priceOnly(operation) || operation.revision !== row.revision || operation.state !== 'ready') throw new Error('Invalid price preview');
          record(shop, operation); results.push({ shop, id: operation.id });
        } catch (reason: any) {
          if (!isCurrent(generation, access, savedSource)) return;
          updateTarget(shop, { error: reason.code || 'request_failed' }); results.push({ shop, id: null });
        }
      }
      if (isCurrent(generation, access, savedSource)) setPrepared({ source: savedSource, targets: results, attempted: false });
    } finally { if (isCurrent(generation, access, savedSource)) setBusy(null); }
  }

  async function send() {
    if (busyRef.current || !confirmed || !ready || !prepared) return;
    const batch = prepared, generation = context.current, access = accessRevision(), savedSource = source;
    setBusy('send'); setConfirmed(false); setPrepared({ ...batch, attempted: true });
    let changed = false;
    try {
      for (const { shop, id } of batch.targets) {
        if (!id || !isCurrent(generation, access, savedSource)) return;
        // Every target has its own durable operation. The other shop's success
        // is never rolled back or submitted again after a partial result.
        unconfirmedSendIds.add(id); setActivity(`send-${shop}`);
        try {
          const operation = await sendProductPublication(id);
          if (!isCurrent(generation, access, savedSource)) return;
          if (!validResult(operation, shop, id) || !priceOnly(operation)) throw new Error('Invalid publication result');
          record(shop, operation, operation.state === 'completed' ? savedSource : undefined); changed = true;
        } catch (reason: any) {
          if (!isCurrent(generation, access, savedSource)) return;
          const rejected = reason.status >= 400 && reason.status < 500 && preflightRejections.has(reason.code);
          if (rejected) unconfirmedSendIds.delete(id);
          updateTarget(shop, { error: reason.code || 'request_failed', uncertain: !rejected, rejected });
        }
      }
    } finally {
      if (isCurrent(generation, access, savedSource)) {
        setSelected([]); setBusy(null);
        if (changed) onChanged?.();
      }
    }
  }

  async function recover(shop: ShopCode, resolve: boolean) {
    const target = targets[shop], operation = target.operation;
    if (busyRef.current || !operation || resolve && (!priceOnly(operation) || !pendingStates.has(operation.state) || !target.settled || target.note.trim().length < 10)) return;
    const generation = context.current, access = accessRevision(), savedSource = source;
    setBusy(`${resolve ? 'resolve' : 'read'}-${shop}`); updateTarget(shop, { error: '' });
    let changed = false;
    try {
      const result = resolve ? await resolveProductPublication(operation.id, target.note.trim()) : await readProductPublication(operation.id);
      if (!isCurrent(generation, access, savedSource)) return;
      if (!validResult(result, shop, operation.id)) throw new Error('Invalid publication result');
      record(shop, result); changed = result.state !== operation.state;
    } catch (reason: any) {
      if (isCurrent(generation, access, savedSource)) updateTarget(shop, { error: reason.code || 'request_failed' });
    } finally {
      if (isCurrent(generation, access, savedSource)) { setBusy(null); if (changed) onChanged?.(); }
    }
  }

  const changeSelection = (next: ShopCode[]) => { setSelected(next); setPrepared(null); setConfirmed(false); };
  return <section className="stock-card-price-publication" data-testid="price-publication" aria-label={t('stockCardPrice.title')} aria-busy={busy}>
    <header className="stock-card-price-header">
      <div><h3>{t('stockCardPrice.title')}</h3><p>{t('stockCardPrice.intro')}</p></div>
      {onClose && <Button type="button" variant="ghost" disabled={busy} onClick={onClose} icon={<ChevronLeft size={16} />}>{t('stockCardPrice.back')}</Button>}
    </header>
    {showSavedPrice && <div className="stock-card-price-saved"><span>{t('stockCardPrice.savedPrice')}</span><strong data-testid="price-publication-saved-price">{money(row.variant.sale_price_gross)}</strong><small>{t('stockCardPrice.inclVat')}</small></div>}
    {loadError && <div className="stock-card-price-warning" role="alert"><p>{errorText(loadError)}</p><p>{t('stockCardPrice.historyRequired')}</p><Button type="button" variant="secondary" disabled={busy} onClick={() => setReload(value => value + 1)}>{t('stockCardPrice.reload')}</Button></div>}
    <fieldset className="stock-card-price-targets" disabled={busy || !historyReady}>
      <legend>{t('stockCardPrice.chooseShops')}</legend>
      <div className="stock-card-price-target-grid">{shops.map(shop => {
        const value = row.shops.find(item => item.shop_code === shop), reason = unavailable(shop);
        return <label key={shop} className={`stock-card-price-target ${selected.includes(shop) ? 'is-selected' : ''} ${reason ? 'is-unavailable' : ''}`}>
          <div><input type="checkbox" data-testid={`price-publication-target-${shop}`} checked={selected.includes(shop)} disabled={busy || !historyReady || !!reason}
            onChange={event => changeSelection(event.target.checked ? shops.filter(code => selected.includes(code) || code === shop) : selected.filter(code => code !== shop))} /><strong>{shopName(shop)}</strong>
            {targets[shop].completedSource === source && <Check size={16} aria-hidden="true" />}</div>
          <span className="stock-card-price-target-value">{money(value?.effective.sale_price_gross)} <small>{t('stockCardPrice.inclVat')}</small></span>
          {value?.overrides.sale_price_gross != null && <small>{t('stockCardPrice.shopOverride')}</small>}
          {value?.mapping && <small className="stock-card-price-identity">{value.mapping.code}{value.mapping.is_variant && value.mapping.parent_code ? ` · ${t('stockCardPrice.variantOf', { code: value.mapping.parent_code })}` : ''}</small>}
          {reason && <small data-testid={`price-publication-unavailable-${shop}`}>{t(`stockCardPrice.${reason}`)}</small>}
        </label>;
      })}</div>
      <Button type="button" variant="ghost" size="sm" data-testid="price-publication-both" disabled={busy || !historyReady || shops.some(shop => unavailable(shop))} onClick={() => changeSelection([...shops])}>{t('stockCardPrice.both')}</Button>
    </fieldset>
    <div className="stock-card-price-actions"><Button type="button" data-testid="price-publication-preview" variant="secondary" icon={<RefreshCw size={16} />} disabled={busy || !historyReady || !selected.length || selected.some(shop => unavailable(shop))} onClick={preview}>{t('stockCardPrice.preview')}</Button><ActionScope effects={['upgates-read', 'hub-write']} /></div>
    {busy && <p className="stock-card-price-progress" role="status" data-testid="price-publication-progress">{t(activity?.startsWith('send') ? 'stockCardPrice.sending' : 'stockCardPrice.loading')}</p>}
    {prepared?.attempted && attemptedResults.length > 0 && <p role="status" className={partial ? 'stock-card-price-warning' : 'stock-card-price-result'} data-testid={partial ? 'price-publication-partial' : 'price-publication-result'}>
      {t(completedCount === attemptedResults.length ? 'stockCardPrice.allCompleted' : partial ? 'stockCardPrice.partial' : 'stockCardPrice.checkResults', { completed: completedCount, total: attemptedResults.length })}
    </p>}
    <div className="stock-card-price-results">{shops.map(shop => {
      const target = targets[shop], operation = target.operation, pending = unresolved(target);
      if (!operation && !target.error) return null;
      return <article key={shop} className={`stock-card-price-shop-result ${operation?.state === 'completed' ? 'is-completed' : pending ? 'is-pending' : ''}`} data-testid={`price-publication-result-${shop}`}>
        <header><h4>{shopName(shop)}</h4>{operation && <span className="stock-card-price-status" data-testid={`price-publication-state-${shop}`}>{target.rejected ? t('productPublication.states.rejected') : target.uncertain && operation.state === 'ready' ? t('stockCardPrice.resultUnknown') : t(`productPublication.states.${operation.state}`, { defaultValue: operation.state })}</span>}</header>
        {target.error && <p className="stock-card-price-error" role="alert">{errorText(target.error)}</p>}
        {operation && <>
          {!priceOnly(operation) ? <p>{t('stockCardPrice.otherOperation')}</p> : <>
            <div className="stock-card-price-change"><div><span>{t('stockCardPrice.remoteBefore')}</span><strong>{money(operation.before.sale_price_gross)}</strong></div><span className="stock-card-price-arrow" aria-hidden="true">→</span><div><span>{t('stockCardPrice.remoteAfter')}</span><strong>{money(operation.after.sale_price_gross)}</strong></div></div>
            <p className="stock-card-price-basis">{t('stockCardPrice.priceBasis')}</p>
            <details className="stock-card-price-details"><summary>{t('stockCardPrice.vatDetails')}</summary><div className="stock-card-price-table-scroll"><table><thead><tr><th>{t('stockCardPrice.detail')}</th><th>{t('stockCardPrice.before')}</th><th>{t('stockCardPrice.after')}</th></tr></thead><tbody>{(['vat', 'price_original', 'product_discount', 'price_sale'] as const).map(field => <tr key={field}><th>{t(`stockCardPrice.fields.${field}`)}</th>{(['before', 'after'] as const).map(side => <td key={side}>{field === 'vat' || field === 'product_discount' ? decimal(operation[side][field]) ? `${operation[side][field]} %` : t('stockCardPrice.unknown') : money(operation[side][field])}</td>)}</tr>)}</tbody></table></div><p>{t('stockCardPrice.discountHelp')}</p></details>
          </>}
          {operation.error && <p role="alert" className="stock-card-price-error">{errorText(typeof operation.error === 'string' ? operation.error : operation.error.code || 'request_failed')}</p>}
          {operation.state === 'ready' && !pending && <small>{t('stockCardPrice.expires', { date: new Date(operation.expires_at).toLocaleString(i18n.language) })}</small>}
          {pending && <div className="stock-card-price-recovery"><p>{t('stockCardPrice.uncertain')}</p><Button type="button" variant="secondary" data-testid={`price-publication-read-${shop}`} disabled={busy} onClick={() => recover(shop, false)}>{t('stockCardPrice.readResult')}</Button><ActionScope effects={['hub-read']} />
            {priceOnly(operation) && pendingStates.has(operation.state) && <details><summary>{t('stockCardPrice.resolve')}</summary><p>{t('stockCardPrice.resolveHelp')}</p>
              <label className="stock-card-price-check"><input type="checkbox" data-testid={`price-publication-settled-${shop}`} checked={target.settled} disabled={busy} onChange={event => updateTarget(shop, { settled: event.target.checked })} />{t('stockCardPrice.settled')}</label>
              <label className="stock-card-price-note">{t('stockCardPrice.note')}<textarea data-testid={`price-publication-note-${shop}`} value={target.note} maxLength={1000} disabled={busy} onChange={event => updateTarget(shop, { note: event.target.value })} /></label>
              <Button type="button" variant="secondary" data-testid={`price-publication-resolve-${shop}`} disabled={busy || !target.settled || target.note.trim().length < 10} onClick={() => recover(shop, true)}>{t('stockCardPrice.resolve')}</Button><ActionScope effects={['upgates-read', 'hub-write']} shop={shop} />
            </details>}
          </div>}
          <small className="stock-card-price-operation">{t('stockCardPrice.operation', { id: operation.id })}</small>
        </>}
      </article>;
    })}</div>
    {ready && <div className="stock-card-price-confirmation"><label className="stock-card-price-check"><input type="checkbox" data-testid="price-publication-confirm" checked={confirmed} disabled={busy} onChange={event => setConfirmed(event.target.checked)} />{t('stockCardPrice.confirm', { shops: prepared!.targets.map(item => shopName(item.shop)).join(' + ') })}</label><Button type="button" data-testid="price-publication-send" icon={<Send size={16} />} disabled={busy || !confirmed} onClick={send}>{t('stockCardPrice.send', { shops: prepared!.targets.map(item => shopName(item.shop)).join(' + ') })}</Button><ActionScope effects={['upgates-write', 'upgates-read', 'hub-write']} /></div>}
    {history.length > 0 && <details className="stock-card-price-history"><summary>{t('stockCardPrice.history')}</summary><ul>{history.map(operation => <li key={operation.id}><span><strong>{shopName(operation.shop_code as ShopCode)}</strong> · {t(`productPublication.states.${operation.state}`, { defaultValue: operation.state })}<small>{priceOnly(operation) ? t('stockCardPrice.priceOperation') : t('stockCardPrice.otherOperationShort')} · {operation.id}</small></span><Button type="button" variant="ghost" size="sm" disabled={busy || unresolved(targets[operation.shop_code as ShopCode]) && targets[operation.shop_code as ShopCode].operation?.id !== operation.id} data-testid={`price-publication-history-${operation.id}`} onClick={() => { record(operation.shop_code as ShopCode, operation); setPrepared(null); setConfirmed(false); }}>{t('stockCardPrice.show')}</Button></li>)}</ul></details>}
  </section>;
}

export default ProductPricePublicationPanel;
