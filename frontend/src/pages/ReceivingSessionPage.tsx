import React, { useState, useRef, useEffect } from 'react';
import { useParams, useNavigate, useLocation } from 'react-router-dom';
import {
  ArrowLeft, Camera, Check, X, List, AlertTriangle,
  Edit2, CheckCircle, RotateCcw, MessageSquare, Loader2
} from 'lucide-react';
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
import { ReceivingResultsModal } from '../components/ReceivingResultsModal';
import { API_BASE } from '../api/client';

function getRawScm(line: ReceivingLine): string {
  return (line.scm || (line.product_code || '').replace(/^PL-/i, '')).trim();
}

export function ReceivingSessionPage() {
  const { invoiceId } = useParams();
  const navigate = useNavigate();
  const location = useLocation();
  const inputRef = useRef<HTMLInputElement>(null);

  const state = location.state as { sessionId?: string; supplier?: string; lines?: ReceivingLine[] } | null;
  const sessionId = state?.sessionId || '';
  const supplier  = state?.supplier  || 'paul-lange';

  const [scannedCode,    setScannedCode]    = useState('');
  const [quantity,       setQuantity]       = useState(1);
  const [lastScan,       setLastScan]       = useState<{ code: string; product: string; sku: string; received: number; expected: number; status: string } | null>(null);
  const [stats,          setStats]          = useState<ReceivingSummary>({ matched: 0, partial: 0, pending: state?.lines?.length || 0, overage: 0, unexpected: 0 });
  const [lines,          setLines]          = useState<ReceivingLine[]>(state?.lines || []);
  const [loading,        setLoading]        = useState(false);
  const [finalizing,     setFinalizing]     = useState(false);
  const [error,          setError]          = useState<string | null>(null);
  const [showConfirm,    setShowConfirm]    = useState(false);
  const [finalizeResult, setFinalizeResult] = useState<FinalizeResult | null>(null);
  const [showCsvModal,   setShowCsvModal]   = useState(false);
  const [showLines,      setShowLines]      = useState(false);
  const [bulkLoading,    setBulkLoading]    = useState(false);
  const [doneItems,      setDoneItems]      = useState<Record<string, boolean>>({});
  const [invoiceNote,    setInvoiceNote]    = useState('');
  const [savingNote,     setSavingNote]     = useState(false);
  const [editingLine,    setEditingLine]    = useState<{ index: number; line: ReceivingLine } | null>(null);
  const [editQty,        setEditQty]        = useState('');
  const [editNote,       setEditNote]       = useState('');
  const [savingEdit,     setSavingEdit]     = useState(false);

  const total    = stats.matched + stats.partial + stats.pending;
  const progress = total > 0 ? Math.round((stats.matched / total) * 100) : 0;

  useEffect(() => {
    inputRef.current?.focus();
    if (!sessionId && !state?.lines) navigate('/receiving');
  }, [sessionId, navigate, state]);

  useEffect(() => {
    if (sessionId) {
      getReceivingSummary(supplier, sessionId)
        .then(data => { setLines(data.lines || []); setStats(data.summary || stats); })
        .catch(err => console.error('Failed to load summary:', err));
    }
  }, [sessionId, supplier]);

  useEffect(() => {
    if (!invoiceId || !supplier) return;
    fetch(`${API_BASE}/suppliers/${supplier}/invoices/${invoiceId}/note`)
      .then(r => r.ok ? r.json() : null)
      .then(data => { if (data?.note) setInvoiceNote(data.note); })
      .catch(() => {});
  }, [invoiceId, supplier]);

  const isLineDone = (line: ReceivingLine): boolean => {
    if (line.received_qty >= line.ordered_qty && line.received_qty > 0) return true;
    return doneItems[getRawScm(line)] === true;
  };

  const toggleDone = (line: ReceivingLine) => {
    const scm = getRawScm(line);
    setDoneItems(prev => ({ ...prev, [scm]: !prev[scm] }));
  };

  const saveNote = async (note: string) => {
    if (!invoiceId) return;
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

  const handleScan = async () => {
    if (!scannedCode.trim() || !sessionId) return;
    setLoading(true); setError(null);
    try {
      const result: ScanResult = await scanCodeApi(supplier, sessionId, scannedCode, quantity);
      setLastScan(result.line
        ? { code: scannedCode, product: result.line.title || 'Neznámy produkt', sku: result.line.product_code || result.line.scm || '', received: result.line.received_qty, expected: result.line.ordered_qty, status: result.status }
        : { code: scannedCode, product: 'Nenájdené na faktúre', sku: '', received: 0, expected: 0, status: result.status });
      setStats(result.summary);
      if (result.line) {
        setLines(prev => prev.map(l => l.scm === result.line?.scm || l.ean === result.line?.ean ? { ...l, ...result.line } : l));
      }
      setScannedCode(''); setQuantity(1);
    } catch { setError('Chyba pri skenovaní'); } finally { setLoading(false); inputRef.current?.focus(); }
  };

  const doFinalize = async () => {
    if (!sessionId) return;
    setFinalizing(true); setError(null); setShowConfirm(false);
    try {
      if (invoiceNote.trim()) await saveNote(invoiceNote.trim());
      await saveReceivedItems();
      const result = await finalizeReceiving(supplier, sessionId);
      setFinalizeResult(result);
      setTimeout(() => setShowCsvModal(true), 2000);
    } catch { setError('Nepodarilo sa dokončiť príjem. Skúste to znova.'); setFinalizing(false); }
  };

  const openEditModal = (index: number, line: ReceivingLine) => { setEditingLine({ index, line }); setEditQty(line.received_qty.toString()); setEditNote(''); };

  const handleSaveEdit = async () => {
    if (!editingLine || !sessionId) return;
    setSavingEdit(true); setError(null);
    try {
      const result = await setLineQuantity(supplier, sessionId, editingLine.index, parseFloat(editQty) || 0, editNote || undefined);
      setLines(prev => prev.map((l, i) => i === editingLine.index ? result.line : l));
      setStats(result.summary); setEditingLine(null);
    } catch { setError('Nepodarilo sa uložiť zmenu'); } finally { setSavingEdit(false); }
  };

  const handleAcceptAll = async () => {
    if (!sessionId) return; setBulkLoading(true);
    try { const r = await acceptAllItems(supplier, sessionId, true); setLines(r.lines); setStats(r.summary); }
    catch { setError('Nepodarilo sa prijať všetky položky'); } finally { setBulkLoading(false); }
  };

  const handleResetAll = async () => {
    if (!sessionId || !confirm('Naozaj chcete vynulovať všetky prijaté množstvá?')) return;
    setBulkLoading(true);
    try { const r = await resetAllItems(supplier, sessionId); setLines(r.lines); setStats(r.summary); }
    catch { setError('Nepodarilo sa vynulovať množstvá'); } finally { setBulkLoading(false); }
  };

  const sc: Record<string, { bg: string; border: string; icon: string; label: string }> = {
    matched:    { bg: 'var(--color-success-subtle)', border: 'var(--color-success)', icon: '✓', label: 'Nájdené' },
    partial:    { bg: 'var(--color-info-subtle)',    border: 'var(--color-info)',    icon: '◐', label: 'Čiastočne' },
    overage:    { bg: 'var(--color-warning-subtle)', border: 'var(--color-warning)', icon: '!', label: 'Prebytok' },
    unexpected: { bg: 'var(--color-error-subtle)',   border: 'var(--color-error)',   icon: '?', label: 'Nenájdené' },
    unknown:    { bg: 'var(--color-error-subtle)',   border: 'var(--color-error)',   icon: '?', label: 'Neznáme' },
    pending:    { bg: 'var(--color-bg-tertiary)',    border: 'var(--color-border-subtle)', icon: '○', label: 'Čaká' },
  };

  return (
    <div className="space-y-6">

      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-4">
          <button onClick={() => navigate('/receiving')} className="p-2 rounded-lg transition-colors"
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
        <Button variant="danger" onClick={() => navigate('/receiving')}><X size={16} /> Ukončiť</Button>
      </div>

      {error && <div className="p-4 rounded-lg border" style={{ backgroundColor: 'var(--color-error-subtle)', borderColor: 'var(--color-error)', color: 'var(--color-error)' }}>{error}</div>}

      {/* Scan */}
      <div className="rounded-xl border p-8" style={{ backgroundColor: 'var(--color-bg-secondary)', borderColor: 'var(--color-border-subtle)' }}>
        <div className="max-w-md mx-auto text-center">
          <div className="w-20 h-20 rounded-2xl border-2 border-dashed flex items-center justify-center mx-auto"
            style={{ backgroundColor: 'var(--color-accent-subtle)', borderColor: 'var(--color-border-accent)', color: 'var(--color-accent)' }}>
            <Camera size={32} />
          </div>
          <h2 className="text-lg font-medium mt-4" style={{ color: 'var(--color-text-primary)' }}>Naskenuj čiarový kód</h2>
          <p className="text-sm mt-1" style={{ color: 'var(--color-text-tertiary)' }}>Použi skener alebo zadaj kód manuálne</p>
          <div className="mt-6 flex gap-2">
            <input ref={inputRef} type="text" value={scannedCode} onChange={e => setScannedCode(e.target.value)} onKeyDown={e => e.key === 'Enter' && handleScan()}
              placeholder="Zadaj EAN, SKU alebo kód produktu..." className="flex-1 py-3" style={{ fontFamily: 'var(--font-mono)' }} autoFocus disabled={loading} />
            <Button variant="primary" onClick={handleScan} loading={loading} disabled={!scannedCode.trim() || !sessionId}>Scan</Button>
          </div>
          <div className="flex items-center justify-center gap-4 mt-4">
            <label className="flex items-center gap-2 text-sm" style={{ color: 'var(--color-text-secondary)' }}>
              Množstvo:
              <input type="number" value={quantity} onChange={e => setQuantity(Number(e.target.value) || 1)} min={-999} step={1} className="w-20 text-center py-1" />
            </label>
          </div>
        </div>
      </div>

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
        <div className="rounded-xl border overflow-hidden" style={{ backgroundColor: 'var(--color-bg-secondary)', borderColor: 'var(--color-border-subtle)' }}>
          <div className="px-4 py-3 border-b flex items-center justify-between" style={{ borderColor: 'var(--color-border-subtle)' }}>
            <span className="text-sm font-medium" style={{ color: 'var(--color-text-secondary)' }}>{lines.length} položiek</span>
            <div className="flex gap-2">
              <button onClick={handleAcceptAll} disabled={bulkLoading || stats.pending === 0}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium disabled:opacity-50"
                style={{ backgroundColor: 'var(--color-success-subtle)', color: 'var(--color-success)' }}>
                <CheckCircle size={14} /> Prijať všetko ({stats.pending})
              </button>
              <button onClick={handleResetAll} disabled={bulkLoading || (stats.matched === 0 && stats.partial === 0)}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium disabled:opacity-50"
                style={{ backgroundColor: 'var(--color-bg-tertiary)', color: 'var(--color-text-secondary)' }}>
                <RotateCcw size={14} /> Resetovať
              </button>
            </div>
          </div>
          <div className="max-h-96 overflow-y-auto">
            <table className="w-full">
              <thead>
                <tr className="border-b sticky top-0" style={{ backgroundColor: 'var(--color-bg-primary)', borderColor: 'var(--color-border-subtle)' }}>
                  <th className="text-left px-4 py-2 text-xs font-medium" style={{ color: 'var(--color-text-tertiary)' }}>SKU</th>
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
                      <td className="px-4 py-2 text-sm" style={{ color: 'var(--color-text-primary)' }}>
                        <div className="truncate max-w-xs" title={line.title}>{line.title}</div>
                      </td>
                      <td className="px-4 py-2 text-sm text-right" style={{ color: 'var(--color-text-secondary)' }}>{line.ordered_qty}</td>
                      <td className="px-4 py-2 text-sm text-right font-medium" style={{ color: line.received_qty >= line.ordered_qty ? 'var(--color-success)' : line.received_qty > 0 ? 'var(--color-info)' : 'var(--color-text-primary)' }}>
                        {line.received_qty}
                      </td>
                      <td className="px-4 py-2 text-center">
                        {partial ? (
                          <button onClick={() => toggleDone(line)} title={done ? 'Hotovo (odznač)' : 'Označiť ako hotovo'}
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
                      <td className="px-4 py-2 text-center cursor-pointer" onClick={() => openEditModal(idx, line)}>
                        <Edit2 size={14} style={{ color: 'var(--color-text-tertiary)' }} />
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
      {editingLine && (
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
                <input type="number" value={editQty} onChange={e => setEditQty(e.target.value)} className="flex-1 py-2 text-center text-lg" style={{ fontFamily: 'var(--font-mono)' }} min={0} step={1} autoFocus />
                <button onClick={() => setEditQty(editingLine.line.ordered_qty.toString())} className="px-3 py-2 rounded-lg text-sm font-medium" style={{ backgroundColor: 'var(--color-accent-subtle)', color: 'var(--color-accent)' }}>= {editingLine.line.ordered_qty}</button>
              </div>
            </div>
            <div className="mt-4">
              <label className="text-sm font-medium" style={{ color: 'var(--color-text-secondary)' }}>Poznámka (voliteľné)</label>
              <input type="text" value={editNote} onChange={e => setEditNote(e.target.value)} placeholder="Napr. poškodené, chýba v dodávke..." className="w-full mt-2 py-2" />
            </div>
            <div className="flex justify-end gap-3 mt-6">
              <Button variant="secondary" onClick={() => setEditingLine(null)}>Zrušiť</Button>
              <Button variant="primary" onClick={handleSaveEdit} loading={savingEdit}>Uložiť</Button>
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
        <textarea value={invoiceNote} onChange={e => setInvoiceNote(e.target.value)} onBlur={e => saveNote(e.target.value.trim())}
          placeholder="Napr. 3 položky nedodané, reklamácia na ..." className="w-full py-2 px-3 rounded-lg text-sm resize-none" rows={2}
          style={{ backgroundColor: 'var(--color-bg-primary)', border: '1px solid var(--color-border-subtle)', color: 'var(--color-text-primary)' }} />
      </div>

      {/* Actions */}
      <div className="flex justify-between">
        <Button variant="secondary" onClick={() => setShowLines(!showLines)}>
          <List size={16} /> {showLines ? 'Skryť položky' : 'Zobraziť položky'}
        </Button>
        <Button variant="success" onClick={() => stats.pending > 0 || stats.partial > 0 ? setShowConfirm(true) : doFinalize()} loading={finalizing} disabled={!sessionId || finalizing}>
          <Check size={16} /> Dokončiť príjem
        </Button>
      </div>

      {/* Confirm modal */}
      {showConfirm && (
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
                  Čiastočne prijaté bez „Hotovo" → záložka <strong>Nespracované</strong>.
                </p>
                <div className="mt-4">
                  <label className="text-sm font-medium" style={{ color: 'var(--color-text-secondary)' }}>Poznámka</label>
                  <textarea value={invoiceNote} onChange={e => setInvoiceNote(e.target.value)} className="w-full mt-2 py-2 px-3 rounded-lg text-sm resize-none" rows={2}
                    style={{ backgroundColor: 'var(--color-bg-primary)', border: '1px solid var(--color-border-subtle)', color: 'var(--color-text-primary)' }} />
                </div>
              </div>
            </div>
            <div className="flex justify-end gap-3 mt-6">
              <Button variant="secondary" onClick={() => setShowConfirm(false)}>Pokračovať v skenovaní</Button>
              <Button variant="primary" onClick={doFinalize}>Áno, dokončiť</Button>
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
            <p className="text-xs mt-4" style={{ color: 'var(--color-text-tertiary)' }}>Načítavam výsledky...</p>
          </div>
        </div>
      )}

      {showCsvModal && (
        <ReceivingResultsModal supplier={supplier} invoiceId={`${supplier}:${invoiceId}`} onClose={() => { setShowCsvModal(false); navigate('/receiving'); }} />
      )}
    </div>
  );
}

export default ReceivingSessionPage;
