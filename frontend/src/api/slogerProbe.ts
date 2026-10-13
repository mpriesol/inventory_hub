import { API_BASE } from './client';
import { hubRequest } from './access';

type PriceBasis = 'net' | 'gross' | 'unknown';
export interface SlogerQuantity {
  quantity: string | null;
  quantity_kind: 'exact' | 'minimum' | 'unknown';
  quantity_relation: 'equal' | 'greater_than' | 'at_least' | 'unknown';
}
export interface SlogerObservation extends SlogerQuantity {
  sku: string;
  product_name: string | null;
  manufacturer_code: string | null;
  eans: string[];
  purchase_price: string | null;
  retail_price: string | null;
  currency: string | null;
  price_basis: PriceBasis;
  retail_price_basis: PriceBasis;
  availability: string | null;
  available: boolean | null;
  availability_tiers: (SlogerQuantity & { label: string; lead_days: number | null })[];
  source_url: string;
  observed_at: string;
}
export interface SlogerProbe {
  id: string;
  status: 'lookup_running' | 'lookup_completed' | 'lookup_failed' | 'lookup_uncertain';
  model: string;
  result: SlogerObservation | null;
  usage: { input_tokens?: number; output_tokens?: number };
  actual_usd: string | null;
  error: string | null;
  diagnostic?: {
    stage: 'login_page' | 'login_submit' | 'search' | 'product';
    reason: 'http_status' | 'network' | 'redirect' | 'content_type' | 'challenge' | 'size' | 'deadline' | 'url' | 'structure' | 'authentication';
    http_status: number | null;
    method?: 'GET' | 'POST';
    path_class?: 'login' | 'shop_return' | 'b2b' | 'search' | 'product' | 'other';
  } | null;
}
export interface SlogerCredentials {
  configured: boolean;
  username: string | null;
}
export const readSlogerCredentials = () =>
  hubRequest<SlogerCredentials>(`${API_BASE}/suppliers/sloger/lookup/credentials`);
export const saveSlogerCredentials = (body: { username: string; password: string }) =>
  hubRequest<SlogerCredentials>(`${API_BASE}/suppliers/sloger/lookup/credentials`, body, undefined, 'PUT');
export const readSlogerProbe = (id: string) =>
  hubRequest<SlogerProbe>(`${API_BASE}/suppliers/sloger/lookup/${encodeURIComponent(id)}`);
