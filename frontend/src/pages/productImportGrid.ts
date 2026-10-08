import { ImportField, ImportValue, ImportValues, ProductImportRow } from '../api/productImport';
import { TargetOptions } from '../api/catalog';
import { parseEditorTsv } from './productEditorGrid';

export type ImportView = 'basic' | 'prices' | 'content' | 'categories' | 'parameters' | 'all';
export interface ImportColumn { key: ImportField; type: 'text' | 'money' | 'long' | 'list' | 'pairs' | 'metadata' | 'category' | 'boolean'; view: Exclude<ImportView, 'all'>; width?: number; readonly?: boolean }
export const IMPORT_COLUMNS: ImportColumn[] = [
  { key: 'name', type: 'text', view: 'basic', width: 280 },
  { key: 'supplier_code', type: 'text', view: 'basic', width: 150 },
  { key: 'group_name', type: 'text', view: 'basic', width: 240 },
  { key: 'brand', type: 'text', view: 'basic' },
  { key: 'manufacturer_code', type: 'text', view: 'basic' },
  { key: 'eans', type: 'list', view: 'basic', width: 180 },
  { key: 'images', type: 'list', view: 'basic', width: 160 },
  { key: 'category_code', type: 'category', view: 'categories', width: 260 },
  { key: 'sale_gross', type: 'money', view: 'prices' },
  { key: 'purchase_net', type: 'money', view: 'prices' },
  { key: 'retail_gross', type: 'money', view: 'prices' },
  { key: 'vat_percent', type: 'money', view: 'prices', width: 110 },
  { key: 'currency', type: 'text', view: 'prices', width: 110 },
  { key: 'availability', type: 'text', view: 'basic', width: 190 },
  { key: 'short_description', type: 'long', view: 'content', width: 280 },
  { key: 'description_html', type: 'long', view: 'content', width: 320 },
  { key: 'seo_title', type: 'text', view: 'content', width: 230 },
  { key: 'seo_description', type: 'long', view: 'content', width: 280 },
  { key: 'seo_url', type: 'text', view: 'content', width: 230 },
  { key: 'parameters', type: 'pairs', view: 'parameters', width: 280 },
  { key: 'variant_attributes', type: 'pairs', view: 'parameters', width: 240 },
  { key: 'metadata', type: 'metadata', view: 'parameters', width: 240 },
];
export const FAMILY_IMPORT_FIELDS = new Set<ImportField>(['group_name', 'brand', 'description_html', 'short_description', 'seo_title', 'seo_description', 'seo_url', 'category_code', 'parameters', 'metadata', 'ai_enabled']);
export type ImportChanges = Record<number, Partial<ImportValues>>;
export const effectiveImportValues = (row: ProductImportRow, changes: ImportChanges) => ({ ...row.values, ...changes[row.id] });
export function importValueText(value: ImportValue | undefined): string {
  if (value === undefined || value === null) return '';
  if (Array.isArray(value)) return value.map(item => typeof item === 'string' ? item : `${item.name}: ${item.value}`).join('\n');
  if (typeof value === 'object') return Object.entries(value).map(([name, item]) => `${name}: ${item}`).join('\n');
  return String(value);
}
export function parseImportValue(column: ImportColumn, raw: string): ImportValue {
  if (column.type === 'long') return raw;
  const value = raw.trim();
  if (column.type === 'money') {
    if (!value) return null;
    if (!/^\d{1,10}(?:[.,]\d{1,2})?$/.test(value)) throw new Error('invalidMoney');
    if (column.key === 'vat_percent' && Number(value.replace(',', '.')) > 100) throw new Error('invalidVat');
    return value.replace(',', '.');
  }
  if (column.type === 'boolean') {
    if (!['true', 'false', '1', '0'].includes(value)) throw new Error('invalidBoolean');
    return value === 'true' || value === '1';
  }
  if (column.type === 'list') return [...new Set(value.split(column.key === 'eans' ? /[,;\s]+/ : /\r?\n/).map(item => item.trim()).filter(Boolean))];
  if (column.type === 'pairs' || column.type === 'metadata') {
    const entries = value.split(/\r?\n/).filter(line => line.trim()).map(line => {
      const index = line.indexOf(':');
      if (index < 1 || !line.slice(index + 1).trim()) throw new Error('invalidPairs');
      return { name: line.slice(0, index).trim(), value: line.slice(index + 1).trim() };
    });
    if (column.key !== 'parameters' && new Set(entries.map(item => item.name)).size !== entries.length) throw new Error('duplicatePairs');
    return column.type === 'metadata' ? Object.fromEntries(entries.map(item => [item.name, item.value])) : entries;
  }
  return column.type === 'category' ? value || null : value;
}
export function stageImportValue(rows: ProductImportRow[], changes: ImportChanges, row: ProductImportRow, field: ImportField, value: ImportValue): ImportChanges {
  const next = { ...changes };
  for (const target of rows.filter(item => item.id === row.id || (FAMILY_IMPORT_FIELDS.has(field) && item.group_key === row.group_key))) {
    const patch = { ...next[target.id], [field]: value };
    if (JSON.stringify(target.values[field]) === JSON.stringify(value)) delete patch[field];
    if (Object.keys(patch).length) next[target.id] = patch; else delete next[target.id];
  }
  return next;
}
export function pasteImportValues(rows: ProductImportRow[], changes: ImportChanges, columns: ImportColumn[], startId: number, startField: ImportField, text: string, canEdit: (row: ProductImportRow, column: ImportColumn) => boolean = () => true, familyRows: ProductImportRow[] = rows): ImportChanges {
  const matrix = parseEditorTsv(text), startRow = rows.findIndex(row => row.id === startId), startColumn = columns.findIndex(column => column.key === startField);
  if (!matrix || startRow < 0 || startColumn < 0 || startRow + matrix.length > rows.length || startColumn + matrix[0].length > columns.length) throw new Error('pasteBounds');
  let next = changes;
  const familyValues = new Map<string, string>();
  matrix.forEach((values, r) => values.forEach((raw, c) => {
    const row = rows[startRow + r], column = columns[startColumn + c];
    if (column.readonly || !canEdit(row, column)) throw new Error('pasteLocked');
    const value = parseImportValue(column, raw);
    if (FAMILY_IMPORT_FIELDS.has(column.key)) {
      const key = `${row.group_key}:${column.key}`, encoded = JSON.stringify(value);
      if (familyValues.has(key) && familyValues.get(key) !== encoded) throw new Error('familyConflict');
      familyValues.set(key, encoded);
    }
    // Stage the actual pasted value across the full family, including resetting
    // to the source value. Looking only at remaining patches would miss resets.
    next = stageImportValue(familyRows, next, row, column.key, value);
  }));
  return next;
}
export function categoryPath(categories: TargetOptions['categories'], code: string | null, language = 'sk'): string {
  const byCode = new Map(categories.map(category => [category.code, category]));
  const names: string[] = [], seen = new Set<string>();
  while (code && !seen.has(code)) { seen.add(code); const item = byCode.get(code); names.unshift(item?.names[language] || item?.names.sk || code); code = item?.parent_code || null; }
  return names.join(' / ');
}
