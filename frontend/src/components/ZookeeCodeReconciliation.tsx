import React, { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { hubRequest } from '../api/access';
import { Button } from './ui/Button.new';

type Row = { product_id: number; old_code: string; new_code: string; name: string };

/** Administrative GET verification + local identity reconciliation, never a shop PUT. */
export function ZookeeCodeReconciliation() {
  const { t } = useTranslation();
  const [rows, setRows] = useState<Row[] | null>(null), [busy, setBusy] = useState(false), [error, setError] = useState('');
  const [results, setResults] = useState<Record<number, string>>({});
  const requestIds = useRef(new Map<number, string>()), running = useRef(false);
  const mounted = useRef(true), controller = useRef<AbortController | null>(null);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; controller.current?.abort(); }; }, []);
  async function load() {
    if (running.current) return;
    running.current = true; controller.current = new AbortController(); setBusy(true); setError('');
    try { const value = await hubRequest<{ items: Row[] }>('/api/supplier-availability/zookee/legacy-codes', undefined, controller.current.signal); if (mounted.current) { setRows(value.items); setResults({}); } }
    catch { setError(t('suppliers.codeRename.failed')); }
    finally { running.current = false; setBusy(false); }
  }
  async function reconcile() {
    if (running.current || !rows) return;
    running.current = true; controller.current = new AbortController(); setBusy(true); setError('');
    try {
      for (const row of rows) {
        if (!mounted.current) break;
        if (results[row.product_id] === 'completed') continue;
        if (!requestIds.current.has(row.product_id)) requestIds.current.set(row.product_id, crypto.randomUUID());
        try {
          await hubRequest(`/api/supplier-availability/zookee/legacy-codes/${row.product_id}`, { request_id: requestIds.current.get(row.product_id) }, controller.current.signal);
          if (mounted.current) setResults(previous => ({ ...previous, [row.product_id]: 'completed' }));
        } catch (value) {
          const code = (value as { code?: string }).code || 'failed';
          if (mounted.current) setResults(previous => ({ ...previous, [row.product_id]: code }));
        }
      }
    } finally { running.current = false; setBusy(false); }
  }
  return <details className="border rounded-lg p-3 space-y-3">
    <summary>{t('suppliers.codeRename.title')}</summary>
    <p className="text-sm">{t('suppliers.codeRename.help')}</p>
    <Button variant="secondary" disabled={busy} onClick={load}>{t('suppliers.codeRename.load')}</Button>
    {error && <p role="alert">{error}</p>}
    {rows && <>
      <div className="max-h-64 overflow-auto"><table className="w-full text-sm"><tbody>{rows.map(row => <tr key={row.product_id}>
        <td>{row.old_code} → {row.new_code}</td><td>{results[row.product_id] ? t(`suppliers.codeRename.results.${results[row.product_id]}`, { defaultValue: t('suppliers.codeRename.notVerified') }) : row.name}</td>
      </tr>)}</tbody></table></div>
      <p role="status">{t('suppliers.codeRename.progress', { done: Object.values(results).filter(value => value === 'completed').length, total: rows.length })}</p>
      {rows.length > 0 && <Button disabled={busy} onClick={reconcile}>{busy ? t('common.loading') : t('suppliers.codeRename.apply')}</Button>}
    </>}
  </details>;
}
