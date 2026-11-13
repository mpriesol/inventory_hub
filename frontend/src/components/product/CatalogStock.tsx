import React from 'react';
import { useTranslation } from 'react-i18next';
import { CatalogProduct } from '../../api/catalog';

// Display normalized Husky evidence; retain the original value in the source snapshot.
// Public XML flags never stand in for wholesale availability or a piece count.
export function CatalogStock({ product: p }: { product: CatalogProduct }) {
  const { t } = useTranslation();
  let label = p.supplier_stock_raw ?? '—';
  let detail = p.availability || '';
  if (p.supplier === 'husky') {
    const expires = Date.parse(p.supplier_observation_expires_at || '');
    if (!Number.isFinite(expires) || expires <= Date.now()) {
      label = t('husky.stockUnknown');
      detail = t('husky.stockExpired');
    } else if (p.warnings.includes('husky_public_stock_only')) {
      label = t('husky.stockUnknown');
      detail = t('husky.publicNoQuantity');
    } else if (p.supplier_available == null) {
      label = t('husky.stockUnknown');
    }
  }
  return <span className="inline-flex flex-col"><span>{label}</span>{detail && <span className="catalog-muted">{detail}</span>}</span>;
}
