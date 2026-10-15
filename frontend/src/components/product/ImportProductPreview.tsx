import React, { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';
import { ProductImportRow } from '../../api/productImport';
import { catalogImageUrl } from '../../api/catalog';
import { ProductThumb } from './ProductDisplay';
import { Button } from '../ui/Button.new';

export function ImportProductSummary({ row }: { row: ProductImportRow }) {
  const { t } = useTranslation();
  const c = (key: string) => t(`productImport.${key}`);
  const [selected, setSelected] = useState(0);
  const [large, setLarge] = useState(false);
  const v = row.values;
  const description = new window.DOMParser().parseFromString(v.description_html || '', 'text/html').body.textContent || '';
  const parameters = [...v.parameters, ...v.variant_attributes].filter((p, index, all) =>
    all.findIndex(other => other.name === p.name && other.value === p.value) === index);
  return <section className="import-product-summary" aria-label={c('productPreview')}>
    <div className="import-product-identity">
      <button type="button" onClick={() => setLarge(!large)} aria-label={c('enlargeImage')} aria-expanded={large} disabled={!v.images.length}>
        <ProductThumb key={v.images[selected]} url={catalogImageUrl(v.images[selected])} name={v.name} size={80} />
      </button>
      <div><strong>{v.name}</strong><p>{v.brand} · {v.code}</p><p>{c('sourceCategory')}: {row.source_category || '—'}</p>
        {row.source_url && <a href={row.source_url} target="_blank" rel="noopener noreferrer">{c('supplierProduct')}</a>}
      </div>
    </div>
    {large && <img className="import-product-large-image" src={catalogImageUrl(v.images[selected]) || undefined} alt={v.name} />}
    {v.images.length > 1 && <div className="import-product-gallery">{v.images.map((url, index) =>
      <button type="button" key={url} aria-label={`${c('image')} ${index + 1}`} aria-pressed={selected === index} onClick={() => { setSelected(index); setLarge(true); }}>
        <ProductThumb url={catalogImageUrl(url)} name={v.name} size={42} />
      </button>)}</div>}
    <details><summary>{c('productDetails')}</summary>
      {v.eans.length > 0 && <p>EAN: {v.eans.join(', ')}</p>}
      {v.manufacturer_code && <p>{c('fields.manufacturer_code')}: {v.manufacturer_code}</p>}
      {v.short_description && <p>{new window.DOMParser().parseFromString(v.short_description, 'text/html').body.textContent}</p>}
      {parameters.length > 0 && <dl>{parameters.map((p, index) => <React.Fragment key={index}><dt>{p.name}</dt><dd>{p.value}</dd></React.Fragment>)}</dl>}
      {description && <p className="import-product-description">{description}</p>}
    </details>
  </section>;
}

export function ImportProductPreview({ row, onClose }: { row: ProductImportRow; onClose: () => void }) {
  const { t } = useTranslation();
  const panel = useRef<HTMLDivElement>(null);
  const close = useRef(onClose); close.current = onClose;
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const overflow = document.body.style.overflow; document.body.style.overflow = 'hidden';
    panel.current?.querySelector<HTMLButtonElement>('button')?.focus();
    const key = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { event.preventDefault(); close.current(); }
      if (event.key !== 'Tab') return;
      const elements = [...(panel.current?.querySelectorAll<HTMLElement>('button:not(:disabled),a[href],summary') || [])].filter(e => e.getClientRects().length);
      const first = elements[0], last = elements[elements.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    };
    document.addEventListener('keydown', key);
    return () => { document.body.style.overflow = overflow; document.removeEventListener('keydown', key); previous?.focus(); };
  }, []);
  return createPortal(<div className="product-import-editor-backdrop"><div ref={panel} className="product-import product-import-cell-editor" role="dialog" aria-modal="true" aria-labelledby="import-preview-title">
    <header><h2 id="import-preview-title">{t('productImport.productPreview')}</h2><Button variant="ghost" onClick={onClose} aria-label={t('productImport.close')}>×</Button></header>
    <div className="import-cell-editor-body"><ImportProductSummary row={row} /></div>
  </div></div>, document.body);
}
