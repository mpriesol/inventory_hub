import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ImportPreview, ImportResult } from '../../api/catalog';
import { Modal } from '../ui/Modal';
import { Button } from '../ui/Button.new';
import { ProductThumb } from './ProductDisplay';

export function CatalogImport({ preview, result, shopName, sending, error, onConfirm, onRetry, onClose }: {
  preview: ImportPreview | null; result: ImportResult | null; shopName: string; sending: boolean; error: string;
  onConfirm: () => void; onRetry: () => void; onClose: () => void;
}) {
  const { t } = useTranslation();
  const [page, setPage] = useState(1);
  const items = result?.items || preview?.items || [];
  const options = result?.options || preview?.options;
  const counts = items.reduce<Record<string, number>>((all, item) => ({ ...all, [item.status]: (all[item.status] || 0) + 1 }), {});
  const running = result?.status === 'running' || result?.status === 'queued';
  const canRetry = !!result && !running && (result.status === 'failed' || !!counts.failed || !!counts.uncertain);
  const issues = [...(result?.errors || preview?.errors || []), ...(error ? [error] : [])];
  return <Modal open onClose={onClose} title={t(result ? 'catalog.importResult' : 'catalog.importPreview')}><div className="supplier-catalog catalog-import" role="dialog" aria-label={t('catalog.importPreview')}>
    <div className="catalog-import-heading"><div><div className="catalog-eyebrow">{t('catalog.targetShop')}</div><h2>{shopName}</h2></div>
      <span className="catalog-badge">{t('catalog.hiddenValidation')}</span></div>
    <p className="catalog-muted">{t('catalog.importScope')}</p>
    <p className="catalog-muted">{t((result?.prices_with_vat ?? preview?.prices_with_vat) ? 'catalog.shopPricesGross' : 'catalog.shopPricesNet')}</p>
    {!result && preview && <p className="catalog-muted">{t('catalog.previewExpires', { date: new Date(preview.expires_at).toLocaleTimeString() })} · {preview.options.currency} · {preview.options.pricelist} · {preview.options.category_code || t('catalog.noCategory')}</p>}
    {!result && preview?.create_validation_field && <div className="catalog-notice">{t('catalog.createValidationField')}</div>}
    <div className="catalog-import-counts">{Object.entries(counts).map(([status, count]) => <div key={status}><strong>{count}</strong><span>{t(`catalog.itemStatus.${status}`)}</span></div>)}</div>
    {issues.length > 0 && <div role="alert" className="catalog-error">{issues.map((issue, i) => <p key={i}>{t(`catalog.codes.${issue}`, { defaultValue: issue })}</p>)}</div>}
    {running && <p role="status" className="catalog-notice">{t('catalog.importRunning')}</p>}
    {counts.uncertain > 0 && <p className="catalog-notice">{t('catalog.uncertainHelp')}</p>}
    <div className="catalog-import-items">{items.slice((page - 1) * 30, page * 30).map(item => <details className="catalog-import-item" key={item.code}>
      <summary><ProductThumb url={item.payload.images?.[0]?.url} name={item.name} size={44} /><span className="catalog-import-item-name"><strong>{item.name}</strong><code>{item.code}{item.variants_count ? ` · ${t('catalog.variantCount', { count: item.variants_count })}` : ''}</code></span>
        {item.payload.prices?.[0]?.pricelists?.[0]?.price_original !== undefined && <span className="catalog-number">{item.payload.prices[0].pricelists[0].price_original} {options?.currency}</span>}
        <span className={`catalog-badge ${item.status === 'created' ? 'catalog-good' : item.errors.length ? 'catalog-bad' : ''}`}>{t(`catalog.itemStatus.${item.status}`)}</span></summary>
      {item.payload.variants?.map((v: any) => <div className="catalog-preview-variant" key={v.code}><ProductThumb url={v.image?.url} name={v.code} size={36} /><code>{v.code}</code><span>{v.parameters?.flatMap((p: any) => p.values?.map((value: any) => value.descriptions?.[0]?.value)).join(' / ')}</span></div>)}
      {[...item.errors, ...item.warnings].map((issue, i) => <p className={item.errors.includes(issue) ? 'catalog-error' : 'catalog-muted'} key={i}>{t(`catalog.codes.${issue}`, { defaultValue: issue })}</p>)}
      {!result && <details><summary>{t('catalog.importPayload')}</summary><pre className="catalog-source">{JSON.stringify(item.payload, null, 2)}</pre></details>}
    </details>)}</div>
    {items.length > 30 && <div className="catalog-pagination"><Button variant="secondary" size="sm" disabled={page === 1} onClick={() => setPage(page - 1)}>{t('common.back')}</Button><span>{page} / {Math.ceil(items.length / 30)}</span><Button variant="secondary" size="sm" disabled={page * 30 >= items.length} onClick={() => setPage(page + 1)}>{t('common.next')}</Button></div>}
    <div className="catalog-modal-actions"><Button variant="secondary" onClick={onClose}>{t('common.close')}</Button>
      {canRetry && <Button loading={sending} onClick={onRetry}>{t('catalog.retryImport')}</Button>}
      {!result && <Button loading={sending} disabled={!counts.ready || !!preview?.errors.length || !preview || Date.now() >= Date.parse(preview.expires_at)} onClick={onConfirm}>{t('catalog.confirmImport', { count: counts.ready || 0 })}</Button>}
    </div>
  </div></Modal>;
}
