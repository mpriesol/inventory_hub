import React, { useEffect, useState, useCallback, useMemo } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import {
  FileText,
  Download,
  ChevronLeft,
  ChevronRight,
  AlertCircle,
  CheckCircle,
  Clock,
  RefreshCw,
  Upload,
  X,
  Building2,
  Calendar,
  Filter,
} from 'lucide-react';

const API_BASE = (import.meta.env.VITE_API_BASE || '/api').replace(/\/$/, '');

// ============================================================================
// Types
// ============================================================================

interface Supplier {
  id: number;
  code: string;
  name: string;
  invoice_count: number;
}

interface Invoice {
  id: number;
  supplier_id: number;
  supplier_code: string;
  supplier_name: string;
  original_filename: string;
  file_type: string;
  invoice_number: string | null;
  invoice_date: string | null;
  due_date: string | null;
  currency: string;
  total_amount: number | null;
  total_with_vat: number | null;
  vat_amount: number | null;
  vat_rate: number;
  vat_included: boolean;
  items_count: number;
  payment_status: 'unpaid' | 'partial' | 'paid';
  receiving_status: string;
  is_overdue: boolean;
  days_until_due: number | null;
  is_parsed: boolean;
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
}

// ============================================================================
// Helpers
// ============================================================================

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

// Debounce hook
function useDebounce<T>(value: T, delay: number): T {
  const [debouncedValue, setDebouncedValue] = useState<T>(value);
  useEffect(() => {
    const handler = setTimeout(() => setDebouncedValue(value), delay);
    return () => clearTimeout(handler);
  }, [value, delay]);
  return debouncedValue;
}

// ============================================================================
// Components
// ============================================================================

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
    not_started: { bg: 'var(--color-bg-tertiary)', color: 'var(--color-text-tertiary)' },
    paused: { bg: 'var(--color-bg-tertiary)', color: 'var(--color-text-secondary)' },
    cancelled: { bg: 'var(--color-error-subtle)', color: 'var(--color-error)' },
  };

  const labels: Record<string, string> = {
    completed: 'Prijaté',
    in_progress: 'Prebieha',
    new: 'Nové',
    not_started: 'Nezačaté',
    paused: 'Pozastavené',
    cancelled: 'Zrušené',
  };

  const style = styles[status] || styles.not_started;

  return (
    <span
      className="px-2 py-1 rounded-full text-xs font-medium"
      style={{ backgroundColor: style.bg, color: style.color }}
    >
      {labels[status] || status}
    </span>
  );
};

// ============================================================================
// Filter Row State
// ============================================================================

interface FilterState {
  f_invoice_number: string;
  f_supplier: string;
  date_from: string;
  date_to: string;
  due_from: string;
  due_to: string;
  f_amount_min: string;
  f_amount_max: string;
  payment_status: string;
  receiving_status: string;
}

const initialFilters: FilterState = {
  f_invoice_number: '',
  f_supplier: '',
  date_from: '',
  date_to: '',
  due_from: '',
  due_to: '',
  f_amount_min: '',
  f_amount_max: '',
  payment_status: '',
  receiving_status: '',
};

// ============================================================================
// Main Component
// ============================================================================

export function InvoicesPage() {
  const { t } = useTranslation();
  const [searchParams, setSearchParams] = useSearchParams();

  // State
  const [invoices, setInvoices] = useState<Invoice[]>([]);
  const [stats, setStats] = useState<InvoiceStats | null>(null);
  const [suppliers, setSuppliers] = useState<Supplier[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [total, setTotal] = useState(0);
  const [totalPages, setTotalPages] = useState(1);
  const [summary, setSummary] = useState<InvoiceListResponse['summary'] | null>(null);

  // Upload modal
  const [uploadModalOpen, setUploadModalOpen] = useState(false);
  const [uploadSupplier, setUploadSupplier] = useState('');
  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [uploadInvoiceNumber, setUploadInvoiceNumber] = useState('');
  const [uploadInvoiceDate, setUploadInvoiceDate] = useState('');
  const [uploadDueDate, setUploadDueDate] = useState('');
  const [uploadVatIncluded, setUploadVatIncluded] = useState(true);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [uploadSuccess, setUploadSuccess] = useState<string | null>(null);

  // Pagination
  const page = parseInt(searchParams.get('page') || '1', 10);
  const pageSize = 25;

  // Filter row state (local, debounced)
  const [filters, setFilters] = useState<FilterState>(() => ({
    f_invoice_number: searchParams.get('f_invoice_number') || '',
    f_supplier: searchParams.get('f_supplier') || '',
    date_from: searchParams.get('date_from') || '',
    date_to: searchParams.get('date_to') || '',
    due_from: searchParams.get('due_from') || '',
    due_to: searchParams.get('due_to') || '',
    f_amount_min: searchParams.get('f_amount_min') || '',
    f_amount_max: searchParams.get('f_amount_max') || '',
    payment_status: searchParams.get('payment_status') || '',
    receiving_status: searchParams.get('receiving_status') || '',
  }));

  const debouncedFilters = useDebounce(filters, 400);

  // Check if any filter is active
  const hasActiveFilters = useMemo(() => {
    return Object.values(filters).some((v) => v !== '');
  }, [filters]);

  // Update filter
  const updateFilter = useCallback((key: keyof FilterState, value: string) => {
    setFilters((prev) => ({ ...prev, [key]: value }));
  }, []);

  // Clear all filters
  const clearFilters = useCallback(() => {
    setFilters(initialFilters);
    setSearchParams({});
  }, [setSearchParams]);

  // Sync debounced filters to URL
  useEffect(() => {
    const params: Record<string, string> = {};
    Object.entries(debouncedFilters).forEach(([k, v]) => {
      if (v) params[k] = v;
    });
    // Keep page if it exists
    const currentPage = searchParams.get('page');
    if (currentPage && currentPage !== '1') {
      // Reset to page 1 when filters change
    }
    setSearchParams(params);
  }, [debouncedFilters, setSearchParams]);

  // Fetch suppliers from main suppliers endpoint (not invoices/suppliers)
  // This returns ALL configured suppliers from filesystem configs
  useEffect(() => {
    const fetchSuppliers = async () => {
      try {
        // Use /api/suppliers - reads from filesystem supplier configs
        const res = await fetch(`${API_BASE}/suppliers`);
        console.log('[InvoicesPage] Suppliers response status:', res.status);
        
        if (res.ok) {
          const data = await res.json();
          console.log('[InvoicesPage] Suppliers data:', data);
          
          // Handle both array and object response formats
          const supplierList = Array.isArray(data) ? data : (data.suppliers || data.items || []);
          
          if (supplierList.length === 0) {
            console.warn('[InvoicesPage] No suppliers found! Check if supplier configs exist in inventory-data/suppliers/*/config.json');
          }
          
          setSuppliers(supplierList);
        } else {
          console.error('[InvoicesPage] Failed to fetch suppliers:', res.status, await res.text());
        }
      } catch (e) {
        console.error('[InvoicesPage] Error fetching suppliers:', e);
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
          setStats(await res.json());
        }
      } catch (e) {
        console.error('Failed to fetch stats:', e);
      }
    };
    fetchStats();
  }, []);

  // Fetch invoices
  const fetchInvoices = useCallback(async () => {
    setLoading(true);
    setError(null);

    try {
      const params = new URLSearchParams();
      params.set('page', String(page));
      params.set('page_size', String(pageSize));

      // Add filters
      Object.entries(debouncedFilters).forEach(([k, v]) => {
        if (v) params.set(k, v);
      });

      const res = await fetch(`${API_BASE}/invoices?${params}`);
      if (!res.ok) {
        throw new Error(`HTTP ${res.status}`);
      }

      const data: InvoiceListResponse = await res.json();
      setInvoices(data.items);
      setTotal(data.total);
      setTotalPages(data.total_pages);
      setSummary(data.summary);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to fetch invoices');
    } finally {
      setLoading(false);
    }
  }, [page, pageSize, debouncedFilters]);

  useEffect(() => {
    fetchInvoices();
  }, [fetchInvoices]);

  // Handle upload
  const handleUpload = async () => {
    if (!uploadSupplier || !uploadFile) {
      setUploadError('Vyberte dodávateľa a súbor');
      return;
    }

    setUploading(true);
    setUploadError(null);
    setUploadSuccess(null);

    try {
      const formData = new FormData();
      formData.append('supplier_code', uploadSupplier);
      formData.append('file', uploadFile);
      if (uploadInvoiceNumber) formData.append('invoice_number', uploadInvoiceNumber);
      if (uploadInvoiceDate) formData.append('invoice_date', uploadInvoiceDate);
      if (uploadDueDate) formData.append('due_date', uploadDueDate);
      formData.append('vat_included', String(uploadVatIncluded));

      const res = await fetch(`${API_BASE}/invoices/upload`, {
        method: 'POST',
        body: formData,
      });

      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `Upload failed: ${res.status}`);
      }

      const result = await res.json();
      setUploadSuccess(`Faktúra ${result.invoice_number || uploadFile.name} bola nahratá`);

      // Reset form
      setUploadFile(null);
      setUploadInvoiceNumber('');
      setUploadInvoiceDate('');
      setUploadDueDate('');
      setUploadVatIncluded(true);

      // Refresh list
      fetchInvoices();

      // Close modal after short delay
      setTimeout(() => {
        setUploadModalOpen(false);
        setUploadSuccess(null);
      }, 1500);
    } catch (e) {
      setUploadError(e instanceof Error ? e.message : 'Upload failed');
    } finally {
      setUploading(false);
    }
  };

  // Pagination handlers
  const goToPage = (newPage: number) => {
    const params = new URLSearchParams(searchParams);
    params.set('page', String(newPage));
    setSearchParams(params);
  };

  // Filter input style
  const filterInputStyle: React.CSSProperties = {
    backgroundColor: 'var(--color-bg-primary)',
    border: '1px solid var(--color-border-subtle)',
    borderRadius: '4px',
    padding: '4px 8px',
    fontSize: '12px',
    color: 'var(--color-text-primary)',
    width: '100%',
    minWidth: '60px',
  };

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold" style={{ color: 'var(--color-text-primary)' }}>
            Faktúry
          </h1>
          <p className="text-sm mt-1" style={{ color: 'var(--color-text-secondary)' }}>
            Zoznam všetkých nahratých faktúr od dodávateľov
          </p>
        </div>
        <div className="flex items-center gap-3">
          <button
            onClick={fetchInvoices}
            className="flex items-center gap-2 px-4 py-2 rounded-lg transition-colors"
            style={{
              backgroundColor: 'var(--color-bg-secondary)',
              color: 'var(--color-text-primary)',
              border: '1px solid var(--color-border-subtle)',
            }}
          >
            <RefreshCw size={16} />
            Obnoviť
          </button>
          <button
            onClick={() => setUploadModalOpen(true)}
            className="flex items-center gap-2 px-4 py-2 rounded-lg transition-colors"
            style={{
              backgroundColor: 'var(--color-accent)',
              color: 'var(--color-bg-primary)',
            }}
          >
            <Upload size={16} />
            Nahrať faktúru
          </button>
        </div>
      </div>

      {/* Stats */}
      {stats && (
        <div className="grid grid-cols-3 gap-4">
          <div
            className="p-4 rounded-xl"
            style={{ backgroundColor: 'var(--color-bg-secondary)', border: '1px solid var(--color-border-subtle)' }}
          >
            <div className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>
              Celkom faktúr
            </div>
            <div className="text-2xl font-bold mt-1" style={{ color: 'var(--color-text-primary)' }}>
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
            <div className="text-2xl font-bold mt-1" style={{ color: 'var(--color-warning)' }}>
              {stats.unpaid_count}
            </div>
            <div className="text-sm mt-1" style={{ color: 'var(--color-warning)' }}>
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
            <div className="text-2xl font-bold mt-1" style={{ color: 'var(--color-error)' }}>
              {stats.overdue_count}
            </div>
            <div className="text-sm mt-1" style={{ color: 'var(--color-error)' }}>
              {formatCurrency(stats.overdue_amount)}
            </div>
          </div>
        </div>
      )}

      {/* Clear filters button */}
      {hasActiveFilters && (
        <div className="flex items-center gap-2">
          <Filter size={14} style={{ color: 'var(--color-text-tertiary)' }} />
          <span className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>
            Aktívne filtre
          </span>
          <button
            onClick={clearFilters}
            className="text-sm px-2 py-1 rounded"
            style={{
              backgroundColor: 'var(--color-bg-tertiary)',
              color: 'var(--color-accent)',
            }}
          >
            Vymazať filtre
          </button>
        </div>
      )}

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

      {/* Summary */}
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

      {/* Table */}
      <div
        className="rounded-xl overflow-hidden"
        style={{ backgroundColor: 'var(--color-bg-secondary)', border: '1px solid var(--color-border-subtle)' }}
      >
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              {/* Header row */}
              <tr style={{ backgroundColor: 'var(--color-bg-tertiary)' }}>
                <th className="px-3 py-3 text-left font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                  Faktúra
                </th>
                <th className="px-3 py-3 text-left font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                  Dodávateľ
                </th>
                <th className="px-3 py-3 text-left font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                  Dátum
                </th>
                <th className="px-3 py-3 text-left font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                  Splatnosť
                </th>
                <th className="px-3 py-3 text-right font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                  Suma
                </th>
                <th className="px-3 py-3 text-center font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                  Položky
                </th>
                <th className="px-3 py-3 text-center font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                  Platba
                </th>
                <th className="px-3 py-3 text-center font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                  Príjem
                </th>
                <th className="px-3 py-3 text-center font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                  Súbor
                </th>
              </tr>

              {/* Filter row */}
              <tr style={{ backgroundColor: 'var(--color-bg-secondary)' }}>
                <td className="px-2 py-2">
                  <input
                    type="text"
                    placeholder="Číslo / Názov..."
                    value={filters.f_invoice_number}
                    onChange={(e) => updateFilter('f_invoice_number', e.target.value)}
                    style={filterInputStyle}
                  />
                </td>
                <td className="px-2 py-2">
                  <input
                    type="text"
                    placeholder="Dodávateľ..."
                    value={filters.f_supplier}
                    onChange={(e) => updateFilter('f_supplier', e.target.value)}
                    style={filterInputStyle}
                  />
                </td>
                <td className="px-2 py-2">
                  <div className="flex gap-1">
                    <input
                      type="date"
                      value={filters.date_from}
                      onChange={(e) => updateFilter('date_from', e.target.value)}
                      style={{ ...filterInputStyle, width: '50%' }}
                      title="Od"
                    />
                    <input
                      type="date"
                      value={filters.date_to}
                      onChange={(e) => updateFilter('date_to', e.target.value)}
                      style={{ ...filterInputStyle, width: '50%' }}
                      title="Do"
                    />
                  </div>
                </td>
                <td className="px-2 py-2">
                  <div className="flex gap-1">
                    <input
                      type="date"
                      value={filters.due_from}
                      onChange={(e) => updateFilter('due_from', e.target.value)}
                      style={{ ...filterInputStyle, width: '50%' }}
                      title="Od"
                    />
                    <input
                      type="date"
                      value={filters.due_to}
                      onChange={(e) => updateFilter('due_to', e.target.value)}
                      style={{ ...filterInputStyle, width: '50%' }}
                      title="Do"
                    />
                  </div>
                </td>
                <td className="px-2 py-2">
                  <div className="flex gap-1">
                    <input
                      type="number"
                      placeholder="Min"
                      value={filters.f_amount_min}
                      onChange={(e) => updateFilter('f_amount_min', e.target.value)}
                      style={{ ...filterInputStyle, width: '50%' }}
                    />
                    <input
                      type="number"
                      placeholder="Max"
                      value={filters.f_amount_max}
                      onChange={(e) => updateFilter('f_amount_max', e.target.value)}
                      style={{ ...filterInputStyle, width: '50%' }}
                    />
                  </div>
                </td>
                <td className="px-2 py-2">
                  {/* Items filter - not commonly needed */}
                </td>
                <td className="px-2 py-2">
                  <select
                    value={filters.payment_status}
                    onChange={(e) => updateFilter('payment_status', e.target.value)}
                    style={filterInputStyle}
                  >
                    <option value="">Všetky</option>
                    <option value="unpaid">Neuhradené</option>
                    <option value="partial">Čiastočne</option>
                    <option value="paid">Uhradené</option>
                  </select>
                </td>
                <td className="px-2 py-2">
                  <select
                    value={filters.receiving_status}
                    onChange={(e) => updateFilter('receiving_status', e.target.value)}
                    style={filterInputStyle}
                  >
                    <option value="">Všetky</option>
                    <option value="not_started">Nezačaté</option>
                    <option value="in_progress">Prebieha</option>
                    <option value="completed">Prijaté</option>
                  </select>
                </td>
                <td className="px-2 py-2">
                  {/* Download column - no filter */}
                </td>
              </tr>
            </thead>
            <tbody>
              {loading ? (
                <tr>
                  <td colSpan={9} className="px-4 py-12 text-center" style={{ color: 'var(--color-text-tertiary)' }}>
                    <RefreshCw className="animate-spin inline-block mr-2" size={20} />
                    Načítavam...
                  </td>
                </tr>
              ) : invoices.length === 0 ? (
                <tr>
                  <td colSpan={9} className="px-4 py-12 text-center" style={{ color: 'var(--color-text-tertiary)' }}>
                    <FileText className="inline-block mr-2 mb-1" size={24} />
                    Žiadne faktúry
                  </td>
                </tr>
              ) : (
                invoices.map((invoice) => (
                  <tr
                    key={invoice.id}
                    className="border-t transition-colors"
                    style={{ borderColor: 'var(--color-border-subtle)' }}
                    onMouseEnter={(e) => (e.currentTarget.style.backgroundColor = 'var(--color-bg-hover)')}
                    onMouseLeave={(e) => (e.currentTarget.style.backgroundColor = 'transparent')}
                  >
                    <td className="px-3 py-3">
                      <Link
                        to={`/invoices/${invoice.id}`}
                        className="font-medium hover:underline"
                        style={{ color: 'var(--color-accent)' }}
                      >
                        {invoice.invoice_number || invoice.original_filename}
                      </Link>
                      {invoice.invoice_number && (
                        <div className="text-xs mt-0.5" style={{ color: 'var(--color-text-tertiary)' }}>
                          {invoice.original_filename}
                        </div>
                      )}
                    </td>
                    <td className="px-3 py-3">
                      <div className="flex items-center gap-2">
                        <Building2 size={14} style={{ color: 'var(--color-text-tertiary)' }} />
                        <span>{invoice.supplier_name}</span>
                      </div>
                    </td>
                    <td className="px-3 py-3" style={{ color: 'var(--color-text-secondary)' }}>
                      {formatDate(invoice.invoice_date)}
                    </td>
                    <td className="px-3 py-3">
                      <div className="flex items-center gap-2">
                        <Calendar size={14} style={{ color: 'var(--color-text-tertiary)' }} />
                        <span style={{ color: invoice.is_overdue ? 'var(--color-error)' : 'var(--color-text-secondary)' }}>
                          {formatDate(invoice.due_date)}
                        </span>
                        {invoice.days_until_due !== null && !invoice.is_overdue && invoice.days_until_due <= 7 && invoice.days_until_due >= 0 && (
                          <span className="text-xs" style={{ color: 'var(--color-warning)' }}>
                            ({invoice.days_until_due}d)
                          </span>
                        )}
                      </div>
                    </td>
                    <td className="px-3 py-3 text-right font-mono">
                      <div>
                        {formatCurrency(invoice.total_with_vat || invoice.total_amount, invoice.currency)}
                      </div>
                      {!invoice.vat_included && invoice.total_amount && (
                        <div className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                          +{invoice.vat_rate}% DPH
                        </div>
                      )}
                    </td>
                    <td className="px-3 py-3 text-center" style={{ color: 'var(--color-text-secondary)' }}>
                      {invoice.items_count}
                    </td>
                    <td className="px-3 py-3 text-center">
                      <PaymentStatusBadge status={invoice.payment_status} isOverdue={invoice.is_overdue} />
                    </td>
                    <td className="px-3 py-3 text-center">
                      <ReceivingStatusBadge status={invoice.receiving_status} />
                    </td>
                    <td className="px-3 py-3 text-center">
                      <a
                        href={`${API_BASE}/invoices/${invoice.id}/download`}
                        className="inline-flex items-center justify-center p-1.5 rounded hover:bg-opacity-80"
                        style={{ backgroundColor: 'var(--color-bg-tertiary)' }}
                        title="Stiahnuť"
                      >
                        <Download size={14} style={{ color: 'var(--color-text-secondary)' }} />
                      </a>
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
                onClick={() => goToPage(page - 1)}
                disabled={page <= 1}
                className="p-2 rounded-lg disabled:opacity-50"
                style={{ backgroundColor: 'var(--color-bg-tertiary)' }}
              >
                <ChevronLeft size={16} />
              </button>
              <button
                onClick={() => goToPage(page + 1)}
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

      {/* Upload Modal */}
      {uploadModalOpen && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center"
          style={{ backgroundColor: 'rgba(0,0,0,0.7)' }}
        >
          <div
            className="w-full max-w-md p-6 rounded-xl"
            style={{ backgroundColor: 'var(--color-bg-secondary)', border: '1px solid var(--color-border-subtle)' }}
          >
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-lg font-bold" style={{ color: 'var(--color-text-primary)' }}>
                Nahrať faktúru
              </h2>
              <button
                onClick={() => setUploadModalOpen(false)}
                className="p-1 rounded hover:bg-opacity-80"
                style={{ backgroundColor: 'var(--color-bg-tertiary)' }}
              >
                <X size={20} style={{ color: 'var(--color-text-secondary)' }} />
              </button>
            </div>

            {uploadError && (
              <div
                className="p-3 rounded-lg mb-4 flex items-center gap-2"
                style={{ backgroundColor: 'var(--color-error-subtle)', color: 'var(--color-error)' }}
              >
                <AlertCircle size={16} />
                {uploadError}
              </div>
            )}

            {uploadSuccess && (
              <div
                className="p-3 rounded-lg mb-4 flex items-center gap-2"
                style={{ backgroundColor: 'var(--color-success-subtle)', color: 'var(--color-success)' }}
              >
                <CheckCircle size={16} />
                {uploadSuccess}
              </div>
            )}

            <div className="space-y-4">
              {/* Supplier */}
              <div>
                <label className="block text-sm font-medium mb-1" style={{ color: 'var(--color-text-secondary)' }}>
                  Dodávateľ *
                </label>
                <select
                  value={uploadSupplier}
                  onChange={(e) => setUploadSupplier(e.target.value)}
                  className="w-full p-2 rounded-lg"
                  style={{
                    backgroundColor: 'var(--color-bg-primary)',
                    border: '1px solid var(--color-border-subtle)',
                    color: 'var(--color-text-primary)',
                  }}
                >
                  <option value="">Vyberte dodávateľa</option>
                  {suppliers.length === 0 && (
                    <option value="" disabled>
                      (Žiadni dodávatelia - skontrolujte konfiguráciu)
                    </option>
                  )}
                  {suppliers.map((s) => (
                    <option key={s.code} value={s.code}>
                      {s.name}
                    </option>
                  ))}
                </select>
                {suppliers.length === 0 && (
                  <p className="text-xs mt-1" style={{ color: 'var(--color-warning)' }}>
                    Tip: Skontrolujte či existujú supplier configs v inventory-data/suppliers/*/config.json
                  </p>
                )}
              </div>

              {/* File */}
              <div>
                <label className="block text-sm font-medium mb-1" style={{ color: 'var(--color-text-secondary)' }}>
                  Súbor *
                </label>
                <input
                  type="file"
                  accept=".pdf,.csv,.xlsx,.xls,.doc,.docx,.xml,.txt"
                  onChange={(e) => setUploadFile(e.target.files?.[0] || null)}
                  className="w-full p-2 rounded-lg"
                  style={{
                    backgroundColor: 'var(--color-bg-primary)',
                    border: '1px solid var(--color-border-subtle)',
                    color: 'var(--color-text-primary)',
                  }}
                />
                {uploadFile && (
                  <div className="text-xs mt-1" style={{ color: 'var(--color-text-tertiary)' }}>
                    {uploadFile.name} ({(uploadFile.size / 1024).toFixed(1)} KB)
                  </div>
                )}
              </div>

              {/* Invoice number */}
              <div>
                <label className="block text-sm font-medium mb-1" style={{ color: 'var(--color-text-secondary)' }}>
                  Číslo faktúry (voliteľné)
                </label>
                <input
                  type="text"
                  value={uploadInvoiceNumber}
                  onChange={(e) => setUploadInvoiceNumber(e.target.value)}
                  placeholder="Automaticky z názvu súboru"
                  className="w-full p-2 rounded-lg"
                  style={{
                    backgroundColor: 'var(--color-bg-primary)',
                    border: '1px solid var(--color-border-subtle)',
                    color: 'var(--color-text-primary)',
                  }}
                />
              </div>

              {/* Dates */}
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-sm font-medium mb-1" style={{ color: 'var(--color-text-secondary)' }}>
                    Dátum vystavenia
                  </label>
                  <input
                    type="date"
                    value={uploadInvoiceDate}
                    onChange={(e) => setUploadInvoiceDate(e.target.value)}
                    className="w-full p-2 rounded-lg"
                    style={{
                      backgroundColor: 'var(--color-bg-primary)',
                      border: '1px solid var(--color-border-subtle)',
                      color: 'var(--color-text-primary)',
                    }}
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium mb-1" style={{ color: 'var(--color-text-secondary)' }}>
                    Splatnosť
                  </label>
                  <input
                    type="date"
                    value={uploadDueDate}
                    onChange={(e) => setUploadDueDate(e.target.value)}
                    className="w-full p-2 rounded-lg"
                    style={{
                      backgroundColor: 'var(--color-bg-primary)',
                      border: '1px solid var(--color-border-subtle)',
                      color: 'var(--color-text-primary)',
                    }}
                  />
                </div>
              </div>

              {/* VAT */}
              <div className="flex items-center gap-2">
                <input
                  type="checkbox"
                  id="vatIncluded"
                  checked={uploadVatIncluded}
                  onChange={(e) => setUploadVatIncluded(e.target.checked)}
                />
                <label htmlFor="vatIncluded" className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>
                  DPH je zahrnutá v cenách
                </label>
              </div>

              {/* Submit */}
              <div className="flex justify-end gap-3 pt-2">
                <button
                  onClick={() => setUploadModalOpen(false)}
                  className="px-4 py-2 rounded-lg"
                  style={{
                    backgroundColor: 'var(--color-bg-tertiary)',
                    color: 'var(--color-text-primary)',
                  }}
                >
                  Zrušiť
                </button>
                <button
                  onClick={handleUpload}
                  disabled={uploading || !uploadSupplier || !uploadFile}
                  className="px-4 py-2 rounded-lg disabled:opacity-50"
                  style={{
                    backgroundColor: 'var(--color-accent)',
                    color: 'var(--color-bg-primary)',
                  }}
                >
                  {uploading ? 'Nahrávam...' : 'Nahrať'}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default InvoicesPage;
