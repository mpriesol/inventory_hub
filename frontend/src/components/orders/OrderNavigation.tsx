import React from 'react';
import { NavLink } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
export function OrderNavigation({ shop }: { shop: string }) {
  const { t } = useTranslation();
  return <nav aria-label={t('orderNavigation.title')} className="flex flex-wrap gap-4 mb-4">
    {[['/orders/inbox', 'inbox'], ['/orders', 'audit'], ['/orders/stock', 'stock']].map(([path, key]) =>
      <NavLink key={key} end to={`${path}?shop=${encodeURIComponent(shop)}`} className={({ isActive }) => `px-3 py-2 rounded border ${isActive ? 'font-semibold' : ''}`} style={({ isActive }) => ({ color: isActive ? 'var(--color-accent)' : 'var(--color-text-secondary)', borderColor: isActive ? 'var(--color-accent)' : 'var(--color-border-subtle)' })}>{t(`orderNavigation.${key}`)}</NavLink>)}
  </nav>;
}
