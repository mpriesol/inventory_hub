import React, { useEffect, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { Search, Download, RefreshCw, AlertTriangle } from 'lucide-react';
import { StatsCard } from '../components/ui/StatsCard';
import { Button } from '../components/ui/Button.new';
import { getStockItems, getStockSummary, type StockSummary } from '../api/stock';
import { UpgatesImportModal } from '../components/UpgatesImportModal';

interface StockItem {
  sku: string;
  name: string;
  brand: string;
  onHand: number;
  reserved: number;
  available: number;
  avgCost: number;
  lowStock: boolean;
}

export function StockPage() {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();

  const [search, setSearch] = useState('');
  const [lowStockOnly, setLowStockOnly] = useState(searchParams.get('filter') === 'low');
  const [items, setItems] = useState<StockItem[]>([]);
  const [summary, setSummary] = useState<StockSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [upgatesShop, setUpgatesShop] = useState<string | null>(null);

  const loadData = async () => {
    setLoading(true);
    setError(null);
    try {
      const [apiItems, apiSummary] = await Promise.all([getStockItems(), getStockSummary()]);
      setItems(apiItems.map((it) => ({
        sku: it.sku,
        name: it.name,
        brand: it.brand,
        onHand: it.on_hand,
        reserved: it.reserved,
        available: it.available,
        avgCost: it.avg_cost,
        lowStock: it.low_stock,
      })));
      setSummary(apiSummary);
    } catch (e: any) {
      setError(e?.message || 'Nepodarilo sa načítať sklad');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { loadData(); }, []);

  const brands = Array.from(new Set(items.map((i) => i.brand).filter(Boolean))).sort();
  const [brandFilter, setBrandFilter] = useState('');

  const filteredItems = items.filter((item) => {
    const matchesSearch = !search ||
      item.sku.toLowerCase().includes(search.toLowerCase()) ||
      item.name.toLowerCase().includes(search.toLowerCase()) ||
      item.brand.toLowerCase().includes(search.toLowerCase());

    const matchesBrand = !brandFilter || item.brand === brandFilter;
    const matchesLowStock = !lowStockOnly || item.lowStock;

    return matchesSearch && matchesBrand && matchesLowStock;
  });

  const totalValue = summary?.inventory_value ?? 0;
  const lowStockCount = summary?.low_stock_count ?? 0;
  const totalReserved = summary?.reserved_total ?? 0;

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
            Prehľad skladu
          </h1>
          <p
            className="text-sm mt-1"
            style={{ color: 'var(--color-text-tertiary)' }}
          >
            Aktuálne stavy a dostupnosť produktov
          </p>
        </div>
        <div className="flex gap-2">
          <Button variant="secondary" disabled title="Pripravujeme — export reálneho skladu">
            <Download size={16} />
            Export CSV
          </Button>
          <Button variant="primary" onClick={() => setUpgatesShop('biketrek')}>
            <RefreshCw size={16} />
            Stiahnuť z Upgates (BikeTrek)
          </Button>
          <Button
            variant="secondary"
            onClick={() => setUpgatesShop('xtrek')}
            title="xTrek zatiaľ nie je na Upgates — funkčné po migrácii"
          >
            <RefreshCw size={16} />
            Stiahnuť z Upgates (xTrek)
          </Button>
        </div>
      </div>

      {/* Stats */}
      <div className="grid grid-cols-4 gap-4">
        <StatsCard
          icon="💰"
          value={`€${totalValue.toLocaleString('sk-SK', { minimumFractionDigits: 0, maximumFractionDigits: 0 })}`}
          label="Hodnota skladu"
        />
        <StatsCard
          icon="📦"
          value={(summary?.products_total ?? 0).toString()}
          label="Produktov"
        />
        <StatsCard
          icon="⚠️"
          value={lowStockCount.toString()}
          label="Nízky stav"
          variant="warning"
          onClick={() => {
            setLowStockOnly(!lowStockOnly);
            setSearchParams(lowStockOnly ? {} : { filter: 'low' });
          }}
        />
        <StatsCard
          icon="🛒"
          value={totalReserved.toString()}
          label="Rezervované"
        />
      </div>

      {/* Filters */}
      <div className="flex items-center gap-4 flex-wrap">
        <div className="relative flex-1 max-w-xs">
          <Search
            size={16}
            className="absolute left-3 top-1/2 -translate-y-1/2"
            style={{ color: 'var(--color-text-tertiary)' }}
          />
          <input
            type="text"
            placeholder="Hľadať produkty..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="w-full pl-9"
          />
        </div>
        <select className="w-40">
          <option value="">Všetky kategórie</option>
        </select>
        <select className="w-40" value={brandFilter} onChange={(e) => setBrandFilter(e.target.value)}>
          <option value="">Všetky značky</option>
          {brands.map((b) => (
            <option key={b} value={b}>{b}</option>
          ))}
        </select>
        <label
          className="flex items-center gap-2 text-sm cursor-pointer"
          style={{ color: 'var(--color-text-secondary)' }}
        >
          <input
            type="checkbox"
            checked={lowStockOnly}
            onChange={(e) => {
              setLowStockOnly(e.target.checked);
              setSearchParams(e.target.checked ? { filter: 'low' } : {});
            }}
          />
          Len nízky stav
        </label>
      </div>

      {/* Table */}
      <div
        className="rounded-xl border overflow-hidden"
        style={{
          backgroundColor: 'var(--color-bg-secondary)',
          borderColor: 'var(--color-border-subtle)',
        }}
      >
        <table className="w-full">
          <thead>
            <tr
              className="border-b"
              style={{
                backgroundColor: 'var(--color-bg-primary)',
                borderColor: 'var(--color-border-subtle)',
              }}
            >
              <th
                className="text-left px-4 py-3 text-xs font-medium uppercase tracking-wider"
                style={{ color: 'var(--color-text-tertiary)' }}
              >
                SKU
              </th>
              <th
                className="text-left px-4 py-3 text-xs font-medium uppercase tracking-wider"
                style={{ color: 'var(--color-text-tertiary)' }}
              >
                Názov
              </th>
              <th
                className="text-left px-4 py-3 text-xs font-medium uppercase tracking-wider"
                style={{ color: 'var(--color-text-tertiary)' }}
              >
                Značka
              </th>
              <th
                className="text-right px-4 py-3 text-xs font-medium uppercase tracking-wider"
                style={{ color: 'var(--color-text-tertiary)' }}
              >
                Na sklade
              </th>
              <th
                className="text-right px-4 py-3 text-xs font-medium uppercase tracking-wider"
                style={{ color: 'var(--color-text-tertiary)' }}
              >
                Rezerv.
              </th>
              <th
                className="text-right px-4 py-3 text-xs font-medium uppercase tracking-wider"
                style={{ color: 'var(--color-text-tertiary)' }}
              >
                Dostupné
              </th>
              <th
                className="text-right px-4 py-3 text-xs font-medium uppercase tracking-wider"
                style={{ color: 'var(--color-text-tertiary)' }}
              >
                Priem. cena
              </th>
            </tr>
          </thead>
          <tbody
            className="divide-y"
            style={{ borderColor: 'var(--color-border-subtle)' }}
          >
            {filteredItems.map((item) => (
              <tr
                key={item.sku}
                className="cursor-pointer transition-colors"
                style={{ backgroundColor: 'transparent' }}
                onMouseEnter={(e) => {
                  e.currentTarget.style.backgroundColor = 'var(--color-bg-tertiary)';
                }}
                onMouseLeave={(e) => {
                  e.currentTarget.style.backgroundColor = 'transparent';
                }}
                onClick={() => navigate(`/products/${item.sku}`)}
              >
                <td className="px-4 py-3">
                  <span
                    className="text-sm"
                    style={{
                      fontFamily: 'var(--font-mono)',
                      color: 'var(--color-accent)',
                    }}
                  >
                    {item.sku}
                  </span>
                </td>
                <td
                  className="px-4 py-3 text-sm"
                  style={{ color: 'var(--color-text-primary)' }}
                >
                  {item.name}
                </td>
                <td
                  className="px-4 py-3 text-sm"
                  style={{ color: 'var(--color-text-secondary)' }}
                >
                  {item.brand}
                </td>
                <td className="px-4 py-3 text-right">
                  <span
                    className="text-sm flex items-center justify-end gap-1"
                    style={{
                      color: item.lowStock
                        ? 'var(--color-warning)'
                        : 'var(--color-text-primary)',
                    }}
                  >
                    {item.onHand}
                    {item.lowStock && (
                      <AlertTriangle
                        size={14}
                        style={{ color: 'var(--color-warning)' }}
                      />
                    )}
                  </span>
                </td>
                <td
                  className="px-4 py-3 text-right text-sm"
                  style={{ color: 'var(--color-text-secondary)' }}
                >
                  {item.reserved}
                </td>
                <td className="px-4 py-3 text-right">
                  <span
                    className="text-sm font-medium"
                    style={{
                      color:
                        item.available === 0
                          ? 'var(--color-error)'
                          : item.lowStock
                          ? 'var(--color-warning)'
                          : 'var(--color-success)',
                    }}
                  >
                    {item.available}
                  </span>
                </td>
                <td
                  className="px-4 py-3 text-right text-sm"
                  style={{
                    fontFamily: 'var(--font-mono)',
                    color: 'var(--color-text-secondary)',
                  }}
                >
                  €{item.avgCost.toFixed(2)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>

        {loading && (
          <div className="p-8 text-center" style={{ color: 'var(--color-text-tertiary)' }}>
            Načítavam sklad…
          </div>
        )}
        {!loading && error && (
          <div className="p-8 text-center" style={{ color: 'var(--color-error)' }}>
            {error}
          </div>
        )}
        {!loading && !error && items.length === 0 && (
          <div className="p-8 text-center" style={{ color: 'var(--color-text-tertiary)' }}>
            Sklad je zatiaľ prázdny. Skladové zásoby sa naplnia po zapnutí zápisu
            skladových pohybov pri príjme faktúr a synchronizácii s Upgates.
          </div>
        )}
        {!loading && !error && items.length > 0 && filteredItems.length === 0 && (
          <div
            className="p-8 text-center"
            style={{ color: 'var(--color-text-tertiary)' }}
          >
            Žiadne produkty nezodpovedajú filtru
          </div>
        )}
      </div>

      {/* Pagination placeholder */}
      <div
        className="flex items-center justify-between text-sm"
        style={{ color: 'var(--color-text-tertiary)' }}
      >
        <div>
          Zobrazených {filteredItems.length} z {items.length} produktov
        </div>
        <div className="flex gap-2">
          <Button variant="secondary" size="sm" disabled>
            ← Predchádzajúca
          </Button>
          <Button variant="secondary" size="sm" disabled>
            Ďalšia →
          </Button>
        </div>
      </div>
      {upgatesShop && (
        <UpgatesImportModal
          shop={upgatesShop}
          onClose={() => setUpgatesShop(null)}
          onImported={loadData}
        />
      )}
    </div>
  );
}

export default StockPage;
