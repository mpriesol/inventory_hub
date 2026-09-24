import React, { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { X } from 'lucide-react';
import { Button } from '../ui/Button.new';
import { ActionScope } from '../ui/ActionScope';
import { accessRevision, subscribeAccess } from '../../api/access';
import { ProductEditorRow, ProductPublication, ProductPublicationField, previewProductPublication, productPublicationHistory,
  readProductPublication, resolveProductPublication, sendProductPublication } from '../../api/productEditor';

const fields: ProductPublicationField[] = ['sale_price_gross', 'visible', 'name', 'ean', 'attributes', 'image_url'];
const lockedStates = new Set(['sending', 'uncertain']);
function shown(value: unknown): string {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'boolean') return value ? '✓' : '—';
  if (Array.isArray(value)) return value.map(item => typeof item === 'object' && item && 'name' in item ? `${item.name}: ${item.value}` : shown(item)).join(' · ');
  if (typeof value === 'object') return Object.entries(value).map(([key, item]) => `${key}: ${shown(item)}`).join(' · ');
  return String(value);
}
export function ProductPublicationPanel({ shop, rows, onClose }: { shop: 'biketrek' | 'xtrek'; rows: ProductEditorRow[]; onClose: () => void }) {
  const { t } = useTranslation();
  const credential = useSyncExternalStore(subscribeAccess, accessRevision);
  const [productId, setProductId] = useState(rows[0].id);
  const row = rows.find(item => item.id === productId)!;
  const mapped = row.shops.some(item => item.shop_code === shop && item.mapped);
  const [selectedFields, setSelectedFields] = useState<ProductPublicationField[]>(['sale_price_gross', 'visible']);
  const [history, setHistory] = useState<ProductPublication[]>([]);
  const [operation, setOperation] = useState<ProductPublication | null>(null);
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [uncertain, setUncertain] = useState(false);
  const [confirmed, setConfirmed] = useState(false), [settled, setSettled] = useState(false), [note, setNote] = useState('');
  const context = useRef(0), busyRef = useRef(false), dialog = useRef<HTMLElement | null>(null);
  const shopName = shop === 'biketrek' ? 'BIKETREK' : 'xTrek';
  const unresolved = uncertain || !!operation && lockedStates.has(operation.state);
  const errorText = (code: string) => t(`productPublication.errors.${code}`, { defaultValue: t('productPublication.failed') });
  const remember = (value: ProductPublication) => { setOperation(value); setHistory(items => [value, ...items.filter(item => item.id !== value.id)]); setConfirmed(false); setUncertain(false); };
  useEffect(() => {
    const id = ++context.current, controller = new AbortController();
    setHistory([]); setOperation(null); setConfirmed(false); setUncertain(false); setError(''); setSettled(false); setNote('');
    busyRef.current = true; setBusy(true);
    productPublicationHistory(productId, controller.signal).then(result => {
      if (controller.signal.aborted || id !== context.current) return;
      const items = result.items.filter(item => item.shop_code === shop); setHistory(items);
      const pending = items.find(item => lockedStates.has(item.state)); if (pending) setOperation(pending);
    }).catch(reason => { if (!controller.signal.aborted && id === context.current) setError(reason.code || 'request_failed'); })
      .finally(() => { if (!controller.signal.aborted && id === context.current) { busyRef.current = false; setBusy(false); } });
    return () => { context.current++; controller.abort(); };
  }, [productId, shop, credential]);
  useEffect(() => {
    dialog.current?.querySelector<HTMLElement>('button')?.focus();
    const listener = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !busyRef.current) { event.preventDefault(); onClose(); }
      if (event.key === 'Tab') {
        const nodes = Array.from(dialog.current?.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), a[href]') || []);
        const first = nodes[0], last = nodes[nodes.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
      }
    };
    document.addEventListener('keydown', listener); return () => document.removeEventListener('keydown', listener);
  }, []);
  async function act(kind: 'preview' | 'send' | 'read' | 'resolve') {
    if (busyRef.current) return;
    if (kind !== 'preview' && !operation) return;
    if (kind === 'send' && (!confirmed || operation?.state !== 'ready' || unresolved)) return;
    const generation = context.current, revision = accessRevision(); busyRef.current = true; setBusy(true); setError('');
    try {
      const result = kind === 'preview' ? await previewProductPublication(row, shop, selectedFields)
        : kind === 'send' ? await sendProductPublication(operation!.id)
        : kind === 'resolve' ? await resolveProductPublication(operation!.id, note.trim())
        : await readProductPublication(operation!.id);
      if (context.current === generation && revision === accessRevision()) remember(result);
    } catch (reason: any) {
      if (context.current === generation && revision === accessRevision()) { setError(reason.code || 'request_failed'); if (kind === 'send') setUncertain(true); }
    } finally { if (context.current === generation && revision === accessRevision()) { busyRef.current = false; setBusy(false); } }
  }
  return <div className="product-editor-modal-backdrop"><section className="product-editor-detail product-publication" ref={dialog} role="dialog" aria-modal="true" aria-label={t('productEditor.uploadTo', { shop: shopName })}>
    <header><div><h2>{t('productEditor.uploadTo', { shop: shopName })}</h2><p className="product-editor-muted">{t('productPublication.intro')}</p></div><button type="button" data-testid="publication-close" aria-label={t('productEditor.closeDetail')} disabled={busy} onClick={onClose}><X size={20} /></button></header>
    <div className="product-editor-detail-body">
      <label>{t('productPublication.product')}<select data-testid="publication-product" value={productId} disabled={busy} onChange={event => setProductId(Number(event.target.value))}>{rows.map(item => <option key={item.id} value={item.id}>{item.sku} · {item.common.name}</option>)}</select></label>
      {!mapped ? <div className="product-editor-alert"><p>{t('productPublication.notMapped', { shop: shopName })}</p><Link data-testid="publication-create" to="/suppliers" onClick={onClose}>{t('productPublication.createProduct')}</Link></div> : <>
        <fieldset className="product-publication-fields" disabled={busy || unresolved || !!operation && operation.state === 'ready'}><legend>{t('productPublication.fields')}</legend>{fields.map(field => <label key={field}><input data-testid={`publication-field-${field}`} type="checkbox" checked={selectedFields.includes(field)} onChange={event => setSelectedFields(previous => event.target.checked ? [...previous, field] : previous.filter(item => item !== field))} />{t(`productPublication.fieldsLabel.${field}`)}</label>)}</fieldset>
        <p className="product-editor-muted">{t('productPublication.fieldHelp')}</p>
        {!operation || !['ready', 'sending', 'uncertain'].includes(operation.state) ? <div className="product-editor-scoped-action"><Button data-testid="publication-preview" disabled={busy || !selectedFields.length} onClick={() => act('preview')}>{t('productPublication.preview')}</Button><ActionScope effects={['upgates-read', 'hub-write']} shop={shop} /></div> : null}
      </>}
      {error && <p role="alert" className="product-editor-error">{errorText(error)}</p>}
      {operation && <div data-testid="publication-preview-result" className="product-editor-alert">
        <strong>{t(`productPublication.states.${operation.state}`, { defaultValue: operation.state })}</strong>
        <p className="product-editor-muted">{row.sku} · {shopName} · {t('productPublication.operation', { id: operation.id })}</p>
        {operation.state === 'ready' && <p>{t('productPublication.expires', { date: new Date(operation.expires_at).toLocaleString() })}</p>}
        <div className="product-editor-diff-scroll"><table className="product-editor-diff"><thead><tr><th>{t('productEditor.field')}</th><th>{t('productPublication.remoteBefore')}</th><th>{t('productPublication.remoteAfter')}</th></tr></thead><tbody>{[...new Set([...Object.keys(operation.before || {}), ...Object.keys(operation.after || {})])].map(key => <tr key={key}><th>{t(`productPublication.fieldsLabel.${key}`, { defaultValue: key })}</th><td>{shown(operation.before?.[key])}</td><td>{shown(operation.after?.[key])}</td></tr>)}</tbody></table></div>
        {operation.error && <p role="alert" className="product-editor-error">{errorText(typeof operation.error === 'string' ? operation.error : operation.error.code || 'request_failed')}</p>}
        {operation.state === 'ready' && !uncertain && <><label className="product-publication-confirm"><input type="checkbox" data-testid="publication-confirm" checked={confirmed} disabled={busy} onChange={event => setConfirmed(event.target.checked)} />{t('productPublication.confirm', { shop: shopName })}</label><div className="product-publication-actions"><div className="product-editor-scoped-action"><Button data-testid="publication-send" disabled={busy || !confirmed} onClick={() => act('send')}>{t('productPublication.send', { shop: shopName })}</Button><ActionScope effects={['upgates-write', 'upgates-read', 'hub-write']} shop={shop} /></div><Button data-testid="publication-new-preview" variant="ghost" disabled={busy} onClick={() => { setOperation(null); setConfirmed(false); }}>{t('productPublication.changeFields')}</Button></div></>}
        {unresolved && <><p>{t('productPublication.uncertain')}</p><div className="product-editor-scoped-action"><Button data-testid="publication-recover" variant="secondary" disabled={busy} onClick={() => act('read')}>{t('productPublication.readResult')}</Button><ActionScope effects={['hub-read']} /></div>{lockedStates.has(operation.state) && <details className="product-publication-resolution"><summary>{t('productPublication.resolve')}</summary><p>{t('productPublication.resolveHelp')}</p><label><input type="checkbox" data-testid="publication-settled" checked={settled} disabled={busy} onChange={event => setSettled(event.target.checked)} />{t('productPublication.settled')}</label><label>{t('productPublication.note')}<textarea data-testid="publication-resolution-note" value={note} maxLength={1000} disabled={busy} onChange={event => setNote(event.target.value)} /></label><div className="product-editor-scoped-action"><Button data-testid="publication-resolve" variant="secondary" disabled={busy || !settled || note.trim().length < 10} onClick={() => act('resolve')}>{t('productPublication.resolve')}</Button><ActionScope effects={['upgates-read', 'hub-write']} shop={shop} /></div></details>}</>}
      </div>}
      {history.length > 0 && <details><summary>{t('productPublication.history')}</summary><ul className="product-publication-history">{history.map(item => <li key={item.id}><button type="button" disabled={busy || unresolved && item.id !== operation?.id} data-testid={`publication-history-${item.id}`} onClick={() => { setOperation(item); setConfirmed(false); setUncertain(false); setSettled(false); setNote(''); }}>{t(`productPublication.states.${item.state}`, { defaultValue: item.state })} · {item.created_at ? new Date(item.created_at).toLocaleString() : item.id}</button></li>)}</ul></details>}
    </div>
  </section></div>;
}
