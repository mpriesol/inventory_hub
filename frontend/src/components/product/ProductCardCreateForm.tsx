import React, { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { createProductCard, ProductCardCreate, ProductEditorRow } from '../../api/productEditor';
import { accessRevision } from '../../api/access';
import { Button } from '../ui/Button.new';

export function ProductCardCreateForm({ onCreated, onClose }: { onCreated: (row: ProductEditorRow) => void; onClose: () => void }) {
  const { t } = useTranslation();
  const [sku, setSku] = useState(''), [name, setName] = useState(''), [active, setActive] = useState(true);
  const [busy, setBusy] = useState(false), [error, setError] = useState('');
  const request = useRef<ProductCardCreate | null>(null), sending = useRef(false);
  const [uncertain, setUncertain] = useState(false);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  async function submit() {
    const started = accessRevision();
    if (sending.current) return;
    sending.current = true; setBusy(true); setError('');
    request.current ||= { request_id: crypto.randomUUID(), sku: sku.trim(), name: name.trim(), is_active: active };
    try {
      const result = await createProductCard(request.current);
      if (!mounted.current || started !== accessRevision()) return;
      const row = result.results[0]?.row;
      if (!row) throw new Error('missing result');
      onCreated(row);
    } catch (value) {
      if (!mounted.current || started !== accessRevision()) return;
      const code = (value as Error & { code?: string }).code;
      if (['product_editor_identity_conflict', 'product_editor_invalid_request', 'product_editor_request_reused'].includes(code || '')) {
        request.current = null; setUncertain(false); setError(code!);
      } else { setUncertain(true); setError('create_uncertain'); }
    } finally { sending.current = false; setBusy(false); }
  }
  return <form className="product-editor-create" aria-label={t('productEditor.newCard')} onSubmit={event => { event.preventDefault(); submit(); }}>
    <h2>{t('productEditor.newCard')}</h2><p>{t('productEditor.newCardHelp')}</p>
    <label>{t('productEditor.fields.sku')}<input data-testid="create-sku" required maxLength={100} disabled={busy || uncertain} value={sku} onChange={event => setSku(event.target.value)} /></label>
    <label>{t('productEditor.fields.name')}<input data-testid="create-name" required maxLength={500} disabled={busy || uncertain} value={name} onChange={event => setName(event.target.value)} /></label>
    <label>{t('productEditor.fields.is_active')}<select data-testid="create-activity" disabled={busy || uncertain} value={String(active)} onChange={event => setActive(event.target.value === 'true')}><option value="true">{t('productEditor.activityStates.active')}</option><option value="false">{t('productEditor.activityStates.inactive')}</option></select></label>
    {error && <p role="alert">{t(`productEditor.errors.${error}`, { defaultValue: t('productEditor.errors.request_failed') })}</p>}
    <Button data-testid="create-submit" type="submit" disabled={busy || !sku.trim() || !name.trim()}>{t(uncertain ? 'productEditor.retryCreate' : 'productEditor.createCard')}</Button>
    <Button type="button" variant="ghost" disabled={busy} onClick={onClose}>{t('productEditor.cancelCreate')}</Button>
  </form>;
}
