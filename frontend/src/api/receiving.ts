import { API_BASE } from "../lib/api"; // uisti sa, že toto je správna cesta u teba

export async function createReceivingSession(supplier: string, invoice_id: string) {
  const res = await fetch(`${API_BASE}/suppliers/${supplier}/receiving/sessions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ invoice_id })
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function scanCode(supplier: string, session_id: string, code: string, qty = 1) {
  const res = await fetch(`${API_BASE}/suppliers/${supplier}/receiving/sessions/${session_id}/scan`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code, qty })
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function getReceivingSummary(supplier: string, session_id: string) {
  const res = await fetch(`${API_BASE}/suppliers/${supplier}/receiving/sessions/${session_id}/summary`);
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function finalizeReceiving(supplier: string, session_id: string) {
  const res = await fetch(`${API_BASE}/suppliers/${supplier}/receiving/sessions/${session_id}/finalize`, {
    method: "POST"
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}
