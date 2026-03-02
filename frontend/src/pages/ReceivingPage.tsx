import React, { useState, useEffect, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import { 
  Check, AlertCircle, Loader2, RefreshCw, Play, PlayCircle, 
  Upload, Download, ExternalLink, XCircle, CheckCircle, FileText
} from 'lucide-react';
import { Button } from '../components/ui/Button.new';
import { getInvoicesIndex, refreshInvoices, type InvoiceIndexItem } from '../api/invoices';
import { listSuppliers, uploadInvoice, type SupplierSummary } from '../api/suppliers';
import { createReceivingSession, resumeReceiving } from '../api/receiving';
import { API_BASE } from '../api/client';
import { ReceivingResultsModal } from '../components/ReceivingResultsModal';

interface RefreshResult {
  ok: boolean;
  downloaded: number;
  skipped: number;
  failed: number;
  pages?: number;
  errors?: string[];
  log_files?: string[];
  message?: string;
}

interface InvoiceDisplay {
  id: string;
  number: string;
  date: string;
  items: number;
  total: string;
  status: 'new' | 'in_progress' | 'processed';
  progress?: string;
  csvPath: string;
  currentSessionId?: string;
  pausedAt?: string;
  processedAt?: string;
  note?: string;
  pauseStats?: {
    total_lines: number;
    received_complete: number;
    received_partial: number;
    not_received: number;
    total_scans: number;
  };
}

type FilterTab = 'new' | 'in_progress' | 'processed';

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

const TAB_LABELS: Record<FilterTab, string> = {
  new: 'Nové',
  in_progress: 'Prebieha',
  processed: 'Dokončené',
};

export function ReceivingPage() {
  const navigate = useNavigate();
  const [selectedInvoice, setSelectedInvoice] = useState<string | null>(null);
  const [suppliers, setSuppliers] = useState<SupplierSummary[]>([]);
  const [supplier, setSupplier] = useState('paul-lange');
  const [invoices, setInvoices] = useState<InvoiceDisplay[]>([]);
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [resuming, setResuming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<FilterTab>('new');

  // Modal pre dokončené faktúry
  const [modalInvoice, setModalInvoice] = useState<InvoiceDisplay | null>(null);

  // Refresh from web state
  const [refreshing, setRefreshing] = useState(false);
  const [refreshResult, setRefreshResult] = useState<RefreshResult | null>(null);
  const [successMessage, setSuccessMessage] = useState<string | null>(null);
  
  // Upload state
  const [uploading, setUploading] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Load suppliers
  useEffect(() => {
    async function loadSuppliers() {
      try {
        const data = await listSuppliers();
        setSuppliers(data);
        if (data.length > 0 && !data.find(s => s.code === supplier)) {
          setSupplier(data[0].code);
        }
      } catch (err) {
        console.error('Failed to load suppliers:', err);
      }
    }
    loadSuppliers();
  }, []);

  function buildDisplayInvoices(items: InvoiceIndexItem[]): InvoiceDisplay[] {
    return (items || [])
      .map(inv => ({
        id: inv.invoice_id,
        number: inv.number || inv.invoice_id,
        date: formatDate(inv.issue_date),
        items: inv.pause_stats?.total_lines || 0,
        total: 'N/A',
        status: inv.status as 'new' | 'in_progress' | 'processed',
        progress: inv.pause_stats
          ? `${inv.pause_stats.received_complete}/${inv.pause_stats.total_lines}`
          : undefined,
        csvPath: inv.csv_path,
        currentSessionId: inv.current_session_id,
        pausedAt: inv.paused_at,
        processedAt: (inv as any).processed_at,
        note: (inv as any).note,
        pauseStats: inv.pause_stats,
      }))
      .sort((a, b) => {
        // in_progress first, then new, then processed
        const order: Record<string, number> = { in_progress: 0, new: 1, processed: 2 };
        return (order[a.status] ?? 9) - (order[b.status] ?? 9);
      });
  }

  // Load invoices for selected supplier
  useEffect(() => {
    async function loadInvoices() {
      try {
        setLoading(true);
        setError(null);
        const data = await getInvoicesIndex(supplier);
        setInvoices(buildDisplayInvoices(data.items || []));
      } catch (err) {
        setError('Nepodarilo sa načítať faktúry');
      } finally {
        setLoading(false);
      }
    }
    loadInvoices();
  }, [supplier]);

  const counts: Record<FilterTab, number> = {
    new: invoices.filter(i => i.status === 'new').length,
    in_progress: invoices.filter(i => i.status === 'in_progress').length,
    processed: invoices.filter(i => i.status === 'processed').length,
  };

  const visibleInvoices = invoices.filter(i => i.status === activeTab);

  const handleStartReceiving = async (invoice: InvoiceDisplay) => {
    if (invoice.status === 'in_progress') {
      setResuming(true);
    } else {
      setCreating(true);
    }
    setSelectedInvoice(invoice.id);
    
    try {
      if (invoice.status === 'in_progress' && invoice.currentSessionId) {
        const session = await resumeReceiving(invoice.currentSessionId);
        navigate(`/receiving/${invoice.number}`, {
          state: { 
            sessionId: invoice.currentSessionId,
            supplier,
            lines: session.lines,
            stats: session.stats,
            isResumed: true,
          }
        });
      } else {
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

  const handleRefreshList = async () => {
    setLoading(true);
    setError(null);
    setSuccessMessage(null);
    try {
      const data = await getInvoicesIndex(supplier);
      setInvoices(buildDisplayInvoices(data.items || []));
    } catch (err) {
      setError('Nepodarilo sa obnoviť zoznam');
    } finally {
      setLoading(false);
    }
  };

  const handleDownloadFromWeb = async () => {
    setRefreshing(true);
    setRefreshResult(null);
    setError(null);
    setSuccessMessage(null);
    try {
      const result = await refreshInvoices(supplier);
      setRefreshResult(result);
      if (result.ok || result.downloaded > 0) {
        setSuccessMessage(
          result.message ||
          `Stiahnuté: ${result.downloaded}, preskočené: ${result.skipped}` +
          (result.failed ? `, chyby: ${result.failed}` : '')
        );
        await handleRefreshList();
      } else {
        const msg = result.errors?.join('\n') || 'Nepodarilo sa stiahnuť faktúry';
        setError(msg);
      }
    } catch (err: any) {
      setError(err?.message || 'Chyba pri sťahovaní faktúr');
    } finally {
      setRefreshing(false);
    }
  };

  const handleUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setUploading(true);
    setError(null);
    try {
      await uploadInvoice(supplier, file);
      setSuccessMessage(`Faktúra ${file.name} bola nahraná`);
      await handleRefreshList();
    } catch (err: any) {
      setError(err?.message || 'Nepodarilo sa nahrať faktúru');
    } finally {
      setUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = '';
    }
  };

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h1
          className="text-2xl font-semibold"
          style={{
            fontFamily: 'var(--font-display)',
            color: 'var(--color-text-primary)',
          }}
        >
          Príjem tovaru
        </h1>
        <div className="flex gap-2">
          <Button
            variant="secondary"
            size="sm"
            onClick={handleRefreshList}
            disabled={loading}
          >
            <RefreshCw size={14} className={loading ? 'animate-spin' : ''} />
            Obnoviť
          </Button>
          <Button
            variant="secondary"
            size="sm"
            onClick={handleDownloadFromWeb}
            disabled={refreshing}
          >
            <Download size={14} className={refreshing ? 'animate-spin' : ''} />
            {refreshing ? 'Sťahujem...' : 'Stiahnuť z webu'}
          </Button>
          <input
            ref={fileInputRef}
            type="file"
            accept=".csv,.xlsx,.xls"
            onChange={handleUpload}
            className="hidden"
          />
          <Button
            variant="secondary"
            size="sm"
            onClick={() => fileInputRef.current?.click()}
            disabled={uploading}
          >
            <Upload size={14} />
            {uploading ? 'Nahrávam...' : 'Nahrať faktúru'}
          </Button>
        </div>
      </div>

      {/* Success message */}
      {successMessage && (
        <div
          className="p-4 rounded-lg border flex items-start gap-3"
          style={{
            backgroundColor: 'var(--color-success-subtle)',
            borderColor: 'var(--color-success)',
            color: 'var(--color-success)',
          }}
        >
          <CheckCircle size={20} className="flex-shrink-0 mt-0.5" />
          <div className="flex-1">
            <pre className="whitespace-pre-wrap text-sm font-sans">{successMessage}</pre>
            {refreshResult?.log_files && refreshResult.log_files.length > 0 && (
              <div className="mt-2 flex flex-wrap gap-2">
                {refreshResult.log_files.map((logPath, i) => (
                  <a
                    key={i}
                    href={`${API_BASE}/files/download?relpath=${encodeURIComponent(logPath)}`}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex items-center gap-1 text-xs underline hover:no-underline"
                    style={{ color: 'inherit' }}
                  >
                    <ExternalLink size={12} />
                    Zobraziť log
                  </a>
                ))}
              </div>
            )}
          </div>
          <button onClick={() => setSuccessMessage(null)} className="p-1 hover:opacity-70">
            <XCircle size={16} />
          </button>
        </div>
      )}

      {/* Error */}
      {error && (
        <div
          className="p-4 rounded-lg border flex items-start gap-3"
          style={{
            backgroundColor: 'var(--color-error-subtle)',
            borderColor: 'var(--color-error)',
            color: 'var(--color-error)',
          }}
        >
          <AlertCircle size={20} className="flex-shrink-0 mt-0.5" />
          <div className="flex-1">
            <pre className="whitespace-pre-wrap text-sm font-sans">{error}</pre>
            {refreshResult?.log_files && refreshResult.log_files.length > 0 && (
              <div className="mt-2 flex flex-wrap gap-2">
                {refreshResult.log_files.map((logPath, i) => (
                  <a
                    key={i}
                    href={`${API_BASE}/files/download?relpath=${encodeURIComponent(logPath)}`}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex items-center gap-1 text-xs underline hover:no-underline"
                    style={{ color: 'inherit' }}
                  >
                    <ExternalLink size={12} />
                    Zobraziť log
                  </a>
                ))}
              </div>
            )}
          </div>
          <button
            onClick={() => { setError(null); setRefreshResult(null); }}
            className="p-1 hover:opacity-70"
          >
            <XCircle size={16} />
          </button>
        </div>
      )}

      {/* Supplier Select */}
      <div className="flex items-center gap-4">
        <label className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>
          Dodávateľ:
        </label>
        <select
          value={supplier}
          onChange={(e) => {
            setSupplier(e.target.value);
            setSelectedInvoice(null);
            setError(null);
            setSuccessMessage(null);
            setRefreshResult(null);
          }}
          className="w-48"
        >
          {suppliers.map(s => (
            <option key={s.code} value={s.code}>{s.name}</option>
          ))}
          {suppliers.length === 0 && <option value="paul-lange">Paul-Lange</option>}
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
        {/* Filter Tabs */}
        <div
          className="flex border-b"
          style={{ borderColor: 'var(--color-border-subtle)' }}
        >
          {(['new', 'in_progress', 'processed'] as FilterTab[]).map(tab => (
            <button
              key={tab}
              onClick={() => setActiveTab(tab)}
              className="px-5 py-3 text-sm font-medium transition-colors relative"
              style={{
                color: activeTab === tab ? 'var(--color-accent)' : 'var(--color-text-secondary)',
                borderBottom: activeTab === tab ? '2px solid var(--color-accent)' : '2px solid transparent',
                marginBottom: -1,
              }}
            >
              {TAB_LABELS[tab]}
              {counts[tab] > 0 && (
                <span
                  className="ml-2 text-xs px-1.5 py-0.5 rounded-full"
                  style={{
                    backgroundColor: tab === 'in_progress' 
                      ? 'var(--color-warning-subtle)' 
                      : tab === 'processed'
                      ? 'var(--color-success-subtle)'
                      : 'var(--color-bg-tertiary)',
                    color: tab === 'in_progress'
                      ? 'var(--color-warning)'
                      : tab === 'processed'
                      ? 'var(--color-success)'
                      : 'var(--color-text-tertiary)',
                  }}
                >
                  {counts[tab]}
                </span>
              )}
            </button>
          ))}
        </div>

        {loading ? (
          <div className="p-8 flex items-center justify-center">
            <Loader2
              className="animate-spin"
              size={24}
              style={{ color: 'var(--color-accent)' }}
            />
          </div>
        ) : visibleInvoices.length === 0 ? (
          <div className="p-8 text-center">
            <p style={{ color: 'var(--color-text-tertiary)' }}>
              {activeTab === 'new'
                ? 'Žiadne nové faktúry'
                : activeTab === 'in_progress'
                ? 'Žiadne prebiehajúce príjmy'
                : 'Žiadne dokončené faktúry'}
            </p>
            {activeTab === 'new' && (
              <p className="text-sm mt-2" style={{ color: 'var(--color-text-tertiary)' }}>
                Skúste stiahnuť faktúry z webu alebo nahrať manuálne
              </p>
            )}
          </div>
        ) : (
          <div className="divide-y" style={{ borderColor: 'var(--color-border-subtle)' }}>
            {visibleInvoices.map((inv) => {
              const isSelected = selectedInvoice === inv.id;
              const isInProgress = inv.status === 'in_progress';
              const isProcessed = inv.status === 'processed';

              return (
                <div
                  key={inv.id}
                  className={`p-4 cursor-pointer transition-colors ${isSelected ? 'ring-2 ring-inset' : ''}`}
                  style={{
                    backgroundColor: isSelected ? 'var(--color-accent-subtle)' : 'transparent',
                  }}
                  onClick={() => setSelectedInvoice(isSelected ? null : inv.id)}
                  onMouseEnter={(e) => {
                    if (!isSelected) e.currentTarget.style.backgroundColor = 'var(--color-bg-tertiary)';
                  }}
                  onMouseLeave={(e) => {
                    if (!isSelected) e.currentTarget.style.backgroundColor = 'transparent';
                  }}
                >
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-4">
                      <div>
                        <div className="font-medium" style={{ color: 'var(--color-text-primary)' }}>
                          {inv.number}
                        </div>
                        <div className="text-sm" style={{ color: 'var(--color-text-tertiary)' }}>
                          {inv.date}
                        </div>
                        {/* Poznámka */}
                        {inv.note && (
                          <div
                            className="text-xs mt-1 italic"
                            style={{ color: 'var(--color-text-tertiary)' }}
                          >
                            📝 {inv.note}
                          </div>
                        )}
                      </div>

                      {isInProgress && (
                        <span
                          className="text-xs px-2 py-0.5 rounded-full font-medium"
                          style={{
                            backgroundColor: 'var(--color-warning-subtle)',
                            color: 'var(--color-warning)',
                          }}
                        >
                          Prebieha {inv.progress}
                        </span>
                      )}
                      {isProcessed && (
                        <span
                          className="text-xs px-2 py-0.5 rounded-full font-medium"
                          style={{
                            backgroundColor: 'var(--color-success-subtle)',
                            color: 'var(--color-success)',
                          }}
                        >
                          Dokončené
                        </span>
                      )}
                    </div>

                    <div className="flex items-center gap-3">
                      {isInProgress && inv.pausedAt && (
                        <span className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                          Pozastavené {formatRelativeTime(inv.pausedAt)}
                        </span>
                      )}
                      {isProcessed && inv.processedAt && (
                        <span className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                          {formatRelativeTime(inv.processedAt)}
                        </span>
                      )}

                      {isProcessed ? (
                        <Button
                          variant="secondary"
                          size="sm"
                          onClick={(e) => {
                            e.stopPropagation();
                            setModalInvoice(inv);
                          }}
                        >
                          <FileText size={14} />
                          Zobraziť výsledky
                        </Button>
                      ) : (
                        <Button
                          variant={isInProgress ? 'primary' : 'secondary'}
                          size="sm"
                          onClick={(e) => {
                            e.stopPropagation();
                            handleStartReceiving(inv);
                          }}
                          disabled={creating || resuming}
                        >
                          {isInProgress ? (
                            <>
                              <PlayCircle size={14} />
                              {resuming && selectedInvoice === inv.id ? 'Načítavam...' : 'Pokračovať'}
                            </>
                          ) : (
                            <>
                              <Play size={14} />
                              {creating && selectedInvoice === inv.id ? 'Spúšťam...' : 'Spustiť príjem'}
                            </>
                          )}
                        </Button>
                      )}
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* Modal pre dokončené faktúry */}
      {modalInvoice && (
        <ReceivingResultsModal
          supplier={supplier}
          invoiceId={modalInvoice.id}
          onClose={() => setModalInvoice(null)}
        />
      )}
    </div>
  );
}

export default ReceivingPage;
