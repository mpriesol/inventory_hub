import type { TFunction } from 'i18next';

const sourceCodes = new Set([
  'receiving_source_unverified', 'receiving_source_currency_unsupported',
  'receiving_source_vat_unverified', 'receiving_source_changed',
  'receiving_source_duplicate', 'receiving_csv_invalid',
  'receiving_direct_csv_unsupported', 'receiving_spreadsheet_supplier_unsupported',
]);

function sourceDetail(error: unknown): Record<string, unknown> | null {
  if (!error || typeof error !== 'object') return null;
  const detail = (error as { detail?: unknown }).detail;
  return detail && typeof detail === 'object' ? detail as Record<string, unknown> : null;
}

export function receivingSourceError(error: unknown, t: TFunction): string | null {
  const detail = sourceDetail(error);
  const code = detail?.code;
  if (typeof code !== 'string' || !sourceCodes.has(code)) return null;
  return t(`receiving.sourceErrors.${code}`);
}

export function duplicateReceivingLink(error: unknown, supplier: string): string | null {
  const detail = sourceDetail(error);
  if (detail?.code !== 'receiving_source_duplicate') return null;
  const session = String(detail.session_id ?? '');
  if (!/^\d+$/.test(session) || typeof detail.invoice_number !== 'string' || !detail.invoice_number) return null;
  return `/receiving/${encodeURIComponent(detail.invoice_number)}?supplier=${encodeURIComponent(supplier)}&session=${session}`;
}
