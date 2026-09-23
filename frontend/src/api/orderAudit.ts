import { hubRequest } from './access';

export type AuditClassification = 'mapped' | 'identified' | 'manual' | 'non_stock' | 'unresolved' | 'conflict';
export interface OrderAuditLine {
  line_key: string;
  code: string;
  title: string;
  ean: string;
  quantity: string | null;
  unit: string;
  classification: AuditClassification;
  product_id: number | null;
  sku: string | null;
  matched_by: string | null;
  reasons: string[];
}
export interface AuditedOrder {
  order_number: string;
  origin: string;
  created_at: string | null;
  updated_at: string | null;
  status_id: number | null;
  status_name: string;
  status_type: string | null;
  paid: boolean;
  resolved: boolean;
  delivered: boolean;
  candidate: 'issue' | 'reserve' | 'cancel' | 'review';
  candidate_reason: string;
  lines: OrderAuditLine[];
  warnings: string[];
}
export interface OrderAudit {
  shop: string;
  fetched_at: string;
  read_only: true;
  page: number;
  number_of_pages: number;
  number_of_items: number;
  has_more: boolean;
  summary: { orders: number; lines: number } & Record<AuditClassification, number>;
  warnings: string[];
  orders: AuditedOrder[];
}
export function fetchOrderAudit(shop: string, days: number, page: number, signal?: AbortSignal) {
  const query = new URLSearchParams({ days: String(days), page: String(page) });
  return hubRequest<OrderAudit>(`/api/shops/${encodeURIComponent(shop)}/upgates/orders/audit?${query}`, undefined, signal);
}
