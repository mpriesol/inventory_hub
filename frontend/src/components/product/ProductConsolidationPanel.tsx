import React, { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { useTranslation } from 'react-i18next';
import { X } from 'lucide-react';
import { Button } from '../ui/Button.new';
import { ActionScope } from '../ui/ActionScope';
import { accessRevision, subscribeAccess } from '../../api/access';
import { ProductEditorRow } from '../../api/productEditor';
import { applyProductConsolidation, ConsolidationBlocker, ConsolidationCard, ConsolidationMapping,
  ConsolidationMappingAction, ConsolidationSelection, ConsolidationTarget, consolidationSourceUrl,
  discardProductConsolidation, forgetProductConsolidation, PendingProductConsolidation, pendingProductConsolidation,
  previewProductConsolidation, ProductConsolidationPreviewBody, ProductConsolidationReview,
  readConsolidationSelection, readProductConsolidation, rememberProductConsolidation } from '../../api/productConsolidation';
import './ProductConsolidationPanel.css';

type Busy = 'selection' | 'preview' | 'apply' | 'discard' | 'read';
type MappingDraft = { action: ConsolidationMappingAction['action']; code: string; parent: string;
  isVariant: boolean; productId: string; variantId: string };
const idsOf = (request: ProductConsolidationPreviewBody) => [request.keeper_product_id, ...request.retired_product_ids].sort((a, b) => a - b);
const sameIds = (left: number[], right: number[]) => JSON.stringify([...left].sort((a, b) => a - b)) === JSON.stringify([...right].sort((a, b) => a - b));
const positiveId = (value: string) => /^[1-9]\d*$/.test(value) && Number.isSafeInteger(Number(value));
const exactCode = (value: string) => !!value && value.length <= 100 && value === value.trim() && !/[;\u0000-\u001f\u007f]/.test(value);
const plainText = (value: string) => value.trim().replace(/[\t\r\n]+/g, ' ');
const shopName = (code: string) => code === 'biketrek' ? 'BIKETREK' : code === 'xtrek' ? 'xTrek' : code;
function defaultMapping(row: ConsolidationMapping, keeper: number): MappingDraft {
  const verified = !!row.external_product_id && positiveId(row.external_product_id) &&
    (!row.is_variant || !!row.external_variant_id && positiveId(row.external_variant_id));
  return { action: row.product_id === keeper && verified ? 'retain' : 'bind', code: row.external_code || '',
    parent: row.is_variant ? row.parent_code || '' : row.external_code || '', isVariant: row.is_variant,
    productId: row.external_product_id || '', variantId: row.external_variant_id || '' };
}
function mappingDraft(row: ConsolidationMapping, keeper: number, saved?: ConsolidationMappingAction): MappingDraft {
  const value = defaultMapping(row, keeper);
  if (!saved) return value;
  return { ...value, action: saved.action, ...(saved.target ? { code: saved.target.code, parent: saved.target.parent_code,
    isVariant: saved.target.is_variant, productId: String(saved.target.native_product_id),
    variantId: saved.target.native_variant_id == null ? '' : String(saved.target.native_variant_id) } : {}) };
}
function validMapping(draft: MappingDraft): boolean {
  if (draft.action === 'retire_absent' || draft.action === 'retire_unverified_code_absent') return true;
  return exactCode(draft.code) && positiveId(draft.productId) &&
    (!draft.isVariant || exactCode(draft.parent) && positiveId(draft.variantId));
}

export function ProductConsolidationPanel({ rows, onClose, onChanged }: {
  rows: ProductEditorRow[]; onClose: () => void; onChanged: () => void;
}) {
  const { t, i18n } = useTranslation();
  const c = (key: string, options: Record<string, unknown> = {}) => t(`productConsolidation.${key}`, options);
  const revision = useSyncExternalStore(subscribeAccess, accessRevision);
  const openedRevision = useRef(revision), initialPending = useRef(pendingProductConsolidation());
  const sessionChanged = openedRevision.current !== revision;
  const [pending, setPending] = useState(initialPending.current);
  const pendingRef = useRef(pending);
  const [selection, setSelection] = useState<ConsolidationSelection | null>(null);
  const [review, setReview] = useState<ProductConsolidationReview | null>(null);
  const [keeper, setKeeper] = useState(initialPending.current?.request.keeper_product_id || 0);
  const [mappings, setMappings] = useState<Record<number, MappingDraft>>({});
  const [unit, setUnit] = useState(initialPending.current?.request.identity_evidence.physical_unit || '');
  const [note, setNote] = useState(initialPending.current?.request.identity_evidence.review_note || '');
  const [sources, setSources] = useState(initialPending.current?.request.identity_evidence.source_urls.join('\n') || '');
  const [sameUnit, setSameUnit] = useState(!!initialPending.current);
  const [preserveHistory, setPreserveHistory] = useState(!!initialPending.current);
  const [keepReceiving, setKeepReceiving] = useState(initialPending.current?.request.preserve_keeper_invoice_receiving === true);
  const [supplierGtin, setSupplierGtin] = useState(initialPending.current?.request.use_keeper_supplier_gtin === true);
  const [confirmedPlan, setConfirmedPlan] = useState(false);
  const [uncertain, setUncertain] = useState<'preview' | 'apply' | 'discard' | null>(() =>
    initialPending.current ? initialPending.current.discard ? 'discard' : initialPending.current.apply ? 'apply' : 'preview' : null);
  const [preparedRecovery, setPreparedRecovery] = useState(false);
  const [missingPreview, setMissingPreview] = useState(false);
  const [busy, setBusy] = useState<Busy | null>(null), [error, setError] = useState('');
  const [clock, setClock] = useState(Date.now);
  const busyRef = useRef(false), mounted = useRef(true), bootstrapped = useRef(false);
  const selectionGeneration = useRef(0), notified = useRef(new Set<string>());
  const selectedIds = useRef(initialPending.current ? idsOf(initialPending.current.request) : rows.map(row => row.id));
  const selectionRequest = useRef<ProductConsolidationPreviewBody | undefined>(initialPending.current?.request);
  const dialog = useRef<HTMLElement | null>(null), closeRef = useRef(onClose), changedRef = useRef(onChanged);
  closeRef.current = onClose; changedRef.current = onChanged;
  const completed = review?.status === 'completed';
  const frozen = !!review || !!uncertain || !!pending?.apply || !!pending?.discard || !!busy || sessionChanged;
  const expired = !!review && (review.status === 'expired' || new Date(review.expires_at).getTime() <= clock);
  const originalScope = review ? [review.keeper_product_id, ...review.retired_product_ids]
    : pending ? idsOf(pending.request) : selectedIds.current;
  const scopeChanged = rows.length > 0 && !sameIds(rows.map(row => row.id), originalScope);
  const cards: ConsolidationCard[] = review ? review.products.map(row => ({ ...row,
    eans: row.identity?.eans || [], attributes: row.identity?.attributes || [],
    manual_eans: row.identity?.manual_eans || null, canonical_product_id: null })) : selection?.products || [];
  const activeKeeper = review?.keeper_product_id || keeper;
  const keeperCard = cards.find(card => card.id === activeKeeper);
  const sourceUrls = [...new Set(sources.split(/\r?\n/).map(value => value.trim()).filter(Boolean))].sort();
  const selectionValid = selectedIds.current.length >= 2 && selectedIds.current.length <= 10 &&
    new Set(selectedIds.current).size === selectedIds.current.length &&
    selectedIds.current.every(id => Number.isSafeInteger(id) && id > 0);
  const mappedRows = review?.existing_mappings || selection?.mappings || [];
  const formIssue = !selectionValid ? 'selectionHelp' : !selection ? 'selectionNeeded' :
    !keeper || !selection.products.some(card => card.id === keeper && card.is_active) ? 'chooseKeeper' :
    selection.blockers.length ? 'blockedHelp' :
    plainText(unit).length < 5 || plainText(unit).length > 500 ? 'unitHelp' :
    plainText(note).length < 20 || plainText(note).length > 3000 ? 'noteHelp' :
    !sourceUrls.length || sourceUrls.length > 10 || !sourceUrls.every(consolidationSourceUrl) ? 'sourcesHelp' :
    !sameUnit || !preserveHistory ? 'confirmHelp' :
    selection.mappings.some(row => !mappings[row.id] || !validMapping(mappings[row.id])) ? 'mappingIncomplete' : '';

  const reason = (code: string) => c(`errors.${code}`, { defaultValue: c('unknownReason', { code }) });
  function store(value: PendingProductConsolidation) {
    rememberProductConsolidation(value); pendingRef.current = value; setPending(value);
  }
  function clearSaved(id: string): boolean {
    try {
      forgetProductConsolidation(id); pendingRef.current = null; setPending(null); return true;
    } catch { setError('storage_unavailable'); return false; }
  }
  function restoreForm(request: ProductConsolidationPreviewBody) {
    selectedIds.current = idsOf(request); selectionRequest.current = request;
    setKeeper(request.keeper_product_id); setUnit(request.identity_evidence.physical_unit);
    setNote(request.identity_evidence.review_note); setSources(request.identity_evidence.source_urls.join('\n'));
    setSameUnit(true); setPreserveHistory(true);
    setKeepReceiving(request.preserve_keeper_invoice_receiving === true); setSupplierGtin(request.use_keeper_supplier_gtin === true);
  }
  async function loadSelection(ids = selectedIds.current, saved = selectionRequest.current, keepError = false) {
    if (busyRef.current || accessRevision() !== openedRevision.current || !mounted.current) return;
    if (ids.length < 2 || ids.length > 10 || new Set(ids).size !== ids.length || ids.some(id => !Number.isSafeInteger(id) || id <= 0)) {
      setError('consolidation_invalid_request'); return;
    }
    const access = accessRevision(), generation = ++selectionGeneration.current;
    busyRef.current = true; setBusy('selection'); setSelection(null);
    if (!keepError) setError('');
    try {
      const value = await readConsolidationSelection(ids);
      if (!mounted.current || access !== accessRevision() || generation !== selectionGeneration.current) return;
      if (!sameIds(value.products.map(row => row.id), ids) || !Array.isArray(value.mappings) ||
          !Array.isArray(value.blockers) || value.mappings.length > 50 ||
          new Set(value.mappings.map(row => row.id)).size !== value.mappings.length ||
          value.mappings.some(row => !ids.includes(row.product_id))) throw new Error('response_mismatch');
      setSelection(value); selectedIds.current = [...ids];
      const chosen = saved?.keeper_product_id || keeper;
      setMappings(Object.fromEntries(value.mappings.map(row => [row.id,
        mappingDraft(row, chosen, saved?.mapping_actions.find(action => action.mapping_id === row.id))])));
    } catch (failure: any) {
      if (mounted.current && access === accessRevision() && generation === selectionGeneration.current)
        setError(failure.code || (failure.message === 'response_mismatch' ? 'response_mismatch' : 'request_failed'));
    } finally {
      if (mounted.current && access === accessRevision() && generation === selectionGeneration.current) {
        busyRef.current = false; setBusy(null);
      }
    }
  }
  function validReview(value: ProductConsolidationReview, saved: PendingProductConsolidation) {
    return value.id === saved.request.request_id && value.keeper_product_id === saved.request.keeper_product_id &&
      sameIds(value.retired_product_ids, saved.request.retired_product_ids) && /^[0-9a-f]{64}$/.test(value.preview_hash) &&
      typeof value.eligible === 'boolean' && Number.isFinite(new Date(value.expires_at).getTime()) &&
      (!(saved.discard || saved.apply) || value.preview_hash === (saved.discard || saved.apply)!.preview_hash) &&
      ['prepared', 'expired', 'completed'].includes(value.status) &&
      sameIds(value.products.map(card => card.id), idsOf(saved.request)) &&
      (value.status !== 'completed' || !!value.result && value.result.keeper_product_id === saved.request.keeper_product_id &&
        sameIds(value.result.retired_product_ids, saved.request.retired_product_ids) &&
        value.result.physical_stock_changed === false && value.result.remote_write_performed === false);
  }
  async function perform(kind: 'preview' | 'apply' | 'discard' | 'read', retry = false) {
    if (busyRef.current || accessRevision() !== openedRevision.current) return;
    let saved = pendingRef.current;
    if (kind === 'preview' && !retry) {
      if (frozen || formIssue || !selection) return;
      const actions: ConsolidationMappingAction[] = selection.mappings.map(row => {
        const draft = mappings[row.id];
        const target: ConsolidationTarget | null = draft.action.startsWith('retire_') ? null : {
          shop_code: row.shop_code, code: draft.code, parent_code: draft.isVariant ? draft.parent : draft.code,
          is_variant: draft.isVariant, native_product_id: Number(draft.productId),
          ...(draft.isVariant ? { native_variant_id: Number(draft.variantId) } : {}), sale_to_stock_factor: 1,
        };
        return { mapping_id: row.id, action: draft.action, target };
      });
      const request: ProductConsolidationPreviewBody = { request_id: crypto.randomUUID(), keeper_product_id: keeper,
        retired_product_ids: selection.products.map(card => card.id).filter(id => id !== keeper).sort((a, b) => a - b),
        mode: 'identity_only_preserve_unconfirmed_history', same_physical_retail_unit_confirmed: true,
        legacy_initial_history_preserved: true,
        ...(keepReceiving ? { preserve_keeper_invoice_receiving: true } : {}),
        ...(supplierGtin ? { use_keeper_supplier_gtin: true } : {}),
        identity_evidence: { physical_unit: plainText(unit), review_note: plainText(note), source_urls: sourceUrls },
        mapping_actions: actions, additional_sales: [] };
      saved = { version: 1, request };
      try { store(saved); } catch { setError('storage_unavailable'); return; }
      selectionRequest.current = request; setReview(null); setConfirmedPlan(false); setMissingPreview(false);
    }
    if (!saved) return;
    if (kind === 'apply' && !retry) {
      if (uncertain || saved.apply || saved.discard || !review || review.status !== 'prepared' || !review.eligible ||
          review.dependencies.blockers.length || !confirmedPlan || new Date(review.expires_at).getTime() <= Date.now()) return;
      saved = { ...saved, apply: { preview_hash: review.preview_hash, confirmed: true } };
      try { store(saved); } catch { setError('storage_unavailable'); return; }
    }
    if (kind === 'apply' && (!saved.apply || saved.discard)) return;
    if (kind === 'discard' && !retry) {
      const stopPendingApply = uncertain === 'apply' && preparedRecovery && !!saved.apply;
      if ((uncertain && !stopPendingApply) || saved.discard || !review || review.status !== 'prepared') return;
      saved = { ...saved, discard: { preview_hash: review.preview_hash, confirmed: true } };
      try { store(saved); } catch { setError('storage_unavailable'); return; }
    }
    if (kind === 'discard' && !saved.discard) return;
    const access = accessRevision();
    let reload: ProductConsolidationPreviewBody | undefined;
    busyRef.current = true; setBusy(kind); setError(''); setPreparedRecovery(false);
    if (kind !== 'read') setUncertain(kind);
    try {
      const value = kind === 'preview' ? await previewProductConsolidation(saved.request)
        : kind === 'apply' ? await applyProductConsolidation(saved.request.request_id, saved.apply!)
        : kind === 'discard' ? await discardProductConsolidation(saved.request.request_id, saved.discard!)
        : await readProductConsolidation(saved.request.request_id);
      if (!mounted.current || access !== accessRevision()) return;
      if (!validReview(value, saved)) throw new Error('response_mismatch');
      setMissingPreview(false); setClock(Date.now()); restoreForm(saved.request); setConfirmedPlan(false);
      if (value.status === 'completed') {
        setReview(value); clearSaved(value.id); setUncertain(null);
        if (!notified.current.has(value.id)) { notified.current.add(value.id); changedRef.current(); }
      } else if (value.status === 'expired') {
        // Persisted expiry from apply/discard uses the backend's identity and
        // review-row fences. An elapsed expires_at alone has no such meaning.
        if (clearSaved(value.id)) {
          setReview(null); setUncertain(null); setError(saved.discard ? '' : 'consolidation_preview_expired'); reload = saved.request;
        }
      } else {
        setReview(value);
        // A GET can observe "prepared" while a formerly sent apply is still
        // in flight. Only completion, a persisted expiry or the explicit fence
        // marker below settles that request.
        setUncertain(saved.discard ? 'discard' : saved.apply ? 'apply' : null);
        setPreparedRecovery(kind === 'read' && !!saved.apply && !saved.discard);
      }
    } catch (failure: any) {
      if (!mounted.current || access !== accessRevision()) return;
      const code = failure.code || (failure.message === 'response_mismatch' ? 'response_mismatch' : 'request_failed');
      setError(code);
      if (kind === 'read' && !saved.apply && !saved.discard && code === 'consolidation_not_found') setMissingPreview(true);
      const rejectedPreview = kind === 'preview' && !retry && failure.status >= 400 && failure.status < 500 &&
        failure.status !== 429 && !['consolidation_request_changed', 'product_links_rate_limited', 'source_rate_limited'].includes(code);
      // A preview retry can fail while the original request is still completing
      // its native read. Its own rollback is not a fence on that first request.
      const fencedFailure = failure.knownUnapplied === true && (kind === 'apply' || kind === 'preview' && !retry);
      if ((!saved.discard && fencedFailure) || rejectedPreview) {
        if (clearSaved(saved.request.request_id)) {
          setUncertain(null); setReview(null); setConfirmedPlan(false); restoreForm(saved.request);
          if (kind === 'apply' || failure.knownUnapplied === true) reload = saved.request;
        }
      }
    } finally {
      if (mounted.current && access === accessRevision()) { busyRef.current = false; setBusy(null); }
    }
    if (reload && mounted.current && access === accessRevision()) await loadSelection(idsOf(reload), reload, true);
  }
  async function editReview() {
    // Prepared reviews themselves hold dependency references. Expire the old
    // server review before replacing its UUID; closing merely retains it.
    await perform('discard');
  }
  function changeKeeper(id: number) {
    if (frozen || !selection) return;
    setKeeper(id); setMappings(Object.fromEntries(selection.mappings.map(row => [row.id, defaultMapping(row, id)])));
  }
  function changeMapping(id: number, change: Partial<MappingDraft>) {
    if (frozen) return;
    setMappings(previous => ({ ...previous, [id]: { ...previous[id], ...change } }));
  }
  useEffect(() => {
    mounted.current = true;
    const previousFocus = document.activeElement as HTMLElement;
    dialog.current?.querySelector<HTMLElement>('button')?.focus();
    if (!bootstrapped.current) {
      bootstrapped.current = true;
      if (pendingRef.current) void perform('read');
      else if (selectionValid) void loadSelection();
    }
    const keyboard = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !busyRef.current) { event.preventDefault(); closeRef.current(); }
      if (event.key !== 'Tab') return;
      const focusable = Array.from(dialog.current?.querySelectorAll<HTMLElement>(
        'button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), summary, a[href]') || [])
        .filter(node => {
          for (let ancestor = node.parentElement; ancestor && ancestor !== dialog.current; ancestor = ancestor.parentElement)
            if (ancestor.tagName === 'DETAILS' && !ancestor.hasAttribute('open') && ancestor.querySelector('summary') !== node) return false;
          return !node.hidden && node.getAttribute('type') !== 'hidden';
        });
      const first = focusable[0], last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    };
    document.addEventListener('keydown', keyboard);
    return () => { mounted.current = false; document.removeEventListener('keydown', keyboard); previousFocus?.focus(); };
  }, []);
  useEffect(() => {
    if (!sessionChanged) return;
    selectionGeneration.current += 1; busyRef.current = false; setBusy(null); setSelection(null); setReview(null);
  }, [sessionChanged]);
  useEffect(() => {
    if (!review || review.status !== 'prepared') return;
    const delay = new Date(review.expires_at).getTime() - Date.now();
    if (!Number.isFinite(delay) || delay <= 0) return;
    const timer = window.setTimeout(() => setClock(Date.now()), Math.min(delay + 50, 2147483647));
    return () => window.clearTimeout(timer);
  }, [review]);

  function blockerList(blockers: ConsolidationBlocker[]) {
    return <div data-testid="consolidation-blockers" className="consolidation-blockers" role="alert">
      <strong>{c('blocked')}</strong><p>{c('blockedHelp')}</p><ul>{blockers.map((item, index) =>
        <li key={`${item.code}-${index}`}>{reason(item.code)}
          {item.product_id && <small>{c('cardReference', { id: item.product_id })}</small>}
          {item.mapping_id && <small>{c('mappingReference', { id: item.mapping_id })}</small>}
          {item.shop_code && <small>{shopName(item.shop_code)}</small>}
          {item.count !== undefined && <small>{c('affectedCount', { count: item.count })}</small>}
        </li>)}</ul>
    </div>;
  }
  const reviewActions = review?.plan.actions || [];
  const canApply = !!review && review.status === 'prepared' && review.eligible && !review.dependencies.blockers.length &&
    !expired && !busy && !uncertain && !pending?.apply && !pending?.discard && confirmedPlan && !sessionChanged;
  return <div className="product-editor-modal-backdrop"><section ref={dialog} role="dialog" aria-modal="true"
    aria-labelledby="consolidation-title" className="product-editor-detail consolidation-panel">
    <header><div><h2 id="consolidation-title">{c('title')}</h2><p className="product-editor-muted">{c('intro')}</p></div>
      <button type="button" data-testid="consolidation-close" disabled={!!busy && !sessionChanged} aria-label={c('close')} onClick={onClose}><X size={20} /></button></header>
    <div className="product-editor-detail-body">
      {sessionChanged ? <p role="alert" className="product-editor-error">{c('errors.access_changed')}</p> : <>
      {scopeChanged && <p className="product-editor-alert" data-testid="consolidation-recovered-scope">{c('recoveredScope')}</p>}
      {!selectionValid && <p role="alert" className="product-editor-error">{c('selectionHelp')}</p>}
      {error && <p role="alert" data-testid="consolidation-error" className="product-editor-error">{reason(error)}</p>}
      {uncertain && <div className="product-editor-alert" data-testid="consolidation-recovery">
        <p>{c(uncertain === 'apply' ? 'applyUnknown' : uncertain === 'discard' ? 'discardUnknown' : 'previewUnknown')}</p>
        <div className="consolidation-actions"><Button variant="secondary" data-testid="consolidation-recover" disabled={!!busy} onClick={() => perform('read')}>{c('readResult')}</Button>
          {uncertain === 'apply' && pending?.apply && <Button variant="secondary" data-testid="consolidation-retry-apply" disabled={!!busy} onClick={() => perform('apply', true)}>{c('retryApply')}</Button>}
          {uncertain === 'apply' && preparedRecovery && review?.status === 'prepared' && <Button variant="secondary" data-testid="consolidation-discard-pending" disabled={!!busy} onClick={() => perform('discard')}>{c('stopPending')}</Button>}
          {uncertain === 'discard' && pending?.discard && <Button variant="secondary" data-testid="consolidation-retry-discard" disabled={!!busy} onClick={() => perform('discard', true)}>{c('retryDiscard')}</Button>}
          {uncertain === 'preview' && missingPreview && <Button variant="secondary" data-testid="consolidation-retry-preview" disabled={!!busy} onClick={() => perform('preview', true)}>{c('retryPreview')}</Button>}
        </div>{uncertain === 'apply' && preparedRecovery && <p>{c('stopPendingHelp')}</p>}<small>{c('recoverySaved')}</small>
      </div>}
      {completed && <div role="status" className="consolidation-success" data-testid="consolidation-completed">
        <h3>{c('completed', { sku: keeperCard?.sku || String(review!.keeper_product_id) })}</h3>
        <p>{c('completedDetail', { count: review!.retired_product_ids.length })}</p><p>{c('stockPreserved')}</p>
        <small>{c('operationId')} <code>{review!.id}</code></small>
      </div>}
      {!!cards.length && <>
        <h3>{c(completed ? 'resultCards' : 'chooseKeeper')}</h3>
        <div className="consolidation-cards">{cards.map(card => <label key={card.id} className={`consolidation-card${card.id === activeKeeper ? ' consolidation-card-selected' : ''}`} data-testid={`consolidation-card-${card.id}`}>
          <div className="consolidation-card-heading"><input type="radio" name="consolidation-keeper" data-testid={`consolidation-keeper-${card.id}`}
            aria-label={c('keepCard', { sku: card.sku })} checked={card.id === activeKeeper}
            disabled={frozen || !card.is_active} onChange={() => changeKeeper(card.id)} />
            <strong><code>{card.sku}</code></strong><span>{card.id === activeKeeper ? c('keeper') : completed ? c('retired') : c('duplicate')}</span></div>
          <div>{card.name}</div><dl><dt>{c('barcode')}</dt><dd>{card.eans.join(', ') || c('noBarcode')}</dd>
            {!!card.manual_eans?.length && <><dt>{c('manualBarcode')}</dt><dd>{card.manual_eans.join(', ')}</dd></>}
            {card.attributes.map((attribute, index) => <React.Fragment key={`${attribute.name}-${index}`}><dt>{attribute.name}</dt><dd>{attribute.value}</dd></React.Fragment>)}
            <dt>{c('cardId')}</dt><dd>{card.id}</dd></dl>
          {!card.is_active && <small>{c('inactive')}</small>}
        </label>)}</div>
      </>}
      {selection?.blockers.length && !review ? blockerList(selection.blockers) : null}
      {!review && !uncertain && !selection && selectionValid && !busy && <Button variant="secondary" data-testid="consolidation-selection-retry" onClick={() => loadSelection()}>{c('reloadSelection')}</Button>}
      {!!mappedRows.length && <section className="consolidation-section"><h3>{c('mappingsTitle')}</h3><p className="product-editor-muted">{c('mappingsHelp')}</p>
        <div className="consolidation-mappings">{mappedRows.map(row => {
          const draft = mappings[row.id], action = reviewActions.find(value => value.mapping_id === row.id);
          return <article key={row.id} className="consolidation-mapping" data-testid={`consolidation-mapping-${row.id}`}>
            <div className="consolidation-mapping-heading"><strong>{shopName(row.shop_code)}</strong><code>{row.external_code || c('missingCode')}</code>
              <span>{c(`actions.${action?.action || draft?.action || 'bind'}`)}</span></div>
            <small>{c('mappingOwner', { sku: cards.find(card => card.id === row.product_id)?.sku || String(row.product_id) })}
              {row.is_listed === false && ` · ${c('unlisted')}`}</small>
            {!review && draft && <details className="consolidation-mapping-editor" open={!validMapping(draft) || undefined}>
              <summary>{c('mappingAdvanced')}</summary>
              <label>{c('mappingAction')}<select data-testid={`consolidation-action-${row.id}`} value={draft.action} disabled={frozen}
                onChange={event => changeMapping(row.id, { action: event.target.value as MappingDraft['action'] })}>
                {row.product_id === keeper && <option value="retain">{c('actions.retain')}</option>}
                <option value="bind">{c('actions.bind')}</option><option value="retire_absent">{c('actions.retire_absent')}</option>
                <option value="retire_unverified_code_absent">{c('actions.retire_unverified_code_absent')}</option>
              </select></label>
              {draft.action.startsWith('retire_') ? <p className="product-editor-muted">{c('absenceHelp')}</p> : <>
                <p className="product-editor-muted">{c('targetHelp')}</p><div className="consolidation-target-grid">
                  <label>{c('targetCode')}<input data-testid={`consolidation-code-${row.id}`} maxLength={100} value={draft.code} disabled={frozen || draft.action === 'retain'} onChange={event => changeMapping(row.id, { code: event.target.value })} /></label>
                  <label>{c('targetKind')}<select data-testid={`consolidation-kind-${row.id}`} value={draft.isVariant ? 'variant' : 'standalone'} disabled={frozen || draft.action === 'retain'} onChange={event => changeMapping(row.id, { isVariant: event.target.value === 'variant' })}>
                    <option value="standalone">{c('standalone')}</option><option value="variant">{c('variant')}</option></select></label>
                  {draft.isVariant && <label>{c('parentCode')}<input data-testid={`consolidation-parent-${row.id}`} maxLength={100} value={draft.parent} disabled={frozen || draft.action === 'retain'} onChange={event => changeMapping(row.id, { parent: event.target.value })} /></label>}
                  <label>{c('nativeProductId')}<input data-testid={`consolidation-product-id-${row.id}`} inputMode="numeric" value={draft.productId} disabled={frozen || draft.action === 'retain'} onChange={event => changeMapping(row.id, { productId: event.target.value })} /></label>
                  {draft.isVariant && <label>{c('nativeVariantId')}<input data-testid={`consolidation-variant-id-${row.id}`} inputMode="numeric" value={draft.variantId} disabled={frozen || draft.action === 'retain'} onChange={event => changeMapping(row.id, { variantId: event.target.value })} /></label>}
                </div>{!validMapping(draft) && <p className="product-editor-error">{c('mappingIncomplete')}</p>}
              </>}
            </details>}
            {action?.target && <div className="consolidation-target-result"><span>{c('targetAfterMerge')}</span> <code>{action.target.code}</code>
              {action.target.is_variant && <small>{c('parentOfTarget', { code: action.target.parent_code })}</small>}</div>}
          </article>;
        })}</div>
      </section>}
      {!completed && <section className="consolidation-section consolidation-evidence"><h3>{c('evidenceTitle')}</h3>
        <div className="consolidation-evidence-grid"><label>{c('unit')}<input data-testid="consolidation-unit" maxLength={500} value={unit} disabled={frozen} placeholder={c('unitPlaceholder')} onChange={event => setUnit(event.target.value)} /><small>{c('unitHelp')}</small></label>
          <label>{c('sources')}<textarea data-testid="consolidation-sources" rows={3} value={sources} disabled={frozen} placeholder="https://" onChange={event => setSources(event.target.value)} /><small>{c('sourcesHelp')}</small></label>
          <label className="consolidation-note">{c('note')}<textarea data-testid="consolidation-note" rows={3} maxLength={3000} value={note} disabled={frozen} onChange={event => setNote(event.target.value)} /><small>{c('noteHelp')}</small></label></div>
        <label className="consolidation-check"><input type="checkbox" data-testid="consolidation-confirm-unit" checked={sameUnit} disabled={frozen} onChange={event => setSameUnit(event.target.checked)} />{c('confirmUnit')}</label>
        <label className="consolidation-check"><input type="checkbox" data-testid="consolidation-confirm-history" checked={preserveHistory} disabled={frozen} onChange={event => setPreserveHistory(event.target.checked)} />{c('confirmHistory')}</label>
        <details className="consolidation-special"><summary>{c('specialCases')}</summary><p className="product-editor-muted">{c('specialHelp')}</p>
          <label className="consolidation-check"><input type="checkbox" data-testid="consolidation-keep-receiving" checked={keepReceiving} disabled={frozen} onChange={event => setKeepReceiving(event.target.checked)} />{c('keepReceiving')}</label>
          <label className="consolidation-check"><input type="checkbox" data-testid="consolidation-supplier-gtin" checked={supplierGtin} disabled={frozen} onChange={event => setSupplierGtin(event.target.checked)} />{c('supplierGtin')}</label>
        </details>
      </section>}
      {review && !completed && <section className="consolidation-section consolidation-preview" data-testid="consolidation-preview-result">
        <h3>{c('previewTitle')}</h3><p>{c('previewSummary', { sku: keeperCard?.sku || String(review.keeper_product_id), count: review.retired_product_ids.length })}</p>
        <p>{c('stockPreserved')}</p>
        {review.dependencies.blockers.length ? blockerList(review.dependencies.blockers) : !review.eligible ? blockerList([{ code: 'consolidation_dependencies_blocked' }]) : <p className="consolidation-eligible">{c('eligible')}</p>}
        {Object.entries(review.native || {}).map(([shop, observation]) => <div key={shop} className="consolidation-native"><strong>{c('verifiedShop', { shop: shopName(shop) })}</strong><ul>{observation.leaves.map(leaf => <li key={`${leaf.is_variant}-${leaf.external_id}`}>
          <code>{leaf.code}</code><span>{leaf.eans.join(', ') || c('noBarcode')}</span>
          {!!leaf.attributes.length && <small>{leaf.attributes.map(item => `${item.name}: ${item.value}`).join(' · ')}</small>}
        </li>)}</ul></div>)}
        {review.supplier_gtin_proof && <p>{c('supplierVerified')}</p>}
        {expired && !uncertain && <p role="alert" className="product-editor-error">{c('errors.consolidation_preview_expired')}</p>}
        <small>{c('expires', { time: Number.isFinite(new Date(review.expires_at).getTime()) ? new Date(review.expires_at).toLocaleString(i18n.language === 'en' ? 'en-GB' : 'sk-SK') : '—' })}</small>
        {!uncertain && <label className="consolidation-check consolidation-final-confirm"><input type="checkbox" data-testid="consolidation-confirm-plan" checked={confirmedPlan}
          disabled={!!busy || !review.eligible || !!review.dependencies.blockers.length || expired} onChange={event => setConfirmedPlan(event.target.checked)} />{c('confirmPlan', { sku: keeperCard?.sku || String(review.keeper_product_id) })}</label>}
      </section>}
      <div className="consolidation-actions">
        {completed ? <Button data-testid="consolidation-done" onClick={onClose}>{c('done')}</Button> : !uncertain && <>
          {!review && <div className="product-editor-scoped-action"><Button data-testid="consolidation-preview" variant="secondary" disabled={!!busy || !!formIssue} title={formIssue ? c(formIssue) : undefined} onClick={() => perform('preview')}>{c('preview')}</Button><ActionScope effects={['upgates-read', 'hub-write']} /></div>}
          {review && <><div className="product-editor-scoped-action"><Button data-testid="consolidation-edit" variant="secondary" disabled={!!busy || !!pending?.apply || !!pending?.discard} onClick={editReview}>{c(expired ? 'refreshPreview' : 'edit')}</Button><ActionScope effects={['hub-write']} /></div>
            <div className="product-editor-scoped-action"><Button data-testid="consolidation-apply" disabled={!canApply} onClick={() => perform('apply')}>{c('apply')}</Button><ActionScope effects={['upgates-read', 'hub-write']} /></div></>}
        </>}
      </div>
      {!review && !uncertain && !!formIssue && selection && <p className="product-editor-muted">{c(formIssue)}</p>}
      {busy && <p role="status" data-testid="consolidation-busy">{c(`busy.${busy}`)}</p>}
      </>}
    </div>
  </section></div>;
}
