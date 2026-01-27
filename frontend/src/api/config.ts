
import { API_BASE, fetchJSON } from "./client";
import type { ConsoleConfig, ShopConfig, SupplierConfig, EffectiveConfig } from "../types/config";
import type {
  FeedSource,
  AuthConfig,
  AuthMode,
  InvoicesDownload,
  InvoicesWebLogin,
  RemoteEndpoint,
} from "../types/config";

// ----- DEFAULTS -----
const DEFAULT_CONSOLE: ConsoleConfig = {
  language: "en",
  default_currency: "EUR",
  currency_rates: { CZK: { EUR: 0 } },
  default_months_window: 3,
};



function normalizeAuth(raw: any, fallbackMode: AuthMode = "none"): AuthConfig {
  return {
    mode: (raw?.mode as AuthMode) ?? fallbackMode,
    login_url: raw?.login_url ?? "",
    user_field: raw?.user_field ?? "login",
    pass_field: raw?.pass_field ?? "password",
    username: raw?.username ?? "",
    password: raw?.password ?? "",
    cookie: raw?.cookie ?? "",
    basic_user: raw?.basic_user ?? "",
    basic_pass: raw?.basic_pass ?? "",
    token: raw?.token ?? "",
    header_name: raw?.header_name ?? "",
    insecure_all: !!raw?.insecure_all,
  };
}

function normalizeRemoteEndpoint(raw: any, authMode: AuthMode = "none"): RemoteEndpoint {
  const auth = normalizeAuth(raw?.auth, authMode);
  const method = (raw?.method === "POST" ? "POST" : "GET") as "GET" | "POST";
  return {
    url: raw?.url ?? "",
    method,
    headers: (raw?.headers && typeof raw.headers === "object") ? raw.headers : {},
    params: (raw?.params && typeof raw.params === "object") ? raw.params : {},
    auth,
  };
}

function normalizeFeedSource(raw: any): FeedSource {
  // Podpor oba vstupy:
  // 1) kanonika: { mode, local_path, remote: {...} }
  // 2) legacy mix: { mode, current_path, remote: {...} }
  const mode = (raw?.mode === "local" ? "local" : "remote") as "remote" | "local";
  const local_path = raw?.local_path ?? raw?.current_path ?? null;
  const remote = normalizeRemoteEndpoint(raw?.remote ?? raw, mode === "remote" ? "none" : "none");
  return { mode, local_path, remote };
}

export function normalizeSupplierConfig(raw: any): SupplierConfig {
  // ak API vracia { config: {...} }, rozbaľ to
  const r = raw?.config ?? raw ?? {};

  // -------- FEEDS -----------------------------------------------------------
  const feedsRaw = r?.feeds ?? {};
  const sourcesRaw = feedsRaw?.sources ?? {};

  // Kanonický zdroj "products" – ak už existuje, použijeme ho; inak zložíme z legacy polí
  const productsSource: FeedSource = sourcesRaw?.products
    ? normalizeFeedSource(sourcesRaw.products)
    : normalizeFeedSource({
        mode: feedsRaw?.mode,                 // legacy top-level
        local_path: feedsRaw?.local_path ?? feedsRaw?.current_path ?? null,
        remote: feedsRaw?.remote ?? {},       // legacy remote endpoint
      });

  // current_key povolené len 'products' | 'stock' | 'prices'
  const allowedKeys = new Set(["products", "stock", "prices"]);
  const currentKey = allowedKeys.has(feedsRaw?.current_key) ? feedsRaw.current_key : "products";

  // -------- INVOICES --------------------------------------------------------
  const invRaw = r?.invoices ?? {};
  const legacyStrategy = invRaw?.download_strategy ?? r?.invoice_download_strategy ?? null;

  // Primárne čítaj kanoniku: invoices.download.strategy
  let strategy: InvoicesDownload["strategy"] =
    invRaw?.download?.strategy ??
    (legacyStrategy as InvoicesDownload["strategy"]) ??
    "manual";

  // Web login (kanonika: invoices.download.web.login), fallback z legacy auth ak treba
  const webLoginRaw = invRaw?.download?.web?.login ?? {};
  const webLogin: InvoicesWebLogin = {
    login: normalizeAuth(
      Object.keys(webLoginRaw).length ? webLoginRaw : (feedsRaw?.remote?.auth ?? {}), // fallback ak nebolo web.login
      strategy === "paul-lange-web" ? "form" : "none"
    ),
    base_url: invRaw?.download?.web?.base_url ?? r?.base_url ?? undefined,
    notes: invRaw?.download?.web?.notes ?? undefined,
  };

  const invoicesDownload: InvoicesDownload = {
    strategy,
    web: (strategy === "paul-lange-web" || strategy === "api" || strategy === "email") ? webLogin : undefined,
  };

  const monthsDefault =
    typeof invRaw?.months_back_default === "number"
      ? invRaw.months_back_default
      : (typeof r?.default_months_window === "number" ? r.default_months_window : 3);

  const layout =
    invRaw?.layout === "by_number_date" ? "by_number_date" : "flat";

  // -------- Výsledok (plná kanonika) ---------------------------------------
  const out: SupplierConfig = {
    feeds: {
      current_key: currentKey as "products" | "stock" | "prices",
      sources: {
        ...sourcesRaw,
        products: productsSource,
      },
    },
    invoices: {
      layout,
      months_back_default: monthsDefault,
      download: invoicesDownload,
    },
    adapter_settings: (r?.adapter_settings && typeof r.adapter_settings === "object")
      ? r.adapter_settings
      : undefined,
  };

  return out;
}


export async function getConsoleConfig(): Promise<ConsoleConfig> {
  const res = await fetchJSON<any>(`${API_BASE}/configs/console`);
  const obj = res?.config ?? res ?? {};
  return {
    ...DEFAULT_CONSOLE,
    ...obj,
    currency_rates: obj.currency_rates ?? DEFAULT_CONSOLE.currency_rates,
  };
}



export async function saveConsoleConfig(cfg: Partial<ConsoleConfig>): Promise<ConsoleConfig> {
  const res = await fetchJSON<any>(`${API_BASE}/configs/console`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(cfg),
  });
  const obj = res?.config ?? res ?? {};
  return {
    ...DEFAULT_CONSOLE,
    ...obj,
    currency_rates: obj.currency_rates ?? DEFAULT_CONSOLE.currency_rates,
  };
}

export async function getShopConfig(shop: string): Promise<ShopConfig> {
  return fetchJSON(`${API_BASE}/shops/${encodeURIComponent(shop)}/config`);
}
export async function putShopConfig(shop: string, payload: ShopConfig): Promise<ShopConfig> {
  return fetchJSON(`${API_BASE}/shops/${encodeURIComponent(shop)}/config`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

// ----- SUPPLIER CONFIG -----
export async function getSupplierConfig(supplier: string): Promise<SupplierConfig> {
  return fetchJSON(`${API_BASE}/suppliers/${encodeURIComponent(supplier)}/config`);
}
export async function putSupplierConfig(supplier: string, payload: SupplierConfig): Promise<SupplierConfig> {
  return fetchJSON(`${API_BASE}/suppliers/${encodeURIComponent(supplier)}/config`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}


export async function saveSupplierConfig(supplier: string, patch: Partial<SupplierConfig>): Promise<SupplierConfig> {
  return fetchJSON<SupplierConfig>(`${API_BASE}/suppliers/${encodeURIComponent(supplier)}/config`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
}


export async function getSupplierEffective(supplier: string) {
  return fetchJSON(`${API_BASE}/suppliers/${encodeURIComponent(supplier)}/effective`);
}




