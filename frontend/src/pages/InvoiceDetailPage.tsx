import React, { useEffect, useState } from 'react';
import { useParams, Link, useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import {
  ArrowLeft,
  Download,
  Edit,
  Trash2,
  AlertCircle,
  CheckCircle,
  Clock,
  RefreshCw,
  Package,
  Building2,
  Calendar,
  FileText,
  Save,
  X,
} from 'lucide-react';

const API_BASE = (import.meta.env.VITE_API_BASE || '/api').replace(/\/$/, '');

// ============================================================================
// Types
// ============================================================================

interface InvoiceLine {
  id: number;
  line_number: number;
  ean: string | null;
  supplier_sku: string | null;
  product_name: string | null;
  quantity: number;
  unit: string;
  unit_price: number | null;
  discount_percent: number | null;
  total_price: number | null;
  vat_rate: number | null;
  unit_price_with_vat: number | null;
  total_price_with_vat: number | null;
  matched_product_id: number | null;
  product_sku: string | null;
  product_name_matched: string | null;
  matched_supplier_product_id: number | null;
  supplier_product_name: string | null;
  supplier_product_images: string[] | null;
  is_new_product: boolean;
}

interface InvoiceDetail {
  id: number;
  supplier_id: number;
  supplier_code: string;
  supplier_name: string;
  original_filename: string;
  stored_filename: string;
  file_path: string;
  file_type: string;
  invoice_number: string | null;
  invoice_date: string | null;
  due_date: string | null;
  currency: string;
  total_amount: number | null;
  total_without_vat: number | null;
  vat_amount: number | null;
  total_with_vat: number | null;
  vat_rate: number;
  vat_included: boolean;
  items_count: number;
  payment_status: 'unpaid' | 'partial' | 'paid';
  paid_at: string | null;
  paid_amount: number | null;
  is_parsed: boolean;
  parse_error: string | null;
  receiving_session_id: number | null;
  receiving_status: string;
  notes: string | null;
  is_overdue: boolean;
  days_until_due: number | null;
  created_at: string;
  updated_at: string;
  lines: InvoiceLine[];
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

const formatDateTime = (dateStr: string | null): string => {
  if (!dateStr) return '—';
  try {
    return new Date(dateStr).toLocaleString('sk-SK');
  } catch {
    return dateStr;
  }
};

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
        className="inline-flex items-center gap-1 px-3 py-1.5 rounded-full text-sm font-medium"
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
      className="inline-flex items-center gap-1 px-3 py-1.5 rounded-full text-sm font-medium"
      style={{ backgroundColor: style.bg, color: style.color }}
    >
      {style.icon}
      {labels[status] || status}
    </span>
  );
};

// ============================================================================
// Main Component
// ============================================================================

export function InvoiceDetailPage() {
  const { t } = useTranslation();
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();

  const [invoice, setInvoice] = useState<InvoiceDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  
  // Edit mode
  const [editing, setEditing] = useState(false);
  const [editForm, setEditForm] = useState({
    invoice_number: '',
    invoice_date: '',
    due_date: '',
    total_amount: '',
    vat_rate: '',
    notes: '',
  });
  const [saving, setSaving] = useState(false);

  // Fetch invoice
  useEffect(() => {
    const fetchInvoice = async () => {
      if (!id) return;
      
      setLoading(true);
      setError(null);

      try {
        const res = await fetch(`${API_BASE}/invoices/${id}`);
        if (!res.ok) {
          if (res.status === 404) {
            throw new Error('Faktúra nebola nájdená');
          }
          throw new Error(`HTTP ${res.status}`);
        }

        const data: InvoiceDetail = await res.json();
        setInvoice(data);
        
        // Initialize edit form
        setEditForm({
          invoice_number: data.invoice_number || '',
          invoice_date: data.invoice_date || '',
          due_date: data.due_date || '',
          total_amount: data.total_amount?.toString() || '',
          vat_rate: data.vat_rate?.toString() || '23',
          notes: data.notes || '',
        });
      } catch (e) {
        setError(e instanceof Error ? e.message : 'Failed to fetch invoice');
      } finally {
        setLoading(false);
      }
    };

    fetchInvoice();
  }, [id]);

  // Handle save
  const handleSave = async () => {
    if (!invoice) return;
    
    setSaving(true);
    try {
      const res = await fetch(`${API_BASE}/invoices/${invoice.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          invoice_number: editForm.invoice_number || null,
          invoice_date: editForm.invoice_date || null,
          due_date: editForm.due_date || null,
          total_amount: editForm.total_amount ? parseFloat(editForm.total_amount) : null,
          vat_rate: editForm.vat_rate ? parseFloat(editForm.vat_rate) : null,
          notes: editForm.notes || null,
        }),
      });

      if (!res.ok) {
        throw new Error('Failed to save');
      }

      // Refresh
      const refreshRes = await fetch(`${API_BASE}/invoices/${invoice.id}`);
      if (refreshRes.ok) {
        setInvoice(await refreshRes.json());
      }
      
      setEditing(false);
    } catch (e) {
      alert(e instanceof Error ? e.message : 'Save failed');
    } finally {
      setSaving(false);
    }
  };

  // Handle payment status change
  const handlePaymentChange = async (newStatus: 'unpaid' | 'partial' | 'paid') => {
    if (!invoice) return;
    
    try {
      const res = await fetch(`${API_BASE}/invoices/${invoice.id}/payment`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ payment_status: newStatus }),
      });

      if (!res.ok) {
        throw new Error('Failed to update payment status');
      }

      // Refresh
      const refreshRes = await fetch(`${API_BASE}/invoices/${invoice.id}`);
      if (refreshRes.ok) {
        setInvoice(await refreshRes.json());
      }
    } catch (e) {
      alert(e instanceof Error ? e.message : 'Update failed');
    }
  };

  // Handle delete
  const handleDelete = async () => {
    if (!invoice) return;
    
    if (!confirm('Naozaj chcete zmazať túto faktúru?')) return;
    
    try {
      const res = await fetch(`${API_BASE}/invoices/${invoice.id}?delete_file=true`, {
        method: 'DELETE',
      });

      if (!res.ok) {
        throw new Error('Failed to delete');
      }

      navigate('/invoices');
    } catch (e) {
      alert(e instanceof Error ? e.message : 'Delete failed');
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center py-20">
        <RefreshCw className="animate-spin mr-2" size={24} />
        <span style={{ color: 'var(--color-text-secondary)' }}>Načítavam...</span>
      </div>
    );
  }

  if (error) {
    return (
      <div className="space-y-4">
        <Link
          to="/invoices"
          className="inline-flex items-center gap-2 text-sm"
          style={{ color: 'var(--color-text-secondary)' }}
        >
          <ArrowLeft size={16} />
          Späť na zoznam
        </Link>
        <div
          className="p-4 rounded-xl flex items-center gap-3"
          style={{ backgroundColor: 'var(--color-error-subtle)', color: 'var(--color-error)' }}
        >
          <AlertCircle size={20} />
          {error}
        </div>
      </div>
    );
  }

  if (!invoice) return null;

  const inputStyle: React.CSSProperties = {
    backgroundColor: 'var(--color-bg-primary)',
    border: '1px solid var(--color-border-subtle)',
    borderRadius: '8px',
    padding: '8px 12px',
    color: 'var(--color-text-primary)',
    width: '100%',
  };

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-4">
          <Link
            to="/invoices"
            className="p-2 rounded-lg hover:bg-opacity-80"
            style={{ backgroundColor: 'var(--color-bg-secondary)' }}
          >
            <ArrowLeft size={20} style={{ color: 'var(--color-text-secondary)' }} />
          </Link>
          <div>
            <h1 className="text-2xl font-bold" style={{ color: 'var(--color-text-primary)' }}>
              {invoice.invoice_number || invoice.original_filename}
            </h1>
            <p className="text-sm mt-1" style={{ color: 'var(--color-text-secondary)' }}>
              {invoice.supplier_name} • {formatDate(invoice.invoice_date)}
            </p>
          </div>
        </div>
        <div className="flex items-center gap-3">
          <a
            href={`${API_BASE}/invoices/${invoice.id}/download`}
            className="flex items-center gap-2 px-4 py-2 rounded-lg"
            style={{
              backgroundColor: 'var(--color-bg-secondary)',
              color: 'var(--color-text-primary)',
              border: '1px solid var(--color-border-subtle)',
            }}
          >
            <Download size={16} />
            Stiahnuť
          </a>
          {!editing ? (
            <button
              onClick={() => setEditing(true)}
              className="flex items-center gap-2 px-4 py-2 rounded-lg"
              style={{
                backgroundColor: 'var(--color-accent)',
                color: 'var(--color-bg-primary)',
              }}
            >
              <Edit size={16} />
              Upraviť
            </button>
          ) : (
            <>
              <button
                onClick={() => setEditing(false)}
                className="flex items-center gap-2 px-4 py-2 rounded-lg"
                style={{
                  backgroundColor: 'var(--color-bg-tertiary)',
                  color: 'var(--color-text-primary)',
                }}
              >
                <X size={16} />
                Zrušiť
              </button>
              <button
                onClick={handleSave}
                disabled={saving}
                className="flex items-center gap-2 px-4 py-2 rounded-lg disabled:opacity-50"
                style={{
                  backgroundColor: 'var(--color-success)',
                  color: 'white',
                }}
              >
                <Save size={16} />
                {saving ? 'Ukladám...' : 'Uložiť'}
              </button>
            </>
          )}
          <button
            onClick={handleDelete}
            className="flex items-center gap-2 px-4 py-2 rounded-lg"
            style={{
              backgroundColor: 'var(--color-error-subtle)',
              color: 'var(--color-error)',
            }}
          >
            <Trash2 size={16} />
          </button>
        </div>
      </div>

      {/* Main info */}
      <div className="grid grid-cols-3 gap-6">
        {/* Left column - Invoice info */}
        <div
          className="col-span-2 p-6 rounded-xl"
          style={{ backgroundColor: 'var(--color-bg-secondary)', border: '1px solid var(--color-border-subtle)' }}
        >
          <h2 className="text-lg font-semibold mb-4" style={{ color: 'var(--color-text-primary)' }}>
            Informácie o faktúre
          </h2>
          
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-sm mb-1" style={{ color: 'var(--color-text-tertiary)' }}>
                Číslo faktúry
              </label>
              {editing ? (
                <input
                  type="text"
                  value={editForm.invoice_number}
                  onChange={(e) => setEditForm({ ...editForm, invoice_number: e.target.value })}
                  style={inputStyle}
                />
              ) : (
                <div style={{ color: 'var(--color-text-primary)' }}>
                  {invoice.invoice_number || '—'}
                </div>
              )}
            </div>
            
            <div>
              <label className="block text-sm mb-1" style={{ color: 'var(--color-text-tertiary)' }}>
                Dodávateľ
              </label>
              <div className="flex items-center gap-2" style={{ color: 'var(--color-text-primary)' }}>
                <Building2 size={16} style={{ color: 'var(--color-text-tertiary)' }} />
                {invoice.supplier_name}
              </div>
            </div>
            
            <div>
              <label className="block text-sm mb-1" style={{ color: 'var(--color-text-tertiary)' }}>
                Dátum vystavenia
              </label>
              {editing ? (
                <input
                  type="date"
                  value={editForm.invoice_date}
                  onChange={(e) => setEditForm({ ...editForm, invoice_date: e.target.value })}
                  style={inputStyle}
                />
              ) : (
                <div className="flex items-center gap-2" style={{ color: 'var(--color-text-primary)' }}>
                  <Calendar size={16} style={{ color: 'var(--color-text-tertiary)' }} />
                  {formatDate(invoice.invoice_date)}
                </div>
              )}
            </div>
            
            <div>
              <label className="block text-sm mb-1" style={{ color: 'var(--color-text-tertiary)' }}>
                Splatnosť
              </label>
              {editing ? (
                <input
                  type="date"
                  value={editForm.due_date}
                  onChange={(e) => setEditForm({ ...editForm, due_date: e.target.value })}
                  style={inputStyle}
                />
              ) : (
                <div className="flex items-center gap-2" style={{ color: invoice.is_overdue ? 'var(--color-error)' : 'var(--color-text-primary)' }}>
                  <Calendar size={16} style={{ color: 'var(--color-text-tertiary)' }} />
                  {formatDate(invoice.due_date)}
                  {invoice.days_until_due !== null && (
                    <span className="text-xs" style={{ color: invoice.is_overdue ? 'var(--color-error)' : 'var(--color-text-tertiary)' }}>
                      ({invoice.days_until_due > 0 ? `${invoice.days_until_due} dní` : invoice.days_until_due === 0 ? 'dnes' : `${Math.abs(invoice.days_until_due)} dní po`})
                    </span>
                  )}
                </div>
              )}
            </div>

            <div>
              <label className="block text-sm mb-1" style={{ color: 'var(--color-text-tertiary)' }}>
                Celková suma
              </label>
              {editing ? (
                <input
                  type="number"
                  step="0.01"
                  value={editForm.total_amount}
                  onChange={(e) => setEditForm({ ...editForm, total_amount: e.target.value })}
                  style={inputStyle}
                />
              ) : (
                <div className="text-lg font-semibold" style={{ color: 'var(--color-text-primary)' }}>
                  {formatCurrency(invoice.total_with_vat || invoice.total_amount, invoice.currency)}
                </div>
              )}
            </div>

            <div>
              <label className="block text-sm mb-1" style={{ color: 'var(--color-text-tertiary)' }}>
                DPH sadzba
              </label>
              {editing ? (
                <input
                  type="number"
                  step="0.01"
                  value={editForm.vat_rate}
                  onChange={(e) => setEditForm({ ...editForm, vat_rate: e.target.value })}
                  style={inputStyle}
                />
              ) : (
                <div style={{ color: 'var(--color-text-primary)' }}>
                  {invoice.vat_rate}% {invoice.vat_included ? '(zahrnutá)' : '(nezahrnutá)'}
                </div>
              )}
            </div>

            <div className="col-span-2">
              <label className="block text-sm mb-1" style={{ color: 'var(--color-text-tertiary)' }}>
                Poznámky
              </label>
              {editing ? (
                <textarea
                  value={editForm.notes}
                  onChange={(e) => setEditForm({ ...editForm, notes: e.target.value })}
                  rows={3}
                  style={inputStyle}
                />
              ) : (
                <div style={{ color: 'var(--color-text-secondary)' }}>
                  {invoice.notes || '—'}
                </div>
              )}
            </div>
          </div>

          {/* File info */}
          <div className="mt-6 pt-4 border-t" style={{ borderColor: 'var(--color-border-subtle)' }}>
            <div className="flex items-center gap-2 text-sm" style={{ color: 'var(--color-text-tertiary)' }}>
              <FileText size={14} />
              <span>{invoice.original_filename}</span>
              <span>•</span>
              <span>Typ: {invoice.file_type.toUpperCase()}</span>
              <span>•</span>
              <span>Nahraté: {formatDateTime(invoice.created_at)}</span>
            </div>
          </div>
        </div>

        {/* Right column - Status */}
        <div className="space-y-4">
          {/* Payment status */}
          <div
            className="p-6 rounded-xl"
            style={{ backgroundColor: 'var(--color-bg-secondary)', border: '1px solid var(--color-border-subtle)' }}
          >
            <h3 className="text-sm font-medium mb-3" style={{ color: 'var(--color-text-tertiary)' }}>
              Stav platby
            </h3>
            <PaymentStatusBadge status={invoice.payment_status} isOverdue={invoice.is_overdue} />
            
            <div className="mt-4 flex gap-2">
              {invoice.payment_status !== 'paid' && (
                <button
                  onClick={() => handlePaymentChange('paid')}
                  className="text-xs px-3 py-1.5 rounded"
                  style={{
                    backgroundColor: 'var(--color-success-subtle)',
                    color: 'var(--color-success)',
                  }}
                >
                  Označiť ako uhradené
                </button>
              )}
              {invoice.payment_status === 'paid' && (
                <button
                  onClick={() => handlePaymentChange('unpaid')}
                  className="text-xs px-3 py-1.5 rounded"
                  style={{
                    backgroundColor: 'var(--color-bg-tertiary)',
                    color: 'var(--color-text-secondary)',
                  }}
                >
                  Označiť ako neuhradené
                </button>
              )}
            </div>

            {invoice.paid_at && (
              <div className="mt-3 text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                Uhradené: {formatDateTime(invoice.paid_at)}
              </div>
            )}
          </div>

          {/* Receiving status */}
          <div
            className="p-6 rounded-xl"
            style={{ backgroundColor: 'var(--color-bg-secondary)', border: '1px solid var(--color-border-subtle)' }}
          >
            <h3 className="text-sm font-medium mb-3" style={{ color: 'var(--color-text-tertiary)' }}>
              Stav príjmu
            </h3>
            <div className="text-sm" style={{ color: 'var(--color-text-primary)' }}>
              {invoice.receiving_status === 'not_started' ? 'Nezačatý' : 
               invoice.receiving_status === 'in_progress' ? 'Prebieha' :
               invoice.receiving_status === 'completed' ? 'Dokončený' : invoice.receiving_status}
            </div>
            {invoice.receiving_session_id && (
              <Link
                to={`/receiving/${invoice.receiving_session_id}`}
                className="mt-2 text-xs inline-block"
                style={{ color: 'var(--color-accent)' }}
              >
                Zobraziť príjem →
              </Link>
            )}
          </div>

          {/* Summary */}
          <div
            className="p-6 rounded-xl"
            style={{ backgroundColor: 'var(--color-bg-secondary)', border: '1px solid var(--color-border-subtle)' }}
          >
            <h3 className="text-sm font-medium mb-3" style={{ color: 'var(--color-text-tertiary)' }}>
              Súhrn
            </h3>
            <div className="space-y-2 text-sm">
              <div className="flex justify-between">
                <span style={{ color: 'var(--color-text-tertiary)' }}>Položky:</span>
                <span style={{ color: 'var(--color-text-primary)' }}>{invoice.items_count}</span>
              </div>
              {invoice.total_without_vat && (
                <div className="flex justify-between">
                  <span style={{ color: 'var(--color-text-tertiary)' }}>Bez DPH:</span>
                  <span style={{ color: 'var(--color-text-primary)' }}>{formatCurrency(invoice.total_without_vat, invoice.currency)}</span>
                </div>
              )}
              {invoice.vat_amount && (
                <div className="flex justify-between">
                  <span style={{ color: 'var(--color-text-tertiary)' }}>DPH:</span>
                  <span style={{ color: 'var(--color-text-primary)' }}>{formatCurrency(invoice.vat_amount, invoice.currency)}</span>
                </div>
              )}
              <div className="flex justify-between pt-2 border-t" style={{ borderColor: 'var(--color-border-subtle)' }}>
                <span style={{ color: 'var(--color-text-tertiary)' }}>Celkom:</span>
                <span className="font-semibold" style={{ color: 'var(--color-text-primary)' }}>
                  {formatCurrency(invoice.total_with_vat || invoice.total_amount, invoice.currency)}
                </span>
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* Lines table */}
      <div
        className="rounded-xl overflow-hidden"
        style={{ backgroundColor: 'var(--color-bg-secondary)', border: '1px solid var(--color-border-subtle)' }}
      >
        <div className="p-4 border-b" style={{ borderColor: 'var(--color-border-subtle)' }}>
          <h2 className="text-lg font-semibold" style={{ color: 'var(--color-text-primary)' }}>
            Položky faktúry ({invoice.lines.length})
          </h2>
        </div>
        
        {invoice.lines.length === 0 ? (
          <div className="p-8 text-center" style={{ color: 'var(--color-text-tertiary)' }}>
            <Package size={32} className="mx-auto mb-2" />
            <p>Žiadne položky</p>
            <p className="text-sm mt-1">Faktúra ešte nebola spracovaná alebo nemá položky</p>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr style={{ backgroundColor: 'var(--color-bg-tertiary)' }}>
                  <th className="px-4 py-3 text-left font-medium" style={{ color: 'var(--color-text-secondary)' }}>#</th>
                  <th className="px-4 py-3 text-left font-medium" style={{ color: 'var(--color-text-secondary)' }}>EAN</th>
                  <th className="px-4 py-3 text-left font-medium" style={{ color: 'var(--color-text-secondary)' }}>Kód</th>
                  <th className="px-4 py-3 text-left font-medium" style={{ color: 'var(--color-text-secondary)' }}>Názov</th>
                  <th className="px-4 py-3 text-right font-medium" style={{ color: 'var(--color-text-secondary)' }}>Množstvo</th>
                  <th className="px-4 py-3 text-right font-medium" style={{ color: 'var(--color-text-secondary)' }}>Cena/ks</th>
                  <th className="px-4 py-3 text-right font-medium" style={{ color: 'var(--color-text-secondary)' }}>Celkom</th>
                  <th className="px-4 py-3 text-center font-medium" style={{ color: 'var(--color-text-secondary)' }}>Stav</th>
                </tr>
              </thead>
              <tbody>
                {invoice.lines.map((line) => (
                  <tr
                    key={line.id}
                    className="border-t"
                    style={{ borderColor: 'var(--color-border-subtle)' }}
                  >
                    <td className="px-4 py-3" style={{ color: 'var(--color-text-tertiary)' }}>
                      {line.line_number}
                    </td>
                    <td className="px-4 py-3 font-mono text-xs" style={{ color: 'var(--color-text-secondary)' }}>
                      {line.ean || '—'}
                    </td>
                    <td className="px-4 py-3 font-mono text-xs" style={{ color: 'var(--color-text-secondary)' }}>
                      {line.supplier_sku || '—'}
                    </td>
                    <td className="px-4 py-3">
                      <div style={{ color: 'var(--color-text-primary)' }}>
                        {line.product_name || '—'}
                      </div>
                      {line.matched_product_id && (
                        <div className="text-xs mt-0.5" style={{ color: 'var(--color-success)' }}>
                          ✓ Prepojené: {line.product_sku}
                        </div>
                      )}
                    </td>
                    <td className="px-4 py-3 text-right" style={{ color: 'var(--color-text-primary)' }}>
                      {line.quantity} {line.unit}
                    </td>
                    <td className="px-4 py-3 text-right font-mono" style={{ color: 'var(--color-text-secondary)' }}>
                      {line.unit_price ? formatCurrency(line.unit_price, invoice.currency) : '—'}
                    </td>
                    <td className="px-4 py-3 text-right font-mono" style={{ color: 'var(--color-text-primary)' }}>
                      {line.total_price ? formatCurrency(line.total_price, invoice.currency) : '—'}
                    </td>
                    <td className="px-4 py-3 text-center">
                      {line.matched_product_id ? (
                        <span
                          className="px-2 py-1 rounded text-xs"
                          style={{ backgroundColor: 'var(--color-success-subtle)', color: 'var(--color-success)' }}
                        >
                          Prepojené
                        </span>
                      ) : line.is_new_product ? (
                        <span
                          className="px-2 py-1 rounded text-xs"
                          style={{ backgroundColor: 'var(--color-warning-subtle)', color: 'var(--color-warning)' }}
                        >
                          Nový
                        </span>
                      ) : (
                        <span
                          className="px-2 py-1 rounded text-xs"
                          style={{ backgroundColor: 'var(--color-bg-tertiary)', color: 'var(--color-text-tertiary)' }}
                        >
                          Neprepojené
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}

export default InvoiceDetailPage;
