import React, { useState, useRef, useEffect } from 'react';
import { useParams, useNavigate, useLocation } from 'react-router-dom';
import { ArrowLeft, Camera, Check, X, Loader2, List, AlertTriangle, Edit2, CheckCircle, RotateCcw, MessageSquare } from 'lucide-react';
import { Button } from '../components/ui/Button.new';
import { 
  scanCode as scanCodeApi, 
  getReceivingSummary, 
  finalizeReceiving,
  setLineQuantity,
  acceptAllItems,
  resetAllItems,
  type ReceivingLine,
  type ReceivingSummary,
  type ScanResult,
  type FinalizeResult,
} from '../api/receiving';
import { ReceivingResultsModal } from "../components/ReceivingResultsModal";
import { API_BASE } from '../api/client';

export function ReceivingSessionPage() {
  const { invoiceId } = useParams();
  const navigate = useNavigate();
  const location = useLocation();
  const inputRef = useRef<HTMLInputElement>(null);

  const state = location.state as { 
    sessionId?: string; 
    supplier?: string; 
    lines?: ReceivingLine[] 
  } | null;
  
  const sessionId = state?.sessionId || '';
  const supplier = state?.supplier || 'paul-lange';

  const [scannedCode, setScannedCode] = useState('');
  const [quantity, setQuantity] = useState(1);
  const [lastScan, setLastScan] = useState<{
    code: string;
    product: string;
    sku: string;
    received: number;
    expected: number;
    status: string;
  } | null>(null);
  const [stats, setStats] = useState<ReceivingSummary>({ 
    matched: 0, 
    partial: 0, 
    pending: state?.lines?.length || 0, 
    overage: 0,
    unexpected: 0 
  });
  const [lines, setLines] = useState<ReceivingLine[]>(state?.lines || []);
  const [loading, setLoading] = useState(false);
  const [finalizing, setFinalizing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showConfirmModal, setShowConfirmModal] = useState(false);
  const [finalizeResult, setFinalizeResult] = useState<FinalizeResult | null>(null);

  // Poznámka k faktúre
  const [invoiceNote, setInvoiceNote] = useState('');
  const [savingNote, setSavingNote] = useState(false);
  
  // Edit modal state
  const [editingLine, setEditingLine] = useState<{ index: number; line: ReceivingLine } | null>(null);
  const [editQty, setEditQty] = useState<string>('');
  const [editNote, setEditNote] = useState('');
  const [savingEdit, setSavingEdit] = useState(false);
  const [showCsvModal, setShowCsvModal] = useState(false);
  
  // Bulk actions
  const [bulkLoading, setBulkLoading] = useState(false);
  
  const isResumed = (location.state as any)?.isResumed || false;
  const [showLines, setShowLines] = useState(false);

  const total = stats.matched + stats.partial + stats.pending;
  const progress = total > 0 ? Math.round((stats.matched / total) * 100) : 0;

  useEffect(() => {
    inputRef.current?.focus();
    if (!sessionId && !state?.lines) {
      navigate('/receiving');
    }
  }, [sessionId, navigate, state]);

  // Load summary
  useEffect(() => {
    if (sessionId) {
      getReceivingSummary(supplier, sessionId)
        .then(data => {
          setLines(data.lines || []);
          setStats(data.summary || stats);
        })
        .catch(err => console.error('Failed to load summary:', err));
    }
  }, [sessionId, supplier]);

  // Load existing note
  useEffect(() => {
    if (!invoiceId || !supplier) return;
    fetch(`${API_BASE}/suppliers/${supplier}/invoices/${invoiceId}/note`)
      .then(r => r.ok ? r.json() : null)
      .then(data => { if (data?.note) setInvoiceNote(data.note); })
      .catch(() => {});
  }, [invoiceId, supplier]);

  const saveNote = async (note: string) => {
    if (!invoiceId) return;
    setSavingNote(true);
    try {
      await fetch(`${API_BASE}/suppliers/${supplier}/invoices/${invoiceId}/note`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ note }),
      });
    } catch (e) {
      console.error('Failed to save note:', e);
    } finally {
      setSavingNote(false);
    }
  };

  const handleScan = async () => {
    if (!scannedCode.trim() || !sessionId) return;

    setLoading(true);
    setError(null);

    try {
      const result: ScanResult = await scanCodeApi(supplier, sessionId, scannedCode, quantity);
      
      if (result.line) {
        setLastScan({
          code: scannedCode,
          product: result.line.title || 'Neznámy produkt',
          sku: result.line.product_code || result.line.scm || '',
          received: result.line.received_qty,
          expected: result.line.ordered_qty,
          status: result.status,
        });
      } else {
        setLastScan({
          code: scannedCode,
          product: 'Nenájdené na faktúre',
          sku: '',
          received: 0,
          expected: 0,
          status: result.status,
        });
      }

      setStats(result.summary);

      if (result.line) {
        setLines(prev => prev.map(l => 
          l.scm === result.line?.scm || l.ean === result.line?.ean 
            ? { ...l, ...result.line }
            : l
        ));
      }

      setScannedCode('');
      setQuantity(1);
    } catch (err) {
      console.error('Scan failed:', err);
      setError('Chyba pri skenovaní');
    } finally {
      setLoading(false);
      inputRef.current?.focus();
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter') handleScan();
  };

  const handleFinalizeClick = () => {
    if (stats.pending > 0 || stats.partial > 0) {
      setShowConfirmModal(true);
    } else {
      doFinalize();
    }
  };

  const openEditModal = (index: number, line: ReceivingLine) => {
    setEditingLine({ index, line });
    setEditQty(line.received_qty.toString());
    setEditNote('');
  };

  const handleSaveEdit = async () => {
    if (!editingLine || !sessionId) return;

    setSavingEdit(true);
    setError(null);

    try {
      const newQty = parseFloat(editQty) || 0;
      const result = await setLineQuantity(
        supplier, 
        sessionId, 
        editingLine.index, 
        newQty,
        editNote || undefined
      );

      setLines(prev => prev.map((l, i) => 
        i === editingLine.index ? result.line : l
      ));
      setStats(result.summary);
      setEditingLine(null);
    } catch (err) {
      console.error('Edit failed:', err);
      setError('Nepodarilo sa uložiť zmenu');
    } finally {
      setSavingEdit(false);
    }
  };

  const handleSetToOrdered = () => {
    if (editingLine) setEditQty(editingLine.line.ordered_qty.toString());
  };

  const handleAcceptAll = async () => {
    if (!sessionId) return;
    setBulkLoading(true);
    setError(null);
    try {
      const result = await acceptAllItems(supplier, sessionId, true);
      setLines(result.lines);
      setStats(result.summary);
    } catch (err) {
      console.error('Accept all failed:', err);
      setError('Nepodarilo sa prijať všetky položky');
    } finally {
      setBulkLoading(false);
    }
  };

  const handleResetAll = async () => {
    if (!sessionId) return;
    if (!confirm('Naozaj chcete vynulovať všetky prijaté množstvá?')) return;
    setBulkLoading(true);
    setError(null);
    try {
      const result = await resetAllItems(supplier, sessionId);
      setLines(result.lines);
      setStats(result.summary);
    } catch (err) {
      console.error('Reset all failed:', err);
      setError('Nepodarilo sa vynulovať množstvá');
    } finally {
      setBulkLoading(false);
    }
  };

  const doFinalize = async () => {
    if (!sessionId) return;

    // Uložiť poznámku pred dokončením
    if (invoiceNote.trim()) {
      await saveNote(invoiceNote.trim());
    }

    setFinalizing(true);
    setError(null);
    setShowConfirmModal(false);

    try {
      const result = await finalizeReceiving(supplier, sessionId);
      setFinalizeResult(result);
      
      setTimeout(() => {
        setShowCsvModal(true);
      }, 2000);
    } catch (err) {
      console.error('Finalize failed:', err);
      setError('Nepodarilo sa dokončiť príjem. Skúste to znova.');
      setFinalizing(false);
    }
  };

  const statusColors: Record<string, { bg: string; border: string; icon: string; label: string }> = {
    matched: { bg: 'var(--color-success-subtle)', border: 'var(--color-success)', icon: '✓', label: 'Nájdené' },
    partial: { bg: 'var(--color-info-subtle)', border: 'var(--color-info)', icon: '◐', label: 'Čiastočne' },
    overage: { bg: 'var(--color-warning-subtle)', border: 'var(--color-warning)', icon: '!', label: 'Prebytok' },
    unexpected: { bg: 'var(--color-error-subtle)', border: 'var(--color-error)', icon: '?', label: 'Nenájdené' },
    unknown: { bg: 'var(--color-error-subtle)', border: 'var(--color-error)', icon: '?', label: 'Neznáme' },
    pending: { bg: 'var(--color-bg-tertiary)', border: 'var(--color-border-subtle)', icon: '○', label: 'Čaká' },
  };

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-4">
          <button
            onClick={() => navigate('/receiving')}
            className="p-2 rounded-lg transition-colors"
            style={{ color: 'var(--color-text-secondary)' }}
            onMouseEnter={(e) => {
              e.currentTarget.style.backgroundColor = 'var(--color-bg-secondary)';
              e.currentTarget.style.color = 'var(--color-text-primary)';
            }}
            onMouseLeave={(e) => {
              e.currentTarget.style.backgroundColor = 'transparent';
              e.currentTarget.style.color = 'var(--color-text-secondary)';
            }}
          >
            <ArrowLeft size={20} />
          </button>
          <div>
            <h1
              className="text-xl font-semibold"
              style={{ fontFamily: 'var(--font-display)', color: 'var(--color-text-primary)' }}
            >
              Príjem:{' '}
              <span style={{ fontFamily: 'var(--font-mono)', color: 'var(--color-accent)' }}>
                {invoiceId}
              </span>
            </h1>
            {sessionId && (
              <div className="text-xs mt-0.5" style={{ color: 'var(--color-text-tertiary)', fontFamily: 'var(--font-mono)' }}>
                Session: {sessionId}
              </div>
            )}
          </div>
        </div>
        <Button variant="danger" onClick={() => navigate('/receiving')}>
          <X size={16} />
          Ukončiť session
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

      {/* Main Scanning Area */}
      <div
        className="rounded-xl border p-8"
        style={{ backgroundColor: 'var(--color-bg-secondary)', borderColor: 'var(--color-border-subtle)' }}
      >
        <div className="max-w-md mx-auto text-center">
          <div
            className="w-20 h-20 rounded-2xl border-2 border-dashed flex items-center justify-center mx-auto"
            style={{
              backgroundColor: 'var(--color-accent-subtle)',
              borderColor: 'var(--color-border-accent)',
              color: 'var(--color-accent)',
            }}
          >
            <Camera size={32} />
          </div>

          <h2 className="text-lg font-medium mt-4" style={{ color: 'var(--color-text-primary)' }}>
            Naskenuj čiarový kód
          </h2>
          <p className="text-sm mt-1" style={{ color: 'var(--color-text-tertiary)' }}>
            Použi skener alebo zadaj kód manuálne
          </p>

          <div className="mt-6 flex gap-2">
            <input
              ref={inputRef}
              type="text"
              value={scannedCode}
              onChange={(e) => setScannedCode(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="Zadaj EAN, SKU alebo kód produktu..."
              className="flex-1 py-3"
              style={{ fontFamily: 'var(--font-mono)' }}
              autoFocus
              disabled={loading}
            />
            <Button
              variant="primary"
              onClick={handleScan}
              loading={loading}
              disabled={!scannedCode.trim() || !sessionId}
            >
              Scan
            </Button>
          </div>

          <div className="flex items-center justify-center gap-4 mt-4">
            <label className="flex items-center gap-2 text-sm" style={{ color: 'var(--color-text-secondary)' }}>
              Množstvo:
              <input
                type="number"
                value={quantity}
                onChange={(e) => setQuantity(Number(e.target.value) || 1)}
                min={-999}
                step={1}
                className="w-20 text-center py-1"
              />
            </label>
          </div>
        </div>
      </div>

      {/* Last Scan Result */}
      {lastScan && (
        <div
          className="rounded-xl border p-4"
          style={{
            backgroundColor: statusColors[lastScan.status]?.bg || statusColors.unknown.bg,
            borderColor: statusColors[lastScan.status]?.border || statusColors.unknown.border,
          }}
        >
          <div className="flex items-center gap-3">
            <div
              className="w-8 h-8 rounded-full flex items-center justify-center"
              style={{
                backgroundColor: `${statusColors[lastScan.status]?.border || statusColors.unknown.border}33`,
                color: statusColors[lastScan.status]?.border || statusColors.unknown.border,
              }}
            >
              {statusColors[lastScan.status]?.icon || '?'}
            </div>
            <div className="flex-1">
              <div className="text-sm font-medium uppercase" style={{ color: statusColors[lastScan.status]?.border || statusColors.unknown.border }}>
                {statusColors[lastScan.status]?.label || 'Neznáme'}
              </div>
              <div className="text-sm mt-0.5" style={{ color: 'var(--color-text-primary)' }}>
                <span style={{ fontFamily: 'var(--font-mono)', color: 'var(--color-text-secondary)' }}>
                  {lastScan.code}
                </span>
                {lastScan.sku && (
                  <>{' → '}<span style={{ fontFamily: 'var(--font-mono)' }}>{lastScan.sku}</span></>
                )}
                <span className="ml-2" style={{ color: 'var(--color-text-secondary)' }}>
                  "{lastScan.product}"
                </span>
              </div>
              {lastScan.expected > 0 && (
                <div className="text-xs mt-1" style={{ color: 'var(--color-text-tertiary)' }}>
                  Prijaté: {lastScan.received}/{lastScan.expected} (+{quantity} práve teraz)
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Progress */}
      <div
        className="rounded-xl border p-4"
        style={{ backgroundColor: 'var(--color-bg-secondary)', borderColor: 'var(--color-border-subtle)' }}
      >
        <div className="flex items-center justify-between mb-3">
          <span className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>Priebeh</span>
          <span className="text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>
            {progress}% dokončené
          </span>
        </div>

        <div className="h-2 rounded-full overflow-hidden" style={{ backgroundColor: 'var(--color-bg-primary)' }}>
          <div
            className="h-full transition-all"
            style={{
              width: `${progress}%`,
              background: 'linear-gradient(90deg, var(--color-accent), var(--color-accent-hover))',
            }}
          />
        </div>

        <div className="flex items-center justify-between mt-4 text-sm">
          <div className="flex items-center gap-1">
            <span style={{ color: 'var(--color-success)' }}>✓</span>
            <span style={{ color: 'var(--color-text-secondary)' }}>{stats.matched} Kompletné</span>
          </div>
          <div className="flex items-center gap-1">
            <span style={{ color: 'var(--color-info)' }}>◐</span>
            <span style={{ color: 'var(--color-text-secondary)' }}>{stats.partial} Čiastočné</span>
          </div>
          <div className="flex items-center gap-1">
            <span style={{ color: 'var(--color-text-tertiary)' }}>○</span>
            <span style={{ color: 'var(--color-text-secondary)' }}>{stats.pending} Čakajúce</span>
          </div>
          <div className="flex items-center gap-1">
            <span style={{ color: 'var(--color-error)' }}>!</span>
            <span style={{ color: 'var(--color-text-secondary)' }}>{stats.overage + stats.unexpected} Problémy</span>
          </div>
        </div>
      </div>

      {/* Lines List (collapsible) */}
      {showLines && (
        <div
          className="rounded-xl border overflow-hidden"
          style={{ backgroundColor: 'var(--color-bg-secondary)', borderColor: 'var(--color-border-subtle)' }}
        >
          <div
            className="px-4 py-3 border-b flex items-center justify-between"
            style={{ borderColor: 'var(--color-border-subtle)' }}
          >
            <span className="text-sm font-medium" style={{ color: 'var(--color-text-secondary)' }}>
              {lines.length} položiek
            </span>
            <div className="flex gap-2">
              <button
                onClick={handleAcceptAll}
                disabled={bulkLoading || stats.pending === 0}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium transition-colors disabled:opacity-50"
                style={{ backgroundColor: 'var(--color-success-subtle)', color: 'var(--color-success)' }}
              >
                <CheckCircle size={14} />
                Prijať všetko ({stats.pending})
              </button>
              <button
                onClick={handleResetAll}
                disabled={bulkLoading || (stats.matched === 0 && stats.partial === 0)}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium transition-colors disabled:opacity-50"
                style={{ backgroundColor: 'var(--color-bg-tertiary)', color: 'var(--color-text-secondary)' }}
              >
                <RotateCcw size={14} />
                Resetovať
              </button>
            </div>
          </div>

          <div className="max-h-96 overflow-y-auto">
            <table className="w-full">
              <thead>
                <tr
                  className="border-b sticky top-0"
                  style={{ backgroundColor: 'var(--color-bg-primary)', borderColor: 'var(--color-border-subtle)' }}
                >
                  <th className="text-left px-4 py-2 text-xs font-medium" style={{ color: 'var(--color-text-tertiary)' }}>SKU</th>
                  <th className="text-left px-4 py-2 text-xs font-medium" style={{ color: 'var(--color-text-tertiary)' }}>Názov</th>
                  <th className="text-right px-4 py-2 text-xs font-medium" style={{ color: 'var(--color-text-tertiary)' }}>Obj.</th>
                  <th className="text-right px-4 py-2 text-xs font-medium" style={{ color: 'var(--color-text-tertiary)' }}>Prij.</th>
                  <th className="text-center px-4 py-2 text-xs font-medium" style={{ color: 'var(--color-text-tertiary)' }}>Stav</th>
                  <th className="text-center px-4 py-2 text-xs font-medium" style={{ color: 'var(--color-text-tertiary)' }}></th>
                </tr>
              </thead>
              <tbody className="divide-y" style={{ borderColor: 'var(--color-border-subtle)' }}>
                {lines.map((line, idx) => (
                  <tr
                    key={idx}
                    className="cursor-pointer transition-colors"
                    style={{ backgroundColor: 'transparent' }}
                    onMouseEnter={(e) => { e.currentTarget.style.backgroundColor = 'var(--color-bg-tertiary)'; }}
                    onMouseLeave={(e) => { e.currentTarget.style.backgroundColor = 'transparent'; }}
                    onClick={() => openEditModal(idx, line)}
                  >
                    <td className="px-4 py-2 text-sm" style={{ fontFamily: 'var(--font-mono)', color: 'var(--color-accent)' }}>
                      {line.product_code || line.scm}
                    </td>
                    <td className="px-4 py-2 text-sm" style={{ color: 'var(--color-text-primary)' }}>
                      <div className="truncate max-w-xs" title={line.title}>{line.title}</div>
                    </td>
                    <td className="px-4 py-2 text-sm text-right" style={{ color: 'var(--color-text-secondary)' }}>
                      {line.ordered_qty}
                    </td>
                    <td className="px-4 py-2 text-sm text-right font-medium" style={{ 
                      color: line.received_qty >= line.ordered_qty 
                        ? 'var(--color-success)' 
                        : line.received_qty > 0 
                        ? 'var(--color-info)'
                        : 'var(--color-text-primary)' 
                    }}>
                      {line.received_qty}
                    </td>
                    <td className="px-4 py-2 text-center">
                      <span style={{ color: statusColors[line.status]?.border }}>
                        {statusColors[line.status]?.icon}
                      </span>
                    </td>
                    <td className="px-4 py-2 text-center">
                      <Edit2 size={14} style={{ color: 'var(--color-text-tertiary)' }} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Edit Line Modal */}
      {editingLine && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center p-4"
          style={{ backgroundColor: 'rgba(0, 0, 0, 0.7)' }}
          onClick={() => setEditingLine(null)}
        >
          <div
            className="rounded-xl border p-6 max-w-md w-full animate-slide-in-bottom"
            style={{ backgroundColor: 'var(--color-bg-secondary)', borderColor: 'var(--color-border-subtle)' }}
            onClick={(e) => e.stopPropagation()}
          >
            <h3 className="text-lg font-semibold" style={{ fontFamily: 'var(--font-display)', color: 'var(--color-text-primary)' }}>
              Upraviť prijaté množstvo
            </h3>
            
            <div className="mt-4 p-3 rounded-lg" style={{ backgroundColor: 'var(--color-bg-primary)' }}>
              <div className="text-sm font-medium" style={{ fontFamily: 'var(--font-mono)', color: 'var(--color-accent)' }}>
                {editingLine.line.product_code || editingLine.line.scm}
              </div>
              <div className="text-sm mt-1" style={{ color: 'var(--color-text-secondary)' }}>
                {editingLine.line.title}
              </div>
              <div className="text-xs mt-2" style={{ color: 'var(--color-text-tertiary)' }}>
                Objednané: <strong>{editingLine.line.ordered_qty}</strong> ks
              </div>
            </div>

            <div className="mt-4">
              <label className="text-sm font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                Prijaté množstvo
              </label>
              <div className="flex gap-2 mt-2">
                <input
                  type="number"
                  value={editQty}
                  onChange={(e) => setEditQty(e.target.value)}
                  className="flex-1 py-2 text-center text-lg"
                  style={{ fontFamily: 'var(--font-mono)' }}
                  min={0}
                  step={1}
                  autoFocus
                />
                <button
                  onClick={handleSetToOrdered}
                  className="px-3 py-2 rounded-lg text-sm font-medium transition-colors"
                  style={{ backgroundColor: 'var(--color-accent-subtle)', color: 'var(--color-accent)' }}
                >
                  = {editingLine.line.ordered_qty}
                </button>
              </div>
            </div>

            <div className="mt-4">
              <label className="text-sm font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                Poznámka (voliteľné)
              </label>
              <input
                type="text"
                value={editNote}
                onChange={(e) => setEditNote(e.target.value)}
                placeholder="Napr. poškodené, chýba v dodávke..."
                className="w-full mt-2 py-2"
              />
            </div>

            <div className="flex justify-end gap-3 mt-6">
              <Button variant="secondary" onClick={() => setEditingLine(null)}>Zrušiť</Button>
              <Button variant="primary" onClick={handleSaveEdit} loading={savingEdit}>Uložiť</Button>
            </div>
          </div>
        </div>
      )}

      {/* Poznámka k faktúre */}
      <div
        className="rounded-xl border p-4"
        style={{ backgroundColor: 'var(--color-bg-secondary)', borderColor: 'var(--color-border-subtle)' }}
      >
        <label
          className="flex items-center gap-2 text-sm font-medium mb-2"
          style={{ color: 'var(--color-text-secondary)' }}
        >
          <MessageSquare size={14} />
          Poznámka k faktúre
          {savingNote && (
            <span className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
              <Loader2 size={10} className="inline animate-spin mr-1" />
              ukladám...
            </span>
          )}
        </label>
        <textarea
          value={invoiceNote}
          onChange={(e) => setInvoiceNote(e.target.value)}
          onBlur={(e) => saveNote(e.target.value.trim())}
          placeholder="Napr. 3 položky nedodané, reklamácia na ..."
          className="w-full py-2 px-3 rounded-lg text-sm resize-none"
          rows={2}
          style={{
            backgroundColor: 'var(--color-bg-primary)',
            borderColor: 'var(--color-border-subtle)',
            border: '1px solid',
            color: 'var(--color-text-primary)',
          }}
        />
      </div>

      {/* Actions */}
      <div className="flex justify-between">
        <Button variant="secondary" onClick={() => setShowLines(!showLines)}>
          <List size={16} />
          {showLines ? 'Skryť položky' : 'Zobraziť položky'}
        </Button>
        
        <Button
          variant="success"
          onClick={handleFinalizeClick}
          loading={finalizing}
          disabled={!sessionId || finalizing}
        >
          <Check size={16} />
          Dokončiť príjem
        </Button>
      </div>

      {/* Confirmation Modal */}
      {showConfirmModal && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center p-4"
          style={{ backgroundColor: 'rgba(0, 0, 0, 0.7)' }}
        >
          <div
            className="rounded-xl border p-6 max-w-md w-full animate-slide-in-bottom"
            style={{ backgroundColor: 'var(--color-bg-secondary)', borderColor: 'var(--color-border-subtle)' }}
          >
            <div className="flex items-start gap-4">
              <div
                className="w-10 h-10 rounded-full flex items-center justify-center flex-shrink-0"
                style={{ backgroundColor: 'var(--color-warning-subtle)', color: 'var(--color-warning)' }}
              >
                <AlertTriangle size={20} />
              </div>
              <div>
                <h3
                  className="text-lg font-semibold"
                  style={{ fontFamily: 'var(--font-display)', color: 'var(--color-text-primary)' }}
                >
                  Potvrdiť dokončenie
                </h3>
                <p className="text-sm mt-2" style={{ color: 'var(--color-text-secondary)' }}>
                  Niektoré položky nie sú úplne prijaté:
                </p>
                <ul className="mt-3 space-y-1 text-sm">
                  {stats.pending > 0 && (
                    <li style={{ color: 'var(--color-text-tertiary)' }}>
                      • <strong style={{ color: 'var(--color-text-primary)' }}>{stats.pending}</strong> položiek neprijatých
                    </li>
                  )}
                  {stats.partial > 0 && (
                    <li style={{ color: 'var(--color-text-tertiary)' }}>
                      • <strong style={{ color: 'var(--color-info)' }}>{stats.partial}</strong> položiek čiastočne prijatých
                    </li>
                  )}
                </ul>

                {/* Poznámka aj v confirm modali */}
                <div className="mt-4">
                  <label className="text-sm font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                    Poznámka k nedodaným položkám (voliteľné)
                  </label>
                  <textarea
                    value={invoiceNote}
                    onChange={(e) => setInvoiceNote(e.target.value)}
                    placeholder="Napr. 3 položky nedodané, objednané znovu..."
                    className="w-full mt-2 py-2 px-3 rounded-lg text-sm resize-none"
                    rows={2}
                    style={{
                      backgroundColor: 'var(--color-bg-primary)',
                      border: '1px solid var(--color-border-subtle)',
                      color: 'var(--color-text-primary)',
                    }}
                  />
                </div>

                <p className="text-sm mt-3" style={{ color: 'var(--color-text-secondary)' }}>
                  Naozaj chcete dokončiť príjem?
                </p>
              </div>
            </div>
            <div className="flex justify-end gap-3 mt-6">
              <Button variant="secondary" onClick={() => setShowConfirmModal(false)}>
                Pokračovať v skenovaní
              </Button>
              <Button variant="primary" onClick={doFinalize}>
                Áno, dokončiť
              </Button>
            </div>
          </div>
        </div>
      )}

      {/* Success Screen */}
      {finalizeResult && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center p-4"
          style={{ backgroundColor: 'rgba(0, 0, 0, 0.8)' }}
        >
          <div
            className="rounded-xl border p-8 max-w-lg w-full text-center animate-slide-in-bottom"
            style={{ backgroundColor: 'var(--color-bg-secondary)', borderColor: 'var(--color-success)' }}
          >
            <div
              className="w-16 h-16 rounded-full flex items-center justify-center mx-auto"
              style={{ backgroundColor: 'var(--color-success-subtle)', color: 'var(--color-success)' }}
            >
              <Check size={32} />
            </div>
            <h2
              className="text-xl font-semibold mt-4"
              style={{ fontFamily: 'var(--font-display)', color: 'var(--color-text-primary)' }}
            >
              Príjem dokončený!
            </h2>
            <p className="text-sm mt-2" style={{ color: 'var(--color-text-secondary)' }}>
              Faktúra{' '}
              <strong style={{ fontFamily: 'var(--font-mono)', color: 'var(--color-accent)' }}>
                {finalizeResult.invoice_no}
              </strong>{' '}
              bola úspešne spracovaná.
            </p>

            <div
              className="mt-6 p-4 rounded-lg grid grid-cols-2 gap-4"
              style={{ backgroundColor: 'var(--color-bg-primary)' }}
            >
              <div>
                <div className="text-2xl font-semibold" style={{ fontFamily: 'var(--font-display)', color: 'var(--color-success)' }}>
                  {finalizeResult.received_items_count}
                </div>
                <div className="text-xs mt-1" style={{ color: 'var(--color-text-tertiary)' }}>Prijatých položiek</div>
              </div>
              <div>
                <div className="text-2xl font-semibold" style={{ fontFamily: 'var(--font-display)', color: 'var(--color-text-primary)' }}>
                  {Math.round(finalizeResult.total_received)}
                </div>
                <div className="text-xs mt-1" style={{ color: 'var(--color-text-tertiary)' }}>Celkom kusov</div>
              </div>
              <div>
                <div className="text-2xl font-semibold" style={{ fontFamily: 'var(--font-display)', color: 'var(--color-success)' }}>
                  {finalizeResult.stats.received_complete}
                </div>
                <div className="text-xs mt-1" style={{ color: 'var(--color-text-tertiary)' }}>Kompletných</div>
              </div>
              <div>
                <div
                  className="text-2xl font-semibold"
                  style={{
                    fontFamily: 'var(--font-display)',
                    color: finalizeResult.stats.not_received > 0 ? 'var(--color-warning)' : 'var(--color-text-secondary)',
                  }}
                >
                  {finalizeResult.stats.not_received}
                </div>
                <div className="text-xs mt-1" style={{ color: 'var(--color-text-tertiary)' }}>Neprijatých</div>
              </div>
            </div>

            <p className="text-xs mt-4" style={{ color: 'var(--color-text-tertiary)' }}>
              Načítavam výsledky...
            </p>
          </div>
        </div>
      )}

      {showCsvModal && (
        <ReceivingResultsModal
          supplier={supplier}
          invoiceId={`${supplier}:${invoiceId}`}
          onClose={() => {
            setShowCsvModal(false);
            navigate('/receiving');
          }}
        />
      )}
    </div>
  );
}

export default ReceivingSessionPage;
