import React, { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { X } from 'lucide-react';
import { accessRevision } from '../../api/access';
import { ProductEditorRow } from '../../api/productEditor';
import { applySupplierLink, forgetSupplierLink, IdentityCard, PendingSupplierLink, pendingSupplierLink,
  previewSupplierLink, readSupplierLink, rememberSupplierLink, supplierLinkCandidates, supplierLinkOptions,
  SupplierLinkOptions, SupplierLinkPreview, SupplierLinkSource } from '../../api/supplierCardLinks';
import { Button } from '../ui/Button.new';
import { ActionScope } from '../ui/ActionScope';
import { IdentityFacts } from './IdentityFacts';
import './ProductSupplierLinksPanel.css';

export function ProductSupplierLinksPanel({ product: initialProduct, onClose, onChanged }: {
  product?: ProductEditorRow; onClose: () => void; onChanged: () => void;
}) {
  const { t, i18n } = useTranslation();
  const c = (key: string, args: Record<string, unknown> = {}) => t(`supplierLinks.${key}`, args);
  const message = (code: string) => t(`supplierLinks.errors.${code}`, { defaultValue: t(`supplierLinks.reasons.${code}`, { defaultValue: c('reasonUnknown', { code }) }) });
  const [product, setProduct] = useState<IdentityCard | null>(initialProduct ? {
    id: initialProduct.id, sku: initialProduct.sku, name: initialProduct.common.name,
    eans: initialProduct.eans || [], attributes: initialProduct.attributes || [],
  } : null);
  const [options, setOptions] = useState<SupplierLinkOptions | null>(null);
  const [supplier, setSupplier] = useState(''), [query, setQuery] = useState('');
  const [candidates, setCandidates] = useState<SupplierLinkSource[] | null>(null);
  const [candidateWarnings, setCandidateWarnings] = useState<string[]>([]);
  const [truncated, setTruncated] = useState(false), [selected, setSelected] = useState<SupplierLinkSource | null>(null);
  const [preview, setPreview] = useState<SupplierLinkPreview | null>(null);
  const [pending, setPending] = useState<PendingSupplierLink | null>(() => pendingSupplierLink());
  const [uncertain, setUncertain] = useState<'preview' | 'apply' | null>(() => {
    const value = pendingSupplierLink(); return value ? value.apply ? 'apply' : 'preview' : null;
  });
  const [missing, setMissing] = useState(false), [error, setError] = useState(''), [busy, setBusy] = useState('');
  const pendingRef = useRef(pending), busyRef = useRef(false), mounted = useRef(true), dialog = useRef<HTMLElement>(null);
  const optionsAttempted = useRef<number | null>(null);
  const closeRef = useRef(onClose), changedRef = useRef(onChanged); closeRef.current = onClose; changedRef.current = onChanged;
  const frozen = !!uncertain || !!pending?.apply;
  const remember = (value: PendingSupplierLink) => { rememberSupplierLink(value); pendingRef.current = value; setPending(value); };
  const accept = (value: SupplierLinkPreview) => {
    setPreview(value); setProduct(value.product); setSelected(value.source); setSupplier(value.source.supplier_code); setMissing(false);
    if (value.status === 'completed') {
      try { forgetSupplierLink(value.id); } catch { /* A completed operation remains readable. */ }
      pendingRef.current = null; setPending(null); setUncertain(null); changedRef.current();
    } else setUncertain(pendingRef.current?.apply ? 'apply' : null);
  };
  async function perform(kind: 'options' | 'search' | 'preview' | 'apply' | 'read', retry = false) {
    if (busyRef.current) return;
    let saved = pendingRef.current;
    if (kind === 'options' && !product || kind === 'search' && (!product || !supplier || frozen)) return;
    if (kind === 'preview' && !retry) {
      if (!product || !selected || frozen) return;
      saved = { version: 1, request: { request_id: crypto.randomUUID(), product_id: product.id, supplier_product_id: selected.id } };
      try { remember(saved); } catch { setError('storage_unavailable'); return; }
      setPreview(null);
    }
    if (kind === 'apply' && !retry) {
      if (frozen || !saved || preview?.status !== 'prepared' || !['ready', 'already_linked'].includes(preview.link_status)) return;
      saved = { ...saved, apply: { preview_hash: preview.preview_hash, confirmed: true } };
      try { remember(saved); } catch { setError('storage_unavailable'); return; }
    }
    if (['preview', 'apply', 'read'].includes(kind) && !saved || kind === 'apply' && !saved?.apply) return;
    const credential = accessRevision(); busyRef.current = true; setBusy(kind); setError('');
    if (kind === 'preview' || kind === 'apply') setUncertain(kind);
    try {
      if (kind === 'options') {
        optionsAttempted.current = product!.id;
        const value = await supplierLinkOptions(product!.id);
        if (mounted.current && credential === accessRevision()) {
          setOptions(value); setProduct(value.product); setSupplier(previous => previous || value.suppliers[0]?.code || '');
        }
      } else if (kind === 'search') {
        const value = await supplierLinkCandidates(product!.id, supplier, query);
        if (mounted.current && credential === accessRevision()) {
          setProduct(value.product); setCandidates(value.rows); setTruncated(value.truncated); setCandidateWarnings(value.warnings || []); setSelected(null); setPreview(null);
        }
      } else {
        const value = kind === 'preview' ? await previewSupplierLink(saved!.request)
          : kind === 'apply' ? await applySupplierLink(saved!.request.request_id, saved!.apply!)
          : await readSupplierLink(saved!.request.request_id);
        if (mounted.current && credential === accessRevision()) accept(value);
      }
    } catch (failure: any) {
      if (!mounted.current || credential !== accessRevision()) return;
      const code = failure.code || 'request_failed'; setError(code);
      if (kind === 'read' && !saved?.apply && failure.status === 404) setMissing(true);
      if (kind === 'preview' && failure.status >= 400 && failure.status < 500 && failure.status !== 429 &&
          code !== 'supplier_card_links_request_changed') {
        try { forgetSupplierLink(saved!.request.request_id); pendingRef.current = null; setPending(null); setUncertain(null); }
        catch { setError('storage_unavailable'); }
      }
      if (kind === 'apply' && failure.knownUnapplied === true) {
        try { remember({ version: 1, request: saved!.request }); setUncertain(null); setPreview(null); }
        catch { setError('storage_unavailable'); }
      }
    } finally {
      if (mounted.current && credential === accessRevision()) { busyRef.current = false; setBusy(''); }
    }
  }
  useEffect(() => {
    mounted.current = true;
    const previous = document.activeElement as HTMLElement | null;
    const overflow = document.body.style.overflow; document.body.style.overflow = 'hidden';
    dialog.current?.querySelector<HTMLButtonElement>('button')?.focus();
    void perform(pendingRef.current ? 'read' : 'options');
    const key = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !busyRef.current) { event.preventDefault(); closeRef.current(); }
      if (event.key !== 'Tab') return;
      const items = [...(dialog.current?.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),select:not(:disabled),summary') || [])];
      const first = items[0], last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    };
    document.addEventListener('keydown', key);
    return () => { mounted.current = false; document.body.style.overflow = overflow; document.removeEventListener('keydown', key); previous?.focus(); };
  }, []);
  // A recovered preview carries its actual card, even when Resume was opened
  // without a table selection or while another card was visible.
  useEffect(() => { if (product && !options && optionsAttempted.current !== product.id && !busy && !frozen) void perform('options'); }, [product?.id, options, busy, frozen]);
  const resetSearch = () => { setCandidates(null); setCandidateWarnings([]); setTruncated(false); setSelected(null); setPreview(null); setError(''); };
  return <div className="product-editor-modal-backdrop"><section ref={dialog} className="product-editor-detail supplier-links-panel" role="dialog" aria-modal="true" aria-labelledby="supplier-links-title">
    <header><div><h2 id="supplier-links-title">{c('title')}</h2><p className="product-editor-muted">{c('intro')}</p></div>
      <button type="button" data-testid="supplier-links-close" disabled={!!busy} onClick={onClose} aria-label={t('productEditor.closeDetail')}><X size={20} /></button></header>
    <div className="product-editor-detail-body">
      {product && <section><h3>{c('card')}</h3><IdentityFacts {...product} /></section>}
      {initialProduct && product && initialProduct.id !== product.id && <p className="product-editor-alert">{c('recoveredScope')}</p>}
      {error && <p role="alert" className="product-editor-error">{message(error)}</p>}
      {product && !options && !frozen && !busy && <Button variant="secondary" onClick={() => perform('options')}>{c('reloadOptions')}</Button>}
      {uncertain && <div className="product-editor-alert" data-testid="supplier-links-recovery"><p>{c(uncertain === 'apply' ? 'applyUnknown' : 'previewUnknown')}</p>
        <div className="supplier-links-actions"><Button data-testid="supplier-links-recover" variant="secondary" disabled={!!busy} onClick={() => perform('read')}>{c('readResult')}</Button>
          {uncertain === 'apply' && <Button data-testid="supplier-links-retry-apply" variant="secondary" disabled={!!busy} onClick={() => perform('apply', true)}>{c('retryApply')}</Button>}
          {uncertain === 'preview' && missing && <Button data-testid="supplier-links-retry-preview" variant="secondary" disabled={!!busy} onClick={() => perform('preview', true)}>{c('retryPreview')}</Button>}
        </div></div>}
      {options && <details><summary>{c('currentLinks', { count: options.current_links.length })}</summary>
        {!options.current_links.length ? <p>{c('noLinks')}</p> : options.current_links.map(source => <p key={source.link_id}>
          <strong>{source.supplier_name}</strong> · <code>{source.supplier_sku}</code> · {source.name} · {c(source.is_active ? 'active' : 'inactive')}</p>)}
      </details>}
      {!frozen && preview?.status !== 'completed' && <>
        <form className="supplier-links-search" onSubmit={event => { event.preventDefault(); void perform('search'); }}>
          <label>{c('supplier')}<select data-testid="supplier-links-supplier" value={supplier} disabled={!!busy || !options} onChange={event => { setSupplier(event.target.value); resetSearch(); }}>
            {!supplier && <option value="">{c('chooseSupplier')}</option>}{options?.suppliers.map(item => <option key={item.id} value={item.code}>{item.name}</option>)}
          </select></label><label>{c('searchLabel')}<input data-testid="supplier-links-query" maxLength={100} value={query} disabled={!!busy} onChange={event => { setQuery(event.target.value); resetSearch(); }} /></label>
          <div className="product-editor-scoped-action"><Button type="submit" data-testid="supplier-links-search" variant="secondary" disabled={!!busy || !supplier || !product}>{c('search')}</Button><ActionScope effects={['hub-read']} /></div>
        </form><p className="product-editor-muted">{c('storedCatalog')}</p>
        {truncated && <p className="product-editor-alert">{c('truncated')}</p>}
        {candidateWarnings.map(code => <p className="product-editor-alert" key={code}>{message(code)}</p>)}
        {candidates && !candidates.length && <p>{c('noCandidates')}</p>}
        <div className="supplier-links-candidates">{candidates?.map(source => <label className="supplier-links-candidate" key={source.id}>
          <input data-testid={`supplier-links-candidate-${source.id}`} type="radio" name="supplier-link-source" disabled={!!busy || !source.active} checked={selected?.id === source.id}
            onChange={() => { setSelected(source); setPreview(null); }} /><div><IdentityFacts sku={source.shop_sku} supplierSku={source.supplier_sku} name={source.name} eans={source.eans} attributes={source.attributes} />
            <div className="identity-match-reasons">{source.match_reasons?.map(reason => <span key={reason}>{message(reason)}</span>)}</div>
            {!source.active && <p>{c('inactive')}</p>}{source.last_seen_at && <small>{c('lastSeen')}: {new Date(source.last_seen_at).toLocaleString(i18n.language)}</small>}
            {source.warnings?.map(reason => <p className="product-editor-muted" key={reason}>{message(reason)}</p>)}
          </div></label>)}</div>
      </>}
      {preview && <section data-testid="supplier-links-preview-result"><h3>{c(`status.${preview.status === 'completed' ? preview.result?.status || 'linked' : preview.link_status}`)}</h3>
        <div className="supplier-links-comparison"><section><h4>{c('card')}</h4><IdentityFacts {...preview.product} /></section>
          <section><h4>{preview.source.supplier_name}</h4><IdentityFacts sku={preview.source.shop_sku} supplierSku={preview.source.supplier_sku} name={preview.source.name} eans={preview.source.eans} attributes={preview.source.attributes} /></section></div>
        {preview.errors.map(reason => <p className="product-editor-error" key={reason}>{message(reason)}</p>)}
        {preview.warnings.map(reason => <p className="product-editor-alert" key={reason}>{message(reason)}</p>)}
        {preview.planned_changes.length > 0 && preview.status !== 'completed' && <div><h4>{c('changes')}</h4><ul>{preview.planned_changes.map(change => <li key={change}>{message(change)}</li>)}</ul></div>}
        {preview.status === 'completed' && <p role="status" className="product-editor-alert" data-testid="supplier-links-completed">{c('completed')}</p>}
      </section>}
      {!frozen && preview?.status !== 'completed' && <div className="supplier-links-actions">
        <div className="product-editor-scoped-action"><Button data-testid="supplier-links-preview" variant="secondary" disabled={!!busy || !selected || !product} onClick={() => perform('preview')}>{c(preview ? 'refreshPreview' : 'preview')}</Button><ActionScope effects={['hub-read', 'hub-write']} /></div>
        {preview && ['ready', 'already_linked'].includes(preview.link_status) && <div className="product-editor-scoped-action"><Button data-testid="supplier-links-apply" disabled={!!busy} onClick={() => perform('apply')}>{c(preview.link_status === 'already_linked' ? 'finishExisting' : 'apply')}</Button><ActionScope effects={['hub-write']} /></div>}
      </div>}
      {busy && <p role="status">{c(`busy.${busy}`)}</p>}
    </div></section></div>;
}
