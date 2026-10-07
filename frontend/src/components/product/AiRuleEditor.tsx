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
  const [imported, setImported] = useState<{ name: string; book: unknown; rules: number; categories: number } | null>(null);
  const [importedId, setImportedId] = useState<number | null>(null);
  const [importSuccess, setImportSuccess] = useState<number | null>(null);
  // Presentation groups follow the subject of the rule. A shop condition still
  // narrows its scope; this does not change backend resolution precedence.
  const groupOf = (r: AiRule) => r.category_profiles?.length ? 'category' : ['product','category','brand','supplier','shop'].find(k => r.scope[k as keyof AiRule['scope']]) || 'common';
  const categoryRules = book.rules.map((r,i) => ({r,i})).filter(({r}) => groupOf(r) === 'category');
  const categoryView = kind === 'categories' || group === 'category';
  const choices = book[kind].map((r,i) => ({r,i})).filter(({r}) => kind === 'categories' || groupOf(r as AiRule) === group);
  const selected = choices.find(c => c.i === index)?.r;
  useEffect(() => { if (kind !== 'categories') return; let stopped = false; Promise.all(['biketrek','xtrek'].map(async shop => { const data = await catalogRequest<TargetOptions>(`/shops/${shop}/import/options`); if (!stopped) setShopOptions(old => ({...old,[shop]:data})); })).catch(e => { if (!stopped) setError(e.message); }); return () => { stopped = true; }; }, [kind]);
  function chooseGroup(next: string) { setGroup(next); setKind('rules'); setIndex(book.rules.findIndex(r => groupOf(r) === next)); }
  function remove() { if (!selected || !window.confirm(t('ai.removeRuleConfirm', {name:selected.name}))) return; const next = {...book, [kind]:book[kind].filter(r => r.id !== selected.id)}; setBook(next); setDraft(null); setImportSuccess(null); setIndex(kind === 'categories' ? 0 : next.rules.findIndex(r => groupOf(r) === group)); }

  const category = kind === 'categories' ? selected as AiCategory : null;
  const rule = kind === 'rules' ? selected as AiRule : null;
  function change(patch: Partial<AiRule & AiCategory>) {
    if (patch.scope && rule) setGroup(groupOf({...rule,...patch}));
    setDraft(null); setImportSuccess(null); setBook(old => ({ ...old, [kind]: old[kind].map((value, i) => i === index ? { ...value, ...patch } : value) }));
  }
  async function execute(fn: () => Promise<void>) { setBusy(true); setError(''); try { await fn(); } catch (e) { setError(t(`ai.errors.${(e as Error & {code?: string}).code}`, {defaultValue:(e as Error).message})); } finally { setBusy(false); } }
  async function readBookFile(file?: File) {
    if (!file) return;
    setBusy(true); setError(''); setImported(null); setImportedId(null); setImportSuccess(null);
    try {
      if (file.size > 2 * 1024 * 1024) { setError(t('ai.ruleBookTooLarge')); return; }
      const parsed = JSON.parse(await file.text());
      const candidate = parsed && typeof parsed === 'object' && 'book' in parsed ? parsed.book : parsed;
      if (!candidate || typeof candidate !== 'object' || !Array.isArray(candidate.rules) || !Array.isArray(candidate.categories)) {
        setError(t('ai.ruleBookInvalid')); return;
      }
      // Untrusted JSON stays separate from the editable book. The existing
      // RuleSave schema validates every entry before anything is rendered.
      setImported({ name: file.name, book: candidate, rules: candidate.rules.length, categories: candidate.categories.length });
    } catch { setError(t('ai.ruleBookInvalid')); }
    finally { setBusy(false); }
  }
  async function saveImportedBook() {
    if (!imported || !note.trim() || busy) return;
    setBusy(true); setError('');
    let savedId = importedId;
    try {
      if (!savedId) {
        const saved = await aiRequest<{ id: number }>('/rules', { expected_published: rules.published_id, book: imported.book, note });
        savedId = saved.id; setImportedId(saved.id);
      }
      // Read the normalized saved version. A failed read retries only this GET,
      // never creates another draft or publishes the uploaded book.
      const saved = await aiRequest<{ id: number; book: AiBook }>(`/rules/${savedId}`);
      setBook(saved.book); setDraft(saved.id); setImportSuccess(saved.id); setImported(null); setImportedId(null);
      if (saved.book.rules.length) { setKind('rules'); setGroup(groupOf(saved.book.rules[0])); setIndex(0); }
      else { setKind('categories'); setGroup('category'); setIndex(0); }
    } catch (e) {
      const code = (e as { code?: string }).code;
      setError(savedId ? t('ai.ruleBookReadFailed', { version: savedId }) : code === 'ai_rules_changed' ? t('ai.ruleBookChanged') : t('ai.ruleBookRejected'));
    } finally { setBusy(false); }
  }
  function exportBook() {
    const url = URL.createObjectURL(new Blob([JSON.stringify(book, null, 2) + '\n'], { type: 'application/json' }));
    const link = document.createElement('a'); link.href = url; link.download = 'hub-ai-rules.json';
    document.body.appendChild(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 0);
  }
  function add() {
    const id = `profile_${Date.now()}`;
    const value = kind === 'rules' ? { id, name: t('ai.newRule'), instructions: '', policy: {}, enabled: false, official_domains: [], scope: { shop: '', supplier: '', category: '', brand: '', product: '', ...(group === 'common' ? {} : {[group]: t('ai.fillScope')}) } }
      : { id, name: t('ai.newCategory'), instructions: '', policy: {}, parameters: [], shop_categories: {}, automatic_import_ready: false };
    setBook(old => ({ ...old, [kind]: [...old[kind], value] })); setIndex(book[kind].length); setDraft(null); setImportSuccess(null);
  }
  return <>
    <div className="ai-notice">{t('ai.rulesHelp')}</div>
    <details className="ai-card"><summary>{t('ai.ruleBookTransfer')}</summary>
      <p>{t('ai.ruleBookWholeVersion')}</p>
      <button disabled={busy} onClick={exportBook}>{t('ai.ruleBookExport')}</button>
      <label>{t('ai.ruleBookFile')}<input type="file" accept=".json,application/json" disabled={busy} onChange={e => { const file = e.target.files?.[0]; e.target.value = ''; void readBookFile(file); }} /></label>
      {imported && <><p>{t('ai.ruleBookSelected', { file: imported.name, rules: imported.rules, categories: imported.categories })}</p>
        <p>{t('ai.ruleBookAwaitingValidation')}</p>
        <div className="ai-actions"><button disabled={busy || !note.trim()} onClick={saveImportedBook}>{t(importedId ? 'ai.ruleBookReloadSaved' : 'ai.ruleBookSaveImported')}</button>
          <button disabled={busy} onClick={() => { setImported(null); setImportedId(null); }}>{t('ai.ruleBookCancel')}</button></div></>}
      {importSuccess && <p role="status">{t('ai.ruleBookSaved', { version: importSuccess, published: rules.published_id })}</p>}
    </details>
    <nav className="ai-rule-groups" aria-label={t('ai.ruleGroupsLabel')}>{['common','supplier','brand','category','shop','product'].map(key => key === 'category'
      ? <button key={key} aria-selected={categoryView} className={categoryView ? 'ai-primary' : ''} onClick={() => { setGroup('category'); setKind('categories'); setIndex(0); }}>{t('ai.ruleGroups.category')} ({book.categories.length})</button>
      : <button key={key} aria-selected={group === key && kind === 'rules'} className={group === key && kind === 'rules' ? 'ai-primary' : ''} onClick={() => chooseGroup(key)}>{t(`ai.ruleGroups.${key}`)} ({book.rules.filter(r => groupOf(r) === key).length})</button>)}</nav>
    {categoryView && categoryRules.length > 0 && <details><summary>{t('ai.legacyCategoryRules')}</summary><p>{t('ai.legacyCategoryRulesHelp')}</p><div className="ai-actions">{categoryRules.map(({r,i}) => <button key={r.id} onClick={() => { setGroup('category'); setKind('rules'); setIndex(i); }}>{r.name}</button>)}</div></details>}
    <div className="ai-toolbar">{!(kind === 'rules' && group === 'category') && <button onClick={add}>{t('ai.add')}</button>}<small>{t('ai.ruleOrder')}</small></div>
    <div className="ai-columns"><aside className="ai-card"><label>{t('ai.chooseProfile')}<select value={index} onChange={e => setIndex(Number(e.target.value))}>{choices.map(({r,i}) => <option key={r.id} value={i}>{r.name}</option>)}</select></label>
      <p>{t('ai.publishedVersion', { version: rules.published_id })}</p>
      <label>{t('ai.versionHistory')}<select value="" disabled={busy} onChange={e => execute(async () => { const value = await aiRequest<{ book: AiBook; id: number }>(`/rules/${e.target.value}`); setBook(value.book); setIndex(kind === 'categories' ? 0 : value.book.rules.findIndex(r => groupOf(r) === group)); setDraft(value.id); setImportSuccess(null); })}><option value="">{t('ai.loadVersion')}</option>{rules.versions.map(v => <option key={v.id} value={v.id}>#{v.id} · {v.note}</option>)}</select></label>
      <p>{t('ai.restoreHelp')}</p>
    </aside><section className="ai-card">{!selected && <p>{t('ai.noRulesInGroup')}</p>}{selected && <><button onClick={remove} disabled={busy}>{t('ai.removeRule')}</button>
      <div className="ai-grid"><label>{t('ai.profileName')}<input value={selected.name} onChange={e => change({ name: e.target.value })} /></label><label>{t('ai.profileId')}<input value={selected.id} onChange={e => change({ id: e.target.value })} /></label></div>
      {rule && <>{!!rule.category_profiles?.length && <p>{t('ai.linkedProfiles')}: {rule.category_profiles.map(id => book.categories.find(c => c.id === id)?.name || id).join(', ')}</p>}<label className="ai-check"><input type="checkbox" checked={rule.enabled} onChange={e => change({ enabled: e.target.checked })} />{t('ai.enabledRule')}</label><h3>{t('ai.scope')}</h3><div className="ai-grid">{(['shop', 'supplier', 'category', 'brand', 'product'] as const).map(key => <label key={key}>{t(`ai.scopeFields.${key}`)}<input value={rule.scope[key]} placeholder={t('ai.all')} onChange={e => change({ scope: { ...rule.scope, [key]: e.target.value } })} /></label>)}</div><label>{t('ai.officialDomains')}<input value={rule.official_domains.join(', ')} placeholder="northfinder.com" onChange={e => change({ official_domains: e.target.value.split(',').map(s => s.trim()).filter(Boolean) })} /></label></>}
      {rule && <details><summary>{t('ai.importPolicy')}</summary><p>{t('ai.importPolicyHelp')}</p><p>{t('ai.supplierAvailabilityConfigHelp')}</p><label>{t('ai.importPolicyFields.supplier_name')}<input value={rule.import_policy?.supplier_name || ''} onChange={e => change({import_policy:{...rule.import_policy,supplier_name:e.target.value || null}})} /></label></details>}
      <AiPolicyFields value={selected.policy} onChange={policy => change({ policy })} />
      <label>{t('ai.instructions')}<textarea className="ai-long-text" value={selected.instructions} onChange={e => change({ instructions: e.target.value })} /></label>
      {category && <><p>{t('ai.registryStatus')}: {t(`ai.registryStatuses.${category.registry_status || 'approved'}`)} · {category.parameters.filter(p => p.approved !== false).length} {t('ai.approvedParameters')}</p>
        <details><summary>{t('ai.linkedCategoryInstructions')}</summary>{book.rules.filter(r => r.enabled && (r.scope.category === category.id || r.category_profiles?.includes(category.id))).map(r => <details key={r.id}><summary>{r.name}</summary><pre className="ai-original">{r.instructions}</pre></details>)}</details>
        <h3>{t('ai.shopMapping')}</h3><div className="ai-grid">{['biketrek', 'xtrek', ...Object.keys(category.shop_categories).filter(k => !['biketrek', 'xtrek'].includes(k))].map(shop => <label key={shop}>{shop}<CategoryTree categories={shopOptions[shop]?.categories || []} value={category.shop_categories[shop] || ''} onChange={code => change({ shop_categories: { ...category.shop_categories, [shop]: code } })} /></label>)}</div>
        <label className="ai-check"><input type="checkbox" checked={category.automatic_import_ready} onChange={e => change({ automatic_import_ready: e.target.checked })} />{t('ai.categoryAutomationReady')}</label>
        <h3>{t('ai.parameterRegistry')}</h3><p>{t('ai.parametersHelp')}</p>
        {category.parameters.map((param, i) => <div className="ai-card" key={i}>{param.approved === false && <p className="ai-notice">{t('ai.draftParameterHelp')}</p>}<div className="ai-grid"><label>{t('ai.parameterName')}<input value={param.name} onChange={e => change({ parameters: category.parameters.map((p, n) => n === i ? { ...p, name: e.target.value } : p) })} /></label><label>{t('ai.parameterScope')}<select value={param.scope} onChange={e => change({ parameters: category.parameters.map((p, n) => n === i ? { ...p, scope: e.target.value as 'parent' | 'variant' | 'choice' } : p) })}><option value="parent">{t('ai.parent')}</option><option value="variant">{t('ai.variant')}</option><option value="choice">{t('ai.choice')}</option></select></label>
          <label>{t('ai.allowedValues')}<input value={param.values.join(' | ')} onChange={e => change({ parameters: category.parameters.map((p, n) => n === i ? { ...p, values: e.target.value.split('|').map(s => s.trim()).filter(Boolean) } : p) })} /></label><label>{t('ai.unit')}<input value={param.unit} onChange={e => change({ parameters: category.parameters.map((p, n) => n === i ? { ...p, unit: e.target.value } : p) })} /></label></div>
          <label>{t('ai.instructions')}<input value={param.instructions} onChange={e => change({ parameters: category.parameters.map((p, n) => n === i ? { ...p, instructions: e.target.value } : p) })} /></label>
          <div className="ai-actions"><label className="ai-check"><input type="checkbox" checked={param.required} onChange={e => change({ parameters: category.parameters.map((p, n) => n === i ? { ...p, required: e.target.checked } : p) })} />{t('ai.required')}</label><button onClick={() => change({ parameters: category.parameters.filter((_, n) => n !== i) })}>{t('ai.removeDraftEntry')}</button></div>
        </div>)}<button onClick={() => change({ parameters: [...category.parameters, { name: '', required: false, scope: 'parent', values: [], unit: '', instructions: '' }] })}>{t('ai.addParameter')}</button></>}
      <details><summary>{t('ai.comparePublished')}</summary><div className="ai-grid"><pre className="ai-original">{rules.book[kind].find(r => r.id === selected.id)?.instructions || t('ai.newProfile')}</pre><pre className="ai-original">{selected.instructions}</pre></div></details>

      <details><summary>{t('ai.aiRuleAssistant')}</summary><p>{t('ai.proposalHelp')}</p><textarea value={prompt} onChange={e => setPrompt(e.target.value)} placeholder={t('ai.proposalPlaceholder')} /><button disabled={busy || !prompt.trim() || !rules.book[kind].some(r => r.id === selected.id)} onClick={() => execute(async () => { onJob(await aiRequest<AiJob>('/rules/proposal', { request_id: crypto.randomUUID(), rule_id: selected.id, category: kind === 'categories', request: prompt })); })}>{t('ai.prepareProposal')}</button></details>
    </>}</section></div><div className="ai-card">      <label>{t('ai.changeNote')}<input value={note} onChange={e => setNote(e.target.value)} /></label>
      <div className="ai-actions"><button disabled={busy || !!imported || !note.trim()} onClick={() => execute(async () => { const result = await aiRequest<{ id: number }>('/rules', { expected_published: rules.published_id, book, note }); setDraft(result.id); setImportSuccess(null); })}>{t('ai.saveDraft')}</button>
        <button className="ai-primary" disabled={busy || !!imported || !draft || draft === rules.published_id} aria-describedby={!draft ? 'ai-publish-help' : undefined} onClick={() => execute(async () => { await aiRequest(`/rules/${draft}/publish`, { expected_published: rules.published_id }); onReload(); })}>{draft ? t('ai.publishVersion', { version: draft }) : t('ai.publishDraft')}</button></div>{!draft && <p id="ai-publish-help">{t('ai.publishDraftHelp')}</p>}</div>{error && <div role="alert" className="ai-notice ai-error">{error}</div>}
  </>;
}
