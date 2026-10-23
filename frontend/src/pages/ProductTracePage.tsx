import React, { useEffect, useState, useSyncExternalStore } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Search } from 'lucide-react';
import { accessRevision, hubUnlocked, subscribeAccess } from '../api/access';
import { getProductTrace, type ProductTrace } from '../api/productTrace';
import { formatNumber } from '../utils/format';
import { Button } from '../components/ui/Button.new';
import { ProductThumb } from '../components/product/ProductDisplay';
import { catalogImageUrl } from '../api/catalog';
import './ProductTracePage.css';

const positivePage = (value: string | null) => value && /^\d+$/.test(value) && Number.isSafeInteger(Number(value)) && Number(value) > 0 ? Number(value) : 1;

export function ProductTracePage() {
  const { t, i18n } = useTranslation();
  const c = (key: string, values: Record<string, unknown> = {}) => t(`productTrace.${key}`, values);
  const [params, setParams] = useSearchParams();
  const revision = useSyncExternalStore(subscribeAccess, accessRevision, accessRevision);
  const code = params.get('code') || '';
  const productIdText = params.get('product_id');
  const productId = productIdText && /^\d+$/.test(productIdText) && Number.isSafeInteger(Number(productIdText)) && Number(productIdText) > 0 ? Number(productIdText) : undefined;
  const invoicePage = positivePage(params.get('invoice_page')), movementPage = positivePage(params.get('movement_page'));
  const [input, setInput] = useState(code);
  const [data, setData] = useState<ProductTrace | null>(null), [busy, setBusy] = useState(false), [error, setError] = useState('');
  const [retry, setRetry] = useState(0);
  const unlocked = hubUnlocked();
  useEffect(() => { setInput(code); }, [code]);
  useEffect(() => {
    const controller = new AbortController(), started = accessRevision();
    setData(null); setError(''); setBusy(false);
    if (!unlocked || (!code && !productId)) return () => controller.abort();
    setBusy(true);
    getProductTrace({ code, product_id: productId, invoice_page: invoicePage, movement_page: movementPage }, controller.signal)
      .then(value => { if (!controller.signal.aborted && started === accessRevision()) setData(value); })
      .catch(value => { if (!controller.signal.aborted && started === accessRevision()) setError(value?.code || 'request_failed'); })
      .finally(() => { if (!controller.signal.aborted && started === accessRevision()) setBusy(false); });
    return () => controller.abort();
  }, [code, productId, invoicePage, movementPage, revision, unlocked, retry]);
  const number = (value: unknown, digits = 3) => value === null || value === undefined || value === '' ? c('unknown') : formatNumber(value, i18n.language, digits);
  const unit = (value?: string | null) => value ? t(`productEditor.units.${value === 'ks' ? 'pcs' : value}`, { defaultValue: value }) : c('unitUnknown');
  const date = (value: string | null, withTime = false) => {
    if (!value) return c('unknown');
    const parsed = new Date(value); if (!Number.isFinite(parsed.getTime())) return c('unknown');
    return withTime ? parsed.toLocaleString(i18n.language) : parsed.toLocaleDateString(i18n.language);
  };
  const money = (value: string | null, currency: string | null) => value === null ? c('unknown') : `${formatNumber(value, i18n.language, 4, 2)} ${currency || c('currencyUnknown')}`;
  const paginate = (kind: 'invoice_page' | 'movement_page', page: number) => {
    const next = new URLSearchParams(params); next.set(kind, String(page)); setParams(next);
  };
  const pager = (kind: 'invoice_page' | 'movement_page', page: { page: number; page_size: number; total: number }) => <div className="trace-pagination">
    <span>{c('page', { page: page.page, pages: Math.max(1, Math.ceil(page.total / page.page_size)), total: page.total })}</span>
    <Button data-testid={`trace-${kind}-previous`} variant="ghost" size="sm" disabled={busy || page.page <= 1} onClick={() => paginate(kind, page.page - 1)}>{c('previous')}</Button>
    <Button data-testid={`trace-${kind}-next`} variant="ghost" size="sm" disabled={busy || page.page * page.page_size >= page.total} onClick={() => paginate(kind, page.page + 1)}>{c('next')}</Button>
  </div>;
  const product = data?.product;
  const sales = data?.sales;
  const documentSales = data?.sales_documents;
  const monthly = sales?.monthly || [];
  const chartMaximum = Math.max(1, ...monthly.flatMap(month => [Number(month.sold) || 0, Number(month.returned) || 0]));
  const chartHeight = (value: string | null) => `${Math.max(0, Math.min(100, Number(value) / chartMaximum * 100 || 0))}%`;
  return <main className="product-trace">
    <header><h1>{c('title')}</h1><p>{c('subtitle')}</p></header>
    {!unlocked ? <p><Link to="/login">{t('accounts.login')}</Link></p> : <>
      <form className="trace-search" onSubmit={event => { event.preventDefault(); const next = input.trim(); if (!next) return; if (next === code && !productId && invoicePage === 1 && movementPage === 1) setRetry(value => value + 1); else setParams({ code: next }); }}>
        <label htmlFor="trace-code">{c('searchLabel')}<input id="trace-code" data-testid="trace-code" value={input} maxLength={100} autoFocus autoComplete="off" spellCheck={false} placeholder={c('searchPlaceholder')} onChange={event => setInput(event.target.value)} /></label>
        <Button data-testid="trace-search" type="submit" disabled={!input.trim()} icon={<Search size={16} />}>{c('search')}</Button>
      </form>
      {busy && <p role="status">{c('loading')}</p>}
      {error && <div className="trace-notice is-error" role="alert">{t(`productTrace.errors.${error}`, { defaultValue: c('errors.request_failed') })}<Button variant="secondary" size="sm" onClick={() => setRetry(value => value + 1)}>{c('retry')}</Button></div>}
      {!code && !productId && <p className="trace-empty">{c('initial')}</p>}
      {data && <>
        {!!data.warnings.filter(warning => warning !== 'sales_hub_ledger_only').length && <div className="trace-notice" role="status">{data.warnings.filter(warning => warning !== 'sales_hub_ledger_only').map(warning => <p key={warning}>{t(`productTrace.warnings.${warning}`, { defaultValue: c('warningFallback') })}</p>)}</div>}
        {!product && data.matches.length > 0 && <section className="trace-panel" aria-labelledby="trace-matches"><h2 id="trace-matches">{c('chooseProduct')}</h2><div className="trace-matches">{data.matches.map(match => <button data-testid={`trace-match-${match.id}`} key={match.id} type="button" onClick={() => setParams({ ...(code ? { code } : {}), product_id: String(match.id) })}><strong>{match.name}</strong><code>{match.sku}</code><span>{t(`productEditor.itemTypes.${match.item_type}`)} · {unit(match.stock_unit)}</span></button>)}</div>{data.matches_truncated && <p>{c('matchesTruncated')}</p>}</section>}
        {product && <>
          <section className="trace-panel trace-card" aria-labelledby="trace-product-name">
            <ProductThumb key={product.image_url || product.id} url={catalogImageUrl(product.image_url)} name={product.name} size={140} />
            <div className="trace-card-description"><code>{product.sku}</code><h2 id="trace-product-name">{product.name}</h2><p>{product.brand || c('brandUnknown')} · {t(`productEditor.itemTypes.${product.item_type}`)} · {unit(product.stock_unit)}</p><p>EAN: {product.eans.length ? product.eans.join(' · ') : c('unknown')}</p></div>
            <div className="trace-card-links">{(product.shops || []).map(shop => <div className="trace-shop-links" key={shop.code}><strong>{shop.name}</strong>{shop.url ? <a href={shop.url} target="_blank" rel="noopener noreferrer">{c('storefront')}</a> : <span>{c('linkMissing')}</span>}{shop.admin_url && <a href={shop.admin_url} target="_blank" rel="noopener noreferrer">{c('administration')}</a>}</div>)}<Link to={product.card_url}>{c('openCard')}</Link><Link to={`/stock/movements?${new URLSearchParams({ sku: product.sku })}`}>{c('allMovements')}</Link></div>
          </section>
          <div className="trace-overview">
            <section className="trace-panel"><h2>{c('stock')}</h2>{product.item_type === 'service' ? <p>{t('productEditor.serviceHelp')}</p> : !data.stocks.length ? <p className="trace-empty">{c('noStock')}</p> : <div className="trace-scroll" tabIndex={0} role="region" aria-label={c('stock')}><table className="trace-table"><thead><tr><th scope="col">{c('warehouse')}</th>{['onHand', 'reserved', 'quarantined', 'available'].map(key => <th scope="col" className="trace-number" key={key}>{c(key)}</th>)}</tr></thead><tbody>{data.stocks.map(stock => <tr key={stock.warehouse_code}><th scope="row">{stock.warehouse_name}{!stock.confirmed && <small>{c('notConfirmed')}</small>}</th>{[stock.on_hand, stock.reserved, stock.quarantined, stock.available].map((quantity, index) => <td className="trace-number" key={index}>{stock.confirmed ? `${number(quantity)} ${unit(product.stock_unit)}` : c('unknown')}</td>)}</tr>)}</tbody></table></div>}</section>
            <section className="trace-panel"><h2>{c('prices')}</h2>{!data.prices.length ? <p className="trace-empty">{c('noPrices')}</p> : <ul className="trace-prices">{data.prices.map((price, index) => <li key={`${price.source}:${index}`}><div><strong>{price.label}</strong><small>{c(price.vat_included === true ? 'vatIncluded' : price.vat_included === false ? 'vatExcluded' : 'vatUnknown')}{price.source_invoice_unit && ` · ${unit(price.source_invoice_unit)}`}</small>{price.observed_at && <small>{c('observedAt', { date: date(price.observed_at) })}</small>}</div><span data-testid={`trace-price-${index}`}>{money(price.price, price.currency)}</span></li>)}</ul>}</section>
          </div>
          {sales && <section className="trace-panel"><h2>{c('sales')}</h2><p className="trace-muted">{c('salesScope')}{sales.since && ` ${c('since', { date: date(sales.since) })}`}</p>
            {product.item_type === 'service' ? <p>{c('serviceSales')}</p> : <><div className="trace-metrics">{[['sold', sales.sold_quantity], ['returned', sales.returned_quantity], ['net', sales.net_quantity]].map(([key, value]) => <div key={key || ''}><span>{c(key || '')}</span><strong>{number(value)} {unit(sales.unit)}</strong></div>)}</div>
            {monthly.length > 0 && <figure className="trace-chart"><div className="trace-chart-scroll"><div className="trace-chart-bars" aria-hidden="true">{monthly.map(month => <div className="trace-chart-month" key={month.month} title={`${month.month}: ${c('sold')} ${number(month.sold)}, ${c('returned')} ${number(month.returned)}`}><div className="trace-chart-pair"><span style={{ height: chartHeight(month.sold) }} /><span className="is-return" style={{ height: chartHeight(month.returned) }} /></div><small>{month.month}</small></div>)}</div></div><figcaption><span>{c('sold')}</span><span className="is-return">{c('returned')}</span></figcaption><details><summary>{c('monthlyData')}</summary><div className="trace-scroll"><table className="trace-table"><thead><tr><th scope="col">{c('month')}</th><th scope="col">{c('sold')}</th><th scope="col">{c('returned')}</th><th scope="col">{c('net')}</th></tr></thead><tbody>{monthly.map(month => <tr key={month.month}><th scope="row">{month.month}</th><td>{number(month.sold)}</td><td>{number(month.returned)}</td><td>{number(month.net)}</td></tr>)}</tbody></table></div></details></figure>}</>}
          </section>}
          {documentSales && <section className="trace-panel" data-testid="trace-document-sales">
            <h2>{c('salesDocuments')}</h2><p className="trace-muted">{c('salesDocumentsScope')}</p>
            {!documentSales.by_unit.length ? <p className="trace-empty">{c('noSalesDocuments')}</p> : <>
              <div className="trace-scroll" tabIndex={0} role="region" aria-label={c('salesDocuments')}><table className="trace-table"><thead><tr>{['unit', 'sold', 'returned', 'net'].map(key => <th scope="col" key={key}>{c(key)}</th>)}</tr></thead><tbody>{documentSales.by_unit.map(row => <tr key={row.unit || 'unknown'}><th scope="row">{unit(row.unit)}</th><td>{number(row.sold_quantity)}</td><td>{number(row.returned_quantity)}</td><td>{number(row.net_quantity)}</td></tr>)}</tbody></table></div>
              {!!documentSales.monthly.length && <details className="trace-monthly-documents"><summary>{c('monthlyData')}</summary><div className="trace-scroll" tabIndex={0} role="region" aria-label={c('monthlyData')}><table className="trace-table"><thead><tr>{['month', 'unit', 'sold', 'returned', 'net'].map(key => <th scope="col" key={key}>{c(key)}</th>)}</tr></thead><tbody>{documentSales.monthly.map(row => <tr key={`${row.month}:${row.unit || 'unknown'}`}><th scope="row">{row.month}</th><td>{unit(row.unit)}</td><td>{number(row.sold)}</td><td>{number(row.returned)}</td><td>{number(row.net)}</td></tr>)}</tbody></table></div></details>}
            </>}
          </section>}
        </>}
        <section className="trace-panel" data-testid="trace-feeds"><h2>{c('feeds')}</h2><p className="trace-muted">{c('feedScope')}</p>
          {!data.feed_matches?.items.length ? <p className="trace-empty">{c('noFeedMatches')}</p> : <div className="trace-feed-list">{data.feed_matches.items.map(feed => <article key={feed.id} data-testid={`trace-feed-${feed.id}`}>
            <ProductThumb url={catalogImageUrl(feed.image_url)} name={feed.name} size={80} />
            <div className="trace-feed-description"><strong>{feed.name}</strong><p>{feed.supplier_name} · <code>{feed.supplier_sku}</code>{feed.manufacturer_sku && ` · ${c('manufacturerCode')}: ${feed.manufacturer_sku}`}</p>
              <p>EAN: {feed.eans.join(' · ') || c('unknown')}</p><small>{c('observedAt', { date: date(feed.last_seen_at, true) })}{!feed.is_active && ` · ${c('feedInactive')}`}</small>
              <p><Link to={feed.catalog_url}>{c('openCatalog')}</Link>{feed.hub_product_id ? <> · <Link to={`/product-trace?product_id=${feed.hub_product_id}`}>{feed.hub_sku}</Link></> : <> · {c('feedOnly')}</>}</p>
            </div><div className="trace-feed-values"><p>{c('purchaseNet')}: <strong>{money(feed.purchase_price, feed.purchase_currency)}</strong></p><p>{c('retailGross')}: <strong>{money(feed.recommended_price, feed.catalog_currency)}</strong></p><p>{c('supplierStock')}: {number(feed.stock_qty)}{feed.supplier_availability_text && ` · ${feed.supplier_availability_text}`}</p></div>
          </article>)}</div>}
          {data.feed_matches?.truncated && <p>{c('feedTruncated')}</p>}
        </section>
        <section className="trace-panel"><h2>{c('invoices')}</h2>{!data.invoices.items.length ? <p className="trace-empty">{c('noInvoices')}</p> : <><div className="trace-scroll" tabIndex={0} role="region" aria-label={c('invoices')}><table className="trace-table trace-documents"><thead><tr>{['invoice', 'direction', 'date', 'party', 'quantity', 'unitPrice', 'lineTotal'].map(key => <th scope="col" key={key}>{c(key)}</th>)}</tr></thead><tbody>{data.invoices.items.map(invoice => <tr key={invoice.line_id}><th scope="row"><Link to={`/invoices/${encodeURIComponent(String(invoice.invoice_id))}`}>{invoice.invoice_number || `#${invoice.invoice_id}`}</Link>{invoice.product_name && <small>{invoice.product_name}</small>}<small>{c('matchedBy')}: {t(`productTrace.matchTypes.${invoice.matched_by}`, { defaultValue: invoice.matched_by })}</small></th><td>{c(invoice.direction === 'sale' ? 'outbound' : 'inbound')}</td><td>{date(invoice.invoice_date)}{invoice.due_date && <small>{c('due', { date: date(invoice.due_date) })}</small>}</td><td>{invoice.supplier_name || invoice.shop_code || c('unknown')}</td><td className="trace-number">{number(invoice.quantity)} {unit(invoice.unit)}</td><td className="trace-number">{money(invoice.unit_price, invoice.currency)}</td><td className="trace-number">{money(invoice.total_price, invoice.currency)}</td></tr>)}</tbody></table></div>{pager('invoice_page', data.invoices)}</>}</section>
        {product && <section className="trace-panel"><h2>{c('movements')}</h2>{!data.movements.items.length ? <p className="trace-empty">{c('noMovements')}</p> : <><div className="trace-scroll" tabIndex={0} role="region" aria-label={c('movements')}><table className="trace-table trace-documents"><thead><tr>{['date', 'movementType', 'quantity', 'balanceAfter', 'warehouse', 'reference'].map(key => <th scope="col" key={key}>{c(key)}</th>)}</tr></thead><tbody>{data.movements.items.map(movement => <tr key={movement.id}><td>{date(movement.created_at, true)}</td><th scope="row">{t(`stockHistory.types.${movement.movement_type}`, { defaultValue: movement.movement_type })}</th><td className="trace-number">{number(movement.quantity)} {unit(product.stock_unit)}</td><td className="trace-number">{number(movement.balance_after)} {unit(product.stock_unit)}</td><td>{movement.warehouse_name}</td><td>{movement.document || movement.reference_label || movement.reference_id || '—'}{movement.tracking_scope === 'historical' && <small>{c('historical')}</small>}</td></tr>)}</tbody></table></div>{pager('movement_page', data.movements)}</>}</section>}
      </>}
    </>}
  </main>;
}
