import React, { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { accessRevision, hubUser } from '../api/access';
import {
  ArchivedInvoiceDetail, InvoiceProcessingAction, InvoiceProcessingApplyRequest, InvoiceProcessingPreview,
  InvoiceProcessingResult, applyInvoiceProcessing, getInvoiceProcessingOperation, previewInvoiceProcessing,
} from '../api/invoiceArchive';
import { invoiceDate } from '../pages/invoiceArchiveUi';

type Pending = { invoice_id: number; actor: string; body: InvoiceProcessingApplyRequest };
const actor = () => String(hubUser()?.id ?? hubUser()?.username ?? 'operator-token');
const key = (id: number) => `hub.invoice-processing.${id}`;
function readPending(id: number): Pending | null {
  try {
    const value = JSON.parse(sessionStorage.getItem(key(id)) || 'null');
    return value?.invoice_id === id && value.actor === actor() && /^[0-9a-f-]{36}$/i.test(value.body?.request_id || '') ? value : null;
  } catch { return null; }
}

export function InvoiceProcessingPanel({ invoice, disabled, onChange, onLockChange }: {
  invoice: ArchivedInvoiceDetail; disabled: boolean;
  onChange: (result: InvoiceProcessingResult) => Promise<void>; onLockChange: (locked: boolean) => void;
}) {
  const { t, i18n } = useTranslation();
  const state = invoice.processing;
  const [action, setAction] = useState<InvoiceProcessingAction>(() => state?.allowed_actions[0] || 'archive');
  const [note, setNote] = useState('');
  const [preview, setPreview] = useState<InvoiceProcessingPreview | null>(null);
  const [pending, setPending] = useState<Pending | null>(() => readPending(invoice.id));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const working = useRef(false);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; onLockChange(false); }; }, [onLockChange]);
  useEffect(() => { onLockChange(busy || !!pending); }, [busy, pending, onLockChange]);
  useEffect(() => {
    setPreview(null);
    if (state?.allowed_actions.length && !state.allowed_actions.includes(action)) setAction(state.allowed_actions[0]);
  }, [state?.revision, state?.allowed_actions.join('|')]);
  const retain = (value: Pending | null) => {
    setPending(value);
    try { if (value) sessionStorage.setItem(key(invoice.id), JSON.stringify(value)); else sessionStorage.removeItem(key(invoice.id)); } catch { /* Mounted state retains the exact request. */ }
  };
  const explain = (reason: unknown) => {
    const code = (reason as { code?: string })?.code;
    return code ? t(`invoiceArchive.processing.errors.${code}`, { defaultValue: t('invoiceArchive.processing.failed') }) : t('invoiceArchive.processing.failed');
  };
  const finish = async (result: InvoiceProcessingResult) => {
    retain(null); setPreview(null); setNote(''); setError(''); setMessage(t('invoiceArchive.processing.saved'));
    try { await onChange(result); } catch { if (mounted.current) setError(t('invoiceArchive.loadFailed')); }
  };
  const loadPreview = async (event: React.FormEvent) => {
    event.preventDefault(); if (!state || working.current || disabled || pending || !note.trim()) return;
    working.current = true; setBusy(true); setError(''); setMessage(''); setPreview(null);
    const access = accessRevision();
    try {
      const value = await previewInvoiceProcessing(invoice.id, { action, expected_revision: state.revision, note: note.trim() });
      if (mounted.current && access === accessRevision()) setPreview(value);
    } catch (reason) { if (mounted.current && access === accessRevision()) setError(explain(reason)); }
    finally { working.current = false; if (mounted.current) setBusy(false); }
  };
  const send = async (operation: Pending) => {
    if (working.current || disabled) return;
    working.current = true; setBusy(true); setError(''); setMessage(''); retain(operation);
    const access = accessRevision();
    try {
      const value = await applyInvoiceProcessing(invoice.id, operation.body);
      if (mounted.current && access === accessRevision()) await finish(value);
    } catch (reason) {
      if (!mounted.current || access !== accessRevision()) return;
      const failure = reason as { knownUnapplied?: boolean; status?: number };
      if (failure.knownUnapplied || [400, 401, 403, 422].includes(failure.status || 0)) {
        retain(null); setPreview(null); setError(explain(reason));
      } else {
        try {
          const value = await getInvoiceProcessingOperation(invoice.id, operation.body.request_id);
          if (mounted.current && access === accessRevision()) await finish(value);
        } catch { if (mounted.current && access === accessRevision()) setError(t('invoiceArchive.processing.uncertain')); }
      }
    } finally { working.current = false; if (mounted.current) setBusy(false); }
  };
  const recover = async () => {
    if (!pending || working.current || disabled) return;
    working.current = true; setBusy(true); setError(''); const access = accessRevision();
    try {
      const value = await getInvoiceProcessingOperation(invoice.id, pending.body.request_id);
      if (mounted.current && access === accessRevision()) await finish(value);
    } catch (reason) { if (mounted.current && access === accessRevision()) setError(explain(reason)); }
    finally { working.current = false; if (mounted.current) setBusy(false); }
  };
  if (!state) return null;
  const locked = disabled || busy || !!pending;
  const status = (value: string) => t(`invoiceArchive.processing.statuses.${value}`, { defaultValue: value });
  const visibility = (archived: boolean) => t(`invoiceArchive.processing.${archived ? 'archived' : 'active'}`);
  return <section className="panel" data-testid="invoice-processing-panel">
    <header><h2>{t('invoiceArchive.processing.title')}</h2><div className="badge-stack">
      {invoice.direction === 'purchase' && <span className="badge" data-testid="invoice-processing-status">{status(state.status)}</span>}
      {state.archived && <span className="badge" data-testid="invoice-archived-badge">{visibility(true)}</span>}
    </div></header>
    <p className="muted small">{t('invoiceArchive.processing.scope')}</p>
    {state.archived && <p className="notice warning">{t('invoiceArchive.processing.archivedHelp')}</p>}
    {error && <p className="notice error" role="alert" data-testid="invoice-processing-error">{error}</p>}
    {message && <p className="notice success" role="status">{message}</p>}
    {pending && <div className="notice warning" data-testid="invoice-processing-pending"><p>{t('invoiceArchive.processing.uncertain')}</p>
      <p className="small">{t('invoiceArchive.requestReference')}: {pending.body.request_id}</p><div className="actions">
        <button disabled={busy || disabled} data-testid="invoice-processing-recover" onClick={recover}>{t('invoiceArchive.processing.recover')}</button>
        <button disabled={busy || disabled} data-testid="invoice-processing-retry" onClick={() => send(pending)}>{t('invoiceArchive.processing.retry')}</button>
      </div></div>}
    {state.allowed_actions.length > 0 && <form onSubmit={loadPreview}>
      <fieldset disabled={locked}><div className="fields">
        <label>{t('invoiceArchive.processing.action')}<select data-testid="invoice-processing-action" value={action}
          onChange={event => { setAction(event.target.value as InvoiceProcessingAction); setPreview(null); setMessage(''); }}>
          {state.allowed_actions.map(value => <option key={value} value={value}>{t(`invoiceArchive.processing.actions.${value}`)}</option>)}
        </select></label>
        <label className="wide">{t('invoiceArchive.reason')}<textarea required maxLength={2000} data-testid="invoice-processing-note" value={note}
          onChange={event => { setNote(event.target.value); setPreview(null); setMessage(''); }} /></label>
      </div><p className="muted small">{t(`invoiceArchive.processing.help.${action}`)}</p>
        <footer><button data-testid="invoice-processing-preview" disabled={!note.trim()}>{t('invoiceArchive.processing.preview')}</button></footer>
      </fieldset>
    </form>}
    {!state.allowed_actions.length && <p className="notice warning">{t('invoiceArchive.processing.receiptFirst')}</p>}
    {preview && <div className="invoice-processing-preview" data-testid="invoice-processing-preview-result">
      <h3>{t('invoiceArchive.processing.previewTitle')}</h3>
      <dl className="facts"><div><dt>{t('invoiceArchive.processing.action')}</dt><dd>{t(`invoiceArchive.processing.actions.${preview.action}`)}</dd></div>
        {invoice.direction === 'purchase' && <div><dt>{t('invoiceArchive.processing.state')}</dt><dd>{status(preview.before.status)} → {status(preview.after.status)}</dd></div>}
        <div><dt>{t('invoiceArchive.processing.visibility')}</dt><dd>{visibility(preview.before.archived)} → {visibility(preview.after.archived)}</dd></div>
        <div><dt>{t('invoiceArchive.reason')}</dt><dd>{preview.note}</dd></div>
      </dl><p className="notice" data-testid="invoice-processing-no-effects">{t('invoiceArchive.processing.noEffects')}</p>
      <footer><button disabled={locked} onClick={() => setPreview(null)}>{t('common.cancel')}</button>
        <button className="primary" disabled={locked} data-testid="invoice-processing-confirm" onClick={() => send({ invoice_id: invoice.id, actor: actor(), body: {
          request_id: crypto.randomUUID(), action: preview.action, expected_revision: preview.before.revision,
          note: preview.note, preview_hash: preview.preview_hash, confirmed: true,
        } })}>{t('invoiceArchive.processing.confirm')}</button></footer>
    </div>}
    {!!state.history.length && <details className="invoice-processing-history"><summary>{t('invoiceArchive.processing.history')}</summary>
      <div className="table-scroll"><table><thead><tr>{['created_at', 'processing.action', 'reason', 'actor'].map(value => <th key={value}>{t(`invoiceArchive.${value}`)}</th>)}</tr></thead>
        <tbody>{state.history.map(item => <tr key={item.request_id}><td>{invoiceDate(item.created_at, i18n.language)}</td>
          <td>{t(`invoiceArchive.processing.actions.${item.action}`)}</td><td>{item.note}</td><td>{item.actor}</td></tr>)}</tbody>
      </table></div>
    </details>}
  </section>;
}
