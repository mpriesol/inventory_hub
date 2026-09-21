import React from 'react';
import { useTranslation } from 'react-i18next';
import { CatalogShopMatch } from '../../api/catalog';

export function CatalogMatches({ matches = [] }: { matches?: CatalogShopMatch[] }) {
  const { t } = useTranslation();
  return <>{matches.map((match, i) => <div className="catalog-muted" key={i}>
    {t(`catalog.matchBy.${match.matched_by}`)}: <code>{match.value}</code> → <code>{match.code || match.parent_code}</code>
    {match.parent_code && match.parent_code !== match.code && <span> · {t('catalog.existingParent')}: <code>{match.parent_code}</code></span>}
  </div>)}</>;
}
