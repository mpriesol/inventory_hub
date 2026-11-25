import React, { useId, useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { SkuBrand, SkuBrandSupplier } from '../../api/skuPolicy';
import { Button } from '../ui/Button.new';

const supplierKey = (name: string) => name.trim().replace(/\s+/g, ' ').toLowerCase();

export function validPortal(value: string) {
  try {
    const url = new URL(value);
    return /^https?:\/\//i.test(value) && !/[#\s\\<>"']/.test(value)
      && !!url.hostname && !url.username && !url.password;
  } catch {
    return false;
  }
}

export function supplierInputError(suppliers: SkuBrandSupplier[]) {
  const names = suppliers.map(supplier => supplierKey(supplier.name)).filter(Boolean);
  if (new Set(names).size !== names.length) return 'duplicateSupplier';
  if (suppliers.some(supplier => supplier.url.trim() && !validPortal(supplier.url.trim()))) return 'invalidPortal';
  return '';
}

export function BrandSuppliersEditor({ brands, suppliers, onChange }: {
  brands: SkuBrand[]; suppliers: SkuBrandSupplier[]; onChange: (suppliers: SkuBrandSupplier[]) => void;
}) {
  const { t } = useTranslation();
  const choicesId = useId();
  const suggestions = useMemo(() => {
    const known = new Map<string, { name: string; urls: Set<string> }>();
    for (const brand of brands) {
      for (const entry of brand.suppliers ?? []) {
        const key = supplierKey(entry.name);
        const supplier = known.get(key) ?? { name: entry.name, urls: new Set<string>() };
        if (entry.url) supplier.urls.add(entry.url);
        known.set(key, supplier);
      }
    }
    return known;
  }, [brands]);

  function setName(index: number, name: string) {
    const selected = suggestions.get(supplierKey(name));
    const current = suppliers[index];
    let url = current.url;
    if (selected?.urls.size === 1) url = Array.from(selected.urls)[0];
    else if (suggestions.get(supplierKey(current.name))?.urls.has(url)) url = '';
    onChange(suppliers.map((item, at) => at === index ? { name, url } : item));
  }
  return <fieldset className="sku-supplier-editor">
    <legend>{t('skuPolicy.registry.suppliers')}</legend>
    <datalist id={choicesId}>{Array.from(suggestions.values()).sort((a, b) => a.name.localeCompare(b.name))
      .map(supplier => <option key={supplier.name} value={supplier.name} />)}</datalist>
    {suppliers.map((supplier, index) => <div key={index} className="sku-supplier-row">
      <label>{t('skuPolicy.registry.supplierName')}
        <input required pattern={String.raw`.*\S.*`} maxLength={100} list={choicesId} value={supplier.name}
          onChange={event => setName(index, event.target.value)} /></label>
      <label>{t('skuPolicy.registry.portalUrl')}
        <input type="url" maxLength={2048} placeholder="https://" value={supplier.url}
          onChange={event => onChange(suppliers.map((item, at) => at === index ? { ...item, url: event.target.value } : item))} /></label>
      <Button type="button" variant="ghost" size="sm" aria-label={t('skuPolicy.registry.unlinkSupplier', { name: supplier.name || index + 1 })}
        onClick={() => onChange(suppliers.filter((_, at) => at !== index))}>{t('skuPolicy.registry.remove')}</Button>
    </div>)}
    <Button type="button" size="sm" variant="secondary" disabled={suppliers.length >= 200}
      onClick={() => onChange([...suppliers, { name: '', url: '' }])}>{t('skuPolicy.registry.addSupplier')}</Button>
  </fieldset>;
}
