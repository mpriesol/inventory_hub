import React, { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { accessRevision, hubUnlocked, hubUser } from '../../api/access';
import { configureOrderWebhooks, getOrderWebhookStatus, OrderWebhookMutation, OrderWebhookStatus, registerOrderWebhooks, resolveOrderWebhookRegistration, retryOrderWebhooks } from '../../api/orderWebhooks';
import { Button } from '../ui/Button.new';
import { ActionScope } from '../ui/ActionScope';

function validBaseUrl(value: string) {
  try {
    const url = new URL(value);
    return value === value.trim() && !/[\s\x00-\x1f\x7f]/.test(value)
      && url.protocol === 'https:' && (!url.port || url.port === '443')
      && !url.username && !url.password && !url.search && !url.hash
      && !url.pathname.includes('//') && !url.pathname.includes('%') && !value.includes('/..');
  } catch { return false; }
}

function checkedStatus(value: OrderWebhookStatus, shop: string) {
  if (!value || value.shop?.code !== shop || typeof value.collector_enabled !== 'boolean'
      || typeof value.connection_matches !== 'boolean' || value.external_write_enabled !== false
      || !Number.isSafeInteger(value.pending_count) || value.pending_count < 0
      || !Number.isSafeInteger(value.review_count) || value.review_count < 0 || !Array.isArray(value.events)
      || (value.webhook !== null && (!value.webhook || !Number.isSafeInteger(value.webhook.revision)
        || value.webhook.revision < 1 || typeof value.webhook.enabled !== 'boolean'
        || !validBaseUrl(value.webhook.public_base_url)
        || !['pending', 'running', 'registered', 'failed', 'disabled'].includes(value.webhook.registration_status)
        || !Number.isSafeInteger(value.webhook.catchup_interval_seconds)))) throw new Error('Invalid webhook status');
  const mutations = value.webhook?.pending_mutations;
  if (mutations !== undefined && (!Array.isArray(mutations) || mutations.length > 3
      || new Set(mutations.map(item => item?.event)).size !== mutations.length
      || mutations.some(item => !item || !['create', 'update', 'delete'].includes(item.event)
        || !['POST', 'PUT'].includes(item.method) || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(item.intent_id)
        || !Number.isFinite(Date.parse(item.started_at))))) throw new Error('Invalid webhook recovery state');
  return value;
}

/** Changes integration delivery only; collection and stock authority keep their own controls. */
export function OrderWebhooksPanel({ shop, blocked, onBusy }: {
  shop: string; blocked: boolean; onBusy: (busy: boolean) => void;
}) {
  const { t, i18n } = useTranslation();
  const [status, setStatus] = useState<OrderWebhookStatus | null>(null);
  const [baseUrl, setBaseUrl] = useState(() => `${window.location.origin}/api`);
  const [interval, setInterval] = useState('3600'), [enabled, setEnabled] = useState(false);
  const [busy, setBusy] = useState(false), [uncertain, setUncertain] = useState(false), [error, setError] = useState('');
  const [saved, setSaved] = useState(false);
  const [resolutions, setResolutions] = useState<Record<string, { note: string; finished: boolean }>>({});
  const active = useRef(false), generation = useRef(0), controller = useRef<AbortController | null>(null);
  const text = (key: string) => t(`orderWebhooks.${key}`);
  const date = (value?: string | null) => value ? new Date(value).toLocaleString(i18n.language) : '—';
  const operator = hubUser()?.role === 'operator';
  const locked = busy || blocked || operator;
  const settings = status?.webhook;
  const dirty = !!status && (enabled !== (settings?.enabled ?? false)
    || baseUrl !== (settings?.public_base_url ?? `${window.location.origin}/api`)
    || interval !== String(settings?.catchup_interval_seconds ?? 3600));
  const valid = validBaseUrl(baseUrl.trim()) && /^\d+$/.test(interval)
    && Number.isSafeInteger(Number(interval)) && Number(interval) >= 300 && Number(interval) <= 86400;
  const ready = !!status?.collector_enabled && !!status.connection_matches;
  const registrationBusy = ['pending', 'running'].includes(settings?.registration_status || '');
  useEffect(() => () => { generation.current++; controller.current?.abort(); onBusy(false); }, []);

  function resolutionReady(mutation: OrderWebhookMutation) {
    const value = resolutions[mutation.intent_id];
    return !!value?.finished && value.note.trim().length >= 10 && value.note.trim().length <= 2000
      && !/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/.test(value.note);
  }
  function editResolution(id: string, changes: Partial<{ note: string; finished: boolean }>) {
    setResolutions(previous => ({ ...previous, [id]: { note: '', finished: false, ...previous[id], ...changes } }));
  }
  async function run(kind: 'load' | 'save' | 'register' | 'retry' | 'resolve', mutation?: OrderWebhookMutation) {
    if (active.current || blocked || operator || !hubUnlocked()) return;
    if (kind !== 'load' && (!status || uncertain)) return;
    if (kind === 'save' && (!valid || (enabled && !ready))) return;
    if (kind === 'register' && (!settings || dirty || !status?.connection_matches || registrationBusy)) return;
    if (kind === 'retry' && (!settings?.enabled || dirty || !ready || registrationBusy)) return;
    if (kind === 'resolve' && (!mutation || !settings || dirty || !resolutionReady(mutation)
        || !settings.pending_mutations?.some(item => item.intent_id === mutation.intent_id && item.event === mutation.event))) return;
    const context = generation.current, credential = accessRevision();
    const abort = new AbortController(); controller.current = abort;
    const current = () => context === generation.current && credential === accessRevision() && !abort.signal.aborted;
    active.current = true; setBusy(true); onBusy(true); setError(''); setSaved(false);
    try {
      const result = checkedStatus(await (kind === 'load' ? getOrderWebhookStatus(shop, abort.signal)
        : kind === 'save' ? configureOrderWebhooks({ shop_code: shop, expected_revision: settings?.revision ?? null,
          confirmed: true, enabled, public_base_url: baseUrl.trim(), catchup_interval_seconds: Number(interval) })
        : kind === 'register' ? registerOrderWebhooks(shop, settings!.revision)
        : kind === 'resolve' ? resolveOrderWebhookRegistration(shop, settings!.revision, mutation!, resolutions[mutation!.intent_id].note.trim())
        : retryOrderWebhooks(shop, settings!.revision)), shop);
      if (current()) {
        setStatus(result); setEnabled(result.webhook?.enabled ?? false);
        setBaseUrl(result.webhook?.public_base_url ?? `${window.location.origin}/api`);
        setInterval(String(result.webhook?.catchup_interval_seconds ?? 3600)); setUncertain(false);
        setResolutions({});
        setSaved(kind !== 'load');
      }
    } catch (value) {
      if (current()) {
        setError((value as { code?: string }).code || 'request_failed');
        if (kind !== 'load') setUncertain(true);
      }
    } finally { if (current()) { active.current = false; setBusy(false); onBusy(false); } }
  }

  return <section className="opening-stock-panel" data-testid="order-webhooks-panel">
    <div className="opening-stock-actions"><h2>{text('title')}</h2>
      <span className="action-control"><Button data-testid="order-webhooks-load" variant="secondary" disabled={locked} onClick={() => run('load')}>{text('load')}</Button><ActionScope effects={['hub-read']} /></span></div>
    <p>{text('help')}</p><p className="opening-stock-muted">{text('scope')}</p>
    {operator && <p className="opening-stock-notice">{text('adminRequired')}</p>}
    {uncertain && <p role="alert" className="opening-stock-notice">{text('uncertain')}</p>}
    {error && <p role="alert" className="opening-stock-errors">{t(`orderWebhooks.errors.${error}`, { defaultValue: text('requestFailed') })}</p>}
    {status && <>
      {!ready && <p data-testid="order-webhooks-prerequisite" className="opening-stock-notice">{text(status.collector_enabled ? 'connectionRequired' : 'collectionRequired')}</p>}
      <div className="orders-inbox-status"><strong>{text(settings?.enabled ? 'enabled' : 'disabled')}</strong><span data-testid="order-webhooks-registration">{text(`registration.${settings?.registration_status || 'disabled'}`)}</span></div>
      <div className="opening-stock-fields">
        <label>{text('baseUrl')}<input data-testid="order-webhooks-base-url" type="url" autoComplete="off" maxLength={500} value={baseUrl} disabled={locked || uncertain} onChange={event => { setBaseUrl(event.target.value); setSaved(false); }} /><small>{text('baseUrlHelp')}</small></label>
        <label>{text('catchupInterval')}<input data-testid="order-webhooks-interval" type="number" min="300" max="86400" step="1" value={interval} disabled={locked || uncertain} onChange={event => { setInterval(event.target.value); setSaved(false); }} /><small>{text('catchupHelp')}</small></label>
      </div>
      <div className="opening-stock-confirmations"><label><input data-testid="order-webhooks-enabled" type="checkbox" checked={enabled} disabled={locked || uncertain || (!ready && !enabled)} onChange={event => { setEnabled(event.target.checked); setSaved(false); }} />{text('enableLabel')}</label></div>
      {dirty && <p className="opening-stock-muted" role="status">{text('unsaved')}</p>}
      {saved && <p role="status" className="opening-stock-success">{text('saved')}</p>}
      <div className="opening-stock-actions">
        <span className="action-control"><Button data-testid="order-webhooks-save" disabled={locked || uncertain || !status || !valid || (enabled && !ready) || (!dirty && !!settings)} onClick={() => run('save')}>{text('save')}</Button><ActionScope effects={['hub-write', 'queued-upgates', 'upgates-read', 'upgates-write']} shop={shop} calls={{ kind: 'variable' }} /></span>
        {settings && <span className="action-control"><Button data-testid="order-webhooks-register" variant="secondary" disabled={locked || uncertain || dirty || !status.connection_matches || registrationBusy} onClick={() => run('register')}>{text('register')}</Button><ActionScope effects={['hub-write', 'queued-upgates', 'upgates-read', 'upgates-write']} shop={shop} calls={{ kind: 'variable' }} /></span>}
      </div>
      {settings && <>
        <dl className="opening-stock-summary"><div><dt>{text('pending')}</dt><dd data-testid="order-webhooks-pending">{status.pending_count}</dd></div><div><dt>{text('review')}</dt><dd data-testid="order-webhooks-review">{status.review_count}</dd></div><div><dt>{text('lastReceived')}</dt><dd>{date(settings.last_received_at)}</dd></div></dl>
        <p className="opening-stock-muted">{text('registeredAt')}: {date(settings.registered_at)} · {text('lastCompleted')}: {date(settings.last_completed_at)}</p>
        {settings.last_error && <p className="opening-stock-errors" role="alert">{t(`orderWebhooks.errors.${settings.last_error}`, { defaultValue: text('requestFailed') })}</p>}
        {!!settings.pending_mutations?.length && <section data-testid="order-webhooks-resolutions" className="opening-stock-notice">
          <h3>{text('resolutionTitle')}</h3><p>{text('resolutionHelp')}</p>
          {settings.pending_mutations.map(mutation => <div key={mutation.intent_id} data-testid={`order-webhooks-resolution-${mutation.event}`}>
            <p><strong>{text(`eventsNames.${mutation.event}`)}</strong> · {date(mutation.started_at)}</p>
            <label>{text('resolutionNote')}<textarea data-testid={`order-webhooks-resolution-${mutation.event}-note`} rows={2} maxLength={2000} disabled={locked || uncertain || dirty}
              value={resolutions[mutation.intent_id]?.note || ''} onChange={event => editResolution(mutation.intent_id, { note: event.target.value })} /></label>
            <div className="opening-stock-confirmations"><label><input data-testid={`order-webhooks-resolution-${mutation.event}-finished`} type="checkbox" checked={resolutions[mutation.intent_id]?.finished || false}
              disabled={locked || uncertain || dirty} onChange={event => editResolution(mutation.intent_id, { finished: event.target.checked })} />{text('resolutionConfirm')}</label></div>
            <span className="action-control"><Button data-testid={`order-webhooks-resolution-${mutation.event}-submit`} variant="secondary"
              disabled={locked || uncertain || dirty || !resolutionReady(mutation)} onClick={() => run('resolve', mutation)}>{text('resolutionSubmit')}</Button><ActionScope effects={['hub-write', 'queued-upgates', 'upgates-read', 'upgates-write']} shop={shop} calls={{ kind: 'variable' }} /></span>
          </div>)}
        </section>}
        {status.review_count > 0 && <span className="action-control"><Button data-testid="order-webhooks-retry" variant="secondary" disabled={locked || uncertain || dirty || !settings.enabled || !ready || registrationBusy} onClick={() => run('retry')}>{text('retry')}</Button><ActionScope effects={['hub-write', 'queued-upgates', 'upgates-read']} shop={shop} calls={{ kind: 'variable' }} /></span>}
        {status.events.length > 0 && <details><summary>{text('events')}</summary><div className="opening-stock-table-scroll"><table><thead><tr><th>{t('orderCollection.orderNumber')}</th><th>{text('event')}</th><th>{text('state')}</th><th>{text('lastReceived')}</th><th>{t('orderCollection.runError')}</th></tr></thead><tbody>{status.events.map(event => <tr key={event.order_number}><td>{event.order_number}</td><td>{text(`eventsNames.${event.event}`)}</td><td>{t(`orderWebhooks.states.${event.status}`, { defaultValue: event.status })}</td><td>{date(event.last_received_at)}</td><td>{event.last_error ? t(`orderWebhooks.errors.${event.last_error}`, { defaultValue: text('requestFailed') }) : '—'}</td></tr>)}</tbody></table></div></details>}
      </>}
    </>}
  </section>;
}
