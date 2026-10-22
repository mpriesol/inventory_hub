import React, { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { hubRequest } from '../api/access';
import { Button } from './ui/Button.new';

type SavedResult = { request_id: string; product_id: number; old_code: string; new_code: string; status: string; completed_at?: string };
const requestIdPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** Read-only results of the retired supplier-code conversion. */
export function ZookeeCodeReconciliation() {
  const { t } = useTranslation();
  const [results, setResults] = useState<SavedResult[] | null>(null), [busy, setBusy] = useState(false), [error, setError] = useState('');
  const [requestId, setRequestId] = useState('');
  const running = useRef(false), mounted = useRef(true), controller = useRef<AbortController | null>(null);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; controller.current?.abort(); }; }, []);
  async function load(originalId?: string) {
    if (running.current || (originalId && !requestIdPattern.test(originalId))) return;
    running.current = true; controller.current = new AbortController(); setBusy(true); setError('');
    try {
      if (originalId) {
        const value = await hubRequest<SavedResult>(`/api/supplier-availability/zookee/legacy-code-results/${originalId}`, undefined, controller.current.signal);
        if (value.request_id.toLowerCase() !== originalId.toLowerCase()) throw new Error('result_mismatch');
        if (mounted.current) setResults(previous => [value, ...(previous || []).filter(item => item.request_id !== value.request_id)]);
      } else {
        const value = await hubRequest<{ retired: boolean; results: SavedResult[] }>('/api/supplier-availability/zookee/legacy-codes', undefined, controller.current.signal);
        if (!value.retired || !Array.isArray(value.results)) throw new Error('history_unavailable');
        if (mounted.current) setResults(value.results);
      }
    } catch (value) {
      if (mounted.current) setError(t((value as { code?: string }).code === 'code_rename_result_missing' ? 'suppliers.codeRename.missing' : 'suppliers.codeRename.failed'));
    } finally {
      running.current = false;
      if (mounted.current) setBusy(false);
    }
  }
  return <details className="border rounded-lg p-3 space-y-3">
    <summary>{t('suppliers.codeRename.title')}</summary>
    <p className="text-sm">{t('suppliers.codeRename.help')}</p>
    <Button data-testid="zookee-history-load" variant="secondary" disabled={busy} onClick={() => load()}>{t('suppliers.codeRename.load')}</Button>
    <label className="block">{t('suppliers.codeRename.requestId')}<input data-testid="zookee-history-id" value={requestId} maxLength={36} disabled={busy} onChange={event => setRequestId(event.target.value.trim())} /></label>
    <Button data-testid="zookee-history-recover" variant="secondary" disabled={busy || !requestIdPattern.test(requestId)} onClick={() => load(requestId)}>{t('suppliers.codeRename.recover')}</Button>
    {error && <p role="alert">{error}</p>}
    {results && (results.length ? <div className="max-h-64 overflow-auto"><table className="w-full text-sm"><tbody>{results.map(row => <tr key={row.request_id}>
      <td>{row.old_code === row.new_code ? row.old_code : <>{row.old_code} → {row.new_code}</>}<small className="block">{row.request_id}</small></td>
      <td>{t(`suppliers.codeRename.results.${row.status}`, { defaultValue: row.status })}{row.completed_at && <small className="block">{row.completed_at}</small>}</td>
    </tr>)}</tbody></table></div> : <p role="status">{t('suppliers.codeRename.empty')}</p>)}
  </details>;
}
