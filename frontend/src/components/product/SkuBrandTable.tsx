import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { SkuBrand } from '../../api/skuPolicy';
import { Link } from 'react-router-dom';
import { SupplierSummary } from '../../api/suppliers';
import { validPortal } from './SkuBrandSuppliers';
import { Button } from '../ui/Button.new';
import './SkuBrandTable.css';

const pageSize = 40;

const supplierKey = (value: string) => value.normalize('NFD').toLowerCase().replace(/[^a-z0-9]+/g, '');

export function SkuBrandTable({ brands, suppliers = [], onSelect, onEdit, onRemove, onRestore }: {
  brands: SkuBrand[];
  suppliers?: SupplierSummary[];
  onSelect?: (brand: SkuBrand) => void;
  onEdit?: (brand: SkuBrand) => void;
  onRemove?: (brand: SkuBrand) => void;
  onRestore?: (brand: SkuBrand) => void;
}) {
  const { t, i18n } = useTranslation();
  const [search, setSearch] = useState('');
  const [archived, setArchived] = useState(false);
  const [page, setPage] = useState(0);
  const query = search.trim().toLocaleLowerCase();
  const matches = (value: string) => value.toLocaleLowerCase().includes(query);
  const rows = brands
    .filter(brand => !!brand.archived === archived)
    .filter(brand => [brand.name, brand.code, ...brand.aliases].some(matches) || (onEdit && (
      matches(brand.source_ref || '') || brand.reserved_codes?.some(matches) ||
      brand.suppliers?.some(supplier => matches(supplier.name) || matches(supplier.url)))))
    .sort((left, right) => left.name.localeCompare(right.name, i18n.resolvedLanguage));

  const pages = Math.ceil(rows.length / pageSize);
  const current = Math.max(0, Math.min(page, pages - 1));
  const pageRows = rows.slice(current * pageSize, (current + 1) * pageSize);
  return <div className="sku-brand-list">
    {onRestore && <div className="sku-brand-filter" role="group" aria-label={t('skuPolicy.registry.status')}>
      {[false, true].map(state => <Button key={String(state)} type="button" variant={archived === state ? 'primary' : 'secondary'} size="sm"
        aria-pressed={archived === state} onClick={() => { setArchived(state); setPage(0); }}>
        {t(state ? 'skuPolicy.registry.archivedList' : 'skuPolicy.registry.activeList')} ({brands.filter(brand => !!brand.archived === state).length})
      </Button>)}
    </div>}
    <label className="sku-brand-search">
      {t(onEdit ? 'skuPolicy.registry.searchSource' : 'skuPolicy.registry.search')}
      <input type="search" value={search} onChange={event => { setSearch(event.target.value); setPage(0); }} />
    </label>
    {!rows.length ? <p role="status">{t('skuPolicy.registry.empty')}</p> :
      <div className="sku-brand-table-scroll">
        <table className="sku-brand-table" aria-label={t('skuPolicy.registry.title')}>
          <thead><tr>
            <th scope="col">{t('skuPolicy.registry.manufacturer')}</th>
            <th scope="col">{t('skuPolicy.registry.prefix')}</th>
            {onEdit && <th scope="col">{t('skuPolicy.registry.suppliers')}</th>}
            <th scope="col">{t('skuPolicy.registry.aliases')}</th>
            {onEdit && <th scope="col">{t('skuPolicy.registry.b2bSource')}</th>}
            {onEdit && <th scope="col">{t('skuPolicy.registry.feedSource')}</th>}
            {onEdit && <th scope="col">{t('skuPolicy.registry.actions')}</th>}
            {onSelect && <th scope="col"><span className="sr-only">{t('skuPolicy.registry.choose')}</span></th>}
          </tr></thead>
          <tbody>{pageRows.map(brand => {
            const aliases = brand.aliases.filter(alias => alias !== brand.name);
            const supplierRows = onEdit && brand.suppliers?.length ? brand.suppliers : [null];
            return supplierRows.map((supplier, index) => {
              const key = supplierKey(supplier?.name || '');
              const links = supplier ? suppliers.filter(item => [item.name, item.code].some(value => supplierKey(value) === key)) : [];
              const linked = links.length === 1 ? links[0] : undefined;
              const span = supplierRows.length;
              return <tr key={`${brand.id}-${supplier?.name || ''}`}>
                {index === 0 && <><th scope="row" rowSpan={span}>{brand.name}{brand.kind !== 'brand' && <small>{t(`skuPolicy.brandKinds.${brand.kind}`)}</small>}</th>
                  <td rowSpan={span}><code>{brand.code}</code>
                    {onEdit && !!brand.reserved_codes?.length && <small className="sku-brand-previous">{t('skuPolicy.registry.previousCodes', { codes: brand.reserved_codes.join(', ') })}</small>}
                  </td></>}
                {onEdit && <td>{supplier?.name || '—'}</td>}
                {index === 0 && <td rowSpan={span}>{aliases.length ? aliases.join(', ') : '—'}</td>}
                {onEdit && <td className="sku-brand-source">{supplier?.url && validPortal(supplier.url)
                  ? <a href={supplier.url} target="_blank" rel="noreferrer">{supplier.url}</a> : '—'}</td>}
                {onEdit && <td className="sku-brand-source">{linked?.listing_feed_keys?.length ?
                  linked.listing_feed_keys.map(feed => <div key={feed}>
                    <Link to={`/suppliers/${encodeURIComponent(linked.code)}/catalog?${new URLSearchParams({ feed })}`}>{feed}</Link>
                  </div>) : '—'}</td>}
                {onEdit && index === 0 && <td rowSpan={span}><div className="sku-registry-actions">
                  {brand.archived && onRestore ? <Button type="button" size="sm" variant="secondary" aria-label={t('skuPolicy.registry.restoreName', { name: brand.name })}
                    onClick={() => onRestore(brand)}>{t('skuPolicy.registry.restore')}</Button> : <>
                    <Button type="button" size="sm" variant="secondary" aria-label={t('skuPolicy.registry.editName', { name: brand.name })}
                      onClick={() => onEdit(brand)}>{t('common.edit')}</Button>
                    {onRemove && <Button type="button" size="sm" variant="danger" aria-label={t('skuPolicy.registry.removeName', { name: brand.name })}
                      onClick={() => onRemove(brand)}>{t('skuPolicy.registry.remove')}</Button>}
                    </>}
                </div></td>}
                {onSelect && index === 0 && <td rowSpan={span}><Button type="button" size="sm" variant="secondary" aria-label={t('skuPolicy.registry.chooseName', { name: brand.name })}
                  onClick={() => onSelect(brand)}>{t('skuPolicy.registry.choose')}</Button></td>}
              </tr>;
            });
          })}</tbody>
        </table>
      </div>}
    {pages > 1 && <div className="sku-brand-pages">
      <Button type="button" size="sm" variant="secondary" disabled={current === 0} onClick={() => setPage(current - 1)}>{t('common.back')}</Button>
      <span aria-live="polite">{t('skuPolicy.registry.range', { first: current * pageSize + 1, last: Math.min((current + 1) * pageSize, rows.length), total: rows.length })}</span>
      <Button type="button" size="sm" variant="secondary" disabled={current + 1 >= pages} onClick={() => setPage(current + 1)}>{t('common.next')}</Button>
    </div>}
  </div>;
}
