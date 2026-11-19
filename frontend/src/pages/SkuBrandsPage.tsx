import React, { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { getSkuBrands, SkuBrand } from '../api/skuPolicy';
import { SkuBrandTable } from '../components/product/SkuBrandTable';
import { Button } from '../components/ui/Button.new';

export function SkuBrandsPage() {
  const { t } = useTranslation();
  const [brands, setBrands] = useState<SkuBrand[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const request = new AbortController();
    getSkuBrands(request.signal).then(result => {
      if (!request.signal.aborted) setBrands(result.brands);
    }).catch(() => {
      if (!request.signal.aborted) setFailed(true);
    });
    return () => request.abort();
  }, [attempt]);

  return <div className="space-y-4">
    <Link to="/settings">{t('common.settings')}</Link>
    <h1 className="text-2xl font-semibold">{t('skuPolicy.registry.title')}</h1>
    <p>{t('skuPolicy.registry.help')}</p>
    {failed ? <div role="alert">
      <p>{t('skuPolicy.errors.registryUnavailable')}</p>
      <Button variant="secondary" onClick={() => { setFailed(false); setAttempt(value => value + 1); }}>{t('skuPolicy.retry')}</Button>
    </div> : brands ? <SkuBrandTable brands={brands} /> : <p role="status">{t('common.loading')}</p>}
  </div>;
}
