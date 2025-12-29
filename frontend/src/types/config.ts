// Canonical SupplierConfig types used by SupplierConfigForm and APIs

export type AuthMode = "none" | "form" | "cookie" | "basic" | "token" | "header";

export type AuthConfig = {
  mode: AuthMode;
  login_url?: string;
  user_field?: string;
  pass_field?: string;
  username?: string;
  password?: string;
  cookie?: string;
  basic_user?: string;
  basic_pass?: string;
  token?: string;
  header_name?: string;8
  insecure_all?: boolean;
};

export type RemoteEndpoint = {
  url: string;
  method: "GET" | "POST";
  headers: Record<string, string>;
  params: Record<string, string>;
  auth: AuthConfig;
};

export type FeedSource = {
  mode: "remote" | "local";
  local_path: string | null;
  remote: RemoteEndpoint;
};

export type FeedsConfig = {
  current_key: "products" | "stock" | "prices";
  sources: {
    [key: string]: FeedSource;
  };
};

export type InvoicesWebLogin = {
  login: AuthConfig;
  base_url?: string;
  notes?: string;
};

export type InvoicesDownload = {
  strategy: "manual" | "paul-lange-web" | "api" | "email";
  web?: InvoicesWebLogin;
};

export type InvoicesConfig = {
  layout: "flat" | "by_number_date";
  months_back_default: number;
  download: InvoicesDownload;
};

export type SupplierConfig = {
  feeds: FeedsConfig;
  invoices: InvoicesConfig;
  adapter_settings?: Record<string, unknown>;
};

// --- doplnené typy pre importy v iných častiach FE ---

// Alias, aby sme mali samostatný názov pre mód feedu
export type FeedMode = "remote" | "local";

// Minimal ConsoleConfig (môžeme časom spresniť)
export type ConsoleConfig = Record<string, unknown>;

// ShopConfig s pár známymi kľúčmi + fallback pre ďalšie
export type ShopConfig = {
  upgates_full_export_url_csv?: string;
  verify_ssl?: boolean;
  ca_bundle_path?: string | null;
  [key: string]: unknown;
};

// Effective config shape pre /suppliers/{supplier}/effective
export type EffectiveConfig = {
  using_feed: {
    key: "products" | "stock" | "prices" | null;
    mode?: "remote" | "local" | null;
    url?: string;
    path?: string;
  };
};