import React, { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { CatalogDetail as Detail, CatalogProduct, catalogRequest } from '../../api/catalog';
import { Modal } from '../ui/Modal';
import { ProductThumb } from './ProductDisplay';
import { Button } from '../ui/Button.new';

export function CatalogDetail({ supplier, product, shop, onClose, onToggle, selected }: {
  supplier: string; product: CatalogProduct; shop: string; onClose: () => void; onToggle: (ids: number[]) => void; selected: Set<number>;
}) {
  const { t } = useTranslation();
  const [data, setData] = useState<Detail | null>(null);
  const [error, setError] = useState('');
  const [tab, setTab] = useState<'data' | 'description' | 'xml'>('data');
  const [image, setImage] = useState(product.images[0]);
  useEffect(() => {
    const controller = new AbortController();
    catalogRequest<Detail>(`/suppliers/${encodeURIComponent(supplier)}/catalog/products/${product.id}?shop=${encodeURIComponent(shop)}`, undefined, controller.signal)
      .then(setData).catch(e => { if (e.name !== 'AbortError') setError(e.code || 'request_failed'); });
    return () => controller.abort();
  }, [supplier, product.id, shop]);
  const p = data?.product || product;
  return <Modal open onClose={onClose} title={p.name}><div className="supplier-catalog catalog-detail" role="dialog" aria-label={p.name}>
    <div className="catalog-detail-head"><div><ProductThumb key={image} url={image} name={p.name} size={220} />
      <div className="catalog-gallery">{p.images.map((url, i) => <button key={url} aria-label={t('catalog.imageNumber', { count: i + 1 })} onClick={() => setImage(url)}><ProductThumb url={url} name={p.name} size={44} /></button>)}</div></div>
      <div><div className="catalog-eyebrow">{p.brand}</div><h2>{p.name}</h2><code>{p.shop_code}</code>
        <p className="catalog-muted">{p.category || t('catalog.noCategory')}</p><p>EAN: {p.eans.join(', ') || '—'}</p>
        <p>{t('catalog.supplierStock')}: {p.supplier_stock_raw ?? '—'} · {p.availability || '—'}</p>
        {p.supplier_stock_external_raw && <p>{t('catalog.externalAvailability')}: {p.supplier_stock_external_raw}</p>}
        {p.url && <a href={p.url} target="_blank" rel="noreferrer">{t('catalog.supplierLink')} ↗</a>}
        <div className="catalog-actions"><Button onClick={() => onToggle([p.id])}>{t(selected.has(p.id) ? 'catalog.removeSelection' : 'catalog.addSelection')}</Button></div>
      </div></div>
    <div className="catalog-tabs" role="tablist">{(['data', 'description', 'xml'] as const).map(value => <button role="tab" aria-selected={tab === value} key={value} onClick={() => setTab(value)}>{t(`catalog.tab.${value}`)}</button>)}</div>
    {error && <p role="alert" className="catalog-error">{t(`catalog.codes.${error}`, { defaultValue: error })}</p>}
    {!data && !error ? <p role="status">{t('common.loading')}</p> : tab === 'xml' ? <>
      <a href={`/api/suppliers/${encodeURIComponent(supplier)}/catalog/products/${p.id}/source`} download>{t('catalog.downloadXml')}</a>
      <pre className="catalog-source">{data?.source_xml}</pre>
    </> : tab === 'description' ? <div className="catalog-description" dangerouslySetInnerHTML={{ __html: data?.description_html || '' }} /> : <>
      <div className="catalog-price-grid">{(['retail_gross', 'retail_net', 'purchase_gross', 'purchase_net'] as const).map(key => <div key={key}><small>{t(`catalog.price.${key}`)}</small><strong>{p.prices[key] ?? '—'} {p.prices.currency}</strong></div>)}</div>
      <p className="catalog-muted">{t('catalog.vat')}: {p.prices.vat_percent ?? '—'} %</p>
      {p.warnings.length > 0 && <div className="catalog-notice">{p.warnings.map(w => <div key={w}>{t(`catalog.codes.${w}`, { defaultValue: w })}</div>)}</div>}
      <table className="catalog-parameters"><tbody>{p.parameters.map((a, i) => <tr key={i}><th>{a.name}</th><td>{a.value}</td></tr>)}</tbody></table>
      <details><summary>{t('catalog.allSourceFields')}</summary><pre className="catalog-source">{JSON.stringify(data?.source_fields, null, 2)}</pre></details>
    </>}
  </div></Modal>;
}
