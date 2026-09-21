import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ImportPreview, ImportResult, catalogImageUrl } from '../../api/catalog';
import { Modal } from '../ui/Modal';
import { Button } from '../ui/Button.new';
import { ProductThumb } from './ProductDisplay';
import { CatalogMatches } from './CatalogMatches';

export function CatalogImport({ preview, result, shopName, sending, error, onConfirm, onRetry, onClose, onReprice, onExclude }: {
  preview: ImportPreview | null; result: ImportResult | null; shopName: string; sending: boolean; error: string;
  onConfirm: () => void; onRetry: () => void; onClose: () => void;
  onReprice: (overrides: Record<number, string>) => void;
  onExclude: (ids: number[], overrides: Record<number, string>) => void;
}) {
  const { t } = useTranslation();
  const [page, setPage] = useState(1);
  const [editingPrices, setEditingPrices] = useState(false);
  const [drafts, setDrafts] = useState<Record<number, string>>({});
  const dirty = Object.keys(drafts).length > 0;
  const validPrices = Object.values(drafts).every(value => !value.trim() || (/^\d{1,10}([.,]\d{1,2})?$/.test(value.trim()) && Number(value.replace(',', '.')) > 0));
  const priceLines = new Map((preview?.price_lines || []).map(line => [line.product_id, line]));
  function priceOverrides(excluded: number[] = []) {
    const overrides = { ...preview?.sale_price_overrides };
    Object.entries(drafts).forEach(([id, value]) => {
      const key = Number(id), cleaned = value.trim().replace(',', '.');
      if (!cleaned || (!priceLines.get(key)?.overridden && Number(cleaned) === Number(priceLines.get(key)?.sale_gross))) delete overrides[key];
      else overrides[key] = cleaned;
    });
    excluded.forEach(id => delete overrides[id]);
    return overrides;
  }
  const items = result?.items || preview?.items || [];
  const options = result?.options || preview?.options;
  const shopCheck = result?.shop_check || (!result ? preview?.shop_check : null);
  const counts = items.reduce<Record<string, number>>((all, item) => ({ ...all, [item.status]: (all[item.status] || 0) + 1 }), {});
  const running = result?.status === 'running' || result?.status === 'queued';
  const canRetry = !!result && !running && (result.status === 'failed' || !!counts.failed || !!counts.uncertain);
  const issues = [...(result?.errors || preview?.errors || []), ...(error ? [error] : [])];
  return <Modal open onClose={onClose} title={t(result ? 'catalog.importResult' : 'catalog.importPreview')}><div className="supplier-catalog catalog-import" role="dialog" aria-label={t('catalog.importPreview')}>
    <div className="catalog-import-heading"><div><div className="catalog-eyebrow">{t('catalog.targetShop')}</div><h2>{shopName}</h2></div>
      <span className="catalog-badge">{t('catalog.hiddenValidation')}</span></div>
    <p className="catalog-muted">{t('catalog.importScope')}</p>
    {shopCheck && <div className="catalog-notice">{t('catalog.shopProductsChecked', { date: new Date(shopCheck.checked_at).toLocaleString() })} · {t(shopCheck.mode === 'full' ? 'catalog.shopCheckFull' : 'catalog.shopCheckChanges')}<br />{t('catalog.shopFullChecked', { date: new Date(shopCheck.full_checked_at).toLocaleString() })}{!result && <p>{t('catalog.shopRecheckBeforeImport')}</p>}</div>}
    <p className="catalog-muted">{t((result?.prices_with_vat ?? preview?.prices_with_vat) ? 'catalog.shopPricesGross' : 'catalog.shopPricesNet')}</p>
    {!result && preview && <p className="catalog-muted">{t('catalog.previewExpires', { date: new Date(preview.expires_at).toLocaleTimeString() })} · {preview.options.currency} · {preview.options.pricelist} · {preview.options.category_code || t('catalog.noCategory')}</p>}
    {!result && preview?.create_validation_field && <div className="catalog-notice">{t('catalog.createValidationField')}</div>}
    <div className="catalog-import-counts">{Object.entries(counts).map(([status, count]) => <div key={status}><strong>{count}</strong><span>{t(`catalog.itemStatus.${status}`)}</span></div>)}</div>
    {issues.length > 0 && <div role="alert" className="catalog-error">{issues.map((issue, i) => <p key={i}>{t(`catalog.codes.${issue}`, { defaultValue: issue })}</p>)}</div>}
    {running && <p role="status" className="catalog-notice">{t('catalog.importRunning')}</p>}
    {counts.uncertain > 0 && <p className="catalog-notice">{t('catalog.uncertainHelp')}</p>}
    {!result && !!preview?.price_lines?.length && <div className="catalog-price-tools"><Button variant="secondary" disabled={sending} onClick={() => setEditingPrices(!editingPrices)}>{t(editingPrices ? 'catalog.hidePriceEditor' : 'catalog.editPrices')}</Button><span className="catalog-muted">{t('catalog.editPricesHelp')}</span></div>}
    {['retail_below_purchase', 'sale_below_purchase'].filter(w => items.some(item => item.warnings.includes(w))).map(w => <p key={w} className="catalog-notice" role="status">{t(`catalog.codes.${w}`)}</p>)}
    <div className="catalog-import-items">{items.slice((page - 1) * 30, page * 30).map(item => {
      const existingIds = item.existing_product_ids || [];
      const editableIds = item.product_ids.filter(id => priceLines.has(id) && !priceLines.get(id)!.blocked && !priceLines.get(id)!.existing && item.status !== 'exists');
      const groupValues = [...new Set(editableIds.map(id => String(drafts[id] ?? priceLines.get(id)?.sale_gross ?? '')))];
      const setGroupPrice = (value: string) => setDrafts(old => ({ ...old, ...Object.fromEntries(editableIds.map(id => [id, value])) }));
      return <details className="catalog-import-item" key={item.code} open={editingPrices || item.errors.length > 0 ? true : undefined}>
      <summary><ProductThumb url={catalogImageUrl(item.payload.images?.[0]?.url)} name={item.name} size={44} /><span className="catalog-import-item-name"><strong>{item.name}</strong><code>{item.code}{item.variants_count ? ` · ${t('catalog.variantCount', { count: item.variants_count })}` : ''}</code></span>
        {item.payload.prices?.[0]?.pricelists?.[0]?.price_original !== undefined && <span className="catalog-number">{item.payload.prices[0].pricelists[0].price_original} {options?.currency}</span>}
        <span className={`catalog-badge ${item.status === 'created' ? 'catalog-good' : item.errors.length ? 'catalog-bad' : ''}`}>{t(`catalog.itemStatus.${item.status}`)}</span></summary>
      {!result && existingIds.length > 0 && <div className="catalog-notice"><p>{t(item.parent_exists ? 'catalog.parentAlreadyExists' : 'catalog.existingVariants', { count: existingIds.length, total: item.product_ids.length })}</p>
        {existingIds.map(id => <div key={id}><code>{priceLines.get(id)?.code}</code><CatalogMatches matches={priceLines.get(id)?.shop_matches} /></div>)}
        {!item.parent_exists && existingIds.length < item.product_ids.length && <Button variant="secondary" size="sm" disabled={sending || !validPrices} onClick={() => onExclude(existingIds, priceOverrides(existingIds))}>{t('catalog.excludeExisting', { count: existingIds.length })}</Button>}
      </div>}
      {!result && editingPrices && item.variants_count > 1 && editableIds.length > 0 && <div className="catalog-group-price">
        <label>{t('catalog.groupSalePrice')}<input type="text" inputMode="decimal" aria-label={t('catalog.groupSalePriceOf', { name: item.name })} disabled={sending} value={groupValues.length === 1 ? groupValues[0] : ''} placeholder={t('catalog.mixedPrices')} onChange={e => setGroupPrice(e.target.value)} /></label>
        <span className="catalog-muted">{t('catalog.groupSalePriceHelp', { count: editableIds.length, total: item.product_ids.length })}</span>
        <button className="catalog-price-reset" disabled={sending} onClick={() => setGroupPrice('')}>{t('catalog.resetGroupSalePrice')}</button>
      </div>}
      {!result && editingPrices ? <div className="catalog-table-scroll"><table className="catalog-edit-prices"><thead><tr><th>{t('catalog.product')}</th><th>{t('catalog.price.retail_gross')}</th><th>{t('catalog.price.purchase_net')}</th><th>{t('catalog.saleGross')}</th></tr></thead><tbody>{item.product_ids.map(id => {
        const line = priceLines.get(id);
        if (!line) return null;
        return <tr key={id}><td><div className="catalog-product-cell"><ProductThumb url={catalogImageUrl(line.image)} name={line.name} size={36} /><div><code>{line.code}</code><div>{line.attributes.map(a => a.value).join(' / ')}</div></div></div>{line.warnings.map(w => <div key={w} className="catalog-muted">{t(`catalog.codes.${w}`, { defaultValue: w })}</div>)}</td>
          <td>{line.retail_gross ?? '—'} {options?.currency}</td><td>{line.purchase_net ?? '—'} {options?.currency}</td>
          <td><label><span className="sr-only">{t('catalog.salePriceOf', { code: line.code })}</span><input type="text" inputMode="decimal" disabled={sending || line.blocked || line.existing || item.status === 'exists'} value={drafts[id] ?? line.sale_gross ?? ''} placeholder={t('catalog.supplierPrice')} onChange={e => setDrafts(old => ({ ...old, [id]: e.target.value }))} /></label>
            {line.existing || item.status === 'exists' ? <div className="catalog-muted">{t('catalog.existingPriceUnchanged')}</div> : <button className="catalog-price-reset" disabled={sending || line.blocked} onClick={() => setDrafts(old => ({ ...old, [id]: '' }))}>{t('catalog.resetSalePrice')}</button>}</td></tr>;
      })}</tbody></table></div> : item.payload.variants?.map((v: any) => <div className="catalog-preview-variant" key={v.code}><ProductThumb url={catalogImageUrl(v.image?.url)} name={v.code} size={36} /><code>{v.code}</code><span>{v.parameters?.flatMap((p: any) => p.values?.map((value: any) => value.descriptions?.[0]?.value)).join(' / ')}</span></div>)}
      {[...item.errors, ...item.warnings].map((issue, i) => <p className={item.errors.includes(issue) ? 'catalog-error' : 'catalog-muted'} key={i}>{t(`catalog.codes.${issue}`, { defaultValue: issue })}</p>)}
      {!result && <details><summary>{t('catalog.importPayload')}</summary><pre className="catalog-source">{JSON.stringify(item.payload, null, 2)}</pre></details>}
    </details>; })}</div>
    {items.length > 30 && <div className="catalog-pagination"><Button variant="secondary" size="sm" disabled={page === 1} onClick={() => setPage(page - 1)}>{t('common.back')}</Button><span>{page} / {Math.ceil(items.length / 30)}</span><Button variant="secondary" size="sm" disabled={page * 30 >= items.length} onClick={() => setPage(page + 1)}>{t('common.next')}</Button></div>}
    {dirty && <p className={validPrices ? 'catalog-notice' : 'catalog-error'} role="status">{t(validPrices ? 'catalog.pricesNeedPreview' : 'catalog.codes.invalid_sale_price')}</p>}
    <div className="catalog-modal-actions"><Button variant="secondary" onClick={onClose}>{t('common.close')}</Button>
      {!result && dirty && <Button variant="secondary" loading={sending} disabled={!validPrices} onClick={() => onReprice(priceOverrides())}>{t('catalog.repricePreview')}</Button>}
      {canRetry && <Button loading={sending} onClick={onRetry}>{t('catalog.retryImport')}</Button>}
      {!result && <Button loading={sending} disabled={dirty || !counts.ready || !!preview?.errors.length || !preview || Date.now() >= Date.parse(preview.expires_at)} onClick={onConfirm}>{t('catalog.confirmImport', { count: counts.ready || 0 })}</Button>}
    </div>
  </div></Modal>;
}
