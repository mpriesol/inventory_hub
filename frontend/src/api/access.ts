import { CatalogApiError } from './catalog';

// Operator credentials stay in this tab's memory, never in storage or URLs.
let accessToken = '';
let revision = 0;
const listeners = new Set<() => void>();

export function unlockHub(value: string) {
  if (value === accessToken) return;
  accessToken = value;
  revision += 1;
  listeners.forEach(listener => listener());
}
export function hubUnlocked() { return !!accessToken; }
export function accessRevision() { return revision; }
export function subscribeAccess(listener: () => void) {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

export async function hubRequest<T>(path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, {
    method: body === undefined ? 'GET' : 'POST',
    headers: { Authorization: `Bearer ${accessToken}`, ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: 'no-store',
    signal,
  });
  const data = await response.json();
  if (!response.ok) throw new CatalogApiError(data?.detail?.code || 'request_failed', typeof data?.detail?.message === 'string' ? data.detail.message : JSON.stringify(data?.detail || response.status));
  return data;
}
