import { EditorMode, ProductEditorChange, ProductEditorRow } from '../api/productEditor';

export interface EditorColumn { key: string; editable?: boolean; type?: 'text' | 'money' | 'vat' | 'integer' | 'visibility'; warehouse?: boolean; modes?: 'common' | 'shop'; hidden?: boolean }
export const EDITOR_COLUMNS: EditorColumn[] = [
  { key: 'sku' }, { key: 'name', editable: true, type: 'text', modes: 'common' },
  { key: 'brand', editable: true, type: 'text', modes: 'common' },
  { key: 'shop_name', editable: true, type: 'text', modes: 'shop' },
  { key: 'shop_price', editable: true, type: 'money', modes: 'shop' },
  { key: 'shop_visible', editable: true, type: 'visibility', modes: 'shop' },
  { key: 'sale_price_gross', editable: true, type: 'money', modes: 'common' },
  { key: 'location', editable: true, type: 'text', warehouse: true },
  { key: 'min_quantity', editable: true, type: 'integer', warehouse: true },
  { key: 'on_hand' }, { key: 'reserved' }, { key: 'available' }, { key: 'cost' }, { key: 'state' },
  { key: 'ean', hidden: true }, { key: 'supplier_codes', hidden: true },
  { key: 'internal_note', editable: true, type: 'text', modes: 'common', hidden: true },
  { key: 'vat_rate', editable: true, type: 'vat', modes: 'common', hidden: true },
  { key: 'note', editable: true, type: 'text', modes: 'common', hidden: true },
];
export interface EditorDraft { base: ProductEditorRow; change: ProductEditorChange; errors: Record<string, string>; status?: string; latest?: ProductEditorRow }
export type Drafts = Record<number, EditorDraft>;
export function fieldPath(field: string, mode: EditorMode): string[] {
  if (field.startsWith('shop_')) return ['shops', mode, ({ shop_name: 'name', shop_price: 'sale_price_gross', shop_visible: 'visible' } as Record<string, string>)[field]];
  if (['name', 'brand', 'internal_note'].includes(field)) return ['common', field];
  if (['location', 'min_quantity'].includes(field)) return ['warehouse', field];
  return ['variant', field];
}
function readPath(value: unknown, path: string[]): any { return path.reduce((current: any, key) => current?.[key], value); }
export function draftValue(draft: EditorDraft | undefined, field: string, mode: EditorMode) { return draft ? readPath(draft.change, fieldPath(field, mode)) : undefined; }
export function originalValue(row: ProductEditorRow, field: string, mode: EditorMode): string | boolean | null {
  if (field.startsWith('shop_')) return readPath(row.shops.find(item => item.shop_code === mode)?.effective, fieldPath(field, mode).slice(2)) ?? null;
  if (['sku'].includes(field)) return row.sku;
  if (field === 'ean') return row.eans.join(', ');
  if (field === 'supplier_codes') return row.supplier_codes.map(item => `${item.supplier_code}: ${item.code}`).join(', ');
  if (['on_hand', 'reserved', 'available', 'cost'].includes(field)) {
    if (!row.stock.known) return null;
    return readPath(row.stock, [({ on_hand: 'qty_on_hand', reserved: 'qty_reserved', available: 'qty_available', cost: 'avg_cost' } as Record<string, string>)[field]]) ?? null;
  }
  return readPath(row, fieldPath(field, mode)) ?? null;
}
export function normalizeEditorValue(column: EditorColumn, raw: string): { value: string | boolean | null; error?: string } {
  const text = raw.trim();
  if (!text || (column.type === 'visibility' && text === 'inherit')) return { value: null };
  if (column.type === 'visibility') return text === 'true' || text === '1' ? { value: true } : text === 'false' || text === '0' ? { value: false } : { value: text, error: 'invalid_visibility' };
  if (column.type === 'money' || column.type === 'vat') {
    const match = /^(\d{1,10})(?:[.,](\d{1,2}))?$/.exec(text);
    if (!match) return { value: text, error: 'invalid_price' };
    const whole = match[1].replace(/^0+(?=\d)/, ''), fraction = (match[2] || '').padEnd(2, '0');
    if (column.type === 'vat' && (whole.length > 3 || Number(whole) > 100 || (whole === '100' && fraction !== '00'))) return { value: text, error: 'invalid_vat' };
    return { value: `${whole}.${fraction}` };
  }
  if (column.type === 'integer') return /^\d{1,9}$/.test(text) ? { value: text.replace(/^0+(?=\d)/, '') } : { value: text, error: 'invalid_minimum' };
  const limit = column.key === 'brand' || column.key === 'location' ? 100 : column.key === 'name' || column.key === 'shop_name' ? 500 : 4000;
  if (text.length > limit || /[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/.test(text)) return { value: text, error: 'invalid_text' };
  return { value: text };
}
export function changeDraft(drafts: Drafts, row: ProductEditorRow, field: string, mode: EditorMode, value: string | boolean | null, error?: string): Drafts {
  const existing = drafts[row.id];
  const draft: EditorDraft = existing ? { ...existing, change: structuredClone(existing.change), errors: { ...existing.errors } } : {
    base: row, change: { product_id: row.id, expected_revision: row.revision, snapshot_hash: row.snapshot_hash }, errors: {},
  };
  const path = fieldPath(field, mode), key = path.join('.');
  if (draft.status === 'invalid') { draft.status = undefined; delete draft.errors._row; }
  const same = value === originalValue(draft.base, field, mode);
  const overridePath = path[0] === 'warehouse' ? ['warehouses', row.warehouse?.code || '', path[1]] : path;
  const inherited = value === null && readPath(draft.base.overrides, overridePath) === undefined;
  let target: any = draft.change;
  for (const part of path.slice(0, -1)) target = target[part] ||= {};
  if ((same || inherited) && !error) delete target[path.at(-1)!]; else target[path.at(-1)!] = value;
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
      rows.at(-1)!.push(field); field = ''; afterQuote = false;
      if (char === '\n') rows.push([]);
    } else { if (afterQuote) return null; field += char; }
    if (rows.length > 101 || rows.at(-1)!.length > 30) return null;
  }
  if (quoted) return null;
  rows.at(-1)!.push(field);
  if (value.endsWith('\n')) rows.pop();
  return rows.length <= 100 && rows[0]?.length > 0 && rows.every(row => row.length === rows[0].length) ? rows : null;
}
