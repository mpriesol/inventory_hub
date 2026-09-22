import { CategoryTree } from '../components/product/CategoryTree';
import React, { useEffect, useRef, useState } from 'react';
import { useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { ArrowLeft, Download, Package, RefreshCw, Search, Sliders, ShoppingBag } from 'lucide-react';
import { Button } from '../components/ui/Button.new';
import { CatalogTable } from '../components/product/CatalogTable';
import { CatalogDetail } from '../components/product/CatalogDetail';
import { CatalogImport } from '../components/product/CatalogImport';
import { CatalogDownload, CatalogPage, CatalogProduct, CatalogStatus, ImportOptions, TargetOptions, ImportPreview, ImportResult, catalogRequest, catalogQuery } from '../api/catalog';
import './SupplierCatalogPage.css';

const defaults: ImportOptions = { language: 'sk', currency: 'EUR', pricelist: 'Predvolené', category_code: null, pricing: 'configured', include_images: true, include_description: true, include_parameters: true };
function stored(key: string) { try { return localStorage.getItem(key) || ''; } catch { return ''; } }
function remember(key: string, value: string) { try { localStorage.setItem(key, value); } catch { /* Storage may be disabled. */ } }

export function SupplierCatalogPage() {
  const { supplier = '' } = useParams();
  const navigate = useNavigate();
  const [url, setUrl] = useSearchParams();
  const { t } = useTranslation();
  const [feed, setFeed] = useState(url.get('feed') || 'products');
  const [q, setQ] = useState(url.get('q') || '');
  const [code, setCode] = useState('');
  const [ean, setEan] = useState('');
  const [manufacturer, setManufacturer] = useState('');
  const [listing, setListing] = useState('all');
  const [sort, setSort] = useState('name');
  const [shop, setShop] = useState(stored('catalog.targetShop'));
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);
  const [grouped, setGrouped] = useState(true);
  const [showPrice, setShowPrice] = useState(true);
  const [showAvailability, setShowAvailability] = useState(true);
  const [advanced, setAdvanced] = useState(false);
  const [status, setStatus] = useState<CatalogStatus | null>(null);
  const [data, setData] = useState<CatalogPage | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [downloaded, setDownloaded] = useState<CatalogDownload | null>(null);
  const [selecting, setSelecting] = useState(false);
  const [reload, setReload] = useState(0);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const snapshot = useRef<number | null>(null);
  const [detail, setDetail] = useState<CatalogProduct | null>(null);
  const [target, setTarget] = useState<TargetOptions | null>(null);
  const [targetLoading, setTargetLoading] = useState(false);
  const [targetError, setTargetError] = useState('');
  const [targetReload, setTargetReload] = useState(0);
  const forceTargetRefresh = useRef(false);
  const [fullShopCheck, setFullShopCheck] = useState(false);
  const [options, setOptions] = useState<ImportOptions>(defaults);
  const [preview, setPreview] = useState<ImportPreview | null>(null);
  const [result, setResult] = useState<ImportResult | null>(null);
  const [jobId, setJobId] = useState('');
  const [dialog, setDialog] = useState(false);
  const [preparing, setPreparing] = useState(false);
  const [sending, setSending] = useState(false);
  const [importError, setImportError] = useState('');
  const base = `/suppliers/${encodeURIComponent(supplier)}/catalog`;
  const filters = { feed_key: feed, q, code, ean, manufacturer, sort, listing, shop };
  const query = catalogQuery({ ...filters, page, page_size: pageSize, grouped });
  const source = status?.sources.find(s => s.key === feed);
  const shopInfo = status?.shops.find(s => s.code === shop);
  const busy = loading || refreshing || selecting || preparing;
  const message = (key: string) => t(`catalog.codes.${key}`, { defaultValue: key });

  useEffect(() => { setSelected(new Set()); snapshot.current = null; setPage(1); setDetail(null); setPreview(null); setDownloaded(null); }, [supplier, feed]);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError('');
    const timer = setTimeout(async () => {
      try {
        const info = await catalogRequest<CatalogStatus>(`${base}?feed_key=${encodeURIComponent(feed)}`, undefined, controller.signal);
        if (controller.signal.aborted) return;
        setStatus(info);
        const current = info.sources.find(s => s.key === feed);
        if (!current?.supported) { setData(null); return; }
        const response = await catalogRequest<CatalogPage>(`${base}/products?${query}`, undefined, controller.signal);
        if (controller.signal.aborted) return;
        if (snapshot.current !== null && snapshot.current !== response.run_id) {
          setSelected(new Set()); setNotice('catalog_changed'); setPreview(null);
        }
        snapshot.current = response.run_id;
        setData(response);
        if (response.pages && page > response.pages) setPage(response.pages);
      } catch (e: any) { if (e.name !== 'AbortError') setError(e.code || 'request_failed'); }
      finally { if (!controller.signal.aborted) setLoading(false); }
    }, 300);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [base, feed, query, reload]);

  useEffect(() => {
    setPage(1); setUrl({ ...(q ? { q } : {}), feed }, { replace: true });
  }, [q, code, ean, manufacturer, sort, listing, shop, feed, pageSize, grouped]);

  useEffect(() => {
    const force = forceTargetRefresh.current;
    forceTargetRefresh.current = false;
    remember('catalog.targetShop', shop);
    setPreview(null); setResult(null); setDialog(false); setImportError('');
    setJobId(shop ? stored(`catalog.import.${shop}`) : '');
    setTarget(null); setTargetError('');
    if (!shop || !shopInfo?.ready || !source?.supported) { setTargetLoading(false); return; }
    const controller = new AbortController();
    setTargetLoading(true);
    catalogRequest<TargetOptions>(`/shops/${encodeURIComponent(shop)}/import/options${force ? '?refresh=true' : ''}`, undefined, controller.signal).then(response => {
      if (controller.signal.aborted) return;
      setTarget(response);
      const language = response.languages.find(l => l.code === 'sk') || response.languages.find(l => l.default) || response.languages[0];
      const pricelist = response.pricelists.find(p => p.default) || response.pricelists[0];
      if (!force) setOptions({ ...defaults, language: language?.code || '', currency: language?.currency || '', pricelist: pricelist?.name || '',
        category_code: response.categories.some(c => c.code === status?.defaults.category_code) ? status!.defaults.category_code : null });
    }).catch(e => { if (e.name !== 'AbortError') setTargetError(e.code || 'request_failed'); })
      .finally(() => { if (!controller.signal.aborted) setTargetLoading(false); });
    return () => controller.abort();
  }, [shop, shopInfo?.ready, source?.supported, supplier, targetReload]);

  useEffect(() => {
    if (!jobId || !shop) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const response = await catalogRequest<ImportResult>(`/shops/${encodeURIComponent(shop)}/import/${jobId}`, undefined, controller.signal);
        if (controller.signal.aborted) return;
        setResult(response);
        if (response.status === 'queued' || response.status === 'running') timer = setTimeout(poll, 2500);
        else setReload(n => n + 1);
      } catch (e: any) { if (e.name !== 'AbortError') { setImportError(e.code || 'request_failed'); if (!['import_not_started', 'preview_not_found'].includes(e.code)) timer = setTimeout(poll, 10000); } }
    }
    poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [shop, jobId, sending]);

  function toggle(ids: number[]) {
    if (busy) return;
    setSelected(old => { const next = new Set(old); const remove = ids.every(id => next.has(id)); ids.forEach(id => remove ? next.delete(id) : next.add(id)); return next; });
    setPreview(null);
  }
  async function refresh() {
    setRefreshing(true); setError(''); setNotice('');
    try {
      await catalogRequest(`${base}/refresh`, { feed_key: feed });
      setSelected(new Set()); setPreview(null); setReload(n => n + 1); setNotice('catalog_refreshed');
    } catch (e: any) { setError(e.code || 'request_failed'); }
    finally { setRefreshing(false); }
  }
  async function downloadSource() {
    setDownloading(true); setError(''); setDownloaded(null);
    try {
      setDownloaded(await catalogRequest<CatalogDownload>(`${base}/download`, { feed_key: feed }));
    } catch (e: any) { setError(e.code || 'request_failed'); }
    finally { setDownloading(false); }
  }
  async function selectAll() {
    setSelecting(true); setError('');
    try {
      const all = await catalogRequest<{ ids: number[]; run_id: number | null }>(`${base}/selection?${catalogQuery(filters)}`);
      if (all.run_id !== snapshot.current) { setSelected(new Set()); setNotice('catalog_changed'); setReload(n => n + 1); }
      else { setSelected(old => new Set([...old, ...all.ids])); setPreview(null); }
    } catch (e: any) { setError(e.code || 'request_failed'); }
    finally { setSelecting(false); }
  }
  async function prepare(salePriceOverrides?: Record<number, string>, selection = [...selected]) {
    setPreparing(true); setError(''); setImportError('');
    try {
      const next = await catalogRequest<ImportPreview>(`/shops/${encodeURIComponent(shop)}/import/preview`, { supplier, feed_key: feed, product_ids: selection, run_id: snapshot.current, options, refresh_shop: salePriceOverrides ? false : fullShopCheck, sale_price_overrides: salePriceOverrides || {} });
      setPreview(next); setResult(null); setJobId(''); setDialog(true);
      setSelected(new Set(selection)); setReload(n => n + 1);
    } catch (e: any) { salePriceOverrides ? setImportError(e.code || 'request_failed') : setError(e.code || 'request_failed'); }
    finally { setPreparing(false); }
  }
  async function confirm(retry = false) {
    const id = retry ? result?.preview_id : preview?.preview_id;
    if (!id) return;
    setSending(true); setImportError('');
    try {
      const next = await catalogRequest<ImportResult>(`/shops/${encodeURIComponent(shop)}/import`, { preview_id: id, retry_failed: retry });
      remember(`catalog.import.${shop}`, id); setResult(next); setJobId(id);
    } catch (e: any) {
      setImportError(e.code || 'request_failed');
      // The server may have accepted the request before a connection failure.
      remember(`catalog.import.${shop}`, id); setJobId(id);
    } finally { setSending(false); }
  }
  const activeImport = result?.status === 'queued' || result?.status === 'running';
  return <div className="supplier-catalog">
    <button className="catalog-back" onClick={() => navigate('/suppliers')}><ArrowLeft size={16} />{t('catalog.backSuppliers')}</button>
    <header className="catalog-header"><div><div className="catalog-eyebrow">{status?.name || supplier}</div><h1>{t('catalog.title')}</h1><p className="catalog-muted">{t('catalog.subtitle')}</p></div>
      <div className="catalog-target"><label htmlFor="catalog-shop"><ShoppingBag size={15} />{t('catalog.targetShop')}</label><select id="catalog-shop" value={shop} onChange={e => setShop(e.target.value)} disabled={preparing || sending}>
        <option value="">{t('catalog.chooseShop')}</option>{status?.shops.map(s => <option key={s.code} value={s.code} disabled={!s.ready}>{s.name}{s.ready ? '' : ` · ${t('catalog.notConfigured')}`}</option>)}
      </select><small>{t('catalog.targetHelp')}</small></div></header>
    {source?.supported && shopInfo?.ready && <div className="catalog-notice" role="status">
      {targetLoading ? t('catalog.checkingShopOptions') : target?.cache && <span>{t('catalog.shopOptionsChecked', { date: new Date(target.cache.checked_at).toLocaleString() })} · {t(target.cache.from_cache ? 'catalog.shopOptionsCached' : 'catalog.shopOptionsFetched')}<br />{t('catalog.shopOptionsExpiry', { date: new Date(target.cache.expires_at).toLocaleTimeString() })}</span>}
      <Button variant="secondary" size="sm" disabled={targetLoading || preparing || sending || activeImport} onClick={() => { forceTargetRefresh.current = true; setTargetReload(n => n + 1); }}>{t('catalog.refreshShopOptions')}</Button>
    </div>}
    <div className="catalog-feed-bar"><label>{t('catalog.source')}<select value={feed} onChange={e => { setFeed(e.target.value); setManufacturer(''); }} disabled={refreshing || downloading || preparing}>{status?.sources.map(s => <option key={s.key} value={s.key}>{s.name}</option>)}</select></label>
      <div className="catalog-feed-state"><span className={`catalog-dot ${data?.run_id ? 'catalog-dot-good' : ''}`} />{data?.fetched_at ? t('catalog.lastDownload', { date: new Date(data.fetched_at).toLocaleString() }) : t('catalog.notDownloaded')}
        {status?.status === 'failed' && <span className="catalog-error">{t('catalog.lastRefreshFailed')}</span>}</div>
      <Button variant="secondary" icon={<Download size={16} />} loading={downloading} disabled={!source?.configured || refreshing || preparing} onClick={downloadSource}>{t('catalog.downloadSource')}</Button>
      <Button variant="secondary" icon={<RefreshCw size={16} />} loading={refreshing} disabled={!source?.supported || !source?.configured || downloading || preparing} onClick={refresh}>{t('catalog.downloadFeed')}</Button></div>
    {downloaded && <div role="status" className="catalog-notice">{t('catalog.sourceDownloaded', { date: new Date(downloaded.downloaded_at).toLocaleString(), size: (downloaded.size_bytes / 1024 / 1024).toFixed(2) })} · <a href={`/api/files/download?${new URLSearchParams({ relpath: downloaded.relpath })}`}>{t('catalog.saveSource')}</a></div>}
    {error && <div role="alert" className="catalog-error catalog-panel">{message(error)}</div>}
    {notice && <div role="status" className="catalog-notice">{message(notice)}</div>}
    {status && !source?.supported && <div className="catalog-empty"><Package size={36} /><h2>{t('catalog.parserPending')}</h2><p>{t('catalog.parserPendingHelp')}</p></div>}
    {source?.supported && <>
      <div className="catalog-tools"><label className="catalog-search"><Search size={18} /><input aria-label={t('catalog.search')} placeholder={t('catalog.searchPlaceholder')} value={q} onChange={e => setQ(e.target.value)} /></label>
        <select aria-label={t('catalog.manufacturer')} value={manufacturer} onChange={e => setManufacturer(e.target.value)}><option value="">{t('catalog.allBrands')}</option>{data?.manufacturers.map(m => <option key={m}>{m}</option>)}</select>
        <select aria-label={t('catalog.listingFilter')} value={listing} onChange={e => setListing(e.target.value)}><option value="all">{t('catalog.allProducts')}</option><option value="unlisted" disabled={!shop}>{t('catalog.notLinked')}</option><option value="listed" disabled={!shop}>{t('catalog.listed')}</option><option value="warnings">{t('catalog.withWarnings')}</option></select>
        <Button variant="secondary" icon={<Sliders size={16} />} aria-expanded={advanced} onClick={() => setAdvanced(!advanced)}>{t('catalog.options')}</Button></div>
      {advanced && <div className="catalog-settings catalog-panel"><div><h3>{t('catalog.searchAndView')}</h3><div className="catalog-fields">
        <label>{t('catalog.exactCode')}<input value={code} onChange={e => setCode(e.target.value)} /></label><label>{t('catalog.exactEan')}<input value={ean} onChange={e => setEan(e.target.value)} /></label>
        <label>{t('catalog.sort')}<select value={sort} onChange={e => setSort(e.target.value)}>{['name', 'code', 'manufacturer'].map(s => <option key={s} value={s}>{t(`catalog.sortBy.${s}`)}</option>)}</select></label>
        <label>{t('catalog.pageSize')}<select value={pageSize} onChange={e => setPageSize(Number(e.target.value))}>{[25, 50, 100].map(n => <option key={n}>{n}</option>)}</select></label></div>
        <div className="catalog-check-options"><label><input type="checkbox" checked={grouped} onChange={e => setGrouped(e.target.checked)} />{t('catalog.groupVariants')}</label><label><input type="checkbox" checked={showPrice} onChange={e => setShowPrice(e.target.checked)} />{t('catalog.showPrices')}</label><label><input type="checkbox" checked={showAvailability} onChange={e => setShowAvailability(e.target.checked)} />{t('catalog.showAvailability')}</label></div></div>
        <div><h3>{t('catalog.importSettings')}</h3>{targetLoading ? <p>{t('common.loading')}</p> : !target ? <p>{targetError ? message(targetError) : t('catalog.chooseShop')}</p> : <><div className="catalog-fields">
          <label>{t('catalog.language')}<select value={options.language} onChange={e => { const l = target.languages.find(l => l.code === e.target.value)!; setOptions({ ...options, language: l.code, currency: l.currency }); }}>{target.languages.map(l => <option key={l.code} value={l.code}>{l.code.toUpperCase()} · {l.currency}</option>)}</select></label>
          <label>{t('catalog.pricelist')}<select value={options.pricelist} onChange={e => setOptions({ ...options, pricelist: e.target.value })}>{target.pricelists.map(p => <option key={p.name}>{p.name}</option>)}</select></label>
          <label>{t('catalog.category')}<CategoryTree categories={target.categories} value={options.category_code || ''} language={options.language} onChange={code => setOptions({ ...options, category_code: code || null })} /></label>
          <label>{t('catalog.pricing')}<select value={options.pricing} onChange={e => setOptions({ ...options, pricing: e.target.value as ImportOptions['pricing'] })}><option value="configured">{t('catalog.configuredPricing')}</option><option value="retail">{t('catalog.retailPricing')}</option></select></label></div>
          <div className="catalog-check-options">{(['include_images', 'include_description', 'include_parameters'] as const).map(key => <label key={key}><input type="checkbox" checked={options[key]} onChange={e => setOptions({ ...options, [key]: e.target.checked })} />{t(`catalog.include.${key}`)}</label>)}</div>
          <div className="catalog-check-options"><label><input type="checkbox" checked={fullShopCheck} onChange={e => { setFullShopCheck(e.target.checked); setPreview(null); }} />{t('catalog.fullShopCheck')}</label></div><p className="catalog-muted">{t('catalog.fullShopCheckHelp')}</p>
          <p className="catalog-muted">{t(target.prices_with_vat ? 'catalog.shopPricesGross' : 'catalog.shopPricesNet')}</p></>}</div>
      </div>}
      {targetError && !advanced && <div role="alert" className="catalog-notice">{message(targetError)}</div>}
      <div className="catalog-result-bar"><span aria-live="polite">{loading ? t('common.loading') : t('catalog.found', { count: data?.total_items || 0 })}{!!data?.total && grouped && data.total !== data.total_items ? ` · ${t('catalog.rowCount', { count: data.total })}` : ''}</span>
        <button disabled={busy || !data?.total_items} onClick={selectAll}>{t('catalog.selectAllResults')}</button><span className="catalog-muted">{data?.shop_checked_at ? t('catalog.listingChecked', { date: new Date(data.shop_checked_at).toLocaleString() }) : t('catalog.localListingHint')}</span></div>
      {data?.items.length ? <CatalogTable rows={data.items} selected={selected} onToggle={toggle} onDetail={setDetail} busy={busy} showPrice={showPrice} showAvailability={showAvailability} shopChecked={!!data.shop_checked_at} /> : !loading && <div className="catalog-empty"><Package size={36} /><h2>{t(data?.run_id ? 'catalog.noResults' : 'catalog.startDownload')}</h2><p>{t(data?.run_id ? 'catalog.noResultsHelp' : 'catalog.startDownloadHelp')}</p></div>}
      {!!data?.pages && <div className="catalog-pagination"><Button variant="secondary" size="sm" disabled={busy || page === 1} onClick={() => setPage(page - 1)}>{t('common.back')}</Button><span>{t('catalog.pageOf', { page, pages: data.pages })}</span><Button variant="secondary" size="sm" disabled={busy || page >= data.pages} onClick={() => setPage(page + 1)}>{t('common.next')}</Button></div>}
      <div className="catalog-selection-bar"><div><strong>{t('catalog.selected', { count: selected.size })}</strong><button disabled={!selected.size || busy} onClick={() => { setSelected(new Set()); setPreview(null); }}>{t('catalog.clearSelection')}</button></div>
        <div className="catalog-selection-target"><small>{shopInfo?.name || t('catalog.chooseShop')}</small><Button variant="secondary" disabled={busy || !selected.size || selected.size > 500 || activeImport} onClick={() => navigate('/ai-content', { state: { selection: { supplier, feed_key: feed, product_ids: [...selected], run_id: snapshot.current, shop, options } } })}>{t('ai.prepareWithAI')}</Button><Button icon={<Download size={16} />} loading={preparing} disabled={busy || !selected.size || selected.size > 20000 || !target || targetLoading || activeImport} onClick={() => prepare()}>{t('catalog.prepareImport')}</Button></div></div>
    </>}
    {result && <div className="catalog-job-bar"><span>{t(activeImport ? 'catalog.importRunning' : 'catalog.lastImport')}</span><Button variant="secondary" size="sm" onClick={() => setDialog(true)}>{t('catalog.showResult')}</Button></div>}
    {detail && <CatalogDetail key={detail.id} supplier={supplier} product={detail} shop={shop} selected={selected} onToggle={toggle} onClose={() => setDetail(null)} />}
    {dialog && (preview || result) && <CatalogImport key={result?.preview_id || preview?.preview_id} preview={preview} result={result} shopName={shopInfo?.name || shop} sending={sending || preparing} error={importError} onReprice={prepare} onExclude={(ids, overrides) => prepare(overrides, [...selected].filter(id => !ids.includes(id)))} onConfirm={() => confirm()} onRetry={() => confirm(true)} onClose={() => setDialog(false)} />}
  </div>;
}
