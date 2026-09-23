import { hubRequest } from './access';

export const STOCK_SETTING_FIELDS = [
  { key: 'poll_interval_seconds', min: 60, max: 86400, advanced: false },
  { key: 'reconcile_interval_hours', min: 1, max: 168, advanced: false },
  { key: 'full_order_check_hours', min: 1, max: 168, advanced: false },
  { key: 'overlap_minutes', min: 5, max: 1440, advanced: true },
  { key: 'reconcile_window_days', min: 1, max: 30, advanced: true },
  { key: 'max_pages_per_pass', min: 1, max: 100, advanced: true },
  { key: 'run_timeout_seconds', min: 30, max: 180, advanced: true },
  { key: 'retry_base_seconds', min: 60, max: 3600, advanced: true },
  { key: 'retry_max_seconds', min: 300, max: 86400, advanced: true },
  { key: 'processing_batch_size', min: 1, max: 100, advanced: true },
  { key: 'processing_retry_minutes', min: 1, max: 1440, advanced: true },
  { key: 'publication_batch_size', min: 1, max: 100, advanced: true },
  { key: 'publication_preview_minutes', min: 5, max: 60, advanced: true },
] as const;
export type StockSettingKey = typeof STOCK_SETTING_FIELDS[number]['key'];
export type StockSettingValues = Record<StockSettingKey, number>;
export type ProcessingMode = 'manual' | 'reserve' | 'fulfill';
export interface WarehouseSettings {
  warehouse_code: string; revision: number; values: StockSettingValues; processing_paused: boolean;
}
export interface EffectiveStockSettings {
  values: StockSettingValues; sources: Record<StockSettingKey, 'warehouse' | 'shop'>; configuration_hash: string;
  mode: ProcessingMode; processing_paused: boolean; automation_starts_at: string | null; issue_starts_at: string | null;
  retry_after_at?: string | null;
  processing_ready?: boolean; processing_error?: string | null;
}
export interface StockSettingsOptions {
  shop: { code: string; name: string }; warehouses: { id: number; code: string; name: string }[];
  policy: { warehouse_id: number; warehouse_code: string; starts_at: string; revision: number } | null;
  warehouse: WarehouseSettings | null;
  shop_settings: { revision: number; overrides: Partial<StockSettingValues>; mode: ProcessingMode;
    automation_starts_at: string | null; issue_starts_at: string | null; authorized_policy_revision: number | null; target_fingerprint: string | null } | null;
  effective: EffectiveStockSettings | null;
}
const base = '/api/stock-settings';
export const getStockSettingsOptions = (shop: string, signal?: AbortSignal) => hubRequest<StockSettingsOptions>(`${base}/options?shop_code=${encodeURIComponent(shop)}`, undefined, signal);
export const getWarehouseSettings = (warehouse: string, signal?: AbortSignal) => hubRequest<WarehouseSettings>(`${base}/warehouse?warehouse_code=${encodeURIComponent(warehouse)}`, undefined, signal);
export const saveWarehouseSettings = (body: { warehouse_code: string; expected_revision: number; values: StockSettingValues; processing_paused: boolean; confirmed: true }) => hubRequest<WarehouseSettings>(`${base}/warehouse`, body);
export const saveShopStockSettings = (body: { shop_code: string; expected_revision: number; expected_warehouse_revision: number;
  overrides: Partial<StockSettingValues>; mode: ProcessingMode; confirmed: true; fulfillment_confirmed: boolean }) => hubRequest<StockSettingsOptions>(`${base}/shop`, body);
