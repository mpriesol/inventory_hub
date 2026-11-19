import React, { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { accessRevision } from '../../api/access';
import { Button } from '../ui/Button.new';
import { catalogPendingKey, clearCatalogPending, readCatalogPending, rememberCatalogPending,
  AvailabilityChange, AvailabilityOption, getCatalogRequest, listAvailabilityOptions, listWarehouses,
  saveAvailabilityOption, saveWarehouseOption, WarehouseChange, WarehouseOption } from '../../api/catalogs';

type Item = AvailabilityOption | WarehouseOption;
type Change = AvailabilityChange | WarehouseChange;
export function CatalogSettingsPanel({ kind, disabled = false, onChanged }: { kind: 'availability' | 'warehouses'; disabled?: boolean; onChanged?: () => void }) {
  const { t } = useTranslation();
  const c = (key: string) => t(`catalogs.${key}`);
  const [items, setItems] = useState<Item[]>([]), [selected, setSelected] = useState<Item | null>(null);
  const [name, setName] = useState(''), [code, setCode] = useState(''), [address, setAddress] = useState('');
  const [active, setActive] = useState(true), [isDefault, setDefault] = useState(false), [order, setOrder] = useState('0');
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [saved, setSaved] = useState(false);
  const pendingKey = catalogPendingKey(kind);
  const [pending, setPending] = useState<Change | null>(() => readCatalogPending<Change>(pendingKey)), [missing, setMissing] = useState(false);
  const sending = useRef(false), mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const load = async (signal?: AbortSignal) => {
    const result = kind === 'availability' ? await listAvailabilityOptions(signal, true) : await listWarehouses(signal, true);
    if (!signal?.aborted && mounted.current) setItems(result.items);
  };
  useEffect(() => { const controller = new AbortController(); load(controller.signal).catch(e => { if (!controller.signal.aborted) setError(e.code || 'request_failed'); }); return () => controller.abort(); }, [kind]);
  const select = (item: Item | null) => {
    setSelected(item); setName(item ? 'label' in item ? item.label : item.name : '');
    setCode(item && 'code' in item ? item.code : ''); setAddress(item && 'address' in item ? item.address || '' : '');
    setActive(item?.is_active ?? true); setDefault(item && 'is_default' in item ? item.is_default : false);
    setOrder(item && 'sort_order' in item ? String(item.sort_order) : '0'); setError(''); setSaved(false);
  };
  const send = async (body: Change, recover = false) => {
    if (sending.current || (!recover && disabled)) return;
    const started = accessRevision();
    if (!recover) {
      try { rememberCatalogPending(pendingKey, body); } catch { setError('catalog_storage_unavailable'); return; }
    }
    sending.current = true; setBusy(true); setPending(body); setError('');
    try {
      if (recover) await getCatalogRequest(body.request_id);
      else if ('label' in body) await saveAvailabilityOption(body); else await saveWarehouseOption(body);
      if (!mounted.current || accessRevision() !== started) return;
      clearCatalogPending(pendingKey, body); setPending(null); setMissing(false); select(null); setSaved(true); onChanged?.(); await load();
    } catch (e) {
      if (!mounted.current || accessRevision() !== started) return;
      const failure = e as { code?: string; status?: number };
      if (recover && failure.status === 404) setMissing(true);
      else if (!recover && failure.status && failure.status < 500 && failure.status !== 429 && failure.code !== 'catalog_request_reused') { clearCatalogPending(pendingKey, body); setPending(null); setMissing(false); }
      setError(failure.code || 'request_failed');
    } finally { sending.current = false; if (mounted.current && accessRevision() === started) setBusy(false); }
  };
  const submit = () => {
    const body: Change = kind === 'availability' ? { request_id: crypto.randomUUID(), id: selected?.id || null,
      expected_revision: selected && 'revision' in selected ? selected.revision : 0, label: name.trim(), is_active: active, sort_order: Number(order) }
      : { request_id: crypto.randomUUID(), code: code.trim(), expected_updated_at: selected && 'updated_at' in selected ? selected.updated_at : null,
        name: name.trim(), address: address.trim() || null, is_active: active, is_default: isDefault };
    void send(body);
  };
  const locked = disabled || busy || !!pending;
  return <section id={kind === 'availability' ? 'dictionary' : 'warehouses'} className="opening-stock-panel" data-testid={`catalog-${kind}`}>
    <h2>{c(kind === 'availability' ? 'availabilityTitle' : 'warehousesTitle')}</h2>
    <p>{c(kind === 'availability' ? 'availabilityHelp' : 'warehouseHelp')}</p>
    {error && <p role="alert" className="opening-stock-errors">{t(`catalogs.errors.${error}`, { defaultValue: c('failed') })}</p>}
    {saved && <p role="status" className="opening-stock-success">{c('saved')}</p>}
    {pending && <div role="status"><p>{c('uncertain')}</p><Button variant="secondary" disabled={busy} onClick={() => void send(pending, true)}>{c('recover')}</Button>{missing && <Button disabled={busy || disabled} onClick={() => void send(pending)}>{c('retry')}</Button>}</div>}
    <div className="opening-stock-table-scroll"><table><thead><tr><th>{c('name')}</th>{kind === 'warehouses' && <th>{c('code')}</th>}<th>{c('state')}</th><th /></tr></thead><tbody>{items.map(item => <tr key={item.id}><td>{'label' in item ? item.label : item.name}{'is_default' in item && item.is_default && ` (${c('default')})`}</td>{'code' in item && <td>{item.code}</td>}<td>{c(item.is_active ? 'active' : 'inactive')}</td><td><Button variant="secondary" disabled={locked} onClick={() => select(item)}>{c('edit')}</Button></td></tr>)}</tbody></table></div>
    <form onSubmit={event => { event.preventDefault(); submit(); }}><fieldset disabled={locked} style={{ border: 0, padding: 0 }}>
      <legend>{c(selected ? 'edit' : 'add')}</legend>
      {kind === 'warehouses' && <label>{c('code')}<input data-testid="catalog-code" type="text" value={code} disabled={!!selected} pattern="[a-zA-Z0-9][a-zA-Z0-9_-]{0,49}" maxLength={50} required onChange={event => setCode(event.target.value)} /></label>}
      <label>{c('name')}<input data-testid="catalog-name" type="text" value={name} maxLength={kind === 'availability' ? 100 : 255} required onChange={event => setName(event.target.value)} /></label>
      {kind === 'availability' ? <label>{c('order')}<input type="number" min={-100000} max={100000} step={1} value={order} required onChange={event => setOrder(event.target.value)} /></label> : <label>{c('address')}<input type="text" value={address} maxLength={2000} onChange={event => setAddress(event.target.value)} /></label>}
      <label className="availability-check"><input type="checkbox" checked={active} onChange={event => setActive(event.target.checked)} />{c('active')}</label>
      {kind === 'warehouses' && <label className="availability-check"><input type="checkbox" checked={isDefault} disabled={!active || !!(selected && 'is_default' in selected && selected.is_default)} onChange={event => setDefault(event.target.checked)} />{c('default')}</label>}
      <Button data-testid="catalog-save" type="submit" disabled={!name.trim() || kind === 'warehouses' && !code.trim()}>{c('save')}</Button>{selected && <Button type="button" variant="secondary" onClick={() => select(null)}>{c('add')}</Button>}
    </fieldset></form>
  </section>;
}
