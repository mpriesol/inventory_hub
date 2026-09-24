import React, { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Columns, Search, Save, X, PanelRightOpen } from 'lucide-react';
import { Button } from '../components/ui/Button.new';
import { ActionScope } from '../components/ui/ActionScope';
import { ProductThumb } from '../components/product/ProductDisplay';
import { FifoPanel } from '../components/product/FifoPanel';
import { accessRevision, hubUnlocked, subscribeAccess, unlockHub } from '../api/access';
import { EditorMode, getEditorProduct, getEditorProducts, getEditorSave, getProductEditorOptions, ProductEditorDetail,
  ProductEditorFilters, ProductEditorOptions, ProductEditorPageData, ProductEditorRow, ProductEditorSave, ProductEditorSaveBody, saveEditorProducts } from '../api/productEditor';
import { changeDraft, clampColumnWidth, columnPreferences, defaultColumnWidth, Drafts, draftValue, EDITOR_COLUMNS, EditorColumn,
  fieldPath, loadColumnPreferences, MAX_COLUMN_WIDTH, MIN_COLUMN_WIDTH, normalizeEditorValue, originalValue, parseEditorTsv, saveColumnPreferences } from './productEditorGrid';
import './ProductEditorPage.css';

const initialFilters: ProductEditorFilters = { q: '', brand: '', shop_code: '', warehouse_code: '', page: 1, page_size: 50, sort: 'group_sku', direction: 'asc' };
interface Editing { row: ProductEditorRow; column: EditorColumn; mode: EditorMode; value: string }

export function ProductEditorPage() {
  const { t, i18n } = useTranslation();
  const revision = useSyncExternalStore(subscribeAccess, accessRevision);
  const [token, setToken] = useState('');
  const [options, setOptions] = useState<ProductEditorOptions | null>(null);
  const [filters, setFilters] = useState(initialFilters);
  const [loaded, setLoaded] = useState<{ data: ProductEditorPageData; filters: ProductEditorFilters; revision: number } | null>(null);
  const data = loaded?.revision === revision ? loaded.data : null;
  const warehouse = loaded?.filters.warehouse_code || '';
  const [mode, setMode] = useState<EditorMode>('common');
  const [preferences, setPreferences] = useState(loadColumnPreferences);
  const [preferencesSaved, setPreferencesSaved] = useState(true);
  const availableColumns = preferences.order.map(key => EDITOR_COLUMNS.find(column => column.key === key)!)
    .filter(column => column && (!column.modes || column.modes === (mode === 'common' ? 'common' : 'shop')));
  const columns = availableColumns.filter(column => preferences.visible.includes(column.key));
  const widthOf = (key: string) => preferences.widths[key] ?? defaultColumnWidth(key);
  const resizing = useRef<(() => void) | null>(null);
  const [collapsedGroups, setCollapsedGroups] = useState<Set<number>>(new Set());
  const grouped = loaded?.filters.sort === 'group_sku';
  const shownRows = data?.items.filter(row => !grouped || !row.group || !collapsedGroups.has(row.group.id)) || [];
  const pageGroups = new Map<number, ProductEditorRow[]>();
  if (grouped) for (const row of data?.items || []) if (row.group) pageGroups.set(row.group.id, [...(pageGroups.get(row.group.id) || []), row]);
  const [showColumns, setShowColumns] = useState(false);
  const [drafts, setDrafts] = useState<Drafts>({});
  const draftsRef = useRef<Drafts>({});
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [active, setActive] = useState<{ id: number; field: string } | null>(null);
  const [editing, setEditing] = useState<Editing | null>(null);
  const editingRef = useRef<Editing | null>(null);
  const editor = useRef<HTMLInputElement | HTMLSelectElement | null>(null);
  const cells = useRef(new Map<string, HTMLTableCellElement>());
  const [bulkField, setBulkField] = useState('name'), [bulkValue, setBulkValue] = useState('');
  const [busy, setBusy] = useState<'load' | 'save' | 'recover' | null>(null);
  const [error, setError] = useState(''), [savedCount, setSavedCount] = useState(0);
  const [uncertain, setUncertain] = useState(false), [saveMissing, setSaveMissing] = useState(false);
  const request = useRef<ProductEditorSaveBody | null>(null);
  const generation = useRef(0), controller = useRef<AbortController | null>(null), busyRef = useRef<string | null>(null);
  const [detailId, setDetailId] = useState<number | null>(null), [detail, setDetail] = useState<ProductEditorDetail | null>(null);
  const [detailTab, setDetailTab] = useState<'product' | 'audit' | 'fifo'>('product'), [detailError, setDetailError] = useState('');
  const detailGeneration = useRef(0), detailController = useRef<AbortController | null>(null);
  const dirtyCount = Object.keys(drafts).length;
  const editable = (column: EditorColumn) => !!column.editable && (!column.warehouse || !!warehouse);
  const locked = busy === 'save' || busy === 'recover' || uncertain;
  const selectedRows = shownRows.filter(row => selected.has(row.id));
  const selectedDetail = detailId ? data?.items.find(row => row.id === detailId) || drafts[detailId]?.base || detail : null;
  function replaceDrafts(value: Drafts) { draftsRef.current = value; setDrafts(value); }
  function stage(value: Drafts) { if (Object.keys(value).length > 100) { setError('draft_limit'); return; } replaceDrafts(value); setSavedCount(0); }
  function reset() {
    resizing.current?.(); setCollapsedGroups(new Set());
    generation.current++; controller.current?.abort(); detailGeneration.current++; detailController.current?.abort();
    setOptions(null); setLoaded(null); replaceDrafts({}); setSelected(new Set()); setActive(null); setEditing(null); editingRef.current = null;
    setDetailId(null); setDetail(null); setDetailError(''); setError(''); setSavedCount(0); setBusy(null); busyRef.current = null; setUncertain(false); setSaveMissing(false); request.current = null; setToken('');
  }
  useEffect(() => { reset(); return () => { generation.current++; controller.current?.abort(); detailGeneration.current++; detailController.current?.abort(); }; }, [revision]);
  useEffect(() => { setPreferencesSaved(saveColumnPreferences(preferences)); }, [preferences]);
  useEffect(() => () => { resizing.current?.(); }, []);
  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => { if (Object.keys(draftsRef.current).length) { event.preventDefault(); event.returnValue = ''; } };
    window.addEventListener('beforeunload', warn); return () => window.removeEventListener('beforeunload', warn);
  }, []);
  useEffect(() => { if (editing) { editor.current?.focus(); if (editor.current?.tagName === 'INPUT') (editor.current as HTMLInputElement).select(); } }, [editing?.row.id, editing?.column.key, editing?.mode]);
  useEffect(() => {
    if (shownRows.length && (!active || !shownRows.some(row => row.id === active.id) || !columns.some(column => column.key === active.field))) {
      setActive({ id: shownRows[0].id, field: columns[0].key });
    }
  }, [shownRows.map(row => row.id).join(','), columns.map(column => column.key).join(','), active]);
  useEffect(() => {
    if (!columns.some(column => column.key === bulkField && editable(column))) setBulkField(columns.find(editable)?.key || '');
  }, [columns.map(column => column.key).join(','), warehouse, bulkField]);
  function report(value: unknown) {
    const code = (value as Error & { code?: string }).code || 'request_failed';
    if (code === 'hub_access_required' || code === 'hub_access_not_configured') unlockHub('');
    setError(code);
  }
  function changeFilter<K extends keyof ProductEditorFilters>(key: K, value: ProductEditorFilters[K]) {
    if (busyRef.current === 'load') { generation.current++; controller.current?.abort(); busyRef.current = null; setBusy(null); }
    setFilters(previous => ({ ...previous, [key]: value, page: 1 })); setError('');
  }
  async function load(next: ProductEditorFilters = { ...filters, page: 1 }) {
    if (busyRef.current || !hubUnlocked() || locked) return;
    finishEdit();
    if (Object.keys(draftsRef.current).length && next.warehouse_code !== (loaded?.filters.warehouse_code || '')) { setError('warehouse_dirty'); return; }
    if (next.warehouse_code !== (loaded?.filters.warehouse_code || '')) { detailGeneration.current++; detailController.current?.abort(); setDetailId(null); setDetail(null); }
    const id = ++generation.current, credential = accessRevision(), abort = new AbortController(); controller.current?.abort(); controller.current = abort;
    busyRef.current = 'load'; setBusy('load'); setError(''); setSelected(new Set());
    const current = () => id === generation.current && credential === accessRevision() && !abort.signal.aborted;
    try {
      if (!options) { const value = await getProductEditorOptions(abort.signal); if (!current()) return; setOptions(value); }
      const value = await getEditorProducts(next, abort.signal); if (current()) { setLoaded({ data: value, filters: next, revision: credential }); setActive(value.items.length ? { id: value.items[0].id, field: 'sku' } : null); }
    } catch (value) { if (current()) report(value); }
    finally { if (current()) { busyRef.current = null; setBusy(null); } }
  }
  function focusCell(id: number, field: string) { setActive({ id, field }); setTimeout(() => cells.current.get(`${id}:${field}`)?.focus(), 0); }
  function resizeColumn(key: string, width: number) {
    if (!Number.isFinite(width)) return;
    setPreferences(previous => ({ ...previous, widths: { ...previous.widths, [key]: clampColumnWidth(width) } }));
  }
  function startResize(event: React.MouseEvent, column: EditorColumn) {
    if (event.button !== 0 || locked || busy) return;
    event.preventDefault(); finishEdit(); resizing.current?.();
    const start = event.clientX, width = widthOf(column.key);
    const move = (next: MouseEvent) => resizeColumn(column.key, width + next.clientX - start);
    const end = () => { window.removeEventListener('mousemove', move); window.removeEventListener('mouseup', end); resizing.current = null; };
    resizing.current = end; window.addEventListener('mousemove', move); window.addEventListener('mouseup', end);
  }
  function reorderColumn(key: string, direction: number) {
    finishEdit(); setActive(null);
    const index = availableColumns.findIndex(column => column.key === key), target = availableColumns[index + direction];
    if (key === 'sku' || !target || target.key === 'sku') return;
    setPreferences(previous => { const order = [...previous.order], a = order.indexOf(key), b = order.indexOf(target.key); [order[a], order[b]] = [order[b], order[a]]; return { ...previous, order }; });
  }
  function toggleGroup(id: number) {
    finishEdit(); setActive(null);
    if (!collapsedGroups.has(id)) setSelected(previous => new Set([...previous].filter(product => !pageGroups.get(id)?.some(row => row.id === product))));
    setCollapsedGroups(previous => { const next = new Set(previous); next.has(id) ? next.delete(id) : next.add(id); return next; });
  }
  function move(id: number, field: string, dr: number, dc: number, skipLocked = false) {
    if (!shownRows.length) return;
    let r = shownRows.findIndex(row => row.id === id), c = columns.findIndex(column => column.key === field);
    if (skipLocked) {
      for (let n = 0; n < shownRows.length * columns.length; n++) {
        c += dc; if (c >= columns.length) { c = 0; r++; } if (c < 0) { c = columns.length - 1; r--; }
        if (r < 0 || r >= shownRows.length) return;
        if (editable(columns[c])) { focusCell(shownRows[r].id, columns[c].key); return; }
      }
    } else focusCell(shownRows[Math.max(0, Math.min(shownRows.length - 1, r + dr))].id, columns[Math.max(0, Math.min(columns.length - 1, c + dc))].key);
  }
  function beginEdit(row: ProductEditorRow, column: EditorColumn, initial?: string) {
    if (!editable(column) || locked || !!busy) return;
    finishEdit(); const staged = draftValue(draftsRef.current[row.id], column.key, mode);
    const value = staged !== undefined ? staged : originalValue(row, column.key, mode);
    const next = { row, column, mode, value: initial ?? (value === null ? '' : String(value)) }; editingRef.current = next; setEditing(next); setActive({ id: row.id, field: column.key });
  }
  function finishEdit(direction?: number, discard = false) {
    const current = editingRef.current; if (!current) return;
    editingRef.current = null; setEditing(null);
    if (!discard) { const result = normalizeEditorValue(current.column, current.value); stage(changeDraft(draftsRef.current, current.row, current.column.key, current.mode, result.value, result.error)); }
    if (direction) move(current.row.id, current.column.key, 0, direction, true); else if (direction === 0) focusCell(current.row.id, current.column.key);
  }
  function key(event: React.KeyboardEvent, row: ProductEditorRow, column: EditorColumn) {
    if (editingRef.current || locked || busy) return;
    if (event.key === 'Enter' || event.key === 'F2') { event.preventDefault(); beginEdit(row, column); }
    else if (event.key === 'Tab') { event.preventDefault(); move(row.id, column.key, 0, event.shiftKey ? -1 : 1, true); }
    else if (['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight'].includes(event.key)) { event.preventDefault(); move(row.id, column.key, event.key === 'ArrowUp' ? -1 : event.key === 'ArrowDown' ? 1 : 0, event.key === 'ArrowLeft' ? -1 : event.key === 'ArrowRight' ? 1 : 0); }
    else if (event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey && editable(column)) { event.preventDefault(); beginEdit(row, column, event.key); }
  }
  function paste(event: React.ClipboardEvent, row: ProductEditorRow, column: EditorColumn) {
    if (locked || busy || !data) return;
    event.preventDefault(); const matrix = parseEditorTsv(event.clipboardData.getData('text/plain'));
    const startRow = shownRows.findIndex(item => item.id === row.id), startColumn = columns.findIndex(item => item.key === column.key);
    if (!matrix || startRow < 0 || startColumn < 0 || startRow + matrix.length > shownRows.length || startColumn + matrix[0].length > columns.length) { setError('paste_bounds'); return; }
    let next = draftsRef.current;
    for (let r = 0; r < matrix.length; r++) for (let c = 0; c < matrix[r].length; c++) {
      const target = columns[startColumn + c];
      if (!editable(target)) { setError('paste_locked'); return; }
      const value = normalizeEditorValue(target, matrix[r][c]);
      if (value.error) { setError(value.error); return; }
      next = changeDraft(next, shownRows[startRow + r], target.key, mode, value.value);
    }
    editingRef.current = null; setEditing(null); setError(''); stage(next); focusCell(row.id, column.key);
  }
  function bulk() {
    const column = columns.find(item => item.key === bulkField);
    if (!column || !editable(column) || !selectedRows.length || locked || busy) return;
    const value = normalizeEditorValue(column, bulkValue); if (value.error) { setError(value.error); return; }
    let next = draftsRef.current; for (const row of selectedRows) next = changeDraft(next, row, column.key, mode, value.value);
    setError(''); stage(next);
  }
  function applyResult(value: ProductEditorSave) {
    const next = { ...draftsRef.current }; const updates = new Map<number, ProductEditorRow>(); let saved = 0;
    for (const item of value.results) {
      if (item.row) updates.set(item.product_id, item.row);
      if (item.status === 'saved') { delete next[item.product_id]; saved++; }
      else if (next[item.product_id]) next[item.product_id] = { ...next[item.product_id], status: item.status, latest: item.row,
        errors: Object.fromEntries(item.errors.map(error => [error.field || '_row', error.code])) };
    }
    replaceDrafts(next); setLoaded(previous => previous ? { ...previous, data: { ...previous.data, items: previous.data.items.map(row => updates.get(row.id) || row) } } : null);
    setSavedCount(saved); setUncertain(false); setSaveMissing(false); request.current = null;
  }
  async function save() {
    if (busyRef.current || uncertain) return;
    finishEdit(); const values = Object.values(draftsRef.current); if (!values.length) return;
    if (values.some(draft => Object.keys(draft.errors).length || draft.status === 'conflict')) { setError('fix_errors'); return; }
    request.current = { request_id: crypto.randomUUID(), warehouse_code: warehouse || null, confirmed: true, changes: structuredClone(values.map(draft => draft.change)) };
    await sendSave();
  }
  async function sendSave() {
    if (!request.current || busyRef.current) return;
    const id = generation.current, credential = accessRevision(), body = request.current;
    busyRef.current = 'save'; setBusy('save'); setError('');
    setSaveMissing(false);
    const current = () => id === generation.current && credential === accessRevision();
    try { const value = await saveEditorProducts(body); if (current()) applyResult(value); }
    catch (value) { if (current()) {
      const code = (value as Error & { code?: string }).code;
      if (!code || code === 'request_failed' || code === 'product_editor_request_reused') setUncertain(true);
      else { setUncertain(false); request.current = null; }
      report(value);
    } }
    finally { if (current()) { busyRef.current = null; setBusy(null); } }
  }
  async function recoverSave() {
    if (!request.current || busyRef.current) return;
    const id = generation.current, credential = accessRevision(), abort = new AbortController(); controller.current = abort; busyRef.current = 'recover'; setBusy('recover'); setError('');
    const current = () => id === generation.current && credential === accessRevision() && !abort.signal.aborted;
    try { const value = await getEditorSave(request.current.request_id, abort.signal); if (current()) applyResult(value); }
    catch (value) { if (current()) {
      if ((value as Error & { code?: string }).code === 'product_editor_save_missing') setSaveMissing(true);
      else report(value);
    } }
    finally { if (current()) { busyRef.current = null; setBusy(null); } }
  }
  async function openDetail(row: ProductEditorRow, tab: 'product' | 'audit' | 'fifo' = 'product') {
    finishEdit(); setDetailId(row.id); setDetailTab(tab); setDetail(null); setDetailError('');
    const id = ++detailGeneration.current, credential = accessRevision(), scope = warehouse, abort = new AbortController(); detailController.current?.abort(); detailController.current = abort;
    try { const value = await getEditorProduct(row.id, scope, abort.signal); if (id === detailGeneration.current && credential === accessRevision() && !abort.signal.aborted) {
      setDetail(value);
      setLoaded(previous => previous && (previous.filters.warehouse_code || '') === scope ? { ...previous, data: { ...previous.data, items: previous.data.items.map(item => item.id === value.id ? value : item) } } : previous);
    } }
    catch (value) { if (id === detailGeneration.current && credential === accessRevision() && !abort.signal.aborted) {
      const code = (value as Error & { code?: string }).code || 'request_failed';
      if (code === 'hub_access_required' || code === 'hub_access_not_configured') unlockHub(''); else setDetailError(code);
    } }
  }
  function discardRow(id: number) { const next = { ...draftsRef.current }; delete next[id]; replaceDrafts(next); }
  function rebase(id: number) {
    const previous = draftsRef.current[id]; if (!previous?.latest) return;
    replaceDrafts({ ...draftsRef.current, [id]: { ...previous, base: previous.latest, latest: undefined, status: undefined, errors: {},
      change: { ...previous.change, expected_revision: previous.latest.revision, snapshot_hash: previous.latest.snapshot_hash } } });
  }
  const message = (code: string) => t(`productEditor.errors.${code}`, { defaultValue: t('productEditor.errors.request_failed') });
  const price = (value: string | boolean | null | undefined) => value === null || value === undefined ? t('productEditor.unknown') : String(value);
  const editableFields = EDITOR_COLUMNS.filter(column => column.editable).flatMap(column => column.modes === 'shop' ? (['biketrek', 'xtrek'] as EditorMode[]).map(scope => ({ column, scope })) : [{ column, scope: 'common' as EditorMode }]);
  const fieldLabel = (column: EditorColumn, scope: EditorMode) => `${scope === 'common' ? '' : scope === 'biketrek' ? 'BIKETREK · ' : 'xTrek · '}${t(`productEditor.fields.${column.key}`)}`;
  const changeValue = (value: string | boolean | null | undefined) => value === null || value === undefined ? t('productEditor.inherit') : typeof value === 'boolean' ? t(value ? 'productEditor.visible' : 'productEditor.hidden') : value || '—';
  function display(row: ProductEditorRow, column: EditorColumn) {
    const staged = draftValue(drafts[row.id], column.key, mode), value = staged !== undefined ? staged : originalValue(row, column.key, mode);
    if (column.key === 'state') {
      if (drafts[row.id]?.status) return t(`productEditor.rowStates.${drafts[row.id].status}`);
      if (drafts[row.id]) return t('productEditor.rowStates.dirty');
      const edited = mode === 'common' ? Object.values(row.overrides).some(scope => Object.keys(scope).length) : row.shops.find(shop => shop.shop_code === mode)?.state === 'saved_unpublished';
      return t(edited ? 'productEditor.rowStates.saved' : 'productEditor.rowStates.source');
    }
    if (staged === null) return <span className="product-editor-inherit">{t('productEditor.inherit')}</span>;
    if (column.type === 'visibility') return value === null ? t('productEditor.inherit') : t(value ? 'productEditor.visible' : 'productEditor.hidden');
    if (['on_hand', 'reserved', 'quarantined', 'available', 'cost', 'total_value', 'sale_price_gross', 'shop_price', 'vat_rate'].includes(column.key)) return price(value);
    return value === null || value === '' ? '—' : String(value);
  }
  return <div className="product-editor">
    <header className="product-editor-heading"><div><p className="product-editor-eyebrow">BIKETREK / xTrek</p><h1>{t('productEditor.title')}</h1><p>{t('productEditor.subtitle')}</p></div><div className="product-editor-heading-actions"><Link to="/stock">{t('productEditor.stockOverview')}</Link>{hubUnlocked() && <Button variant="ghost" size="sm" onClick={() => unlockHub('')}>{t('productEditor.lock')}</Button>}</div></header>
    {!hubUnlocked() ? <form className="product-editor-unlock" onSubmit={event => { event.preventDefault(); if (token.trim()) unlockHub(token.trim()); }}><label>{t('productEditor.token')}<input data-testid="unlock-token" type="password" autoComplete="off" value={token} onChange={event => setToken(event.target.value)} /></label><p>{t('productEditor.tokenHelp')}</p><Button data-testid="unlock" type="submit" disabled={!token.trim()}>{t('productEditor.unlock')}</Button></form> : <>
      <div className="product-editor-toolbar"><div className="product-editor-modes" role="tablist" aria-label={t('productEditor.mode')}>
        {(['common', 'biketrek', 'xtrek'] as EditorMode[]).map(value => <button key={value} data-testid={`mode-${value}`} role="tab" aria-selected={mode === value} disabled={locked} onClick={() => { finishEdit(); setMode(value); setBulkField(value === 'common' ? 'name' : 'shop_name'); setBulkValue(''); setActive(null); }}>{value === 'common' ? t('productEditor.common') : value === 'biketrek' ? 'BIKETREK' : 'xTrek'}</button>)}
      </div><span className="product-editor-price-basis">EUR · {t('productEditor.inclVat')}</span><Button data-testid="columns" variant="ghost" size="sm" icon={<Columns size={15} />} disabled={locked || !!busy} aria-expanded={showColumns} onClick={() => { finishEdit(); setShowColumns(value => !value); }}>{t('productEditor.columns')}</Button></div>
      {showColumns && <div className="product-editor-columns"><p>{t(preferencesSaved ? 'productEditor.browserPreferences' : 'productEditor.preferencesUnavailable')}</p>
        <div className="product-editor-column-list">{availableColumns.map((column, index) => <div className="product-editor-column-setting" key={column.key}>
          <label><input data-testid={`column-${column.key}`} type="checkbox" checked={preferences.visible.includes(column.key)} disabled={column.key === 'sku' || locked || !!busy} onChange={event => { finishEdit(); setActive(null); const checked = event.target.checked; setPreferences(previous => ({ ...previous, visible: checked ? [...previous.visible, column.key] : previous.visible.filter(key => key !== column.key) })); }} />{t(`productEditor.fields.${column.key}`)}</label>
          <label className="product-editor-width-label"><span>{t('productEditor.width')}</span><input data-testid={`width-${column.key}`} type="number" min={MIN_COLUMN_WIDTH} max={MAX_COLUMN_WIDTH} step="10" value={widthOf(column.key)} disabled={locked || !!busy} aria-label={t('productEditor.columnWidth', { column: t(`productEditor.fields.${column.key}`) })} onChange={event => { if (event.target.value) resizeColumn(column.key, Number(event.target.value)); }} /></label>
          <button data-testid={`column-up-${column.key}`} aria-label={t('productEditor.moveLeft', { column: t(`productEditor.fields.${column.key}`) })} disabled={index < 2 || locked || !!busy} onClick={() => reorderColumn(column.key, -1)}>↑</button>
          <button data-testid={`column-down-${column.key}`} aria-label={t('productEditor.moveRight', { column: t(`productEditor.fields.${column.key}`) })} disabled={column.key === 'sku' || index === availableColumns.length - 1 || locked || !!busy} onClick={() => reorderColumn(column.key, 1)}>↓</button>
        </div>)}</div><Button data-testid="reset-columns" size="sm" variant="ghost" disabled={locked || !!busy} onClick={() => { finishEdit(); setActive(null); setPreferences(columnPreferences()); }}>{t('productEditor.resetColumns')}</Button></div>}
      <form className="product-editor-filters" onSubmit={event => { event.preventDefault(); load(); }}>
        <label className="product-editor-search"><span><Search size={15} />{t('productEditor.search')}</span><input data-testid="search" value={filters.q} maxLength={200} disabled={locked} onChange={event => changeFilter('q', event.target.value)} placeholder={t('productEditor.searchHint')} /></label>
        <label>{t('productEditor.brand')}<select data-testid="brand" value={filters.brand} disabled={locked} onChange={event => changeFilter('brand', event.target.value)}><option value="">{t('productEditor.allBrands')}</option>{options?.brands.map(brand => <option key={brand}>{brand}</option>)}</select></label>
        <label>{t('productEditor.listing')}<select data-testid="shop-filter" value={filters.shop_code} disabled={locked} onChange={event => changeFilter('shop_code', event.target.value)}><option value="">{t('productEditor.allProducts')}</option>{options?.shops.map(shop => <option key={shop.code} value={shop.code}>{shop.name}</option>)}</select></label>
        <label title={dirtyCount ? t('productEditor.warehouseDirty') : ''}>{t('productEditor.warehouse')}<select data-testid="warehouse" value={filters.warehouse_code} disabled={locked || dirtyCount > 0} onChange={event => changeFilter('warehouse_code', event.target.value)}><option value="">{t('productEditor.allWarehouses')}</option>{options?.warehouses.map(item => <option key={item.code} value={item.code}>{item.name}</option>)}</select></label>
        <label>{t('productEditor.sort')}<select data-testid="sort" value={filters.sort} disabled={locked} onChange={event => changeFilter('sort', event.target.value as ProductEditorFilters['sort'])}>{['group_sku', 'sku', 'name'].map(value => <option value={value} key={value}>{t(`productEditor.sorts.${value}`)}</option>)}</select></label>
        <div className="product-editor-scoped-action"><Button data-testid="load" type="submit" variant="secondary" disabled={!!busy || uncertain}>{t(busy === 'load' ? 'productEditor.loading' : 'productEditor.load')}</Button><ActionScope effects={['hub-read']} /></div>
      </form>
      <div className="product-editor-savebar"><div><strong>{dirtyCount ? t('productEditor.dirtyCount', { count: dirtyCount }) : t('productEditor.noChanges')}</strong><small>{t('productEditor.localOnly')}</small></div>
        <div><Button data-testid="discard" variant="ghost" disabled={!dirtyCount || locked} onClick={() => { editingRef.current = null; setEditing(null); replaceDrafts({}); setError(''); }}>{t('productEditor.discard')}</Button><div className="product-editor-scoped-action"><Button data-testid="save" icon={<Save size={15} />} disabled={!dirtyCount || !!busy || uncertain} onClick={save}>{t('productEditor.save')}</Button><ActionScope effects={['hub-write']} /></div></div></div>
      {uncertain && <div role="alert" className="product-editor-alert"><p>{t(saveMissing ? 'productEditor.saveMissing' : 'productEditor.uncertain')}</p><Button data-testid="recover-save" variant="secondary" disabled={!!busy} onClick={recoverSave}>{t('productEditor.recover')}</Button>{saveMissing && <Button data-testid="retry-save" variant="secondary" disabled={!!busy} onClick={sendSave}>{t('productEditor.retrySame')}</Button>}</div>}
      {error && <div role="alert" className="product-editor-alert product-editor-error">{message(error)}</div>}
      {savedCount > 0 && <p role="status" className="product-editor-success">{t('productEditor.savedCount', { count: savedCount })}</p>}
      {selectedRows.length > 0 && <div className="product-editor-bulk"><strong>{t('productEditor.selectedPage', { count: selectedRows.length })}</strong><select data-testid="bulk-field" aria-label={t('productEditor.bulkField')} disabled={locked || !!busy} value={bulkField} onChange={event => { setBulkField(event.target.value); setBulkValue(''); }}>{columns.filter(editable).map(column => <option key={column.key} value={column.key}>{t(`productEditor.fields.${column.key}`)}</option>)}</select>
        {bulkField === 'shop_visible' ? <select data-testid="bulk-value" value={bulkValue} disabled={locked || !!busy} onChange={event => setBulkValue(event.target.value)}><option value="">{t('productEditor.inherit')}</option><option value="true">{t('productEditor.visible')}</option><option value="false">{t('productEditor.hidden')}</option></select> : <input data-testid="bulk-value" aria-label={t('productEditor.bulkValue')} disabled={locked || !!busy} value={bulkValue} onChange={event => setBulkValue(event.target.value)} />}
        <Button data-testid="bulk-apply" variant="secondary" size="sm" disabled={locked || !!busy || !bulkField} onClick={bulk}>{t('productEditor.bulkApply')}</Button><button className="product-editor-text-button" onClick={() => setSelected(new Set())}>{t('productEditor.clearSelection')}</button></div>}
      <div className={`product-editor-workspace ${detailId ? 'with-detail' : ''}`}><div className="product-editor-table-area">
        {!data ? <div className="product-editor-empty">{t('productEditor.initialHelp')}</div> : <>
          <div className="product-editor-grid-scroll" data-testid="grid-scroll"><table role="grid" aria-label={t('productEditor.title')} style={{ width: 76 + columns.reduce((sum, column) => sum + widthOf(column.key), 0) }}>
            <colgroup><col style={{ width: 34 }} />{columns.map(column => <col key={column.key} style={{ width: widthOf(column.key) }} />)}<col style={{ width: 42 }} /></colgroup>
            <thead><tr><th className="product-editor-select"><input data-testid="select-page" type="checkbox" aria-label={t('productEditor.selectPage')} checked={shownRows.length > 0 && shownRows.every(row => selected.has(row.id))} disabled={locked || !!busy || !shownRows.length} onChange={event => setSelected(new Set(event.target.checked ? shownRows.map(row => row.id) : []))} /></th>{columns.map(column => <th key={column.key} data-testid={`header-${column.key}`} style={{ width: widthOf(column.key) }} className={`product-editor-col-${column.key}`}><span className="product-editor-column-title">{t(`productEditor.fields.${column.key}`)}{editable(column) && <span className="product-editor-pencil" aria-hidden="true">✎</span>}</span>
              <button type="button" role="separator" aria-orientation="vertical" aria-label={t('productEditor.resizeColumn', { column: t(`productEditor.fields.${column.key}`) })} aria-valuemin={MIN_COLUMN_WIDTH} aria-valuemax={MAX_COLUMN_WIDTH} aria-valuenow={widthOf(column.key)} data-testid={`resize-${column.key}`} className="product-editor-resize" disabled={locked || !!busy} title={t('productEditor.resizeHelp')} onMouseDown={event => startResize(event, column)} onDoubleClick={() => resizeColumn(column.key, defaultColumnWidth(column.key))} onKeyDown={event => {
                if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
                event.preventDefault(); finishEdit(); const step = event.shiftKey ? 40 : 10;
                resizeColumn(column.key, event.key === 'Home' ? MIN_COLUMN_WIDTH : event.key === 'End' ? MAX_COLUMN_WIDTH : widthOf(column.key) + (event.key === 'ArrowLeft' ? -step : step));
              }} />
            </th>)}<th aria-label={t('productEditor.detail')} /></tr></thead>
            <tbody>{data.items.map((row, index) => <React.Fragment key={row.id}>
              {grouped && row.group && (index === 0 || data.items[index - 1].group?.id !== row.group.id) && <tr className="product-editor-family" data-testid={`family-${row.group.id}`}>
                <td className="product-editor-select"><input type="checkbox" data-testid={`select-family-${row.group.id}`} aria-label={t('productEditor.selectFamilyPage', { name: row.group.name, count: pageGroups.get(row.group.id)!.length })} disabled={locked || !!busy || collapsedGroups.has(row.group.id)} checked={!collapsedGroups.has(row.group.id) && pageGroups.get(row.group.id)!.every(item => selected.has(item.id))} onChange={event => { const checked = event.target.checked; setSelected(previous => { const next = new Set(previous); for (const item of pageGroups.get(row.group!.id)!) checked ? next.add(item.id) : next.delete(item.id); return next; }); }} /></td>
                <td colSpan={columns.length + 1}><button data-testid={`toggle-family-${row.group.id}`} aria-expanded={!collapsedGroups.has(row.group.id)} disabled={locked || !!busy} onClick={() => toggleGroup(row.group!.id)}><span aria-hidden="true">{collapsedGroups.has(row.group.id) ? '▸' : '▾'}</span> {row.group.name || row.group.code}</button><span>{t('productEditor.familyPageCount', { count: pageGroups.get(row.group.id)!.length })}</span>{pageGroups.get(row.group.id)!.some(item => drafts[item.id]) && <strong>{t('productEditor.familyDrafts')}</strong>}</td>
              </tr>}
              {(!grouped || !row.group || !collapsedGroups.has(row.group.id)) && <tr className={selected.has(row.id) ? 'is-selected' : ''}>
              <td className="product-editor-select"><input data-testid={`select-${row.id}`} type="checkbox" aria-label={t('productEditor.selectProduct', { sku: row.sku })} checked={selected.has(row.id)} disabled={locked} onChange={event => setSelected(previous => { const next = new Set(previous); event.target.checked ? next.add(row.id) : next.delete(row.id); return next; })} /></td>
              {columns.map(column => {
                const dirty = draftValue(drafts[row.id], column.key, mode) !== undefined, code = drafts[row.id]?.errors[fieldPath(column.key, mode).join('.')] || (column.key === 'state' ? drafts[row.id]?.errors._row : undefined);
                const isEditing = editing?.row.id === row.id && editing.column.key === column.key;
                return <td key={column.key} ref={element => { if (element) cells.current.set(`${row.id}:${column.key}`, element); else cells.current.delete(`${row.id}:${column.key}`); }} data-testid={`cell-${row.id}-${column.key}`} data-dirty={dirty || undefined} aria-readonly={!editable(column)} aria-invalid={!!code} tabIndex={active ? active.id === row.id && active.field === column.key ? 0 : -1 : index === 0 && column.key === 'sku' ? 0 : -1}
                  className={`product-editor-cell product-editor-col-${column.key} ${editable(column) ? 'is-editable' : ''} ${dirty ? 'is-dirty' : ''} ${code ? 'has-error' : ''}`}
                  title={code ? message(code) : column.warehouse && !warehouse ? t('productEditor.chooseWarehouse') : undefined}
                  onFocus={() => setActive({ id: row.id, field: column.key })} onDoubleClick={() => beginEdit(row, column)} onKeyDown={event => key(event, row, column)} onPaste={event => paste(event, row, column)}>
                  {isEditing ? column.type === 'visibility' ? <select data-testid="cell-editor" ref={element => { editor.current = element; }} value={editing.value} onChange={event => { const next = { ...editing, value: event.target.value }; editingRef.current = next; setEditing(next); }} onBlur={() => finishEdit()} onKeyDown={event => { if (['Enter', 'Tab', 'Escape'].includes(event.key)) { event.preventDefault(); event.stopPropagation(); finishEdit(event.key === 'Tab' ? event.shiftKey ? -1 : 1 : 0, event.key === 'Escape'); } }}><option value="">{t('productEditor.inherit')}</option><option value="true">{t('productEditor.visible')}</option><option value="false">{t('productEditor.hidden')}</option></select> : <input data-testid="cell-editor" ref={element => { editor.current = element; }} value={editing.value} inputMode={column.type === 'money' || column.type === 'vat' ? 'decimal' : column.type === 'integer' ? 'numeric' : 'text'} onChange={event => { const next = { ...editing, value: event.target.value }; editingRef.current = next; setEditing(next); }} onBlur={() => finishEdit()} onKeyDown={event => { if (['Enter', 'Tab', 'Escape'].includes(event.key)) { event.preventDefault(); event.stopPropagation(); finishEdit(event.key === 'Tab' ? event.shiftKey ? -1 : 1 : 0, event.key === 'Escape'); } }} /> : <>
                    <span className="product-editor-cell-value">{display(row, column)}</span>{column.key === 'sku' && row.attributes.length > 0 && <small title={row.attributes.map(attribute => `${attribute.name}: ${attribute.value}`).join(' · ')}>{row.attributes.map(attribute => `${attribute.name}: ${attribute.value}`).join(' · ')}</small>}{code && <small className="product-editor-cell-error">{message(code)}</small>}
                  </>}
                </td>;
              })}<td><button data-testid={`detail-${row.id}`} className="product-editor-detail-button" aria-label={t('productEditor.detailOf', { sku: row.sku })} onClick={() => openDetail(row)}><PanelRightOpen size={17} /></button></td>
            </tr>}</React.Fragment>)}</tbody>
          </table>{!data.items.length && <div className="product-editor-empty">{t('productEditor.empty')}</div>}</div>
          <footer className="product-editor-pagination"><span>{t('productEditor.range', { from: data.total ? (data.page - 1) * data.page_size + 1 : 0, to: (data.page - 1) * data.page_size + data.items.length, total: data.total })}</span><label>{t('productEditor.pageSize')}<select data-testid="page-size" disabled={locked} value={filters.page_size} onChange={event => changeFilter('page_size', Number(event.target.value))}>{[25, 50, 100].map(value => <option key={value}>{value}</option>)}</select></label><Button data-testid="previous" variant="ghost" size="sm" disabled={!!busy || uncertain || data.page <= 1} onClick={() => load({ ...loaded!.filters, page: data.page - 1 })}>←</Button><span>{data.page} / {Math.max(1, Math.ceil(data.total / data.page_size))}</span><Button data-testid="next" variant="ghost" size="sm" disabled={!!busy || uncertain || data.page * data.page_size >= data.total} onClick={() => load({ ...loaded!.filters, page: data.page + 1 })}>→</Button></footer>
        </>}
        <p className="product-editor-keyboard-help">{t('productEditor.keyboardHelp')}</p>
      </div>
      {detailId && selectedDetail && <aside className="product-editor-detail" aria-label={t('productEditor.detail')}>
        <header><div><code>{selectedDetail.sku}</code><h2>{selectedDetail.common.name}</h2></div><button data-testid="close-detail" aria-label={t('productEditor.closeDetail')} onClick={() => { detailGeneration.current++; detailController.current?.abort(); setDetailId(null); setDetail(null); }}><X size={20} /></button></header>
        <div className="product-editor-detail-tabs">{(['product', 'fifo', 'audit'] as const).map(tab => <button key={tab} data-testid={`detail-tab-${tab}`} aria-selected={detailTab === tab} onClick={() => setDetailTab(tab)}>{t(`productEditor.tabs.${tab}`)}</button>)}</div>
        <div className="product-editor-history-link"><Link data-testid="movement-history" to={`/stock/movements?sku=${encodeURIComponent(selectedDetail.sku)}`}>{t('productEditor.movementHistory')}</Link><ActionScope effects={['hub-read']} /></div>
        {detailTab === 'fifo' ? <FifoPanel productId={detailId} warehouseCode={warehouse} onChanged={() => { if (selectedDetail) openDetail(selectedDetail, 'fifo'); }} /> : detailTab === 'audit' ? <div className="product-editor-detail-body"><h3>{t('productEditor.auditTitle')}</h3>{!detail ? <p>{t('productEditor.loading')}</p> : !detail.audit.length ? <p>{t('productEditor.noAudit')}</p> : detail.audit.map(item => {
          const before = { ...selectedDetail, ...item.before }, after = { ...selectedDetail, ...item.after };
          const changes = editableFields.filter(({ column, scope }) => originalValue(before, column.key, scope) !== originalValue(after, column.key, scope));
          return <div className="product-editor-audit" key={item.id}><time>{item.created_at ? new Date(item.created_at).toLocaleString(i18n.language) : '—'}</time><p>{t('productEditor.auditSaved')}</p>{changes.length > 0 && <table data-testid={`audit-diff-${item.id}`} className="product-editor-diff"><thead><tr><th>{t('productEditor.field')}</th><th>{t('productEditor.previousValue')}</th><th>{t('productEditor.savedValue')}</th></tr></thead><tbody>{changes.map(({ column, scope }) => <tr key={`${scope}:${column.key}`}><th>{fieldLabel(column, scope)}</th><td>{changeValue(originalValue(before, column.key, scope))}</td><td>{changeValue(originalValue(after, column.key, scope))}</td></tr>)}</tbody></table>}</div>;
        })}</div> : <div className="product-editor-detail-body">
          <ProductThumb url={selectedDetail.image_url} name={selectedDetail.common.name} size={100} /><dl><dt>{t('productEditor.fields.ean')}</dt><dd>{selectedDetail.eans.join(', ') || '—'}</dd><dt>{t('productEditor.fields.supplier_codes')}</dt><dd>{selectedDetail.supplier_codes.map(item => `${item.supplier_code}: ${item.code}`).join(', ') || '—'}</dd>{selectedDetail.attributes.map(attribute => <React.Fragment key={attribute.name}><dt>{attribute.name}</dt><dd>{attribute.value}</dd></React.Fragment>)}</dl>
          {selectedDetail.shops.map(shop => <div className="product-editor-shop-card" key={shop.shop_code}><strong>{shop.shop_code === 'xtrek' ? 'xTrek' : shop.shop_code === 'biketrek' ? 'BIKETREK' : shop.shop_code}</strong><p>{t(shop.mapped ? 'productEditor.mapped' : 'productEditor.unmapped')}</p><dl><dt>{t('productEditor.observedName')}</dt><dd>{shop.observed.name || '—'}</dd><dt>{t('productEditor.observedPrice')}</dt><dd>{price(shop.observed.price)} <small>{t('productEditor.unknownTax')}</small></dd></dl></div>)}
          {drafts[detailId]?.latest && <div className="product-editor-conflict"><h3>{t('productEditor.conflictTitle')}</h3><p>{t('productEditor.conflictHelp')}</p><div className="product-editor-diff-scroll"><table data-testid="conflict-diff" className="product-editor-diff"><thead><tr><th>{t('productEditor.field')}</th><th>{t('productEditor.currentServer')}</th><th>{t('productEditor.yourChanges')}</th></tr></thead><tbody>{editableFields.filter(({ column, scope }) => draftValue(drafts[detailId], column.key, scope) !== undefined).map(({ column, scope }) => <tr key={`${scope}:${column.key}`}><th>{fieldLabel(column, scope)}</th><td>{changeValue(originalValue(drafts[detailId].latest!, column.key, scope))}</td><td>{changeValue(draftValue(drafts[detailId], column.key, scope))}</td></tr>)}</tbody></table></div><Button data-testid={`confirm-rebase-${detailId}`} variant="secondary" size="sm" disabled={locked} onClick={() => rebase(detailId)}>{t('productEditor.rebase')}</Button></div>}
          {drafts[detailId] && <Button data-testid={`discard-row-${detailId}`} variant="ghost" size="sm" disabled={locked} onClick={() => discardRow(detailId)}>{t('productEditor.discardRow')}</Button>}
        </div>}{detailError && <p className="product-editor-error">{message(detailError)}</p>}
      </aside>}
      </div>
    </>}
  </div>;
}
