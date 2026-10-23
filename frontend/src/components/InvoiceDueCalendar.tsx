import React, { useEffect, useId, useMemo, useState, useSyncExternalStore } from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { ChevronLeft, ChevronRight } from 'lucide-react';
import { accessRevision, subscribeAccess } from '../api/access';
import { InvoiceArchiveFilters } from '../api/invoiceArchive';
import { getInvoiceCalendar, invoiceCalendarQuery, InvoiceCalendarItem, InvoiceCalendarResult, InvoiceCalendarTotal } from '../api/invoiceCalendar';
import { invoiceMoney } from '../pages/invoiceArchiveUi';
import { todayDate } from '../utils/format';
import './invoiceCalendar.css';

export type InvoiceCalendarView = 'week' | 'month' | 'year';
const DAY = 86400000;
export function calendarDate(value: Date) { return value.toISOString().slice(0, 10); }
function parsedDate(value: string) {
  const result = new Date(`${value}T12:00:00Z`);
  return /^\d{4}-\d{2}-\d{2}$/.test(value) && !Number.isNaN(result.valueOf()) && calendarDate(result) === value ? result : null;
}
function dayOffset(value: Date, amount: number) { return new Date(value.valueOf() + amount * DAY); }
function monthStart(value: Date, offset = 0) {
  const result = new Date(value.valueOf()); result.setUTCDate(1); result.setUTCMonth(result.getUTCMonth() + offset); return result;
}
function monday(value: Date) { return dayOffset(value, -((value.getUTCDay() + 6) % 7)); }
export function calendarPeriod(anchor: string, view: InvoiceCalendarView) {
  const value = parsedDate(anchor);
  if (!value) throw new Error('Invalid calendar date');
  let first: Date, last: Date;
  if (view === 'week') { first = monday(value); last = dayOffset(first, 6); }
  else if (view === 'month') { first = monthStart(value); last = dayOffset(monthStart(value, 1), -1); }
  else { first = monthStart(value); first.setUTCMonth(0); last = monthStart(first); last.setUTCFullYear(last.getUTCFullYear() + 1); last = dayOffset(last, -1); }
  return { from: calendarDate(first), to: calendarDate(last) };
}
export function moveCalendar(anchor: string, view: InvoiceCalendarView, offset: number) {
  const original = parsedDate(anchor);
  if (!original) throw new Error('Invalid calendar date');
  if (view === 'week') return calendarDate(dayOffset(original, offset * 7));
  const next = monthStart(original, view === 'month' ? offset : offset * 12);
  const end = dayOffset(monthStart(next, 1), -1).getUTCDate();
  next.setUTCDate(Math.min(original.getUTCDate(), end));
  return calendarDate(next);
}
function dateLabel(value: string, language: string, options: Intl.DateTimeFormatOptions) {
  const date = parsedDate(value);
  return date ? new Intl.DateTimeFormat(language === 'sk' ? 'sk-SK' : 'en-GB', { timeZone: 'UTC', ...options }).format(date) : '—';
}

export function InvoiceDueCalendar({ filters, refresh = 0 }: { filters: InvoiceArchiveFilters; refresh?: number }) {
  const { t, i18n } = useTranslation();
  const titleId = useId();
  const access = useSyncExternalStore(subscribeAccess, accessRevision);
  const [view, setView] = useState<InvoiceCalendarView>('month');
  const [anchor, setAnchor] = useState(todayDate);
  const [selectedDay, setSelectedDay] = useState(todayDate);
  const [data, setData] = useState<InvoiceCalendarResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [retry, setRetry] = useState(0);
  const period = calendarPeriod(anchor, view);
  const query = invoiceCalendarQuery(filters, period.from, period.to);
  useEffect(() => {
    const controller = new AbortController();
    setData(null); setLoading(true); setError('');
    getInvoiceCalendar(filters, period.from, period.to, controller.signal).then(value => {
      if (!controller.signal.aborted) setData(value);
    }).catch(reason => {
      if (!controller.signal.aborted) setError((reason as { code?: string })?.code === 'invoice_calendar_range_too_large' ? 'limit' : 'failed');
    }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [query, refresh, retry, access]);
  const grouped = useMemo(() => {
    const result = new Map<string, InvoiceCalendarItem[]>();
    (data?.items || []).forEach(item => { const rows = result.get(item.due_date) || []; rows.push(item); result.set(item.due_date, rows); });
    return result;
  }, [data]);
  const language = i18n.language.startsWith('sk') ? 'sk' : 'en';
  const today = todayDate();
  const locale = language === 'sk' ? 'sk-SK' : 'en-GB';
  const weekdays = Array.from({ length: 7 }, (_, index) => new Intl.DateTimeFormat(locale, { weekday: 'short', timeZone: 'UTC' }).format(dayOffset(new Date('2026-01-05T12:00:00Z'), index)));
  const fullDate = (value: string) => dateLabel(value, language, { day: 'numeric', month: 'long', year: 'numeric' });
  const title = view === 'year' ? anchor.slice(0, 4) : view === 'month'
    ? dateLabel(anchor, language, { month: 'long', year: 'numeric' }) : `${fullDate(period.from)} – ${fullDate(period.to)}`;
  const chooseDay = (value: string) => {
    setSelectedDay(value);
    setAnchor(value);
  };
  const changePeriod = (offset: number) => { const value = moveCalendar(anchor, view, offset); setAnchor(value); setSelectedDay(value); };
  const stateForDay = (items: InvoiceCalendarItem[]) => items.some(item => item.is_overdue) ? 'overdue'
    : items.every(item => item.payment_status === 'paid') ? 'paid' : items.some(item => ['unpaid', 'partial'].includes(item.payment_status)) ? 'unpaid' : 'unknown';
  const status = (item: InvoiceCalendarItem) => <span className={`calendar-status ${item.is_overdue ? 'overdue' : item.payment_status}`}>
    {t(`invoiceArchive.status.${item.is_overdue ? 'overdue' : item.payment_status}`)}</span>;
  const invoiceLink = (item: InvoiceCalendarItem, compact = false) => <Link key={item.id}
    to={`/invoices/${item.id}`} className={`calendar-invoice-link ${item.is_overdue ? 'overdue' : item.payment_status}${compact ? ' compact' : ''}`}
    title={`${item.invoice_number || item.original_filename} · ${item.supplier_name || item.shop_name || '—'} · ${fullDate(item.due_date)}`}>
    <strong>{item.invoice_number || item.original_filename}</strong>
    {!compact && <><span>{item.supplier_name || item.shop_name || '—'}</span><span className="calendar-invoice-amount">{t('invoiceCalendar.remaining')}: {invoiceMoney(item.outstanding_amount, item.currency, language)}<small>{t('invoiceArchive.total_amount')}: {invoiceMoney(item.gross_amount, item.currency, language)}</small></span>{status(item)}</>}
  </Link>;
  const monthDays = (month: Date) => Array.from({ length: 42 }, (_, index) => dayOffset(monday(monthStart(month)), index));
  const dayCell = (date: Date, month: number | null) => {
    const key = calendarDate(date), items = grouped.get(key) || [], outside = month !== null && date.getUTCMonth() !== month;
    return <div className={`calendar-day${outside ? ' outside' : ''}${key === today ? ' today' : ''}${key === selectedDay ? ' selected' : ''}`} key={key}>
      <button type="button" className="calendar-day-heading" onClick={() => chooseDay(key)} aria-pressed={key === selectedDay}
        aria-label={`${fullDate(key)} · ${outside ? t('invoiceCalendar.openMonth') : t('invoiceCalendar.count', { count: items.length })}`} title={outside ? t('invoiceCalendar.openMonth') : undefined} data-testid={`calendar-day-${key}`}>
        <time dateTime={key}>{date.getUTCDate()}</time>{!outside && items.length > 0 && <span className="calendar-day-count">{items.length}</span>}
      </button>
      <div className="calendar-day-items">{!outside && items.slice(0, view === 'week' ? 6 : 3).map(item => invoiceLink(item, true))}
        {!outside && items.length > (view === 'week' ? 6 : 3) && <button type="button" className="calendar-more" onClick={() => chooseDay(key)}>{t('invoiceCalendar.more', { count: items.length - (view === 'week' ? 6 : 3) })}</button>}
      </div>
    </div>;
  };
  const chosenItems = grouped.get(selectedDay) || [];
  const totals = (values: InvoiceCalendarTotal[]) => values.length ? values.map(value => <span className="calendar-total" key={value.currency || 'unknown'}>
    <strong>{value.outstanding_amount === null ? `${t('invoiceCalendar.unknownAmount')}${value.currency ? ` · ${value.currency}` : ''}` : invoiceMoney(value.outstanding_amount, value.currency, language)}</strong>
    {!value.currency && <small>{t('invoiceArchive.unknownCurrency')}</small>}
    {value.unknown_count > 0 && <small className="calendar-unknown-total">{t('invoiceCalendar.unknownTotal', { count: value.unknown_count })}</small>}
  </span>) : <span className="muted">{t('invoiceCalendar.noPayments')}</span>;
  return <section className="invoice-due-calendar panel" aria-labelledby={titleId} data-testid="invoice-calendar">
    <div className="calendar-title-row"><div><h2 id={titleId}>{t('invoiceCalendar.title')}</h2><p className="muted small">{t('invoiceCalendar.help')}</p></div>
      <div className="calendar-views" aria-label={t('invoiceCalendar.view')}>
        {(['week', 'month', 'year'] as const).map(value => <button type="button" key={value} aria-pressed={view === value}
          data-testid={`calendar-view-${value}`} onClick={() => setView(value)}>{t(`invoiceCalendar.${value}`)}</button>)}
      </div>
    </div>
    <div className="calendar-toolbar">
      <div className="calendar-period-controls"><button type="button" aria-label={t('invoiceCalendar.previous')} data-testid="calendar-prev" onClick={() => changePeriod(-1)}><ChevronLeft size={18} /></button>
        <button type="button" data-testid="calendar-today" onClick={() => { const value = todayDate(); setAnchor(value); setSelectedDay(value); }}>{t('invoiceCalendar.today')}</button>
        <button type="button" aria-label={t('invoiceCalendar.next')} data-testid="calendar-next" onClick={() => changePeriod(1)}><ChevronRight size={18} /></button>
      </div>
      <h3 aria-live="polite" data-testid="calendar-period">{title}</h3>
      <label className="calendar-jump">{t('invoiceCalendar.date')}<input type="date" data-testid="calendar-anchor" value={anchor} onChange={event => {
        if (parsedDate(event.target.value)) { setAnchor(event.target.value); setSelectedDay(event.target.value); }
      }} /></label>
    </div>
    {loading && <p role="status" className="calendar-notice">{t('invoiceCalendar.loading')}</p>}
    {error && <div role="alert" className="notice warning" data-testid="calendar-error"><p>{t(`invoiceCalendar.${error === 'limit' ? 'tooMany' : 'failed'}`)}</p>
      {error !== 'limit' && <button type="button" onClick={() => setRetry(value => value + 1)}>{t('invoiceCalendar.retry')}</button>}</div>}
    {!loading && !error && <>
      <div className="calendar-result-count" role="status" data-testid="calendar-count">{t('invoiceCalendar.count', { count: data?.total || 0 })}</div>
      <section className="calendar-payment-summary" data-testid="calendar-payment-summary">
        <h3>{t('invoiceCalendar.periodTotal')}</h3><div className="calendar-totals">{totals(data?.totals || [])}</div>
        <p className="muted small">{t('invoiceCalendar.summaryHelp')}</p>
      </section>
      {view !== 'year' && <section className="calendar-week-planning" data-testid="calendar-week-planning"><h3>{t('invoiceCalendar.weekPlanning')}</h3>
        <p className="muted small">{t('invoiceCalendar.weekScope')}</p>
        {(data?.weeks || []).map(week => <details key={week.from} data-testid={`calendar-week-${week.from}`}>
          <summary><span>{fullDate(week.from)} – {fullDate(week.to)}</span><span className="calendar-totals">{totals(week.totals)}</span></summary>
          <div className="calendar-selected-invoices">{(data?.items || []).filter(item => week.invoice_ids.includes(item.id)).map(item => invoiceLink(item))}</div>
          {!week.invoice_ids.length && <p className="muted small">{t('invoiceCalendar.emptyDay')}</p>}
        </details>)}
      </section>}
      <div className="calendar-legend">{['paid', 'unpaid', 'overdue', 'unknown'].map(value => <span key={value} className={`calendar-status ${value}`}>{t(`invoiceArchive.status.${value}`)}</span>)}</div>
      {view === 'year' ? <div className="calendar-year" data-testid="calendar-year-grid">
        {Array.from({ length: 12 }, (_, index) => { const month = monthStart(parsedDate(`${anchor.slice(0, 4)}-01-01`)!, index); return <section className="calendar-mini-month" key={index}>
          <h3><button type="button" onClick={() => { setAnchor(calendarDate(month)); setSelectedDay(calendarDate(month)); setView('month'); }}>{dateLabel(calendarDate(month), language, { month: 'long' })}</button></h3>
          <div className="calendar-month-totals" data-testid={`calendar-month-total-${index + 1}`}>{totals(data?.months?.find(value => value.from.slice(0, 7) === calendarDate(month).slice(0, 7))?.totals || [])}</div>
          <div className="calendar-mini-grid">{weekdays.map((day, position) => <span className="calendar-mini-weekday" key={position}>{day}</span>)}
            {monthDays(month).map(date => { const key = calendarDate(date), items = grouped.get(key) || [], inMonth = date.getUTCMonth() === index; return inMonth
              ? <button type="button" key={key} data-testid={`calendar-mini-${key}`} onClick={() => chooseDay(key)}
                  className={`${items.length ? `has-invoices ${stateForDay(items)}` : ''}${key === selectedDay ? ' selected' : ''}${key === today ? ' today' : ''}`}
                  aria-pressed={key === selectedDay} aria-label={`${fullDate(key)} · ${t('invoiceCalendar.count', { count: items.length })}`}>
                  {date.getUTCDate()}{items.length > 0 && <span className="calendar-mini-count">{items.length}</span>}</button>
              : <span key={key} />; })}
          </div>
        </section>; })}
      </div> : <div className="calendar-grid-scroll"><div className={`calendar-main-grid ${view}`} data-testid="calendar-main-grid">
        {weekdays.map((day, index) => <div className="calendar-weekday" key={index}>{day}</div>)}
        {(view === 'week' ? Array.from({ length: 7 }, (_, index) => dayOffset(parsedDate(period.from)!, index)) : monthDays(parsedDate(anchor)!))
          .map(date => dayCell(date, view === 'month' ? parsedDate(anchor)!.getUTCMonth() : null))}
      </div></div>}
      <section className="calendar-selected-day" aria-label={t('invoiceCalendar.selectedDay')}>
        <h3>{fullDate(selectedDay)} <span className="muted">· {t('invoiceCalendar.count', { count: chosenItems.length })}</span></h3>
        {chosenItems.length > 0 ? <div className="calendar-selected-invoices" data-testid="calendar-selected-invoices">{chosenItems.map(item => invoiceLink(item))}</div>
          : <p className="muted small">{t('invoiceCalendar.emptyDay')}</p>}
      </section>
      <p className="muted small calendar-footnote">{t('invoiceCalendar.noDueDate')}</p>
    </>}
  </section>;
}
