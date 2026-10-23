import React from 'react';
import { useTranslation } from 'react-i18next';
import { TFunction } from 'i18next';
import { ArchivedInvoice, InvoiceAmount, ShopInvoiceSourceDiagnostic } from '../api/invoiceArchive';

/** String formatting preserves the stored amount; calculation belongs to the API. */
export function invoiceMoney(value: InvoiceAmount | null | undefined, currency: string | null = 'EUR', language = 'sk'): string {
  if (value === null || value === undefined || !/^-?\d+(\.\d+)?$/.test(String(value))) return '—';
  const [integer, fraction = ''] = String(value).split('.');
  const grouped = BigInt(integer).toLocaleString(language === 'sk' ? 'sk-SK' : 'en-GB');
  const decimal = language === 'sk' ? ',' : '.';
  const negativeZero = integer.startsWith('-') && BigInt(integer) === 0n ? '-' : '';
  return `${negativeZero}${grouped}${decimal}${fraction.padEnd(2, '0')} ${currency || '—'}`;
}
export function invoiceDate(value?: string | null, language = 'sk'): string {
  if (!value) return '—';
  const date = new Date(value.includes('T') ? value : `${value}T12:00:00Z`);
  return Number.isNaN(date.valueOf()) ? '—' : date.toLocaleDateString(language === 'sk' ? 'sk-SK' : 'en-GB');
}
export function InvoicePaymentBadge({ invoice }: { invoice: ArchivedInvoice }) {
  const { t } = useTranslation();
  const status = invoice.finance?.status || invoice.payment_status || 'unknown';
  const overdue = invoice.finance?.is_overdue ?? invoice.is_overdue;
  return <span className="badge-stack"><span className={`badge ${status}`}>{t(`invoiceArchive.status.${status}`)}</span>
    {overdue && <span className="badge overdue">{t('invoiceArchive.status.overdue')}</span>}</span>;
}
export function invoiceError(error: unknown, fallback: string): string {
  const code = (error as { code?: string })?.code;
  return code && code !== 'request_failed' ? `${fallback} (${code})` : fallback;
}

export function invoiceSourceDiagnostic(value: unknown, t: TFunction): string {
  const diagnostic = value as ShopInvoiceSourceDiagnostic | null | undefined;
  if (diagnostic?.operation !== 'list' && diagnostic?.operation !== 'pdf') return '';
  const label = t(diagnostic.operation === 'list' ? 'invoiceArchive.syncListOperation' : 'invoiceArchive.syncPdfOperation');
  const status = diagnostic.upstream_status;
  const summary = typeof status === 'number' && Number.isInteger(status) && status >= 100 && status <= 599
    ? `${label} (HTTP ${status})` : label;
  if (diagnostic.operation !== 'list' || status !== 400 && status !== 422 || !Array.isArray(diagnostic.rejected_parameters)) return summary;
  const parameters = (['creation_time_from', 'creation_time_to', 'page'] as const)
    .filter(name => diagnostic.rejected_parameters?.includes(name))
    .map(name => t(`invoiceArchive.syncParameterLabels.${name}`));
  return parameters.length ? `${summary}. ${t('invoiceArchive.syncRejectedParameters', { parameters: parameters.join(', ') })}` : summary;
}
