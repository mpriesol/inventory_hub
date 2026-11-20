import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { SkuBrand } from '../../api/skuPolicy';
import { Button } from '../ui/Button.new';
import './SkuBrandTable.css';

const pageSize = 40;

function RegistrySource({ value }: { value?: string }) {
  const { t } = useTranslation();
  if (!value) return <>—</>;
  const lines = value.split('\n').filter(Boolean).map(line => {
    const match = line.match(/^(?:(.+?)\s+—\s+)?(https?:\/\/[^\s]+)$/i);
    if (match) return { label: match[1] || match[2], url: match[2] };
    return { label: line === 'operator:sku-registry' ? t('skuPolicy.registry.manual') : line, url: '' };
  });
  const entries = lines.map(({ label, url }, index) => <div key={index}>
    {url ? <a href={url} target="_blank" rel="noreferrer">{label}</a> : label}
  </div>);
  if (lines.length < 2) return <>{entries}</>;
  return <details>
    <summary>{lines[0].label} (+{lines.length - 1})</summary>
    {entries}
  </details>;
}

export function SkuBrandTable({ brands, onSelect, onEdit, onRemove, onRestore }: {
  brands: SkuBrand[];
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
  const rows = brands
    .filter(brand => !!brand.archived === archived)
    .filter(brand => [brand.name, brand.code, onEdit ? brand.source_ref || '' : '', ...(onEdit ? brand.reserved_codes ?? [] : []), ...brand.aliases].some(value => value.toLocaleLowerCase().includes(query)))
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
            <th scope="col">{t('skuPolicy.registry.aliases')}</th>
            {onEdit && <th scope="col">{t('skuPolicy.registry.source')}</th>}
            {onEdit && <th scope="col">{t('skuPolicy.registry.actions')}</th>}
            {onSelect && <th scope="col"><span className="sr-only">{t('skuPolicy.registry.choose')}</span></th>}
          </tr></thead>
          <tbody>{pageRows.map(brand => {
            const aliases = brand.aliases.filter(alias => alias !== brand.name);
            return <tr key={brand.id}>
              <th scope="row">{brand.name}{brand.kind !== 'brand' && <small>{t(`skuPolicy.brandKinds.${brand.kind}`)}</small>}</th>
              <td><code>{brand.code}</code>
                {onEdit && !!brand.reserved_codes?.length && <small className="sku-brand-previous">{t('skuPolicy.registry.previousCodes', { codes: brand.reserved_codes.join(', ') })}</small>}
              </td>
              <td>{aliases.length ? aliases.join(', ') : '—'}</td>
              {onEdit && <td className="sku-brand-source"><RegistrySource value={brand.source_ref} /></td>}
              {onEdit && <td><div className="sku-registry-actions">
                {brand.archived && onRestore ? <Button type="button" size="sm" variant="secondary" aria-label={t('skuPolicy.registry.restoreName', { name: brand.name })}
                  onClick={() => onRestore(brand)}>{t('skuPolicy.registry.restore')}</Button> : <>
                <Button type="button" size="sm" variant="secondary" aria-label={t('skuPolicy.registry.editName', { name: brand.name })}
                  onClick={() => onEdit(brand)}>{t('common.edit')}</Button>
                {onRemove && <Button type="button" size="sm" variant="danger" aria-label={t('skuPolicy.registry.removeName', { name: brand.name })}
                  onClick={() => onRemove(brand)}>{t('skuPolicy.registry.remove')}</Button>}
                </>}
              </div></td>}
              {onSelect && <td><Button type="button" size="sm" variant="secondary" aria-label={t('skuPolicy.registry.chooseName', { name: brand.name })}
                onClick={() => onSelect(brand)}>{t('skuPolicy.registry.choose')}</Button></td>}
            </tr>;
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
