import React, { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { AiJob, AiRules, aiRequest } from '../../api/aiContent';
import { TargetOptions, catalogRequest } from '../../api/catalog';
import { CategoryTree } from './CategoryTree';

/** Capture current shop content first; generating and updating are separate actions. */
export function AiExistingProduct({ rules, onJob }: { rules: AiRules; onJob: (job: AiJob) => void }) {
  const { t } = useTranslation();
  const [shops, setShops] = useState<{ code: string; name: string }[]>([]);
  const [shop, setShop] = useState('');
  const [code, setCode] = useState('');
  const [supplier, setSupplier] = useState('');
  const [brand, setBrand] = useState('');
  const [profile, setProfile] = useState('general');
  const [category, setCategory] = useState('');
  const [research, setResearch] = useState('official');
  const [options, setOptions] = useState<TargetOptions | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const requestId = useRef<string | null>(null);
  const translateError = (e: unknown) => t(`ai.errors.${(e as Error & { code?: string }).code}`, { defaultValue: (e as Error).message });
  useEffect(() => {
    let stopped = false;
    aiRequest<{ shops: { code: string; name: string }[] }>('/existing-products/options').then(data => {
      if (!stopped) { setShops(data.shops); if (data.shops.length === 1) setShop(data.shops[0].code); }
    }).catch(e => { if (!stopped) setError(translateError(e)); });
    return () => { stopped = true; };
  }, []);
  useEffect(() => {
    setOptions(null); setCategory('');
    if (!shop) return;
    let stopped = false;
    catalogRequest<TargetOptions>(`/shops/${shop}/import/options`).then(data => { if (!stopped) setOptions(data); })
      .catch(e => { if (!stopped) setError(translateError(e)); });
    return () => { stopped = true; };
  }, [shop]);
  function change(setter: (value: string) => void, value: string) { requestId.current = null; setter(value); setError(''); }
  async function prepare(e: React.FormEvent) {
    e.preventDefault(); setBusy(true); setError(''); requestId.current ||= crypto.randomUUID();
    try {
      const result = await aiRequest<{ job: AiJob }>('/existing-products', { request_id: requestId.current,
        shop, code: code.trim(), supplier, brand, category_profile: profile, category_code: category || null, research });
      onJob(result.job);
    } catch (e) { setError(translateError(e)); } finally { setBusy(false); }
  }
  const suppliers = [...new Set(rules.book.rules.filter(r => r.enabled && r.scope.supplier).map(r => r.scope.supplier))].sort();
  const brands = [...new Set(rules.book.rules.filter(r => r.enabled && r.scope.brand).map(r => r.scope.brand))].sort();
  return <form className="ai-card" onSubmit={prepare}>
    <h2>{t('ai.existing.title')}</h2><p>{t('ai.existing.help')}</p>
    <fieldset disabled={busy} style={{ border: 0, padding: 0, margin: 0 }}>
      <div className="ai-grid">
        <label>{t('ai.existing.shop')}<select required value={shop} onChange={e => change(setShop, e.target.value)}>
          <option value="">{t('ai.existing.chooseShop')}</option>{shops.map(s => <option key={s.code} value={s.code}>{s.name}</option>)}
        </select></label>
        <label>{t('ai.existing.code')}<input required maxLength={100} autoComplete="off" value={code} onChange={e => change(setCode, e.target.value)} placeholder="PL-3571351" /></label>
        <label>{t('ai.existing.supplier')}<select value={supplier} onChange={e => change(setSupplier, e.target.value)}>
          <option value="">{t('ai.existing.noSupplier')}</option>{suppliers.map(v => <option key={v}>{v}</option>)}
        </select></label>
        <label>{t('ai.existing.brand')}<select value={brand} onChange={e => change(setBrand, e.target.value)}>
          <option value="">{t('ai.existing.autoBrand')}</option>{brands.map(v => <option key={v}>{v}</option>)}
        </select></label>
        <label>{t('ai.categoryProfile')}<select value={profile} onChange={e => change(setProfile, e.target.value)}>
          {rules.book.categories.filter(c => c.parameters.every(p => p.scope === 'parent' || !p.required)).map(c => <option value={c.id} key={c.id}>{c.name}</option>)}
        </select></label>
        <label>{t('ai.existing.category')}<CategoryTree categories={options?.categories || []} value={category} onChange={v => change(setCategory, v)} /></label>
        <label>{t('ai.researchMode')}<select value={research} onChange={e => change(setResearch, e.target.value)}>
          <option value="official">{t('ai.researchOfficial')}</option><option value="feed_only">{t('ai.existing.shopOnly')}</option>
        </select></label>
      </div>
      <p className="ai-notice">{t('ai.existing.boundaries')}</p>
      <button className="ai-primary" disabled={busy || !shop || !code.trim()}>{busy ? t('common.loading') : t('ai.existing.prepare')}</button>
    </fieldset>
    {error && <div className="ai-notice ai-error" role="alert">{error}</div>}
  </form>;
}
