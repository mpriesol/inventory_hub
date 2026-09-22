import { CategoryTree } from './CategoryTree';
import { TargetOptions, catalogRequest } from '../../api/catalog';
import React, { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { AiBook, AiCategory, AiJob, AiRule, AiRules, aiRequest } from '../../api/aiContent';
import { AiPolicyFields } from './AiPolicyFields';

export function AiRuleEditor({ rules, onReload, onJob }: { rules: AiRules; onReload: () => void; onJob: (job: AiJob) => void }) {
  const { t } = useTranslation();
  const [book, setBook] = useState<AiBook>(() => structuredClone(rules.book));
  const [group, setGroup] = useState('common');
  const [shopOptions, setShopOptions] = useState<Record<string, TargetOptions>>({});
  const [kind, setKind] = useState<'rules' | 'categories'>('rules');
  const [index, setIndex] = useState(0);
  const [draft, setDraft] = useState<number | null>(null);
  const [note, setNote] = useState('');
  const [prompt, setPrompt] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const groupOf = (r: AiRule) => ['product','shop','category','brand','supplier'].find(k => r.scope[k as keyof AiRule['scope']]) || 'common';
  const choices = book[kind].map((r,i) => ({r,i})).filter(({r}) => kind === 'categories' || groupOf(r as AiRule) === group);
  const selected = choices.find(c => c.i === index)?.r;
  useEffect(() => { if (kind !== 'categories') return; let stopped = false; Promise.all(['biketrek','xtrek'].map(async shop => { const data = await catalogRequest<TargetOptions>(`/shops/${shop}/import/options`); if (!stopped) setShopOptions(old => ({...old,[shop]:data})); })).catch(e => { if (!stopped) setError(e.message); }); return () => { stopped = true; }; }, [kind]);
  function chooseGroup(next: string) { setGroup(next); setKind('rules'); setIndex(book.rules.findIndex(r => groupOf(r) === next)); }
  function remove() { if (!selected || !window.confirm(t('ai.removeRuleConfirm', {name:selected.name}))) return; const next = {...book, [kind]:book[kind].filter(r => r.id !== selected.id)}; setBook(next); setDraft(null); setIndex(kind === 'categories' ? 0 : next.rules.findIndex(r => groupOf(r) === group)); }

  const category = kind === 'categories' ? selected as AiCategory : null;
  const rule = kind === 'rules' ? selected as AiRule : null;
  function change(patch: Partial<AiRule & AiCategory>) {
    if (patch.scope && rule) setGroup(groupOf({...rule,...patch}));
    setDraft(null); setBook(old => ({ ...old, [kind]: old[kind].map((value, i) => i === index ? { ...value, ...patch } : value) }));
  }
  async function execute(fn: () => Promise<void>) { setBusy(true); setError(''); try { await fn(); } catch (e) { setError(t(`ai.errors.${(e as Error & {code?: string}).code}`, {defaultValue:(e as Error).message})); } finally { setBusy(false); } }
  function add() {
    const id = `profile_${Date.now()}`;
    const value = kind === 'rules' ? { id, name: t('ai.newRule'), instructions: '', policy: {}, enabled: false, official_domains: [], scope: { shop: '', supplier: '', category: '', brand: '', product: '', ...(group === 'common' ? {} : {[group]: t('ai.fillScope')}) } }
      : { id, name: t('ai.newCategory'), instructions: '', policy: {}, parameters: [], shop_categories: {}, automatic_import_ready: false };
    setBook(old => ({ ...old, [kind]: [...old[kind], value] })); setIndex(book[kind].length); setDraft(null);
  }
  return <>
    <div className="ai-notice">{t('ai.rulesHelp')}</div>
    <nav className="ai-rule-groups" aria-label={t('ai.ruleGroupsLabel')}>{['common','supplier','brand','category','shop','product'].map(key => <button key={key} aria-selected={group === key && kind === 'rules'} className={group === key && kind === 'rules' ? 'ai-primary' : ''} onClick={() => chooseGroup(key)}>{t(`ai.ruleGroups.${key}`)} ({book.rules.filter(r => groupOf(r) === key).length})</button>)}<button aria-selected={kind === 'categories'} className={kind === 'categories' ? 'ai-primary' : ''} onClick={() => { setKind('categories'); setIndex(0); }}>{t('ai.categoryProfiles')}</button></nav><div className="ai-toolbar"><button onClick={add}>{t('ai.add')}</button><small>{t('ai.ruleOrder')}</small></div>
    <div className="ai-columns"><aside className="ai-card"><label>{t('ai.chooseProfile')}<select value={index} onChange={e => setIndex(Number(e.target.value))}>{choices.map(({r,i}) => <option key={r.id} value={i}>{r.name}</option>)}</select></label>
      <p>{t('ai.publishedVersion', { version: rules.published_id })}</p>
      <label>{t('ai.versionHistory')}<select value="" disabled={busy} onChange={e => execute(async () => { const value = await aiRequest<{ book: AiBook; id: number }>(`/rules/${e.target.value}`); setBook(value.book); setIndex(kind === 'categories' ? 0 : value.book.rules.findIndex(r => groupOf(r) === group)); setDraft(value.id); })}><option value="">{t('ai.loadVersion')}</option>{rules.versions.map(v => <option key={v.id} value={v.id}>#{v.id} · {v.note}</option>)}</select></label>
      <p>{t('ai.restoreHelp')}</p>
    </aside><section className="ai-card">{!selected && <p>{t('ai.noRulesInGroup')}</p>}{selected && <><button onClick={remove} disabled={busy}>{t('ai.removeRule')}</button>
      <div className="ai-grid"><label>{t('ai.profileName')}<input value={selected.name} onChange={e => change({ name: e.target.value })} /></label><label>{t('ai.profileId')}<input value={selected.id} onChange={e => change({ id: e.target.value })} /></label></div>
      {rule && <><label className="ai-check"><input type="checkbox" checked={rule.enabled} onChange={e => change({ enabled: e.target.checked })} />{t('ai.enabledRule')}</label><h3>{t('ai.scope')}</h3><div className="ai-grid">{(['shop', 'supplier', 'category', 'brand', 'product'] as const).map(key => <label key={key}>{t(`ai.scopeFields.${key}`)}<input value={rule.scope[key]} placeholder={t('ai.all')} onChange={e => change({ scope: { ...rule.scope, [key]: e.target.value } })} /></label>)}</div><label>{t('ai.officialDomains')}<input value={rule.official_domains.join(', ')} placeholder="northfinder.com" onChange={e => change({ official_domains: e.target.value.split(',').map(s => s.trim()).filter(Boolean) })} /></label></>}
      {rule && <details><summary>{t('ai.importPolicy')}</summary><p>{t('ai.importPolicyHelp')}</p><div className="ai-grid">{(['orderable','unknown','supplier_name'] as const).map(key => <label key={key}>{t(`ai.importPolicyFields.${key}`)}<input value={rule.import_policy?.[key] || ''} onChange={e => change({import_policy:{...rule.import_policy,[key]:e.target.value || null}})} /></label>)}</div><label>{t('ai.hideZeroStock')}<select value={rule.import_policy?.hide_zero_stock == null ? '' : String(rule.import_policy.hide_zero_stock)} onChange={e => change({import_policy:{...rule.import_policy,hide_zero_stock:e.target.value === '' ? null : e.target.value === 'true'}})}><option value="">{t('ai.inherit')}</option><option value="true">{t('ai.on')}</option><option value="false">{t('ai.off')}</option></select></label></details>}
      <AiPolicyFields value={selected.policy} onChange={policy => change({ policy })} />
      <label>{t('ai.instructions')}<textarea className="ai-long-text" value={selected.instructions} onChange={e => change({ instructions: e.target.value })} /></label>
      {category && <><h3>{t('ai.shopMapping')}</h3><div className="ai-grid">{['biketrek', 'xtrek', ...Object.keys(category.shop_categories).filter(k => !['biketrek', 'xtrek'].includes(k))].map(shop => <label key={shop}>{shop}<CategoryTree categories={shopOptions[shop]?.categories || []} value={category.shop_categories[shop] || ''} onChange={code => change({ shop_categories: { ...category.shop_categories, [shop]: code } })} /></label>)}</div>
        <label className="ai-check"><input type="checkbox" checked={category.automatic_import_ready} onChange={e => change({ automatic_import_ready: e.target.checked })} />{t('ai.categoryAutomationReady')}</label>
        <h3>{t('ai.parameterRegistry')}</h3><p>{t('ai.parametersHelp')}</p>
        {category.parameters.map((param, i) => <div className="ai-card" key={i}><div className="ai-grid"><label>{t('ai.parameterName')}<input value={param.name} onChange={e => change({ parameters: category.parameters.map((p, n) => n === i ? { ...p, name: e.target.value } : p) })} /></label><label>{t('ai.parameterScope')}<select value={param.scope} onChange={e => change({ parameters: category.parameters.map((p, n) => n === i ? { ...p, scope: e.target.value as 'parent' | 'variant' } : p) })}><option value="parent">{t('ai.parent')}</option><option value="variant">{t('ai.variant')}</option></select></label>
          <label>{t('ai.allowedValues')}<input value={param.values.join(' | ')} onChange={e => change({ parameters: category.parameters.map((p, n) => n === i ? { ...p, values: e.target.value.split('|').map(s => s.trim()).filter(Boolean) } : p) })} /></label><label>{t('ai.unit')}<input value={param.unit} onChange={e => change({ parameters: category.parameters.map((p, n) => n === i ? { ...p, unit: e.target.value } : p) })} /></label></div>
          <label>{t('ai.instructions')}<input value={param.instructions} onChange={e => change({ parameters: category.parameters.map((p, n) => n === i ? { ...p, instructions: e.target.value } : p) })} /></label>
          <div className="ai-actions"><label className="ai-check"><input type="checkbox" checked={param.required} onChange={e => change({ parameters: category.parameters.map((p, n) => n === i ? { ...p, required: e.target.checked } : p) })} />{t('ai.required')}</label><button onClick={() => change({ parameters: category.parameters.filter((_, n) => n !== i) })}>{t('ai.removeDraftEntry')}</button></div>
        </div>)}<button onClick={() => change({ parameters: [...category.parameters, { name: '', required: false, scope: 'parent', values: [], unit: '', instructions: '' }] })}>{t('ai.addParameter')}</button></>}
      <details><summary>{t('ai.comparePublished')}</summary><div className="ai-grid"><pre className="ai-original">{rules.book[kind].find(r => r.id === selected.id)?.instructions || t('ai.newProfile')}</pre><pre className="ai-original">{selected.instructions}</pre></div></details>

      <details><summary>{t('ai.aiRuleAssistant')}</summary><p>{t('ai.proposalHelp')}</p><textarea value={prompt} onChange={e => setPrompt(e.target.value)} placeholder={t('ai.proposalPlaceholder')} /><button disabled={busy || !prompt.trim() || !rules.book[kind].some(r => r.id === selected.id)} onClick={() => execute(async () => { onJob(await aiRequest<AiJob>('/rules/proposal', { request_id: crypto.randomUUID(), rule_id: selected.id, category: kind === 'categories', request: prompt })); })}>{t('ai.prepareProposal')}</button></details>
    </>}</section></div><div className="ai-card">      <label>{t('ai.changeNote')}<input value={note} onChange={e => setNote(e.target.value)} /></label>
      <div className="ai-actions"><button disabled={busy || !note.trim()} onClick={() => execute(async () => { const result = await aiRequest<{ id: number }>('/rules', { expected_published: rules.published_id, book, note }); setDraft(result.id); })}>{t('ai.saveDraft')}</button>
        <button className="ai-primary" disabled={busy || !draft || draft === rules.published_id} aria-describedby={!draft ? 'ai-publish-help' : undefined} onClick={() => execute(async () => { await aiRequest(`/rules/${draft}/publish`, { expected_published: rules.published_id }); onReload(); })}>{draft ? t('ai.publishVersion', { version: draft }) : t('ai.publishDraft')}</button></div>{!draft && <p id="ai-publish-help">{t('ai.publishDraftHelp')}</p>}</div>{error && <div role="alert" className="ai-notice ai-error">{error}</div>}
  </>;
}
