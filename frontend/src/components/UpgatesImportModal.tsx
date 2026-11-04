// src/components/UpgatesImportModal.tsx
// "Stiahnuť z Upgates" — preview new products in the shop, select, import into local DB.
import React, { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { X, Download, RefreshCw } from 'lucide-react';
import { Button } from './ui/Button.new';
import {
  getUpgatesPreview,
  importUpgatesProducts,
  type UpgatesPreview,
  type UpgatesImportResult,
  type UpgatesIdentityConflict,
  type UpgatesNewProduct,
  type UpgatesLeafSelection,
} from '../api/upgates';

interface Props {
  shop: string;              // e.g. "biketrek"
  onClose: () => void;
  onImported: () => void;    // parent refreshes stock data
}

type ImportRow = UpgatesNewProduct & { leafSelection?: UpgatesLeafSelection; parentCode?: string; ean?: string };

function selectionRows(preview: UpgatesPreview | null, leaves: boolean, existingOnly: boolean): ImportRow[] {
  const families = preview?.new_products || [];
  const rows: ImportRow[] = leaves ? families.flatMap(p => (p.leaves || []).map(leaf => ({
    ...p, key: leaf.selection.code, code: leaf.selection.code,
    title: `${p.title} – ${leaf.title}`, variants_count: 0,
    identity_status: leaf.identity_status,
    linkable_existing_count: leaf.identity_status === 'identified' ? 1 : 0,
    new_card_count: leaf.identity_status === 'new' ? 1 : 0,
    leafSelection: leaf.selection, parentCode: p.code, ean: leaf.ean,
  }))) : families;
  return rows.filter(p => !existingOnly || (p.linkable_existing_count ?? 0) > 0);
}

function IdentityConflicts({ title, count, conflicts }: { title: string; count: number; conflicts: UpgatesIdentityConflict[] }) {
  const { t } = useTranslation();
  if (!count) return null;
  return (
    <div role="alert" className="mb-4 rounded-lg border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-sm">
      <h3 className="font-medium">{title} · {t('upgatesImport.conflictCount', { count })}</h3>
      <p className="mt-1">{t('upgatesImport.conflictsHelp')}</p>
      <ul className="mt-3 space-y-3">
        {conflicts.map((conflict, index) => (
          <li key={`${conflict.code}-${index}`}>
            <strong style={{ fontFamily: 'var(--font-mono)' }}>{conflict.code}</strong>
            <ul className="list-disc pl-5">
              {conflict.reasons.map(reason => <li key={reason}>{t(`upgatesImport.reasons.${reason}`, { defaultValue: t('upgatesImport.unknownConflict', { reason }) })}</li>)}
            </ul>
            {!!conflict.candidate_product_ids?.length && <p className="text-xs mt-1">{t('upgatesImport.candidates', { ids: conflict.candidate_product_ids.join(', ') })}</p>}
          </li>
        ))}
      </ul>
    </div>
  );
}

export function UpgatesImportModal({ shop, onClose, onImported }: Props) {
  const { t } = useTranslation();
  const [preview, setPreview] = useState<UpgatesPreview | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [codeSelectionText, setCodeSelectionText] = useState('');
  const [codeSelectionReport, setCodeSelectionReport] = useState<{ matched: number; unavailable: string[] } | null>(null);
  const [importing, setImporting] = useState(false);
  const [result, setResult] = useState<UpgatesImportResult | null>(null);
  const [updateExisting, setUpdateExisting] = useState(false);
  const [existingOnly, setExistingOnly] = useState(false);
  const [selectLeaves, setSelectLeaves] = useState(false);
  const selectableProducts = useMemo(() => selectionRows(preview, selectLeaves, existingOnly),
    [preview, selectLeaves, existingOnly]);
  const selectedExistingCount = selectableProducts.filter(p => selected.has(p.key))
    .reduce((sum, p) => sum + (p.linkable_existing_count ?? 0), 0);
  const previewConflictCount = preview?.conflict_count ?? preview?.conflicts?.length ?? 0;
  const resultConflictCount = result?.conflict_count ?? result?.conflicts?.length ?? 0;
  const codeSelectionPending = !!codeSelectionText.trim() && !codeSelectionReport;

  const clearCodeSelection = () => {
    setCodeSelectionText('');
    setCodeSelectionReport(null);
  };

  const loadPreview = async (refresh = false) => {
    setLoading(true);
    setError(null);
    clearCodeSelection();
    if (refresh) setResult(null);
    try {
      const data = await getUpgatesPreview(shop, refresh);
      setPreview(data);
      setSelected(new Set(selectLeaves ? [] : selectionRows(data, false, existingOnly).map(p => p.key)));
    } catch (e: any) {
      setError(e?.message || 'Nepodarilo sa načítať produkty z Upgates');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { loadPreview(false); /* eslint-disable-next-line */ }, [shop]);

  const allChecked = useMemo(
    () => selectableProducts.length > 0 && selected.size === selectableProducts.length,
    [selectableProducts, selected],
  );

  const toggle = (code: string) => {
    if (importing) return;
    clearCodeSelection();
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(code)) next.delete(code); else next.add(code);
      return next;
    });
  };

  const toggleAll = () => {
    if (!preview || importing) return;
    clearCodeSelection();
    setSelected(allChecked ? new Set() : new Set(selectableProducts.map((p) => p.key)));
  };

  const applyCodeSelection = () => {
    if (!preview || loading || importing || !codeSelectionText.trim()) return;
    const requested = new Set(codeSelectionText.split(/\r?\n/).map(code => code.trim()).filter(Boolean));
    const matching = selectableProducts.filter(product => requested.has(product.code));
    const matchedCodes = new Set(matching.map(product => product.code));
    setSelected(new Set(matching.map(product => product.key)));
    setCodeSelectionReport({ matched: matching.length, unavailable: [...requested].filter(code => !matchedCodes.has(code)) });
  };

  const handleImport = async () => {
    if (importing || loading || error || codeSelectionPending || (selected.size === 0 && !updateExisting)) return;
    setImporting(true);
    setError(null);
    try {
      const res = await importUpgatesProducts(shop, selectLeaves ? [] : Array.from(selected), {
        updateExisting,
        existingOnly,
        ...(selectLeaves ? { leaves: selectableProducts.filter(p => selected.has(p.key)).map(p => p.leafSelection!) } : {}),
      });
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
              {t('upgatesImport.title', { shop })}
            </h2>
            <p className="text-xs mt-0.5" style={{ color: 'var(--color-text-tertiary)' }}>
              {preview && t('upgatesImport.summary', { total: preview.total_in_upgates, known: preview.already_in_db, pending: preview.new_count })}
              {previewConflictCount > 0 && ` · ${t('upgatesImport.conflictCount', { count: previewConflictCount })}`}
              {preview && preview.without_any_code > 0 && ` · bez kódu: ${preview.without_any_code}`}
              {preview && (preview.catalog_source === 'cache'
                ? ` · katalóg z cache (${Math.round(preview.catalog_age_s / 60)} min)`
                : ' · katalóg čerstvo stiahnutý')}
            </p>
          </div>
          <div className="flex items-center gap-3">
            <Button
              variant="secondary"
              size="sm"
              onClick={() => loadPreview(true)}
              disabled={loading || importing}
              title="Stiahne čerstvý katalóg z Upgates API (~1 call na 100 produktov)"
            >
              <RefreshCw size={14} /> Obnoviť z Upgates
            </Button>
            <button onClick={onClose} aria-label={t('common.close')} style={{ color: 'var(--color-text-tertiary)' }}>
              <X size={18} />
            </button>
          </div>
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
                <Button variant="secondary" size="sm" onClick={() => loadPreview()}>
                  <RefreshCw size={14} /> Skúsiť znovu
                </Button>
              </div>
            </div>
          )}

          {!loading && result && (
            <>
            <div
              className="mb-4 rounded-lg border px-4 py-3 text-sm"
              role="status"
              style={{ borderColor: resultConflictCount ? 'var(--color-warning)' : 'var(--color-success)', color: resultConflictCount ? 'var(--color-warning)' : 'var(--color-success)' }}
            >
              {t('upgatesImport.resultSummary', { created: result.created_products ?? 0, variants: result.created_variants ?? 0, linked: result.linked_products ?? 0, snapshots: result.content_saved ?? 0 })}
              {result.existing_only && <p>{t('upgatesImport.skippedUnresolved', { count: result.skipped_unresolved ?? 0 })}</p>}
              {result.skipped.length > 0 && (
                <span style={{ color: 'var(--color-text-tertiary)' }}>
                  {' '}(preskočených: {result.skipped.length})
                </span>
              )}
            </div>
            <IdentityConflicts title={t('upgatesImport.importConflicts')} count={resultConflictCount} conflicts={result.conflicts || []} />
            </>
          )}

          {!loading && !error && preview && <IdentityConflicts title={t('upgatesImport.previewConflicts')} count={previewConflictCount} conflicts={preview.conflicts || []} />}

          {!loading && !error && preview && selectableProducts.length === 0 && (
            <div className="py-10 text-center text-sm" style={{ color: 'var(--color-text-tertiary)' }}>
              {t(existingOnly ? 'upgatesImport.noExisting' : previewConflictCount > 0 ? 'upgatesImport.onlyConflicts' : 'upgatesImport.noPending')}
            </div>
          )}

          {!loading && !error && preview && selectableProducts.length > 0 && (
            <>
            <details className="mb-4 rounded-lg border p-3" style={{ borderColor: 'var(--color-border-subtle)' }}>
              <summary className="cursor-pointer text-sm font-medium">{t('upgatesImport.selectCodes')}</summary>
              <label className="mt-3 block text-sm" htmlFor="upgates-code-selection">{t('upgatesImport.codeList')}</label>
              <p id="upgates-code-selection-help" className="my-2 text-xs">{t('upgatesImport.codeListHelp')}</p>
              <textarea
                id="upgates-code-selection"
                aria-describedby="upgates-code-selection-help"
                rows={5}
                className="w-full rounded border p-2 text-sm"
                style={{ backgroundColor: 'var(--color-bg-secondary)', borderColor: 'var(--color-border-subtle)', fontFamily: 'var(--font-mono)' }}
                value={codeSelectionText}
                disabled={importing}
                onChange={event => { setCodeSelectionText(event.target.value); setCodeSelectionReport(null); }}
              />
              <Button variant="secondary" size="sm" onClick={applyCodeSelection} disabled={importing || !codeSelectionText.trim()}>
                {t('upgatesImport.applyCodeSelection')}
              </Button>
              {codeSelectionPending && <p className="mt-2 text-xs">{t('upgatesImport.applyCodeSelectionFirst')}</p>}
              {codeSelectionReport && <div role="status" className="mt-2 text-sm">
                <p>{t('upgatesImport.codeSelectionResult', { count: codeSelectionReport.matched })}</p>
                {codeSelectionReport.unavailable.length > 0 && <>
                  <p>{t('upgatesImport.codesUnavailable', { count: codeSelectionReport.unavailable.length })}</p>
                  <pre className="mt-1 max-h-32 overflow-auto whitespace-pre-wrap break-words text-xs">{codeSelectionReport.unavailable.join('\n')}</pre>
                </>}
              </div>}
            </details>
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b" style={{ borderColor: 'var(--color-border-subtle)' }}>
                  <th className="w-8 px-2 py-2">
                    <input type="checkbox" aria-label={t('catalog.selectPage')} checked={allChecked} onChange={toggleAll} disabled={importing} />
                  </th>
                  <th className="text-left px-2 py-2 text-xs uppercase" style={{ color: 'var(--color-text-tertiary)' }}>Kód</th>
                  <th className="text-left px-2 py-2 text-xs uppercase" style={{ color: 'var(--color-text-tertiary)' }}>Názov</th>
                  <th className="text-left px-2 py-2 text-xs uppercase" style={{ color: 'var(--color-text-tertiary)' }}>Výrobca</th>
                  <th className="text-left px-2 py-2 text-xs uppercase" style={{ color: 'var(--color-text-tertiary)' }}>{t('upgatesImport.identity')}</th>
                  <th className="text-right px-2 py-2 text-xs uppercase" style={{ color: 'var(--color-text-tertiary)' }}>Varianty</th>
                </tr>
              </thead>
              <tbody className="divide-y" style={{ borderColor: 'var(--color-border-subtle)' }}>
                {selectableProducts.map((p) => (
                  <tr key={p.key} className="cursor-pointer" onClick={() => toggle(p.key)}>
                    <td className="px-2 py-2">
                      <input
                        type="checkbox"
                        aria-label={t('catalog.selectProduct', { name: p.code })}
                        checked={selected.has(p.key)}
                        disabled={importing}
                        onChange={() => toggle(p.key)}
                        onClick={(e) => e.stopPropagation()}
                      />
                    </td>
                    <td className="px-2 py-2" style={{ fontFamily: 'var(--font-mono)', color: 'var(--color-accent)' }}>
                      {p.code}
                      {p.parentCode && <p className="text-xs">{t('upgatesImport.leafParent', { code: p.parentCode })}</p>}
                      {p.ean && <p className="text-xs">EAN: {p.ean}</p>}
                    </td>
                    <td className="px-2 py-2" style={{ color: 'var(--color-text-primary)' }}>{p.title}</td>
                    <td className="px-2 py-2" style={{ color: 'var(--color-text-secondary)' }}>{p.manufacturer}</td>
                    <td className="px-2 py-2 text-xs" style={{ color: 'var(--color-text-secondary)' }}>
                      {p.identity_status ? t(`upgatesImport.identityStatus.${p.identity_status}`) : '—'}
                      {p.linkable_existing_count !== undefined && <p>{t('upgatesImport.cardCounts', { linked: p.linkable_existing_count, created: p.new_card_count ?? 0 })}</p>}
                    </td>
                    <td className="px-2 py-2 text-right" style={{ color: 'var(--color-text-secondary)' }}>
                      {p.variants_count || '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            </>
          )}
        </div>

        {/* Footer */}
        <div
          className="flex items-center justify-between gap-4 px-5 py-4 border-t"
          style={{ borderColor: 'var(--color-border-subtle)' }}
        >
          <div className="flex flex-col gap-1.5 text-xs" style={{ color: 'var(--color-text-secondary)' }}>
            <p>{t('upgatesImport.productOnlyNote')}</p>
            <label className="flex items-center gap-2 cursor-pointer">
              <input
                type="checkbox"
                checked={updateExisting}
                disabled={loading || importing || existingOnly || selectLeaves}
                aria-describedby="upgates-refresh-snapshots-help"
                onChange={(e) => setUpdateExisting(e.target.checked)}
              />
              {t('upgatesImport.refreshSnapshots')}
            </label>
            <p id="upgates-refresh-snapshots-help">{t('upgatesImport.refreshSnapshotsHelp')}</p>
            <label className="flex items-center gap-2 cursor-pointer">
              <input type="checkbox" checked={existingOnly} disabled={loading || importing}
                onChange={e => {
                  const enabled = e.target.checked;
                  setExistingOnly(enabled);
                  if (enabled) setUpdateExisting(false);
                  clearCodeSelection();
                  setSelected(new Set(selectLeaves ? [] : selectionRows(preview, false, enabled).map(p => p.key)));
                }} />
              {t('upgatesImport.existingOnly')}
            </label>
            {existingOnly && <p role="status">{t('upgatesImport.existingSelection', { count: selectedExistingCount })}</p>}
            <label className="flex items-center gap-2 cursor-pointer">
              <input type="checkbox" checked={selectLeaves} disabled={loading || importing}
                onChange={e => {
                  setSelectLeaves(e.target.checked);
                  setUpdateExisting(false);
                  clearCodeSelection();
                  setSelected(new Set());
                }} />
              {t('upgatesImport.selectLeaves')}
            </label>
            {selectLeaves && <p role="status">{t('upgatesImport.selectLeavesHelp')}</p>}
          </div>
          <Button
            variant="primary"
            onClick={handleImport}
            disabled={importing || loading || !!error || codeSelectionPending || (selected.size === 0 && !updateExisting)}
          >
            <Download size={16} />
            {importing
              ? 'Importujem…'
              : updateExisting && selected.size === 0
              ? t('upgatesImport.refreshSnapshotsAction')
              : `Importovať vybrané (${selected.size})`}
          </Button>
        </div>
      </div>
    </div>
  );
}
