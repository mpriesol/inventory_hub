import React, { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { createProductCard, ProductCardCreate, ProductEditorRow } from '../../api/productEditor';
import { accessRevision } from '../../api/access';
import { Button } from '../ui/Button.new';
import { emptySkuIdentity, SkuPreview } from '../../api/skuPolicy';
import { SkuIdentityForm } from './SkuIdentityForm';

export function ProductCardCreateForm({ onCreated, onClose }: { onCreated: (row: ProductEditorRow) => void; onClose: () => void }) {
  const { t } = useTranslation();
  const [sku, setSku] = useState(''), [name, setName] = useState(''), [active, setActive] = useState(true), [ean, setEan] = useState('');
  const [identity, setIdentity] = useState(emptySkuIdentity), [preview, setPreview] = useState<SkuPreview | null>(null);
  const [itemType, setItemType] = useState<'stock' | 'service'>('stock'), [stockUnit, setStockUnit] = useState<'pcs' | 'pair' | 'pack' | 'm'>('pcs');
  const [busy, setBusy] = useState(false), [error, setError] = useState('');
  const [errorReasons, setErrorReasons] = useState<string[]>([]), [errorCandidates, setErrorCandidates] = useState<number[]>([]), [checkRevision, setCheckRevision] = useState(0);
  const request = useRef<ProductCardCreate | null>(null), sending = useRef(false);
  const [uncertain, setUncertain] = useState(false);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  async function submit() {
    const started = accessRevision();
    if (sending.current) return;
    sending.current = true; setBusy(true); setError(''); setErrorReasons([]); setErrorCandidates([]);
    if (!request.current && (!preview || !['new', 'existing'].includes(preview.status))) { sending.current = false; setBusy(false); return; }
    request.current ||= { request_id: crypto.randomUUID(), ...(sku ? { sku } : {}), name: name.trim(), is_active: active,
      eans: ean ? [ean] : [], ...(identity.brand && identity.source_code ? { sku_identity: identity } : {}), item_type: itemType, stock_unit: itemType === 'service' ? 'pcs' : stockUnit };
    try {
      const result = await createProductCard(request.current);
      if (!mounted.current || started !== accessRevision()) return;
      const row = result.results[0]?.row;
      if (!row) throw new Error('missing result');
      onCreated(row);
    } catch (value) {
      if (!mounted.current || started !== accessRevision()) return;
      const code = (value as Error & { code?: string }).code, status = (value as Error & { status?: number }).status;
      if ((status && status >= 400 && status < 500) || ['product_editor_identity_conflict', 'product_editor_invalid_request', 'product_editor_request_reused'].includes(code || '')) {
        request.current = null; setUncertain(false); setError(code || 'product_editor_invalid_request'); setCheckRevision(count => count + 1);
        const detail = (value as { detail?: { reasons?: string[]; candidate_product_ids?: number[] } }).detail;
        setErrorReasons(detail?.reasons || []); setErrorCandidates(detail?.candidate_product_ids || []);
      } else { setUncertain(true); setError('create_uncertain'); }
    } finally { sending.current = false; setBusy(false); }
  }
  return <form className="product-editor-create" aria-label={t('productEditor.newCard')} onSubmit={event => { event.preventDefault(); submit(); }}>
    <h2>{t('productEditor.newCard')}</h2><p>{t('productEditor.newCardHelp')}</p>
    <details><summary>{t('skuPolicy.lookupExisting')}</summary><label>{t('skuPolicy.existingSku')}<input data-testid="create-sku" maxLength={100} disabled={busy || uncertain} value={sku} onChange={event => setSku(event.target.value)} /></label></details>
    <label>{t('skuPolicy.ean')}<input data-testid="create-ean" maxLength={14} disabled={busy || uncertain} value={ean} onChange={event => setEan(event.target.value)} /></label>
    <label>{t('skuPolicy.newCardName')}<input data-testid="create-name" required={!uncertain && preview?.status !== 'existing'} maxLength={500} disabled={busy || uncertain} value={name} onChange={event => setName(event.target.value)} /></label>
    <label>{t('productEditor.fields.is_active')}<select data-testid="create-activity" disabled={busy || uncertain} value={String(active)} onChange={event => setActive(event.target.value === 'true')}><option value="true">{t('productEditor.activityStates.active')}</option><option value="false">{t('productEditor.activityStates.inactive')}</option></select></label>
    <label>{t('productEditor.fields.item_type')}<select data-testid="create-item-type" disabled={busy || uncertain} value={itemType} onChange={event => setItemType(event.target.value as 'stock' | 'service')}><option value="stock">{t('productEditor.itemTypes.stock')}</option><option value="service">{t('productEditor.itemTypes.service')}</option></select></label>
    {itemType === 'stock' ? <label>{t('productEditor.fields.stock_unit')}<select data-testid="create-stock-unit" disabled={busy || uncertain} value={stockUnit} onChange={event => setStockUnit(event.target.value as typeof stockUnit)}>{(['pcs', 'pair', 'pack', 'm'] as const).map(unit => <option key={unit} value={unit}>{t(`productEditor.units.${unit}`)}</option>)}</select><small>{t('productEditor.unitHelp')}</small>{stockUnit === 'm' && <small role="note">{t('productEditor.meterLimit')}</small>}</label> : <p>{t('productEditor.serviceHelp')}</p>}
    <SkuIdentityForm key={checkRevision} simplified value={identity} onChange={setIdentity} onPreview={setPreview} context={{ barcodes: ean ? [ean] : [], sku, item_type: itemType }} disabled={busy || uncertain} />
    {error && <p role="alert">{t(`productEditor.errors.${error}`, { defaultValue: t(`skuPolicy.errors.${error}`, { defaultValue: t('productEditor.errors.request_failed') }) })}</p>}
    {errorReasons.map(reason => <p key={reason} role="alert">{t(`skuPolicy.errors.${reason}`, { defaultValue: t('skuPolicy.errors.identity_conflict') })}</p>)}
    {!!errorCandidates.length && <p>{t('skuPolicy.candidates', { ids: errorCandidates.join(', ') })}</p>}
    <Button data-testid="create-submit" type="submit" disabled={busy || (!uncertain && ((preview?.status !== 'existing' && !name.trim()) || !['new', 'existing'].includes(preview?.status || '')))}>{t(uncertain ? 'productEditor.retryCreate' : preview?.status === 'existing' ? 'skuPolicy.useExisting' : 'productEditor.createCard')}</Button>
    <Button type="button" variant="ghost" disabled={busy} onClick={onClose}>{t('productEditor.cancelCreate')}</Button>
  </form>;
}
