import React, { useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { accessRevision, hubUser } from '../api/access';
import { addInvoiceBatchPayment, getInvoiceBatchPayment, InvoiceBatchPaymentRequest, InvoiceBatchPaymentResult,
  InvoicePaymentMethod, invoicePaymentMethods } from '../api/invoiceArchive';
import { invoiceError, invoiceMoney } from '../pages/invoiceArchiveUi';
import { todayDate } from '../utils/format';

function readPending(key: string): InvoiceBatchPaymentRequest | null {
  try {
    const value = JSON.parse(sessionStorage.getItem(key) || 'null');
    return /^[0-9a-f-]{36}$/i.test(value?.request_id || '') && Array.isArray(value?.invoice_ids) ? value : null;
  } catch { return null; }
}

export function InvoiceBatchPayments({ selected, disabled, onApplied }: {
  selected: number[]; disabled: boolean; onApplied: () => void;
}) {
  const { t, i18n } = useTranslation();
  const key = `hub.invoice-batch-payment.${hubUser()?.id ?? 'operator-token'}`;
  const [pending, setPending] = useState(() => readPending(key));
  const [scope, setScope] = useState<number[]>([]);
  const [paidDate, setPaidDate] = useState(todayDate);
  const [method, setMethod] = useState<InvoicePaymentMethod | ''>('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [result, setResult] = useState<InvoiceBatchPaymentResult | null>(null);
  const sending = useRef(false);

  function clearPending() {
    try { sessionStorage.removeItem(key); } catch { /* The settled UUID remains replayable. */ }
    setPending(null);
  }
  async function submit(request: InvoiceBatchPaymentRequest, recover = false) {
    if (sending.current) return;
    if (!recover) {
      try { sessionStorage.setItem(key, JSON.stringify(request)); }
      catch { setError(t('invoiceArchive.operationStorageUnavailable')); return; }
      setPending(request);
    }
    sending.current = true;
    setBusy(true); setError('');
    const revision = accessRevision();
    try {
      let value;
      try { value = await (recover ? getInvoiceBatchPayment(request) : addInvoiceBatchPayment(request)); }
      catch (reason) {
        if (recover || (reason as { knownUnapplied?: boolean })?.knownUnapplied) throw reason;
        value = await getInvoiceBatchPayment(request);
      }
      if (revision === accessRevision()) {
        clearPending(); setScope([]); setResult(value); onApplied();
      }
    } catch (reason) {
      if (revision === accessRevision()) {
        if (!recover && (reason as { knownUnapplied?: boolean })?.knownUnapplied) {
          clearPending(); setError(invoiceError(reason, t('invoiceArchive.paymentFailed')));
        } else setError(t('invoiceArchive.paymentUncertain'));
      }
    } finally { sending.current = false; setBusy(false); }
  }

  return <div className="invoice-batch-payments">
    <button type="button" data-testid="invoice-batch-payment-open" disabled={disabled || !!pending || !!scope.length || !selected.length}
      onClick={() => { setScope([...selected]); setPaidDate(todayDate()); setMethod(''); setError(''); setResult(null); }}>{t('invoiceArchive.batchPayment.action')}</button>
    {error && <p role="alert">{error}</p>}
    {pending && <div className="notice warning"><p>{t('invoiceArchive.requestReference')} {pending.request_id}</p>
      <button type="button" disabled={busy} onClick={() => submit(pending, true)}>{t('invoiceArchive.recoverPayment')}</button>
      <button type="button" disabled={busy} onClick={() => submit(pending)}>{t('invoiceArchive.retrySamePayment')}</button></div>}
    {!!scope.length && !pending && <form onSubmit={event => {
      event.preventDefault();
      void submit({ request_id: crypto.randomUUID(), invoice_ids: scope, paid_date: paidDate, ...(method ? { method } : {}) });
    }}><p>{t('invoiceArchive.batchPayment.help', { count: scope.length })}</p>
      <div className="fields">
        <label>{t('invoiceArchive.paymentDate')}<input required type="date" value={paidDate} onChange={event => setPaidDate(event.target.value)} /></label>
        <label>{t('invoiceArchive.paymentMethod')}<select value={method} onChange={event => setMethod(event.target.value as InvoicePaymentMethod | '')}>
          <option value="">—</option>{invoicePaymentMethods.map(value => <option key={value} value={value}>{t(`invoiceArchive.paymentMethods.${value}`)}</option>)}</select></label>
      </div>
      <footer><button type="button" onClick={() => setScope([])}>{t('common.cancel')}</button>
        <button type="submit" className="primary" disabled={busy || !paidDate}>{t('invoiceArchive.batchPayment.action')}</button></footer>
    </form>}
    {result && <div role="status"><p>{t('invoiceArchive.batchPayment.result', { paid: result.paid.length, skipped: result.skipped.length })}</p>
      <ul>{result.paid.map(row => <li key={row.invoice_id}><Link to={`/invoices/${row.invoice_id}`}>#{row.invoice_id}</Link> {invoiceMoney(row.amount, row.currency, i18n.language)}</li>)}
        {result.skipped.map(row => <li key={row.invoice_id}><Link to={`/invoices/${row.invoice_id}`}>#{row.invoice_id}</Link> {t(`invoiceArchive.batchPayment.reasons.${row.reason}`)}</li>)}</ul>
    </div>}
  </div>;
}
