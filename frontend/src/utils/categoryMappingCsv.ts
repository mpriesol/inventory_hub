import type { FeedCategory, FeedDefinition } from '../api/feedMapping';

export const CATEGORY_CSV_MAX_BYTES = 8 * 1024 * 1024;
type Rule = FeedDefinition['category_rules'][number];
export interface CsvShopCategory {
  code: string; name: string; path: string; parent_code?: string | null;
  active?: boolean; assignable?: boolean;
}
export interface CategoryCsvContext {
  supplier: string; feedKey: string; shop: string; revision: number;
  sources: FeedCategory[]; categories: CsvShopCategory[];
}
export class CategoryCsvError extends Error {
  constructor(public code: string, public row = 0) { super(code); }
}

// Protect spreadsheet exports, while retaining exact identifiers on reimport.
function spreadsheetCell(value: unknown): string {
  const text = String(value ?? '');
  return /^[=+\-@\t\r']/.test(text) ? "'" + text : text;
}
function originalCell(value: string): string {
  return /^'[=+\-@\t\r']/.test(value) ? value.slice(1) : value;
}
function csv(rows: unknown[][]): string {
  return '\uFEFF' + rows.map(row => row.map(value => {
    const text = spreadsheetCell(value);
    return /[;"\r\n]/.test(text) ? '"' + text.replace(/"/g, '""') + '"' : text;
  }).join(';')).join('\r\n') + '\r\n';
}

export function categoryMappingCsv(context: CategoryCsvContext, rules: Rule[]): string {
  const bySource = new Map(rules.map(rule => [rule.source, rule.target_code]));
  const paths = new Map(context.categories.map(category => [category.code, category.path]));
  return csv([
    ['supplier', 'feed_key', 'shop', 'mapping_revision', 'source', 'source_label', 'product_count', 'target_code', 'target_path'],
    ...context.sources.map(source => {
      const target = bySource.get(source.source) || '';
      return [context.supplier, context.feedKey, context.shop, context.revision, source.source,
        source.label, source.count, target, paths.get(target) || ''];
    }),
  ]);
}

export function shopCategoriesCsv(context: CategoryCsvContext): string {
  return csv([
    ['shop', 'code', 'name', 'path', 'parent_code', 'active', 'assignable'],
    ...context.categories.map(category => [context.shop, category.code, category.name, category.path,
      category.parent_code || '', category.active !== false, category.assignable !== false]),
  ]);
}

function readCsv(text: string): { cells: string[]; line: number }[] {
  if (text.length > CATEGORY_CSV_MAX_BYTES) throw new CategoryCsvError('csvTooLarge');
  text = text.replace(/^\uFEFF/, '');
  if (text.includes('\0')) throw new CategoryCsvError('csvMalformed', 1);
  const header = text.split(/\r?\n/, 1)[0];
  const separator = [';', '\t', ','].sort((a, b) => header.split(b).length - header.split(a).length)[0];
  const rows: { cells: string[]; line: number }[] = [];
  let cells: string[] = [], cell = '', quoted = false, closed = false, line = 1, start = 1;
  function field() {
    cells.push(originalCell(cell)); cell = ''; closed = false;
    if (cells.length > 32) throw new CategoryCsvError('csvMalformed', start);
  }
  function row() {
    field();
    if (cells.some(value => value !== '')) rows.push({ cells, line: start });
    cells = [];
    if (rows.length > 5001) throw new CategoryCsvError('csvTooManyRows');
  }
  for (let i = 0; i < text.length; i++) {
    const char = text[i];
    if (quoted) {
      if (char === '"') {
        if (text[i + 1] === '"') { cell += '"'; i++; }
        else { quoted = false; closed = true; }
      } else { cell += char; if (char === '\n') line++; }
    } else if (char === separator) field();
    else if (char === '\r' || char === '\n') {
      if (char === '\r' && text[i + 1] === '\n') i++;
      row(); line++; start = line;
    } else if (char === '"' && !cell && !closed) quoted = true;
    else {
      if (closed || char === '"') throw new CategoryCsvError('csvMalformed', line);
      cell += char;
    }
  }
  if (quoted) throw new CategoryCsvError('csvMalformed', start);
  if (cell || cells.length || closed) row();
  return rows;
}

export function importCategoryMappingCsv(text: string, context: CategoryCsvContext, current: Rule[]) {
  const records = readCsv(text);
  const headers = records.shift()?.cells.map(value => value.trim()) || [];
  const required = ['supplier', 'feed_key', 'shop', 'mapping_revision', 'source', 'target_code'];
  if (!required.every(name => headers.includes(name)) || new Set(headers).size !== headers.length) {
    throw new CategoryCsvError('csvHeaders');
  }
  const sources = new Set(context.sources.map(source => source.source));
  const targets = new Set(context.categories.filter(category => category.assignable !== false).map(category => category.code));
  const seen = new Set<string>(), rules = new Map(current.map(rule => [rule.source, rule.target_code]));
  let added = 0, changed = 0, unchanged = 0, blank = 0;
  for (const record of records) {
    if (record.cells.length !== headers.length) throw new CategoryCsvError('csvMalformed', record.line);
    const values = Object.fromEntries(headers.map((name, index) => [name, record.cells[index]]));
    if (values.supplier !== context.supplier || values.feed_key !== context.feedKey || values.shop !== context.shop) {
      throw new CategoryCsvError('csvContext', record.line);
    }
    if (values.mapping_revision !== String(context.revision)) throw new CategoryCsvError('csvStale', record.line);
    if (!values.source || !sources.has(values.source)) throw new CategoryCsvError('csvSource', record.line);
    if (seen.has(values.source)) throw new CategoryCsvError('csvDuplicate', record.line);
    seen.add(values.source);
    const target = values.target_code.trim();
    // A partial spreadsheet must never erase the other saved/manual mappings.
    if (!target) { blank++; continue; }
    if (!targets.has(target)) throw new CategoryCsvError('csvTarget', record.line);
    const previous = rules.get(values.source);
    if (previous === target) unchanged++;
    else if (previous) changed++;
    else added++;
    rules.set(values.source, target);
  }
  if (!added && !changed && !unchanged) throw new CategoryCsvError('csvEmpty');
  if (rules.size > 5000) throw new CategoryCsvError('csvTooManyRows');
  return { rules: Array.from(rules, ([source, target_code]) => ({ source, target_code })), added, changed, unchanged, blank };
}

export function downloadCategoryCsv(filename: string, content: string) {
  const url = URL.createObjectURL(new Blob([content], { type: 'text/csv;charset=utf-8' }));
  const link = document.createElement('a'); link.href = url; link.download = filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}
