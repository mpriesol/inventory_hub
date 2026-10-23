/** Display only: never round source/edit values or coerce identifiers. */
export function formatNumber(value: unknown, language = 'sk', maximumFractionDigits = 3, minimumFractionDigits = 0): string {
  if (value === null || value === undefined || value === '') return '—';
  const number = Number(value);
  return Number.isFinite(number) ? new Intl.NumberFormat(language, { maximumFractionDigits, minimumFractionDigits }).format(number) : String(value);
}

/** Exact addition of server decimal strings; formatting is a separate step. */
export function sumDecimals(values: string[]): string {
  const parts = values.map(value => value.split('.'));
  const scale = Math.max(0, ...parts.map(part => (part[1] || '').length));
  const total = parts.reduce((sum, [whole, fraction = '']) => sum + BigInt(whole + fraction.padEnd(scale, '0')), 0n);
  const raw = (total < 0n ? -total : total).toString().padStart(scale + 1, '0');
  return `${total < 0n ? '-' : ''}${scale ? `${raw.slice(0, -scale)}.${raw.slice(-scale)}` : raw}`;
}

/** Business date for invoice forms; independent of browser UTC offset. */
export function todayDate(now = new Date()) {
  const pieces = new Intl.DateTimeFormat('en-CA', { timeZone: 'Europe/Bratislava', year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(now);
  const part = (type: string) => pieces.find(piece => piece.type === type)?.value;
  return `${part('year')}-${part('month')}-${part('day')}`;
}
