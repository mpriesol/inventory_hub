import React from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { HelpHint } from '../../components/ui/HelpHint';
import type { SupplierConfig } from '../../api/suppliers';

type Props = { config: SupplierConfig; updateConfig: (update: (current: SupplierConfig) => SupplierConfig) => void };

export function HuskyConnectorSettings({ config, updateConfig }: Props) {
  const { t } = useTranslation();
  const options = config.adapter_settings?.husky || {};
  const change = (patch: Partial<typeof options>) => updateConfig(current => ({ ...current,
    adapter_settings: { ...current.adapter_settings, husky: { ...current.adapter_settings?.husky, ...patch } } }));
  function setup() {
    updateConfig(current => {
      const sources = { ...current.feeds?.sources };
      for (const [key, url] of Object.entries({ products: 'https://www.huskysk.sk/feed/partners.xml', stock: 'https://www.huskysk.sk/data/partners/Katalog_export.xml' })) {
        const existing = sources[key];
        if (!existing?.remote?.url && !existing?.remote?.url_configured && !existing?.local_path) {
          sources[key] = { mode: 'remote', local_path: null, remote: { url, method: 'GET', headers: {}, params: {}, auth: { mode: 'none' } } };
        }
      }
      return { ...current, feeds: { ...current.feeds, current_key: 'products', sources } };
    });
  }
  return <section className="space-y-4 mt-6" data-testid="husky-connector">
    <h3 className="text-lg font-semibold">{t('husky.title')}<HelpHint label={t('husky.title')}>{t('husky.help')}</HelpHint></h3>
    <button type="button" className="underline" data-testid="husky-setup" onClick={setup}>{t('husky.setup')}</button><HelpHint label={t('husky.setup')}>{t('husky.saveHelp')}</HelpHint>
    <label className="flex items-center gap-2"><input type="checkbox" data-testid="husky-enabled" checked={options.b2b_enabled === true} onChange={event => change({ b2b_enabled: event.target.checked })} />{t('husky.enabled')}</label>
    <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
      <label>{t('husky.username')}<input className="w-full p-2 border rounded" data-testid="husky-username" value={options.username || ''} maxLength={200} autoComplete="off" onChange={event => change({ username: event.target.value })} /></label>
      <label>{t('husky.password')}<input className="w-full p-2 border rounded" data-testid="husky-password" type="password" value={options.password || ''} maxLength={500} autoComplete="new-password" onChange={event => change({ password: event.target.value })} />{options.password_configured && <small>{t('husky.passwordSaved')}</small>}</label>
    </div>
    <label className="block">{t('husky.priceBasis')}<HelpHint label={t('husky.priceBasis')}>{t('husky.priceHelp')}</HelpHint><select className="block p-2 border rounded" data-testid="husky-price-basis" value={options.purchase_price_basis || 'unknown'} onChange={event => change({ purchase_price_basis: event.target.value as 'unknown' | 'net' | 'gross' })}>
      <option value="unknown">{t('husky.unknown')}</option><option value="net">{t('husky.net')}</option><option value="gross">{t('husky.gross')}</option>
    </select></label>
    <Link className="underline" to="/settings/availability?supplier=husky">{t('husky.schedule')}</Link>
  </section>;
}
