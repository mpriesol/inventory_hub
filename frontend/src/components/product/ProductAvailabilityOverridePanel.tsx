import React, { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { accessRevision } from '../../api/access';
import { catalogPendingKey, clearCatalogPending, readCatalogPending, rememberCatalogPending,
  AvailabilityOption, listAvailabilityOptions, ProductAvailabilityInput, saveProductAvailability } from '../../api/catalogs';
import { getEditorSave, ProductEditorDetail } from '../../api/productEditor';
import { Button } from '../ui/Button.new';

export function ProductAvailabilityOverridePanel({ row, disabled, onChanged, onDirtyChange, onBusyChange }: {
  row: ProductEditorDetail; disabled: boolean; onChanged: () => void;
  onDirtyChange: (value: boolean) => void; onBusyChange: (value: boolean) => void;
}) {
  const { t, i18n } = useTranslation();
  const c = (key: string) => t(`catalogs.${key}`);
  const manual = row.availability_override;
  const [options, setOptions] = useState<AvailabilityOption[]>([]);
  const [label, setLabel] = useState(manual?.label || ''), [visible, setVisible] = useState(manual?.visible === undefined ? '' : String(manual.visible));
  const [busy, setBusy] = useState(false), [error, setError] = useState('');
  const pendingKey = catalogPendingKey(`product-availability:${row.id}`);
  const [pending, setPending] = useState<ProductAvailabilityInput | null>(() => readCatalogPending<ProductAvailabilityInput>(pendingKey)), [missing, setMissing] = useState(false);
  const sending = useRef(false), mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const dirty = label !== (manual?.label || '') || visible !== (manual?.visible === undefined ? '' : String(manual.visible));
  useEffect(() => { const controller = new AbortController(); listAvailabilityOptions(controller.signal).then(result => { if (!controller.signal.aborted) setOptions(result.items); }).catch(e => { if (!controller.signal.aborted) setError(e.code || 'request_failed'); }); return () => controller.abort(); }, []);
  useEffect(() => { setLabel(manual?.label || ''); setVisible(manual?.visible === undefined ? '' : String(manual.visible)); }, [row.id, row.revision]);
  useEffect(() => { onDirtyChange(dirty || !!pending); return () => onDirtyChange(false); }, [dirty, pending, onDirtyChange]);
  useEffect(() => { onBusyChange(busy); return () => onBusyChange(false); }, [busy, onBusyChange]);
  const send = async (body: ProductAvailabilityInput, recover = false) => {
    if (sending.current || (!recover && disabled)) return;
    const started = accessRevision();
    if (!recover) {
      try { rememberCatalogPending(pendingKey, body); } catch { setError('catalog_storage_unavailable'); return; }
    }
    sending.current = true; setBusy(true); setPending(body); setError('');
    try {
      if (recover) await getEditorSave(body.request_id); else await saveProductAvailability(body);
      if (!mounted.current || accessRevision() !== started) return;
      clearCatalogPending(pendingKey, body); setPending(null); setMissing(false); onChanged();
    } catch (e) {
      if (!mounted.current || accessRevision() !== started) return;
      const failure = e as { code?: string; status?: number };
      if (recover && failure.status === 404) setMissing(true);
      else if (!recover && failure.status && failure.status < 500 && failure.status !== 429 && failure.code !== 'catalog_request_reused') { clearCatalogPending(pendingKey, body); setPending(null); setMissing(false); }
      setError(failure.code || 'request_failed');
    } finally { sending.current = false; if (mounted.current && accessRevision() === started) setBusy(false); }
  };
  const save = (clear = false) => {
    const body: ProductAvailabilityInput = { request_id: crypto.randomUUID(), changes: [{ product_id: row.id, expected_revision: row.revision,
      ...(clear ? { clear: true } : { label: label || null, visible: visible === '' ? null : visible === 'true' }) }] };
    void send(body);
  };
  const locked = disabled || busy || !!pending;
  return <section className="stock-card__block" data-testid="product-availability-override">
    <h4>{c('overrideTitle')}</h4>
    {manual ? <p className="stock-card__note"><strong>{c('manual')}</strong> · {manual.label || c('automaticAvailability')}{manual.visible !== undefined && ` · ${c(manual.visible ? 'visible' : 'hidden')}`}<br /><time dateTime={manual.updated_at}>{new Date(manual.updated_at).toLocaleString(i18n.language)}</time> · {manual.actor}</p> : <p>{c('automaticAvailability')}</p>}
    {error && <p role="alert" className="opening-stock-errors">{t(`catalogs.errors.${error}`, { defaultValue: c('failed') })}</p>}
    {pending && <div role="status"><p>{c('uncertain')}</p><Button variant="secondary" disabled={busy} onClick={() => void send(pending, true)}>{c('recover')}</Button>{missing && <Button disabled={busy || disabled} onClick={() => void send(pending)}>{c('retry')}</Button>}</div>}
    <label>{c('availability')}<select data-testid="manual-availability-label" value={label} disabled={locked} onChange={event => setLabel(event.target.value)}><option value="">{c('automatic')}</option>{label && !options.some(item => item.label === label) && <option value={label}>{label}</option>}{options.map(item => <option key={item.id} value={item.label}>{item.label}</option>)}</select></label>
    <label>{c('visibility')}<select data-testid="manual-availability-visible" value={visible} disabled={locked} onChange={event => setVisible(event.target.value)}><option value="">{c('automatic')}</option><option value="true">{c('visible')}</option><option value="false">{c('hidden')}</option></select></label>
    <p className="stock-card__note">{c('overrideHelp')}</p>
    <div className="stock-card__actions"><Button data-testid="manual-availability-save" disabled={locked || !dirty} onClick={() => save()}>{c('save')}</Button>{manual && <Button data-testid="manual-availability-clear" variant="secondary" disabled={locked} onClick={() => save(true)}>{c('clearOverride')}</Button>}</div>
  </section>;
}
