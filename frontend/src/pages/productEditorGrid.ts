import { EditorMode, ProductEditorChange, ProductEditorRow } from '../api/productEditor';

export type EditorValue = string | boolean | string[] | { name: string; value: string }[] | null;
export interface EditorColumn { key: string; editable?: boolean; type?: 'text' | 'money' | 'vat' | 'integer' | 'visibility' | 'attributes' | 'eans' | 'url'; warehouse?: boolean; modes?: 'common' | 'shop'; hidden?: boolean }
export const EDITOR_COLUMNS: EditorColumn[] = [
  { key: 'sku', editable: true, type: 'text' }, { key: 'image_url', editable: true, type: 'url' },
  { key: 'name', editable: true, type: 'text' }, { key: 'brand', editable: true, type: 'text' },
  { key: 'sale_price_gross', editable: true, type: 'money' },
  { key: 'biketrek' }, { key: 'xtrek' }, { key: 'supplier_quantity' }, { key: 'supplier_availability' },
  { key: 'location', editable: true, type: 'text', warehouse: true },
  { key: 'min_quantity', editable: true, type: 'integer', warehouse: true },
  { key: 'on_hand' }, { key: 'reserved' }, { key: 'available' }, { key: 'cost' }, { key: 'state' },
  { key: 'ean', editable: true, type: 'eans', hidden: true }, { key: 'supplier_codes', hidden: true },
  { key: 'internal_note', editable: true, type: 'text', hidden: true },
  { key: 'vat_rate', editable: true, type: 'vat', hidden: true },
  { key: 'note', editable: true, type: 'text', hidden: true },
  { key: 'color', editable: true, type: 'text' }, { key: 'size', editable: true, type: 'text' },
  { key: 'attributes', editable: true, type: 'attributes', hidden: true },
  { key: 'quarantined', hidden: true }, { key: 'total_value', hidden: true }, { key: 'channels', hidden: true },
  ...(['biketrek', 'xtrek'] as const).flatMap(shop => [
    { key: `${shop}_name`, editable: true, type: 'text' as const, hidden: true },
    { key: `${shop}_price`, editable: true, type: 'money' as const, hidden: true },
    { key: `${shop}_visible`, editable: true, type: 'visibility' as const, hidden: true },
  ]),
];

export const COLUMN_PREFERENCES_KEY = 'product-editor-columns-v1';
export const MIN_COLUMN_WIDTH = 80, MAX_COLUMN_WIDTH = 640;
export interface ColumnPreferences { version: 1; order: string[]; visible: string[]; widths: Record<string, number> }
const defaultWidths: Record<string, number> = { sku: 165, name: 265, shop_name: 265, internal_note: 300, note: 300,
  supplier_quantity: 135, supplier_availability: 200, image_url: 90, biketrek: 220, xtrek: 220, biketrek_name: 265, xtrek_name: 265, state: 175, on_hand: 110, reserved: 110, available: 110, cost: 110, attributes: 280, supplier_codes: 220, ean: 170 };
export const defaultColumnWidth = (key: string) => defaultWidths[key] || 135;
export const clampColumnWidth = (value: number) => Math.max(MIN_COLUMN_WIDTH, Math.min(MAX_COLUMN_WIDTH, Math.round(value)));
export function columnPreferences(value?: unknown): ColumnPreferences {
  const defaults: ColumnPreferences = { version: 1, order: EDITOR_COLUMNS.map(column => column.key),
    visible: EDITOR_COLUMNS.filter(column => !column.hidden).map(column => column.key), widths: {} };
  if (!value || typeof value !== 'object' || (value as ColumnPreferences).version !== 1) return defaults;
  const saved = value as ColumnPreferences, allowed = new Set(defaults.order);
  const keys = (values: unknown) => Array.isArray(values) ? [...new Set(values.filter((key): key is string => typeof key === 'string' && allowed.has(key)))] : [];
  const order = keys(saved.order);
  const widths: Record<string, number> = {};
  for (const key of defaults.order) {
    const width = saved.widths?.[key];
    if (typeof width === 'number' && Number.isFinite(width)) widths[key] = clampColumnWidth(width);
  }
  return { version: 1, order: ['sku', ...order.filter(key => key !== 'sku'), ...defaults.order.filter(key => key !== 'sku' && !order.includes(key))],
    visible: Array.isArray(saved.visible) ? ['sku', ...keys(saved.visible).filter(key => key !== 'sku'), ...defaults.visible.filter(key => key !== 'sku' && !order.includes(key) && !keys(saved.visible).includes(key))] : defaults.visible, widths };
}
export function loadColumnPreferences(): ColumnPreferences {
  try { return columnPreferences(JSON.parse(window.localStorage.getItem(COLUMN_PREFERENCES_KEY) || 'null')); }
  catch { return columnPreferences(); }
}
export function saveColumnPreferences(preferences: ColumnPreferences): boolean {
  try { window.localStorage.setItem(COLUMN_PREFERENCES_KEY, JSON.stringify(columnPreferences(preferences))); return true; }
  catch { return false; }
}
const attributeName = (name: string) => name.trim().normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase();
const attributeAliases: Record<string, string[]> = { color: ['farba', 'barva', 'color', 'colour'], size: ['velkost', 'velikost', 'size'] };
export function namedAttribute(row: ProductEditorRow, field: string) {
  return row.attributes.filter(item => attributeAliases[field]?.includes(attributeName(item.name))).map(item => item.value).join(' / ');
}
export interface EditorDraft { base: ProductEditorRow; change: ProductEditorChange; errors: Record<string, string>; status?: string; latest?: ProductEditorRow }
export type Drafts = Record<number, EditorDraft>;
export function shopColumn(field: string) {
  const match = /^(biketrek|xtrek)_(name|price|visible)$/.exec(field);
  return match ? { shop: match[1] as EditorMode, field: ({ name: 'name', price: 'sale_price_gross', visible: 'visible' } as Record<string, string>)[match[2]] } : null;
}
export function fieldPath(field: string, mode: EditorMode): string[] {
  const shop = shopColumn(field);
  if (shop) return ['shops', shop.shop, shop.field];
  if (field.startsWith('shop_')) return ['shops', mode, ({ shop_name: 'name', shop_price: 'sale_price_gross', shop_visible: 'visible' } as Record<string, string>)[field]];
  if (['name', 'brand', 'internal_note', 'image_url'].includes(field)) return ['common', field];
  if (['location', 'min_quantity'].includes(field)) return ['warehouse', field];
  if (field === 'ean') return ['variant', 'eans'];
  if (field === 'color' || field === 'size') return ['variant', 'attributes'];
  return ['variant', field];
}
function readPath(value: unknown, path: string[]): any { return path.reduce((current: any, key) => current?.[key], value); }
export function draftValue(draft: EditorDraft | undefined, field: string, mode: EditorMode): EditorValue | undefined {
  if (!draft) return undefined;
  const value = readPath(draft.change, fieldPath(field, mode));
  if ((field === 'color' || field === 'size') && Array.isArray(value)) return namedAttribute({ ...draft.base, attributes: value }, field);
  return value;
}
export function originalValue(row: ProductEditorRow, field: string, mode: EditorMode): EditorValue {
  const shop = shopColumn(field);
  if (shop) return readPath(row.shops.find(item => item.shop_code === shop.shop)?.effective, [shop.field]) ?? null;
  if (field.startsWith('shop_')) return readPath(row.shops.find(item => item.shop_code === mode)?.effective, fieldPath(field, mode).slice(2)) ?? null;
  if (field === 'sku') return row.sku;
  if (field === 'image_url') return row.image_url;
  if (field === 'ean') return row.eans;
  if (field === 'supplier_codes') return row.supplier_codes.map(item => `${item.supplier_code}: ${item.code}`).join(', ');
  if (field === 'color' || field === 'size') return namedAttribute(row, field);
  if (field === 'attributes') return row.attributes;
  if (field === 'channels') return row.shops.filter(item => item.mapped).map(item => item.shop_code === 'biketrek' ? 'BIKETREK' : item.shop_code === 'xtrek' ? 'xTrek' : item.shop_code).join(', ');
  if (['on_hand', 'reserved', 'quarantined', 'available', 'cost', 'total_value'].includes(field)) {
    if (!row.stock.known) return null;
    return readPath(row.stock, [({ on_hand: 'qty_on_hand', reserved: 'qty_reserved', quarantined: 'qty_quarantined', available: 'qty_available', cost: 'avg_cost', total_value: 'total_value' } as Record<string, string>)[field]]) ?? null;
  }
  return readPath(row, fieldPath(field, mode)) ?? null;
}
export function editorValueText(value: EditorValue | undefined): string {
  if (value === null || value === undefined) return '';
  if (Array.isArray(value)) return value.map(item => typeof item === 'string' ? item : `${item.name}: ${item.value}`).join(typeof value[0] === 'string' ? ', ' : ' · ');
  return String(value);
}
export function normalizeEditorValue(column: EditorColumn, raw: string): { value: EditorValue; error?: string } {
  const text = raw.trim();
  if (column.type === 'eans' && !text) return { value: [] };
  if (column.key === 'sku' && (!text || text.length > 100 || text.includes(';'))) return { value: text, error: 'invalid_sku' };
  if (!text || (column.type === 'visibility' && text === 'inherit')) return { value: null };
  if (column.type === 'url') {
    try { const url = new URL(text); return url.protocol === 'https:' && !url.username && !url.password && text.length <= 2000 ? { value: text } : { value: text, error: 'invalid_image_url' }; } catch { return { value: text, error: 'invalid_image_url' }; }
  }
  if (column.type === 'eans') { const values = text.split(/[,;\s]+/).filter(Boolean); return values.length <= 20 && values.every(value => /^\d{8,14}$/.test(value)) ? { value: [...new Set(values)] } : { value: text, error: 'invalid_eans' }; }
  if (column.type === 'attributes') {
    const parts = text.split(/\s*·\s*|\n/).filter(Boolean), values = parts.map(part => { const index = part.indexOf(':'); return index > 0 ? { name: part.slice(0, index).trim(), value: part.slice(index + 1).trim() } : null; });
    return values.length <= 100 && values.every(value => value && value.name.length <= 100 && value.value && value.value.length <= 255) && new Set(values.map(value => value && attributeName(value.name))).size === values.length ? { value: values as {name:string;value:string}[] } : { value: text, error: 'invalid_attributes' };
  }
  if (column.type === 'visibility') return text === 'true' || text === '1' ? { value: true } : text === 'false' || text === '0' ? { value: false } : { value: text, error: 'invalid_visibility' };
  if (column.type === 'money' || column.type === 'vat') {
    const match = /^(\d{1,10})(?:[.,](\d{1,2}))?$/.exec(text);
    if (!match) return { value: text, error: 'invalid_price' };
    const whole = match[1].replace(/^0+(?=\d)/, ''), fraction = (match[2] || '').padEnd(2, '0');
    if (column.type === 'vat' && (whole.length > 3 || Number(whole) > 100 || (whole === '100' && fraction !== '00'))) return { value: text, error: 'invalid_vat' };
    return { value: `${whole}.${fraction}` };
  }
  if (column.type === 'integer') return /^\d{1,9}$/.test(text) ? { value: text.replace(/^0+(?=\d)/, '') } : { value: text, error: 'invalid_minimum' };
  const limit = column.key === 'brand' || column.key === 'location' ? 100 : column.key === 'name' || column.key === 'shop_name' || column.key.endsWith('_name') ? 500 : 4000;
  if (text.length > limit || /[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/.test(text)) return { value: text, error: 'invalid_text' };
  return { value: text };
}
export function changeDraft(drafts: Drafts, row: ProductEditorRow, field: string, mode: EditorMode, value: EditorValue, error?: string): Drafts {
  const existing = drafts[row.id];
  const draft: EditorDraft = existing ? { ...existing, change: structuredClone(existing.change), errors: { ...existing.errors } } : {
    base: row, change: { product_id: row.id, expected_revision: row.revision, snapshot_hash: row.snapshot_hash }, errors: {},
  };
  const path = fieldPath(field, mode), key = path.join('.');
  if (draft.status === 'invalid') { draft.status = undefined; delete draft.errors._row; }
  const attributeField = field === 'color' || field === 'size';
  if (attributeField && !error) {
    const attributes = draft.change.variant?.attributes ?? draft.base.attributes;
    const aliases = attributeAliases[field], previous = attributes.find(item => aliases.includes(attributeName(item.name)));
    const others = attributes.filter(item => !aliases.includes(attributeName(item.name)));
    value = value === null ? others : previous ? attributes.map(item => item === previous ? { ...item, value: String(value) } : item) : [...others, { name: field === 'color' ? 'Farba' : 'Veľkosť', value: String(value) }];
  }
  const same = JSON.stringify(value) === JSON.stringify(attributeField ? draft.base.attributes : originalValue(draft.base, field, mode));
  const overridePath = path[0] === 'warehouse' ? ['warehouses', row.warehouse?.code || '', path[1]] : path;
  const inherited = value === null && readPath(draft.base.overrides, overridePath) === undefined;
  let target: any = draft.change;
  for (const part of path.slice(0, -1)) target = target[part] ||= {};
  if ((same || inherited) && !error) delete target[path[path.length - 1]]; else target[path[path.length - 1]] = value;
  if (error) draft.errors[key] = error; else delete draft.errors[key];
  for (const scope of ['common', 'variant', 'warehouse', 'shops']) {
    const block = (draft.change as any)[scope];
    if (scope === 'shops' && block) for (const code of Object.keys(block)) if (!Object.keys(block[code]).length) delete block[code];
    if (block && !Object.keys(block).length) delete (draft.change as any)[scope];
  }
  const next = { ...drafts };
  if (Object.keys(draft.change).length === 3) delete next[row.id]; else next[row.id] = draft;
  return next;
}
export function parseEditorTsv(text: string): string[][] | null {
  if (!text || text.length > 200000) return null;
  const rows: string[][] = [[]]; let field = '', quoted = false, afterQuote = false;
  const value = text.replace(/\r\n/g, '\n').replace(/\r/g, '\n');
  for (let i = 0; i < value.length; i++) {
    const char = value[i];
    if (quoted) {
      if (char === '"' && value[i + 1] === '"') { field += '"'; i++; }
      else if (char === '"') { quoted = false; afterQuote = true; }
      else field += char;
    } else if (char === '"' && !field && !afterQuote) quoted = true;
    else if (char === '\t' || char === '\n') {
      rows[rows.length - 1].push(field); field = ''; afterQuote = false;
      if (char === '\n') rows.push([]);
    } else { if (afterQuote) return null; field += char; }
    if (rows.length > 101 || rows[rows.length - 1].length > 50) return null;
  }
  if (quoted) return null;
  rows[rows.length - 1].push(field);
  if (value.endsWith('\n')) rows.pop();
  return rows.length <= 100 && rows[0]?.length > 0 && rows.every(row => row.length === rows[0].length) ? rows : null;
}
