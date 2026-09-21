import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { AiBook, AiCategory, AiJob, AiRule, AiRules, aiRequest } from '../../api/aiContent';
import { AiPolicyFields } from './AiPolicyFields';

export function AiRuleEditor({ rules, onReload, onJob }: { rules: AiRules; onReload: () => void; onJob: (job: AiJob) => void }) {
  const { t } = useTranslation();
  const [book, setBook] = useState<AiBook>(() => structuredClone(rules.book));
  const [kind, setKind] = useState<'rules' | 'categories'>('rules');
  const [index, setIndex] = useState(0);
  const [draft, setDraft] = useState<number | null>(null);
  const [note, setNote] = useState('');
  const [prompt, setPrompt] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const selected = book[kind][index];
  const category = kind === 'categories' ? selected as AiCategory : null;
  const rule = kind === 'rules' ? selected as AiRule : null;
  function change(patch: Partial<AiRule & AiCategory>) {
    setDraft(null); setBook(old => ({ ...old, [kind]: old[kind].map((value, i) => i === index ? { ...value, ...patch } : value) }));
  }
  async function execute(fn: () => Promise<void>) { setBusy(true); setError(''); try { await fn(); } catch (e) { setError((e as Error).message); } finally { setBusy(false); } }
  function add() {
    const id = `profile_${Date.now()}`;
    const value = kind === 'rules' ? { id, name: t('ai.newRule'), instructions: '', policy: {}, enabled: false, official_domains: [], scope: { shop: '', supplier: '', category: '', brand: '', product: '' } }
      : { id, name: t('ai.newCategory'), instructions: '', policy: {}, parameters: [], shop_categories: {}, automatic_import_ready: false };
    setBook(old => ({ ...old, [kind]: [...old[kind], value] })); setIndex(book[kind].length); setDraft(null);
  }
  return <>
    <div className="ai-notice">{t('ai.rulesHelp')}</div>
    <div className="ai-toolbar"><button aria-selected={kind === 'rules'} onClick={() => { setKind('rules'); setIndex(0); }}>{t('ai.rules')}</button><button aria-selected={kind === 'categories'} onClick={() => { setKind('categories'); setIndex(0); }}>{t('ai.categoryProfiles')}</button><button onClick={add}>{t('ai.add')}</button></div>
    <div className="ai-columns"><aside className="ai-card"><label>{t('ai.chooseProfile')}<select value={index} onChange={e => setIndex(Number(e.target.value))}>{book[kind].map((r, i) => <option key={r.id} value={i}>{r.name}</option>)}</select></label>
      <p>{t('ai.publishedVersion', { version: rules.published_id })}</p>
      <label>{t('ai.versionHistory')}<select value="" disabled={busy} onChange={e => execute(async () => { const value = await aiRequest<{ book: AiBook; id: number }>(`/rules/${e.target.value}`); setBook(value.book); setIndex(0); setDraft(value.id); })}><option value="">{t('ai.loadVersion')}</option>{rules.versions.map(v => <option key={v.id} value={v.id}>#{v.id} · {v.note}</option>)}</select></label>
      <p>{t('ai.restoreHelp')}</p>
    </aside><section className="ai-card">{selected && <>
      <div className="ai-grid"><label>{t('ai.profileName')}<input value={selected.name} onChange={e => change({ name: e.target.value })} /></label><label>{t('ai.profileId')}<input value={selected.id} onChange={e => change({ id: e.target.value })} /></label></div>
      {rule && <><label className="ai-check"><input type="checkbox" checked={rule.enabled} onChange={e => change({ enabled: e.target.checked })} />{t('ai.enabledRule')}</label><h3>{t('ai.scope')}</h3><div className="ai-grid">{(['shop', 'supplier', 'category', 'brand', 'product'] as const).map(key => <label key={key}>{t(`ai.scopeFields.${key}`)}<input value={rule.scope[key]} placeholder={t('ai.all')} onChange={e => change({ scope: { ...rule.scope, [key]: e.target.value } })} /></label>)}</div><label>{t('ai.officialDomains')}<input value={rule.official_domains.join(', ')} placeholder="northfinder.com" onChange={e => change({ official_domains: e.target.value.split(',').map(s => s.trim()).filter(Boolean) })} /></label></>}
      <AiPolicyFields value={selected.policy} onChange={policy => change({ policy })} />
      <label>{t('ai.instructions')}<textarea className="ai-long-text" value={selected.instructions} onChange={e => change({ instructions: e.target.value })} /></label>
      {category && <><h3>{t('ai.shopMapping')}</h3><div className="ai-grid">{['biketrek', 'xtrek', ...Object.keys(category.shop_categories).filter(k => !['biketrek', 'xtrek'].includes(k))].map(shop => <label key={shop}>{shop}<input placeholder={t('ai.categoryCode')} value={category.shop_categories[shop] || ''} onChange={e => change({ shop_categories: { ...category.shop_categories, [shop]: e.target.value } })} /></label>)}</div>
        <label className="ai-check"><input type="checkbox" checked={category.automatic_import_ready} onChange={e => change({ automatic_import_ready: e.target.checked })} />{t('ai.categoryAutomationReady')}</label>
        <h3>{t('ai.parameterRegistry')}</h3><p>{t('ai.parametersHelp')}</p>
        {category.parameters.map((param, i) => <div className="ai-card" key={i}><div className="ai-grid"><label>{t('ai.parameterName')}<input value={param.name} onChange={e => change({ parameters: category.parameters.map((p, n) => n === i ? { ...p, name: e.target.value } : p) })} /></label><label>{t('ai.parameterScope')}<select value={param.scope} onChange={e => change({ parameters: category.parameters.map((p, n) => n === i ? { ...p, scope: e.target.value as 'parent' | 'variant' } : p) })}><option value="parent">{t('ai.parent')}</option><option value="variant">{t('ai.variant')}</option></select></label>
          <label>{t('ai.allowedValues')}<input value={param.values.join(' | ')} onChange={e => change({ parameters: category.parameters.map((p, n) => n === i ? { ...p, values: e.target.value.split('|').map(s => s.trim()).filter(Boolean) } : p) })} /></label><label>{t('ai.unit')}<input value={param.unit} onChange={e => change({ parameters: category.parameters.map((p, n) => n === i ? { ...p, unit: e.target.value } : p) })} /></label></div>
          <label>{t('ai.instructions')}<input value={param.instructions} onChange={e => change({ parameters: category.parameters.map((p, n) => n === i ? { ...p, instructions: e.target.value } : p) })} /></label>
          <div className="ai-actions"><label className="ai-check"><input type="checkbox" checked={param.required} onChange={e => change({ parameters: category.parameters.map((p, n) => n === i ? { ...p, required: e.target.checked } : p) })} />{t('ai.required')}</label><button onClick={() => change({ parameters: category.parameters.filter((_, n) => n !== i) })}>{t('ai.removeDraftEntry')}</button></div>
        </div>)}<button onClick={() => change({ parameters: [...category.parameters, { name: '', required: false, scope: 'parent', values: [], unit: '', instructions: '' }] })}>{t('ai.addParameter')}</button></>}
      <details><summary>{t('ai.comparePublished')}</summary><div className="ai-grid"><pre className="ai-original">{rules.book[kind].find(r => r.id === selected.id)?.instructions || t('ai.newProfile')}</pre><pre className="ai-original">{selected.instructions}</pre></div></details>
      <label>{t('ai.changeNote')}<input value={note} onChange={e => setNote(e.target.value)} /></label>
      <div className="ai-actions"><button disabled={busy || !note.trim()} onClick={() => execute(async () => { const result = await aiRequest<{ id: number }>('/rules', { expected_published: rules.published_id, book, note }); setDraft(result.id); })}>{t('ai.saveDraft')}</button>
        <button className="ai-primary" disabled={busy || !draft || draft === rules.published_id} onClick={() => execute(async () => { await aiRequest(`/rules/${draft}/publish`, { expected_published: rules.published_id }); onReload(); })}>{t('ai.publishVersion', { version: draft || '—' })}</button></div>
      <details><summary>{t('ai.aiRuleAssistant')}</summary><p>{t('ai.proposalHelp')}</p><textarea value={prompt} onChange={e => setPrompt(e.target.value)} placeholder={t('ai.proposalPlaceholder')} /><button disabled={busy || !prompt.trim() || !rules.book[kind].some(r => r.id === selected.id)} onClick={() => execute(async () => { onJob(await aiRequest<AiJob>('/rules/proposal', { request_id: crypto.randomUUID(), rule_id: selected.id, category: kind === 'categories', request: prompt })); })}>{t('ai.prepareProposal')}</button></details>
    </>}</section></div>{error && <div role="alert" className="ai-notice ai-error">{error}</div>}
  </>;
}
