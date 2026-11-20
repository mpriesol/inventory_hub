import React, { FormEvent, useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { createSkuBrand, getSkuBrands, removeSkuBrand, restoreSkuBrand, SkuBrand, updateSkuBrand } from '../api/skuPolicy';
import { SkuBrandTable } from '../components/product/SkuBrandTable';
import { Button } from '../components/ui/Button.new';

type Editor = { action: 'create' } | { action: 'edit' | 'remove' | 'restore'; brand: SkuBrand };
type Outcome = 'saved' | 'removed' | 'archived' | 'restored';

function BrandEditor({ editor, onDone, onCancel, onReload }: {
  editor: Editor; onDone: (brand: SkuBrand, outcome: Outcome) => void; onCancel: () => void; onReload: () => void;
}) {
  const { t } = useTranslation();
  const brand = editor.action === 'create' ? undefined : editor.brand;
  const [name, setName] = useState(brand?.name ?? '');
  const [code, setCode] = useState(brand?.code ?? '');
  const [aliases, setAliases] = useState(brand?.aliases.filter(alias => alias !== brand.name).join('\n') ?? '');
  const [source, setSource] = useState(brand?.source_ref ?? '');
  const [keepPreviousName, setKeepPreviousName] = useState(true);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const removing = editor.action === 'remove';
  const restoring = editor.action === 'restore';
  const editing = !removing && !restoring;
  const stale = ['sku_brand_changed', 'sku_brand_not_found', 'sku_brand_archived'].includes(error);
  const renamed = !!brand && name.trim() !== brand.name;
  const names = aliases.split('\n').map(value => value.trim()).filter(Boolean);
  if (renamed && keepPreviousName) names.push(brand.name);
  const aliasValues = Array.from(new Set(names));
  const invalidAliases = (editor.action === 'create' && aliasValues.length > 50) || aliasValues.some(value => value.length > 100);
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy || stale || (editing && (!name.trim() || !/^[A-Z]{2}$/.test(code) || invalidAliases))) return;
    setBusy(true);
    setError('');
    try {
      if (editor.action === 'remove') {
        const { archived } = await removeSkuBrand(editor.brand);
        onDone({ ...editor.brand, archived }, archived ? 'archived' : 'removed');
      } else if (editor.action === 'restore') {
        onDone(await restoreSkuBrand(editor.brand), 'restored');
      } else {
        const sourceRef = source.split('\n').map(line => line.trim()).filter(Boolean).join('\n');
        const values = { name: name.trim(), code, aliases: aliasValues, kind: brand?.kind ?? 'brand', source_ref: sourceRef || 'operator:sku-registry' };
        const saved = editor.action === 'edit' ? await updateSkuBrand(editor.brand, values) : await createSkuBrand(values);
        onDone(saved, 'saved');
      }
    } catch (cause) {
      setError((cause as { code?: string })?.code || 'registryWriteFailed');
    } finally {
      setBusy(false);
    }
  }
  const titleKey = { create: 'add', edit: 'editName', remove: 'removeName', restore: 'restoreName' }[editor.action];
  const title = t(`skuPolicy.registry.${titleKey}`, { name: brand?.name });
  return <form onSubmit={submit} aria-label={title} className="sku-registry-editor">
    <h2>{title}</h2>
    {!editing ? <p>{t(removing ? 'skuPolicy.registry.confirmRemove' : 'skuPolicy.registry.confirmRestore', { name: brand.name, code: brand.code })}</p> :
      <fieldset disabled={busy || stale}>
        <div className="sku-registry-fields">
          <label>{t('skuPolicy.registry.manufacturer')}<input autoFocus required maxLength={100} value={name} onChange={event => setName(event.target.value)} /></label>
          <label>{t('skuPolicy.registry.prefix')}<input required maxLength={2} pattern="[A-Z]{2}" placeholder="AB" value={code}
            onChange={event => setCode(event.target.value.toUpperCase())} /></label>
        </div>
        {brand && code !== brand.code && /^[A-Z]{2}$/.test(code) && <p>{t('skuPolicy.registry.futurePrefix', { code })}</p>}
        <label>{t('skuPolicy.registry.aliasLines')}
          <textarea rows={3} value={aliases} onChange={event => setAliases(event.target.value)} />
        </label>
        {renamed && <label className="sku-keep-name"><input type="checkbox" checked={keepPreviousName}
          onChange={event => setKeepPreviousName(event.target.checked)} />{t('skuPolicy.registry.keepName', { name: brand.name })}</label>}
        <label>{t('skuPolicy.registry.source')}
          <textarea rows={2} maxLength={2000} placeholder={t('skuPolicy.registry.sourceHint')} value={source} onChange={event => setSource(event.target.value)} />
        </label>
        {invalidAliases && <p role="alert">{t(editor.action === 'create' ? 'skuPolicy.registry.aliasLimit' : 'skuPolicy.registry.aliasLength')}</p>}
      </fieldset>}
    {error && <p role="alert">{t(`skuPolicy.errors.${error}`, { defaultValue: t('skuPolicy.errors.registryWriteFailed') })}</p>}
    <div className="sku-registry-actions">
      <Button type="button" variant="secondary" autoFocus={!editing} disabled={busy} onClick={onCancel}>{t('common.cancel')}</Button>
      {stale ? <Button type="button" onClick={onReload}>{t('skuPolicy.registry.reload')}</Button> :
        <Button type="submit" variant={removing ? 'danger' : 'primary'} disabled={busy || (editing && invalidAliases)}>
          {t(removing ? 'skuPolicy.registry.remove' : restoring ? 'skuPolicy.registry.restore' : 'common.save')}
        </Button>}
    </div>
  </form>;
}

export function SkuBrandsPage() {
  const { t } = useTranslation();
  const [brands, setBrands] = useState<SkuBrand[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [editor, setEditor] = useState<Editor | null>(null);
  const [message, setMessage] = useState('');
  const addButton = useRef<HTMLDivElement>(null);
  const opener = useRef<HTMLElement | null>(null);

  useEffect(() => {
    const request = new AbortController();
    getSkuBrands(request.signal, true).then(result => {
      if (!request.signal.aborted) setBrands(result.brands);
    }).catch(() => {
      if (!request.signal.aborted) setFailed(true);
    });
    return () => request.abort();
  }, [attempt]);

  function open(next: Editor) {
    opener.current = document.activeElement as HTMLElement;
    setMessage('');
    setEditor(next);
  }
  function close() {
    setEditor(null);
    window.setTimeout(() => {
      if (opener.current?.isConnected && opener.current !== document.body) opener.current.focus();
      else addButton.current?.querySelector('button')?.focus();
    });
  }
  function reload() {
    setEditor(null);
    setBrands(null);
    setFailed(false);
    setAttempt(value => value + 1);
  }
  function done(brand: SkuBrand, outcome: Outcome) {
    setBrands(current => {
      const remaining = (current ?? []).filter(row => row.id !== brand.id);
      return outcome === 'removed' ? remaining : [...remaining, brand];
    });
    setMessage(t(`skuPolicy.registry.${outcome}`, { name: brand.name, code: brand.code }));
    close();
  }
  return <div className="space-y-4 sku-registry-page">
    <Link to="/settings">{t('common.settings')}</Link>
    <header className="sku-registry-header">
      <div><h1 className="text-2xl font-semibold">{t('skuPolicy.registry.title')}</h1>
        <p>{t('skuPolicy.registry.help')}</p></div>
      {brands && <div ref={addButton} hidden={!!editor}>
        <Button type="button" onClick={() => open({ action: 'create' })}>{t('skuPolicy.registry.add')}</Button>
      </div>}
    </header>
    {failed ? <div role="alert">
      <p>{t('skuPolicy.errors.registryUnavailable')}</p>
      <Button variant="secondary" onClick={reload}>{t('skuPolicy.retry')}</Button>
    </div> : brands ? <>
      {message && <p role="status">{message}</p>}
      {editor && <BrandEditor editor={editor} onDone={done} onCancel={close} onReload={reload} />}
      <div hidden={!!editor}>
        <SkuBrandTable brands={brands} onEdit={brand => open({ action: 'edit', brand })}
          onRemove={brand => open({ action: 'remove', brand })} onRestore={brand => open({ action: 'restore', brand })} />
      </div>
    </> : <p role="status">{t('common.loading')}</p>}
  </div>;
}
