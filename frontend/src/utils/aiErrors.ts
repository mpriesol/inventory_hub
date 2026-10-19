import type { TFunction } from 'i18next';
export function aiErrorText(t: TFunction, value: string): string {
  const [code, ...details] = value.split(':');
  const message = t(`ai.errors.${code}`, { defaultValue: code });
  return details.length ? `${message} · ${details.join(':')}` : message;
}
