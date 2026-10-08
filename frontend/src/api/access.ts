import { CatalogApiError } from './catalog';

// Operator credentials stay in this tab's memory, never in storage or URLs.
export interface HubUser { id: number | null; username: string; display_name: string; role: 'admin' | 'operator'; active: boolean; token_login?: boolean }
let currentUser: HubUser | null = null;
let accessToken = '';
export function hubUser() { return currentUser; }
export function setHubUser(user: HubUser | null) {
  if (!accessToken && ((!user && !currentUser) || (user && currentUser &&
    (['id', 'username', 'display_name', 'role', 'active', 'token_login'] as const).every(key => user[key] === currentUser?.[key])))) return;
  currentUser = user; accessToken = ''; revision += 1; listeners.forEach(listener => listener());
}
export async function restoreSession() {
  const started = revision;
  const result = await hubRequest<{ user: HubUser | null }>('/api/auth/session');
  if (started === revision) setHubUser(result.user);
}
export async function logoutHub() {
  await hubRequest('/api/auth/logout', {});
  setHubUser(null);
}

let revision = 0;
const listeners = new Set<() => void>();

export function unlockHub(value: string) {
  if (!value && currentUser) { void logoutHub().catch(() => window.location.reload()); return; }
  if (value === accessToken) return;
  accessToken = value;
  revision += 1;
  listeners.forEach(listener => listener());
}
export function hubUnlocked() { return !!accessToken || !!currentUser; }
export function accessRevision() { return revision; }
export function subscribeAccess(listener: () => void) {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

export async function hubRequest<T>(path: string, body?: unknown, signal?: AbortSignal, method: 'POST' | 'PUT' = 'POST'): Promise<T> {
  const response = await fetch(path, {
    method: body === undefined ? 'GET' : method,
    credentials: 'same-origin',
    headers: { 'X-Hub-Request': '1', ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}), ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: 'no-store',
    signal,
  });
  const data = await response.json();
  if (response.status === 401 && currentUser) setHubUser(null);
  if (!response.ok) throw new CatalogApiError(data?.detail?.code || 'request_failed', typeof data?.detail?.message === 'string' ? data.detail.message : JSON.stringify(data?.detail || response.status));
  return data;
}

/** Multipart requests share the same in-memory credential and session protection. */
export async function hubUpload<T>(path: string, body: FormData, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, {
    method: 'POST', credentials: 'same-origin',
    headers: { 'X-Hub-Request': '1', ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}) },
    body, signal, cache: 'no-store',
  });
  let data: any;
  try { data = await response.json(); } catch { throw new CatalogApiError('request_failed', String(response.status)); }
  if (response.status === 401 && currentUser) setHubUser(null);
  if (!response.ok) throw new CatalogApiError(data?.detail?.code || 'request_failed', typeof data?.detail?.message === 'string' ? data.detail.message : JSON.stringify(data?.detail || response.status));
  return data;
}
