import React, { useEffect, useState, useSyncExternalStore } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { PackageCheck, ClipboardList, RefreshCw } from 'lucide-react';
import { StatsCard } from '../components/ui/StatsCard';
import { QuickAction } from '../components/ui/Button.new';
import { ActionScope } from '../components/ui/ActionScope';
import { getDashboardStats, getRecentActivity, type DashboardStats, type ActivityItem } from '../api/dashboard';
import { accessRevision, hubUnlocked, subscribeAccess } from '../api/access';

export function DashboardPage() {
  const { t, i18n } = useTranslation();
  const navigate = useNavigate();
  const revision = useSyncExternalStore(subscribeAccess, accessRevision, accessRevision);
  const [stats, setStats] = useState<DashboardStats | null>(null);
  const [activity, setActivity] = useState<ActivityItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  useEffect(() => {
    let active = true;
    setStats(null); setActivity([]); setLoading(true); setError(false);
    Promise.all([getDashboardStats(), getRecentActivity()]).then(([summary, recent]) => {
      if (active) { setStats(summary); setActivity(recent); }
    }).catch(() => { if (active) setError(true); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [revision]);
  const number = (value: number) => value.toLocaleString(i18n.language, { maximumFractionDigits: 3 });
  return <main className="space-y-6">
    <header><h1 className="text-2xl font-semibold">{t('dashboard.title')}</h1>
      <p>{t('dashboard.subtitle')}</p>
      <ActionScope effects={['hub-read']} calls={{ kind: 'known', count: 0 }}>{t('dashboard.localOnly')}</ActionScope>
    </header>
    {loading && <p role="status">{t('dashboard.loading')}</p>}
    {error && <p role="alert">{t('dashboard.loadError')}</p>}
    {stats && <>
      <p data-testid="dashboard-confirmed-stock">{t('dashboard.confirmedProducts', { count: stats.confirmedProducts ?? 0 })} <Link to="/stock">{t('dashboard.openStock')}</Link></p>
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        <StatsCard icon="📦" value={number(stats.totalProducts)} label={t('dashboard.products')} />
        <StatsCard icon="⚠" value={number(stats.lowStockCount)} label={t('dashboard.lowStock')} sublabel={t('dashboard.lowStockHint')} />
        <StatsCard icon="🛒" value={number(stats.openOrders)} label={t('dashboard.openOrders')} sublabel={t('dashboard.openOrdersHint')} onClick={() => navigate('/orders/inbox')} />
        <StatsCard icon="€" value={stats.inventoryValue === null ? t('stock.unknownValue') : new Intl.NumberFormat(i18n.language, { style: 'currency', currency: 'EUR' }).format(stats.inventoryValue)} label={t('dashboard.inventoryValue')} sublabel={t('stock.valuationBasis')} />
      </div>
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        <StatsCard icon="📦" value={number(stats.onHand)} label={t('dashboard.onHand')} />
        <StatsCard icon="📋" value={number(stats.reserved)} label={t('dashboard.reserved')} />
        <StatsCard icon="✓" value={number(stats.available)} label={t('dashboard.available')} />
        <StatsCard icon="⏸" value={number(stats.quarantined)} label={t('dashboard.quarantined')} />
      </div>
    </>}
    <section className="rounded-xl border p-6">
      <h2 className="mb-4">{t('dashboard.quickActions')}</h2>
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <QuickAction icon={<PackageCheck size={24} />} label={t('dashboard.receiving')} description={t('dashboard.receivingHint')} onClick={() => navigate('/receiving')} />
        <QuickAction icon={<ClipboardList size={24} />} label={t('stockHistory.title')} description={t('dashboard.historyHint')} onClick={() => navigate('/stock/movements')} />
        <QuickAction icon={<RefreshCw size={24} />} label={t('dashboard.settings')} description={t('dashboard.settingsHint')} onClick={() => navigate('/settings/stock')} />
      </div>
    </section>
    <section className="rounded-xl border p-6">
      <h2>{t('dashboard.recent')}</h2>
      {!hubUnlocked() ? <p><Link to="/stock/movements">{t('dashboard.unlockHistory')}</Link></p> : <>
        {!loading && !error && !activity.length && <p>{t('dashboard.noActivity')}</p>}
        <ul>{activity.map(item => <li key={item.id} className="py-3 border-b">
          <Link to={`/stock/movements?sku=${encodeURIComponent(item.sku)}`}>{item.sku}</Link> — {item.name}
          {' · '}{t(`stockHistory.types.${item.movementType}`, item.movementType)}{' · '}{Number(item.quantity) > 0 ? '+' : ''}{number(Number(item.quantity))}
          <small className="block">{new Date(item.timestamp).toLocaleString(i18n.language)}{item.reference ? ` · ${item.reference}` : ''}</small>
        </li>)}</ul>
        <Link to="/stock/movements">{t('dashboard.allMovements')}</Link>
      </>}
    </section>
  </main>;
}
