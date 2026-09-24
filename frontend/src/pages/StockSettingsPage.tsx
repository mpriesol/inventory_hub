import React, { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Button } from '../components/ui/Button.new';
import { ActionScope } from '../components/ui/ActionScope';
import { accessRevision, hubUnlocked, subscribeAccess, unlockHub } from '../api/access';
import { getStockSettingsOptions, getWarehouseSettings, saveShopStockSettings, saveWarehouseSettings,
  STOCK_SETTING_FIELDS, ProcessingMode, StockSettingKey, StockSettingValues, StockSettingsOptions, WarehouseSettings } from '../api/stockSettings';
import './OpeningStockPage.css';
import './StockSettingsPage.css';

type Inputs = Partial<Record<StockSettingKey, string>>;
type Operation = 'options' | 'warehouse' | 'save-warehouse' | 'save-shop' | null;
const inputs = (values: Partial<StockSettingValues>): Inputs => Object.fromEntries(Object.entries(values).map(([key, value]) => [key, String(value)]));
function numbers(values: Inputs, inherited?: StockSettingValues): StockSettingValues | null {
  const result = { ...inherited } as StockSettingValues;
  for (const field of STOCK_SETTING_FIELDS) {
    if (values[field.key] === undefined && inherited) continue;
    const raw = values[field.key] ?? '';
    if (!/^\d+$/.test(raw)) return null;
    const value = Number(raw);
    if (!Number.isInteger(value) || value < field.min || value > field.max) return null;
    result[field.key] = value;
  }
  return result.retry_max_seconds >= result.retry_base_seconds ? result : null;
}

export function StockSettingsPage() {
  const { t, i18n } = useTranslation();
  const [query] = useSearchParams();
  const revision = useSyncExternalStore(subscribeAccess, accessRevision);
  const [shop, setShop] = useState(query.get('shop') === 'xtrek' ? 'xtrek' : 'biketrek');
  const [token, setToken] = useState('');
  const [loaded, setLoaded] = useState<{ shop: string; revision: number; value: StockSettingsOptions } | null>(null);
  const options = loaded?.shop === shop && loaded.revision === revision ? loaded.value : null;
  const [warehouseCode, setWarehouseCode] = useState('');
  const [warehouse, setWarehouse] = useState<WarehouseSettings | null>(null);
  const [warehouseValues, setWarehouseValues] = useState<Inputs>({});
  const [paused, setPaused] = useState(false);
  const [overrides, setOverrides] = useState<Inputs>({});
  const [mode, setMode] = useState<ProcessingMode>('manual');
  const [warehouseConfirm, setWarehouseConfirm] = useState(false);
  const [shopConfirm, setShopConfirm] = useState(false);
  const [fulfillmentConfirm, setFulfillmentConfirm] = useState(false);
  const [busy, setBusy] = useState<Operation>(null);
  const [uncertain, setUncertain] = useState<'warehouse' | 'shop' | null>(null);
  const [optionsStale, setOptionsStale] = useState(false);
  const [error, setError] = useState('');
  const [saved, setSaved] = useState('');
  const generation = useRef(0), controller = useRef<AbortController | null>(null), busyRef = useRef<Operation>(null), mutation = useRef(false);
  const writing = busy === 'save-shop' || busy === 'save-warehouse';
  function clearConfirmations() { setWarehouseConfirm(false); setShopConfirm(false); setFulfillmentConfirm(false); setSaved(''); setError(''); }
  function reset() {
    generation.current += 1; controller.current?.abort(); busyRef.current = null; setBusy(null);
    setLoaded(null); setWarehouse(null); setWarehouseCode(''); setWarehouseValues({}); setOverrides({}); setMode('manual'); setPaused(false);
    setUncertain(null); setOptionsStale(false); clearConfirmations();
  }
  useEffect(() => { reset(); setToken(''); return () => { generation.current += 1; controller.current?.abort(); }; }, [revision]);
  function report(value: unknown) {
    const code = (value as Error & { code?: string }).code || 'request_failed';
    if (code === 'hub_access_required' || code === 'hub_access_not_configured') unlockHub('');
    setError(code);
  }
  function acceptWarehouse(value: WarehouseSettings) { setWarehouse(value); setWarehouseCode(value.warehouse_code); setWarehouseValues(inputs(value.values)); setPaused(value.processing_paused); }
  function acceptOptions(value: StockSettingsOptions) {
    setLoaded({ shop, revision: accessRevision(), value }); setOverrides(inputs(value.shop_settings?.overrides || {})); setMode(value.shop_settings?.mode || 'manual');
    setWarehouseCode(value.policy?.warehouse_code || ''); setWarehouse(value.warehouse);
    setWarehouseValues(inputs(value.warehouse?.values || {})); setPaused(value.warehouse?.processing_paused || false);
    setOptionsStale(false); setUncertain(null); clearConfirmations();
  }
  async function read<T>(operation: Operation, request: (signal: AbortSignal) => Promise<T>, accept: (value: T) => void) {
    if (busyRef.current || !hubUnlocked()) return;
    const id = ++generation.current, credential = accessRevision(), abort = new AbortController(); controller.current?.abort(); controller.current = abort;
    busyRef.current = operation; setBusy(operation); clearConfirmations();
    const current = () => id === generation.current && credential === accessRevision() && !abort.signal.aborted;
    try { const value = await request(abort.signal); if (current()) accept(value); }
    catch (value) { if (current()) report(value); }
    finally { if (current()) { busyRef.current = null; setBusy(null); } }
  }
  function loadOptions() { read('options', signal => getStockSettingsOptions(shop, signal), acceptOptions); }
  function loadWarehouse() { if (warehouseCode) read('warehouse', signal => getWarehouseSettings(warehouseCode, signal), value => {
    acceptWarehouse(value);
    if (options?.warehouse?.warehouse_code === value.warehouse_code && options.warehouse.revision !== value.revision) setOptionsStale(true);
    if (uncertain === 'warehouse') setUncertain(null);
  }); }
  function changeWarehouse(code: string) {
    generation.current += 1; controller.current?.abort(); busyRef.current = null; setBusy(null); setWarehouseCode(code); setWarehouse(null); setWarehouseValues({}); clearConfirmations();
  }
  async function save(target: 'warehouse' | 'shop') {
    if (busyRef.current || mutation.current || !options || uncertain) return;
    const warehouseNumbers = target === 'warehouse' ? numbers(warehouseValues) : null;
    const shopNumbers = target === 'shop' && options.warehouse ? numbers(overrides, options.warehouse.values) : null;
    if (target === 'warehouse' && (!warehouse || !warehouseConfirm)) return;
    if (target === 'shop' && (!shopConfirm || !options.policy || optionsStale || (mode === 'fulfill' && !fulfillmentConfirm))) return;
    if (target === 'warehouse' ? !warehouseNumbers : !shopNumbers) { setError('stock_settings_invalid_values'); return; }
    const id = generation.current, credential = accessRevision(); mutation.current = true; busyRef.current = `save-${target}`; setBusy(`save-${target}`); setError(''); setSaved('');
    const current = () => id === generation.current && credential === accessRevision();
    try {
      if (target === 'warehouse') {
        const value = await saveWarehouseSettings({ warehouse_code: warehouse!.warehouse_code, expected_revision: warehouse!.revision, values: warehouseNumbers!, processing_paused: paused, confirmed: true });
        if (current()) { acceptWarehouse(value); setOptionsStale(true); clearConfirmations(); setSaved('warehouseSaved'); }
      } else {
        const selected = Object.fromEntries(Object.keys(overrides).map(key => [key, shopNumbers![key as StockSettingKey]]));
        const value = await saveShopStockSettings({ shop_code: shop, expected_revision: options.shop_settings?.revision ?? 0, expected_warehouse_revision: options.warehouse?.revision ?? 0,
          overrides: selected, mode, confirmed: true, fulfillment_confirmed: mode === 'fulfill' && fulfillmentConfirm });
        if (current()) { acceptOptions(value); setSaved('shopSaved'); }
      }
    } catch (value) { if (current()) { setUncertain(target); clearConfirmations(); report(value); } }
    finally { mutation.current = false; if (current()) { busyRef.current = null; setBusy(null); } }
  }
  const date = (value: string | null | undefined) => value ? new Date(value).toLocaleString(i18n.language) : '—';
  function fields(target: 'warehouse' | 'shop', advanced: boolean) {
    return <div className="stock-settings-fields">{STOCK_SETTING_FIELDS.filter(field => field.advanced === advanced).map(field => {
      const inherited = overrides[field.key] === undefined;
      return <div className="stock-settings-field" key={field.key}>
        <label>{t(`stockSettings.fields.${field.key}`)}<input data-testid={`${target === 'warehouse' ? 'warehouse' : 'override'}-${field.key}`} type="number" step="1" min={field.min} max={field.max}
          disabled={!!busy || (target === 'shop' && inherited)} value={target === 'warehouse' ? warehouseValues[field.key] ?? '' : overrides[field.key] ?? options?.warehouse?.values[field.key] ?? ''}
          onChange={event => { clearConfirmations(); const value = event.target.value; target === 'warehouse' ? setWarehouseValues(previous => ({ ...previous, [field.key]: value })) : setOverrides(previous => ({ ...previous, [field.key]: value })); }} /></label>
        <small>{t('stockSettings.range', { min: field.min, max: field.max })}</small>
        {target === 'shop' && <><label className="stock-settings-inherit"><input data-testid={`inherit-${field.key}`} type="checkbox" checked={inherited} disabled={!!busy} onChange={event => {
          clearConfirmations(); setOverrides(previous => { const result = { ...previous }; if (event.target.checked) delete result[field.key]; else result[field.key] = String(options?.warehouse?.values[field.key] ?? ''); return result; });
        }} />{t('stockSettings.inherit', { value: options?.warehouse?.values[field.key] })}</label>
          <small>{t('stockSettings.savedEffective', { value: options?.effective?.values[field.key], source: t(`stockSettings.sources.${options?.effective?.sources[field.key] || 'warehouse'}`) })}</small></>}
      </div>;
    })}</div>;
  }
  return <div className="opening-stock stock-settings">
    <Link className="opening-stock-back" to="/settings">← {t('stockSettings.back')}</Link>
    <header><div><h1>{t('stockSettings.title')}</h1><p>{t('stockSettings.subtitle')}</p></div>{hubUnlocked() && <Button variant="secondary" onClick={() => unlockHub('')}>{t('stockSettings.lock')}</Button>}</header>
    <div className="opening-stock-notice">{t('stockSettings.scope')}</div>
    {!hubUnlocked() ? <form className="opening-stock-panel opening-stock-unlock" onSubmit={event => { event.preventDefault(); if (token.trim()) unlockHub(token.trim()); }}>
      <p>{t('stockSettings.tokenHelp')}</p><label>{t('stockSettings.token')}<input data-testid="unlock-token" type="password" autoComplete="off" value={token} onChange={event => setToken(event.target.value)} /></label>
      <Button data-testid="unlock" type="submit" disabled={!token.trim()}>{t('stockSettings.unlock')}</Button>
    </form> : <>
      <section className="opening-stock-panel"><div className="opening-stock-actions"><label>{t('stockSettings.shop')}<select data-testid="shop" value={shop} disabled={writing} onChange={event => { reset(); setShop(event.target.value); }}><option value="biketrek">BIKETREK</option><option value="xtrek">xTrek</option></select></label>
        <span className="action-control"><Button data-testid="load-options" variant="secondary" disabled={!!busy} onClick={loadOptions}>{t('stockSettings.loadOptions')}</Button><ActionScope effects={['hub-read']} /></span></div>
        <p><Link to={`/orders/inbox?shop=${encodeURIComponent(shop)}`}>{t('orderCollection.open')}</Link> · <Link to={`/orders/stock?shop=${encodeURIComponent(shop)}`}>{t('stockSettings.policyLink')}</Link></p>
      </section>
      {uncertain && <div role="alert" className="opening-stock-notice">{t(uncertain === 'warehouse' ? 'stockSettings.warehouseUncertain' : 'stockSettings.shopUncertain')}</div>}
      {saved && <div role="status" className="opening-stock-success">{t(`stockSettings.${saved}`)}</div>}
      {options && <>
        <section className="opening-stock-panel"><h2>{t('stockSettings.warehouseTitle')}</h2><p>{t('stockSettings.warehouseHelp')}</p>
          <div className="opening-stock-actions"><label>{t('stockSettings.warehouse')}<select data-testid="warehouse" value={warehouseCode} disabled={writing} onChange={event => changeWarehouse(event.target.value)}><option value="">{t('stockSettings.chooseWarehouse')}</option>{options.warehouses.map(item => <option key={item.code} value={item.code}>{item.name}</option>)}</select></label>
            <span className="action-control"><Button data-testid="load-warehouse" variant="secondary" disabled={!!busy || !warehouseCode} onClick={loadWarehouse}>{t('stockSettings.loadWarehouse')}</Button><ActionScope effects={['hub-read']} /></span></div>
          {warehouse && <>{fields('warehouse', false)}<details><summary>{t('stockSettings.advanced')}</summary>{fields('warehouse', true)}</details>
            <div className="opening-stock-confirmations"><label><input data-testid="warehouse-paused" type="checkbox" checked={paused} disabled={!!busy} onChange={event => { clearConfirmations(); setPaused(event.target.checked); }} />{t('stockSettings.processingPaused')}</label><small>{t('stockSettings.pauseHelp')}</small>
              <label><input data-testid="warehouse-confirm" type="checkbox" checked={warehouseConfirm} disabled={!!busy || !!uncertain} onChange={event => setWarehouseConfirm(event.target.checked)} />{t('stockSettings.warehouseConfirm')}</label></div>
            <span className="action-control"><Button data-testid="save-warehouse" disabled={!!busy || !!uncertain || !warehouseConfirm || !numbers(warehouseValues)} onClick={() => save('warehouse')}>{t('stockSettings.saveWarehouse')}</Button><ActionScope effects={['hub-write']} /></span>
          </>}
        </section>
        <section className="opening-stock-panel"><h2>{t('stockSettings.shopTitle', { shop: options.shop.name })}</h2>
          {!options.policy ? <p><Link to={`/orders/stock?shop=${encodeURIComponent(shop)}`}>{t('stockSettings.policyRequired')}</Link></p> : optionsStale ? <p className="opening-stock-notice">{t('stockSettings.reloadAfterWarehouse')}</p> : <>
            <p>{t('stockSettings.inheritanceHelp', { warehouse: options.policy.warehouse_code })}</p>{fields('shop', false)}<details><summary>{t('stockSettings.advanced')}</summary>{fields('shop', true)}</details>
            <label className="stock-settings-mode">{t('stockSettings.mode')}<select data-testid="mode" value={mode} disabled={!!busy} onChange={event => { clearConfirmations(); setMode(event.target.value as ProcessingMode); }}>{['manual', 'reserve', 'fulfill'].map(value => <option key={value} value={value}>{t(`stockSettings.modes.${value}`)}</option>)}</select></label>
            <p>{t(`stockSettings.modeHelp.${mode}`)}</p><p className="opening-stock-muted">{t('stockSettings.cutoverHelp')}</p>
            {options.effective?.processing_error && <div className="opening-stock-errors" role="alert">{t(`orderCollection.processingErrors.${options.effective.processing_error}`, { defaultValue: t('stockSettings.errors.request_failed') })}</div>}
            {options.effective?.processing_paused && <p className="opening-stock-notice">{t('stockSettings.currentlyPaused')}</p>}
            <p className="opening-stock-muted">{t('stockSettings.activationDates', { automation: date(options.shop_settings?.automation_starts_at), issue: date(options.shop_settings?.issue_starts_at) })}</p>
            <div className="opening-stock-confirmations"><label><input data-testid="shop-confirm" type="checkbox" checked={shopConfirm} disabled={!!busy || !!uncertain} onChange={event => setShopConfirm(event.target.checked)} />{t('stockSettings.shopConfirm')}</label>
              {mode === 'fulfill' && <label><input data-testid="fulfillment-confirm" type="checkbox" checked={fulfillmentConfirm} disabled={!!busy || !!uncertain} onChange={event => setFulfillmentConfirm(event.target.checked)} />{t('stockSettings.fulfillmentConfirm')}</label>}</div>
            <span className="action-control"><Button data-testid="save-shop" disabled={!!busy || !!uncertain || !shopConfirm || (mode === 'fulfill' && !fulfillmentConfirm) || !options.warehouse || !numbers(overrides, options.warehouse.values)} onClick={() => save('shop')}>{t('stockSettings.saveShop')}</Button><ActionScope effects={mode === 'manual' ? ['hub-write'] : ['hub-write', 'queued-upgates', 'upgates-read']} shop={shop} calls={{ kind: 'variable' }} /></span>
          </>}
        </section>
      </>}
    </>}{error && <div role="alert" className="opening-stock-errors">{t(`stockSettings.errors.${error}`, { defaultValue: t('stockSettings.errors.request_failed') })}</div>}
  </div>;
}
