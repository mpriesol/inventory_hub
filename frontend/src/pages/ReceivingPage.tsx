import React, { useState, useEffect, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import { 
  Check, AlertCircle, Loader2, RefreshCw, Play, PlayCircle, 
  Upload, Download, ExternalLink, XCircle, CheckCircle 
} from 'lucide-react';
import { Button } from '../components/ui/Button.new';
import { StatusBadge } from '../components/ui/Badge.new';
import { getInvoicesIndex, refreshInvoices, type InvoiceIndexItem } from '../api/invoices';
import { listSuppliers, uploadInvoice, type SupplierSummary } from '../api/suppliers';
import { createReceivingSession, resumeReceiving } from '../api/receiving';
import { API_BASE } from '../api/client';

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
  const [suppliers, setSuppliers] = useState<SupplierSummary[]>([]);
  const [supplier, setSupplier] = useState('paul-lange');
  const [invoices, setInvoices] = useState<InvoiceDisplay[]>([]);
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [resuming, setResuming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  
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

  // Load invoices for selected supplier
  useEffect(() => {
    async function loadInvoices() {
      try {
        setLoading(true);
        setError(null);
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
      } catch (err) {
        setError('Nepodarilo sa načítať faktúry');
      } finally {
        setLoading(false);
      }
    }
    loadInvoices();
  }, [supplier]);

  const pendingCount = invoices.filter(inv => inv.status === 'new').length;
  const inProgressCount = invoices.filter(inv => inv.status === 'in_progress').length;

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

  // Refresh invoice list from local index
  const handleRefreshList = async () => {
    setLoading(true);
    setError(null);
    setSuccessMessage(null);
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
    } catch (err) {
      setError('Nepodarilo sa obnoviť zoznam');
    } finally {
      setLoading(false);
    }
  };

  // Download invoices from supplier website (Playwright/web scraping)
  const handleDownloadFromWeb = async () => {
    setRefreshing(true);
    setRefreshResult(null);
    setError(null);
    setSuccessMessage(null);
    
    try {
      const result = await refreshInvoices(supplier) as RefreshResult;
      setRefreshResult(result);
      
      // Check if ok is explicitly false or if there are errors
      const hasErrors = result.ok === false || (result.errors && result.errors.length > 0) || result.failed > 0;
      
      if (hasErrors) {
        // Build error message
        const parts: string[] = [];
        parts.push(`Stiahnuté: ${result.downloaded || 0}, Preskočené: ${result.skipped || 0}, Zlyhané: ${result.failed || 0}`);
        
        if (result.errors && result.errors.length > 0) {
          // Show first error, truncated if too long
          const firstError = result.errors[0];
          const shortError = firstError.length > 200 ? firstError.substring(0, 200) + '...' : firstError;
          parts.push(shortError);
        }
        
        setError(parts.join('\n'));
      } else {
        // Success
        setSuccessMessage(`Stiahnuté: ${result.downloaded || 0}, Preskočené: ${result.skipped || 0}`);
      }
      
      // Reload invoice list regardless of result
      await handleRefreshList();
      
    } catch (err: any) {
      // HTTP-level error
      setError(`Zlyhalo sťahovanie: ${err.message || String(err)}`);
      setRefreshResult({ ok: false, downloaded: 0, skipped: 0, failed: 0, errors: [String(err)] });
    } finally {
      setRefreshing(false);
    }
  };

  // Manual file upload
  const handleUploadClick = () => {
    fileInputRef.current?.click();
  };

  const handleFileChange = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    
    setUploading(true);
    setError(null);
    setSuccessMessage(null);
    
    try {
      await uploadInvoice(supplier, file);
      setSuccessMessage(`Faktúra "${file.name}" bola úspešne nahratá`);
      // Reload invoice list
      await handleRefreshList();
    } catch (err: any) {
      setError(`Zlyhalo nahrávanie: ${err.message || String(err)}`);
    } finally {
      setUploading(false);
      // Reset file input
      if (fileInputRef.current) {
        fileInputRef.current.value = '';
      }
    }
  };

  return (
    <div className="space-y-6">
      {/* Hidden file input */}
      <input
        ref={fileInputRef}
        type="file"
        accept=".csv,.xlsx,.xls,.pdf"
        onChange={handleFileChange}
        className="hidden"
      />

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
        <div className="flex items-center gap-2">
          <Button 
            variant="secondary" 
            onClick={handleUploadClick} 
            disabled={uploading}
          >
            <Upload size={16} className={uploading ? 'animate-pulse' : ''} />
            {uploading ? 'Nahrávam...' : 'Nahrať faktúru'}
          </Button>
          <Button 
            variant="secondary" 
            onClick={handleDownloadFromWeb} 
            disabled={refreshing}
          >
            <Download size={16} className={refreshing ? 'animate-spin' : ''} />
            {refreshing ? 'Sťahujem...' : 'Stiahnuť z webu'}
          </Button>
          <Button variant="ghost" onClick={handleRefreshList} disabled={loading}>
            <RefreshCw size={16} className={loading ? 'animate-spin' : ''} />
          </Button>
        </div>
      </div>

      {/* Success Message */}
      {successMessage && (
        <div
          className="p-4 rounded-lg border flex items-start gap-3"
          style={{
            backgroundColor: 'rgba(34, 197, 94, 0.1)',
            borderColor: 'rgb(34, 197, 94)',
            color: 'rgb(34, 197, 94)',
          }}
        >
          <CheckCircle size={20} className="flex-shrink-0 mt-0.5" />
          <div className="flex-1">
            <span>{successMessage}</span>
            {/* Log file links on success */}
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
            onClick={() => setSuccessMessage(null)}
            className="p-1 hover:opacity-70"
          >
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
            {/* Log file links even on error */}
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
            setError(null);
            setSuccessMessage(null);
            setRefreshResult(null);
          }}
          className="w-48"
        >
          {suppliers.map(s => (
            <option key={s.code} value={s.code}>
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
            {inProgressCount > 0 && (
              <span className="ml-2 text-yellow-500">
                + {inProgressCount} rozpracované
              </span>
            )}
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
        ) : invoices.length === 0 ? (
          <div className="p-8 text-center">
            <p style={{ color: 'var(--color-text-tertiary)' }}>
              Žiadne čakajúce faktúry
            </p>
            <p className="text-sm mt-2" style={{ color: 'var(--color-text-tertiary)' }}>
              Skúste stiahnuť faktúry z webu alebo nahrať manuálne
            </p>
          </div>
        ) : (
          <div
            className="divide-y"
            style={{ borderColor: 'var(--color-border-subtle)' }}
          >
            {invoices.map((inv) => {
              const isSelected = selectedInvoice === inv.id;
              const isInProgress = inv.status === 'in_progress';
              
              return (
                <div
                  key={inv.id}
                  className={`p-4 cursor-pointer transition-colors ${isSelected ? 'ring-2 ring-inset' : ''}`}
                  style={{
                    backgroundColor: isSelected 
                      ? 'var(--color-accent-subtle)' 
                      : 'transparent',
                    ringColor: isSelected ? 'var(--color-accent)' : 'transparent',
                  }}
                  onClick={() => setSelectedInvoice(inv.id)}
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
                    <div className="flex items-center gap-4">
                      <div>
                        <div 
                          className="font-medium"
                          style={{ color: 'var(--color-text-primary)' }}
                        >
                          {inv.number}
                        </div>
                        <div 
                          className="text-sm"
                          style={{ color: 'var(--color-text-tertiary)' }}
                        >
                          {inv.date}
                        </div>
                      </div>
                      
                      {isInProgress && (
                        <StatusBadge variant="warning">
                          Rozpracované {inv.progress}
                        </StatusBadge>
                      )}
                    </div>
                    
                    <div className="flex items-center gap-3">
                      {isInProgress && inv.pausedAt && (
                        <span 
                          className="text-xs"
                          style={{ color: 'var(--color-text-tertiary)' }}
                        >
                          Pozastavené {formatRelativeTime(inv.pausedAt)}
                        </span>
                      )}
                      
                      <Button
                        variant={isInProgress ? "primary" : "secondary"}
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
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}

export default ReceivingPage;
