// src/features/configs/supplierShape.ts
import type { SupplierConfig, AuthConfig, FeedMode } from "../../types/config";

/** Plochý tvar pre modal (jednoduché polia) */
export type SupplierForm = {
  /** ktorý feed práve edituje modal (key v feeds.sources) */
  feed_source: string; // "products" | "stock" | "prices" | custom
  /** URL alebo lokálna cesta – UI to má iba v jednom inpute */
  feed_url: string;
  /** stratégie sťahovania faktúr */
  invoice_download_strategy: string; // "manual" | "paul-lange-web" | "api" | "email" | ...
  /** koľko mesiacov dozadu */
  default_months_window: number;
  /** login údaje – použijú sa pre invoices, ak je PL-web; inak pre feed auth */
  auth: AuthConfig;
};

const DEFAULT_AUTH: AuthConfig = {
  mode: "none",
  login_url: "",
  user_field: "login",
  pass_field: "password",
  username: "",
  password: "",
  cookie: "",
  basic_user: "",
  basic_pass: "",
  insecure_all: false,
};

/** heuristika: rozliš, či je to lokálna cesta alebo URL */
function isLocalPath(s: string): boolean {
  const v = (s || "").trim();
  if (!v) return false;
  if (/^[a-zA-Z]:[\\/]/.test(v)) return true; // C:\... alebo D:\...
  if (v.startsWith("\\\\") || v.startsWith("/")) return true; // UNC / unix path
  if (/^https?:\/\//i.test(v)) return false;
  if (/^[a-z]+:\/\//i.test(v)) return false; // iné schémy
  // ak to vyzerá ako súbor bez schémy a obsahuje backslash, berme ako lokálne
  if (v.includes("\\") && !v.includes("://")) return true;
  return false;
}

/** obranné zlúčenie auth – neprepisuj prázdnymi hodnotami citlivé polia */
function mergeAuthKeepSecrets(prev: AuthConfig, next: Partial<AuthConfig>): AuthConfig {
  const out: AuthConfig = { ...DEFAULT_AUTH, ...prev, ...next };
  const keepIfEmpty: (keyof AuthConfig)[] = ["password", "basic_pass", "cookie", "token" as any];
  for (const k of keepIfEmpty) {
    const val = (next as any)[k];
    if (val === "" || val === null || val === undefined) {
      (out as any)[k] = (prev as any)[k] ?? (out as any)[k];
    }
  }
  return out;
}

/** vezmi AuthConfig z invoices.download.web.login ak stratégia je PL-web, inak z feed auth */
function pickFormAuth(raw: SupplierConfig, feedKey: string): AuthConfig {
  const strat = raw?.invoices?.download?.strategy ?? "manual";
  if (String(strat).toLowerCase().replace(/_/g, "-") === "paul-lange-web") {
    return { ...DEFAULT_AUTH, ...(raw?.invoices?.download?.web?.login ?? {}) };
  }
  const feed = raw?.feeds?.sources?.[feedKey];
  return { ...DEFAULT_AUTH, ...(feed?.remote?.auth ?? {}) };
}

/** získať aktuálny feed key + zdroj (fallback na "products") */
function currentFeedKey(raw: SupplierConfig): string {
  const k = raw?.feeds?.current_key ?? "products";
  if (raw?.feeds?.sources && raw.feeds.sources[k]) return k;
  // fallback ak current_key ukazuje na neexistujúci kľúč
  const keys = Object.keys(raw?.feeds?.sources ?? {});
  return keys[0] || "products";
}

/** pre UI: urob plochý formulár zo surového configu */
export function toForm(raw: SupplierConfig): SupplierForm {
  const key = currentFeedKey(raw);
  const source = raw?.feeds?.sources?.[key];
  const feedMode: FeedMode = source?.mode ?? "remote";

  const feed_url =
    feedMode === "local"
      ? (source?.local_path ?? "")
      : (source?.remote?.url ?? "");

  return {
    feed_source: key,
    feed_url,
    invoice_download_strategy: raw?.invoices?.download?.strategy ?? "manual",
    default_months_window: raw?.invoices?.months_back_default ?? 3,
    auth: pickFormAuth(raw, key),
  };
}

/**
 * z plochého formulára urob PATCH pre backend (nový vnorený tvar),
 * s ohľadom na zachovanie tajomstiev. Pre merge treba aj predchádzajúci raw.
 */
export function fromForm(form: SupplierForm, prev: SupplierConfig): Partial<SupplierConfig> {
  const key = form.feed_source || currentFeedKey(prev);

  // ensure target feed exists
  const prevFeed = prev?.feeds?.sources?.[key];
  const prevAuthFeed: AuthConfig = { ...DEFAULT_AUTH, ...(prevFeed?.remote?.auth ?? {}) };
  const prevAuthWeb: AuthConfig = { ...DEFAULT_AUTH, ...(prev?.invoices?.download?.web?.login ?? {}) };

  // decide where auth belongs
  const isPL = String(form.invoice_download_strategy).toLowerCase().replace(/_/g, "-") === "paul-lange-web";
  const mergedWebAuth = mergeAuthKeepSecrets(prevAuthWeb, form.auth);
  const mergedFeedAuth = mergeAuthKeepSecrets(prevAuthFeed, form.auth);

  // feed url mapping
  const local = isLocalPath(form.feed_url);
  const feedPatchForKey = local
    ? {
        mode: "local" as FeedMode,
        local_path: form.feed_url,
        remote: {
          ...(prevFeed?.remote ?? { url: "", method: "GET", headers: {}, params: {}, auth: DEFAULT_AUTH }),
          // pri local nemeníme remote.url, len držíme predchádzajúci
        },
      }
    : {
        mode: "remote" as FeedMode,
        local_path: null,
        remote: {
          ...(prevFeed?.remote ?? { url: "", method: "GET", headers: {}, params: {}, auth: DEFAULT_AUTH }),
          url: form.feed_url,
          // auth sa nastaví nižšie podľa stratégie
        },
      };

  // zostav finálny patch
  const patch: Partial<SupplierConfig> = {
    feeds: {
      current_key: key as any,
      sources: {
        [key]: feedPatchForKey as any,
      },
    },
    invoices: {
      ...prev.invoices,
      months_back_default: form.default_months_window,
      download: {
        ...(prev.invoices?.download ?? { strategy: "manual" }),
        strategy: form.invoice_download_strategy as any,
      } as any,
    },
  };

  // priraď auth na správne miesto
  if (isPL) {
    // auth patrí faktúram (web login)
    (patch.invoices as any).download = {
      ...(patch.invoices as any).download,
      web: {
        ...(prev.invoices?.download?.web ?? { base_url: "", notes: "" }),
        login: mergedWebAuth,
      },
    };
    // feed auth necháme pôvodné (ak existovalo)
    if (!local) {
      // ak je remote režim a predtým bolo feed auth, zachováme ho
      const prevAuth = prevFeed?.remote?.auth;
      if (prevAuth) {
        (patch.feeds as any).sources[key].remote.auth = prevAuth;
      }
    }
  } else {
    // auth patrí k feedu
    (patch.feeds as any).sources[key].remote.auth = mergedFeedAuth;
    // invoices web login ponechaj bez zmeny
    if (prev?.invoices?.download?.web?.login) {
      (patch.invoices as any).download = {
        ...(patch.invoices as any).download,
        web: {
          ...(prev.invoices?.download?.web ?? { base_url: "", notes: "" }),
          login: prev.invoices.download.web.login,
        },
      };
    }
  }

  return patch;
}
