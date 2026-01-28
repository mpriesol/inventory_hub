import React, { useEffect, useState } from 'react';
import { useParams, Link, useNavigate } from 'react-router-dom';
import {
  ArrowLeft,
  FileText,
  Building2,
  Calendar,
  Package,
  CheckCircle,
  Clock,
  AlertCircle,
  CreditCard,
  Download,
  RefreshCw,
  Search,
  Link as LinkIcon,
  PlusCircle,
  Image,
  ExternalLink,
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
  total_without_vat: number | null;
  vat_amount: number | null;
  total_with_vat: number | null;
  vat_rate: number;
  vat_included: boolean;
  payment_status: 'unpaid' | 'partial' | 'paid';
  paid_at: string | null;
  paid_amount: number | null;
  receiving_status: string;
  items_count: number;
  supplier: Supplier;
  warehouse: { id: number; code: string; name: string };
  invoice_file_path: string | null;
  is_overdue: boolean;
  days_until_due: number | null;
  created_at: string;
  updated_at: string;
}

interface InvoiceLine {
  id: number;
  line_number: number;
  ean: string | null;
  supplier_sku: string | null;
  description: string | null;
  ordered_qty: number;
  received_qty: number;
  unit_price: number | null;
  total_price: number | null;
  unit_price_with_vat: number | null;
  total_price_with_vat: number | null;
  vat_rate: number | null;
  status: string;
  is_new_product: boolean;
  product_image_url: string | null;
  product: {
    id: number;
    sku: string;
    name: string;
    brand: string | null;
  } | null;
  supplier_product: {
    id: number;
    name: string;
    images: string[];
    purchase_price: number | null;
  } | null;
  match_method: string | null;
}

interface InvoiceStats {
  total_lines: number;
  matched_products: number;
  new_products: number;
  pending_lines: number;
  matched_lines: number;
  partial_lines: number;
}

interface InvoiceDetailResponse {
  invoice: Invoice;
  lines: InvoiceLine[];
  stats: InvoiceStats;
}

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

const formatDateTime = (dateStr: string | null): string => {
  if (!dateStr) return '—';
  try {
    return new Date(dateStr).toLocaleString('sk-SK');
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
        className="inline-flex items-center gap-1 px-3 py-1.5 rounded-lg text-sm font-medium"
        style={{
          backgroundColor: 'var(--color-error-subtle)',
          color: 'var(--color-error)',
        }}
      >
        <AlertCircle size={14} />
        Po splatnosti
      </span>
    );
  }

  const styles: Record<string, { bg: string; color: string; icon: React.ReactNode }> = {
    paid: {
      bg: 'var(--color-success-subtle)',
      color: 'var(--color-success)',
      icon: <CheckCircle size={14} />,
    },
    partial: {
      bg: 'var(--color-warning-subtle)',
      color: 'var(--color-warning)',
      icon: <Clock size={14} />,
    },
    unpaid: {
      bg: 'var(--color-bg-tertiary)',
      color: 'var(--color-text-secondary)',
      icon: <Clock size={14} />,
    },
  };

  const style = styles[status] || styles.unpaid;
  const labels: Record<string, string> = {
    paid: 'Uhradené',
    partial: 'Čiastočne uhradené',
    unpaid: 'Neuhradené',
  };

  return (
    <span
      className="inline-flex items-center gap-1 px-3 py-1.5 rounded-lg text-sm font-medium"
      style={{ backgroundColor: style.bg, color: style.color }}
    >
      {style.icon}
      {labels[status] || status}
    </span>
  );
};

const LineStatusBadge: React.FC<{ status: string; isNew: boolean }> = ({ status, isNew }) => {
  if (isNew) {
    return (
      <span
        className="inline-flex items-center gap-1 px-2 py-1 rounded-full text-xs font-medium"
        style={{
          backgroundColor: 'var(--color-info-subtle)',
          color: 'var(--color-info)',
        }}
      >
        <PlusCircle size={12} />
        Nový produkt
      </span>
    );
  }

  const styles: Record<string, { bg: string; color: string }> = {
    matched: { bg: 'var(--color-success-subtle)', color: 'var(--color-success)' },
    partial: { bg: 'var(--color-warning-subtle)', color: 'var(--color-warning)' },
    pending: { bg: 'var(--color-bg-tertiary)', color: 'var(--color-text-secondary)' },
    overage: { bg: 'var(--color-error-subtle)', color: 'var(--color-error)' },
  };

  const labels: Record<string, string> = {
    matched: 'Prijaté',
    partial: 'Čiastočne',
    pending: 'Čaká',
    overage: 'Naviac',
  };

  const style = styles[status] || styles.pending;

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
export function InvoiceDetailPage() {
  const { invoiceId } = useParams<{ invoiceId: string }>();
  const navigate = useNavigate();

  const [data, setData] = useState<InvoiceDetailResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [updating, setUpdating] = useState(false);
  const [lineFilter, setLineFilter] = useState('');

  // Fetch invoice detail
  useEffect(() => {
    const fetchDetail = async () => {
      if (!invoiceId) return;

      setLoading(true);
      setError(null);

      try {
        const res = await fetch(`${API_BASE}/invoices/${invoiceId}`);
        if (!res.ok) {
          throw new Error(`HTTP ${res.status}: ${res.statusText}`);
        }

        const result = await res.json();
        setData(result);
      } catch (e: any) {
        setError(e.message || 'Chyba pri načítaní faktúry');
      } finally {
        setLoading(false);
      }
    };

    fetchDetail();
  }, [invoiceId]);

  // Update payment status
  const updatePaymentStatus = async (newStatus: 'unpaid' | 'partial' | 'paid') => {
    if (!data || updating) return;

    setUpdating(true);
    try {
      const res = await fetch(`${API_BASE}/invoices/${invoiceId}/payment`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ payment_status: newStatus }),
      });

      if (!res.ok) {
        throw new Error(`HTTP ${res.status}`);
      }

      const result = await res.json();
      setData({
        ...data,
        invoice: {
          ...data.invoice,
          payment_status: result.payment_status,
          paid_at: result.paid_at,
          paid_amount: result.paid_amount,
        },
      });
    } catch (e: any) {
      console.error('Failed to update payment status:', e);
    } finally {
      setUpdating(false);
    }
  };

  // Filter lines
  const filteredLines = data?.lines.filter((line) => {
    if (!lineFilter) return true;
    const search = lineFilter.toLowerCase();
    return (
      line.ean?.toLowerCase().includes(search) ||
      line.supplier_sku?.toLowerCase().includes(search) ||
      line.description?.toLowerCase().includes(search) ||
      line.product?.sku.toLowerCase().includes(search) ||
      line.product?.name.toLowerCase().includes(search)
    );
  }) || [];

  if (loading) {
    return (
      <div className="p-6 flex items-center justify-center h-64">
        <RefreshCw className="animate-spin mr-2" size={24} style={{ color: 'var(--color-accent)' }} />
        <span style={{ color: 'var(--color-text-secondary)' }}>Načítavam...</span>
      </div>
    );
  }

  if (error || !data) {
    return (
      <div className="p-6">
        <div
          className="p-4 rounded-xl flex items-center gap-3"
          style={{ backgroundColor: 'var(--color-error-subtle)', color: 'var(--color-error)' }}
        >
          <AlertCircle size={20} />
          {error || 'Faktúra nenájdená'}
        </div>
        <Link
          to="/invoices"
          className="inline-flex items-center gap-2 mt-4 px-4 py-2 rounded-lg"
          style={{ backgroundColor: 'var(--color-bg-tertiary)', color: 'var(--color-text-primary)' }}
        >
          <ArrowLeft size={16} />
          Späť na zoznam
        </Link>
      </div>
    );
  }

  const { invoice, lines, stats } = data;

  return (
    <div className="p-6 space-y-6">
      {/* Header */}
      <div className="flex items-start justify-between">
        <div>
          <Link
            to="/invoices"
            className="inline-flex items-center gap-1 text-sm mb-2 hover:underline"
            style={{ color: 'var(--color-text-secondary)' }}
          >
            <ArrowLeft size={14} />
            Späť na faktúry
          </Link>
          <h1
            className="text-2xl font-semibold flex items-center gap-3"
            style={{ fontFamily: 'var(--font-display)', color: 'var(--color-text-primary)' }}
          >
            <FileText size={28} />
            Faktúra {invoice.invoice_number}
          </h1>
          <p className="mt-1" style={{ color: 'var(--color-text-secondary)' }}>
            {invoice.supplier.name}
          </p>
        </div>

        <PaymentStatusBadge status={invoice.payment_status} isOverdue={invoice.is_overdue} />
      </div>

      {/* Info Cards */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        {/* Invoice Info */}
        <div
          className="p-4 rounded-xl"
          style={{ backgroundColor: 'var(--color-bg-secondary)', border: '1px solid var(--color-border-subtle)' }}
        >
          <div className="flex items-center gap-2 mb-3" style={{ color: 'var(--color-text-secondary)' }}>
            <FileText size={16} />
            <span className="text-sm font-medium">Údaje faktúry</span>
          </div>
          <dl className="space-y-2 text-sm">
            <div className="flex justify-between">
              <dt style={{ color: 'var(--color-text-tertiary)' }}>Číslo:</dt>
              <dd className="font-mono">{invoice.invoice_number}</dd>
            </div>
            <div className="flex justify-between">
              <dt style={{ color: 'var(--color-text-tertiary)' }}>Dátum:</dt>
              <dd>{formatDate(invoice.invoice_date)}</dd>
            </div>
            <div className="flex justify-between">
              <dt style={{ color: 'var(--color-text-tertiary)' }}>Splatnosť:</dt>
              <dd style={{ color: invoice.is_overdue ? 'var(--color-error)' : 'inherit' }}>
                {formatDate(invoice.due_date)}
              </dd>
            </div>
            <div className="flex justify-between">
              <dt style={{ color: 'var(--color-text-tertiary)' }}>Vytvorené:</dt>
              <dd>{formatDateTime(invoice.created_at)}</dd>
            </div>
          </dl>
        </div>

        {/* Amounts */}
        <div
          className="p-4 rounded-xl"
          style={{ backgroundColor: 'var(--color-bg-secondary)', border: '1px solid var(--color-border-subtle)' }}
        >
          <div className="flex items-center gap-2 mb-3" style={{ color: 'var(--color-text-secondary)' }}>
            <CreditCard size={16} />
            <span className="text-sm font-medium">Sumy</span>
          </div>
          <dl className="space-y-2 text-sm">
            <div className="flex justify-between">
              <dt style={{ color: 'var(--color-text-tertiary)' }}>Bez DPH:</dt>
              <dd className="font-mono">{formatCurrency(invoice.total_without_vat, invoice.currency)}</dd>
            </div>
            <div className="flex justify-between">
              <dt style={{ color: 'var(--color-text-tertiary)' }}>DPH ({invoice.vat_rate}%):</dt>
              <dd className="font-mono">{formatCurrency(invoice.vat_amount, invoice.currency)}</dd>
            </div>
            <div
              className="flex justify-between pt-2 border-t font-medium"
              style={{ borderColor: 'var(--color-border-subtle)' }}
            >
              <dt>S DPH:</dt>
              <dd className="font-mono">{formatCurrency(invoice.total_with_vat || invoice.total_amount, invoice.currency)}</dd>
            </div>
            {!invoice.vat_included && (
              <div className="text-xs pt-2" style={{ color: 'var(--color-warning)' }}>
                ⚠️ Reverse charge – DPH bude dopočítané
              </div>
            )}
          </dl>
        </div>

        {/* Payment & Actions */}
        <div
          className="p-4 rounded-xl"
          style={{ backgroundColor: 'var(--color-bg-secondary)', border: '1px solid var(--color-border-subtle)' }}
        >
          <div className="flex items-center gap-2 mb-3" style={{ color: 'var(--color-text-secondary)' }}>
            <CheckCircle size={16} />
            <span className="text-sm font-medium">Stav platby</span>
          </div>

          <div className="space-y-3">
            <div className="flex gap-2">
              <button
                onClick={() => updatePaymentStatus('unpaid')}
                disabled={updating || invoice.payment_status === 'unpaid'}
                className="flex-1 px-3 py-2 rounded-lg text-sm font-medium transition-colors disabled:opacity-50"
                style={{
                  backgroundColor:
                    invoice.payment_status === 'unpaid'
                      ? 'var(--color-accent-subtle)'
                      : 'var(--color-bg-tertiary)',
                  color:
                    invoice.payment_status === 'unpaid' ? 'var(--color-accent)' : 'var(--color-text-secondary)',
                  border:
                    invoice.payment_status === 'unpaid'
                      ? '1px solid var(--color-accent)'
                      : '1px solid var(--color-border-subtle)',
                }}
              >
                Neuhradené
              </button>
              <button
                onClick={() => updatePaymentStatus('paid')}
                disabled={updating || invoice.payment_status === 'paid'}
                className="flex-1 px-3 py-2 rounded-lg text-sm font-medium transition-colors disabled:opacity-50"
                style={{
                  backgroundColor:
                    invoice.payment_status === 'paid'
                      ? 'var(--color-success-subtle)'
                      : 'var(--color-bg-tertiary)',
                  color:
                    invoice.payment_status === 'paid' ? 'var(--color-success)' : 'var(--color-text-secondary)',
                  border:
                    invoice.payment_status === 'paid'
                      ? '1px solid var(--color-success)'
                      : '1px solid var(--color-border-subtle)',
                }}
              >
                Uhradené
              </button>
            </div>

            {invoice.paid_at && (
              <div className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                Uhradené: {formatDateTime(invoice.paid_at)}
                {invoice.paid_amount && ` • ${formatCurrency(invoice.paid_amount, invoice.currency)}`}
              </div>
            )}

            {invoice.invoice_file_path && (
              <a
                href={`${API_BASE}/files/download?relpath=${encodeURIComponent(invoice.invoice_file_path)}`}
                target="_blank"
                rel="noreferrer"
                className="flex items-center justify-center gap-2 px-3 py-2 rounded-lg text-sm font-medium transition-colors"
                style={{
                  backgroundColor: 'var(--color-bg-tertiary)',
                  color: 'var(--color-text-primary)',
                  border: '1px solid var(--color-border-subtle)',
                }}
              >
                <Download size={14} />
                Stiahnuť PDF
              </a>
            )}
          </div>
        </div>
      </div>

      {/* Lines Stats */}
      <div className="grid grid-cols-2 md:grid-cols-6 gap-3">
        <div
          className="p-3 rounded-lg text-center"
          style={{ backgroundColor: 'var(--color-bg-secondary)', border: '1px solid var(--color-border-subtle)' }}
        >
          <div className="text-lg font-semibold" style={{ fontFamily: 'var(--font-display)' }}>
            {stats.total_lines}
          </div>
          <div className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
            Položiek
          </div>
        </div>
        <div
          className="p-3 rounded-lg text-center"
          style={{ backgroundColor: 'var(--color-success-subtle)' }}
        >
          <div className="text-lg font-semibold" style={{ fontFamily: 'var(--font-display)', color: 'var(--color-success)' }}>
            {stats.matched_products}
          </div>
          <div className="text-xs" style={{ color: 'var(--color-success)' }}>
            Spárované
          </div>
        </div>
        <div
          className="p-3 rounded-lg text-center"
          style={{ backgroundColor: 'var(--color-info-subtle)' }}
        >
          <div className="text-lg font-semibold" style={{ fontFamily: 'var(--font-display)', color: 'var(--color-info)' }}>
            {stats.new_products}
          </div>
          <div className="text-xs" style={{ color: 'var(--color-info)' }}>
            Nové produkty
          </div>
        </div>
        <div
          className="p-3 rounded-lg text-center"
          style={{ backgroundColor: 'var(--color-bg-secondary)', border: '1px solid var(--color-border-subtle)' }}
        >
          <div className="text-lg font-semibold" style={{ fontFamily: 'var(--font-display)' }}>
            {stats.pending_lines}
          </div>
          <div className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
            Čaká na príjem
          </div>
        </div>
        <div
          className="p-3 rounded-lg text-center"
          style={{ backgroundColor: 'var(--color-success-subtle)' }}
        >
          <div className="text-lg font-semibold" style={{ fontFamily: 'var(--font-display)', color: 'var(--color-success)' }}>
            {stats.matched_lines}
          </div>
          <div className="text-xs" style={{ color: 'var(--color-success)' }}>
            Prijaté
          </div>
        </div>
        <div
          className="p-3 rounded-lg text-center"
          style={{ backgroundColor: 'var(--color-warning-subtle)' }}
        >
          <div className="text-lg font-semibold" style={{ fontFamily: 'var(--font-display)', color: 'var(--color-warning)' }}>
            {stats.partial_lines}
          </div>
          <div className="text-xs" style={{ color: 'var(--color-warning)' }}>
            Čiastočne
          </div>
        </div>
      </div>

      {/* Lines Table */}
      <div
        className="rounded-xl overflow-hidden"
        style={{ backgroundColor: 'var(--color-bg-secondary)', border: '1px solid var(--color-border-subtle)' }}
      >
        {/* Search */}
        <div className="p-4 border-b" style={{ borderColor: 'var(--color-border-subtle)' }}>
          <div className="relative max-w-md">
            <Search
              size={16}
              className="absolute left-3 top-1/2 -translate-y-1/2"
              style={{ color: 'var(--color-text-tertiary)' }}
            />
            <input
              type="text"
              placeholder="Hľadať podľa EAN, kódu, názvu..."
              value={lineFilter}
              onChange={(e) => setLineFilter(e.target.value)}
              className="w-full pl-9"
            />
          </div>
        </div>

        {/* Table */}
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr style={{ backgroundColor: 'var(--color-bg-tertiary)' }}>
                <th className="px-4 py-3 text-left font-medium w-12" style={{ color: 'var(--color-text-secondary)' }}>
                  #
                </th>
                <th className="px-4 py-3 text-left font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                  Produkt
                </th>
                <th className="px-4 py-3 text-left font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                  EAN / Kód
                </th>
                <th className="px-4 py-3 text-right font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                  Obj. / Prij.
                </th>
                <th className="px-4 py-3 text-right font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                  Cena
                </th>
                <th className="px-4 py-3 text-right font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                  Celkom
                </th>
                <th className="px-4 py-3 text-center font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                  Stav
                </th>
              </tr>
            </thead>
            <tbody>
              {filteredLines.length === 0 ? (
                <tr>
                  <td colSpan={7} className="px-4 py-8 text-center" style={{ color: 'var(--color-text-tertiary)' }}>
                    {lineFilter ? 'Žiadne položky vyhovujúce filtru' : 'Žiadne položky'}
                  </td>
                </tr>
              ) : (
                filteredLines.map((line) => (
                  <tr
                    key={line.id}
                    className="border-t"
                    style={{ borderColor: 'var(--color-border-subtle)' }}
                  >
                    <td className="px-4 py-3" style={{ color: 'var(--color-text-tertiary)' }}>
                      {line.line_number}
                    </td>
                    <td className="px-4 py-3">
                      <div className="flex items-start gap-3">
                        {/* Image */}
                        {(line.product_image_url || line.supplier_product?.images?.[0]) ? (
                          <img
                            src={line.product_image_url || line.supplier_product?.images?.[0]}
                            alt=""
                            className="w-10 h-10 rounded object-cover"
                            style={{ backgroundColor: 'var(--color-bg-tertiary)' }}
                          />
                        ) : (
                          <div
                            className="w-10 h-10 rounded flex items-center justify-center"
                            style={{ backgroundColor: 'var(--color-bg-tertiary)' }}
                          >
                            <Image size={16} style={{ color: 'var(--color-text-tertiary)' }} />
                          </div>
                        )}
                        <div className="flex-1 min-w-0">
                          <div className="font-medium truncate" title={line.description || ''}>
                            {line.description || '—'}
                          </div>
                          {line.product ? (
                            <div className="flex items-center gap-1 text-xs mt-0.5" style={{ color: 'var(--color-success)' }}>
                              <LinkIcon size={10} />
                              <span className="font-mono">{line.product.sku}</span>
                              {line.product.name && (
                                <span style={{ color: 'var(--color-text-tertiary)' }}>
                                  — {line.product.name}
                                </span>
                              )}
                            </div>
                          ) : line.supplier_product ? (
                            <div className="flex items-center gap-1 text-xs mt-0.5" style={{ color: 'var(--color-info)' }}>
                              <Package size={10} />
                              <span>Z feedu: {line.supplier_product.name}</span>
                            </div>
                          ) : null}
                        </div>
                      </div>
                    </td>
                    <td className="px-4 py-3 font-mono text-xs">
                      {line.ean && (
                        <div title="EAN">{line.ean}</div>
                      )}
                      {line.supplier_sku && (
                        <div style={{ color: 'var(--color-text-tertiary)' }} title="Kód dodávateľa">
                          {line.supplier_sku}
                        </div>
                      )}
                    </td>
                    <td className="px-4 py-3 text-right font-mono">
                      <span>{line.ordered_qty}</span>
                      <span style={{ color: 'var(--color-text-tertiary)' }}> / </span>
                      <span
                        style={{
                          color:
                            line.received_qty >= line.ordered_qty
                              ? 'var(--color-success)'
                              : line.received_qty > 0
                              ? 'var(--color-warning)'
                              : 'inherit',
                        }}
                      >
                        {line.received_qty}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-right font-mono">
                      {formatCurrency(line.unit_price_with_vat || line.unit_price, invoice.currency)}
                    </td>
                    <td className="px-4 py-3 text-right font-mono">
                      {formatCurrency(line.total_price_with_vat || line.total_price, invoice.currency)}
                    </td>
                    <td className="px-4 py-3 text-center">
                      <LineStatusBadge status={line.status} isNew={line.is_new_product} />
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

export default InvoiceDetailPage;
