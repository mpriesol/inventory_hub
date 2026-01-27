import React, { useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { Search, Download, RefreshCw, AlertTriangle } from 'lucide-react';
import { StatsCard } from '../components/ui/StatsCard';
import { Button } from '../components/ui/Button.new';

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

// Mock data
const mockStockItems: StockItem[] = [
  { sku: 'PL-ESHRM500', name: 'Shimano Deore XT RD-M8100', brand: 'Shimano', onHand: 45, reserved: 3, available: 42, avgCost: 89.50, lowStock: false },
  { sku: 'PL-ESHRM510', name: 'Shimano SLX RD-M7100', brand: 'Shimano', onHand: 12, reserved: 0, available: 12, avgCost: 65.00, lowStock: false },
  { sku: 'PL-ESLZ001', name: 'Lazer Genesis MIPS Helmet', brand: 'Lazer', onHand: 3, reserved: 2, available: 1, avgCost: 142.00, lowStock: true },
  { sku: 'PL-ORTAN01', name: 'Ortlieb Seat-Pack 16.5L', brand: 'Ortlieb', onHand: 28, reserved: 5, available: 23, avgCost: 156.00, lowStock: false },
  { sku: 'PL-ORTAN02', name: 'Ortlieb Frame-Pack RC', brand: 'Ortlieb', onHand: 0, reserved: 0, available: 0, avgCost: 98.00, lowStock: true },
  { sku: 'PL-PROHB01', name: 'PRO Discover Handlebar Bag', brand: 'PRO', onHand: 15, reserved: 1, available: 14, avgCost: 78.00, lowStock: false },
  { sku: 'PL-SRMOT01', name: 'Motorex Chain Lube Dry', brand: 'Motorex', onHand: 67, reserved: 0, available: 67, avgCost: 12.50, lowStock: false },
  { sku: 'PL-ESLZ002', name: 'Lazer Strada KinetiCore', brand: 'Lazer', onHand: 5, reserved: 3, available: 2, avgCost: 89.00, lowStock: true },
];

export function StockPage() {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  
  const [search, setSearch] = useState('');
  const [lowStockOnly, setLowStockOnly] = useState(searchParams.get('filter') === 'low');
  const [items] = useState<StockItem[]>(mockStockItems);

  const filteredItems = items.filter((item) => {
    const matchesSearch = !search || 
      item.sku.toLowerCase().includes(search.toLowerCase()) ||
      item.name.toLowerCase().includes(search.toLowerCase()) ||
      item.brand.toLowerCase().includes(search.toLowerCase());
    
    const matchesLowStock = !lowStockOnly || item.lowStock;
    
    return matchesSearch && matchesLowStock;
  });

  const totalValue = items.reduce((sum, item) => sum + item.onHand * item.avgCost, 0);
  const lowStockCount = items.filter(item => item.lowStock).length;
  const totalReserved = items.reduce((sum, item) => sum + item.reserved, 0);

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
          <Button variant="secondary">
            <Download size={16} />
            Export CSV
          </Button>
          <Button variant="primary">
            <RefreshCw size={16} />
            Synchronizovať
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
          value={items.length.toString()}
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
        <select className="w-40">
          <option value="">Všetky značky</option>
          <option value="shimano">Shimano</option>
          <option value="lazer">Lazer</option>
          <option value="ortlieb">Ortlieb</option>
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

        {filteredItems.length === 0 && (
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
    </div>
  );
}

export default StockPage;
