import { hubRequest } from './access';

export interface OpeningIssue { row: number | null; code: string; field?: string }
export interface OpeningWarehouse { id: number; code: string; name: string }
export interface OpeningSummary { lines: number; quantity: string; total_value: string }
export interface OpeningLine {
  line_number: number; product_id: number; sku: string; name: string;
  quantity: string; unit_cost: string; value: string; unit: 'ks'; warnings: string[];
}
export interface OpeningResult {
  batch_id: string; completed_at: string; movements_created: number; summary: OpeningSummary;
  lines: { line_number: number; sku: string; product_id: number; movement_id: number; quantity: string; unit_cost: string; value: string }[];
}
export interface OpeningBatchInfo {
  id: string; status: 'prepared' | 'completed'; preview_hash: string;
  warehouse: OpeningWarehouse; source_reference: string; operator_name: string;
  counted_at: string; created_at: string; expires_at: string; completed_at: string | null;
  unit: 'ks'; currency: 'EUR'; price_basis: 'ex_vat'; summary: OpeningSummary; warnings: OpeningIssue[];
}
export interface OpeningBatch extends OpeningBatchInfo { lines: OpeningLine[]; result: OpeningResult | null }
export interface OpeningOptions {
  warehouses: OpeningWarehouse[];
  limits: { max_bytes: number; max_rows: number; max_quantity: string; max_unit_cost: string };
  unit: 'ks'; currency: 'EUR'; price_basis: 'ex_vat'; expires_minutes: number;
}
export interface OpeningInput {
  request_id: string; warehouse_code: string; source_reference: string; operator_name: string;
  counted_at: string; csv_text: string;
}
export interface OpeningPreview { ready: boolean; errors: OpeningIssue[]; warnings: OpeningIssue[]; batch: OpeningBatch | null }
const base = '/api/stock/opening';
export const getOpeningOptions = (signal?: AbortSignal) => hubRequest<OpeningOptions>(`${base}/options`, undefined, signal);
export const previewOpening = (input: OpeningInput, signal?: AbortSignal) => hubRequest<OpeningPreview>(`${base}/preview`, input, signal);
export const listOpeningBatches = (signal?: AbortSignal) => hubRequest<{ batches: OpeningBatchInfo[]; limit: number }>(`${base}/batches`, undefined, signal);
export const getOpeningBatch = (id: string, signal?: AbortSignal) => hubRequest<OpeningBatch>(`${base}/${encodeURIComponent(id)}`, undefined, signal);
export const finalizeOpening = (batch: OpeningBatch) => hubRequest<OpeningResult>(`${base}/${encodeURIComponent(batch.id)}/finalize`, {
  preview_hash: batch.preview_hash, confirmed: true, receipts_reconciled: true,
});
