import React, { useEffect, useState, useMemo } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import {
  FileText,
  Search,
  Filter,
  Download,
  ChevronLeft,
  ChevronRight,
  AlertCircle,
  CheckCircle,
  Clock,
  ArrowUpDown,
  Calendar,
  Building2,
  RefreshCw,
} from 'lucide-react';

const API_BASE = (import.meta.env.VITE_API_BASE || '/api').replace(/\/$/, '');

// Types
interface Supplier {
  id: number;
  code: string;
  name: string;
}

interface Invoice {
  id: number;
  invoice_number: string;
  invoice_date: string | null;
  due_date: string | null;
  currency: string;
  total_amount: number | null;
  total_with_vat: number | null;
  vat_amount: number | null;
  vat_rate: number;
  vat_included: boolean;
  payment_status: 'unpaid' | 'partial' | 'paid';
  receiving_status: string;
  items_count: number;
  supplier: Supplier;
  warehouse: { id: number; code: string; name: string };
  is_overdue: boolean;
  days_until_due: number | null;
  created_at: string;
}

interface InvoiceListResponse {
  items: Invoice[];
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
  summary: {
    filtered_count: number;
    filtered_total: number;
    unpaid_count: number;
    unpaid_total: number;
  };
}

interface InvoiceStats {
  total_invoices: number;
  total_amount: number;
  unpaid_count: number;
  unpaid_amount: number;
  overdue_count: number;
  overdue_amount: number;
  by_supplier: { code: string; name: string; count: number; total: number }[];
}

type SortField = 'invoice_date' | 'due_date' | 'total' | 'supplier' | 'invoice_number';
type SortOrder = 'asc' | 'desc';

// Helpers
const formatCurrency = (amount: number | null, currency = 'EUR'): string => {
  if (amount === null || amount === undefined) return '—';
  return new Intl.NumberFormat('sk-SK', {
    style: 'currency',
    currency,
    minimumFractionDigits: 2,
  }).format(amount);
};

const formatDate = (dateStr: string | null): string => {
  if (!dateStr) return '—';
  try {
    return new Date(dateStr).toLocaleDateString('sk-SK');
  } catch {
    return dateStr;
  }
};

// Components
const PaymentStatusBadge: React.FC<{ status: string; isOverdue: boolean }> = ({
  status,
  isOverdue,
}) => {
  if (isOverdue) {
    return (
      <span
        className="inline-flex items-center gap-1 px-2 py-1 rounded-full text-xs font-medium"
        style={{
          backgroundColor: 'var(--color-error-subtle)',
          color: 'var(--color-error)',
        }}
      >
        <AlertCircle size={12} />
        Po splatnosti
      </span>
    );
  }

  const styles: Record<string, { bg: string; color: string; icon: React.ReactNode }> = {
    paid: {
      bg: 'var(--color-success-subtle)',
      color: 'var(--color-success)',
      icon: <CheckCircle size={12} />,
    },
    partial: {
      bg: 'var(--color-warning-subtle)',
      color: 'var(--color-warning)',
      icon: <Clock size={12} />,
    },
    unpaid: {
      bg: 'var(--color-bg-tertiary)',
      color: 'var(--color-text-secondary)',
      icon: <Clock size={12} />,
    },
  };

  const style = styles[status] || styles.unpaid;
  const labels: Record<string, string> = {
    paid: 'Uhradené',
    partial: 'Čiastočne',
    unpaid: 'Neuhradené',
  };

  return (
    <span
      className="inline-flex items-center gap-1 px-2 py-1 rounded-full text-xs font-medium"
      style={{ backgroundColor: style.bg, color: style.color }}
    >
      {style.icon}
      {labels[status] || status}
    </span>
  );
};

const ReceivingStatusBadge: React.FC<{ status: string }> = ({ status }) => {
  const styles: Record<string, { bg: string; color: string }> = {
    completed: { bg: 'var(--color-success-subtle)', color: 'var(--color-success)' },
    in_progress: { bg: 'var(--color-info-subtle)', color: 'var(--color-info)' },
    new: { bg: 'var(--color-warning-subtle)', color: 'var(--color-warning)' },
    paused: { bg: 'var(--color-bg-tertiary)', color: 'var(--color-text-secondary)' },
    cancelled: { bg: 'var(--color-error-subtle)', color: 'var(--color-error)' },
  };

  const labels: Record<string, string> = {
    completed: 'Prijaté',
    in_progress: 'Prebieha',
    new: 'Nové',
    paused: 'Pozastavené',
    cancelled: 'Zrušené',
  };

  const style = styles[status] || styles.new;

  return (
    <span
      className="px-2 py-1 rounded-full text-xs font-medium"
      style={{ backgroundColor: style.bg, color: style.color }}
    >
      {labels[status] || status}
    </span>
  );
};

// Main Component
export function InvoicesPage() {
  const { t } = useTranslation();
  const [searchParams, setSearchParams] = useSearchParams();

  // State
  const [invoices, setInvoices] = useState<Invoice[]>([]);
  const [stats, setStats] = useState<InvoiceStats | null>(null);
  const [suppliers, setSuppliers] = useState<{ id: number; code: string; name: string }[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [total, setTotal] = useState(0);
  const [totalPages, setTotalPages] = useState(1);
  const [summary, setSummary] = useState<InvoiceListResponse['summary'] | null>(null);

  // Filters from URL
  const page = parseInt(searchParams.get('page') || '1', 10);
  const pageSize = parseInt(searchParams.get('pageSize') || '25', 10);
  const supplierCode = searchParams.get('supplier') || '';
  const paymentStatus = searchParams.get('payment') || '';
  const receivingStatus = searchParams.get('receiving') || '';
  const search = searchParams.get('search') || '';
  const sortBy = (searchParams.get('sortBy') || 'invoice_date') as SortField;
  const sortOrder = (searchParams.get('sortOrder') || 'desc') as SortOrder;
  const isOverdue = searchParams.get('overdue') === 'true' ? true : searchParams.get('overdue') === 'false' ? false : undefined;

  // Fetch suppliers
  useEffect(() => {
    const fetchSuppliers = async () => {
      try {
        const res = await fetch(`${API_BASE}/invoices/suppliers`);
        if (res.ok) {
          const data = await res.json();
          setSuppliers(data.suppliers || []);
        }
      } catch (e) {
        console.error('Failed to fetch suppliers:', e);
      }
    };
    fetchSuppliers();
  }, []);

  // Fetch stats
  useEffect(() => {
    const fetchStats = async () => {
      try {
        const res = await fetch(`${API_BASE}/invoices/stats`);
        if (res.ok) {
          const data = await res.json();
          setStats(data);
        }
      } catch (e) {
        console.error('Failed to fetch stats:', e);
      }
    };
    fetchStats();
  }, []);

  // Fetch invoices
  useEffect(() => {
    const fetchInvoices = async () => {
      setLoading(true);
      setError(null);

      try {
        const params = new URLSearchParams();
        params.set('page', String(page));
        params.set('page_size', String(pageSize));
        params.set('sort_by', sortBy);
        params.set('sort_order', sortOrder);

        if (supplierCode) params.set('supplier_code', supplierCode);
        if (paymentStatus) params.set('payment_status', paymentStatus);
        if (receivingStatus) params.set('receiving_status', receivingStatus);
        if (search) params.set('search', search);
        if (isOverdue !== undefined) params.set('is_overdue', String(isOverdue));

        const res = await fetch(`${API_BASE}/invoices?${params.toString()}`);
        if (!res.ok) {
          throw new Error(`HTTP ${res.status}: ${res.statusText}`);
        }

        const data: InvoiceListResponse = await res.json();
        setInvoices(data.items);
        setTotal(data.total);
        setTotalPages(data.total_pages);
        setSummary(data.summary);
      } catch (e: any) {
        setError(e.message || 'Chyba pri načítaní faktúr');
        setInvoices([]);
      } finally {
        setLoading(false);
      }
    };

    fetchInvoices();
  }, [page, pageSize, supplierCode, paymentStatus, receivingStatus, search, sortBy, sortOrder, isOverdue]);

  // Handlers
  const updateFilter = (key: string, value: string) => {
    const newParams = new URLSearchParams(searchParams);
    if (value) {
      newParams.set(key, value);
    } else {
      newParams.delete(key);
    }
    // Reset to page 1 when filtering
    if (key !== 'page') {
      newParams.set('page', '1');
    }
    setSearchParams(newParams);
  };

  const toggleSort = (field: SortField) => {
    if (sortBy === field) {
      updateFilter('sortOrder', sortOrder === 'asc' ? 'desc' : 'asc');
    } else {
      const newParams = new URLSearchParams(searchParams);
      newParams.set('sortBy', field);
      newParams.set('sortOrder', 'desc');
      newParams.set('page', '1');
      setSearchParams(newParams);
    }
  };

  const SortButton: React.FC<{ field: SortField; children: React.ReactNode }> = ({
    field,
    children,
  }) => (
    <button
      onClick={() => toggleSort(field)}
      className="flex items-center gap-1 hover:opacity-80"
      style={{ color: sortBy === field ? 'var(--color-accent)' : 'inherit' }}
    >
      {children}
      <ArrowUpDown size={14} />
    </button>
  );

  return (
    <div className="p-6 space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1
            className="text-2xl font-semibold"
            style={{ fontFamily: 'var(--font-display)', color: 'var(--color-text-primary)' }}
          >
            <FileText className="inline-block mr-2 mb-1" size={28} />
            Faktúry
          </h1>
          <p style={{ color: 'var(--color-text-secondary)' }}>
            Správa dodávateľských faktúr a platobného stavu
          </p>
        </div>

        <button
          onClick={() => window.location.reload()}
          className="flex items-center gap-2 px-4 py-2 rounded-lg transition-colors"
          style={{
            backgroundColor: 'var(--color-accent)',
            color: 'var(--color-text-inverse)',
          }}
        >
          <RefreshCw size={16} />
          Obnoviť
        </button>
      </div>

      {/* Stats Cards */}
      {stats && (
        <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
          <div
            className="p-4 rounded-xl"
            style={{ backgroundColor: 'var(--color-bg-secondary)', border: '1px solid var(--color-border-subtle)' }}
          >
            <div className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>
              Celkom faktúr
            </div>
            <div className="text-2xl font-semibold mt-1" style={{ fontFamily: 'var(--font-display)' }}>
              {stats.total_invoices}
            </div>
            <div className="text-sm mt-1" style={{ color: 'var(--color-text-tertiary)' }}>
              {formatCurrency(stats.total_amount)}
            </div>
          </div>

          <div
            className="p-4 rounded-xl"
            style={{ backgroundColor: 'var(--color-bg-secondary)', border: '1px solid var(--color-border-subtle)' }}
          >
            <div className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>
              Neuhradené
            </div>
            <div className="text-2xl font-semibold mt-1" style={{ fontFamily: 'var(--font-display)', color: 'var(--color-warning)' }}>
              {stats.unpaid_count}
            </div>
            <div className="text-sm mt-1" style={{ color: 'var(--color-text-tertiary)' }}>
              {formatCurrency(stats.unpaid_amount)}
            </div>
          </div>

          <div
            className="p-4 rounded-xl"
            style={{ backgroundColor: 'var(--color-bg-secondary)', border: '1px solid var(--color-border-subtle)' }}
          >
            <div className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>
              Po splatnosti
            </div>
            <div className="text-2xl font-semibold mt-1" style={{ fontFamily: 'var(--font-display)', color: 'var(--color-error)' }}>
              {stats.overdue_count}
            </div>
            <div className="text-sm mt-1" style={{ color: 'var(--color-text-tertiary)' }}>
              {formatCurrency(stats.overdue_amount)}
            </div>
          </div>

          <div
            className="p-4 rounded-xl"
            style={{ backgroundColor: 'var(--color-bg-secondary)', border: '1px solid var(--color-border-subtle)' }}
          >
            <div className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>
              Dodávatelia
            </div>
            <div className="text-2xl font-semibold mt-1" style={{ fontFamily: 'var(--font-display)' }}>
              {stats.by_supplier.length}
            </div>
            <div className="text-sm mt-1" style={{ color: 'var(--color-text-tertiary)' }}>
              s faktúrami
            </div>
          </div>
        </div>
      )}

      {/* Filters */}
      <div
        className="p-4 rounded-xl space-y-4"
        style={{ backgroundColor: 'var(--color-bg-secondary)', border: '1px solid var(--color-border-subtle)' }}
      >
        <div className="flex items-center gap-2 text-sm" style={{ color: 'var(--color-text-secondary)' }}>
          <Filter size={16} />
          Filtre
        </div>

        <div className="grid grid-cols-1 md:grid-cols-5 gap-4">
          {/* Search */}
          <div className="relative">
            <Search
              size={16}
              className="absolute left-3 top-1/2 -translate-y-1/2"
              style={{ color: 'var(--color-text-tertiary)' }}
            />
            <input
              type="text"
              placeholder="Číslo faktúry..."
              value={search}
              onChange={(e) => updateFilter('search', e.target.value)}
              className="w-full pl-9"
            />
          </div>

          {/* Supplier */}
          <select
            value={supplierCode}
            onChange={(e) => updateFilter('supplier', e.target.value)}
          >
            <option value="">Všetci dodávatelia</option>
            {suppliers.map((s) => (
              <option key={s.code} value={s.code}>
                {s.name}
              </option>
            ))}
          </select>

          {/* Payment Status */}
          <select
            value={paymentStatus}
            onChange={(e) => updateFilter('payment', e.target.value)}
          >
            <option value="">Všetky platby</option>
            <option value="unpaid">Neuhradené</option>
            <option value="partial">Čiastočne</option>
            <option value="paid">Uhradené</option>
          </select>

          {/* Receiving Status */}
          <select
            value={receivingStatus}
            onChange={(e) => updateFilter('receiving', e.target.value)}
          >
            <option value="">Všetky príjmy</option>
            <option value="new">Nové</option>
            <option value="in_progress">Prebieha</option>
            <option value="completed">Prijaté</option>
            <option value="paused">Pozastavené</option>
          </select>

          {/* Overdue */}
          <select
            value={isOverdue === undefined ? '' : String(isOverdue)}
            onChange={(e) => updateFilter('overdue', e.target.value)}
          >
            <option value="">Splatnosť</option>
            <option value="true">Po splatnosti</option>
            <option value="false">V termíne</option>
          </select>
        </div>

        {/* Active filters summary */}
        {summary && (
          <div className="flex items-center gap-4 text-sm" style={{ color: 'var(--color-text-secondary)' }}>
            <span>
              Zobrazených: <strong>{summary.filtered_count}</strong> faktúr
            </span>
            <span>
              Suma: <strong>{formatCurrency(summary.filtered_total)}</strong>
            </span>
            {summary.unpaid_count > 0 && (
              <span style={{ color: 'var(--color-warning)' }}>
                Neuhradené: <strong>{formatCurrency(summary.unpaid_total)}</strong>
              </span>
            )}
          </div>
        )}
      </div>

      {/* Error */}
      {error && (
        <div
          className="p-4 rounded-xl flex items-center gap-3"
          style={{ backgroundColor: 'var(--color-error-subtle)', color: 'var(--color-error)' }}
        >
          <AlertCircle size={20} />
          {error}
        </div>
      )}

      {/* Table */}
      <div
        className="rounded-xl overflow-hidden"
        style={{ backgroundColor: 'var(--color-bg-secondary)', border: '1px solid var(--color-border-subtle)' }}
      >
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr style={{ backgroundColor: 'var(--color-bg-tertiary)' }}>
                <th className="px-4 py-3 text-left font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                  <SortButton field="invoice_number">Faktúra</SortButton>
                </th>
                <th className="px-4 py-3 text-left font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                  <SortButton field="supplier">Dodávateľ</SortButton>
                </th>
                <th className="px-4 py-3 text-left font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                  <SortButton field="invoice_date">Dátum</SortButton>
                </th>
                <th className="px-4 py-3 text-left font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                  <SortButton field="due_date">Splatnosť</SortButton>
                </th>
                <th className="px-4 py-3 text-right font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                  <SortButton field="total">Suma</SortButton>
                </th>
                <th className="px-4 py-3 text-center font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                  Položky
                </th>
                <th className="px-4 py-3 text-center font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                  Platba
                </th>
                <th className="px-4 py-3 text-center font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                  Príjem
                </th>
              </tr>
            </thead>
            <tbody>
              {loading ? (
                <tr>
                  <td colSpan={8} className="px-4 py-12 text-center" style={{ color: 'var(--color-text-tertiary)' }}>
                    <RefreshCw className="animate-spin inline-block mr-2" size={20} />
                    Načítavam...
                  </td>
                </tr>
              ) : invoices.length === 0 ? (
                <tr>
                  <td colSpan={8} className="px-4 py-12 text-center" style={{ color: 'var(--color-text-tertiary)' }}>
                    <FileText className="inline-block mr-2 mb-1" size={24} />
                    Žiadne faktúry
                  </td>
                </tr>
              ) : (
                invoices.map((invoice) => (
                  <tr
                    key={invoice.id}
                    className="border-t transition-colors hover:bg-opacity-50"
                    style={{ borderColor: 'var(--color-border-subtle)' }}
                    onMouseEnter={(e) => (e.currentTarget.style.backgroundColor = 'var(--color-bg-hover)')}
                    onMouseLeave={(e) => (e.currentTarget.style.backgroundColor = 'transparent')}
                  >
                    <td className="px-4 py-3">
                      <Link
                        to={`/invoices/${invoice.id}`}
                        className="font-medium hover:underline"
                        style={{ color: 'var(--color-accent)' }}
                      >
                        {invoice.invoice_number}
                      </Link>
                    </td>
                    <td className="px-4 py-3">
                      <div className="flex items-center gap-2">
                        <Building2 size={14} style={{ color: 'var(--color-text-tertiary)' }} />
                        <span>{invoice.supplier.name}</span>
                      </div>
                    </td>
                    <td className="px-4 py-3" style={{ color: 'var(--color-text-secondary)' }}>
                      {formatDate(invoice.invoice_date)}
                    </td>
                    <td className="px-4 py-3">
                      <div className="flex items-center gap-2">
                        <Calendar size={14} style={{ color: 'var(--color-text-tertiary)' }} />
                        <span style={{ color: invoice.is_overdue ? 'var(--color-error)' : 'var(--color-text-secondary)' }}>
                          {formatDate(invoice.due_date)}
                        </span>
                        {invoice.days_until_due !== null && !invoice.is_overdue && invoice.days_until_due <= 7 && (
                          <span className="text-xs" style={{ color: 'var(--color-warning)' }}>
                            ({invoice.days_until_due}d)
                          </span>
                        )}
                      </div>
                    </td>
                    <td className="px-4 py-3 text-right font-mono">
                      <div>
                        {formatCurrency(invoice.total_with_vat || invoice.total_amount, invoice.currency)}
                      </div>
                      {!invoice.vat_included && (
                        <div className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                          +{invoice.vat_rate}% DPH
                        </div>
                      )}
                    </td>
                    <td className="px-4 py-3 text-center" style={{ color: 'var(--color-text-secondary)' }}>
                      {invoice.items_count}
                    </td>
                    <td className="px-4 py-3 text-center">
                      <PaymentStatusBadge status={invoice.payment_status} isOverdue={invoice.is_overdue} />
                    </td>
                    <td className="px-4 py-3 text-center">
                      <ReceivingStatusBadge status={invoice.receiving_status} />
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>

        {/* Pagination */}
        {totalPages > 1 && (
          <div
            className="flex items-center justify-between px-4 py-3 border-t"
            style={{ borderColor: 'var(--color-border-subtle)' }}
          >
            <div className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>
              Strana {page} z {totalPages} ({total} záznamov)
            </div>
            <div className="flex items-center gap-2">
              <button
                onClick={() => updateFilter('page', String(page - 1))}
                disabled={page <= 1}
                className="p-2 rounded-lg disabled:opacity-50"
                style={{ backgroundColor: 'var(--color-bg-tertiary)' }}
              >
                <ChevronLeft size={16} />
              </button>
              <button
                onClick={() => updateFilter('page', String(page + 1))}
                disabled={page >= totalPages}
                className="p-2 rounded-lg disabled:opacity-50"
                style={{ backgroundColor: 'var(--color-bg-tertiary)' }}
              >
                <ChevronRight size={16} />
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

export default InvoicesPage;
