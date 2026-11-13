import React, { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { accessRevision, hubUnlocked, subscribeAccess } from '../api/access';
import { getOperationLogs, OperationLogPage } from '../api/operationLogs';
import { Button } from '../components/ui/Button.new';
import { HelpHint } from '../components/ui/HelpHint';
import './OperationLogsPage.css';

const fields = ['component', 'supplier', 'level', 'run_id', 'request_id', 'since', 'until'] as const;
type Filters = Record<typeof fields[number], string>;
function fromSearch(search: URLSearchParams): Filters {
  return Object.fromEntries(fields.map(key => {
    const value = search.get(key) || '';
    if ((key === 'since' || key === 'until') && value) {
      const date = new Date(value);
      return [key, Number.isFinite(date.getTime()) ? new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16) : ''];
    }
    return [key, value];
  })) as Filters;
}

function LogContent() {
  const { t, i18n } = useTranslation();
  const [search, setSearch] = useSearchParams();
  const [filters, setFilters] = useState<Filters>(() => fromSearch(search));
  const [result, setResult] = useState<OperationLogPage | null>(null), [busy, setBusy] = useState(false), [error, setError] = useState('');
  const abort = useRef<AbortController | null>(null), generation = useRef(0);
  async function load(params: URLSearchParams) {
    abort.current?.abort();
    const controller = new AbortController(), version = ++generation.current, access = accessRevision();
    abort.current = controller; setBusy(true); setError(''); setResult(null);
    const current = () => !controller.signal.aborted && generation.current === version && accessRevision() === access;
    try { const value = await getOperationLogs(params, controller.signal); if (current()) setResult(value); }
    catch (value) { if (current()) setError((value as { code?: string }).code || 'request_failed'); }
    finally { if (current()) setBusy(false); }
  }
  const query = search.toString();
  useEffect(() => { setFilters(fromSearch(new URLSearchParams(query))); void load(new URLSearchParams(query)); return () => { generation.current++; abort.current?.abort(); }; }, [query]);
  function apply(event: React.FormEvent) {
    event.preventDefault();
    const params = new URLSearchParams();
    for (const key of fields) if (filters[key]) params.set(key, key === 'since' || key === 'until' ? new Date(filters[key]).toISOString() : filters[key].trim());
    if (params.toString() === query) void load(params); else setSearch(params);
  }
  function next() { if (result?.next_cursor) { const params = new URLSearchParams(search); params.set('before', result.next_cursor); setSearch(params); } }
  function refresh() { const params = new URLSearchParams(search); params.delete('before'); if (params.toString() === query) void load(params); else setSearch(params); }
  return <div className="operation-logs">
    <header><h1>{t('operationLogs.title')}<HelpHint label={t('operationLogs.helpLabel')}>{t('operationLogs.help')}</HelpHint></h1><Link to="/settings">{t('ai.settings')}</Link></header>
    <form className="operation-log-filters" onSubmit={apply}>
      {fields.map(key => <label key={key}>{t(`operationLogs.fields.${key}`)}
        {key === 'level' || key === 'component' ? <select data-testid={`log-${key}`} value={filters[key]} onChange={event => setFilters({ ...filters, [key]: event.target.value })}>
          <option value="">{t('operationLogs.all')}</option>{(key === 'level' ? ['info', 'warning', 'error'] : ['api', 'husky', 'supplier_availability']).map(value => <option key={value} value={value}>{t(`operationLogs.${key}.${value}`)}</option>)}
        </select> : <input data-testid={`log-${key}`} value={filters[key]} type={key === 'run_id' ? 'number' : key === 'since' || key === 'until' ? 'datetime-local' : 'text'} min={key === 'run_id' ? 1 : undefined}
          maxLength={key === 'request_id' ? 32 : 100}
          onChange={event => setFilters({ ...filters, [key]: event.target.value })} />}
      </label>)}
      <Button type="submit" data-testid="log-apply" disabled={busy}>{t('operationLogs.apply')}</Button>
      <Button type="button" variant="secondary" data-testid="log-reset" disabled={busy} onClick={() => { setFilters(Object.fromEntries(fields.map(key => [key, ''])) as Filters); setSearch({}); }}>{t('operationLogs.reset')}</Button>
    </form>
    <div className="operation-log-toolbar"><Button variant="secondary" data-testid="log-refresh" disabled={busy} onClick={refresh}>{t('operationLogs.refresh')}</Button>
      <Button variant="secondary" data-testid="log-next" disabled={busy || !result?.next_cursor} onClick={next}>{t('operationLogs.older')}</Button>
      {result && <small>{t('operationLogs.retention', { mb: result.max_bytes / 1_000_000 })}</small>}</div>
    {error && <p role="alert">{t(`operationLogs.errors.${error}`, { defaultValue: t('availabilitySync.requestFailed') })}</p>}
    {busy && <p role="status">{t('operationLogs.loading')}</p>}
    {result && !result.items.length && <p>{t('operationLogs.empty')}</p>}
    {!!result?.items.length && <div className="operation-log-table"><table><thead><tr>{['time', 'operation', 'result', 'duration', 'context'].map(key => <th key={key}>{t(`operationLogs.columns.${key}`)}</th>)}</tr></thead>
      <tbody>{result.items.map(item => <tr key={item.id} data-level={item.level}>
        <td><time dateTime={item.timestamp}>{new Date(item.timestamp).toLocaleString(i18n.language)}</time></td>
        <td><strong>{t(`operationLogs.events.${item.event}`, { defaultValue: item.event })}</strong><small>{item.component}{item.supplier ? ` · ${item.supplier}` : ''}</small>{item.target && <code>{item.method} {item.target}</code>}</td>
        <td><span>{t(`operationLogs.level.${item.level}`)}{item.status_code ? ` · HTTP ${item.status_code}` : ''}</span>{item.error_code && <small>{t(`availabilitySync.errors.${item.error_code}`, { defaultValue: item.error_code })}</small>}{item.reason && <code>{item.reason}</code>}</td>
        <td>{item.duration_ms == null ? '—' : `${item.duration_ms} ms`}</td>
        <td>{item.run_id && <span>{t('operationLogs.run', { id: item.run_id })}</span>}{item.phase && <small>{t(`operationLogs.phases.${item.phase}`, { defaultValue: item.phase })}</small>}{item.page != null && <span>{t('operationLogs.page', { page: item.page })}</span>}{item.rows != null && <small>{t('operationLogs.rows', { count: item.rows })}</small>}{item.observed_page != null && <small>{t('operationLogs.observedPage', { page: item.observed_page })}</small>}{item.request_id && <details><summary>{t('operationLogs.request')}</summary><code>{item.request_id}</code></details>}</td>
      </tr>)}</tbody></table></div>}
  </div>;
}

export function OperationLogsPage() {
  const revision = useSyncExternalStore(subscribeAccess, accessRevision);
  const { t } = useTranslation();
  return hubUnlocked() ? <LogContent key={revision} /> : <p>{t('availabilitySync.errors.hub_access_required')}</p>;
}
