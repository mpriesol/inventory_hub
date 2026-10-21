import { hubRequest } from './access';
import { AiJob } from './aiContent';
import { ImportItem, ImportOptions, ImportPreview, ImportResult, TargetOptions } from './catalog';
import { SkuIdentityInput, SkuPreview } from './skuPolicy';

export interface ProductImportSelection { supplier: string; feed_key: string; run_id: number | null; product_ids: number[]; shop: string; options: ImportOptions }
export interface ImportValues {
  sku_identity?: SkuIdentityInput | null;
  code: string; supplier_code: string; group_name: string; name: string; brand: string; manufacturer_code: string;
  eans: string[]; images: string[]; description_html: string; short_description: string;
  seo_title: string; seo_description: string; seo_url: string; category_code: string | null;
  parameters: { name: string; value: string }[]; variant_attributes: { name: string; value: string }[];
  metadata: Record<string, string>; purchase_net: string | null; retail_gross: string | null;
  sale_gross: string | null; vat_percent: string | null; currency: string; availability: string; ai_enabled: boolean; ai_category_profile: string; target_shops: string[]; target_category_codes: Record<string, string | null>;
}
export type ImportField = keyof ImportValues;
export type ImportValue = ImportValues[ImportField];
export type ImportProvenance = 'feed' | 'invoice' | 'mapping' | 'manual' | 'ai' | 'derived' | 'supplier';
export interface ProductImportLink { shop: string; shop_url: string | null; shop_admin_url: string | null; status: string }
export interface ProductImportItem extends ImportItem { shop?: string; shop_url?: string | null; shop_admin_url?: string | null }
export interface ProductImportPreview extends Omit<ImportPreview, 'items'> { items: ProductImportItem[] }
export interface ProductImportResult extends Omit<ImportResult, 'items'> { items: ProductImportItem[] }
export interface ProductImportRow {
  sku_preview?: SkuPreview | null; hub_errors?: string[];
  source_url?: string | null;
  id: number; group_key: string; group_name?: string; is_variant: boolean; source_category: string | null;
  mapping_revision: number | null; mapping_provenance?: Record<string, unknown>; values: ImportValues;
  manual_fields: string[]; provenance: Partial<Record<ImportField, ImportProvenance>>;
  publication_status?: string | null; publication_links?: ProductImportLink[]; warnings: string[]; errors: string[]; hub_product_id: number | null; ai_job: AiJob | null;
  expected_hub_product_id?: number | null;
  identity_decision?: { action: 'link' | 'create'; product_id?: number } | null;
  categories?: { code: string; main_yn: boolean }[];
  source_status?: 'pending' | 'completed' | 'failed' | 'skipped'; source_error?: string | null;
}
export interface ProductImportDraft {
  supplier_id?: number;
  id: string; revision: number; status: string; supplier: string; shop: string; feed_key: string;
  created_at?: string; updated_at?: string; options: ImportOptions; categories: TargetOptions['categories'];
  default_shop?: string; shops?: { code: string; name: string; categories: TargetOptions['categories']; error?: string }[];
  publications?: Record<string, { preview: ImportPreview; result: ImportResult | null; state: string | null }>;
  publication_state?: string | null; rows: ProductImportRow[]; publication: ProductImportPreview | null; publication_result: ProductImportResult | null;
  publication_preview_retryable?: boolean;
  receiving_origin?: { kind: 'receiving'; supplier: string; session_id: string | number; invoice_no: string } | null;
  source_lookup?: { state: 'queued' | 'running' | 'failed' | 'completed' | 'skipped'; total: number; completed: number; failed: number; skipped: number; error: string | null; retryable: boolean; skippable: boolean } | null;
  identity_review_required?: boolean; identity_review_count?: number;
}
export interface ImportIdentityDecision { row_id: number; action: 'link' | 'create'; product_id?: number }
export interface ImportIdentityCandidate {
  product_id: number; sku: string; name: string; brand: string | null; eans: string[];
  attributes: { name: string; value: string }[]; is_active: boolean; reasons: string[]; warnings: string[];
  can_link: boolean; errors: string[];
}
export interface ImportIdentityReview {
  draft_id: string; revision: number; review_hash: string;
  rows: { row_id: number; source: { sku: string; supplier_sku: string; name: string; eans: string[]; attributes: { name: string; value: string }[]; brand: string | null };
    status: 'new' | 'review' | 'linked' | 'conflict' | 'saved'; matched_product_id: number | null;
    can_create: boolean; candidates: ImportIdentityCandidate[]; truncated: boolean;
    decision: Omit<ImportIdentityDecision, 'row_id'> | null; errors: string[] }[];
}
export interface ProductImportSummary { needs_attention?: boolean; title?: string; codes?: string[]; id: string; revision: number; status: string; publication_state?: string | null; supplier: string; shop: string; updated_at: string; rows_count: number }
export interface ImportRowPatch { id: number; values: Partial<ImportValues> }
const base = '/api/product-imports';
export const listProductImports = (signal?: AbortSignal, q = '') => hubRequest<{ items: ProductImportSummary[] }>(`${base}${q ? `?q=${encodeURIComponent(q)}` : ''}`, undefined, signal);
export const createProductImport = (selection: ProductImportSelection, request_id: string) => hubRequest<ProductImportDraft>(base, { ...selection, request_id });
export const readProductImport = (id: string, signal?: AbortSignal) => hubRequest<ProductImportDraft>(`${base}/${encodeURIComponent(id)}`, undefined, signal);
export const refreshProductImportCategories = (draft: ProductImportDraft) => hubRequest<ProductImportDraft>(`${base}/${draft.id}/categories-refresh`, { expected_revision: draft.revision });
export const retryProductImportSource = (draft: ProductImportDraft) => hubRequest<ProductImportDraft>(`${base}/${draft.id}/source-retry`, { expected_revision: draft.revision });
export const skipProductImportSource = (draft: ProductImportDraft) => hubRequest<ProductImportDraft>(`${base}/${draft.id}/source-skip`, { expected_revision: draft.revision });
export const editProductImport = (draft: ProductImportDraft, rows: ImportRowPatch[]) => hubRequest<ProductImportDraft>(`${base}/${draft.id}`, { expected_revision: draft.revision, rows }, undefined, 'PUT');
export const saveProductImport = (draft: ProductImportDraft) => hubRequest<ProductImportDraft>(`${base}/${draft.id}/save`, { expected_revision: draft.revision });
export const previewProductImport = (draft: ProductImportDraft) => hubRequest<ProductImportDraft>(`${base}/${draft.id}/publish-preview`, { expected_revision: draft.revision });
export const publishProductImport = (draft: ProductImportDraft, retry_failed = false) => hubRequest<ProductImportDraft>(`${base}/${draft.id}/publish`, { expected_revision: draft.revision, preview_id: draft.publication?.preview_id, retry_failed });
export const prepareProductImportAi = (draft: ProductImportDraft) => hubRequest<ProductImportDraft>(`${base}/${draft.id}/ai`, { expected_revision: draft.revision, research: 'official' });
export const startProductImportAi = (draft: ProductImportDraft) => hubRequest<ProductImportDraft>(`${base}/${draft.id}/ai-start`, { expected_revision: draft.revision });
export const applyProductImportAi = (draft: ProductImportDraft) => hubRequest<ProductImportDraft>(`${base}/${draft.id}/ai-apply`, { expected_revision: draft.revision });
export const reviewProductImportIdentity = (draft: ProductImportDraft) => hubRequest<ImportIdentityReview>(`${base}/${draft.id}/identity-review`, { expected_revision: draft.revision });
export const decideProductImportIdentity = (draft: ProductImportDraft, review_hash: string, decisions: ImportIdentityDecision[]) =>
  hubRequest<ProductImportDraft>(`${base}/${draft.id}/identity-decisions`, { expected_revision: draft.revision, review_hash, decisions });
