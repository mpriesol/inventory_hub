import React, { Fragment, useEffect, useRef, useState } from 'react';
import { ChevronDown, ChevronRight, Info, AlertTriangle } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { CatalogProduct, CatalogRow } from '../../api/catalog';
import { ProductThumb } from './ProductDisplay';

export function CatalogCheckbox({ ids, selected, onToggle, label, disabled = false }: {
  ids: number[]; selected: Set<number>; onToggle: (ids: number[]) => void; label: string; disabled?: boolean;
}) {
  const ref = useRef<HTMLInputElement>(null);
  const count = ids.filter(id => selected.has(id)).length;
  useEffect(() => { if (ref.current) ref.current.indeterminate = count > 0 && count < ids.length; }, [count, ids.length]);
  return <input ref={ref} type="checkbox" aria-label={label} checked={ids.length > 0 && count === ids.length}
    disabled={disabled || !ids.length} onClick={e => e.stopPropagation()} onChange={() => onToggle(ids)} />;
}

export function CatalogTable({ rows, selected, onToggle, onDetail, busy, showPrice, showAvailability }: {
  rows: CatalogRow[]; selected: Set<number>; onToggle: (ids: number[]) => void; onDetail: (p: CatalogProduct) => void;
  busy: boolean; showPrice: boolean; showAvailability: boolean;
}) {
  const { t, i18n } = useTranslation();
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const toggle = (key: string) => setExpanded(old => { const next = new Set(old); next.has(key) ? next.delete(key) : next.add(key); return next; });
  const formatPrice = (p: CatalogProduct) => p.prices.retail_gross === null ? '—' : new Intl.NumberFormat(i18n.language, { style: 'currency', currency: p.prices.currency }).format(Number(p.prices.retail_gross));
  function cells(p: CatalogProduct, row?: CatalogRow) {
    const group = !!row?.is_group;
    return <>
      <td className="catalog-check"><CatalogCheckbox ids={group ? row.matching_ids : [p.id]} selected={selected} onToggle={onToggle} disabled={busy}
        label={t(group ? 'catalog.selectGroup' : 'catalog.selectProduct', { name: group ? p.group_name || p.name : p.name })} /></td>
      <td><div className="catalog-product-cell">
        {group ? <button className="catalog-icon" aria-expanded={expanded.has(row.key)} aria-label={t('catalog.expandVariants')} onClick={e => { e.stopPropagation(); toggle(row.key); }}>
          {expanded.has(row.key) ? <ChevronDown size={18} /> : <ChevronRight size={18} />}</button> : <span className="catalog-indent" />}
        <ProductThumb key={p.images[0] || p.id} url={p.images[0]} name={p.name} size={52} />
        <div><button className="catalog-name" onClick={e => { e.stopPropagation(); group ? toggle(row.key) : onDetail(p); }}>
          {group ? p.group_name || p.name : p.name}</button>
          <div className="catalog-muted">{p.brand}{group ? ` · ${t('catalog.variantCount', { count: row.variants_count })}` : p.variant_attributes.length ? ` · ${p.variant_attributes.map(a => a.value).join(' / ')}` : ''}</div>
        </div>
      </div></td>
      <td><code>{group ? p.group_code : p.shop_code}</code><div className="catalog-muted">{group ? t('catalog.supplierGroup') : p.eans[0] || t('catalog.noEan')}</div></td>
      {showPrice && <td className="catalog-number">{group ? t('catalog.fromPrice', { price: formatPrice(row.variants.reduce((low, v) => Number(v.prices.retail_gross ?? Infinity) < Number(low.prices.retail_gross ?? Infinity) ? v : low, p)) }) : formatPrice(p)}</td>}
      {showAvailability && <td>{group ? '—' : <><span>{p.supplier_stock_raw ?? '—'}</span><div className="catalog-muted">{p.availability || ''}</div></>}</td>}
      <td><span className={`catalog-badge ${p.listed ? 'catalog-good' : ''}`}>{t(p.listed ? 'catalog.listed' : 'catalog.notLinked')}</span>
        {p.warnings.length > 0 && <span className="catalog-warning-icon" title={p.warnings.map(w => t(`catalog.codes.${w}`, { defaultValue: w })).join('\n')}><AlertTriangle size={14} /></span>}</td>
      <td><button className="catalog-icon" aria-label={t('catalog.detailOf', { name: p.name })} onClick={e => { e.stopPropagation(); onDetail(p); }}><Info size={18} /></button></td>
    </>;
  }
  return <div className="catalog-table-scroll"><table className="catalog-table"><thead><tr>
    <th><CatalogCheckbox ids={rows.flatMap(r => r.matching_ids)} selected={selected} onToggle={onToggle} disabled={busy} label={t('catalog.selectPage')} /></th>
    <th>{t('catalog.product')}</th><th>{t('catalog.codeEan')}</th>
    {showPrice && <th>{t('catalog.retailGross')}</th>}{showAvailability && <th>{t('catalog.supplierStock')}</th>}
    <th>{t('catalog.shopStatus')}</th><th><span className="sr-only">{t('catalog.detail')}</span></th>
  </tr></thead><tbody>{rows.map(row => <Fragment key={row.key}>
    <tr className={row.is_group ? 'catalog-group' : ''} onClick={() => row.is_group ? toggle(row.key) : onDetail(row.product)}>{cells(row.product, row)}</tr>
    {row.is_group && expanded.has(row.key) && row.variants.map(p => <tr key={p.id} className="catalog-variant" onClick={() => onDetail(p)}>{cells(p)}</tr>)}
  </Fragment>)}</tbody></table></div>;
}
