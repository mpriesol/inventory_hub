import { API_BASE, fetchJSON } from "./client";

export interface ReceivingSession {
  session_id: string;
  invoice_no: string;
  lines: ReceivingLine[];
}

export interface ReceivingLine {
  ean: string;
  scm: string;
  product_code: string;
  title: string;
  ordered_qty: number;
  received_qty: number;
  status: 'pending' | 'partial' | 'matched' | 'overage';
}

export interface ScanResult {
  status: 'matched' | 'partial' | 'pending' | 'overage' | 'unexpected' | 'unknown';
  line: ReceivingLine | null;
  summary: ReceivingSummary;
}

export interface ReceivingSummary {
  matched: number;
  partial: number;
  pending: number;
  overage: number;
  unexpected: number;
}

export interface FinalizeResult {
  success: boolean;
  invoice_no: string;
  session_id: string;
  completed_at: string;
  stats: {
    total_lines: number;
    received_complete: number;
    received_partial: number;
    received_overage: number;
    not_received: number;
    total_scans: number;
    unexpected_scans: number;
  };
  total_ordered: number;
  total_received: number;
  received_items_count: number;
  message: string;
}

export async function createReceivingSession(supplier: string, invoice_id: string): Promise<ReceivingSession> {
  return fetchJSON<ReceivingSession>(`${API_BASE}/suppliers/${supplier}/receiving/sessions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ invoice_id })
  });
}

export async function scanCode(
  supplier: string, 
  session_id: string, 
  code: string, 
  qty: number = 1
): Promise<ScanResult> {
  return fetchJSON<ScanResult>(`${API_BASE}/suppliers/${supplier}/receiving/sessions/${session_id}/scan`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code, qty })
  });
}

export async function getReceivingSummary(
  supplier: string, 
  session_id: string
): Promise<{ invoice_no: string; lines: ReceivingLine[]; summary: ReceivingSummary }> {
  return fetchJSON(`${API_BASE}/suppliers/${supplier}/receiving/sessions/${session_id}/summary`);
}

export async function finalizeReceiving(
  supplier: string, 
  session_id: string,
  force: boolean = false
): Promise<FinalizeResult> {
  return fetchJSON<FinalizeResult>(`${API_BASE}/suppliers/${supplier}/receiving/sessions/${session_id}/finalize`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ force })
  });
}

export interface PauseResult {
  success: boolean;
  invoice_no: string;
  session_id: string;
  paused_at: string;
  stats: {
    total_lines: number;
    received_complete: number;
    received_partial: number;
    not_received: number;
    total_scans: number;
  };
  message: string;
}

export async function pauseReceiving(
  supplier: string,
  session_id: string
): Promise<PauseResult> {
  return fetchJSON<PauseResult>(`${API_BASE}/suppliers/${supplier}/receiving/sessions/${session_id}/pause`, {
    method: "POST"
  });
}

export interface ResumeResult {
  session_id: string;
  invoice_no: string;
  lines: ReceivingLine[];
  scans: Array<{ ts: string; code: string; qty: number; status: string }>;
  created_at: string;
  resumed_at: string;
}

export async function resumeReceiving(
  supplier: string,
  session_id: string
): Promise<ResumeResult> {
  return fetchJSON<ResumeResult>(`${API_BASE}/suppliers/${supplier}/receiving/sessions/${session_id}/resume`, {
    method: "POST"
  });
}

export interface ActiveSessionInfo {
  has_session: boolean;
  session: {
    session_id: string;
    created_at: string;
    is_paused: boolean;
    paused_at: string | null;
    lines_count: number;
    scans_count: number;
    stats: {
      matched: number;
      partial: number;
      pending: number;
    };
  } | null;
}

export async function getActiveSession(
  supplier: string,
  invoice_no: string
): Promise<ActiveSessionInfo> {
  return fetchJSON<ActiveSessionInfo>(`${API_BASE}/suppliers/${supplier}/invoices/${invoice_no}/active-session`);
}

export async function reopenInvoice(
  supplier: string,
  invoice_no: string
): Promise<{ success: boolean; message: string }> {
  return fetchJSON(`${API_BASE}/suppliers/${supplier}/invoices/${invoice_no}/reopen`, {
    method: "POST"
  });
}

// Manual quantity edit
export interface SetQtyResult {
  success: boolean;
  line: ReceivingLine;
  summary: ReceivingSummary;
}

export async function setLineQuantity(
  supplier: string,
  session_id: string,
  line_index: number,
  received_qty: number,
  note?: string
): Promise<SetQtyResult> {
  return fetchJSON<SetQtyResult>(`${API_BASE}/suppliers/${supplier}/receiving/sessions/${session_id}/set-qty`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ line_index, received_qty, note })
  });
}

// Bulk accept all
export interface BulkActionResult {
  success: boolean;
  updated_count: number;
  lines: ReceivingLine[];
  summary: ReceivingSummary;
  message: string;
}

export async function acceptAllItems(
  supplier: string,
  session_id: string,
  only_pending: boolean = true
): Promise<BulkActionResult> {
  return fetchJSON<BulkActionResult>(`${API_BASE}/suppliers/${supplier}/receiving/sessions/${session_id}/accept-all`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ only_pending })
  });
}

// Reset all quantities
export async function resetAllItems(
  supplier: string,
  session_id: string
): Promise<BulkActionResult> {
  return fetchJSON<BulkActionResult>(`${API_BASE}/suppliers/${supplier}/receiving/sessions/${session_id}/reset-all`, {
    method: "POST"
  });
}
