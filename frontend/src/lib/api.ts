export const API_BASE = (import.meta.env.VITE_API_BASE || "/api").replace(/\/$/, "");

async function handle<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`${res.status} ${res.statusText}${text ? ` — ${text}` : ""}`);
  }
  return res.json() as Promise<T>;
}

export const api = {
  get: <T>(path: string, init?: RequestInit) => handle<T>(fetch(`${API_BASE}${path}`, { ...init, method: "GET" })),
  put: <T>(path: string, body: unknown, init?: RequestInit) =>
    handle<T>(
      fetch(`${API_BASE}${path}`, {
        ...init,
        method: "PUT",
        headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
        body: JSON.stringify(body),
      })
    ),
};
