import React, { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';
import { accessRevision } from '../../api/access';
import { decideProductImportIdentity, ImportIdentityDecision, ImportIdentityReview, ProductImportDraft,
  readProductImport, reviewProductImportIdentity } from '../../api/productImport';
import { Button } from '../ui/Button.new';
import { ActionScope } from '../ui/ActionScope';
import { IdentityFacts } from './IdentityFacts';
import './ProductSupplierLinksPanel.css';

export function ImportIdentityReviewPanel({ draft, onChanged, onClose }: {
  draft: ProductImportDraft; onChanged: (value: ProductImportDraft) => void; onClose: () => void;
}) {
  const { t } = useTranslation();
  const c = (key: string, args: Record<string, unknown> = {}) => t(`importIdentity.${key}`, args);
  const reason = (code: string) => t(`importIdentity.errors.${code}`, { defaultValue: t(`supplierLinks.errors.${code}`, {
    defaultValue: t(`supplierLinks.reasons.${code}`, { defaultValue: c('reasonUnknown', { code }) }) }) });
  const [review, setReview] = useState<ImportIdentityReview | null>(null);
  const [choices, setChoices] = useState<Record<number, ImportIdentityDecision>>({});
  const [error, setError] = useState(''), [busy, setBusy] = useState(''), [uncertain, setUncertain] = useState(false);
  const [page, setPage] = useState(1), [showAll, setShowAll] = useState(false);
  const currentDraft = useRef(draft), mounted = useRef(true), busyRef = useRef(false), dialog = useRef<HTMLDivElement>(null);
  const changedRef = useRef(onChanged), closeRef = useRef(onClose); changedRef.current = onChanged; closeRef.current = onClose;
  const relevant = review?.rows.filter(row => showAll || row.status === 'review' || row.status === 'conflict' || !!row.decision) || [];
  const shown = relevant.slice((page - 1) * 20, page * 20);
  async function load(recover = false) {
    if (busyRef.current) return;
    const credential = accessRevision(); busyRef.current = true; setBusy('read'); setError('');
    try {
      // A fresh server revision is the only basis for further choices after a
      // lost response or CAS conflict; no automatic repeated decision POST.
      const value = recover ? await readProductImport(currentDraft.current.id) : currentDraft.current;
      const result = await reviewProductImportIdentity(value);
      if (!mounted.current || credential !== accessRevision()) return;
      currentDraft.current = value; if (recover) changedRef.current(value);
      setReview(result); setPage(1); setUncertain(false);
      setChoices(Object.fromEntries(result.rows.filter(row => row.decision && row.status !== 'saved' && row.status !== 'conflict')
        .map(row => [row.row_id, { row_id: row.row_id, ...row.decision! }])));
    } catch (failure: any) { if (mounted.current && credential === accessRevision()) setError(failure.code || 'request_failed'); }
    finally { if (mounted.current && credential === accessRevision()) { busyRef.current = false; setBusy(''); } }
  }
  async function save() {
    if (busyRef.current || uncertain || !review || !Object.keys(choices).length) return;
    const decisions = Object.values(choices).sort((a, b) => a.row_id - b.row_id);
    if (decisions.some(item => {
      const row = review.rows.find(row => row.row_id === item.row_id);
      return !row || row.status === 'saved' || row.status === 'conflict'
        || (row.status === 'linked' && (item.action !== 'link' || item.product_id !== row.matched_product_id))
        || (item.action === 'create' ? !row.can_create : !row.candidates.some(candidate => candidate.product_id === item.product_id && candidate.can_link));
    })) return;
    const credential = accessRevision(); busyRef.current = true; setBusy('save'); setError('');
    try {
      const value = await decideProductImportIdentity(currentDraft.current, review.review_hash, decisions);
      if (!mounted.current || credential !== accessRevision()) return;
      changedRef.current(value); closeRef.current();
    } catch (failure: any) {
      if (mounted.current && credential === accessRevision()) { setError(failure.code || 'request_failed'); setUncertain(true); }
    } finally { if (mounted.current && credential === accessRevision()) { busyRef.current = false; setBusy(''); } }
  }
  useEffect(() => {
    mounted.current = true;
    const previous = document.activeElement as HTMLElement | null, overflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden'; dialog.current?.querySelector<HTMLButtonElement>('button')?.focus(); void load();
    const key = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !busyRef.current) { event.preventDefault(); closeRef.current(); }
      if (event.key !== 'Tab') return;
      const items = [...(dialog.current?.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),summary') || [])];
      const first = items[0], last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    };
    document.addEventListener('keydown', key);
    return () => { mounted.current = false; document.body.style.overflow = overflow; document.removeEventListener('keydown', key); previous?.focus(); };
  }, []);
  const choose = (value: ImportIdentityDecision) => { if (!busyRef.current && !uncertain) setChoices(old => ({ ...old, [value.row_id]: value })); };
  return createPortal(<div className="product-import-editor-backdrop"><div ref={dialog} className="product-import product-import-cell-editor identity-review-panel" role="dialog" aria-modal="true" aria-labelledby="identity-review-title">
    <header><div><h2 id="identity-review-title">{c('title')}</h2><p>{c('intro')}</p></div><Button data-testid="identity-review-close" variant="ghost" disabled={!!busy} onClick={onClose} aria-label={t('productImport.close')}>×</Button></header>
    <div className="import-cell-editor-body">
      <p className="import-notice">{c('linkOnly')}</p>
      {error && <p className="import-error" role="alert">{reason(error)}</p>}
      {uncertain && <p className="import-notice" data-testid="identity-review-recovery">{c('uncertain')}</p>}
      <div className="identity-review-toolbar"><label><input type="checkbox" checked={showAll} onChange={event => { setShowAll(event.target.checked); setPage(1); }} /> {c('showAll')}</label>
        <Button data-testid="identity-review-refresh" variant="secondary" size="sm" disabled={!!busy} onClick={() => load(true)}>{c('refresh')}</Button></div>
      {review && <p>{c('summary', { count: review.rows.filter(row => row.status === 'review' || row.status === 'conflict').length, total: review.rows.length })}</p>}
      {review && !relevant.length && <p>{c('noReview')}</p>}
      {shown.map(row => <section className="identity-review-row" key={row.row_id} data-testid={`identity-review-row-${row.row_id}`}>
        <h3>{c(`status.${row.status}`)}</h3><div className="supplier-links-comparison"><section><h4>{c('incoming')}</h4>
          <IdentityFacts {...row.source} supplierSku={row.source.supplier_sku} /></section><section><h4>{c('existing')}</h4>
          {!row.candidates.length && <p>{c('noCandidates')}</p>}
          {row.candidates.map(candidate => <label className={`identity-review-choice ${!candidate.can_link ? 'is-blocked' : ''}`} key={candidate.product_id}>
            <input data-testid={`identity-review-link-${row.row_id}-${candidate.product_id}`} type="radio" name={`identity-row-${row.row_id}`}
              disabled={!!busy || uncertain || !candidate.can_link || row.status === 'saved' || row.status === 'linked' || row.status === 'conflict'}
              checked={choices[row.row_id]?.action === 'link' && choices[row.row_id]?.product_id === candidate.product_id || (row.status === 'linked' || row.status === 'saved') && row.matched_product_id === candidate.product_id}
              onChange={() => choose({ row_id: row.row_id, action: 'link', product_id: candidate.product_id })} />
            <div><IdentityFacts {...candidate} /><div className="identity-match-reasons">{candidate.reasons.map(code => <span key={code}>{reason(code)}</span>)}</div>
              {candidate.reasons.some(code => code === 'similar_name' || code === 'similar_sku') && <p className="import-muted">{c('similarityOnly')}</p>}
              {candidate.errors.map(code => <p className="import-error" key={code}>{reason(code)}</p>)}
              {candidate.warnings.map(code => <p className="import-muted" key={code}>{reason(code)}</p>)}
            </div></label>)}
          {row.status === 'linked' && row.matched_product_id != null && row.candidates.some(candidate => candidate.product_id === row.matched_product_id && candidate.can_link) &&
            <Button data-testid={`identity-review-link-only-${row.row_id}`} variant="secondary" size="sm"
              disabled={!!busy || uncertain || choices[row.row_id]?.action === 'link'}
              onClick={() => choose({ row_id: row.row_id, action: 'link', product_id: row.matched_product_id! })}>
              {c(choices[row.row_id]?.action === 'link' ? 'linkOnlySelected' : 'chooseLinkOnly')}
            </Button>}
          {row.can_create && row.status !== 'saved' && row.status !== 'linked' && row.status !== 'conflict' && <label className="identity-review-choice"><input data-testid={`identity-review-create-${row.row_id}`} type="radio" name={`identity-row-${row.row_id}`}
            disabled={!!busy || uncertain} checked={choices[row.row_id]?.action === 'create'} onChange={() => choose({ row_id: row.row_id, action: 'create' })} />
            <span><strong>{c('createNew')}</strong><p>{c('createHelp')}</p></span></label>}
        </section></div>
        {row.errors.map(code => <p className="import-error" key={code}>{reason(code)}</p>)}
        {row.truncated && <p className="import-notice">{c('truncated')}</p>}
      </section>)}
      {relevant.length > 20 && <div className="import-actions"><Button variant="secondary" disabled={page === 1} onClick={() => setPage(page - 1)}>{c('previous')}</Button><span>{page} / {Math.ceil(relevant.length / 20)}</span><Button variant="secondary" disabled={page * 20 >= relevant.length} onClick={() => setPage(page + 1)}>{c('next')}</Button></div>}
      <div className="supplier-links-actions"><div className="product-editor-scoped-action"><Button data-testid="identity-review-save" disabled={!!busy || uncertain || !Object.keys(choices).length} onClick={save}>{c('save', { count: Object.keys(choices).length })}</Button><ActionScope effects={['hub-write']} /></div><p>{c('saveHelp')}</p></div>
      {busy && <p role="status">{c(`busy.${busy}`)}</p>}
    </div></div></div>, document.body);
}
