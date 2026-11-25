import { hubRequest } from './access';
import { SkuIdentityInput } from './skuPolicy';

const base = '/api/supplier-preorders';

export interface PreorderInvoice { id: number; invoice_number: string | null; original_filename: string; invoice_date: string | null }
export interface PreorderLine {
  id: number; product_id: number; sku: string; name: string; stock_unit: string; expected_on: string;
  ordered_quantity: number; received_quantity: number; remaining_quantity: number;
  deliveries: { invoice_id: number; invoice_number: string; quantity: number; received_at: string }[];
}
export interface SupplierPreorder {
  id: string; supplier_name: string; reference: string; ordered_on: string; approved_at: string; status: 'open' | 'completed';
  lines: PreorderLine[]; invoices: PreorderInvoice[];
}
export interface PreorderCreate {
  request_id: string; supplier_id: number; reference: string; ordered_on: string; approved: boolean;
  lines: { sku?: string; new_card?: { name: string; sku_identity: SkuIdentityInput; eans: string[] };
    ordered_quantity: string; expected_on: string }[];
}
export const getPreorderSuppliers = (signal?: AbortSignal) => hubRequest<{ id: number; name: string }[]>(`${base}/suppliers`, undefined, signal);
export const listSupplierPreorders = (params: { q?: string; page?: number; product_id?: number } = {}, signal?: AbortSignal) => {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) if (value !== undefined) query.set(key, String(value));
  return hubRequest<{ items: SupplierPreorder[]; total: number; page: number }>(`${base}?${query}`, undefined, signal);
};
export const getSupplierPreorder = (id: string, signal?: AbortSignal) => hubRequest<SupplierPreorder>(`${base}/${encodeURIComponent(id)}`, undefined, signal);
export const createSupplierPreorder = (value: PreorderCreate) => hubRequest<SupplierPreorder>(base, value);
export const listPreorderInvoices = (id: string, q: string, signal?: AbortSignal) =>
  hubRequest<PreorderInvoice[]>(`${base}/${encodeURIComponent(id)}/invoices?${new URLSearchParams({ q })}`, undefined, signal);
export const linkPreorderInvoice = (id: string, invoice_id: number) =>
  hubRequest<SupplierPreorder>(`${base}/${encodeURIComponent(id)}/invoices`, { invoice_id });
export const changePreorderDate = (id: string, line: PreorderLine, expected_on: string) =>
  hubRequest<SupplierPreorder>(`${base}/${encodeURIComponent(id)}/lines/${line.id}`, { expected_on, previous_expected_on: line.expected_on }, undefined, 'PUT');
