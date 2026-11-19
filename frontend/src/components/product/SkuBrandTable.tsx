import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { SkuBrand } from '../../api/skuPolicy';
import { Button } from '../ui/Button.new';
import './SkuBrandTable.css';

export function SkuBrandTable({ brands, onSelect }: {
  brands: SkuBrand[];
  onSelect?: (brand: SkuBrand) => void;
}) {
  const { t, i18n } = useTranslation();
  const [search, setSearch] = useState('');
  const query = search.trim().toLocaleLowerCase();
  const rows = brands
    .filter(brand => [brand.name, brand.code, ...brand.aliases].some(value => value.toLocaleLowerCase().includes(query)))
    .sort((left, right) => left.name.localeCompare(right.name, i18n.resolvedLanguage));

  return <div className="sku-brand-list">
    <label className="sku-brand-search">
      {t('skuPolicy.registry.search')}
      <input type="search" value={search} onChange={event => setSearch(event.target.value)} />
    </label>
    {!rows.length ? <p role="status">{t('skuPolicy.registry.empty')}</p> :
      <div className="sku-brand-table-scroll">
        <table className="sku-brand-table" aria-label={t('skuPolicy.registry.title')}>
          <thead><tr>
            <th scope="col">{t('skuPolicy.registry.manufacturer')}</th>
            <th scope="col">{t('skuPolicy.registry.prefix')}</th>
            <th scope="col">{t('skuPolicy.registry.aliases')}</th>
            {onSelect && <th scope="col"><span className="sr-only">{t('skuPolicy.registry.choose')}</span></th>}
          </tr></thead>
          <tbody>{rows.map(brand => {
            const aliases = brand.aliases.filter(alias => alias !== brand.name);
            return <tr key={brand.id}>
              <th scope="row">{brand.name}{brand.kind !== 'brand' && <small>{t(`skuPolicy.brandKinds.${brand.kind}`)}</small>}</th>
              <td><code>{brand.code}</code></td>
              <td>{aliases.length ? aliases.join(', ') : '—'}</td>
              {onSelect && <td><Button type="button" size="sm" variant="secondary" aria-label={t('skuPolicy.registry.chooseName', { name: brand.name })}
                onClick={() => onSelect(brand)}>{t('skuPolicy.registry.choose')}</Button></td>}
            </tr>;
          })}</tbody>
        </table>
      </div>}
  </div>;
}
