// src/components/UpgatesImportModal.tsx
// "Stiahnuť z Upgates" — preview new products in the shop, select, import into local DB.
import React, { useEffect, useMemo, useState } from 'react';
import { X, Download, RefreshCw } from 'lucide-react';
import { Button } from './ui/Button.new';
import {
  getUpgatesPreview,
  importUpgatesProducts,
  type UpgatesPreview,
  type UpgatesImportResult,
} from '../api/upgates';

interface Props {
  shop: string;              // e.g. "biketrek"
  onClose: () => void;
  onImported: () => void;    // parent refreshes stock data
}

export function UpgatesImportModal({ shop, onClose, onImported }: Props) {
  const [preview, setPreview] = useState<UpgatesPreview | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [importing, setImporting] = useState(false);
  const [result, setResult] = useState<UpgatesImportResult | null>(null);

  const loadPreview = async () => {
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const data = await getUpgatesPreview(shop);
      setPreview(data);
      setSelected(new Set(data.new_products.map((p) => p.code)));
    } catch (e: any) {
      setError(e?.message || 'Nepodarilo sa načítať produkty z Upgates');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { loadPreview(); /* eslint-disable-next-line */ }, [shop]);

  const allChecked = useMemo(
    () => !!preview && preview.new_products.length > 0 && selected.size === preview.new_products.length,
    [preview, selected],
  );

  const toggle = (code: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(code)) next.delete(code); else next.add(code);
      return next;
    });
  };

  const toggleAll = () => {
    if (!preview) return;
    setSelected(allChecked ? new Set() : new Set(preview.new_products.map((p) => p.code)));
  };

  const handleImport = async () => {
    if (selected.size === 0) return;
    setImporting(true);
    setError(null);
    try {
      const res = await importUpgatesProducts(shop, Array.from(selected));
      setResult(res);
      onImported();
      await loadPreview();
    } catch (e: any) {
      setError(e?.message || 'Import zlyhal');
    } finally {
      setImporting(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-6"
      style={{ backgroundColor: 'rgba(0,0,0,0.6)' }}
      onClick={onClose}
    >
      <div
        className="rounded-xl border w-full max-w-3xl max-h-[85vh] flex flex-col"
        style={{ backgroundColor: 'var(--color-bg-secondary)', borderColor: 'var(--color-border-subtle)' }}
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div
          className="flex items-center justify-between px-5 py-4 border-b"
          style={{ borderColor: 'var(--color-border-subtle)' }}
        >
          <div>
            <h2 className="text-lg font-semibold" style={{ color: 'var(--color-text-primary)' }}>
              Stiahnuť z Upgates
            </h2>
            <p className="text-xs mt-0.5" style={{ color: 'var(--color-text-tertiary)' }}>
              Shop: {shop}
              {preview && ` · v Upgates: ${preview.total_in_upgates} · už v databáze: ${preview.already_in_db} · nové: ${preview.new_count}`}
            </p>
          </div>
          <button onClick={onClose} style={{ color: 'var(--color-text-tertiary)' }}>
            <X size={18} />
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-auto px-5 py-4">
          {loading && (
            <div className="py-10 text-center animate-pulse" style={{ color: 'var(--color-text-tertiary)' }}>
              Načítavam produkty z Upgates…
            </div>
          )}

          {!loading && error && (
            <div className="py-8 text-center text-sm" style={{ color: 'var(--color-error)' }}>
              {error}
              <div className="mt-3">
                <Button variant="secondary" size="sm" onClick={loadPreview}>
                  <RefreshCw size={14} /> Skúsiť znovu
                </Button>
              </div>
            </div>
          )}

          {!loading && !error && result && (
            <div
              className="mb-4 rounded-lg border px-4 py-3 text-sm"
              style={{ borderColor: 'var(--color-success)', color: 'var(--color-success)' }}
            >
              {result.message}
              {result.skipped.length > 0 && (
                <span style={{ color: 'var(--color-text-tertiary)' }}>
                  {' '}(preskočených: {result.skipped.length})
                </span>
              )}
            </div>
          )}

          {!loading && !error && preview && preview.new_products.length === 0 && (
            <div className="py-10 text-center text-sm" style={{ color: 'var(--color-text-tertiary)' }}>
              Žiadne nové produkty — všetko z Upgates už je v lokálnej databáze. 🎉
            </div>
          )}

          {!loading && !error && preview && preview.new_products.length > 0 && (
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b" style={{ borderColor: 'var(--color-border-subtle)' }}>
                  <th className="w-8 px-2 py-2">
                    <input type="checkbox" checked={allChecked} onChange={toggleAll} />
                  </th>
                  <th className="text-left px-2 py-2 text-xs uppercase" style={{ color: 'var(--color-text-tertiary)' }}>Kód</th>
                  <th className="text-left px-2 py-2 text-xs uppercase" style={{ color: 'var(--color-text-tertiary)' }}>Názov</th>
                  <th className="text-left px-2 py-2 text-xs uppercase" style={{ color: 'var(--color-text-tertiary)' }}>Výrobca</th>
                  <th className="text-right px-2 py-2 text-xs uppercase" style={{ color: 'var(--color-text-tertiary)' }}>Varianty</th>
                </tr>
              </thead>
              <tbody className="divide-y" style={{ borderColor: 'var(--color-border-subtle)' }}>
                {preview.new_products.map((p) => (
                  <tr key={p.code} className="cursor-pointer" onClick={() => toggle(p.code)}>
                    <td className="px-2 py-2">
                      <input
                        type="checkbox"
                        checked={selected.has(p.code)}
                        onChange={() => toggle(p.code)}
                        onClick={(e) => e.stopPropagation()}
                      />
                    </td>
                    <td className="px-2 py-2" style={{ fontFamily: 'var(--font-mono)', color: 'var(--color-accent)' }}>
                      {p.code}
                    </td>
                    <td className="px-2 py-2" style={{ color: 'var(--color-text-primary)' }}>{p.title}</td>
                    <td className="px-2 py-2" style={{ color: 'var(--color-text-secondary)' }}>{p.manufacturer}</td>
                    <td className="px-2 py-2 text-right" style={{ color: 'var(--color-text-secondary)' }}>
                      {p.variants_count || '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        {/* Footer */}
        <div
          className="flex items-center justify-between px-5 py-4 border-t"
          style={{ borderColor: 'var(--color-border-subtle)' }}
        >
          <span className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
            Importujú sa len identifikačné údaje (kódy, EAN, varianty). Popisy a ceny zostávajú v Upgates.
          </span>
          <Button
            variant="primary"
            onClick={handleImport}
            disabled={importing || loading || selected.size === 0}
          >
            <Download size={16} />
            {importing ? 'Importujem…' : `Importovať vybrané (${selected.size})`}
          </Button>
        </div>
      </div>
    </div>
  );
}
