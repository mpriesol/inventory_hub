import React, { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { accessRevision, hubUnlocked, subscribeAccess } from '../api/access';
import { actOnAttention, AttentionItem, AttentionPage, AttentionRule, AttentionState, getAttention, getAttentionRules, saveAttentionRule } from '../api/attention';
import { supplierAvailabilityErrorCode } from '../api/supplierAvailability';
import { Button } from './ui/Button.new';
import { HelpHint } from './ui/HelpHint';
import './AttentionCenter.css';

export function AttentionDetails({ item }: { item: AttentionItem }) {
  const { t, i18n } = useTranslation();
  const data = item.details, number = (v: number) => v.toLocaleString(i18n.language);
  return <>
    {item.kind === 'supplier_availability.partial' && <p>{t('attention.partialSummary', {
      accepted: number(data.accepted || 0), rejected: number(data.rejected || 0), unknown: number(data.unknown || 0),
    })}</p>}
    {(data.coverage || []).map(check => <p key={check.target}>{t('operationLogs.coverageFailure', {
      source: t(`operationLogs.targets.${check.target}`, { defaultValue: check.target }), actual: number(check.rows),
      previous: number(check.previous_rows), percent: check.min_coverage_percent, required: number(check.required_rows),
      missing: number(Math.max(0, check.required_rows - check.rows)),
    })}</p>)}
    {item.source !== 'supplier_availability' && <p>{t(`attention.messages.${item.kind}`, { ...data, defaultValue: data.error_code || item.kind })}</p>}
    {data.error_code && <p>{t(`availabilitySync.errors.${supplierAvailabilityErrorCode(item.scope, data.error_code)}`, { defaultValue: data.error_code })}</p>}
    {!!data.warnings?.length && <details><summary>{t('attention.reasons')}</summary><ul>{data.warnings.map(code =>
      <li key={code}>{t(`availabilitySync.errors.${code}`, { defaultValue: code })}</li>)}</ul></details>}
  </>;
}

function Center({ compact }: { compact: boolean }) {
  const { t, i18n } = useTranslation();
  const [state, setState] = useState<AttentionState>('active'), [source, setSource] = useState(''), [offset, setOffset] = useState(0);
  const [page, setPage] = useState<AttentionPage | null>(null), [rules, setRules] = useState<AttentionRule[]>([]);
  const [tab, setTab] = useState<'events' | 'rules'>('events');
  const [busy, setBusy] = useState(false), [error, setError] = useState('');
  const [editor, setEditor] = useState<AttentionRule | null>(null), [context, setContext] = useState<AttentionItem | null>(null);
  const controller = useRef<AbortController | null>(null), generation = useRef(0);
  const waiting = useRef(false), editing = useRef(false); waiting.current = busy; editing.current = !!editor;
  const Heading = compact ? 'h2' : 'h1';
  const title = (kind: string) => t(`attention.kinds.${kind}`, { defaultValue: kind });
  async function load() {
    controller.current?.abort(); const abort = new AbortController(), version = ++generation.current, access = accessRevision();
    controller.current = abort; setBusy(true); setError(''); setPage(null);
    try {
      const [data, policies] = await Promise.all([
        getAttention(new URLSearchParams({ state, ...(source ? { source } : {}), offset: String(offset), limit: compact ? '5' : '20' }), abort.signal),
        getAttentionRules(abort.signal),
      ]);
      if (!abort.signal.aborted && version === generation.current && access === accessRevision()) { setPage(data); setRules(policies.items); }
    } catch (value) { if (!abort.signal.aborted && version === generation.current) setError((value as { code?: string }).code || 'attention_load_failed'); }
    finally { if (!abort.signal.aborted && version === generation.current) setBusy(false); }
  }
  useEffect(() => {
    void load();
    const timer = window.setInterval(() => { if (document.visibilityState === 'visible' && !waiting.current && !editing.current) void load(); }, 60000);
    return () => { window.clearInterval(timer); generation.current++; controller.current?.abort(); };
  }, [state, source, offset]);
  async function write(operation: (signal: AbortSignal) => Promise<unknown>) {
    if (waiting.current) return;
    waiting.current = true;
    controller.current?.abort(); const abort = new AbortController(), version = ++generation.current, access = accessRevision();
    controller.current = abort; setBusy(true); setError('');
    try {
      await operation(abort.signal);
      if (!abort.signal.aborted && version === generation.current && access === accessRevision()) { setEditor(null); setContext(null); await load(); }
    } catch (value) {
      if (!abort.signal.aborted && version === generation.current) {
        setError((value as { code?: string }).code || 'attention_write_failed'); setBusy(false);
      }
    }
  }
  function edit(kind: string, scope: string, item: AttentionItem | null) {
    const existing = rules.find(rule => rule.kind === kind && rule.scope === scope);
    const inherited = rules.find(rule => rule.kind === kind && rule.scope === '*');
    setContext(item); setEditor(existing || { kind, scope, revision: 0, enabled: inherited?.enabled ?? true, min_occurrences: inherited?.min_occurrences ?? 1 });
  }
  return <section className="attention-center" data-testid={compact ? 'attention-dashboard' : 'attention-center'}>
    <header><div><Heading>{t('attention.title')}{page && state === 'active' ? ` (${page.total})` : ''}
      <HelpHint label={t('attention.helpLabel')}>{t('attention.help')}</HelpHint></Heading></div>
      {compact ? <Link to="/settings/attention">{t('attention.all')}</Link> : <Link to="/">{t('dashboard.title')}</Link>}
    </header>
    <div className="attention-toolbar">
      {!compact && <><Button variant={tab === 'events' ? 'primary' : 'secondary'} onClick={() => setTab('events')}>{t('attention.events')}</Button>
        <Button data-testid="attention-rules" variant={tab === 'rules' ? 'primary' : 'secondary'} onClick={() => setTab('rules')}>{t('attention.rules')}</Button>
        {tab === 'events' && <><label>{t('attention.state')}<select data-testid="attention-state" value={state} disabled={busy} onChange={e => { setState(e.target.value as AttentionState); setOffset(0); }}>
          {(['active', 'ignored', 'muted', 'resolved'] as const).map(v => <option key={v} value={v}>{t(`attention.states.${v}`)}</option>)}</select></label>
          <label>{t('attention.source')}<select value={source} disabled={busy} onChange={e => { setSource(e.target.value); setOffset(0); }}>
            <option value="">{t('attention.allSources')}</option>{(page?.sources || []).map(v => <option key={v} value={v}>{t(`attention.sources.${v}`, { defaultValue: v })}</option>)}</select></label></>}
      </>}
      <Button variant="secondary" data-testid="attention-refresh" disabled={busy} onClick={() => { setEditor(null); setContext(null); void load(); }}>{t('attention.refresh')}</Button>
    </div>
    {busy && <p role="status">{t('attention.loading')}</p>}
    {error && <p role="alert">{t(`attention.errors.${error}`, { defaultValue: t('attention.errors.attention_write_failed') })}</p>}
    {editor && <form className="attention-rule" onSubmit={e => { e.preventDefault(); if (Number.isInteger(editor.min_occurrences) && editor.min_occurrences >= 1 && editor.min_occurrences <= 100) void write(signal => saveAttentionRule(editor, signal)); }}>
      <h3>{title(editor.kind)}</h3>
      {context ? <label>{t('attention.appliesTo')}<select data-testid="attention-rule-scope" value={editor.scope} disabled={busy} onChange={e => edit(editor.kind, e.target.value, context)}>
        <option value={context.scope}>{context.scope}</option><option value="*">{t('attention.allSources')}</option></select></label>
        : <p>{editor.scope === '*' ? t('attention.allSources') : editor.scope}</p>}
      <label><input data-testid="attention-rule-enabled" type="checkbox" checked={editor.enabled} disabled={busy} onChange={e => setEditor({ ...editor, enabled: e.target.checked })} />{t('attention.enabled')}</label>
      <label>{t('attention.minimum')}<input data-testid="attention-rule-minimum" type="number" min="1" max="100" value={editor.min_occurrences} disabled={busy} onChange={e => setEditor({ ...editor, min_occurrences: Number(e.target.value) })} /></label>
      {context?.settings_link && <Link to={context.settings_link}>{t('attention.sourceSettings')}</Link>}
      <div className="attention-actions"><Button type="submit" data-testid="attention-rule-save" disabled={busy || !Number.isInteger(editor.min_occurrences) || editor.min_occurrences < 1 || editor.min_occurrences > 100}>{t('attention.save')}</Button>
        <Button type="button" variant="secondary" disabled={busy} onClick={() => { setEditor(null); setContext(null); }}>{t('attention.cancel')}</Button></div>
    </form>}
    {tab === 'rules' && !compact ? <ul className="attention-list">{rules.map(rule => <li key={`${rule.kind}:${rule.scope}`}>
      <strong>{title(rule.kind)}</strong> · {rule.scope === '*' ? t('attention.allSources') : rule.scope} · {t(rule.enabled ? 'attention.enabled' : 'attention.disabled')}
      <small>{t('attention.repeatFrom', { count: rule.min_occurrences })}</small>
      <Button variant="secondary" disabled={busy} onClick={() => edit(rule.kind, rule.scope, null)}>{t('attention.configure')}</Button>
    </li>)}</ul> : <>
      {page && !page.items.length && <p>{t(`attention.empty.${state}`)}</p>}
      <ul className="attention-list">{page?.items.map(item => <li key={item.id} data-testid={`attention-item-${item.id}`} data-severity={item.severity}>
        <div className="attention-item-heading"><strong>{item.scope} · {title(item.kind)}</strong><span>{t(`attention.severity.${item.severity}`)}</span></div>
        <AttentionDetails item={item} />
        <small>{new Date(item.last_seen).toLocaleString(i18n.language)} · {t('attention.occurrences', { count: item.occurrences })}</small>
        <div className="attention-actions"><Link to={item.link}>{t('attention.openService')}</Link><Link to={item.log_link}>{t('attention.log')}</Link>
          {item.active && <Button data-testid={`attention-ignore-${item.id}`} variant="secondary" disabled={busy} onClick={() => void write(signal => actOnAttention(item, item.ignored_at ? 'restore' : 'ignore', signal))}>{t(item.ignored_at ? 'attention.restore' : 'attention.ignore')}</Button>}
          <Button data-testid={`attention-configure-${item.id}`} variant="secondary" disabled={busy} onClick={() => edit(item.kind, item.scope, item)}>{t('attention.configure')}</Button></div>
      </li>)}</ul>
      {!compact && page && <div className="attention-actions"><Button variant="secondary" disabled={busy || offset === 0} onClick={() => setOffset(Math.max(0, offset - 20))}>{t('availabilitySync.previousPage')}</Button>
        <span>{offset + (page.items.length ? 1 : 0)}–{offset + page.items.length} / {page.total}</span>
        <Button variant="secondary" disabled={busy || offset + page.items.length >= page.total} onClick={() => setOffset(offset + 20)}>{t('availabilitySync.nextPage')}</Button></div>}
    </>}
  </section>;
}

export function AttentionCenter({ compact = false }: { compact?: boolean }) {
  const revision = useSyncExternalStore(subscribeAccess, accessRevision);
  const { t } = useTranslation();
  return hubUnlocked() ? <Center key={revision} compact={compact} /> : compact ? null : <p>{t('availabilitySync.errors.hub_access_required')}</p>;
}
