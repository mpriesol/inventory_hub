import React, { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { Link, useBlocker, useParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { ArrowLeft, Download, Edit, RefreshCw } from 'lucide-react';
import { accessRevision, hubUser, subscribeAccess } from '../api/access';
import {
  ArchivedInvoiceDetail, InvoiceMetadataChange, InvoicePaymentRequest, InvoicePaymentResult, InvoiceReversalRequest,
  addInvoicePayment, downloadInvoiceArchive, getArchivedInvoice,
  getInvoicePaymentOperation, reverseInvoicePayment, updateArchivedInvoice,
} from '../api/invoiceArchive';
import { invoiceDate, invoiceError, invoiceMoney, InvoicePaymentBadge } from './invoiceArchiveUi';
import './invoiceArchive.css';

type PendingPayment = { invoice_id: number; actor: string; payment_id?: number; body: InvoicePaymentRequest | InvoiceReversalRequest };
const storageKey = (id: number) => `hub.invoice-payment.${id}`;
const currentActor = () => String(hubUser()?.id ?? hubUser()?.username ?? 'operator-token');
function readPending(id: number): PendingPayment | null {
  try { const value = JSON.parse(sessionStorage.getItem(storageKey(id)) || 'null');
    return value?.invoice_id === id && value.actor === currentActor() && /^[0-9a-f-]{36}$/i.test(value.body?.request_id || '') ? value : null;
  } catch { return null; }
}
function writePending(id: number, value: PendingPayment | null) {
  try { if (value) sessionStorage.setItem(storageKey(id), JSON.stringify(value)); else sessionStorage.removeItem(storageKey(id)); } catch { /* The mounted page still retains the exact request. */ }
}
const editValues = (invoice: ArchivedInvoiceDetail) => ({
  invoice_number: invoice.invoice_number || '', invoice_date: invoice.invoice_date || '', due_date: invoice.due_date || '',
  total_amount: invoice.total_amount === null ? '' : String(invoice.total_amount), vat_rate: invoice.vat_rate === null ? '' : String(invoice.vat_rate),
  vat_included: invoice.vat_included, notes: invoice.notes || '', currency: invoice.currency || '',
});
const decimalInput = (value: string) => value.trim().replace(',', '.');
const positiveMoney = (value: string) => /^\d+(\.\d{1,2})?$/.test(decimalInput(value)) && /[1-9]/.test(value);
const signedMoney = (value: string) => /^-?\d+(\.\d{1,2})?$/.test(decimalInput(value));

export function InvoiceDetailPage() {
  const { t, i18n } = useTranslation();
  const { invoiceId } = useParams<{ invoiceId: string }>();
  const id = Number(invoiceId);
  const access = useSyncExternalStore(subscribeAccess, accessRevision);
  const [invoice, setInvoice] = useState<ArchivedInvoiceDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [refresh, setRefresh] = useState(0);
  const [editing, setEditing] = useState(false);
  const [edit, setEdit] = useState<ReturnType<typeof editValues> | null>(null);
  const [pending, setPending] = useState<PendingPayment | null>(() => readPending(id));
  const request = useRef(pending);
  const [paymentOpen, setPaymentOpen] = useState(false);
  const [payment, setPayment] = useState({ kind: 'payment' as 'payment' | 'refund', amount: '', paid_at: '', note: '', opening_paid_amount: '' });
  const [reverseId, setReverseId] = useState<number | null>(null);
  const [reverseNote, setReverseNote] = useState('');
  const [conflict, setConflict] = useState(false);
  const working = useRef(false);
  const currentId = useRef(id); currentId.current = id;
  const dirty = !!(editing && invoice && edit && JSON.stringify(editValues(invoice)) !== JSON.stringify(edit));
  const blocker = useBlocker(({ currentLocation, nextLocation }) => dirty && currentLocation.pathname !== nextLocation.pathname);
  useEffect(() => {
    if (!dirty) return;
    const beforeUnload = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ''; };
    window.addEventListener('beforeunload', beforeUnload); return () => window.removeEventListener('beforeunload', beforeUnload);
  }, [dirty]);
  useEffect(() => {
    const controller = new AbortController(); setLoading(true); setError(''); setInvoice(null); setEditing(false); setConflict(false);
    setPaymentOpen(false); setReverseId(null); setPending(readPending(id)); request.current = readPending(id);
    if (!Number.isSafeInteger(id) || id < 1) { setError(t('invoiceArchive.notFound')); setLoading(false); return; }
    getArchivedInvoice(id, controller.signal).then(value => {
      if (!controller.signal.aborted) { setInvoice(value); setEdit(editValues(value)); }
    }).catch(reason => { if (!controller.signal.aborted) setError(invoiceError(reason, t('invoiceArchive.loadFailed'))); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [id, access, refresh, t]);
  const retain = (value: PendingPayment | null) => { request.current = value; setPending(value); writePending(id, value); };
  const fresh = async () => {
    const value = await getArchivedInvoice(id);
    if (currentId.current === id) { setInvoice(value); setEdit(editValues(value)); }
    return value;
  };
  const finishPayment = (value: InvoicePaymentResult) => {
    retain(null); setPaymentOpen(false); setReverseId(null); setReverseNote('');
    setPayment({ kind: 'payment', amount: '', paid_at: '', note: '', opening_paid_amount: '' });
    setInvoice(old => old ? { ...old, finance: value.finance, payment_revision: value.payment_revision, payment_status: value.finance.status, is_overdue: value.finance.is_overdue } : old);
    setError(''); setMessage(t('invoiceArchive.paymentSaved'));
  };
  const recover = async () => {
    const operation = request.current; if (!operation || working.current) return;
    const startedAccess = accessRevision(); working.current = true; setBusy(true); setError('');
    try { const value = await getInvoicePaymentOperation(id, operation.body.request_id); if (startedAccess === accessRevision() && currentId.current === id) finishPayment(value); }
    catch (reason) { if (startedAccess === accessRevision() && currentId.current === id) setError(invoiceError(reason, t('invoiceArchive.paymentRecoveryPending'))); }
    finally { working.current = false; setBusy(false); }
  };
  const submitPayment = async (operation: PendingPayment) => {
    if (working.current) return;
    const startedAccess = accessRevision(); working.current = true; setBusy(true); setError(''); setMessage(''); retain(operation);
    try {
      const value = operation.payment_id === undefined ? await addInvoicePayment(id, operation.body as InvoicePaymentRequest) : await reverseInvoicePayment(id, operation.payment_id, operation.body);
      if (startedAccess === accessRevision() && currentId.current === id) finishPayment(value);
    } catch (reason) {
      if (startedAccess !== accessRevision() || currentId.current !== id) return;
      const failure = reason as { knownUnapplied?: boolean; status?: number };
      if (failure.knownUnapplied || [400, 401, 403, 422].includes(failure.status || 0)) {
        retain(null); setConflict(failure.status === 409); setError(invoiceError(reason, t(failure.status === 409 ? 'invoiceArchive.conflict' : 'invoiceArchive.paymentFailed')));
      } else {
        try { const result = await getInvoicePaymentOperation(id, operation.body.request_id); if (startedAccess === accessRevision() && currentId.current === id) finishPayment(result); }
        catch { if (startedAccess === accessRevision() && currentId.current === id) setError(t('invoiceArchive.paymentUncertain')); }
      }
    } finally { working.current = false; setBusy(false); }
  };
  const saveMetadata = async (event: React.FormEvent) => {
    event.preventDefault(); if (!invoice || !edit || working.current || pending) return;
    working.current = true; setBusy(true); setError(''); setMessage('');
    const body: InvoiceMetadataChange = { expected_revision: invoice.finance?.payment_revision ?? invoice.payment_revision, due_date: edit.due_date || null, notes: edit.notes || null };
    if (!['supplier', 'shop'].includes(invoice.source_kind || '')) {
      body.invoice_number = edit.invoice_number || null; body.invoice_date = edit.invoice_date || null;
      if (!invoice.finance?.payments.length) { body.total_amount = decimalInput(edit.total_amount) || null; body.vat_rate = decimalInput(edit.vat_rate) || null; body.vat_included = edit.vat_included; body.currency = edit.currency || null; }
    }
    try { await updateArchivedInvoice(id, body); setEditing(false); await fresh(); setMessage(t('invoiceArchive.metadataSaved')); }
    catch (reason) {
      setConflict((reason as { status?: number }).status === 409);
      setError(invoiceError(reason, t((reason as { status?: number }).status === 409 ? 'invoiceArchive.conflict' : 'invoiceArchive.metadataFailed')));
    } finally { working.current = false; setBusy(false); }
  };
  const download = async () => {
    if (working.current) return; working.current = true; setBusy(true); setError('');
    try { await downloadInvoiceArchive(id); } catch (reason) { setError(invoiceError(reason, t('invoiceArchive.downloadFailed'))); }
    finally { working.current = false; setBusy(false); }
  };
  const locked = busy || !!pending || conflict;
  const money = (value: string | number | null | undefined) => invoiceMoney(value, invoice?.currency, i18n.language);
  const finance = invoice?.finance;
  const sourceReadOnly = ['supplier', 'shop'].includes(invoice?.source_kind || '');
  const amountLocked = sourceReadOnly || !!finance?.payments.length;
  const paymentValid = positiveMoney(payment.amount) && !!payment.paid_at && (!finance?.opening_required || signedMoney(payment.opening_paid_amount));

  return <div className="invoice-archive">
    <header><div><Link to="/invoices"><ArrowLeft size={15} style={{ display: 'inline', marginRight: '.35rem' }} />{t('invoiceArchive.back')}</Link>
      <h1 style={{ marginTop: '.65rem' }}>{invoice?.invoice_number || invoice?.original_filename || t('invoiceArchive.detailTitle')}</h1>
      {invoice && <p className="muted">{invoice.supplier_name || invoice.shop_name || invoice.shop_code || '—'} · {invoiceDate(invoice.invoice_date, i18n.language)}</p>}
    </div><div className="actions"><button data-testid="invoice-detail-refresh" disabled={busy || dirty} onClick={() => setRefresh(value => value + 1)}><RefreshCw size={16} />{t('common.refresh')}</button>
      {invoice && <button disabled={busy} data-testid="invoice-original-download" onClick={download}><Download size={16} />{t('invoiceArchive.downloadOriginal')}</button>}</div></header>
    {blocker.state === 'blocked' && <div className="notice warning" role="alert"><p>{t('invoiceArchive.unsaved')}</p><div className="actions"><button onClick={() => blocker.reset()}>{t('invoiceArchive.keepEditing')}</button><button onClick={() => blocker.proceed()}>{t('invoiceArchive.discard')}</button></div></div>}
    {error && <div className="notice error" role="alert" data-testid="invoice-detail-error">{error}</div>}
    {message && <div className="notice success" role="status">{message}</div>}
    {pending && <section className="notice warning" data-testid="invoice-payment-pending"><p>{t('invoiceArchive.paymentUncertain')}</p><p className="small">{t('invoiceArchive.requestReference')}: {pending.body.request_id}</p><div className="actions">
      <button disabled={busy} data-testid="invoice-payment-recover" onClick={recover}>{t('invoiceArchive.recoverPayment')}</button><button disabled={busy} data-testid="invoice-payment-retry" onClick={() => submitPayment(pending)}>{t('invoiceArchive.retrySamePayment')}</button>
    </div></section>}
    {conflict && <div className="notice warning"><p>{t('invoiceArchive.conflict')}</p><button data-testid="invoice-conflict-reload" disabled={busy || !!pending} onClick={() => { setEditing(false); setRefresh(value => value + 1); }}>{t('invoiceArchive.loadLatest')}</button></div>}
    {loading ? <div className="empty" role="status">{t('common.loading')}</div> : invoice && edit && <>
      <div className="split"><section className="panel"><header><h2>{t('invoiceArchive.invoiceInfo')}</h2>{!editing && <button disabled={locked} data-testid="invoice-edit-open" onClick={() => { setEdit(editValues(invoice)); setEditing(true); }}><Edit size={15} />{t('common.edit')}</button>}</header>
        {editing ? <form onSubmit={saveMetadata}><fieldset disabled={locked}><div className="fields">
          {(['invoice_number', 'invoice_date', 'due_date'] as const).map(key => <label key={key}>{t(`invoiceArchive.${key}`)}<input data-testid={`invoice-edit-${key}`} disabled={sourceReadOnly && key !== 'due_date'} type={key.endsWith('date') ? 'date' : 'text'} value={edit[key]} onChange={event => setEdit({ ...edit, [key]: event.target.value })} /></label>)}
          <label>{t('invoiceArchive.currency')}<input data-testid="invoice-edit-currency" disabled={amountLocked} pattern="[A-Z]{3}" maxLength={3} value={edit.currency} onChange={event => setEdit({ ...edit, currency: event.target.value.toUpperCase() })} /></label>
          <label>{t('invoiceArchive.sourceAmount')}<input data-testid="invoice-edit-total" inputMode="decimal" pattern="-?[0-9]+([.,][0-9]{1,2})?" disabled={amountLocked} value={edit.total_amount} onChange={event => setEdit({ ...edit, total_amount: event.target.value })} /></label>
          <label>{t('invoiceArchive.vatRate')}<input inputMode="decimal" disabled={amountLocked} value={edit.vat_rate} onChange={event => setEdit({ ...edit, vat_rate: event.target.value })} /></label>
          <label className="check"><input type="checkbox" disabled={amountLocked} checked={edit.vat_included} onChange={event => setEdit({ ...edit, vat_included: event.target.checked })} />{t('invoiceArchive.vatIncluded')}</label>
          <label className="wide">{t('invoiceArchive.notes')}<textarea rows={3} data-testid="invoice-edit-notes" value={edit.notes} onChange={event => setEdit({ ...edit, notes: event.target.value })} /></label>
        </div>{amountLocked && <p className="muted small">{t('invoiceArchive.amountLocked')}</p>}<footer><button type="button" onClick={() => setEditing(false)}>{t('common.cancel')}</button><button className="primary" data-testid="invoice-edit-save" disabled={!dirty}>{t('common.save')}</button></footer></fieldset></form>
          : <dl><dt>{t('invoiceArchive.invoice_number')}</dt><dd>{invoice.invoice_number || '—'}</dd><dt>{t('invoiceArchive.direction')}</dt><dd>{t(`invoiceArchive.directionLabels.${invoice.direction || 'purchase'}`)}{invoice.document_type && invoice.document_type !== 'invoice' && ` · ${t(`invoiceArchive.documentType.${invoice.document_type}`, { defaultValue: invoice.document_type })}`}</dd>
            <dt>{t('invoiceArchive.invoice_date')}</dt><dd>{invoiceDate(invoice.invoice_date, i18n.language)}</dd><dt>{t('invoiceArchive.due_date')}</dt><dd>{invoiceDate(invoice.due_date, i18n.language)}</dd>
            <dt>{t('invoiceArchive.currency')}</dt><dd>{invoice.currency || '—'}</dd><dt>{t('invoiceArchive.netAmount')}</dt><dd>{money(invoice.total_without_vat)}</dd><dt>{t('invoiceArchive.vatAmount')}</dt><dd>{money(invoice.vat_amount)}</dd>
            <dt>{t('invoiceArchive.total_amount')}</dt><dd><strong>{money(finance ? finance.gross_amount : invoice.total_with_vat ?? invoice.total_amount)}</strong></dd><dt>{t('invoiceArchive.notes')}</dt><dd style={{ whiteSpace: 'pre-wrap' }}>{invoice.notes || '—'}</dd></dl>}
        <p className="muted small" style={{ marginTop: '1rem' }}>{invoice.original_filename} · {invoice.file_type?.toUpperCase()} · {t('invoiceArchive.created_at')}: {invoiceDate(invoice.created_at, i18n.language)}</p>
      </section><div className="stack"><section className="panel"><h2>{t('invoiceArchive.paymentStatus')}</h2><InvoicePaymentBadge invoice={invoice} /><dl style={{ marginTop: '1rem' }}><dt>{t('invoiceArchive.paidAmount')}</dt><dd>{money(finance?.paid_amount)}</dd><dt>{t('invoiceArchive.outstanding')}</dt><dd><strong>{money(finance?.outstanding_amount)}</strong></dd></dl>
        <p className="muted small">{t('invoiceArchive.manualPaymentsHelp')}</p>
        <button style={{ marginTop: '.7rem' }} className="primary" disabled={locked || editing || !finance} data-testid="invoice-payment-open" onClick={() => setPaymentOpen(value => !value)}>{t('invoiceArchive.addPayment')}</button>
      </section>{invoice.direction !== 'sale' && <section className="panel"><h2>{t('invoiceArchive.receiving')}</h2><p>{t(`invoiceArchive.receivingStatus.${invoice.receiving_status}`, { defaultValue: invoice.receiving_status })}</p>
        {!!invoice.receiving_session_id && invoice.receiving_invoice_number && invoice.supplier_code && <Link data-testid="invoice-receiving-link" to={`/receiving/${encodeURIComponent(invoice.receiving_invoice_number)}?supplier=${encodeURIComponent(invoice.supplier_code)}&session=${encodeURIComponent(invoice.receiving_session_id)}`}>{t('invoiceArchive.openReceiving')}</Link>}
      </section>}</div></div>
      {paymentOpen && finance && <form className="panel" data-testid="invoice-payment-form" onSubmit={event => { event.preventDefault(); if (!paymentValid || locked) return;
        const body: InvoicePaymentRequest = { request_id: crypto.randomUUID(), expected_revision: finance.payment_revision, kind: payment.kind, amount: decimalInput(payment.amount), paid_at: `${payment.paid_at}T00:00:00Z`, note: payment.note };
        if (finance.opening_required) body.opening_paid_amount = decimalInput(payment.opening_paid_amount);
        submitPayment({ invoice_id: id, actor: currentActor(), body });
      }}><h2>{t('invoiceArchive.addPayment')}</h2><fieldset disabled={locked}><div className="fields">
        <label>{t('invoiceArchive.paymentKind')}<select data-testid="invoice-payment-kind" value={payment.kind} onChange={event => setPayment({ ...payment, kind: event.target.value as 'payment' | 'refund' })}><option value="payment">{t('invoiceArchive.paymentKinds.payment')}</option><option value="refund">{t('invoiceArchive.paymentKinds.refund')}</option></select></label>
        <label>{t('invoiceArchive.amount')} ({invoice.currency})<input required data-testid="invoice-payment-amount" inputMode="decimal" pattern="[0-9]+([.,][0-9]{1,2})?" value={payment.amount} onChange={event => setPayment({ ...payment, amount: event.target.value })} /></label>
        <label>{t('invoiceArchive.paymentDate')}<input required data-testid="invoice-payment-date" type="date" value={payment.paid_at} onChange={event => setPayment({ ...payment, paid_at: event.target.value })} /></label>
        <label className="wide">{t('invoiceArchive.notes')}<input data-testid="invoice-payment-note" value={payment.note} onChange={event => setPayment({ ...payment, note: event.target.value })} /></label>
        {finance.opening_required && <label className="wide">{t('invoiceArchive.openingPaidAmount')} ({invoice.currency})<input required data-testid="invoice-opening-paid" inputMode="decimal" value={payment.opening_paid_amount} onChange={event => setPayment({ ...payment, opening_paid_amount: event.target.value })} /><span>{t('invoiceArchive.openingPaidHelp')}</span></label>}
      </div><p className="muted small">{t('invoiceArchive.paymentSignsHelp')}</p><footer><button type="button" onClick={() => setPaymentOpen(false)}>{t('common.cancel')}</button><button className="primary" data-testid="invoice-payment-save" disabled={!paymentValid}>{t('invoiceArchive.confirmPayment')}</button></footer></fieldset></form>}
      <section className="panel"><h2>{t('invoiceArchive.paymentHistory')}</h2>{finance?.payments.length ? <div className="table-scroll"><table><thead><tr>{['paymentDate', 'paymentKind', 'amount', 'notes', 'actor', 'actions'].map(key => <th key={key}>{t(`invoiceArchive.${key}`)}</th>)}</tr></thead><tbody>{finance.payments.map(row => <tr key={row.id} data-testid={`invoice-payment-row-${row.id}`}>
        <td>{invoiceDate(row.paid_at, i18n.language)}</td><td>{t(`invoiceArchive.paymentKinds.${row.reverses_payment_id ? 'reversal' : row.kind}`)}{row.reversed_by_id && <span className="secondary">{t('invoiceArchive.reversed')}</span>}</td><td className="amount">{invoiceMoney(row.amount, row.currency, i18n.language)}</td><td>{row.note || '—'}</td><td>{row.actor || '—'}</td>
        <td>{!['opening', 'reversal'].includes(row.kind) && !row.reverses_payment_id && !row.reversed_by_id && <button disabled={locked || editing} data-testid={`invoice-reverse-${row.id}`} onClick={() => { setReverseId(row.id); setReverseNote(''); }}>{t('invoiceArchive.reversePayment')}</button>}</td>
      </tr>)}</tbody></table></div> : <p className="muted">{t('invoiceArchive.noPaymentHistory')}</p>}
        {reverseId !== null && <form style={{ marginTop: '1rem' }} onSubmit={event => { event.preventDefault(); if (!finance || locked || !reverseNote.trim()) return;
          submitPayment({ invoice_id: id, actor: currentActor(), payment_id: reverseId, body: { request_id: crypto.randomUUID(), expected_revision: finance.payment_revision, note: reverseNote.trim() } });
        }}><fieldset disabled={locked}><p>{t('invoiceArchive.reverseHelp')}</p><label>{t('invoiceArchive.reason')}<input data-testid="invoice-reverse-note" required value={reverseNote} onChange={event => setReverseNote(event.target.value)} /></label><footer><button type="button" onClick={() => setReverseId(null)}>{t('common.cancel')}</button><button className="danger" data-testid="invoice-reverse-confirm" disabled={!reverseNote.trim()}>{t('invoiceArchive.confirmReversal')}</button></footer></fieldset></form>}
      </section>
      <section className="panel"><h2>{t('invoiceArchive.lines', { count: invoice.lines.length })}</h2>{invoice.parse_error && <p className="notice warning">{t('invoiceArchive.parseIncomplete')}</p>}
        {!invoice.lines.length ? <p className="muted">{t('invoiceArchive.noLines')}</p> : <div className="table-scroll"><table><thead><tr>{['line', 'ean', 'code', 'description', 'quantity', 'unitPrice', 'total_amount', 'linkedProduct'].map(key => <th key={key} className={['quantity', 'unitPrice', 'total_amount'].includes(key) ? 'amount' : ''}>{t(`invoiceArchive.${key}`)}</th>)}</tr></thead><tbody>{invoice.lines.map(line => <tr key={line.id}>
          <td>{line.line_number}</td><td>{line.ean || '—'}</td><td>{line.supplier_sku || '—'}</td><td>{line.product_name || '—'}</td><td className="amount">{line.quantity ?? '—'} {line.unit || '—'}</td>
          <td className="amount">{money(invoice.vat_included ? line.unit_price_with_vat ?? line.unit_price : line.unit_price)}</td><td className="amount">{money(invoice.vat_included ? line.total_price_with_vat ?? line.total_price : line.total_price)}</td>
          <td>{line.matched_product_id && line.product_sku ? <Link to={`/products/${encodeURIComponent(line.product_sku)}`}>{line.product_sku}</Link> : t('invoiceArchive.unlinked')}</td>
        </tr>)}</tbody></table><p className="muted small">{t(invoice.vat_included ? 'invoiceArchive.pricesGross' : 'invoiceArchive.pricesNet')}</p></div>}
      </section>
    </>}
  </div>;
}
export default InvoiceDetailPage;
