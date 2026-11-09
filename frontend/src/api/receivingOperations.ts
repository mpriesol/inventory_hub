import { API_BASE } from './client';
import { hubRequest } from './access';
import type { ReceivingStatus } from './receiving';

export type ReceivingOperationAction = 'reverse' | 'reopen' | 'archive' | 'restore';
export interface ReceivingOperationImpact {
  product_id: number; sku: string; name: string; stock_unit: string;
  before_quantity: string; delta: string; after_quantity: string;
  reserved: string; quarantined: string; available_before: string; available_after: string;
}
export interface ReceivingOperationResult {
  id: string; action: ReceivingOperationAction; reason: string; actor: string;
  session_id: number; supplier_code: string; invoice_number: string;
  status: ReceivingStatus; archived: boolean; posting_revision: number;
  stock_effect: 'unchanged' | 'reverse_receipt'; impacts: ReceivingOperationImpact[];
  movement_ids: number[]; completed_at: string;
}
export interface ReceivingOperationReview {
  id: string; session_id: number; action: ReceivingOperationAction; reason: string; actor: string;
  status: 'prepared' | 'blocked' | 'completed'; preview_hash: string; ready: boolean; expires_at: string;
  invoice_number: string; receipt_status: ReceivingStatus; after_status?: ReceivingStatus;
  warehouse: { id: number; code: string; name: string }; posting_revision: number; archived: boolean;
  stock_effect: 'unchanged' | 'reverse_receipt'; impacts: ReceivingOperationImpact[];
  errors: { code: string; product_id?: number }[]; result: ReceivingOperationResult | null;
}
export type ReceivingOperationRequest = { request_id: string; action: ReceivingOperationAction; reason: string };
export type ReceivingOperationConfirmation = { preview_hash: string; confirmed: true };
export interface ReceivingOperationHistoryEntry {
  id: string; action: ReceivingOperationAction; reason: string; actor: string; at: string;
  stock_effect: 'unchanged' | 'reverse_receipt'; impacts: ReceivingOperationImpact[];
  status: ReceivingStatus; archived: boolean;
}
const path = (supplier: string, session: string) => `${API_BASE}/suppliers/${encodeURIComponent(supplier)}/receiving/sessions/${encodeURIComponent(session)}/operations`;
const object = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value);
export async function getReceivingOperationHistory(supplier: string, session: string, signal?: AbortSignal) {
  const value = await hubRequest<unknown>(path(supplier, session), undefined, signal);
  if (!object(value) || String(value.session_id) !== session || !Array.isArray(value.operations) || value.operations.length > 50
      || !value.operations.every(item => object(item) && ['id', 'reason', 'actor', 'at'].every(key => typeof item[key] === 'string')
        && ['reopen', 'reverse', 'archive', 'restore'].includes(String(item.action)) && Array.isArray(item.impacts))) {
    throw new Error('receiving_operation_history_invalid');
  }
  return value as unknown as { session_id: number; limit: number; operations: ReceivingOperationHistoryEntry[] };
}
export function validReceivingResult(value: unknown, supplier: string, session: string, id: string): value is ReceivingOperationResult {
  return object(value) && value.id === id && String(value.session_id) === session && value.supplier_code === supplier
    && ['new', 'in_progress', 'paused', 'completed', 'cancelled'].includes(String(value.status))
    && typeof value.archived === 'boolean' && Number.isInteger(value.posting_revision)
    && typeof value.completed_at === 'string' && Array.isArray(value.movement_ids) && Array.isArray(value.impacts);
}
export async function previewReceivingOperation(supplier: string, session: string, body: ReceivingOperationRequest) {
  const value = await hubRequest<unknown>(`${path(supplier, session)}/preview`, body);
  if (!object(value) || value.id !== body.request_id || String(value.session_id) !== session
      || value.action !== body.action || value.reason !== body.reason || !/^[0-9a-f]{64}$/.test(String(value.preview_hash))
      || !['prepared', 'blocked', 'completed'].includes(String(value.status)) || typeof value.ready !== 'boolean'
      || !Array.isArray(value.impacts) || !Array.isArray(value.errors)
      || (value.status === 'completed' && !validReceivingResult(value.result, supplier, session, body.request_id))) {
    throw new Error('receiving_operation_response_invalid');
  }
  return value as unknown as ReceivingOperationReview;
}
export async function applyReceivingOperation(supplier: string, session: string, id: string, body: ReceivingOperationConfirmation) {
  const value = await hubRequest<unknown>(`${path(supplier, session)}/${encodeURIComponent(id)}/apply`, body);
  if (!validReceivingResult(value, supplier, session, id)) throw new Error('receiving_operation_response_invalid');
  return value;
}
