import type { ProductEditorChange, ProductEditorDetail, ProductEditorSaveBody, ProductEditorSnapshot } from '../../api/productEditor';
import { hubUser } from '../../api/access';

/** Navigation and image URLs must never become executable browser URLs. */
export function stockCardSafeUrl(value: string | null | undefined): string | null {
  if (!value || /[\s\\\u0000-\u001f\u007f]/.test(value)) return null;
  try {
    const url = new URL(value);
    return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password ? url.href : null;
  } catch { return null; }
}

export type StockCardEditorKind = 'product' | 'prices' | 'links';
export interface StockCardDraft {
  kind: StockCardEditorKind;
  base: ProductEditorDetail;
  values: Record<string, string>;
  shop?: string;
}

export function stockCardDraft(row: ProductEditorDetail, kind: StockCardEditorKind, shop?: string): StockCardDraft {
  const linked = row.shops.find(value => value.shop_code === shop);
  const values = kind === 'product' ? {
    name: row.common.name, brand: row.common.brand || '', eans: row.eans.join('\n'),
    description: row.common.description || '', restore_description: 'false', image_url: row.image_url || '', internal_note: row.common.internal_note || '',
    location: row.warehouse?.location || '', min_quantity: row.warehouse?.min_quantity || '',
    item_type: row.item_type, stock_unit: row.stock_unit, is_active: String(row.is_active),
  } : kind === 'prices' ? {
    sale_price_gross: row.variant.sale_price_gross || '', vat_rate: row.variant.vat_rate || '',
    ...Object.fromEntries(row.shops.flatMap(value => [
      [`${value.shop_code}_price`, value.overrides.sale_price_gross || ''],
      [`${value.shop_code}_inherit`, String(value.overrides.sale_price_gross == null)],
    ])),
  } : { shop_url: linked?.shop_url || '', shop_admin_url: linked?.shop_admin_url || '', restore_links: 'false' };
  return { kind, base: row, values: values as Record<string, string>, shop };
}

export function stockCardDraftChanged(draft: StockCardDraft | null): boolean {
  return !!draft && JSON.stringify(draft.values) !== JSON.stringify(stockCardDraft(draft.base, draft.kind, draft.shop).values);
}

/** Send only edited fields. A stock quantity is deliberately not a metadata field. */
export function stockCardChange(draft: StockCardDraft): ProductEditorChange {
  const { base, kind, values } = draft;
  const original = stockCardDraft(base, kind, draft.shop).values;
  const changed = (key: string) => values[key] !== original[key];
  const nullable = (key: string) => values[key].trim() || null;
  const change: ProductEditorChange = { product_id: base.id, expected_revision: base.revision, snapshot_hash: base.snapshot_hash };
  if (kind === 'product') {
    for (const key of ['name', 'brand', 'description', 'image_url', 'internal_note'] as const) if (changed(key)) {
      change.common = { ...change.common, [key]: key === 'description' ? values[key] : nullable(key) };
    }
    if (values.restore_description === 'true') change.common = { ...change.common, description: null };
    if (changed('eans')) change.variant = { eans: [...new Set(values.eans.split(/[\s,;]+/).filter(Boolean))] };
    for (const key of ['location', 'min_quantity'] as const) if (changed(key)) {
      const value = nullable(key);
      change.warehouse = { ...change.warehouse, [key]: key === 'min_quantity' ? value?.replace(',', '.') ?? null : value };
    }
    if (changed('item_type')) change.item_type = values.item_type as ProductEditorChange['item_type'];
    if (changed('stock_unit')) change.stock_unit = values.stock_unit as ProductEditorChange['stock_unit'];
    if (changed('is_active')) change.is_active = values.is_active === 'true';
  } else if (kind === 'prices') {
    for (const key of ['sale_price_gross', 'vat_rate'] as const) if (changed(key)) change.variant = { ...change.variant, [key]: nullable(key)?.replace(',', '.') ?? null };
    for (const shop of base.shops) {
      if (changed(`${shop.shop_code}_price`) || changed(`${shop.shop_code}_inherit`)) {
        change.shops = { ...change.shops, [shop.shop_code]: { sale_price_gross: values[`${shop.shop_code}_inherit`] === 'true' ? null : nullable(`${shop.shop_code}_price`)?.replace(',', '.') ?? null } };
      }
    }
  } else if (draft.shop) {
    const links: NonNullable<ProductEditorChange['shops']>[string] = {};
    for (const key of ['shop_url', 'shop_admin_url'] as const) {
      if (values.restore_links === 'true') links[key] = null;
      else if (changed(key)) links[key] = nullable(key);
    }
    if (Object.keys(links).length) change.shops = { [draft.shop]: links };
  }
  return change;
}

export function stockCardSaveKey(productId: number, warehouse: string): string {
  const actor = String(hubUser()?.id ?? hubUser()?.username ?? 'operator-token');
  return `stock-card-save:${encodeURIComponent(actor)}:${productId}:${encodeURIComponent(warehouse)}`;
}

/** Source descriptions can contain HTML; the overview remains inert plain text. */
export function stockCardDescription(value: string | null | undefined): string {
  if (!value) return '';
  const entities: Record<string, string> = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ' };
  return value.replace(/<(script|style|noscript)\b[^>]*>[\s\S]*?<\/\1\s*>/gi, '')
    .replace(/<br\s*\/?\s*>|<\/(?:p|div|li|h[1-6])\s*>/gi, '\n').replace(/<[^>]*>/g, '')
    .replace(/&(?:#(\d+)|#x([\da-f]+)|([a-z]+));/gi, (match, decimal, hex, name) => {
      if (name) return entities[String(name).toLowerCase()] ?? match;
      const point = Number.parseInt(decimal || hex, hex ? 16 : 10);
      return Number.isSafeInteger(point) && point > 0 && point <= 0x10ffff ? String.fromCodePoint(point) : match;
    }).replace(/\n{3,}/g, '\n\n').trim();
}

export function stockCardPending(key: string, productId: number, warehouse: string): ProductEditorSaveBody | null {
  try {
    const value = JSON.parse(sessionStorage.getItem(key) || 'null');
    return value && /^[0-9a-f-]{36}$/i.test(value.request_id) && value.confirmed === true &&
      value.warehouse_code === (warehouse || null) && Array.isArray(value.changes) && value.changes.length === 1 &&
      value.changes[0].product_id === productId && Number.isSafeInteger(value.changes[0].expected_revision) &&
      typeof value.changes[0].snapshot_hash === 'string' ? value : null;
  } catch { return null; }
}

export function stockCardAuditChanges(before: ProductEditorSnapshot, after: ProductEditorSnapshot): { field: string; before: unknown; after: unknown }[] {
  const flatten = (snapshot: ProductEditorSnapshot): Record<string, unknown> => ({
    ...(snapshot.common || {}), ...(snapshot.variant || {}), ...(snapshot.warehouse || {}),
    ...Object.fromEntries(['sku', 'is_active', 'item_type', 'stock_unit', 'eans', 'attributes'].filter(key => key in snapshot).map(key => [key, snapshot[key as keyof ProductEditorSnapshot]])),
    ...Object.fromEntries((snapshot.shops || []).flatMap(shop => Object.entries(shop.overrides || {}).map(([key, value]) => [`${shop.shop_code}.${key}`, value]))),
  });
  const a = flatten(before), b = flatten(after);
  return [...new Set([...Object.keys(a), ...Object.keys(b)])].filter(key => JSON.stringify(a[key]) !== JSON.stringify(b[key]))
    .map(field => ({ field, before: a[field], after: b[field] }));
}
