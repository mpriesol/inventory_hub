import { hubRequest } from './access';
import { InvoiceArchiveFilters, InvoiceDirection, InvoicePaymentStatus } from './invoiceArchive';

export interface InvoiceCalendarItem {
  id: number; invoice_number: string | null; original_filename: string; direction: InvoiceDirection;
  supplier_code: string | null; supplier_name: string | null; shop_code: string | null; shop_name: string | null;
  due_date: string; currency: string | null; gross_amount: string | null; outstanding_amount: string | null;
  payment_status: InvoicePaymentStatus; is_overdue: boolean;
}
export interface InvoiceCalendarResult {
  range_from: string; range_to: string; items: InvoiceCalendarItem[]; total: number; limit: number;
  totals: InvoiceCalendarTotal[]; weeks: InvoiceCalendarPeriod[]; months: InvoiceCalendarPeriod[];
}
export interface InvoiceCalendarTotal { currency: string | null; outstanding_amount: string; unknown_count: number; invoice_count: number }
export interface InvoiceCalendarPeriod { from: string; to: string; totals: InvoiceCalendarTotal[]; invoice_ids: number[] }

export function invoiceCalendarQuery(filters: InvoiceArchiveFilters, from: string, to: string) {
  const values: Record<string, unknown> = { ...filters, range_from: from, range_to: to };
  const query = new URLSearchParams();
  Object.entries(values).forEach(([key, value]) => {
    if (!['sort_by', 'sort_order'].includes(key) && value !== '' && value !== undefined && value !== null)
      query.set(key, String(value));
  });
  return query.toString();
}
export const getInvoiceCalendar = (filters: InvoiceArchiveFilters, from: string, to: string, signal?: AbortSignal) =>
  hubRequest<InvoiceCalendarResult>(`/api/invoices/calendar?${invoiceCalendarQuery(filters, from, to)}`, undefined, signal);
