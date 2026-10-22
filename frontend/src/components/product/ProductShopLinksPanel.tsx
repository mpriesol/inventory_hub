import React, { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { X } from 'lucide-react';
import { Button } from '../ui/Button.new';
import { ActionScope } from '../ui/ActionScope';
import { accessRevision } from '../../api/access';
import { ProductEditorRow } from '../../api/productEditor';
import { applyProductLinks, forgetProductLink, PendingProductLink, pendingProductLink, ProductLinkBatch,
  ProductLinkPreviewBody, ProductLinkRow, ProductLinkShop, readProductLinks, rememberProductLink,
  previewProductLinks } from '../../api/productLinks';
import './ProductShopLinksPanel.css';

type Manual = Record<number, { parent: string; variant: string; replace?: boolean }>;
type SelectedCard = { id: number; sku: string; name: string };

export function ProductShopLinksPanel({ rows, onClose, onChanged }: {
  rows: ProductEditorRow[]; onClose: () => void; onChanged: () => void;
}) {
  const { t } = useTranslation();
  const [shop, setShop] = useState<ProductLinkShop>('biketrek');
  const [manual, setManual] = useState<Manual>(() => Object.fromEntries(
    (pendingProductLink()?.request.manual_matches || []).map(item => [item.product_id,
      { parent: item.parent_code, variant: item.is_variant ? item.code : '', replace: item.replace_deleted_variant === true }])));
  const [batch, setBatch] = useState<ProductLinkBatch | null>(null);
  const [recoveredScope, setRecoveredScope] = useState<SelectedCard[]>([]);
  const [pending, setPending] = useState<PendingProductLink | null>(() => pendingProductLink());
  const [selected, setSelected] = useState<number[]>([]);
  const [busy, setBusy] = useState<'preview' | 'apply' | 'read' | null>(null);
  const [uncertain, setUncertain] = useState<'preview' | 'apply' | null>(() => {
    const saved = pendingProductLink(); return saved ? saved.apply ? 'apply' : 'preview' : null;
  });
  const [error, setError] = useState('');
  const [missingPreview, setMissingPreview] = useState(false);
  const dialog = useRef<HTMLElement | null>(null), busyRef = useRef(false), mounted = useRef(true);
  const pendingRef = useRef(pending), closeRef = useRef(onClose), changedRef = useRef(onChanged);
  closeRef.current = onClose; changedRef.current = onChanged;
  const shopName = shop === 'biketrek' ? 'BIKETREK' : 'xTrek';
  const frozen = !!uncertain || !!pending?.apply;
  const selectedCards = rows.length ? rows.map(row => ({ id: row.id, sku: row.sku, name: row.common.name })) : recoveredScope;
  const sourceRows = batch?.rows || selectedCards.map(row => ({ product_id: row.id, sku: row.sku, name: row.name }));
  const ready = batch?.rows.filter(row => row.status === 'ready') || [];
  const allAlreadyLinked = !!batch?.rows.length && batch.rows.every(row => row.status === 'already_linked');
  const settled = batch?.status === 'completed' || (allAlreadyLinked && !frozen);
  const count = (status: ProductLinkRow['status']) => batch?.rows.filter(row => row.status === status).length || 0;
  const displayedIds = batch?.rows.map(row => row.product_id) || pending?.request.product_ids;
  const scopeChanged = rows.length > 0 && !!displayedIds && ((pending?.request.shop_code !== undefined && pending.request.shop_code !== shop) ||
    JSON.stringify([...displayedIds].sort((a, b) => a - b)) !== JSON.stringify(rows.map(row => row.id).sort((a, b) => a - b)));
  const text = (code: string) => t(`productLinks.errors.${code}`, { defaultValue: t('productLinks.failed') });
  const reason = (code: string) => t(`productLinks.reasons.${code}`, { defaultValue: t(`productLinks.errors.${code}`, { defaultValue: t('productLinks.reasonUnknown', { code }) }) });

  function store(value: PendingProductLink) {
    rememberProductLink(value); pendingRef.current = value; setPending(value);
  }
  function accept(value: ProductLinkBatch) {
    setBatch(value); setShop(value.shop_code); setMissingPreview(false);
    if (!rows.length) setRecoveredScope(value.rows.map(row => ({ id: row.product_id, sku: row.sku, name: row.name })));
    setSelected(value.rows.filter(row => row.status === 'ready').map(row => row.product_id));
    // A read-only preview with nothing to link needs no apply. A prepared GET
    // must still retain any earlier uncertain apply, even if all rows match.
    const noChanges = value.status === 'prepared' && value.rows.length > 0 &&
      value.rows.every(row => row.status === 'already_linked') && !pendingRef.current?.apply;
    if (value.status === 'completed' || noChanges) {
      try { forgetProductLink(value.id); } catch { /* The settled read-only result can be recovered again. */ }
      pendingRef.current = null; setPending(null); setUncertain(null); changedRef.current();
    } else {
      setUncertain(pendingRef.current?.apply ? 'apply' : null);
    }
  }
  async function perform(kind: 'preview' | 'apply' | 'read', retry = false) {
    if (busyRef.current) return;
    let saved = pendingRef.current;
    if (kind === 'preview' && !retry) {
      if (frozen || !selectedCards.length || selectedCards.length > 100) return;
      if (Object.values(manual).some(value => value.variant.trim() && !value.parent.trim())) { setError('manual_parent_required'); return; }
      if (Object.values(manual).some(value => [value.parent, value.variant].some(code => /[;\u0000-\u001f\u007f]/.test(code)))) { setError('manual_code_invalid'); return; }
      const request: ProductLinkPreviewBody = { request_id: crypto.randomUUID(), shop_code: shop,
        product_ids: selectedCards.map(row => row.id), manual_matches: selectedCards.flatMap(row => {
          const value = manual[row.id];
          if (!value?.parent.trim()) return [];
          return [{ product_id: row.id, code: value.variant.trim() || value.parent.trim(),
            parent_code: value.parent.trim(), is_variant: !!value.variant.trim(), ...(value.replace ? { replace_deleted_variant: true } : {}) }];
        }) };
      saved = { version: 1, request };
      try { store(saved); } catch { setError('storage_unavailable'); return; }
      setBatch(null); setSelected([]);
    }
    if (!saved) return;
    if (kind === 'apply' && !retry) {
      if (frozen || !batch || batch.status !== 'prepared' || !selected.length ||
          selected.some(id => !ready.some(row => row.product_id === id))) return;
      saved = { ...saved, apply: { preview_hash: batch.preview_hash, product_ids: [...selected].sort((a, b) => a - b), confirmed: true } };
      try { store(saved); } catch { setError('storage_unavailable'); return; }
    }
    if (kind === 'apply' && !saved.apply) return;
    const credential = accessRevision();
    busyRef.current = true; setBusy(kind); setError('');
    if (kind !== 'read') setUncertain(kind);
    try {
      const value = kind === 'preview' ? await previewProductLinks(saved.request)
        : kind === 'apply' ? await applyProductLinks(saved.request.request_id, saved.apply!)
        : await readProductLinks(saved.request.request_id);
      if (mounted.current && credential === accessRevision()) accept(value);
    } catch (failure: any) {
      if (!mounted.current || credential !== accessRevision()) return;
      const code = failure.code || 'request_failed'; setError(code);
      if (kind === 'read' && !saved.apply && ['product_links_not_found', 'product_link_batch_not_found'].includes(code)) setMissingPreview(true);
      // An explicit input/auth rejection means this preview was not accepted.
      // A reused UUID still belongs to the durable original request: recover it.
      if (kind === 'preview' && failure.status >= 400 && failure.status < 500 &&
          !['product_links_request_changed', 'product_links_rate_limited'].includes(code) && failure.status !== 429) {
        try { forgetProductLink(saved.request.request_id); pendingRef.current = null; setPending(null); setUncertain(null); }
        catch { setError('storage_unavailable'); }
      }
      // This marker follows a committed expiry fence. It also settles any earlier
      // lost apply response; a status code or prepared GET alone cannot do that.
      if (kind === 'apply' && failure.knownUnapplied === true) {
        const prepared = { version: 1 as const, request: saved.request };
        try { store(prepared); setUncertain(null); setBatch(null); setSelected([]); }
        catch { setError('storage_unavailable'); }
      }
    } finally {
      if (mounted.current && credential === accessRevision()) { busyRef.current = false; setBusy(null); }
    }
  }

  useEffect(() => {
    mounted.current = true;
    const originalFocus = document.activeElement as HTMLElement;
    dialog.current?.querySelector<HTMLElement>('button')?.focus();
    if (pendingRef.current) { setShop(pendingRef.current.request.shop_code); void perform('read'); }
    const keyboard = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !busyRef.current) { event.preventDefault(); closeRef.current(); }
      if (event.key !== 'Tab') return;
      const focusable = Array.from(dialog.current?.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), select:not(:disabled), summary') || []);
      const first = focusable[0], last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    };
    document.addEventListener('keydown', keyboard);
    return () => { mounted.current = false; document.removeEventListener('keydown', keyboard); originalFocus?.focus(); };
  }, []);

  function changeManual(id: number, key: 'parent' | 'variant', value: string) {
    if (frozen || busyRef.current) return;
    setManual(previous => ({ ...previous, [id]: { parent: '', variant: '', ...previous[id], [key]: value, ...(key === 'variant' && value ? { replace: false } : {}) } }));
    setBatch(null); setSelected([]);
  }
  const changeShop = (value: ProductLinkShop) => {
    if (frozen || busyRef.current) return;
    setManual(previous => Object.fromEntries(Object.entries(previous).map(([id, entry]) => [id, { ...entry, replace: false }])));
    setShop(value); setBatch(null); setSelected([]); setError('');
  };
  const statuses: ProductLinkRow['status'][] = ['ready', 'already_linked', 'conflict', 'not_found'];
  return <div className="product-editor-modal-backdrop"><section ref={dialog} role="dialog" aria-modal="true"
    aria-labelledby="product-links-title" className="product-editor-detail product-links-panel">
    <header><div><h2 id="product-links-title">{t('productLinks.title')}</h2><p className="product-editor-muted">{t('productLinks.intro')}</p></div>
      <button data-testid="product-links-close" type="button" disabled={!!busy} aria-label={t('productEditor.closeDetail')} onClick={onClose}><X size={20} /></button></header>
    <div className="product-editor-detail-body">
      <div className="product-links-toolbar"><label>{t('productLinks.shop')}<select data-testid="product-links-shop" value={shop}
        disabled={!!busy || frozen} onChange={event => changeShop(event.target.value as ProductLinkShop)}>
        <option value="biketrek">BIKETREK</option><option value="xtrek">xTrek</option></select></label>
        <p>{t('productLinks.selectionCount', { count: batch?.rows.length || selectedCards.length })}</p>
      </div>
      {scopeChanged && <p className="product-editor-alert">{t('productLinks.recoveredScope')}</p>}
      {selectedCards.length > 100 && <p role="alert" className="product-editor-error">{t('productLinks.limit')}</p>}
      {error && <p role="alert" className="product-editor-error">{text(error)}</p>}
      {uncertain && <div className="product-editor-alert" data-testid="product-links-recovery">
        <p>{t(uncertain === 'apply' ? 'productLinks.applyUnknown' : 'productLinks.previewUnknown')}</p>
        <div className="product-links-actions"><Button variant="secondary" data-testid="product-links-recover" disabled={!!busy} onClick={() => perform('read')}>{t('productLinks.readResult')}</Button>
          {uncertain === 'apply' && pending?.apply && <Button variant="secondary" data-testid="product-links-retry-apply" disabled={!!busy} onClick={() => perform('apply', true)}>{t('productLinks.retryApply')}</Button>}
          {uncertain === 'preview' && missingPreview && <Button variant="secondary" data-testid="product-links-retry-preview" disabled={!!busy} onClick={() => perform('preview', true)}>{t('productLinks.retryPreview')}</Button>}
        </div><small>{t('productLinks.recoverySaved')}</small>
      </div>}
      {batch && <>
        <div className="product-links-counts" aria-label={t('productLinks.summary')}>{statuses.map(status => <span key={status} data-testid={`product-links-count-${status}`}><strong>{count(status)}</strong> {t(`productLinks.status.${status}`)}</span>)}</div>
        {settled && <p role="status" className="product-links-success" data-testid="product-links-completed">{t(allAlreadyLinked ? 'productLinks.allAlreadyLinked' : 'productLinks.completed', { count: batch.result?.linked_product_ids.length || 0, shop: shopName })}</p>}
      </>}
      <div className="product-links-table-scroll"><table className="product-editor-diff product-links-table"><thead><tr>
        <th><input data-testid="product-links-select-ready" type="checkbox" aria-label={t('productLinks.selectReady')}
          disabled={!!busy || frozen || !ready.length || batch?.status !== 'prepared'} checked={!!ready.length && ready.every(row => selected.includes(row.product_id))}
          onChange={event => setSelected(event.target.checked ? ready.map(row => row.product_id) : [])} /></th>
        <th>{t('productLinks.localProduct')}</th><th>{t('productLinks.remoteProduct')}</th><th>{t('productLinks.match')}</th><th>{t('productLinks.result')}</th>
      </tr></thead><tbody>{sourceRows.map(row => {
        const checked = batch?.rows.find(item => item.product_id === row.product_id);
        const remote = checked?.remote;
        const canReplace = rows.find(item => item.id === row.product_id)?.shops?.find(item => item.shop_code === shop)?.mapping?.is_variant === true;
        const rawBarcodes = remote?.raw_barcodes ?? (remote?.eans.length ? remote.eans : undefined);
        const invalidBarcodes = remote?.invalid_barcodes || [];
        const warnings = checked?.warnings.filter(code =>
          !(code === 'unverified_barcode' && invalidBarcodes.length > 0) &&
          !(code === 'barcode_missing' && checked.local_eans !== undefined && remote?.raw_barcodes !== undefined)) || [];
        return <tr key={row.product_id} data-testid={`product-links-row-${row.product_id}`}>
          <td><input type="checkbox" data-testid={`product-links-select-${row.product_id}`} aria-label={t('productLinks.selectProduct', { sku: row.sku })}
            disabled={!!busy || frozen || checked?.status !== 'ready' || batch?.status !== 'prepared'} checked={selected.includes(row.product_id)}
            onChange={event => setSelected(previous => event.target.checked ? [...previous, row.product_id] : previous.filter(id => id !== row.product_id))} /></td>
          <td><strong><code>{row.sku}</code></strong><div>{row.name}</div>{checked && <dl>
            <dt>{t('productLinks.hubEan')}</dt><dd data-testid={`product-links-hub-ean-${row.product_id}`}>
              {checked.local_eans === undefined ? t('productLinks.barcodeNotRecorded') : checked.local_eans.join(', ') || t('productLinks.barcodeAbsent')}
            </dd></dl>}</td>
          <td>{remote ? <><code>{remote.code}</code><div>{remote.name}</div><dl>
            <dt>{t('productLinks.parent')}</dt><dd><code>{remote.parent_code}</code></dd>
            <dt>{t(remote.is_variant ? 'productLinks.variantId' : 'productLinks.productId')}</dt><dd>{remote.external_id}</dd>
            <dt>{t('productLinks.shopBarcode', { shop: shopName })}</dt><dd data-testid={`product-links-shop-barcode-${row.product_id}`}>
              {rawBarcodes === undefined ? t('productLinks.barcodeNotRecorded') : rawBarcodes.join(', ') || t('productLinks.barcodeAbsent')}
              {invalidBarcodes.map(value => <p key={value} className="product-links-barcode-warning">{t('productLinks.barcodeInvalid', { value })}</p>)}
            </dd>
            {remote.attributes.map(item => <React.Fragment key={item.name}><dt>{item.name}</dt><dd>{item.value}</dd></React.Fragment>)}
          </dl></> : <span className="product-editor-muted">{t('productLinks.notChecked')}</span>}
            {checked?.previous_mapping && <p data-testid={`product-links-replacement-${row.product_id}`}>{t('productLinks.replacementPreview', { old: checked.previous_mapping.external_code, code: remote?.code || '—' })}</p>}
            {!frozen && batch?.status !== 'completed' && (!settled || canReplace) && <details className="product-links-manual"><summary>{t('productLinks.exactTarget')}</summary>
              <p>{t('productLinks.manualHelp')}</p>
              <label>{t('productLinks.parentInput')}<input data-testid={`product-links-parent-${row.product_id}`} maxLength={100} value={manual[row.product_id]?.parent || ''}
                disabled={!!busy} onChange={event => changeManual(row.product_id, 'parent', event.target.value)} /></label>
              <label>{t('productLinks.variantInput')}<input data-testid={`product-links-variant-${row.product_id}`} maxLength={100} value={manual[row.product_id]?.variant || ''}
                disabled={!!busy || manual[row.product_id]?.replace} onChange={event => changeManual(row.product_id, 'variant', event.target.value)} /></label>
              {canReplace && <label><input type="checkbox" data-testid={`product-links-replace-${row.product_id}`} disabled={!!busy} checked={manual[row.product_id]?.replace || false} onChange={event => { const checked = event.target.checked; setManual(previous => ({ ...previous, [row.product_id]: { parent: '', ...previous[row.product_id], variant: checked ? '' : previous[row.product_id]?.variant || '', replace: checked } })); setBatch(null); setSelected([]); }} />{t('productLinks.replaceDeletedVariant')}</label>}
              {manual[row.product_id]?.replace && <p>{t('productLinks.replacementHelp')}</p>}
            </details>}
          </td>
          <td>{checked?.match_by ? t(`productLinks.matchBy.${checked.match_by}`, { defaultValue: checked.match_by }) : '—'}</td>
          <td>{checked && <><strong>{t(`productLinks.status.${checked.status}`)}</strong>
            {checked.errors.map(code => <p key={code} className="product-editor-error">{reason(code)}</p>)}
            {warnings.map(code => <p key={code} className="product-editor-muted">{reason(code)}</p>)}</>}</td>
        </tr>;
      })}</tbody></table></div>
      {!frozen && <div className="product-links-actions">{settled && <Button data-testid="product-links-done" disabled={!!busy} onClick={onClose}>{t('productLinks.done')}</Button>}
        {!(settled && allAlreadyLinked) && <div className="product-editor-scoped-action">
        <Button data-testid="product-links-preview" variant="secondary" disabled={!!busy || !selectedCards.length || selectedCards.length > 100} onClick={() => perform('preview')}>{t(batch ? 'productLinks.refreshPreview' : 'productLinks.preview')}</Button>
        <ActionScope effects={['upgates-read', 'hub-write']} shop={shop} /></div>}
        {batch?.status === 'prepared' && ready.length > 0 && <div className="product-editor-scoped-action"><Button data-testid="product-links-apply" disabled={!!busy || !selected.length} onClick={() => perform('apply')}>
          {t('productLinks.apply', { count: selected.length })}</Button><ActionScope effects={['upgates-read', 'hub-write']} shop={shop} /></div>}
      </div>}
      {busy && <p role="status">{t(`productLinks.busy.${busy}`)}</p>}
    </div>
  </section></div>;
}
