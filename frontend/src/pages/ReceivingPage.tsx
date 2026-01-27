import React, { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { Check, AlertCircle, Loader2, RefreshCw, Play, PlayCircle } from 'lucide-react';
import { Button } from '../components/ui/Button.new';
import { StatusBadge } from '../components/ui/Badge.new';
import { getInvoicesIndex, type InvoiceIndexItem } from '../api/invoices';
import { getSuppliers, type Supplier } from '../api/dashboard';
import { createReceivingSession, resumeReceiving } from '../api/receiving';

interface InvoiceDisplay {
  id: string;
  number: string;
  date: string;
  items: number;
  total: string;
  status: 'new' | 'in_progress' | 'processed';
  progress?: string;
  csvPath: string;
  // For in_progress
  currentSessionId?: string;
  pausedAt?: string;
  pauseStats?: {
    total_lines: number;
    received_complete: number;
    received_partial: number;
    not_received: number;
    total_scans: number;
  };
}

function formatDate(isoDate: string | null): string {
  if (!isoDate) return 'N/A';
  try {
    return new Date(isoDate).toLocaleDateString('sk-SK');
  } catch {
    return isoDate;
  }
}

function formatRelativeTime(isoDate: string | null): string {
  if (!isoDate) return '';
  try {
    const date = new Date(isoDate);
    const now = new Date();
    const diffMs = now.getTime() - date.getTime();
    const diffMins = Math.floor(diffMs / 60000);
    const diffHours = Math.floor(diffMs / 3600000);
    const diffDays = Math.floor(diffMs / 86400000);

    if (diffMins < 1) return 'práve teraz';
    if (diffMins < 60) return `pred ${diffMins} min`;
    if (diffHours < 24) return `pred ${diffHours}h`;
    if (diffDays === 1) return 'včera';
    return date.toLocaleDateString('sk-SK');
  } catch {
    return '';
  }
}

export function ReceivingPage() {
  const navigate = useNavigate();
  const [selectedInvoice, setSelectedInvoice] = useState<string | null>(null);
  const [suppliers, setSuppliers] = useState<Supplier[]>([]);
  const [supplier, setSupplier] = useState('paul-lange');
  const [invoices, setInvoices] = useState<InvoiceDisplay[]>([]);
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [resuming, setResuming] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Load suppliers
  useEffect(() => {
    async function loadSuppliers() {
      try {
        const data = await getSuppliers();
        setSuppliers(data);
        if (data.length > 0 && !data.find(s => s.supplier_code === supplier)) {
          setSupplier(data[0].supplier_code);
        }
      } catch (err) {
        console.error('Failed to load suppliers:', err);
      }
    }
    loadSuppliers();
  }, []);

  // Load invoices for selected supplier
  useEffect(() => {
    async function loadInvoices() {
      try {
        setLoading(true);
        setError(null);
        const data = await getInvoicesIndex(supplier);
        
        // Transform to display format - show new and in_progress
        const displayInvoices: InvoiceDisplay[] = (data.items || [])
          .filter(inv => inv.status === 'new' || inv.status === 'in_progress')
          .map(inv => ({
            id: inv.invoice_id,
            number: inv.number || inv.invoice_id,
            date: formatDate(inv.issue_date),
            items: inv.pause_stats?.total_lines || 0,
            total: 'N/A',
            status: inv.status as 'new' | 'in_progress',
            progress: inv.pause_stats 
              ? `${inv.pause_stats.received_complete}/${inv.pause_stats.total_lines}`
              : undefined,
            csvPath: inv.csv_path,
            currentSessionId: inv.current_session_id,
            pausedAt: inv.paused_at,
            pauseStats: inv.pause_stats,
          }))
          // Sort: in_progress first, then by date
          .sort((a, b) => {
            if (a.status === 'in_progress' && b.status !== 'in_progress') return -1;
            if (a.status !== 'in_progress' && b.status === 'in_progress') return 1;
            return 0;
          })
          .slice(0, 20);
        
        setInvoices(displayInvoices);
      } catch (err) {
        console.error('Failed to load invoices:', err);
        setError('Nepodarilo sa načítať faktúry');
        setInvoices([]);
      } finally {
        setLoading(false);
      }
    }
    loadInvoices();
  }, [supplier]);

  const pendingCount = invoices.length;
  const inProgressCount = invoices.filter(inv => inv.status === 'in_progress').length;

  const handleStartReceiving = async () => {
    if (!selectedInvoice) return;
    
    const invoice = invoices.find(inv => inv.id === selectedInvoice);
    if (!invoice) return;

    try {
      setCreating(true);
      setError(null);
      
      // Check if this is a resume or new session
      if (invoice.status === 'in_progress' && invoice.currentSessionId) {
        // Resume existing session
        setResuming(true);
        const resumed = await resumeReceiving(supplier, invoice.currentSessionId);
        
        navigate(`/receiving/${resumed.invoice_no}`, {
          state: { 
            sessionId: resumed.session_id,
            supplier,
            lines: resumed.lines,
            isResumed: true,
          }
        });
      } else {
        // Create new session
        const session = await createReceivingSession(supplier, invoice.number);
        
        navigate(`/receiving/${session.invoice_no}`, {
          state: { 
            sessionId: session.session_id,
            supplier,
            lines: session.lines,
          }
        });
      }
    } catch (err) {
      console.error('Failed to start/resume receiving session:', err);
      setError('Nepodarilo sa spustiť príjem');
    } finally {
      setCreating(false);
      setResuming(false);
    }
  };

  const handleRefresh = async () => {
    setLoading(true);
    try {
      const data = await getInvoicesIndex(supplier);
      const displayInvoices: InvoiceDisplay[] = (data.items || [])
        .filter(inv => inv.status === 'new' || inv.status === 'in_progress')
        .map(inv => ({
          id: inv.invoice_id,
          number: inv.number || inv.invoice_id,
          date: formatDate(inv.issue_date),
          items: inv.pause_stats?.total_lines || 0,
          total: 'N/A',
          status: inv.status as 'new' | 'in_progress',
          progress: inv.pause_stats 
            ? `${inv.pause_stats.received_complete}/${inv.pause_stats.total_lines}`
            : undefined,
          csvPath: inv.csv_path,
          currentSessionId: inv.current_session_id,
          pausedAt: inv.paused_at,
          pauseStats: inv.pause_stats,
        }))
        .sort((a, b) => {
          if (a.status === 'in_progress' && b.status !== 'in_progress') return -1;
          if (a.status !== 'in_progress' && b.status === 'in_progress') return 1;
          return 0;
        })
        .slice(0, 20);
      setInvoices(displayInvoices);
      setError(null);
    } catch (err) {
      setError('Nepodarilo sa obnoviť zoznam');
    } finally {
      setLoading(false);
    }
  };

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
            Príjem tovaru
          </h1>
          <p
            className="text-sm mt-1"
            style={{ color: 'var(--color-text-tertiary)' }}
          >
            Vyber faktúru pre spracovanie príjmu na sklad.
          </p>
        </div>
        <Button variant="secondary" onClick={handleRefresh} disabled={loading}>
          <RefreshCw size={16} className={loading ? 'animate-spin' : ''} />
          Obnoviť
        </Button>
      </div>

      {/* Error */}
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

      {/* Supplier Select */}
      <div className="flex items-center gap-4">
        <label
          className="text-sm"
          style={{ color: 'var(--color-text-secondary)' }}
        >
          Dodávateľ:
        </label>
        <select
          value={supplier}
          onChange={(e) => {
            setSupplier(e.target.value);
            setSelectedInvoice(null);
          }}
          className="w-48"
        >
          {suppliers.map(s => (
            <option key={s.supplier_code} value={s.supplier_code}>
              {s.name}
            </option>
          ))}
          {suppliers.length === 0 && (
            <option value="paul-lange">Paul-Lange</option>
          )}
        </select>
      </div>

      {/* Invoices List */}
      <div
        className="rounded-xl border overflow-hidden"
        style={{
          backgroundColor: 'var(--color-bg-secondary)',
          borderColor: 'var(--color-border-subtle)',
        }}
      >
        <div
          className="px-4 py-3 border-b"
          style={{ borderColor: 'var(--color-border-subtle)' }}
        >
          <h2
            className="text-sm font-medium uppercase tracking-wider"
            style={{ color: 'var(--color-text-secondary)' }}
          >
            Čakajúce faktúry ({pendingCount})
          </h2>
        </div>

        {loading ? (
          <div className="p-8 flex items-center justify-center">
            <Loader2 
              className="animate-spin" 
              size={24} 
              style={{ color: 'var(--color-accent)' }} 
            />
          </div>
        ) : (
          <div
            className="divide-y"
            style={{ borderColor: 'var(--color-border-subtle)' }}
          >
            {invoices.map((inv) => {
              const isSelected = selectedInvoice === inv.id;

              return (
                <div
                  key={inv.id}
                  onClick={() => setSelectedInvoice(inv.id)}
                  className="p-4 cursor-pointer transition-all"
                  style={{
                    backgroundColor: isSelected
                      ? 'var(--color-accent-subtle)'
                      : 'transparent',
                    borderLeft: isSelected
                      ? '3px solid var(--color-accent)'
                      : '3px solid transparent',
                  }}
                  onMouseEnter={(e) => {
                    if (!isSelected) {
                      e.currentTarget.style.backgroundColor = 'var(--color-bg-tertiary)';
                    }
                  }}
                  onMouseLeave={(e) => {
                    if (!isSelected) {
                      e.currentTarget.style.backgroundColor = 'transparent';
                    }
                  }}
                >
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-3">
                      {/* Selection indicator */}
                      <div
                        className="w-5 h-5 rounded-full border-2 flex items-center justify-center flex-shrink-0"
                        style={{
                          borderColor: isSelected
                            ? 'var(--color-accent)'
                            : inv.status === 'in_progress'
                            ? 'var(--color-info)'
                            : 'var(--color-text-tertiary)',
                          backgroundColor: isSelected
                            ? 'var(--color-accent)'
                            : inv.status === 'in_progress'
                            ? 'var(--color-info-subtle)'
                            : 'transparent',
                        }}
                      >
                        {isSelected && (
                          <Check
                            size={12}
                            style={{ color: 'var(--color-text-inverse)' }}
                          />
                        )}
                        {!isSelected && inv.status === 'in_progress' && (
                          <Play
                            size={10}
                            style={{ color: 'var(--color-info)' }}
                          />
                        )}
                      </div>

                      <div>
                        <div className="flex items-center gap-2">
                          <span
                            className="font-medium"
                            style={{
                              fontFamily: 'var(--font-mono)',
                              color: 'var(--color-text-primary)',
                            }}
                          >
                            {inv.number}
                          </span>
                          {inv.status === 'in_progress' && (
                            <span
                              className="text-xs px-2 py-0.5 rounded-full"
                              style={{
                                backgroundColor: 'var(--color-info-subtle)',
                                color: 'var(--color-info)',
                              }}
                            >
                              Rozpracovaná
                            </span>
                          )}
                        </div>
                        <div
                          className="text-sm mt-0.5 flex items-center gap-2"
                          style={{ color: 'var(--color-text-tertiary)' }}
                        >
                          <span>{inv.date}</span>
                          {inv.status === 'in_progress' && inv.pausedAt && (
                            <>
                              <span>•</span>
                              <span>Pozastavené {formatRelativeTime(inv.pausedAt)}</span>
                            </>
                          )}
                          {inv.status === 'in_progress' && inv.progress && (
                            <>
                              <span>•</span>
                              <span style={{ color: 'var(--color-info)' }}>{inv.progress} položiek</span>
                            </>
                          )}
                        </div>
                      </div>
                    </div>

                    <StatusBadge
                      status={inv.status === 'in_progress' ? 'partial' : inv.status === 'new' ? 'pending' : inv.status}
                      label={
                        inv.status === 'new'
                          ? 'Nová'
                          : inv.status === 'in_progress'
                          ? `${inv.progress || 'Rozpracovaná'}`
                          : 'Dokončená'
                      }
                    />
                  </div>
                </div>
              );
            })}

            {invoices.length === 0 && !loading && (
              <div
                className="p-8 text-center"
                style={{ color: 'var(--color-text-tertiary)' }}
              >
                <AlertCircle
                  size={32}
                  className="mx-auto mb-2"
                  style={{ color: 'var(--color-text-tertiary)' }}
                />
                <div>Žiadne čakajúce faktúry</div>
              </div>
            )}
          </div>
        )}
      </div>

      {/* Actions */}
      <div className="flex justify-end gap-3">
        <Button variant="secondary" onClick={() => navigate('/')}>
          Späť
        </Button>
        {selectedInvoice && invoices.find(inv => inv.id === selectedInvoice)?.status === 'in_progress' ? (
          <Button
            variant="primary"
            onClick={handleStartReceiving}
            disabled={!selectedInvoice || creating || resuming}
            loading={resuming}
          >
            <PlayCircle size={16} />
            Pokračovať v príjme →
          </Button>
        ) : (
          <Button
            variant="primary"
            onClick={handleStartReceiving}
            disabled={!selectedInvoice || creating}
            loading={creating}
          >
            Začať príjem →
          </Button>
        )}
      </div>
    </div>
  );
}

export default ReceivingPage;
