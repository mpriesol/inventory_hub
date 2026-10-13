import React, { useState, useRef, useEffect } from 'react';
import { useParams, useNavigate, useLocation } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import {
  ArrowLeft, Camera, Check, X, List, AlertTriangle,
  Edit2, CheckCircle, RotateCcw, MessageSquare, Loader2, ChevronUp, ChevronDown
} from 'lucide-react';
import { Button } from '../components/ui/Button.new';
import { ActionScope } from '../components/ui/ActionScope';
import { useReceivingScanner } from '../hooks/useReceivingScanner';
import {
  getReceivingSummary,
  finalizeReceiving,
  pauseReceiving,
  setLineQuantity,
  acceptAllItems,
  resetAllItems,
  type ReceivingLine,
  type ReceivingSummary,
  type ReceivingStatus,
  type FinalizeResult,
} from '../api/receiving';
import { ReceivingResultsModal } from '../components/ReceivingResultsModal';
import { API_BASE } from '../api/client';

function getRawScm(line: ReceivingLine): string {
  return (line.scm || (line.product_code || '').replace(/^PL-/i, '')).trim();
}

export function ReceivingSessionPage() {
  const { t } = useTranslation();
  const { invoiceId } = useParams();
  const navigate = useNavigate();
  const location = useLocation();
  const inputRef = useRef<HTMLInputElement>(null);
  const resultsTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const state = location.state as { sessionId?: string; supplier?: string; lines?: ReceivingLine[] } | null;
  const sessionId = state?.sessionId || '';
  const supplier  = state?.supplier  || 'paul-lange';

  const receiptKey = `${supplier}:${sessionId}`;
  const receiptContext = useRef({ key: receiptKey, revision: 0 });
  if (receiptContext.current.key !== receiptKey) receiptContext.current = { key: receiptKey, revision: 0 };
  const [summaryReady, setSummaryReady] = useState(false);
  const [sessionState, setSessionState] = useState<{ key: string; status: ReceivingStatus } | null>(null);
  const sessionStatus = sessionState?.key === receiptKey ? sessionState.status : null;
  const canEdit = summaryReady && (sessionStatus === 'new' || sessionStatus === 'in_progress');
  const inactive = sessionStatus === 'completed' || sessionStatus === 'cancelled' || sessionStatus === 'paused';

  const [scannedCode,    setScannedCode]    = useState('');
  const [quantity,       setQuantity]       = useState(1);
  const [lastScan,       setLastScan]       = useState<{ code: string; product: string; sku: string; received: number; expected: number; status: string } | null>(null);
  const [stats,          setStats]          = useState<ReceivingSummary>({ matched: 0, partial: 0, pending: state?.lines?.length || 0, overage: 0, unexpected: 0 });
  const [lines,          setLines]          = useState<ReceivingLine[]>(state?.lines || []);
  const [finalizing,     setFinalizing]     = useState(false);
  const [error,          setError]          = useState<string | null>(null);
  const [showConfirm,    setShowConfirm]    = useState(false);
  const [finalizeResult, setFinalizeResult] = useState<FinalizeResult | null>(null);
  const [showCsvModal,   setShowCsvModal]   = useState(false);
  const [showLines,      setShowLines]      = useState(true);
  const [bulkLoading,    setBulkLoading]    = useState(false);
  const [doneItems,      setDoneItems]      = useState<Record<string, boolean>>({});
  const [invoiceNote,    setInvoiceNote]    = useState('');
  const [savingNote,     setSavingNote]     = useState(false);
  const [editingLine,    setEditingLine]    = useState<{ index: number; line: ReceivingLine } | null>(null);
  const [editQty,        setEditQty]        = useState('');
  const [editNote,       setEditNote]       = useState('');
  const [savingEdit,     setSavingEdit]     = useState(false);
  const [quantityNeedsRefresh, setQuantityNeedsRefresh] = useState(false);
  const [pausing,        setPausing]        = useState(false);

  const total    = stats.matched + stats.partial + stats.pending;
  const progress = total > 0 ? Math.round((stats.matched / total) * 100) : 0;

  useEffect(() => {
    inputRef.current?.focus();
    if (!sessionId && !state?.lines) navigate('/receiving');
  }, [sessionId, navigate, state]);

  useEffect(() => {
    const context = receiptContext.current;
    const revision = context.revision;
    let active = true;
    if (resultsTimer.current) clearTimeout(resultsTimer.current);
    setLines(state?.lines || []);
    setStats({ matched: 0, partial: 0, pending: state?.lines?.length || 0, overage: 0, unexpected: 0 });
    setSummaryReady(false); setSessionState(null); setLastScan(null); setError(null); setEditingLine(null);
    setShowConfirm(false); setFinalizeResult(null); setShowCsvModal(false); setDoneItems({}); setShowLines(true);
    mutation.current = false; setSavingEdit(false); setQuantityNeedsRefresh(false);
    scanInput.current = ''; setScannedCode(''); setQuantity(1);
    if (sessionId) getReceivingSummary(supplier, sessionId).then(data => {
      if (active && receiptContext.current === context && context.revision === revision) {
        setLines(data.lines); setStats(data.summary); setSessionState({ key: receiptKey, status: data.status }); setSummaryReady(true);
      }
    }).catch(() => {
      if (active && receiptContext.current === context && context.revision === revision) setError(t('actions.receiving.summaryFailed'));
    });
    return () => { active = false; if (resultsTimer.current) clearTimeout(resultsTimer.current); };
  }, [sessionId, supplier]);

  useEffect(() => {
    let active = true;
    setInvoiceNote('');
    if (invoiceId && supplier) fetch(`${API_BASE}/suppliers/${supplier}/invoices/${invoiceId}/note`)
      .then(r => r.ok ? r.json() : null)
      .then(data => { if (active && data?.note) setInvoiceNote(data.note); })
      .catch(() => {});
    return () => { active = false; };
  }, [invoiceId, supplier]);

  const isLineDone = (line: ReceivingLine): boolean => {
    if (line.received_qty >= line.ordered_qty && line.received_qty > 0) return true;
    return doneItems[getRawScm(line)] === true;
  };

  const toggleDone = (line: ReceivingLine) => {
    if (!canEdit) return;
    const scm = getRawScm(line);
    setDoneItems(prev => ({ ...prev, [scm]: !prev[scm] }));
  };

  const saveNote = async (note: string) => {
    if (!invoiceId || !canEdit) return;
    setSavingNote(true);
    try {
      await fetch(`${API_BASE}/suppliers/${supplier}/invoices/${invoiceId}/note`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ note }),
      });
    } catch (e) { console.error('Failed to save note:', e); } finally { setSavingNote(false); }
  };

  const saveReceivedItems = async () => {
    if (!invoiceId) return;
    const items: Record<string, { qty: number; done: boolean }> = {};
    lines.forEach(line => {
      const scm = getRawScm(line);
      if (scm) items[scm] = { qty: line.received_qty, done: isLineDone(line) };
    });
    try {
      await fetch(`${API_BASE}/suppliers/${supplier}/invoices/${invoiceId}/received-items`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ items }),
      });
    } catch (e) { console.error('Failed to save received items:', e); }
  };

  const scanInput = useRef('');
  const mutation = useRef(false);
  const scanner = useReceivingScanner(supplier, sessionId, async (result, code) => {
    const context = receiptContext.current;
    context.revision += 1;
    setLastScan(result.line
      ? { code, product: result.line.title || 'Neznámy produkt', sku: result.line.product_code || result.line.scm || '', received: result.line.received_qty, expected: result.line.ordered_qty, status: result.status }
      : { code, product: 'Nenájdené na faktúre', sku: '', received: 0, expected: 0, status: result.status });
    if (result.replayed) {
      // The replay response is its original snapshot. Keep this queue item
      // pending until the current local receipt is read successfully.
      const current = await getReceivingSummary(supplier, sessionId);
      if (receiptContext.current !== context) return;
      setLines(current.lines); setStats(current.summary); setSessionState({ key: receiptKey, status: current.status }); setSummaryReady(true);
    } else {
      setStats(result.summary);
      if (result.line) setLines(previous => previous.map(line => {
        const matched = result.line!.id !== undefined ? line.id === result.line!.id
          : !!result.line!.scm && line.scm === result.line!.scm;
        return matched ? { ...line, ...result.line } : line;
      }));
    }
  }, kind => setError(t(`actions.receiving.${kind}`)));
  const loading = scanner.busy || scanner.pending > 0;
  const handleScan = () => {
    const code = scanInput.current.trim();
    if (!code || !sessionId || !canEdit || mutation.current || finalizing || finalizeResult || pausing) return;
    if (!Number.isFinite(quantity) || quantity <= 0 || quantity > 999999999.999 || Math.abs(quantity * 1000 - Math.round(quantity * 1000)) > 0.000001) {
      setError(t('actions.receiving.invalidQuantity')); return;
    }
    receiptContext.current.revision += 1;
    // Consume the input synchronously: a double click cannot queue it twice.
    // A separately entered identical code is always a new physical scan.
    scanInput.current = ''; setScannedCode(''); setQuantity(1);
    if (!scanner.uncertain) setError(null);
    scanner.enqueue(code, quantity); inputRef.current?.focus();
  };

  const doFinalize = async (force = false) => {
    if (!sessionId || !canEdit || mutation.current || scanner.hasPending()) return;
    const context = receiptContext.current;
    mutation.current = true; context.revision += 1;
    setFinalizing(true); setError(null); setShowConfirm(false);
    try {
      if (invoiceNote.trim()) await saveNote(invoiceNote.trim());
      await saveReceivedItems();
      const result = await finalizeReceiving(supplier, sessionId, force);
      if (receiptContext.current !== context) return;
      setSessionState({ key: receiptKey, status: 'completed' });
      setFinalizeResult(result);
      if (supplier === 'paul-lange') {
        resultsTimer.current = setTimeout(() => { if (receiptContext.current === context) setShowCsvModal(true); }, 2000);
      }
    } catch (error) {
      setError(error instanceof Error && error.message.includes('stock_publication_warehouse_held')
        ? t('stockPublication.errors.stock_publication_warehouse_held')
        : 'Nepodarilo sa dokončiť príjem. Skúste to znova.');
      setFinalizing(false);
    } finally { mutation.current = false; }
  };

  const handleExit = async () => {
    if (mutation.current || scanner.busy || (canEdit && scanner.hasPending()) || pausing || bulkLoading || savingEdit || (finalizing && !finalizeResult)) return;
    if (!sessionId || inactive || finalizeResult || !summaryReady) {
      navigate('/receiving');
      return;
    }
    const context = receiptContext.current;
    mutation.current = true; setPausing(true); setError(null);
    try {
      // Another tab may already have completed or paused this receipt. Leaving
      // a historical session must not write notes/items or attempt to pause it.
      const current = await getReceivingSummary(supplier, sessionId);
      if (receiptContext.current !== context) return;
      setSessionState({ key: receiptKey, status: current.status });
      if (current.status !== 'new' && current.status !== 'in_progress') {
        navigate('/receiving'); return;
      }
      if (invoiceNote.trim()) await saveNote(invoiceNote.trim());
      await saveReceivedItems();
      await pauseReceiving(supplier, sessionId);
      if (receiptContext.current === context) navigate('/receiving');
    } catch {
      // A response can be lost after pausing, or completion can win the race
      // after the preflight read. Only a confirmed inactive status permits exit.
      try {
        const current = await getReceivingSummary(supplier, sessionId);
        if (receiptContext.current !== context) return;
        setSessionState({ key: receiptKey, status: current.status });
        if (current.status !== 'new' && current.status !== 'in_progress') {
          navigate('/receiving'); return;
        }
      } catch { /* Preserve the active draft and report the pause failure. */ }
      if (receiptContext.current === context) setError(t('receiving.pauseError'));
    } finally {
      mutation.current = false; setPausing(false);
    }
  };

  const openEditModal = (index: number, line: ReceivingLine) => { if (!canEdit || scanner.hasPending() || mutation.current) return; setEditingLine({ index, line }); setEditQty(line.received_qty.toString()); setEditNote(''); };

  const refreshQuantities = async () => {
    if (!sessionId || mutation.current || scanner.hasPending()) return;
    const context = receiptContext.current;
    mutation.current = true; setSavingEdit(true);
    try {
      const current = await getReceivingSummary(supplier, sessionId);
      if (receiptContext.current !== context) return;
      setLines(current.lines); setStats(current.summary); setSessionState({ key: receiptKey, status: current.status });
      setSummaryReady(true); setQuantityNeedsRefresh(false); setError(null);
    } catch {
      if (receiptContext.current === context) setError(t('receiving.quantityRefreshRequired'));
    } finally {
      if (receiptContext.current === context) { mutation.current = false; setSavingEdit(false); }
    }
  };

  const saveLineQuantity = async (index: number, nextQty: number, note?: string) => {
    if (!sessionId || !canEdit || mutation.current || scanner.hasPending()) return;
    if (!Number.isFinite(nextQty) || nextQty < 0 || nextQty > 999999999.999 || Math.abs(nextQty * 1000 - Math.round(nextQty * 1000)) > 0.000001) {
      setError(t('receiving.invalidManualQuantity')); return;
    }
    const context = receiptContext.current;
    mutation.current = true; context.revision += 1;
    setSavingEdit(true); setError(null);
    try {
      const result = await setLineQuantity(supplier, sessionId, index, nextQty, note);
      if (receiptContext.current !== context) return;
      if (lines[index]?.id !== undefined && result.line.id !== lines[index].id) throw new Error('receiving_quantity_line_mismatch');
      setLines(prev => prev.map((line, i) => i === index ? { ...line, ...result.line } : line));
      setStats(result.summary); setEditingLine(null);
    } catch {
      if (receiptContext.current !== context) return;
      // An absolute quantity may already have been committed when its response
      // is lost. Read the saved draft before another click computes its target.
      setSummaryReady(false);
      try {
        const current = await getReceivingSummary(supplier, sessionId);
        if (receiptContext.current !== context) return;
        setLines(current.lines); setStats(current.summary); setSessionState({ key: receiptKey, status: current.status });
        setSummaryReady(true); setEditingLine(null); setError(t('receiving.quantityReconciled'));
      } catch {
        if (receiptContext.current === context) {
          setQuantityNeedsRefresh(true); setError(t('receiving.quantityRefreshRequired'));
        }
      }
    } finally {
      if (receiptContext.current === context) { mutation.current = false; setSavingEdit(false); }
    }
  };

  const handleSaveEdit = () => {
    if (!editingLine) return;
    void saveLineQuantity(editingLine.index, editQty.trim() === '' ? NaN : Number(editQty), editNote || undefined);
  };

  const adjustLineQuantity = (index: number, delta: number) => {
    const line = lines[index];
    if (!line) return;
    void saveLineQuantity(index, Math.max(0, Math.round((line.received_qty + delta) * 1000) / 1000));
  };

  const handleAcceptAll = async () => {
    if (!sessionId || !canEdit || mutation.current || scanner.hasPending()) return; mutation.current = true; receiptContext.current.revision += 1; setBulkLoading(true);
    try { const r = await acceptAllItems(supplier, sessionId, true); setLines(r.lines); setStats(r.summary); }
    catch { setError('Nepodarilo sa prijať všetky položky'); } finally { mutation.current = false; setBulkLoading(false); }
  };

  const handleResetAll = async () => {
    if (!sessionId || !canEdit || mutation.current || scanner.hasPending() || !confirm('Naozaj chcete vynulovať všetky prijaté množstvá?')) return;
    mutation.current = true; receiptContext.current.revision += 1; setBulkLoading(true);
    try { const r = await resetAllItems(supplier, sessionId); setLines(r.lines); setStats(r.summary); }
    catch { setError('Nepodarilo sa vynulovať množstvá'); } finally { mutation.current = false; setBulkLoading(false); }
  };

  const sc: Record<string, { bg: string; border: string; icon: string; label: string }> = {
    matched:    { bg: 'var(--color-success-subtle)', border: 'var(--color-success)', icon: '✓', label: 'Nájdené' },
    partial:    { bg: 'var(--color-info-subtle)',    border: 'var(--color-info)',    icon: '◐', label: 'Čiastočne' },
    overage:    { bg: 'var(--color-warning-subtle)', border: 'var(--color-warning)', icon: '!', label: 'Prebytok' },
    unexpected: { bg: 'var(--color-error-subtle)',   border: 'var(--color-error)',   icon: '?', label: 'Nenájdené' },
    unknown:    { bg: 'var(--color-error-subtle)',   border: 'var(--color-error)',   icon: '?', label: 'Neznáme' },
    pending:    { bg: 'var(--color-bg-tertiary)',    border: 'var(--color-border-subtle)', icon: '○', label: 'Čaká' },
  };

  const exitDisabled = pausing || scanner.busy || (canEdit && scanner.pending > 0) || bulkLoading || savingEdit || (finalizing && !finalizeResult);

  return (
    <div className="space-y-6">

      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-4">
          <button onClick={handleExit} data-testid="receiving-back" aria-label={t('receiving.backToList')} disabled={exitDisabled} className="p-2 rounded-lg transition-colors"
            style={{ color: 'var(--color-text-secondary)' }}
            onMouseEnter={e => { e.currentTarget.style.backgroundColor = 'var(--color-bg-secondary)'; e.currentTarget.style.color = 'var(--color-text-primary)'; }}
            onMouseLeave={e => { e.currentTarget.style.backgroundColor = 'transparent'; e.currentTarget.style.color = 'var(--color-text-secondary)'; }}>
            <ArrowLeft size={20} />
          </button>
          <div>
            <h1 className="text-xl font-semibold" style={{ fontFamily: 'var(--font-display)', color: 'var(--color-text-primary)' }}>
              Príjem: <span style={{ fontFamily: 'var(--font-mono)', color: 'var(--color-accent)' }}>{invoiceId}</span>
            </h1>
            {sessionId && <div className="text-xs mt-0.5" style={{ color: 'var(--color-text-tertiary)', fontFamily: 'var(--font-mono)' }}>Session: {sessionId}</div>}
          </div>
        </div>
        <span className="action-control"><Button data-testid="receiving-exit" variant="danger" onClick={handleExit} loading={pausing} disabled={exitDisabled}><X size={16} /> Ukončiť</Button>{canEdit && <ActionScope effects={['hub-write']} />}</span>
      </div>

      {error && <div className="p-4 rounded-lg border" style={{ backgroundColor: 'var(--color-error-subtle)', borderColor: 'var(--color-error)', color: 'var(--color-error)' }}>{error}</div>}
      {quantityNeedsRefresh && <Button data-testid="receiving-quantity-refresh" disabled={savingEdit} onClick={refreshQuantities}>{t('receiving.refreshQuantities')}</Button>}

      {inactive && <div role="status" data-testid="receiving-session-status" className="p-4 rounded-lg border" style={{ borderColor: 'var(--color-border-subtle)', backgroundColor: 'var(--color-bg-secondary)' }}>{t(`receiving.sessionStatus.${sessionStatus}`)}</div>}
      {scanner.pending > 0 && <p role="status" data-testid="receiving-scan-pending">{t(inactive ? 'receiving.closedPendingScans' : 'actions.receiving.pending', { count: scanner.pending })}</p>}
      {scanner.uncertain && <Button data-testid="receiving-scan-retry" disabled={!summaryReady || scanner.busy} onClick={() => { setError(null); scanner.retry(); }}>{t('actions.receiving.retry')}</Button>}

      {/* Scan */}
      {!inactive && <div className="rounded-xl border p-8" style={{ backgroundColor: 'var(--color-bg-secondary)', borderColor: 'var(--color-border-subtle)' }}>
        <div className="max-w-md mx-auto text-center">
          <div className="w-20 h-20 rounded-2xl border-2 border-dashed flex items-center justify-center mx-auto"
            style={{ backgroundColor: 'var(--color-accent-subtle)', borderColor: 'var(--color-border-accent)', color: 'var(--color-accent)' }}>
            <Camera size={32} />
          </div>
          <h2 className="text-lg font-medium mt-4" style={{ color: 'var(--color-text-primary)' }}>Naskenuj čiarový kód</h2>
          <p className="text-sm mt-1" style={{ color: 'var(--color-text-tertiary)' }}>Použi skener alebo zadaj kód manuálne</p>
          <div className="mt-6 flex gap-2">
            <input ref={inputRef} type="text" value={scannedCode} onChange={e => { scanInput.current = e.target.value; setScannedCode(e.target.value); }} onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); handleScan(); } }}
              placeholder="Zadaj EAN, SKU alebo kód produktu..." className="flex-1 py-3" style={{ fontFamily: 'var(--font-mono)' }} data-testid="receiving-scan-code" autoFocus disabled={!canEdit || pausing || bulkLoading || savingEdit || finalizing || !!finalizeResult} />
            <Button data-testid="receiving-scan" variant="primary" onClick={handleScan} disabled={!scannedCode.trim() || !sessionId || !canEdit || pausing || bulkLoading || savingEdit || finalizing || !!finalizeResult}>{t('actions.receiving.scan')}</Button>
          </div>
          <p className="text-sm mt-3">{t('actions.receiving.help')}</p>
          <ActionScope effects={['hub-write']}>{t('actions.receiving.local')}</ActionScope>
          <div className="flex items-center justify-center gap-4 mt-4">
            <label className="flex items-center gap-2 text-sm" style={{ color: 'var(--color-text-secondary)' }}>
              Množstvo:
              <input data-testid="receiving-scan-quantity" disabled={!canEdit || pausing || finalizing} type="number" value={Number.isNaN(quantity) ? '' : quantity} onChange={e => setQuantity(e.target.value === '' ? NaN : Number(e.target.value))} min={0.001} max={999999999.999} step={1} className="w-20 text-center py-1" />
            </label>
          </div>
        </div>
      </div>}

      {/* Last scan */}
      {lastScan && (
        <div className="rounded-xl border p-4" style={{ backgroundColor: sc[lastScan.status]?.bg || sc.unknown.bg, borderColor: sc[lastScan.status]?.border || sc.unknown.border }}>
          <div className="flex items-center gap-3">
            <div className="w-8 h-8 rounded-full flex items-center justify-center"
              style={{ backgroundColor: `${sc[lastScan.status]?.border || sc.unknown.border}33`, color: sc[lastScan.status]?.border || sc.unknown.border }}>
              {sc[lastScan.status]?.icon || '?'}
            </div>
            <div className="flex-1">
              <div className="text-sm font-medium uppercase" style={{ color: sc[lastScan.status]?.border }}>{sc[lastScan.status]?.label}</div>
              <div className="text-sm mt-0.5" style={{ color: 'var(--color-text-primary)' }}>
                <span style={{ fontFamily: 'var(--font-mono)', color: 'var(--color-text-secondary)' }}>{lastScan.code}</span>
                {lastScan.sku && <> → <span style={{ fontFamily: 'var(--font-mono)' }}>{lastScan.sku}</span></>}
                <span className="ml-2" style={{ color: 'var(--color-text-secondary)' }}>"{lastScan.product}"</span>
              </div>
              {lastScan.expected > 0 && <div className="text-xs mt-1" style={{ color: 'var(--color-text-tertiary)' }}>Prijaté: {lastScan.received}/{lastScan.expected}</div>}
            </div>
          </div>
        </div>
      )}

      {/* Progress */}
      <div className="rounded-xl border p-4" style={{ backgroundColor: 'var(--color-bg-secondary)', borderColor: 'var(--color-border-subtle)' }}>
        <div className="flex items-center justify-between mb-3">
          <span className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>Priebeh</span>
          <span className="text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>{progress}% dokončené</span>
        </div>
        <div className="h-2 rounded-full overflow-hidden" style={{ backgroundColor: 'var(--color-bg-primary)' }}>
          <div className="h-full transition-all" style={{ width: `${progress}%`, background: 'linear-gradient(90deg, var(--color-accent), var(--color-accent-hover))' }} />
        </div>
        <div className="flex items-center justify-between mt-4 text-sm">
          <span style={{ color: 'var(--color-text-secondary)' }}><span style={{ color: 'var(--color-success)' }}>✓</span> {stats.matched} Kompletné</span>
          <span style={{ color: 'var(--color-text-secondary)' }}><span style={{ color: 'var(--color-info)' }}>◐</span> {stats.partial} Čiastočné</span>
          <span style={{ color: 'var(--color-text-secondary)' }}><span style={{ color: 'var(--color-text-tertiary)' }}>○</span> {stats.pending} Čakajúce</span>
          <span style={{ color: 'var(--color-text-secondary)' }}><span style={{ color: 'var(--color-error)' }}>!</span> {stats.overage + stats.unexpected} Problémy</span>
        </div>
      </div>

      {/* Lines */}
      {showLines && (
        <div data-testid="receiving-lines" className="rounded-xl border overflow-hidden" style={{ backgroundColor: 'var(--color-bg-secondary)', borderColor: 'var(--color-border-subtle)' }}>
          <div className="px-4 py-3 border-b flex items-center justify-between" style={{ borderColor: 'var(--color-border-subtle)' }}>
            <span className="text-sm font-medium" style={{ color: 'var(--color-text-secondary)' }}>{lines.length} položiek</span>
            {canEdit && <div className="flex gap-2">
              <span className="action-control"><button onClick={handleAcceptAll} disabled={loading || savingEdit || bulkLoading || pausing || stats.pending === 0}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium disabled:opacity-50"
                style={{ backgroundColor: 'var(--color-success-subtle)', color: 'var(--color-success)' }}>
                <CheckCircle size={14} /> Prijať všetko ({stats.pending})
              </button><ActionScope effects={['hub-write']} /></span>
              <span className="action-control"><button onClick={handleResetAll} disabled={loading || savingEdit || bulkLoading || pausing || (stats.matched === 0 && stats.partial === 0)}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium disabled:opacity-50"
                style={{ backgroundColor: 'var(--color-bg-tertiary)', color: 'var(--color-text-secondary)' }}>
                <RotateCcw size={14} /> Resetovať
              </button><ActionScope effects={['hub-write']} /></span>
            </div>}
          </div>
          <div className="max-h-96 overflow-auto">
            <table className="w-full">
              <thead>
                <tr className="border-b sticky top-0" style={{ backgroundColor: 'var(--color-bg-primary)', borderColor: 'var(--color-border-subtle)' }}>
                  <th className="text-left px-4 py-2 text-xs font-medium" style={{ color: 'var(--color-text-tertiary)' }}>SKU</th>
                  <th className="text-left px-4 py-2 text-xs font-medium" style={{ color: 'var(--color-text-tertiary)' }}>{t('receiving.ean')}</th>
                  <th className="text-left px-4 py-2 text-xs font-medium" style={{ color: 'var(--color-text-tertiary)' }}>Názov</th>
                  <th className="text-right px-4 py-2 text-xs font-medium" style={{ color: 'var(--color-text-tertiary)' }}>Obj.</th>
                  <th className="text-right px-4 py-2 text-xs font-medium" style={{ color: 'var(--color-text-tertiary)' }}>Prij.</th>
                  <th className="text-center px-4 py-2 text-xs font-medium" style={{ color: 'var(--color-text-tertiary)' }}>Hotovo</th>
                  <th className="px-4 py-2"></th>
                </tr>
              </thead>
              <tbody className="divide-y" style={{ borderColor: 'var(--color-border-subtle)' }}>
                {lines.map((line, idx) => {
                  const done    = isLineDone(line);
                  const partial = line.received_qty > 0 && line.received_qty < line.ordered_qty;
                  return (
                    <tr key={idx} className="transition-colors" style={{ backgroundColor: 'transparent' }}
                      onMouseEnter={e => { e.currentTarget.style.backgroundColor = 'var(--color-bg-tertiary)'; }}
                      onMouseLeave={e => { e.currentTarget.style.backgroundColor = 'transparent'; }}>
                      <td className="px-4 py-2 text-sm" style={{ fontFamily: 'var(--font-mono)', color: 'var(--color-accent)' }}>{line.product_code || line.scm}</td>
                      <td data-testid={`receiving-line-ean-${idx}`} title={line.ean_source ? t(`receiving.eanSources.${line.ean_source}`) : undefined} className="px-4 py-2 text-xs whitespace-nowrap" style={{ color: 'var(--color-text-secondary)' }}>
                        <div style={{ fontFamily: 'var(--font-mono)' }}>{(line.display_ean ?? line.ean) || '–'}</div>
                        {line.ean_source === 'supplier_catalog' && <div className="mt-1" style={{ color: 'var(--color-text-tertiary)' }}>{t('receiving.catalogEanLabel')}</div>}
                      </td>
                      <td className="px-4 py-2 text-sm" style={{ color: 'var(--color-text-primary)' }}>
                        <div className="truncate max-w-xs" title={line.title}>{line.title}</div>
                      </td>
                      <td className="px-4 py-2 text-sm text-right" style={{ color: 'var(--color-text-secondary)' }}>{line.ordered_qty}</td>
                      <td className="px-4 py-2 text-sm text-right font-medium" style={{ color: line.received_qty >= line.ordered_qty ? 'var(--color-success)' : line.received_qty > 0 ? 'var(--color-info)' : 'var(--color-text-primary)' }}>
                        <div className="flex items-center justify-end gap-2">
                          <span>{line.received_qty}</span>
                          {canEdit && <span className="flex flex-col">
                            <button data-testid={`receiving-increase-line-${idx}`} type="button" aria-label={t('receiving.increaseQuantity', { code: line.product_code || line.scm })} title={t('receiving.increaseQuantity', { code: line.product_code || line.scm })}
                              disabled={loading || savingEdit || bulkLoading || pausing || finalizing || line.received_qty > 999999998.999}
                              onClick={() => adjustLineQuantity(idx, 1)} className="px-1 rounded border disabled:opacity-40" style={{ borderColor: 'var(--color-border-subtle)' }}><ChevronUp size={16} /></button>
                            <button data-testid={`receiving-decrease-line-${idx}`} type="button" aria-label={t('receiving.decreaseQuantity', { code: line.product_code || line.scm })} title={t('receiving.decreaseQuantity', { code: line.product_code || line.scm })}
                              disabled={loading || savingEdit || bulkLoading || pausing || finalizing || line.received_qty <= 0}
                              onClick={() => adjustLineQuantity(idx, -1)} className="px-1 rounded border disabled:opacity-40" style={{ borderColor: 'var(--color-border-subtle)' }}><ChevronDown size={16} /></button>
                          </span>}
                        </div>
                      </td>
                      <td className="px-4 py-2 text-center">
                        {partial ? (
                          <button disabled={!canEdit} onClick={() => toggleDone(line)} title={done ? 'Hotovo (odznač)' : 'Označiť ako hotovo'}
                            className="w-6 h-6 rounded border-2 flex items-center justify-center mx-auto transition-colors"
                            style={{ borderColor: done ? 'var(--color-success)' : 'var(--color-border-subtle)', backgroundColor: done ? 'var(--color-success-subtle)' : 'transparent', color: done ? 'var(--color-success)' : 'transparent' }}>
                            <Check size={12} />
                          </button>
                        ) : line.received_qty >= line.ordered_qty && line.received_qty > 0 ? (
                          <span style={{ color: 'var(--color-success)' }}>✓</span>
                        ) : (
                          <span style={{ color: 'var(--color-text-tertiary)' }}>–</span>
                        )}
                      </td>
                      <td className="px-4 py-2 text-center">
                        {canEdit && <button data-testid={`receiving-edit-line-${idx}`} disabled={loading || savingEdit || bulkLoading || pausing || finalizing} aria-label={t('receiving.editQuantity')} onClick={() => openEditModal(idx, line)}><Edit2 size={14} style={{ color: 'var(--color-text-tertiary)' }} /></button>}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Edit modal */}
      {canEdit && editingLine && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4" style={{ backgroundColor: 'rgba(0,0,0,0.7)' }} onClick={() => setEditingLine(null)}>
          <div className="rounded-xl border p-6 max-w-md w-full" style={{ backgroundColor: 'var(--color-bg-secondary)', borderColor: 'var(--color-border-subtle)' }} onClick={e => e.stopPropagation()}>
            <h3 className="text-lg font-semibold" style={{ fontFamily: 'var(--font-display)', color: 'var(--color-text-primary)' }}>Upraviť prijaté množstvo</h3>
            <div className="mt-4 p-3 rounded-lg" style={{ backgroundColor: 'var(--color-bg-primary)' }}>
              <div className="text-sm font-medium" style={{ fontFamily: 'var(--font-mono)', color: 'var(--color-accent)' }}>{editingLine.line.product_code || editingLine.line.scm}</div>
              <div className="text-sm mt-1" style={{ color: 'var(--color-text-secondary)' }}>{editingLine.line.title}</div>
              <div className="text-xs mt-2" style={{ color: 'var(--color-text-tertiary)' }}>Objednané: <strong>{editingLine.line.ordered_qty}</strong> ks</div>
            </div>
            <div className="mt-4">
              <label className="text-sm font-medium" style={{ color: 'var(--color-text-secondary)' }}>Prijaté množstvo</label>
              <div className="flex gap-2 mt-2">
                <input type="number" value={editQty} onChange={e => setEditQty(e.target.value)} className="flex-1 py-2 text-center text-lg" style={{ fontFamily: 'var(--font-mono)' }} min={0} step={0.001} autoFocus />
                <button onClick={() => setEditQty(editingLine.line.ordered_qty.toString())} className="px-3 py-2 rounded-lg text-sm font-medium" style={{ backgroundColor: 'var(--color-accent-subtle)', color: 'var(--color-accent)' }}>= {editingLine.line.ordered_qty}</button>
              </div>
            </div>
            <div className="mt-4">
              <label className="text-sm font-medium" style={{ color: 'var(--color-text-secondary)' }}>Poznámka (voliteľné)</label>
              <input type="text" value={editNote} onChange={e => setEditNote(e.target.value)} placeholder="Napr. poškodené, chýba v dodávke..." className="w-full mt-2 py-2" />
            </div>
            <div className="flex flex-wrap justify-end gap-3 mt-6">
              <Button variant="secondary" onClick={() => setEditingLine(null)}>Zrušiť</Button>
              <span className="action-control"><Button variant="primary" onClick={handleSaveEdit} loading={savingEdit} disabled={pausing || loading || bulkLoading}>Uložiť</Button><ActionScope effects={['hub-write']} /></span>
            </div>
          </div>
        </div>
      )}

      {/* Poznámka */}
      <div className="rounded-xl border p-4" style={{ backgroundColor: 'var(--color-bg-secondary)', borderColor: 'var(--color-border-subtle)' }}>
        <label className="flex items-center gap-2 text-sm font-medium mb-2" style={{ color: 'var(--color-text-secondary)' }}>
          <MessageSquare size={14} /> Poznámka k faktúre
          {savingNote && <span className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}><Loader2 size={10} className="inline animate-spin mr-1" />ukladám...</span>}
        </label>
        <textarea readOnly={!canEdit} data-testid="receiving-note" value={invoiceNote} onChange={e => setInvoiceNote(e.target.value)} onBlur={e => saveNote(e.target.value.trim())}
          placeholder="Napr. 3 položky nedodané, reklamácia na ..." className="w-full py-2 px-3 rounded-lg text-sm resize-none" rows={2}
          style={{ backgroundColor: 'var(--color-bg-primary)', border: '1px solid var(--color-border-subtle)', color: 'var(--color-text-primary)' }} />
      </div>

      {/* Actions */}
      <div className="flex flex-wrap justify-between gap-3">
        <Button variant="secondary" onClick={() => setShowLines(!showLines)}>
          <List size={16} /> {showLines ? 'Skryť položky' : 'Zobraziť položky'}
        </Button>
        {canEdit && <Button data-testid="receiving-finalize" variant="success" onClick={() => stats.pending > 0 || stats.partial > 0 ? setShowConfirm(true) : doFinalize()} loading={finalizing} disabled={!sessionId || loading || savingEdit || bulkLoading || finalizing || pausing}>
          <Check size={16} /> {t(stats.pending > 0 || stats.partial > 0 ? 'receiving.finishIncomplete' : 'receiving.finish')}
        </Button>}
      </div>

      {/* Confirm modal */}
      {canEdit && showConfirm && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4" style={{ backgroundColor: 'rgba(0,0,0,0.7)' }}>
          <div className="rounded-xl border p-6 max-w-md w-full" style={{ backgroundColor: 'var(--color-bg-secondary)', borderColor: 'var(--color-border-subtle)' }}>
            <div className="flex items-start gap-4">
              <div className="w-10 h-10 rounded-full flex items-center justify-center flex-shrink-0" style={{ backgroundColor: 'var(--color-warning-subtle)', color: 'var(--color-warning)' }}>
                <AlertTriangle size={20} />
              </div>
              <div className="flex-1">
                <h3 className="text-lg font-semibold" style={{ fontFamily: 'var(--font-display)', color: 'var(--color-text-primary)' }}>Potvrdiť dokončenie</h3>
                <ul className="mt-3 space-y-1 text-sm">
                  {stats.pending > 0 && <li style={{ color: 'var(--color-text-tertiary)' }}>• <strong style={{ color: 'var(--color-text-primary)' }}>{stats.pending}</strong> položiek neprijatých</li>}
                  {stats.partial > 0 && <li style={{ color: 'var(--color-text-tertiary)' }}>• <strong style={{ color: 'var(--color-info)' }}>{stats.partial}</strong> položiek čiastočne prijatých</li>}
                </ul>
                <p className="text-xs mt-2" style={{ color: 'var(--color-text-tertiary)' }}>
                  {t('receiving.finishIncompleteExplanation')}
                </p>
                <div className="mt-4">
                  <label className="text-sm font-medium" style={{ color: 'var(--color-text-secondary)' }}>Poznámka</label>
                  <textarea value={invoiceNote} onChange={e => setInvoiceNote(e.target.value)} className="w-full mt-2 py-2 px-3 rounded-lg text-sm resize-none" rows={2}
                    style={{ backgroundColor: 'var(--color-bg-primary)', border: '1px solid var(--color-border-subtle)', color: 'var(--color-text-primary)' }} />
                </div>
              </div>
            </div>
            <div className="flex flex-wrap justify-end gap-3 mt-6">
              <Button variant="secondary" onClick={() => setShowConfirm(false)}>Pokračovať v skenovaní</Button>
              <span className="action-control"><Button data-testid="receiving-finalize-incomplete" variant="primary" disabled={loading || finalizing || savingEdit || bulkLoading || pausing} onClick={() => doFinalize(true)}>{t('receiving.confirmFinishIncomplete')}</Button><ActionScope effects={['hub-write']}>{t('actions.receiving.finalize')}</ActionScope></span>
            </div>
          </div>
        </div>
      )}

      {/* Success */}
      {finalizeResult && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4" style={{ backgroundColor: 'rgba(0,0,0,0.8)' }}>
          <div className="rounded-xl border p-8 max-w-lg w-full text-center" style={{ backgroundColor: 'var(--color-bg-secondary)', borderColor: 'var(--color-success)' }}>
            <div className="w-16 h-16 rounded-full flex items-center justify-center mx-auto" style={{ backgroundColor: 'var(--color-success-subtle)', color: 'var(--color-success)' }}><Check size={32} /></div>
            <h2 className="text-xl font-semibold mt-4" style={{ fontFamily: 'var(--font-display)', color: 'var(--color-text-primary)' }}>Príjem dokončený!</h2>
            <p className="text-sm mt-2" style={{ color: 'var(--color-text-secondary)' }}>
              Faktúra <strong style={{ fontFamily: 'var(--font-mono)', color: 'var(--color-accent)' }}>{finalizeResult.invoice_no}</strong> úspešne spracovaná.
            </p>
            <div className="mt-6 p-4 rounded-lg grid grid-cols-2 gap-4" style={{ backgroundColor: 'var(--color-bg-primary)' }}>
              <div><div className="text-2xl font-semibold" style={{ fontFamily: 'var(--font-display)', color: 'var(--color-success)' }}>{finalizeResult.stats.received_complete}</div><div className="text-xs mt-1" style={{ color: 'var(--color-text-tertiary)' }}>Kompletných</div></div>
              <div><div className="text-2xl font-semibold" style={{ fontFamily: 'var(--font-display)', color: finalizeResult.stats.not_received > 0 ? 'var(--color-warning)' : 'var(--color-text-secondary)' }}>{finalizeResult.stats.not_received}</div><div className="text-xs mt-1" style={{ color: 'var(--color-text-tertiary)' }}>Neprijatých</div></div>
            </div>
            {supplier === 'paul-lange' ? <p className="text-xs mt-4" style={{ color: 'var(--color-text-tertiary)' }}>Načítavam výsledky...</p> :
              <Button data-testid="receiving-completion-back" className="mt-4" onClick={() => navigate('/receiving')}>{t('receiving.backToList')}</Button>}
          </div>
        </div>
      )}

      {supplier === 'paul-lange' && showCsvModal && (
        <ReceivingResultsModal supplier={supplier} invoiceId={`${supplier}:${invoiceId}`} onClose={() => { setShowCsvModal(false); navigate('/receiving'); }} />
      )}
    </div>
  );
}

export default ReceivingSessionPage;
