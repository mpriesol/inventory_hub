import React, { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Button } from '../components/ui/Button.new';
import { accessRevision, hubUnlocked, subscribeAccess } from '../api/access';
import { ATOMER_CONTENT_FIELDS, AtomerContentField, AtomerContentItem, AtomerContentPreview, AtomerContentPublication,
  atomerContentHistory, previewAtomerContent, previewAtomerRollback, readAtomerPublication, resolveAtomerPublication, sendAtomerPublication } from '../api/atomerContent';
import './OpeningStockPage.css';

type Selection = { source_code: string; hub_sku: string };
type Frozen = { file: File; items: AtomerContentItem[] };
const emptySelection = (): Selection => ({ source_code: '', hub_sku: '' });
const valueText = (value: unknown) => typeof value === 'string' ? value : JSON.stringify(value, null, 2) ?? '';
const errorCode = (value: unknown) => (value as { code?: string })?.code || 'request_failed';
const MAX_BYTES = 100 * 1024 * 1024;
const PREFLIGHT_REJECTIONS = new Set(['product_publication_expired', 'product_publication_remote_changed',
  'product_publication_target_changed', 'product_publication_inflight', 'product_publication_identity_changed',
  'atomer_content_local_changed', 'atomer_content_family_mapping_changed', 'atomer_content_hub_brand_conflict',
  'atomer_content_anchor_changed']);

export function AtomerContentMigration() {
  const { t, i18n } = useTranslation();
  const revision = useSyncExternalStore(subscribeAccess, accessRevision);
  const [file, setFile] = useState<File | null>(null);
  const [rows, setRows] = useState<Selection[]>([emptySelection()]);
  const [fields, setFields] = useState<AtomerContentField[]>([...ATOMER_CONTENT_FIELDS]);
  const [frozen, setFrozen] = useState<Frozen | null>(null);
  const [preview, setPreview] = useState<AtomerContentPreview | null>(null);
  const [publications, setPublications] = useState<AtomerContentPublication[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState('');
  const [unknown, setUnknown] = useState<Record<string, boolean>>({});
  const [notSent, setNotSent] = useState<Record<string, boolean>>({});
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [settled, setSettled] = useState<Record<string, boolean>>({});
  const [rollbackRequests, setRollbackRequests] = useState<Record<string, string>>({});
  const busyRef = useRef<string | null>(null), generation = useRef(0), fileInput = useRef<HTMLInputElement>(null);
  const locked = !!frozen || !!busy;
  const selectionBlocked = rows.some(row => publications.some(record => record.source_code === row.source_code.trim()
    && (['sending', 'uncertain'].includes(record.state) || unknown[record.id])));
  const valid = !!file && file.size > 0 && file.size <= MAX_BYTES && fields.length > 0 && rows.length <= 10
    && rows.every(row => row.source_code.trim() && row.hub_sku.trim())
    && new Set(rows.map(row => row.source_code.trim())).size === rows.length && !selectionBlocked;
  function message(code: string) { return t(`atomerContent.errors.${code}`, { defaultValue: t('atomerContent.errors.generic', { code }) }); }
  function accept(record: AtomerContentPublication) {
    if (record.workflow !== 'atomer_content' || record.shop_code !== 'xtrek') throw new Error('Unexpected publication scope');
    setPublications(current => [record, ...current.filter(item => item.id !== record.id)]);
    if (!['ready', 'sending', 'uncertain'].includes(record.state)) setUnknown(current => ({ ...current, [record.id]: false }));
  }
  async function run<T>(key: string, request: () => Promise<T>, success: (value: T) => void, onError?: (caught: unknown) => void) {
    if (busyRef.current || !hubUnlocked()) return;
    const current = generation.current, credential = accessRevision();
    busyRef.current = key; setBusy(key); setError('');
    try {
      const result = await request();
      if (current === generation.current && credential === accessRevision()) success(result);
    } catch (caught) {
      if (current === generation.current && credential === accessRevision()) { setError(errorCode(caught)); onError?.(caught); }
    } finally {
      if (current === generation.current && credential === accessRevision()) { busyRef.current = null; setBusy(null); }
    }
  }
  function history() {
    return run('history', () => atomerContentHistory(), result => {
      if (result.items.some(item => item.workflow !== 'atomer_content' || item.shop_code !== 'xtrek')) throw new Error('Unexpected publication scope');
      setPublications(current => [...result.items, ...current.filter(item => !result.items.some(found => found.id === item.id))]);
      setUnknown(current => {
        const next = { ...current };
        result.items.forEach(item => { if (!['ready', 'sending', 'uncertain'].includes(item.state)) delete next[item.id]; });
        return next;
      });
    });
  }
  useEffect(() => {
    generation.current += 1; busyRef.current = null; setBusy(null); setFile(null); setFrozen(null); setPreview(null);
    setRows([emptySelection()]); setFields([...ATOMER_CONTENT_FIELDS]); setPublications([]); setUnknown({}); setNotSent({}); setNotes({}); setSettled({}); setRollbackRequests({}); setError('');
    if (fileInput.current) fileInput.current.value = '';
    if (hubUnlocked()) void history();
    return () => { generation.current += 1; };
  }, [revision]);
  useEffect(() => {
    if (!busy && !(frozen && !preview)) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ''; };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [busy, frozen, preview]);
  async function prepare() {
    if (busyRef.current || (!frozen && !valid)) return;
    const input = frozen || { file: file!, items: rows.map(row => ({ request_id: crypto.randomUUID(), source_code: row.source_code.trim(), hub_sku: row.hub_sku.trim(), fields: [...fields] })) };
    setFrozen(input);
    await run('preview', () => previewAtomerContent(input.file, input.items), result => {
      setPreview(result);
      result.items.forEach(item => { if (item.publication) accept(item.publication); });
    }, caught => {
      // These errors are returned by the complete XML/selection validation before
      // any item is persisted. A lost or unclassified response retains the UUIDs.
      const code = errorCode(caught);
      if (code.startsWith('atomer_source_') || code === 'atomer_content_invalid_selection') setFrozen(null);
    });
  }
  function newSelection() {
    if (busyRef.current || (frozen && !preview)) return;
    setFrozen(null); setPreview(null); setError('');
  }
  async function recoverPreview() {
    if (!frozen) return;
    await run('recover-preview', async () => {
      const result: AtomerContentPublication[] = [];
      for (const item of frozen.items) result.push(await readAtomerPublication(item.request_id));
      return result;
    }, result => {
      result.forEach(accept);
      // The original request IDs recovered every intended operation; no upload or PUT is repeated.
      setPreview({ source: { sha256: result[0]?.source_sha256 || '', size_bytes: frozen.file.size, total_rows: null },
        items: result.map(publication => ({ source_code: publication.source_code, hub_sku: publication.hub_sku, publication })) });
    });
  }
  async function rollbackPreview(record: AtomerContentPublication) {
    if (busyRef.current) return;
    const requestId = rollbackRequests[record.id] || crypto.randomUUID();
    setRollbackRequests(current => ({ ...current, [record.id]: requestId }));
    await run(`rollback:${record.id}`, () => previewAtomerRollback(record.id, requestId), accept);
  }
  function sendFailure(id: string, caught: unknown) {
    if (PREFLIGHT_REJECTIONS.has(errorCode(caught))) setNotSent(current => ({ ...current, [id]: true }));
    else setUnknown(current => ({ ...current, [id]: true }));
  }
  function download() {
    const blob = new Blob([JSON.stringify({ source: preview?.source, preview_results: preview?.items, publications }, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob), anchor = document.createElement('a');
    anchor.href = url; anchor.download = 'xtrek-atomer-content-results.json'; anchor.click();
    URL.revokeObjectURL(url);
  }
  const sourceLabel = (index: number) => t('atomerContent.sourceCode', { number: index + 1 });
  const skuLabel = (index: number) => t('atomerContent.hubSku', { number: index + 1 });
  return <div className="opening-stock">
    <Link className="opening-stock-back" to="/shops">{t('atomerContent.back')}</Link>
    <header><div><h1>{t('atomerContent.title')}</h1><p>{t('atomerContent.subtitle')}</p></div></header>
    <p className="opening-stock-notice">{t('atomerContent.scope')}</p>
    {error && <div role="alert" className="opening-stock-errors">{message(error)}</div>}
    <section className="opening-stock-panel" aria-labelledby="atomer-input-title">
      <h2 id="atomer-input-title">{t('atomerContent.prepareTitle')}</h2><p>{t('atomerContent.selectionHelp')}</p>
      <fieldset disabled={locked || !hubUnlocked()}>
        <label>{t('atomerContent.file')}<input ref={fileInput} type="file" accept=".xml,application/xml,text/xml" onChange={event => {
          const selected = event.target.files?.[0] || null; setFile(selected); setError(selected && (selected.size > MAX_BYTES || !selected.size) ? 'file_size' : '');
        }} /></label>
        {rows.map((row, index) => <div key={index} className="opening-stock-fields">
          <label>{sourceLabel(index)}<input value={row.source_code} maxLength={100} onChange={event => setRows(current => current.map((item, number) => number === index ? { ...item, source_code: event.target.value } : item))} /></label>
          <label>{skuLabel(index)}<input value={row.hub_sku} maxLength={100} onChange={event => setRows(current => current.map((item, number) => number === index ? { ...item, hub_sku: event.target.value } : item))} /></label>
        </div>)}
        <div className="opening-stock-actions"><Button variant="secondary" disabled={rows.length >= 10} onClick={() => setRows(current => [...current, emptySelection()])}>{t('atomerContent.addRow')}</Button>
          {rows.length > 1 && <Button variant="ghost" onClick={() => setRows(current => current.slice(0, -1))}>{t('atomerContent.removeRow')}</Button>}</div>
        <div className="opening-stock-confirmations">{ATOMER_CONTENT_FIELDS.map(field => <label key={field}><input type="checkbox" checked={fields.includes(field)} onChange={event => setFields(current => event.target.checked ? ATOMER_CONTENT_FIELDS.filter(value => current.includes(value) || value === field) : current.filter(value => value !== field))} />{t(`atomerContent.fields.${field}`)}</label>)}</div>
      </fieldset>
      {frozen && !preview && <p role="status">{t('atomerContent.previewRecovery')}</p>}
      {!frozen && selectionBlocked && <p role="alert" className="opening-stock-warnings">{t('atomerContent.uncertain')}</p>}
      <div className="opening-stock-actions">
        <Button disabled={!!busy || !hubUnlocked() || !!preview || (!frozen && !valid)} loading={busy === 'preview'} onClick={() => void prepare()}>{t(frozen ? 'atomerContent.retryPreview' : 'atomerContent.preview')}</Button>
        {frozen && !preview && <Button variant="secondary" disabled={!!busy} onClick={() => void recoverPreview()}>{t('atomerContent.recoverPreview')}</Button>}
        {preview && <Button variant="secondary" disabled={!!busy} onClick={newSelection}>{t('atomerContent.newSelection')}</Button>}
      </div>
      {preview && <div role="status"><p>{t('atomerContent.previewReady')}</p><p className="opening-stock-muted">{t('atomerContent.checksum')}: <code>{preview.source.sha256}</code></p>
        {preview.items.filter(item => item.error).map(item => <div role="alert" className="opening-stock-errors" key={item.source_code}><strong>{item.source_code} / {item.hub_sku}</strong>: {message(item.error!.code)}</div>)}</div>}
    </section>
    <section className="opening-stock-panel" aria-labelledby="atomer-results-title">
      <div className="opening-stock-actions"><h2 id="atomer-results-title">{t('atomerContent.results')}</h2><div className="flex gap-2 flex-wrap">
        <Button variant="secondary" disabled={!!busy || !hubUnlocked()} loading={busy === 'history'} onClick={() => void history()}>{t('atomerContent.history')}</Button>
        <Button variant="secondary" disabled={!publications.length && !preview} onClick={download}>{t('atomerContent.download')}</Button>
      </div></div>
      {!publications.length && <p>{t('atomerContent.empty')}</p>}
      {publications.map(record => {
        const uncertain = ['sending', 'uncertain'].includes(record.state) || unknown[record.id];
        const expired = new Date(record.expires_at).getTime() <= Date.now();
        const state = record.no_op ? 'noop' : notSent[record.id] && record.state === 'ready' ? 'rejected' : record.state;
        return <article className="opening-stock-panel" key={record.id} data-publication-id={record.id}>
          <div className="opening-stock-actions"><h3 className="font-semibold">{record.source_code} · {record.hub_sku}</h3><span role="status" className="opening-stock-badge">{t(`atomerContent.states.${state}`, { defaultValue: state })}</span></div>
          <p className="opening-stock-muted">{t('atomerContent.operation')}: <code>{record.id}</code></p>
          {record.rollback_of && <p>{t('atomerContent.rollbackOf')}: <code>{record.rollback_of}</code></p>}
          {record.error && <div role="alert" className="opening-stock-errors">{message(typeof record.error === 'string' ? record.error : record.error.code || 'request_failed')}</div>}
          <div className="opening-stock-table-scroll"><table><thead><tr><th>{t('atomerContent.field')}</th><th>{t('atomerContent.before')}</th><th>{t('atomerContent.after')}</th></tr></thead><tbody>
            {record.fields.map(field => <tr key={field}><th scope="row">{t(`atomerContent.fields.${field}`)}</th><td><pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', minWidth: 220, maxHeight: 320, overflow: 'auto' }}>{valueText(record.before[field])}</pre></td><td><pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', minWidth: 220, maxHeight: 320, overflow: 'auto' }}>{valueText(record.after[field])}</pre></td></tr>)}
          </tbody></table></div>
          {record.protected_changes && Object.values(record.protected_changes).some(value => Array.isArray(value) ? value.length > 0 : value && typeof value === 'object' ? Object.keys(value).length > 0 : !!value) && <details><summary>{t('atomerContent.protectedChanges')}</summary><pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{valueText(record.protected_changes)}</pre></details>}
          {record.state === 'ready' && expired && <p>{t('atomerContent.expired')}</p>}
          {record.state === 'ready' && notSent[record.id] && <p>{t('atomerContent.preflightStopped')}</p>}
          {uncertain && <p role="alert" className="opening-stock-warnings">{t('atomerContent.uncertain')}</p>}
          <div className="opening-stock-actions">
            {record.state === 'ready' && !record.no_op && !uncertain && !notSent[record.id] && <Button disabled={!!busy || expired || !hubUnlocked()} loading={busy === `send:${record.id}`} onClick={() => void run(`send:${record.id}`, () => sendAtomerPublication(record.id), accept, caught => sendFailure(record.id, caught))}>{t('atomerContent.send')}</Button>}
            <Button variant="secondary" disabled={!!busy || !hubUnlocked()} onClick={() => void run(`read:${record.id}`, () => readAtomerPublication(record.id), accept)}>{t('atomerContent.readResult')}</Button>
            {record.state === 'completed' && !record.no_op && !record.rollback_of && <Button variant="secondary" disabled={!!busy || !hubUnlocked()} loading={busy === `rollback:${record.id}`} onClick={() => void rollbackPreview(record)}>{t(rollbackRequests[record.id] ? 'atomerContent.retryRollback' : 'atomerContent.rollbackPreview')}</Button>}
          </div>
          {record.state === 'completed' && !record.no_op && !record.rollback_of && <p className="opening-stock-muted">{t('atomerContent.rollbackHelp')}</p>}
          {['sending', 'uncertain'].includes(record.state) && <details className="mt-4"><summary>{t('atomerContent.resolve')}</summary><p>{t('atomerContent.resolveHelp')}</p>
            <label>{t('atomerContent.note')}<textarea rows={3} maxLength={1000} value={notes[record.id] || ''} disabled={!!busy} onChange={event => setNotes(current => ({ ...current, [record.id]: event.target.value }))} /></label>
            <div className="opening-stock-confirmations"><label><input type="checkbox" checked={settled[record.id] || false} disabled={!!busy} onChange={event => setSettled(current => ({ ...current, [record.id]: event.target.checked }))} />{t('atomerContent.settled')}</label></div>
            <Button variant="secondary" disabled={!!busy || !settled[record.id] || (notes[record.id] || '').trim().length < 10} onClick={() => void run(`resolve:${record.id}`, () => resolveAtomerPublication(record.id, notes[record.id].trim()), result => { accept(result); setSettled(current => ({ ...current, [record.id]: false })); }, () => setUnknown(current => ({ ...current, [record.id]: true })))}>{t('atomerContent.resolveSubmit')}</Button>
          </details>}
          <p className="opening-stock-muted">{t('atomerContent.checksum')}: <code>{record.source_sha256}</code>{record.created_at ? ` · ${new Date(record.created_at).toLocaleString(i18n.language)}` : ''}</p>
        </article>;
      })}
    </section>
  </div>;
}
