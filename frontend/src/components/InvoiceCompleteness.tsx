import React, { useState } from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { compareInvoiceChecklist, previewInvoiceChecklist, InvoiceChecklistResult, InvoiceChecklistRow } from '../api/invoiceArchive';
import { invoiceDate, invoiceError, invoiceMoney } from '../pages/invoiceArchiveUi';
import { todayDate } from '../utils/format';

export function InvoiceCompleteness({ from, to }: { from: string; to: string }) {
  const { t, i18n } = useTranslation();
  const label = (key: string) => t(`invoiceArchive.completeness.${key}`);
  const [dateFrom, setDateFrom] = useState(from || todayDate().slice(0, 7) + '-01');
  const [dateTo, setDateTo] = useState(to || todayDate());
  const [file, setFile] = useState<File | null>(null);
  const [rows, setRows] = useState<InvoiceChecklistRow[]>([]);
  const [report, setReport] = useState<InvoiceChecklistResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  async function preview() {
    if (!file || busy) return;
    setBusy(true); setRows([]); setReport(null); setError('');
    try { setRows((await previewInvoiceChecklist(file)).rows); }
    catch (reason) { setError(invoiceError(reason, label('failed'))); }
    finally { setBusy(false); }
  }
  async function compare() {
    if (!rows.length || busy || !dateFrom || !dateTo || dateFrom > dateTo) return;
    setBusy(true); setReport(null); setError('');
    try { setReport(await compareInvoiceChecklist(dateFrom, dateTo, rows)); }
    catch (reason) { setError(invoiceError(reason, label('failed'))); }
    finally { setBusy(false); }
  }

  function table(items: InvoiceChecklistRow[]) {
    return <div className="table-scroll"><table>
      <thead><tr>{['row', 'issuer', 'number', 'date', 'amount', 'reason'].map(key => <th scope="col" key={key}>{label(key)}</th>)}</tr></thead>
      <tbody>{items.map(row => <tr key={row.row}>
        <td>{row.row}</td><td>{row.issuer || '—'}</td>
        <td>{row.invoice_id ? <Link to={`/invoices/${row.invoice_id}`}>{row.number}</Link> : row.number || '—'}</td>
        <td>{invoiceDate(row.invoice_date, i18n.language)}</td><td>{invoiceMoney(row.amount, row.currency, i18n.language)}</td>
        <td>{row.issue ? label(`issues.${row.issue}`) : '—'}{row.issue === 'unreadable' && row.raw && <small className="secondary">{row.raw}</small>}</td>
      </tr>)}</tbody>
    </table></div>;
  }

  return <section className="panel">
    <h2>{label('title')}</h2><p className="small muted">{label('help')}</p>
    <fieldset disabled={busy}><div className="fields">
      <label>{t('invoiceArchive.date_from')}<input type="date" value={dateFrom} onChange={event => { setDateFrom(event.target.value); setReport(null); }} /></label>
      <label>{t('invoiceArchive.date_to')}<input type="date" value={dateTo} onChange={event => { setDateTo(event.target.value); setReport(null); }} /></label>
      <label>{label('file')}<input type="file" accept=".csv,.xlsx,.pdf" onChange={event => { setFile(event.target.files?.[0] || null); setReport(null); setRows([]); }} /></label>
    </div><div className="actions">
      <button type="button" disabled={!file} onClick={preview}>{label('preview')}</button>
      <button type="button" className="primary" disabled={!rows.length || !dateFrom || !dateTo || dateFrom > dateTo} onClick={compare}>{label('compare')}</button>
    </div></fieldset>
    {busy && <p role="status">{t('common.loading')}</p>}
    {error && <p role="alert">{error}</p>}
    {!report && !!rows.length && <section><h3>{label('preview')}</h3>{table(rows)}</section>}
    {report && (['found', 'missing', 'review'] as const).map(group => <details key={group} open>
      <summary>{label(group)} ({report[group].length})</summary>{!!report[group].length && table(report[group])}
    </details>)}
  </section>;
}
