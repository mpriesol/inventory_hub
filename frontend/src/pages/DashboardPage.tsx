import React, { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { PackageCheck, ClipboardList, RefreshCw, Loader2 } from 'lucide-react';
import { StatsCard } from '../components/ui/StatsCard';
import { QuickAction } from '../components/ui/Button.new';
import { getDashboardStats, getRecentActivity, type DashboardStats, type ActivityItem } from '../api/dashboard';

// Activity Item Component
interface ActivityItemProps {
  time: string;
  message: string;
  type?: 'default' | 'success' | 'warning' | 'info';
}

function ActivityItemRow({ time, message, type = 'default' }: ActivityItemProps) {
  const typeColors = {
    default: 'var(--color-border-subtle)',
    success: 'var(--color-success)',
    warning: 'var(--color-warning)',
    info: 'var(--color-info)',
  };

  return (
    <div
      className="flex items-start gap-3 py-3 border-b last:border-0"
      style={{ borderColor: 'var(--color-border-subtle)' }}
    >
      <div
        className="w-2 h-2 rounded-full mt-1.5 flex-shrink-0"
        style={{ backgroundColor: typeColors[type] }}
      />
      <div className="flex-1 min-w-0">
        <div
          className="text-sm"
          style={{ color: 'var(--color-text-primary)' }}
        >
          {message}
        </div>
        <div
          className="text-xs mt-0.5"
          style={{ color: 'var(--color-text-tertiary)' }}
        >
          {time}
        </div>
      </div>
    </div>
  );
}

function formatRelativeTime(isoString: string): string {
  const date = new Date(isoString);
  const now = new Date();
  const diffMs = now.getTime() - date.getTime();
  const diffMins = Math.floor(diffMs / 60000);
  const diffHours = Math.floor(diffMs / 3600000);
  const diffDays = Math.floor(diffMs / 86400000);

  if (diffMins < 1) return 'Práve teraz';
  if (diffMins < 60) return `Pred ${diffMins} min`;
  if (diffHours < 24) return `Pred ${diffHours}h`;
  if (diffDays === 1) return 'Včera';
  return date.toLocaleDateString('sk-SK');
}

function activityTypeToColor(type: ActivityItem['type']): 'default' | 'success' | 'warning' | 'info' {
  switch (type) {
    case 'receiving': return 'success';
    case 'sync': return 'info';
    case 'invoice': return 'warning';
    case 'count': return 'default';
    default: return 'default';
  }
}

export function DashboardPage() {
  const navigate = useNavigate();
  const [stats, setStats] = useState<DashboardStats | null>(null);
  const [activity, setActivity] = useState<ActivityItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    async function loadData() {
      try {
        setLoading(true);
        const [statsData, activityData] = await Promise.all([
          getDashboardStats(),
          getRecentActivity(),
        ]);
        setStats(statsData);
        setActivity(activityData);
        setError(null);
      } catch (err) {
        console.error('Failed to load dashboard:', err);
        setError('Nepodarilo sa načítať dáta');
      } finally {
        setLoading(false);
      }
    }
    loadData();
  }, []);

  // Format current date
  const today = new Date().toLocaleDateString('sk-SK', {
    weekday: 'long',
    day: 'numeric',
    month: 'long',
    year: 'numeric',
  });

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <Loader2 
          className="animate-spin" 
          size={32} 
          style={{ color: 'var(--color-accent)' }} 
        />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1
            className="text-2xl font-semibold"
            style={{
              fontFamily: 'var(--font-display)',
              color: 'var(--color-text-primary)',
            }}
          >
            Vitaj späť, Miro
          </h1>
          <p
            className="text-sm mt-1"
            style={{ color: 'var(--color-text-tertiary)' }}
          >
            Tu je prehľad stavu tvojho skladu.
          </p>
        </div>
        <div
          className="text-sm"
          style={{ color: 'var(--color-text-tertiary)' }}
        >
          {today}
        </div>
      </div>

      {error && (
        <div
          className="p-4 rounded-lg border"
          style={{
            backgroundColor: 'var(--color-error-subtle)',
            borderColor: 'var(--color-error)',
            color: 'var(--color-error)',
          }}
        >
          {error}
        </div>
      )}

      {/* Main Stats Grid */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        <StatsCard
          icon="📦"
          value={stats?.totalProducts.toLocaleString('sk-SK') || '0'}
          label="Celkom produktov"
          sublabel="+12 tento týždeň"
        />
        <StatsCard
          icon="⚠️"
          value={stats?.lowStockCount.toString() || '0'}
          label="Nízky stav"
          sublabel="Vyžaduje pozornosť"
          variant="warning"
          onClick={() => navigate('/stock?filter=low')}
        />
        <StatsCard
          icon="🛒"
          value={stats?.openOrders.toString() || '0'}
          label="Otvorené objednávky"
          sublabel="Rezervované položky"
        />
        <StatsCard
          icon="💰"
          value={`€${stats?.inventoryValue.toLocaleString('sk-SK') || '0'}`}
          label="Hodnota skladu"
          sublabel="WAC valuácia"
        />
      </div>

      {/* Secondary Stats */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <StatsCard
          icon="📥"
          value={stats?.pendingInvoices.toString() || '0'}
          label="Čakajúce faktúry"
          sublabel="Pripravené na príjem"
          variant={stats?.pendingInvoices ? 'warning' : 'default'}
          onClick={() => navigate('/receiving')}
        />
        <StatsCard
          icon="✓"
          value={`${stats?.syncStatus.percentage || 0}%`}
          label="Stav synchronizácie"
          sublabel={`${stats?.syncStatus.shopsConnected || 0} shopov pripojených`}
          variant="success"
        />
        <StatsCard
          icon="🔄"
          value={stats?.syncStatus.lastSync ? formatRelativeTime(stats.syncStatus.lastSync) : 'N/A'}
          label="Posledná synchronizácia"
          sublabel="BikeTrek e-shop"
        />
      </div>

      {/* Quick Actions */}
      <div
        className="rounded-xl border p-6"
        style={{
          backgroundColor: 'var(--color-bg-secondary)',
          borderColor: 'var(--color-border-subtle)',
        }}
      >
        <h2
          className="text-sm font-medium uppercase tracking-wider mb-4"
          style={{ color: 'var(--color-text-primary)' }}
        >
          Rýchle akcie
        </h2>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <QuickAction
            icon={<PackageCheck size={24} />}
            label="Začať príjem"
            description="Spracovať novú faktúru"
            onClick={() => navigate('/receiving')}
          />
          <QuickAction
            icon={<ClipboardList size={24} />}
            label="Inventúra"
            description="Spustiť počítanie skladu"
            onClick={() => navigate('/stock/count')}
          />
          <QuickAction
            icon={<RefreshCw size={24} />}
            label="Synchronizovať"
            description="Push stav do shopov"
            onClick={() => navigate('/shops')}
          />
        </div>
      </div>

      {/* Recent Activity */}
      <div
        className="rounded-xl border p-6"
        style={{
          backgroundColor: 'var(--color-bg-secondary)',
          borderColor: 'var(--color-border-subtle)',
        }}
      >
        <h2
          className="text-sm font-medium uppercase tracking-wider mb-4"
          style={{ color: 'var(--color-text-primary)' }}
        >
          Posledná aktivita
        </h2>
        <div>
          {activity.map((item) => (
            <ActivityItemRow
              key={item.id}
              time={formatRelativeTime(item.timestamp)}
              message={item.message}
              type={activityTypeToColor(item.type)}
            />
          ))}
          {activity.length === 0 && (
            <div
              className="text-sm py-4 text-center"
              style={{ color: 'var(--color-text-tertiary)' }}
            >
              Žiadna aktivita
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

export default DashboardPage;
