import React, { useState } from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Button } from '../components/ui/Button.new';
import { UpgatesImportModal } from '../components/UpgatesImportModal';

export function ShopsPage() {
  const { t } = useTranslation();
  const [pullShop, setPullShop] = useState<string | null>(null);
  return <div className="space-y-6"><h1 className="text-2xl font-semibold">{t('shopsOverview.title')}</h1><p>{t('shopsOverview.help')}</p>
    <div className="grid md:grid-cols-2 gap-6">{['biketrek', 'xtrek'].map(shop => <section key={shop} className="rounded-xl border p-6 space-y-4" style={{ borderColor: 'var(--color-border-subtle)' }}>
      <h2 className="text-xl font-semibold">{shop === 'biketrek' ? 'BIKETREK' : 'xTrek'}</h2>
      <p>{t('shopsOverview.statusHelp')}</p>
      <Link className="block" to={`/orders/inbox?shop=${shop}`}>{t('shopsOverview.orders')}</Link>
      <Link className="block" to={`/settings/availability?shop=${shop}`}>{t('shopsOverview.availability')}</Link>
      <Link className="block" to={`/stock/publication?shop=${shop}`}>{t('shopsOverview.stock')}</Link>
      <Link className="block" to="/product-import">{t('nav.productImport')}</Link>
      {shop === 'xtrek' && <Link className="block" to="/migration/atomer-progress">{t('atomerProgress.title')}</Link>}
      {shop === 'xtrek' && <Link className="block" to="/migration/atomer">{t('atomerContent.title')}</Link>}
      <a className="block" href={`/api/migration/catalog-export?shop_code=${shop}&refresh=true`}>{t('shopsOverview.catalogExport')}</a>
      <Button variant="secondary" onClick={() => setPullShop(shop)}>{t('upgatesImport.title', { shop: shop === 'biketrek' ? 'BIKETREK' : 'xTrek' })}</Button>
    </section>)}</div>
    <Link to="/settings/stock">{t('stockSettings.title')}</Link>
    {pullShop && <UpgatesImportModal shop={pullShop} onClose={() => setPullShop(null)} onImported={() => {}} />}
  </div>;
}

export function SettingsPage() {
  const { t } = useTranslation();
  return (
    <div className="space-y-6"><h1 className="text-2xl font-semibold">{t('ai.settings')}</h1>
      <Link className="block rounded-xl border p-6" style={{ borderColor: 'var(--color-border-subtle)', background: 'var(--color-bg-secondary)' }} to="/settings/users">
        <strong>{t('accounts.title')}</strong><p>{t('accounts.remember')}</p>
      </Link>
      <Link className="block rounded-xl border p-6" style={{ borderColor: 'var(--color-border-subtle)', background: 'var(--color-bg-secondary)' }} to="/settings/ai-content">
        <strong>{t('ai.title')}</strong><p>{t('ai.settingsDescription')}</p>
      </Link>
      <Link className="block rounded-xl border p-6" style={{ borderColor: 'var(--color-border-subtle)', background: 'var(--color-bg-secondary)' }} to="/settings/availability">
        <strong>{t('availabilitySync.title')}</strong><p>{t('availabilitySync.subtitle')}</p>
      </Link>
      <Link className="block rounded-xl border p-6" style={{ borderColor: 'var(--color-border-subtle)', background: 'var(--color-bg-secondary)' }} to="/settings/logs">
        <strong>{t('operationLogs.title')}</strong>
      </Link>
      <Link className="block rounded-xl border p-6" style={{ borderColor: 'var(--color-border-subtle)', background: 'var(--color-bg-secondary)' }} to="/settings/purchase-costs">
        <strong>{t('fifoCostSync.title')}</strong><p>{t('fifoCostSync.subtitle')}</p>
      </Link>
      <Link className="block rounded-xl border p-6" style={{ borderColor: 'var(--color-border-subtle)', background: 'var(--color-bg-secondary)' }} to="/settings/stock">
        <strong>{t('stockSettings.title')}</strong><p>{t('stockSettings.subtitle')}</p>
      </Link>
      <Link className="block rounded-xl border p-6" style={{ borderColor: 'var(--color-border-subtle)', background: 'var(--color-bg-secondary)' }} to="/stock/publication">
        <strong>{t('stockPublication.title')}</strong><p>{t('stockPublication.subtitle')}</p>
      </Link></div>
  );
}
