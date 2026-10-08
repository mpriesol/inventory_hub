import React, { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { Link, useLocation, useNavigate, useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { ArrowLeft, Columns, Save, Sparkles, RefreshCw, Upload } from 'lucide-react';
import { accessRevision, hubUnlocked, subscribeAccess } from '../api/access';
import { applyProductImportAi, createProductImport, editProductImport, ImportField, ImportValue, listProductImports, prepareProductImportAi, previewProductImport, ProductImportDraft, ProductImportRow, ProductImportSelection, ProductImportSummary, publishProductImport, readProductImport, saveProductImport, startProductImportAi } from '../api/productImport';
import { AiCategory, AiRules, aiRequest, aiContentStatusKey } from '../api/aiContent';
import { catalogImageUrl } from '../api/catalog';
import { Button } from '../components/ui/Button.new';
import { ProductThumb } from '../components/product/ProductDisplay';
import { ImportCellEditor } from '../components/product/ImportCellEditor';
import { useUnsavedNavigationGuard } from '../hooks/useUnsavedNavigationGuard';
import { categoryPath, effectiveImportValues, FAMILY_IMPORT_FIELDS, IMPORT_COLUMNS, ImportChanges, ImportColumn, ImportView, importValueText, parseImportValue, pasteImportValues, stageImportValue } from './productImportGrid';
import './ProductImportPage.css';

const PROCESSING_AI = new Set(['queued', 'generating', 'preparing_import', 'import_queued', 'importing']);
const APPLY_AI = new Set(['review', 'ready', 'approved']);
const VIEWS: ImportView[] = ['basic', 'prices', 'content', 'categories', 'parameters', 'all'];
const DEFAULT_VISIBLE: ImportField[] = ['name', 'brand', 'eans', 'category_code', 'sale_gross', 'availability'];
const PREFERENCES = 'product-import-columns-v1';
interface ColumnSettings { visible: ImportField[]; widths: Record<string, number> }
function loadSettings(): ColumnSettings {
  try { const saved = JSON.parse(localStorage.getItem(PREFERENCES) || 'null'); if (Array.isArray(saved?.visible)) return { visible: saved.visible.filter((key: ImportField) => IMPORT_COLUMNS.some(column => column.key === key)), widths: saved.widths || {} }; } catch { /* Browser preferences are optional. */ }
  return { visible: DEFAULT_VISIBLE, widths: {} };
}

export function ProductImportPage() {
  const { t, i18n } = useTranslation();
  const c = (key: string, values: Record<string, unknown> = {}) => t(`productImport.${key}`, values);
  const location = useLocation(), navigate = useNavigate();
  const [query, setQuery] = useSearchParams();
  const selection = (location.state as { selection?: ProductImportSelection } | null)?.selection;
  const draftId = query.get('draft');
  useSyncExternalStore(subscribeAccess, accessRevision);
  const unlocked = hubUnlocked();
  const [draft, setDraft] = useState<ProductImportDraft | null>(null);
  const draftRef = useRef(draft); draftRef.current = draft;
  const [recent, setRecent] = useState<ProductImportSummary[]>([]);
  const [changes, setChanges] = useState<ImportChanges>({});
  const changesRef = useRef(changes); changesRef.current = changes;
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [busy, setBusy] = useState('');
  const busyRef = useRef(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [view, setView] = useState<ImportView>('basic');
  const [settings, setSettings] = useState(loadSettings);
  const [showColumns, setShowColumns] = useState(false);
  const [bulkField, setBulkField] = useState<ImportField>('category_code');
  const [bulkValue, setBulkValue] = useState('');
  const [profiles, setProfiles] = useState<AiCategory[]>([]);
  const [profilesError, setProfilesError] = useState('');
  const [research, setResearch] = useState<'official' | 'feed_only'>('official');
  const [editor, setEditor] = useState<{ row: ProductImportRow; column: ImportColumn } | null>(null);
  const [modalDirty, setModalDirty] = useState(false);
  const [editing, setEditing] = useState<{ id: number; key: ImportField; text: string } | null>(null);
  const editingRef = useRef(false); editingRef.current = !!editing || !!editor;
  const [active, setActive] = useState<{ id: number; key: ImportField } | null>(null);
  const [page, setPage] = useState(1);
  const [filter, setFilter] = useState('');
  const cells = useRef(new Map<string, HTMLTableCellElement>());
  const input = useRef<HTMLInputElement>(null);
  const createId = useRef(crypto.randomUUID());
  const creating = useRef<Promise<ProductImportDraft> | null>(null);
  const [loadAttempt, setLoadAttempt] = useState(0);
  const generation = useRef(0);
  const dirty = Object.keys(changes).length;
  useUnsavedNavigationGuard(!!dirty || !!editing || modalDirty, c('discardChanges'));
  const errorText = (value: string) => t(`productImport.errors.${value}`, { defaultValue: t(`catalog.codes.${value}`, { defaultValue: t(`ai.errors.${value}`, { defaultValue: value }) }) });
  const rows = draft?.rows || [];
  const profileChanged = (row: ProductImportRow) => (effectiveImportValues(row, changes).ai_category_profile || 'auto') !== (row.values.ai_category_profile || 'auto');
  const needsPreparation = (row: ProductImportRow) => profileChanged(row) || row.ai_job?.staging?.profile_matches === false || (row.ai_job?.status === 'completed' && row.ai_job.staging?.ai_applied === false);
  const filteredRows = rows.filter(row => { const values = effectiveImportValues(row, changes); return `${values.code} ${values.name} ${values.eans.join(' ')} ${row.source_category || ''}`.toLocaleLowerCase().includes(filter.toLocaleLowerCase()); });
  const shownRows = filteredRows.slice((page - 1) * 50, page * 50);
  const columns = IMPORT_COLUMNS.filter(column => view === 'all' || (view === 'basic' ? settings.visible.includes(column.key) : column.key === 'name' || column.view === view));
  const jobs = [...new Map(rows.filter(row => row.ai_job).map(row => [row.ai_job!.id, row.ai_job!])).values()];
  const actionableJobs = jobs.filter(job => rows.some(row => row.ai_job?.id === job.id && effectiveImportValues(row, changes).ai_enabled && !needsPreparation(row)));
  const aiBusy = jobs.some(job => PROCESSING_AI.has(job.status));
  const publicationBusy = ['queued', 'running'].includes(draft?.publication_result?.status || '') || jobs.some(job => ['import_queued', 'importing'].includes(job.status));
  const publicationUncertain = draft?.publication_result?.items.some(item => item.status === 'uncertain');
  const publicationDone = draft?.publication_result?.status === 'completed' && draft.publication_result.items.length > 0 && draft.publication_result.items.every(item => ['created', 'exists'].includes(item.status));
  const deliveryState = draft?.publication_state || (publicationDone ? 'completed' : draft?.publication_result?.status);
  const aiPending = rows.some(row => effectiveImportValues(row, changes).ai_enabled && (row.ai_job?.status !== 'completed' || needsPreparation(row)));
  const locked = !!busy || publicationBusy || !!publicationUncertain || publicationDone;
  const aiCount = rows.filter(row => effectiveImportValues(row, changes).ai_enabled).length;
  const estimates = actionableJobs.filter(job => job.status === 'estimate');
  const estimate = estimates.reduce((sum, job) => sum + Number(job.estimate_usd || 0), 0);
  const actual = jobs.reduce((sum, job) => sum + Number(job.actual_usd || 0), 0);
  const width = (column: ImportColumn) => Math.max(100, Math.min(600, Number(settings.widths[column.key]) || column.width || 150));
  const cellEditable = (row: ProductImportRow, column: ImportColumn) => !locked && !(row.hub_product_id && ['supplier_code', 'eans'].includes(column.key));
  function replaceChanges(value: ImportChanges) { changesRef.current = value; setChanges(value); }
  function accept(value: ProductImportDraft) { draftRef.current = value; setDraft(value); }
  function stage(row: ProductImportRow, field: ImportField, value: ImportValue) {
    if (locked) return;
    generation.current++; replaceChanges(stageImportValue(rows, changesRef.current, row, field, value)); setNotice('');
  }
  useEffect(() => {
    if (!unlocked) return;
    let stopped = false;
    aiRequest<AiRules>('/rules').then(value => { if (!stopped) { setProfiles(value.book.categories); setProfilesError(''); } }).catch(error => { if (!stopped) setProfilesError(error.code || error.message); });
    return () => { stopped = true; };
  }, [unlocked, loadAttempt]);
  useEffect(() => { try { localStorage.setItem(PREFERENCES, JSON.stringify(settings)); } catch { /* No product data is stored in browser preferences. */ } }, [settings]);
  useEffect(() => {
    if (!unlocked) return;
    let stopped = false;
    if (draftId && draftRef.current?.id === draftId) return;
    if (!draftId && !selection) {
      listProductImports().then(value => { if (!stopped) setRecent(value.items); }).catch(value => { if (!stopped) setError(value.code || value.message); });
      return () => { stopped = true; };
    }
    busyRef.current = true; setBusy('loading'); setError('');
    if (!draftId && !creating.current) creating.current = createProductImport(selection!, createId.current);
    const request = draftId ? readProductImport(draftId) : creating.current!;
    request.then(value => {
      if (stopped) return;
      accept(value); replaceChanges({}); setSelected(new Set(value.rows.map(row => row.id)));
      if (!draftId) setQuery({ draft: value.id }, { replace: true });
    }).catch(value => { if (!stopped) { setError(value.code || value.message); creating.current = null; } }).finally(() => { if (!stopped) { busyRef.current = false; setBusy(''); } });
    return () => { stopped = true; };
  }, [unlocked, draftId, selection, loadAttempt]);
  useEffect(() => {
    if (!unlocked || !draft || (!aiBusy && !publicationBusy)) return;
    const controller = new AbortController();
    const poll = async () => {
      if (busyRef.current || editingRef.current || Object.keys(changesRef.current).length) return;
      const started = generation.current;
      try { const value = await readProductImport(draft.id, controller.signal); if (!controller.signal.aborted && started === generation.current && !busyRef.current && !editingRef.current && !Object.keys(changesRef.current).length && draftRef.current?.id === value.id) accept(value); }
      catch (error: any) { if (error.name !== 'AbortError') setError(error.code || error.message); }
    };
    const timer = window.setInterval(poll, 3500);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [unlocked, draft?.id, aiBusy, publicationBusy]);
  useEffect(() => { if (editing) { input.current?.focus(); input.current?.select(); } }, [editing?.id, editing?.key]);
  useEffect(() => { setPage(1); }, [filter]);
  async function run(name: string, action: (value: ProductImportDraft) => Promise<ProductImportDraft>, persist = true) {
    if (busyRef.current || !draftRef.current) return;
    busyRef.current = true; generation.current++; setBusy(name); setError(''); setNotice('');
    try {
      let value = draftRef.current;
      if (persist && Object.keys(changesRef.current).length) {
        value = await editProductImport(value, Object.entries(changesRef.current).map(([id, values]) => ({ id: Number(id), values })));
        accept(value); replaceChanges({});
      }
      value = await action(value); accept(value); setNotice(name);
    } catch (error: any) { setError(error.code || error.message); }
    finally { busyRef.current = false; setBusy(''); }
  }
  async function reload() {
    if (dirty && !window.confirm(c('discardChanges'))) return;
    await run('refreshed', async value => {
      const loaded = await readProductImport(value.id);
      replaceChanges({}); setEditing(null); return loaded;
    }, false);
  }
  function focus(id: number, key: ImportField) { setActive({ id, key }); window.setTimeout(() => cells.current.get(`${id}:${key}`)?.focus(), 0); }
  function move(row: ProductImportRow, column: ImportColumn, dr: number, dc: number) {
    let r = shownRows.findIndex(item => item.id === row.id) + dr, col = columns.findIndex(item => item.key === column.key) + dc;
    if (col >= columns.length) { col = 0; r++; } if (col < 0) { col = columns.length - 1; r--; }
    if (shownRows[r] && columns[col]) focus(shownRows[r].id, columns[col].key);
  }
  function startEdit(row: ProductImportRow, column: ImportColumn, initial?: string) {
    if (!cellEditable(row, column)) return;
    generation.current++;
    if (['long', 'list', 'pairs', 'metadata', 'category'].includes(column.type)) { setEditor({ row, column }); return; }
    setEditing({ id: row.id, key: column.key, text: initial ?? importValueText(effectiveImportValues(row, changesRef.current)[column.key]) });
  }
  function finish(row: ProductImportRow, column: ImportColumn): boolean {
    if (!editing) return true;
    try { stage(row, column.key, parseImportValue(column, editing.text)); setEditing(null); setError(''); return true; }
    catch (error) { setError((error as Error).message); return false; }
  }
  function key(event: React.KeyboardEvent, row: ProductImportRow, column: ImportColumn) {
    if (event.target !== event.currentTarget || editing || locked) return;
    if (event.key === 'Enter' || event.key === 'F2') { event.preventDefault(); startEdit(row, column); }
    else if (event.key === 'Tab') { event.preventDefault(); move(row, column, 0, event.shiftKey ? -1 : 1); }
    else if (event.key.startsWith('Arrow')) { event.preventDefault(); move(row, column, event.key === 'ArrowUp' ? -1 : event.key === 'ArrowDown' ? 1 : 0, event.key === 'ArrowLeft' ? -1 : event.key === 'ArrowRight' ? 1 : 0); }
    else if (event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) { event.preventDefault(); startEdit(row, column, event.key); }
  }
  function paste(event: React.ClipboardEvent, row: ProductImportRow, column: ImportColumn) {
    if (locked || editing) return;
    event.preventDefault();
    try {
      const next = pasteImportValues(shownRows, changesRef.current, columns, row.id, column.key, event.clipboardData.getData('text/plain'), cellEditable, rows);
      generation.current++; replaceChanges(next); setError(''); focus(row.id, column.key);
    } catch (error) { setError((error as Error).message); }
  }
  function bulk() {
    const column: ImportColumn | undefined = bulkField === 'ai_category_profile' ? { key: 'ai_category_profile', type: 'text', view: 'categories' } : IMPORT_COLUMNS.find(item => item.key === bulkField); if (!column || locked || (bulkField === 'ai_category_profile' && aiBusy)) return;
    try {
      if (rows.some(row => selected.has(row.id) && !cellEditable(row, column))) throw new Error('import_identity_saved');
      const value = bulkField === 'ai_category_profile' ? bulkValue || 'auto' : parseImportValue(column, bulkValue); let next = changesRef.current;
      for (const row of rows.filter(item => selected.has(item.id))) next = stageImportValue(rows, next, row, column.key, value);
      generation.current++; replaceChanges(next); setError('');
    } catch (error) { setError((error as Error).message); }
  }
  function toggleAI(enabled: boolean) { let next = changesRef.current; for (const row of rows.filter(item => selected.has(item.id))) next = stageImportValue(rows, next, row, 'ai_enabled', enabled); generation.current++; replaceChanges(next); }
  function goBack() { navigate(draft ? `/suppliers/${encodeURIComponent(draft.supplier)}/catalog` : '/suppliers'); }
  const categories = draft?.categories || [];
  const leafCategories = categories.filter(category => category.assignable !== false && category.active !== false && !categories.some(child => child.parent_code === category.code && child.assignable !== false));
  const readyItems = draft?.publication?.items.filter(item => item.status === 'ready') || [];
  return <div className="product-import">
    <header className="import-heading"><div><button className="import-back" onClick={goBack}><ArrowLeft size={16} />{c('back')}</button><h1>{c('title')}</h1><p className="import-muted">{c('subtitle')}</p></div>{draft && <div className="import-batch-meta"><strong>{draft.supplier} · {draft.shop}</strong><small>{c('batch', { id: draft.id.slice(0, 8) })} · {c('revision', { count: draft.revision })}</small><span className={`import-status ${draft.status === 'saved' && !dirty ? 'is-saved' : ''}`}>{deliveryState ? c(`delivery.${deliveryState}`) : c(dirty ? 'unsaved' : draft.status === 'saved' ? 'savedHub' : 'draft')}</span></div>}</header>
    {!unlocked ? <div className="import-panel"><p>{c('loginRequired')}</p><Link to="/login">{t('accounts.login')}</Link></div> : <>
      {error && <div className="import-error import-panel" role="alert">{errorText(error)}{draft ? <Button variant="secondary" size="sm" disabled={locked} onClick={reload}>{c('reload')}</Button> : <Button variant="secondary" size="sm" disabled={!!busy} onClick={() => setLoadAttempt(value => value + 1)}>{c('retry')}</Button>}</div>}
      {notice && !(notice === 'published' && !publicationBusy) && <div className="import-notice" role="status">{c(`notices.${notice}`)}</div>}
      {busy === 'loading' && <p role="status">{t('common.loading')}</p>}
      {!draft && !busy && <div className="import-panel"><h2>{c('recent')}</h2>{!recent.length ? <p>{c('empty')}</p> : <div className="import-recent">{recent.map(item => <Link key={item.id} to={`?draft=${item.id}`}>{item.supplier} · {item.shop}<strong>{item.publication_state ? c(`delivery.${item.publication_state}`) : c(item.status === 'saved' ? 'savedHub' : 'draft')}</strong><span>{c('rowCount', { count: item.rows_count })} · {new Date(item.updated_at).toLocaleString()}</span></Link>)}</div>}</div>}
      {draft && <>
        {profilesError && <p className="import-error" role="alert">{c('profilesLoadError')} <Button variant="ghost" size="sm" onClick={() => setLoadAttempt(value => value + 1)}>{c('retry')}</Button></p>}
        {publicationDone && <div className="import-notice" role="status"><strong>{c('delivery.completed')}</strong><p>{c('completedHelp')}</p>{aiPending && <p>{c('completedWithoutAi')}</p>}<a href="#import-publication">{c('showResult')}</a></div>}
        <div className="import-controls"><div className="import-views" role="tablist" aria-label={c('viewsLabel')}>{VIEWS.map(item => <button role="tab" aria-selected={view === item} key={item} onClick={() => { if (!editing) setView(item); }}>{c(`views.${item}`)}</button>)}</div><div className="import-control-tools"><input aria-label={c('search')} placeholder={c('search')} value={filter} onChange={event => setFilter(event.target.value)} /><Button variant="ghost" size="sm" icon={<Columns size={16} />} onClick={() => setShowColumns(!showColumns)}>{c('columns')}</Button><Button variant="ghost" size="sm" disabled={!!busy || !!editing} icon={<RefreshCw size={16} />} onClick={reload}>{c('refresh')}</Button></div></div>
        {showColumns && <div className="import-column-settings import-panel"><p>{c('columnsHelp')}</p>{IMPORT_COLUMNS.map(column => <label key={column.key}><input type="checkbox" checked={settings.visible.includes(column.key)} onChange={event => setSettings(old => ({ ...old, visible: event.target.checked ? [...old.visible, column.key] : old.visible.filter(key => key !== column.key) }))} />{c(`fields.${column.key}`)}<input type="number" min={100} max={600} step={10} aria-label={c('columnWidth', { field: c(`fields.${column.key}`) })} value={width(column)} onChange={event => setSettings(old => ({ ...old, widths: { ...old.widths, [column.key]: Math.max(100, Math.min(600, Number(event.target.value))) } }))} /></label>)}</div>}
        <div className="import-bulk"><strong>{c('selected', { count: selected.size })}</strong><select aria-label={c('bulkField')} value={bulkField} disabled={locked} onChange={event => { setBulkField(event.target.value as ImportField); setBulkValue(''); }}>{IMPORT_COLUMNS.map(column => <option key={column.key} value={column.key}>{c(`fields.${column.key}`)}</option>)}<option value="ai_category_profile">{c('fields.ai_category_profile')}</option></select>
          {bulkField === 'ai_category_profile' ? <select aria-label={c('bulkValue')} value={bulkValue || 'auto'} disabled={locked || aiBusy || !!profilesError} onChange={event => setBulkValue(event.target.value)}><option value="auto">{c('autoProfile')}</option>{profiles.map(profile => <option key={profile.id} value={profile.id}>{profile.name}</option>)}</select> : bulkField === 'category_code' ? <select aria-label={c('bulkValue')} value={bulkValue} disabled={locked} onChange={event => setBulkValue(event.target.value)}><option value="">{c('noCategory')}</option>{leafCategories.map(category => <option key={category.code} value={category.code}>{categoryPath(categories, category.code, i18n.language)}</option>)}</select> : <input aria-label={c('bulkValue')} value={bulkValue} disabled={locked} onChange={event => setBulkValue(event.target.value)} placeholder={c('value')} />}
          <Button variant="secondary" size="sm" disabled={locked || !selected.size || !!editing || (bulkField === 'ai_category_profile' && (aiBusy || !!profilesError))} onClick={bulk}>{c('bulkApply')}</Button><Button variant="ghost" size="sm" disabled={locked || !selected.size} onClick={() => toggleAI(true)}>{c('aiSelected')}</Button><Button variant="ghost" size="sm" disabled={locked || !selected.size} onClick={() => toggleAI(false)}>{c('aiNone')}</Button>
        </div>
        <div className="import-grid-scroll"><table className="import-grid" role="grid" aria-label={c('table')}><thead><tr><th className="import-fixed import-select"><input type="checkbox" aria-label={c('selectPage')} disabled={locked} checked={!!shownRows.length && shownRows.every(row => selected.has(row.id))} onChange={event => setSelected(old => { const next = new Set(old); shownRows.forEach(row => event.target.checked ? next.add(row.id) : next.delete(row.id)); return next; })} /></th><th className="import-fixed import-code">{c('fields.code')}</th><th className="import-ai-column">AI</th>{columns.map(column => <th key={column.key} style={{ minWidth: width(column), width: width(column) }}>{c(`fields.${column.key}`)}{FAMILY_IMPORT_FIELDS.has(column.key) && <span title={c('familyField')} className="import-shared">◇</span>}</th>)}<th>{c('status')}</th></tr></thead><tbody>
          {shownRows.map((row, index) => { const values = effectiveImportValues(row, changes), first = row.is_variant && shownRows[index - 1]?.group_key !== row.group_key; return <React.Fragment key={row.id}>
            {first && <tr className="import-family"><td colSpan={columns.length + 4}><strong>{values.group_name || row.group_name || row.group_key}</strong><span>{c('familyRows', { count: rows.filter(item => item.group_key === row.group_key).length })}</span><small>{c('familyField')}</small></td></tr>}
            <tr data-row={row.id} className={Object.keys(changes[row.id] || {}).length ? 'import-row-dirty' : ''}>
              <td className="import-fixed import-select"><input type="checkbox" aria-label={c('selectRow', { code: values.code })} disabled={locked} checked={selected.has(row.id)} onChange={event => setSelected(old => { const next = new Set(old); event.target.checked ? next.add(row.id) : next.delete(row.id); return next; })} /></td>
              <td className="import-fixed import-code"><div><ProductThumb key={values.images[0]} url={catalogImageUrl(values.images[0])} name={values.name} size={36} /><span><code title={c('codeHelp')}>{values.code}</code><small>{row.is_variant ? c('variant') : c('standalone')}</small></span></div></td>
              <td className="import-ai-column"><input type="checkbox" aria-label={c('aiRow', { code: values.code })} disabled={locked || aiBusy} checked={values.ai_enabled} onChange={event => stage(row, 'ai_enabled', event.target.checked)} /><label className="import-ai-profile">{c('fields.ai_category_profile')}<select aria-label={c('profileRow', { code: values.code })} disabled={locked || aiBusy || !!profilesError} value={values.ai_category_profile || 'auto'} onChange={event => stage(row, 'ai_category_profile', event.target.value)}><option value="auto">{c('autoProfile')}</option>{values.ai_category_profile && values.ai_category_profile !== 'auto' && !profiles.some(profile => profile.id === values.ai_category_profile) && <option value={values.ai_category_profile}>{values.ai_category_profile}</option>}{profiles.map(profile => <option key={profile.id} value={profile.id}>{profile.name}</option>)}</select></label></td>
              {columns.map(column => { const value = values[column.key], modified = changes[row.id] && column.key in changes[row.id], origin = modified ? 'manual' : row.provenance[column.key] || 'feed'; const text = column.type === 'category' ? categoryPath(categories, value as string | null, i18n.language) || c('noCategory') : column.key === 'images' ? c('imageCount', { count: values.images.length }) : importValueText(value); const current = editing?.id === row.id && editing.key === column.key;
                return <td key={column.key} ref={element => { if (element) cells.current.set(`${row.id}:${column.key}`, element); else cells.current.delete(`${row.id}:${column.key}`); }} data-field={column.key} role="gridcell" tabIndex={active ? active.id === row.id && active.key === column.key ? 0 : -1 : index === 0 && columns[0]?.key === column.key ? 0 : -1} aria-label={`${c(`fields.${column.key}`)}: ${values.code}`} className={modified ? 'import-cell-dirty' : ''} style={{ minWidth: width(column), maxWidth: width(column) }} onFocus={() => setActive({ id: row.id, key: column.key })} onDoubleClick={() => startEdit(row, column)} onKeyDown={event => key(event, row, column)} onPaste={event => paste(event, row, column)}>
                  {current ? <input ref={input} aria-label={c(`fields.${column.key}`)} value={editing.text} onChange={event => setEditing({ ...editing, text: event.target.value })} onBlur={() => finish(row, column)} onKeyDown={event => { if (event.key === 'Escape') { event.preventDefault(); setEditing(null); focus(row.id, column.key); } else if (event.key === 'Enter' || event.key === 'Tab') { event.preventDefault(); if (finish(row, column)) event.key === 'Tab' ? move(row, column, 0, event.shiftKey ? -1 : 1) : focus(row.id, column.key); } }} /> : <button className="import-cell-value" tabIndex={-1} disabled={!cellEditable(row, column)} onClick={() => startEdit(row, column)} title={cellEditable(row, column) ? text : errorText('import_identity_saved')}>{text || <span className="import-muted">—</span>}</button>}
                  <span className={`import-origin is-${origin}`}>{c(`origin.${origin}`)}</span>
                  {column.key === 'category_code' && <small className="import-source-category">{c('sourceCategory')}: {row.source_category || '—'}{value ? ` · ${c('withAncestors')}` : ''}</small>}
                </td>;
              })}
              <td className="import-row-state">{row.errors.map((issue, n) => <p key={n} className="import-error">{errorText(issue)}</p>)}{row.warnings.length > 0 && <details><summary>{c('warnings', { count: row.warnings.length })}</summary>{row.warnings.map((issue, n) => <p key={n}>{errorText(issue)}</p>)}</details>}{row.hub_product_id && <span className="import-status is-saved">{c('savedHub')}</span>}{row.publication_status && <span className={`import-status ${['created', 'exists'].includes(row.publication_status) ? 'is-saved' : ''}`}>{t(`catalog.itemStatus.${row.publication_status}`, {defaultValue:row.publication_status})}</span>}{row.ai_job && <div><span className="import-ai-state">AI · {needsPreparation(row) ? c('profileNeedsPreparation') : t(aiContentStatusKey(row.ai_job), { defaultValue: row.ai_job.status })}</span><Link to={`/ai-content?job=${encodeURIComponent(row.ai_job.id)}`}>{c('openAI')}</Link>{row.ai_job.staging?.automation_paused && <small>{t('ai.staging.automationPaused')}</small>}{row.ai_job.error && <small className="import-error">{errorText(row.ai_job.error)}</small>}{row.ai_job.checks?.errors?.map((issue, n) => <small key={n} className="import-error">{errorText(issue)}</small>)}</div>}</td>
            </tr>
          </React.Fragment>; })}
        </tbody></table></div>
        <div className="import-grid-footer"><p>{c('keyboardHelp')}</p><span>{c('rowCount', { count: filteredRows.length })}</span><Button variant="ghost" size="sm" disabled={page <= 1 || !!editing} onClick={() => setPage(page - 1)}>{t('common.back')}</Button><span>{page} / {Math.max(1, Math.ceil(filteredRows.length / 50))}</span><Button variant="ghost" size="sm" disabled={page * 50 >= filteredRows.length || !!editing} onClick={() => setPage(page + 1)}>{t('common.next')}</Button></div>
        {!publicationDone && aiPending && <p className="import-notice">{c('aiPendingHelp')}</p>}
        {!publicationDone && <div className="import-workflow">
          <section className="import-panel import-ai-panel"><div><h2><Sparkles size={18} />{c('aiTitle')}</h2><p>{c('aiHelp')}</p><p>{c('profileHelp')}</p><Link to="/settings/ai-content">{t('ai.modelSettings.title')}</Link></div><div className="import-actions"><label>{c('research')}<select value={research} disabled={locked || aiBusy} onChange={event => setResearch(event.target.value as 'official' | 'feed_only')}><option value="official">{c('official')}</option><option value="feed_only">{c('feedOnly')}</option></select></label><Button variant="secondary" disabled={locked || aiBusy || !aiCount || !!editing} onClick={() => run('aiPrepared', value => prepareProductImportAi(value, research))}>{c('prepareAI', { count: aiCount })}</Button>{estimates.length > 0 && <Button disabled={locked || aiBusy || !!editing} onClick={() => run('aiStarted', startProductImportAi)}>{c('startAI', { amount: estimate.toFixed(3) })}</Button>}{actionableJobs.some(job => APPLY_AI.has(job.status) && !job.checks?.errors?.length) && <Button variant="secondary" disabled={locked || aiBusy || !!editing} onClick={() => run('aiApplied', applyProductImportAi)}>{c('applyAI')}</Button>}</div>{jobs.length > 0 && <p className="import-muted">{aiBusy ? c('aiRunning') : c('aiCost', { amount: actual.toFixed(3) })}{dirty && aiBusy ? ` · ${c('pollPaused')}` : ''}</p>}</section>
          <section className="import-panel import-save-panel"><h2><Save size={18} />{c('saveTitle')}</h2><p>{c('saveHelp')}</p><div className="import-actions"><Button variant="secondary" disabled={locked || !dirty || !!editing} onClick={() => run('draftSaved', async value => value)}>{c('saveDraft', { count: dirty })}</Button><Button disabled={locked || aiBusy || !!editing} onClick={() => run('hubSaved', saveProductImport)}>{c('saveHub')}</Button><Button variant="secondary" icon={<Upload size={16} />} disabled={locked || aiBusy || dirty > 0 || draft.status !== 'saved' || aiPending || !!editing} onClick={() => run('previewReady', previewProductImport, false)}>{c('previewShop')}</Button></div></section>
        </div>}
        {(draft.publication || draft.publication_result) && <section id="import-publication" className="import-panel import-publication"><header><div><h2>{c('publicationTitle', { shop: draft.shop })}</h2><p>{c(draft.publication_result ? 'publicationResultHelp' : 'publicationHelp')}</p></div><span className="import-status">{deliveryState ? c(`delivery.${deliveryState}`) : c('hiddenProduct')}</span></header>
          {(draft.publication_result?.errors || draft.publication?.errors || []).map((issue, n) => <p key={n} className="import-error">{errorText(issue)}</p>)}
          <div className="import-publication-items">{(draft.publication_result?.items || draft.publication?.items || []).map(item => <details key={item.code}><summary><code>{item.code}</code><span>{item.name}</span><strong>{t(`catalog.itemStatus.${item.status}`, { defaultValue: item.status })}</strong></summary>{[...item.errors, ...item.warnings].map((issue, n) => <p key={n}>{errorText(issue)}</p>)}<pre>{JSON.stringify(item.payload, null, 2)}</pre></details>)}</div>
          <div className="import-actions">{!draft.publication_result && <Button disabled={locked || aiPending || !!dirty || !readyItems.length || !!draft.publication?.errors.length || Date.now() >= Date.parse(draft.publication?.expires_at || '')} onClick={() => run('published', value => publishProductImport(value), false)}>{c('publish', { count: readyItems.length, shop: draft.shop })}</Button>}{draft.publication_result && !publicationBusy && (draft.publication_result.status === 'failed' || draft.publication_result.items.some(item => ['failed', 'uncertain'].includes(item.status))) && <Button variant="secondary" disabled={!!busy || !!dirty} onClick={() => run('published', value => publishProductImport(value, true), false)}>{c('recover')}</Button>}</div>
        </section>}
        {editor && <ImportCellEditor key={`${editor.row.id}:${editor.column.key}`} column={editor.column} row={editor.row} value={effectiveImportValues(editor.row, changes)[editor.column.key]} draft={draft} onDirtyChange={setModalDirty} onApply={value => { stage(editor.row, editor.column.key, value); setEditor(null); }} onClose={() => setEditor(null)} />}
      </>}
    </>}
  </div>;
}
