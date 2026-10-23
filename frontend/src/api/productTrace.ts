import { hubRequest } from './access';
import type { Movement } from './stockHistory';
import type { ProductItemType, StockUnit } from './productEditor';

type Decimal = string | null;
export interface TraceMatch { id: number; sku: string; name: string; item_type: ProductItemType; stock_unit: StockUnit }
export interface TraceInvoice {
  invoice_id: number; line_id: number; direction: 'purchase' | 'sale'; invoice_number: string | null; product_name: string | null; invoice_date: string | null; due_date: string | null;
  supplier_name: string | null; shop_code: string | null; currency: string | null;
  quantity: Decimal; unit: string | null; unit_price: Decimal; total_price: Decimal; matched_by: string;
}
export interface TracePage<T> { items: T[]; total: number; page: number; page_size: number }
export interface ProductTrace {
  code: string; matches: TraceMatch[]; matches_truncated?: boolean;
  product: (TraceMatch & { brand: string | null; eans: string[]; card_url: string }) | null;
  stocks: { warehouse_code: string; warehouse_name: string; on_hand: Decimal; reserved: Decimal; quarantined: Decimal; available: Decimal; confirmed: boolean }[];
  prices: { source: string; label: string; price: Decimal; currency: string | null; vat_included: boolean | null; observed_at: string | null; source_invoice_unit?: string | null }[];
  invoices: TracePage<TraceInvoice>;
  sales: { sold_quantity: Decimal; returned_quantity: Decimal; net_quantity: Decimal; unit: string; source: 'hub_ledger'; since: string | null; monthly: { month: string; sold: Decimal; returned: Decimal; net: Decimal }[] } | null;
  sales_documents?: {
    source: 'shop_invoice_archive';
    by_unit: { unit: string | null; sold_quantity: Decimal; returned_quantity: Decimal; net_quantity: Decimal }[];
    monthly: { month: string; unit: string | null; sold: Decimal; returned: Decimal; net: Decimal }[];
  } | null;
  movements: TracePage<Movement>;
  warnings: string[];
}
export function getProductTrace(filters: { code?: string; product_id?: number; invoice_page?: number; movement_page?: number }, signal?: AbortSignal) {
  const params = new URLSearchParams();
  Object.entries(filters).forEach(([key, value]) => { if (value !== undefined && value !== '') params.set(key, String(value)); });
  return hubRequest<ProductTrace>(`/api/product-trace?${params}`, undefined, signal);
}
