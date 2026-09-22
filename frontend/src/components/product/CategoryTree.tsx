import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { TargetOptions } from '../../api/catalog';

export function CategoryTree({ categories, value, language = 'sk', onChange }: {
  categories: TargetOptions['categories']; value: string; language?: string; onChange: (code: string) => void;
}) {
  const { t } = useTranslation();
  const [search, setSearch] = useState('');
  const normalize = (s: string) => s.normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase();
  const index = new Map(categories.map(c => [c.code, c]));
  const name = (code: string) => index.get(code)?.names[language] || code;
  function path(code: string) { const result: string[] = []; const seen = new Set<string>(); while (code && !seen.has(code)) { seen.add(code); result.unshift(name(code)); code = index.get(code)?.parent_code || ''; } return result.join(' / '); }
  const matches = categories.filter(c => c.assignable !== false && normalize(path(c.code) + c.code).includes(normalize(search)));
  const draw = (parent: string | null, seen = new Set<string>()): React.ReactNode => categories.filter(c => (c.parent_code && index.has(c.parent_code) ? c.parent_code : null) === parent).map(c => {
    if (seen.has(c.code)) return null;
    const descendants = categories.some(child => child.parent_code === c.code);
    const button = c.assignable === false ? <span>{name(c.code)}</span> : <button type="button" aria-pressed={value === c.code} className={value === c.code ? 'ai-primary' : ''} onClick={() => onChange(c.code)}>{name(c.code)} <small>{c.code}{c.active === false ? ` · ${t('ai.inactiveCategory')}` : ''}</small></button>;
    return <li key={c.code}>{descendants ? <details open={value === c.code || path(value).startsWith(path(c.code) + ' / ') || undefined}><summary>{button}</summary><ul>{draw(c.code, new Set([...seen,c.code]))}</ul></details> : button}</li>;
  });
  return <div className="category-tree"><details><summary>{value ? path(value) : t('ai.chooseCategory')}</summary><input aria-label={t('ai.searchCategories')} placeholder={t('ai.searchCategories')} value={search} onChange={e => setSearch(e.target.value)} /><button type="button" onClick={() => onChange('')}>{t('ai.clearCategory')}</button><div className="category-tree-list">{search ? <ul>{!matches.length && <li>{t('ai.noCategoryResults')}</li>}{matches.map(c => <li key={c.code}><button type="button" onClick={() => onChange(c.code)}>{path(c.code)} · {c.code}</button></li>)}</ul> : <ul>{draw(null)}</ul>}</div></details>{value && <small>{t('ai.categoryAncestorsHelp')}</small>}</div>;
}
