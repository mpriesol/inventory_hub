import React, { useEffect, useRef, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { accessRevision } from '../api/access';
import {
  changePreorderDate, createSupplierPreorder, getPreorderSuppliers, getSupplierPreorder, linkPreorderInvoice,
  listPreorderInvoices, listSupplierPreorders, PreorderCreate, PreorderInvoice, PreorderLine, SupplierPreorder,
} from '../api/supplierPreorders';
import { emptySkuIdentity, SkuIdentityInput } from '../api/skuPolicy';
import { SkuIdentityForm } from '../components/product/SkuIdentityForm';
import { formatNumber, todayDate } from '../utils/format';
import { invoiceDate } from './invoiceArchiveUi';
import './invoiceArchive.css';

type NewLine = {
  key: number; sku: string; name: string; quantity: string; expected: string; mode: 'existing' | 'new';
  identity: SkuIdentityInput; ean: string; verified: boolean;
};
const blankLine = (key: number): NewLine => ({
  key, sku: '', name: '', quantity: '', expected: '', mode: 'existing', identity: emptySkuIdentity(), ean: '', verified: false,
});
function useLabels() {
  const { t, i18n } = useTranslation();
  return { l: (key: string) => t(`supplierPreorders.${key}`), t,
    date: (value: string | null) => invoiceDate(value, i18n.language),
    number: (value: number) => formatNumber(value, i18n.language, 3),
    describe: (error: unknown) => {
      const code = (error as { code?: string })?.code || 'request_failed';
      return t(`supplierPreorders.errors.${code}`, { defaultValue: t(`skuPolicy.errors.${code}`, { defaultValue: t('supplierPreorders.errors.request_failed') }) });
    },
  };
}

function PreorderEntry({ close }: { close: () => void }) {
  const { l, t, describe } = useLabels();
  const navigate = useNavigate();
  const [suppliers, setSuppliers] = useState<{ id: number; name: string }[]>([]);
  const [supplier, setSupplier] = useState('');
  const [reference, setReference] = useState('');
  const [ordered, setOrdered] = useState(todayDate);
  const [approved, setApproved] = useState(false);
  const [lines, setLines] = useState([blankLine(0)]);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const nextKey = useRef(1);
  const identifier = useRef<string | null>(null);
  const submitting = useRef(false);
  useEffect(() => {
    const controller = new AbortController();
    getPreorderSuppliers(controller.signal).then(value => {
      if (!controller.signal.aborted) setSuppliers(value);
    }).catch(reason => { if (!controller.signal.aborted) setError(describe(reason)); });
    return () => controller.abort();
  }, []);
  const update = (key: number, value: Partial<NewLine>) =>
    setLines(now => now.map(line => line.key === key ? { ...line, ...value } : line));

  async function submit() {
    if (!approved || !supplier || submitting.current) return;
    submitting.current = true; setBusy(true); setError('');
    const prior = identifier.current;
    const requestId = prior ?? crypto.randomUUID();
    identifier.current = requestId;
    const started = accessRevision();
    try {
      let value: SupplierPreorder | null = null;
      if (prior) {
        try { value = await getSupplierPreorder(requestId); }
        catch (reason) { if ((reason as { code?: string })?.code !== 'preorder_not_found') throw reason; }
      }
      const payload: PreorderCreate = { request_id: requestId, supplier_id: Number(supplier), reference, ordered_on: ordered, approved,
        lines: lines.map(line => ({ ordered_quantity: line.quantity.trim().replace(',', '.'), expected_on: line.expected,
          ...(line.mode === 'existing' ? { sku: line.sku.trim() } : {
            new_card: { name: line.name.trim(), sku_identity: line.identity, eans: line.ean.trim() ? [line.ean.trim()] : [] },
          }) })) };
      if (!value) value = await createSupplierPreorder(payload);
      if (started === accessRevision()) navigate(`/supplier-preorders/${value.id}`);
    } catch (reason) {
      if (started === accessRevision()) {
        try {
          const existing = await getSupplierPreorder(requestId);
          if (started === accessRevision()) navigate(`/supplier-preorders/${existing.id}`);
        } catch { if (started === accessRevision()) setError(describe(reason)); }
      }
    } finally { submitting.current = false; setBusy(false); }
  }

  return <form className="panel" onSubmit={event => { event.preventDefault(); void submit(); }}>
    <h2>{l('add')}</h2>
    {error && <p role="alert" className="notice error">{error}</p>}
    <fieldset disabled={busy}>
      <div className="fields">
        <label>{l('supplier')}<select required value={supplier} onChange={event => setSupplier(event.target.value)}>
          <option value="">—</option>{suppliers.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
        </select></label>
        <label>{l('reference')}<input required maxLength={100} value={reference} onChange={event => setReference(event.target.value)} /></label>
        <label>{l('orderedOn')}<input type="date" required value={ordered} onChange={event => setOrdered(event.target.value)} /></label>
      </div>
      {lines.map((line, index) => <fieldset key={line.key} className="supplier-preorder-line">
        <legend>{t('supplierPreorders.line', { number: index + 1 })}</legend>
        <div className="fields">
          <label>{l('card')}<select value={line.mode} onChange={event => update(line.key, { mode: event.target.value as NewLine['mode'] })}>
            <option value="existing">{l('existingCard')}</option><option value="new">{l('newCard')}</option>
          </select></label>
          {line.mode === 'existing' ? <label>SKU<input required maxLength={100} value={line.sku} onChange={event => update(line.key, { sku: event.target.value })} /></label> :
            <><label>{l('name')}<input required maxLength={500} value={line.name} onChange={event => update(line.key, { name: event.target.value })} /></label>
              <label>EAN<input inputMode="numeric" maxLength={14} value={line.ean} onChange={event => update(line.key, { ean: event.target.value, verified: false })} /></label></>}
          <label>{l('orderedQuantity')}<input inputMode="decimal" required pattern="[0-9]+([.,][0-9]{1,3})?" value={line.quantity} onChange={event => update(line.key, { quantity: event.target.value })} /></label>
          <label>{l('expected')}<input type="date" required value={line.expected} onChange={event => update(line.key, { expected: event.target.value })} /></label>
        </div>
        {line.mode === 'new' && <SkuIdentityForm value={line.identity} simplified allowRegistryWrites={false}
          context={{ item_type: 'stock', barcodes: line.ean.trim() ? [line.ean.trim()] : [] }}
          onChange={identity => update(line.key, { identity, verified: false })}
          onPreview={preview => update(line.key, { verified: preview?.status === 'new' || preview?.status === 'existing' })} />}
        {lines.length > 1 && <button type="button" onClick={() => setLines(current => current.filter(item => item.key !== line.key))}>{l('removeLine')}</button>}
      </fieldset>)}
      <button type="button" disabled={lines.length >= 100} onClick={() => setLines(current => [...current, blankLine(nextKey.current++)])}>{l('addLine')}</button>
      <label className="check"><input type="checkbox" checked={approved} onChange={event => setApproved(event.target.checked)} />{l('approved')}</label>
      <footer><button type="button" onClick={close}>{t('common.cancel')}</button>
        <button className="primary" type="submit" disabled={!approved || !supplier || lines.some(line => line.mode === 'new' && !line.verified)}>{l('save')}</button></footer>
    </fieldset>
  </form>;
}

function OrderDetail({ order, setOrder }: { order: SupplierPreorder; setOrder: (value: SupplierPreorder) => void }) {
  const { l, t, date, number, describe } = useLabels();
  const [query, setQuery] = useState('');
  const [options, setOptions] = useState<PreorderInvoice[]>([]);
  const [invoice, setInvoice] = useState('');
  const [searched, setSearched] = useState(false);
  const [edit, setEdit] = useState<{ line: PreorderLine; value: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  async function operation(action: () => Promise<void>) {
    setBusy(true); setError('');
    try { await action(); } catch (reason) { setError(describe(reason)); } finally { setBusy(false); }
  }

  return <>
    {error && <p role="alert" className="notice error">{error}</p>}
    <section className="panel"><header><div><h2>{order.reference}</h2>
      <p>{order.supplier_name} · {date(order.ordered_on)} · {l(order.status)}</p></div></header>
      <div className="table-scroll"><table><thead><tr>{['card', 'orderedQuantity', 'received', 'remaining', 'expected', 'deliveries'].map(key =>
        <th key={key}>{l(key)}</th>)}</tr></thead>
        <tbody>{order.lines.map(line => <tr key={line.id}>
          <td><Link to={`/products/card/${line.product_id}`}>{line.name}</Link><span className="secondary">{line.sku} · {t(`productEditor.units.${line.stock_unit}`)}</span></td>
          <td>{number(line.ordered_quantity)}</td><td>{number(line.received_quantity)}</td><td>{number(line.remaining_quantity)}</td>
          <td>{edit?.line.id === line.id ? <form onSubmit={event => {
            event.preventDefault(); void operation(async () => {
              setOrder(await changePreorderDate(order.id, line, edit.value)); setEdit(null);
            });
          }}><input type="date" aria-label={l('expected')} required value={edit.value} onChange={event => setEdit({ line, value: event.target.value })} />
            <button disabled={busy}>{t('common.save')}</button><button type="button" disabled={busy} onClick={() => setEdit(null)}>{t('common.cancel')}</button></form> :
            <button type="button" disabled={busy} onClick={() => setEdit({ line, value: line.expected_on })}>{date(line.expected_on)} · {t('common.edit')}</button>}</td>
          <td>{line.deliveries.length ? <ul>{line.deliveries.map((delivery, index) => <li key={index}>
            <Link to={`/invoices/${delivery.invoice_id}`}>{delivery.invoice_number}</Link> · {date(delivery.received_at)} · {number(delivery.quantity)}
          </li>)}</ul> : '—'}</td>
        </tr>)}</tbody></table></div>
    </section>
    <section className="panel"><h2>{l('invoices')}</h2>
      <ul>{order.invoices.map(item => <li key={item.id}><Link to={`/invoices/${item.id}`}>{item.invoice_number || item.original_filename}</Link> · {date(item.invoice_date)}</li>)}</ul>
      <form onSubmit={event => { event.preventDefault(); void operation(async () => {
        setOptions(await listPreorderInvoices(order.id, query)); setInvoice(''); setSearched(true);
      }); }}><div className="actions"><label>{l('findInvoice')}<input value={query} onChange={event => setQuery(event.target.value)} /></label>
        <button disabled={busy}>{l('search')}</button></div></form>
      {searched && (options.length ? <div className="actions">
        <label>{l('invoices')}<select value={invoice} onChange={event => setInvoice(event.target.value)}>
          <option value="">—</option>{options.map(item => <option key={item.id} value={item.id}>{item.invoice_number || item.original_filename} · {date(item.invoice_date)}</option>)}
        </select></label>
        <button type="button" disabled={busy || !invoice} onClick={() => { void operation(async () => {
          setOrder(await linkPreorderInvoice(order.id, Number(invoice)));
          setOptions(current => current.filter(item => item.id !== Number(invoice))); setInvoice('');
        }); }}>{l('linkInvoice')}</button>
      </div> : <p>{l('noInvoices')}</p>)}
    </section>
  </>;
}

export function SupplierPreordersPage() {
  const { l, t, date, describe } = useLabels();
  const { preorderId } = useParams();
  const [items, setItems] = useState<SupplierPreorder[]>([]);
  const [order, setOrder] = useState<SupplierPreorder | null>(null);
  const [newOrder, setNewOrder] = useState(false);
  const [search, setSearch] = useState('');
  const [query, setQuery] = useState('');
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError(''); setOrder(null); setItems([]);
    const request = preorderId ? getSupplierPreorder(preorderId, controller.signal).then(value => {
      if (!controller.signal.aborted) setOrder(value);
    }) : listSupplierPreorders({ q: query, page }, controller.signal).then(value => {
      if (!controller.signal.aborted) { setItems(value.items); setTotal(value.total); }
    });
    request.catch(reason => { if (!controller.signal.aborted) setError(describe(reason)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [preorderId, query, page]);

  return <main className="invoice-archive">
    <header><div>{preorderId && <Link to="/supplier-preorders">{l('title')}</Link>}<h1>{preorderId ? l('detail') : l('title')}</h1></div>
      {!preorderId && !newOrder && <button className="primary" type="button" onClick={() => setNewOrder(true)}>{l('add')}</button>}</header>
    {error && <p role="alert" className="notice error">{error}</p>}
    {loading && <p role="status">{t('common.loading')}</p>}
    {preorderId ? order && <OrderDetail key={order.id} order={order} setOrder={setOrder} /> : newOrder ? <PreorderEntry close={() => setNewOrder(false)} /> :
      <section className="panel">
        <form onSubmit={event => { event.preventDefault(); setQuery(search.trim()); setPage(1); }} className="actions">
          <label>{l('searchLabel')}<input maxLength={100} value={search} onChange={event => setSearch(event.target.value)} /></label>
          <button>{l('search')}</button>
        </form>
        <div className="table-scroll"><table><thead><tr>{['reference', 'supplier', 'orderedOn', 'status'].map(key => <th key={key}>{l(key)}</th>)}</tr></thead>
          <tbody>{items.map(item => <tr key={item.id}><td><Link to={`/supplier-preorders/${item.id}`}>{item.reference}</Link></td>
            <td>{item.supplier_name}</td><td>{date(item.ordered_on)}</td><td>{l(item.status)}</td></tr>)}</tbody></table></div>
        {!loading && !items.length && <p>{l('empty')}</p>}
        {total > 20 && <div className="actions">
          <button disabled={page <= 1 || loading} type="button" onClick={() => setPage(value => value - 1)}>{t('common.back')}</button>
          <button disabled={page * 20 >= total || loading} type="button" onClick={() => setPage(value => value + 1)}>{t('common.next')}</button>
        </div>}
      </section>}
  </main>;
}
