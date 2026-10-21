import React, { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';
import { accessRevision } from '../../api/access';
import { ProductEditorRow } from '../../api/productEditor';
import { SupplierLinkOptions, supplierLinkOptions } from '../../api/supplierCardLinks';
import { emptySkuIdentity, previewSkuMigration, SkuIdentityInput, SkuMigrationPreview } from '../../api/skuPolicy';
import { Button } from '../ui/Button.new';
import { ActionScope } from '../ui/ActionScope';
import { SkuIdentityForm } from './SkuIdentityForm';
import './SkuMigrationPreviewDialog.css';

export function SkuMigrationPreviewDialog({ product, onClose }: { product: ProductEditorRow; onClose: () => void }) {
  const { t, i18n } = useTranslation();
  const c = (key: string, vars: Record<string, unknown> = {}) => t(`skuMigration.${key}`, vars);
  const [identity, setIdentity] = useState(() => emptySkuIdentity(product.common.brand || ''));
  const [result, setResult] = useState<SkuMigrationPreview | null>(null), [busy, setBusy] = useState<'local' | 'remote' | null>(null);
  const [error, setError] = useState('');
  const [suppliers, setSuppliers] = useState<SupplierLinkOptions['suppliers']>([]), [supplierError, setSupplierError] = useState(false);
  const panel = useRef<HTMLElement>(null), controller = useRef<AbortController | null>(null), generation = useRef(0);
  const submitted = useRef<SkuIdentityInput | null>(null), sending = useRef(false);
  const closeRef = useRef(onClose); closeRef.current = onClose;
  const ready = !!(identity.brand && identity.source_code && identity.source_ref && identity.leaf_confirmed);
  const reason = (code: string) => t(`skuMigration.reasons.${code}`, { defaultValue: t(`skuPolicy.errors.${code}`, { defaultValue: t(`productLinks.errors.${code}`, { defaultValue: t(`catalog.codes.${code}`, { defaultValue: c('unknownReason') }) }) }) });
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null, overflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden'; panel.current?.querySelector<HTMLElement>('button,input')?.focus();
    const key = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { event.preventDefault(); closeRef.current(); }
      if (event.key === 'Tab') {
        const nodes = Array.from(panel.current?.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),select:not(:disabled),summary,a[href]') || []);
        if (event.shiftKey && document.activeElement === nodes[0]) { event.preventDefault(); nodes[nodes.length - 1]?.focus(); }
        else if (!event.shiftKey && document.activeElement === nodes[nodes.length - 1]) { event.preventDefault(); nodes[0]?.focus(); }
      }
    };
    document.addEventListener('keydown', key);
    return () => { generation.current++; controller.current?.abort(); document.body.style.overflow = overflow; document.removeEventListener('keydown', key); previous?.focus(); };
  }, []);
  useEffect(() => {
    let stopped = false;
    supplierLinkOptions(product.id).then(value => { if (!stopped) setSuppliers(value.suppliers); }).catch(() => { if (!stopped) setSupplierError(true); });
    return () => { stopped = true; };
  }, [product.id]);
  function change(value: SkuIdentityInput) {
    generation.current++; controller.current?.abort(); submitted.current = null; setIdentity(value); setResult(null); setError('');
  }
  async function preview(remote: boolean) {
    if (sending.current || !ready || (remote && (!result || !submitted.current))) return;
    sending.current = true;
    const requestId = ++generation.current, revision = accessRevision(), abort = new AbortController();
    controller.current?.abort(); controller.current = abort;
    const proof = structuredClone(remote ? submitted.current! : identity);
    setBusy(remote ? 'remote' : 'local'); setError('');
    const current = () => !abort.signal.aborted && requestId === generation.current && revision === accessRevision();
    try {
      const value = await previewSkuMigration({ items: [{ product_id: product.id, sku_identity: proof }], verify_remote: remote }, abort.signal);
      if (current()) { submitted.current = proof; setResult(value); }
    } catch (error: any) { if (current()) setError(error.code || 'request_failed'); }
    finally { if (current()) setBusy(null); sending.current = false; }
  }
  function download() {
    if (!result || busy) return;
    try {
      const url = URL.createObjectURL(new Blob([JSON.stringify(result, null, 2) + '\n'], { type: 'application/json' }));
      const link = document.createElement('a'); link.href = url; link.download = `sku-migration-preview-${product.id}.json`;
      document.body.appendChild(link); link.click(); link.remove(); window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch { setError('download_failed'); }
  }
  const shopName = (code: string) => code === 'biketrek' ? 'BIKETREK' : code === 'xtrek' ? 'xTrek' : code;
  return createPortal(<div className="sku-migration-backdrop"><section ref={panel} className="sku-migration-dialog" role="dialog" aria-modal="true" aria-label={c('title')} data-testid="sku-migration-dialog">
    <header><div><h2>{c('title')}</h2><p>{product.common.name}</p><small>{c('hubId', { id: product.id })} · <code>{product.sku}</code></small></div><Button data-testid="sku-migration-close" variant="ghost" onClick={onClose}>{t('common.close')}</Button></header>
    <div className="sku-migration-body">
      <p className="sku-migration-notice" data-testid="sku-migration-readonly">{c('readOnly')}</p>
      <details className="sku-migration-supplier"><summary>{c('supplierException')}</summary><p>{c('supplierExceptionHelp')}</p><label>{c('supplier')}<select data-testid="sku-migration-supplier" disabled={!!busy} value={identity.supplier_id || ''} onChange={event => change({ ...identity, supplier_id: event.target.value ? Number(event.target.value) : undefined, leaf_confirmed: false })}><option value="">{c('noSupplier')}</option>{suppliers.map(supplier => <option key={supplier.id} value={supplier.id}>{supplier.name}</option>)}</select></label>{supplierError && <p role="alert">{c('errors.suppliers_unavailable')}</p>}</details>
      <SkuIdentityForm value={identity} onChange={change} onPreview={() => {}} previewEnabled={false} allowRegistryWrites={false} disabled={!!busy}
        context={{ supplier_id: identity.supplier_id }}
        source={{ supplierCode: product.supplier_codes.map(item => `${item.supplier_code}: ${item.code}`).join(', '), groupCode: product.group?.code }} />
      <div className="sku-migration-actions"><span><Button data-testid="sku-migration-local" disabled={!!busy || !ready} onClick={() => preview(false)}>{c(busy === 'local' ? 'loadingLocal' : 'localPreview')}</Button><ActionScope effects={['hub-read']} /></span>
        <span><Button data-testid="sku-migration-remote" variant="secondary" disabled={!!busy || !result} onClick={() => preview(true)}>{c(busy === 'remote' ? 'loadingRemote' : 'verifyRemote')}</Button><ActionScope effects={['upgates-read']} /></span>
      </div><p className="sku-migration-help">{c('remoteHelp')}</p>
      {error && <p role="alert" className="sku-migration-error">{t(`skuMigration.errors.${error}`, { defaultValue: t(`skuPolicy.errors.${error}`, { defaultValue: c('errors.request_failed') }) })}</p>}
      {result && <section className="sku-migration-result" aria-live="polite" data-testid="sku-migration-result">
        <p><strong>{c('resultTitle')}</strong> · <time dateTime={result.created_at}>{new Date(result.created_at).toLocaleString(i18n.language)}</time></p>
        {result.items.map(item => <article key={item.product_id}>
          <div className="sku-migration-codes"><div><span>{c('oldSku')}</span><code>{item.old_sku}</code></div><div><span>{c('proposedSku')}</span><code>{item.proposed_sku || '—'}</code></div></div>
          <p className={item.status === 'blocked' ? 'sku-migration-error' : ''}><strong>{c(`statuses.${item.status}`)}</strong> · {c(item.remote_verified ? 'remoteVerified' : 'remoteNotVerified')}</p>
          {item.identity.group_code && <p>{c('hubGroup')}: <code>{item.identity.group_code}</code></p>}
          {!!item.reasons.length && <ul>{item.reasons.map(code => <li key={code}>{reason(code)}</li>)}</ul>}
          {!!item.candidate_product_ids.length && <p>{c('candidates', { ids: item.candidate_product_ids.join(', ') })}</p>}
          <h3>{c('shopMappings')}</h3>{!item.mappings.length ? <p>{c('noMappings')}</p> : <div className="sku-migration-table"><table><thead><tr><th>{c('shop')}</th><th>{c('kind')}</th><th>{c('oldSku')}</th><th>{c('proposedSku')}</th><th>{c('nativeId')}</th><th>{c('parent')}</th><th>{c('verification')}</th></tr></thead><tbody>{item.mappings.map(mapping => <tr key={`${mapping.shop_code}:${mapping.mapping_id}`}>
            <td>{shopName(mapping.shop_code)}</td><td>{c(mapping.is_variant ? 'variant' : 'product')}</td><td><code>{mapping.old_code}</code></td><td><code>{mapping.proposed_code}</code></td><td>{mapping.native_id || c('missing')}</td><td><code>{mapping.parent_code || '—'}</code><small>{c('parentNativeId')}: {mapping.parent_native_id || '—'}</small></td><td>{c(`remoteStates.${mapping.remote_status}`)}{mapping.remote_checked_at && <small>{new Date(mapping.remote_checked_at).toLocaleString(i18n.language)}</small>}{mapping.reasons.map(code => <small key={code}>{reason(code)}</small>)}</td>
          </tr>)}</tbody></table></div>}
          <h3>{c('identifiers')}</h3><p>{c('barcodeLabel')}: {item.identity.barcodes.join(', ') || '—'}</p>
          {!item.aliases.length ? <p>{c('noAliases')}</p> : <div className="sku-migration-table"><table><thead><tr><th>{c('identifierType')}</th><th>{c('identifierValue')}</th><th>{c('supplierScope')}</th><th>{c('origin')}</th></tr></thead><tbody>{item.aliases.map((alias, index) => <tr key={`${alias.type}:${alias.value}:${index}`}><td>{c(`identifierTypes.${alias.type}`, { defaultValue: alias.type })}</td><td><code>{alias.value}</code></td><td>{alias.supplier_id === null ? '—' : alias.supplier_id}</td><td>{c('savedIdentifier')}</td></tr>)}</tbody></table></div>}
          <h3>{c('impacts')}</h3><p>{c(`stockStates.${item.impacts.stock_state}`)}</p><dl className="sku-migration-counts">{Object.entries(item.impacts.counts).map(([key, count]) => <React.Fragment key={key}><dt>{c(`counts.${key}`, { defaultValue: c('relatedRecords') })}</dt><dd>{count}</dd></React.Fragment>)}</dl>
          {!!item.impacts.requires_review.length && <><h4>{c('requiresReview')}</h4><ul>{item.impacts.requires_review.map(code => <li key={code}>{reason(code)}</li>)}</ul></>}
          {!!item.impacts.unverified.length && <><h4>{c('unverified')}</h4><ul>{item.impacts.unverified.map(code => <li key={code}>{reason(code)}</li>)}</ul></>}
          {!!item.impacts.refresh_required.length && <><h4>{c('refreshRequired')}</h4><ul>{item.impacts.refresh_required.map(code => <li key={code}>{reason(code)}</li>)}</ul></>}
        </article>)}
        {!!result.required_checks.length && <><h3>{c('requiredChecks')}</h3><ul>{result.required_checks.map(code => <li key={code}>{reason(code)}</li>)}</ul></>}
        <p className="sku-migration-notice">{c('noApply')}</p>
        <Button data-testid="sku-migration-download" variant="secondary" disabled={!!busy} onClick={download}>{c('download')}</Button>
      </section>}
    </div>
  </section></div>, document.body);
}
