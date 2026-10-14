/** Only the Hub's same-origin API may receive the shared credential. */
export function hubApiUrl(input: RequestInfo | URL, base = '/api'): URL | null {
  if (typeof window === 'undefined') return null;
  try {
    const api = new URL(base, window.location.href);
    const raw = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
    const url = new URL(raw, window.location.href);
    const prefix = api.pathname.replace(/\/$/, '');
    return api.origin === window.location.origin && url.origin === api.origin &&
      !url.username && !url.password && (url.pathname === prefix || url.pathname.startsWith(`${prefix}/`))
      ? url : null;
  } catch { return null; }
}

export function hubFetchInit(input: RequestInfo | URL, init?: RequestInit, token = '', base = '/api'): RequestInit | undefined {
  if (!hubApiUrl(input, base)) return init;
  const headers = new Headers(init?.headers ?? (typeof input === 'object' && 'headers' in input ? input.headers : undefined));
  headers.set('X-Hub-Request', '1');
  if (token && !headers.has('Authorization')) headers.set('Authorization', `Bearer ${token}`);
  return { ...init, headers, credentials: 'same-origin', cache: 'no-store', redirect: 'error' };
}
