import React from 'react';
import { useTranslation } from 'react-i18next';

export function IdentityFacts({ sku, name, eans, attributes, brand, supplierSku }: {
  sku: string; name: string; eans: string[]; attributes: { name: string; value: string }[];
  brand?: string | null; supplierSku?: string;
}) {
  const { t } = useTranslation();
  return <div className="identity-facts"><strong>{name}</strong><div><code>{sku}</code>{brand && <span> · {brand}</span>}</div>
    <dl>{supplierSku && <><dt>{t('supplierLinks.supplierSku')}</dt><dd><code>{supplierSku}</code></dd></>}
      <dt>EAN</dt><dd>{eans.join(', ') || t('supplierLinks.unknown')}</dd>
      {attributes.map((item, index) => <React.Fragment key={`${item.name}:${index}`}><dt>{item.name}</dt><dd>{item.value}</dd></React.Fragment>)}
    </dl></div>;
}
