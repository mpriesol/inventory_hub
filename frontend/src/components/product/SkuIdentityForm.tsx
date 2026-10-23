import React, { useEffect, useId, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { accessRevision } from '../../api/access';
import { addSkuBrandAlias, createSkuBrand, getSkuBrands, previewSku, SkuBrand, SkuIdentityInput, SkuPreview, SkuPreviewContext } from '../../api/skuPolicy';
import { Button } from '../ui/Button.new';
import './SkuIdentityForm.css';

export function SkuIdentityForm({ value, onChange, onPreview, context = {}, disabled = false, source, previewEnabled = true, allowRegistryWrites = true, simplified = false }: {
  value: SkuIdentityInput; onChange: (value: SkuIdentityInput) => void; onPreview: (value: SkuPreview | null) => void;
  context?: SkuPreviewContext; disabled?: boolean; source?: { supplierCode?: string; manufacturerCode?: string; groupCode?: string };
  previewEnabled?: boolean; allowRegistryWrites?: boolean; simplified?: boolean;
}) {
  const { t } = useTranslation();
  const c = (key: string, vars: Record<string, unknown> = {}) => t(`skuPolicy.${key}`, vars);
  const id = useId();
  const [brands, setBrands] = useState<SkuBrand[]>([]), [loadAttempt, setLoadAttempt] = useState(0);
  const [error, setError] = useState(''), [checking, setChecking] = useState(false), [preview, setPreview] = useState<SkuPreview | null>(null);
  const [register, setRegister] = useState(false), [registerBusy, setRegisterBusy] = useState(false);
  const [registerCode, setRegisterCode] = useState(''), [registerKind, setRegisterKind] = useState<SkuBrand['kind']>('brand');
  const [registerRef, setRegisterRef] = useState(''), [aliasTarget, setAliasTarget] = useState('');
  const previewCallback = useRef(onPreview); previewCallback.current = onPreview;
  const signature = JSON.stringify({ value, context });
  const payload = useRef({ value, context }); payload.current = { value, context };
  const selectedBrand = brands.find(brand => [brand.name, ...brand.aliases].some(name => name.toLocaleLowerCase() === value.brand.toLocaleLowerCase()));
  const namespaceKind = selectedBrand?.kind;
  useEffect(() => {
    if (!simplified || !namespaceKind) return;
    const codeKind = context.item_type === 'service' && (namespaceKind === 'service' || namespaceKind === 'unbranded')
      ? namespaceKind : 'manufacturer';
    if (value.code_kind !== codeKind) onChange({ ...value, code_kind: codeKind, leaf_confirmed: false });
  }, [simplified, namespaceKind, value.code_kind, context.item_type]);
  useEffect(() => {
    const abort = new AbortController();
    getSkuBrands(abort.signal).then(result => { if (!abort.signal.aborted) { setBrands(result.brands); setError(''); } })
      .catch(() => { if (!abort.signal.aborted) setError('registryUnavailable'); });
    return () => abort.abort();
  }, [loadAttempt]);
  useEffect(() => {
    const abort = new AbortController(), revision = accessRevision();
    setPreview(null); previewCallback.current(null); setChecking(false);
    const complete = !!(value.brand && value.source_code && (simplified || value.source_ref));
    if (!previewEnabled || disabled || (!complete && !context.barcodes?.length && !context.supplier_sku && !context.sku && !context.expected_product_id)) return () => abort.abort();
    setChecking(true);
    const timer = window.setTimeout(() => {
      const current = payload.current;
      previewSku(complete ? current.value : null, current.context, abort.signal).then(result => {
        if (!abort.signal.aborted && revision === accessRevision()) { setPreview(result); previewCallback.current(result); setChecking(false); }
      }).catch(() => { if (!abort.signal.aborted && revision === accessRevision()) { setChecking(false); setError('previewUnavailable'); } });
    }, 250);
    return () => { abort.abort(); window.clearTimeout(timer); };
  }, [signature, disabled, loadAttempt, previewEnabled, simplified]);
  function change(patch: Partial<SkuIdentityInput>) {
    const identityChanged = (['brand', 'source_code', 'code_kind'] as const).some(key => key in patch && patch[key] !== value[key]);
    setError(''); onChange({ ...value, ...patch,
      ...(simplified && identityChanged ? { source_ref: '', leaf_confirmed: false } : {}),
      ...(context.supplier_id ? { supplier_id: context.supplier_id } : {}) });
  }
  async function saveBrand() {
    if (registerBusy || !allowRegistryWrites) return;
    setRegisterBusy(true); setError('');
    try {
      const brand = aliasTarget ? await addSkuBrandAlias(Number(aliasTarget), value.brand, registerRef)
        : await createSkuBrand({ name: value.brand, code: registerCode, kind: registerKind, aliases: [], source_ref: registerRef });
      setBrands(old => [...old.filter(item => item.id !== brand.id), brand]); setRegister(false); change({ brand: brand.name });
    } catch (error: any) { setError(error.code || 'registryUnavailable'); }
    finally { setRegisterBusy(false); }
  }
  const reasonText = (reason: string) => t(`skuPolicy.errors.${reason}`, { defaultValue: t('skuPolicy.errors.identity_conflict') });
  return <fieldset className="sku-identity-form" disabled={disabled || registerBusy}>
    <legend>{c('title')}</legend><p>{previewEnabled ? c('help') : t('skuMigration.identityHelp')}</p>
    {source && <details className="sku-source"><summary>{c('sourceValues')}</summary><dl>
      <dt>{c('supplierCode')}</dt><dd>{source.supplierCode || '—'}</dd>
      <dt>{c('sourceManufacturer')}</dt><dd>{source.manufacturerCode || '—'}</dd>
      <dt>{c('groupCode')}</dt><dd>{source.groupCode || '—'}</dd>
    </dl><p>{c('sourceHelp')}</p></details>}
    <div className="sku-identity-grid">
      <label>{c('brand')}<input data-testid="sku-brand" list={`${id}-brands`} autoComplete="off" value={value.brand} maxLength={100} onChange={event => change({ brand: event.target.value, leaf_confirmed: false })} /><datalist id={`${id}-brands`}>{brands.map(brand => <option key={brand.id} value={brand.name}>{brand.code} · {c(`brandKinds.${brand.kind}`)}</option>)}</datalist>{selectedBrand && <small>{selectedBrand.code} · {selectedBrand.name}</small>}</label>
      {!simplified && <label>{c('codeKind')}<select data-testid="sku-code-kind" value={value.code_kind} onChange={event => change({ code_kind: event.target.value as SkuIdentityInput['code_kind'], leaf_confirmed: false })}>{(['manufacturer', 'supplier', 'gtin', 'service', 'unbranded'] as const).map(kind => <option key={kind} value={kind} disabled={kind === 'supplier' && !context.supplier_id && !value.supplier_id}>{c(`codeKinds.${kind}`)}</option>)}</select></label>}
      <label>{c(context.item_type === 'service' ? 'serviceCode' : value.code_kind === 'manufacturer' ? 'sourceCode' : 'existingIdentityCode')}<input data-testid="sku-source-code" value={value.source_code} maxLength={97} autoComplete="off" onChange={event => change({ source_code: event.target.value, leaf_confirmed: false })} /><small>{c('preserveCode')}</small></label>
      {!simplified && <label>{c('sourceRef')}<input data-testid="sku-source-ref" value={value.source_ref} maxLength={1000} onChange={event => change({ source_ref: event.target.value })} placeholder={c('sourceRefHint')} /></label>}
    </div>
    {!simplified && value.code_kind !== 'manufacturer' && <p className="sku-exception">{c(`exceptions.${value.code_kind}`)}</p>}
    <label className="sku-leaf-confirm"><input data-testid="sku-leaf-confirmed" type="checkbox" checked={value.leaf_confirmed} onChange={event => change({ leaf_confirmed: event.target.checked, ...(simplified && event.target.checked && (!value.source_ref || ['operator:manufacturer-code', 'operator:service-code'].includes(value.source_ref)) ? { source_ref: context.item_type === 'service' ? 'operator:service-code' : 'operator:manufacturer-code' } : {}) })} /><span>{c(context.item_type === 'service' ? 'serviceConfirmed' : value.code_kind === 'manufacturer' ? 'leafConfirmed' : 'existingIdentityConfirmed')}</span></label>
    {!!value.brand && !selectedBrand && allowRegistryWrites && <div className="sku-brand-register"><Button type="button" variant="ghost" size="sm" data-testid="sku-register-toggle" onClick={() => setRegister(!register)}>{c('registerBrand')}</Button>{register && <div>
      <p>{c('registerHelp')}</p><label>{c('aliasTarget')}<select data-testid="sku-alias-target" value={aliasTarget} onChange={event => setAliasTarget(event.target.value)}><option value="">{c('newBrand')}</option>{brands.map(brand => <option key={brand.id} value={brand.id}>{brand.name} · {brand.code}</option>)}</select></label>
      {!aliasTarget && <div className="sku-identity-grid"><label>{c('brandCode')}<input data-testid="sku-register-code" value={registerCode} maxLength={2} pattern="[A-Z]{2}" onChange={event => setRegisterCode(event.target.value.toUpperCase())} /></label><label>{c('brandKind')}<select value={registerKind} onChange={event => setRegisterKind(event.target.value as SkuBrand['kind'])}>{(['brand', 'service', 'unbranded'] as const).map(kind => <option key={kind} value={kind}>{c(`brandKinds.${kind}`)}</option>)}</select></label></div>}
      <label>{c('registryRef')}<input data-testid="sku-register-ref" value={registerRef} maxLength={1000} onChange={event => setRegisterRef(event.target.value)} /></label>
      <Button type="button" data-testid="sku-register-save" size="sm" disabled={!registerRef.trim() || (!aliasTarget && !/^[A-Z]{2}$/.test(registerCode))} onClick={saveBrand}>{c(aliasTarget ? 'saveAlias' : 'saveBrand')}</Button>
    </div>}</div>}
    {!!value.brand && !selectedBrand && !allowRegistryWrites && <p>{t('skuMigration.registryReadOnly')}</p>}
    {previewEnabled && <div className={`sku-preview ${preview?.status || ''}`} aria-live="polite" data-testid="sku-preview">
      {checking ? <p>{c('checking')}</p> : preview ? <><strong>{c(`states.${preview.status}`)}</strong>{preview.sku && <code>{preview.sku}</code>}{preview.product_id && <p>{c('existingCard', { id: preview.product_id })}</p>}{preview.reasons.map(reason => <p key={reason}>{reasonText(reason)}</p>)}{!!preview.candidate_product_ids.length && preview.status === 'conflict' && <p>{c('candidates', { ids: preview.candidate_product_ids.join(', ') })}</p>}</> : <p>{c('completeFields')}</p>}
    </div>}
    {error && <p role="alert">{t(`skuPolicy.errors.${error}`, { defaultValue: c('errors.registryUnavailable') })} {['registryUnavailable', 'previewUnavailable'].includes(error) && <Button type="button" variant="ghost" size="sm" onClick={() => setLoadAttempt(count => count + 1)}>{c('retry')}</Button>}</p>}
  </fieldset>;
}
