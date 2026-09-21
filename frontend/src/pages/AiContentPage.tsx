import React, { useEffect, useRef, useState } from 'react';
import { Link, useLocation } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Sparkles } from 'lucide-react';
import { AiJob, AiPolicy, AiRules, AiStatus, aiRequest, aiUnlocked, unlockAi } from '../api/aiContent';
import { CatalogProduct, CatalogStatus, ImportOptions, TargetOptions, catalogImageUrl, catalogRequest } from '../api/catalog';
import { AiPolicyFields } from '../components/product/AiPolicyFields';
import { AiRuleEditor } from '../components/product/AiRuleEditor';
import { AiJobDetail } from '../components/product/AiJobDetail';
import './AiContentPage.css';
import './SupplierCatalogPage.css';

interface Selection { supplier: string; feed_key: string; product_ids: number[]; run_id: number | null; shop: string; options: ImportOptions }
interface Family { code: string; name: string; image: string | null; product_ids: number[]; products: CatalogProduct[] }
interface Target { shop: string; options: ImportOptions; policy: AiPolicy }
const fallbackOptions: ImportOptions = { language: 'sk', currency: 'EUR', pricelist: 'Predvolené', category_code: null, pricing: 'configured', include_images: true, include_description: true, include_parameters: true };
const processing = new Set(['queued', 'generating', 'preparing_import', 'import_queued', 'importing']);

export function AiContentPage() {
  const { t } = useTranslation();
  const location = useLocation();
  const selection = (location.state as { selection?: Selection } | null)?.selection;
  const [tab, setTab] = useState(selection ? 'prepare' : location.pathname.includes('settings') ? 'rules' : 'jobs');
  const [status, setStatus] = useState<AiStatus | null>(null);
  const [unlocked, setUnlocked] = useState(aiUnlocked());
  const [token, setToken] = useState('');
  const [rules, setRules] = useState<AiRules | null>(null);
  const [families, setFamilies] = useState<Family[]>([]);
  const [aiFamilies, setAiFamilies] = useState<Set<string>>(new Set());
  const [profiles, setProfiles] = useState<Record<string, string>>({});
  const [shops, setShops] = useState<CatalogStatus['shops']>([]);
  const [targets, setTargets] = useState<Target[]>([]);
  const [targetOptions, setTargetOptions] = useState<Record<string, TargetOptions>>({});
  const [research, setResearch] = useState('official');
  const [jobs, setJobs] = useState<AiJob[]>([]);
  const [selectedJobs, setSelectedJobs] = useState<Set<string>>(new Set());
  const [detail, setDetail] = useState<AiJob | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [reload, setReload] = useState(0);
  const batchRequestId = useRef<string | null>(null);
  const detailRef = useRef(detail); detailRef.current = detail;
  useEffect(() => { aiRequest<AiStatus>('/status').then(setStatus).catch(e => setError(e.message)); }, []);
  async function loadRules() { setRules(await aiRequest<AiRules>('/rules')); }
  async function execute(fn: () => Promise<void>) { setBusy(true); setError(''); try { await fn(); } catch (e) { setError((e as Error).message); } finally { setBusy(false); } }
  useEffect(() => { if (!unlocked) return; loadRules().catch(e => setError(e.message)); }, [unlocked, reload]);
  useEffect(() => {
    if (!unlocked) return;
    let stopped = false;
    const poll = async () => {
      try {
        const data = await aiRequest<AiJob[]>('/jobs'); if (stopped) return; setJobs(data);
        const current = detailRef.current;
        if (current && processing.has(current.status)) {
          const next = await aiRequest<AiJob>(`/jobs/${current.id}`); if (!stopped) setDetail(next);
        }
      } catch (e) { if (!stopped) setError((e as Error).message); }
    };
    poll(); const timer = window.setInterval(poll, 5000); return () => { stopped = true; window.clearInterval(timer); };
  }, [unlocked, reload]);
  useEffect(() => {
    if (!unlocked || !selection) return;
    let stopped = false;
    execute(async () => {
      const [families, supplier] = await Promise.all([
        aiRequest<Family[]>('/selection', { supplier: selection.supplier, feed_key: selection.feed_key, product_ids: selection.product_ids, run_id: selection.run_id }),
        catalogRequest<CatalogStatus>(`/suppliers/${selection.supplier}/catalog?feed_key=${selection.feed_key}`),
      ]);
      if (stopped) return;
      setFamilies(families); setAiFamilies(new Set(families.map(f => f.code))); setShops(supplier.shops.filter(s => s.ready));
      if (selection.shop) await addTarget(selection.shop, selection.options);
    });
    return () => { stopped = true; };
  }, [unlocked, selection]);
  async function addTarget(shop: string, selectedOptions?: ImportOptions) {
    const remote = targetOptions[shop] || await catalogRequest<TargetOptions>(`/shops/${shop}/import/options`);
    setTargetOptions(old => ({ ...old, [shop]: remote }));
    const language = remote.languages.find(l => l.code === 'sk') || remote.languages[0];
    const options = selectedOptions || { ...fallbackOptions, language: language?.code || 'sk', currency: language?.currency || 'EUR', pricelist: remote.pricelists.find(p => p.default)?.name || remote.pricelists[0]?.name || 'Predvolené' };
    setTargets(old => old.some(v => v.shop === shop) ? old : [...old, { shop, options, policy: {} }]);
    batchRequestId.current = null;
  }
  function changeTarget(shop: string, change: Partial<Target>) { batchRequestId.current = null; setTargets(old => old.map(v => v.shop === shop ? { ...v, ...change } : v)); }
  function showJob(job: AiJob) { setDetail(job); setTab('jobs'); setReload(v => v + 1); }
  async function prepare() {
    if (!selection) return;
    batchRequestId.current ||= crypto.randomUUID();
    const result = await aiRequest<{ jobs: AiJob[] }>('/batches', { request_id: batchRequestId.current,
      supplier: selection.supplier, feed_key: selection.feed_key, run_id: selection.run_id, product_ids: selection.product_ids,
      ai_product_ids: families.filter(f => aiFamilies.has(f.code)).flatMap(f => f.product_ids),
      category_profiles: Object.fromEntries(families.flatMap(f => f.product_ids.map(id => [id, profiles[f.code] || 'general']))), targets, research });
    setJobs(result.jobs); setSelectedJobs(new Set(result.jobs.map(j => j.id))); setTab('jobs'); setReload(v => v + 1);
    if (result.jobs.length) setDetail(await aiRequest<AiJob>(`/jobs/${result.jobs[0].id}`));
  }
  return <div className="ai-content"><header><div><h1><Sparkles size={24} style={{ display: 'inline', marginRight: 10 }} />{t('ai.title')}</h1><p>{t('ai.subtitle')}</p></div><Link to="/suppliers">{t('ai.backSuppliers')}</Link></header>
    {status && <div className="ai-toolbar"><span className="ai-badge">{t(status.enabled && status.key_configured ? 'ai.connected' : 'ai.notConfigured')}</span><small>{status.model} · {t('ai.monthlyBudget')}: {rules?.used_usd || '0'} / {status.monthly_limit_usd} USD</small></div>}
    {status && (!status.enabled || !status.key_configured || !status.access_configured) && <div className="ai-notice">{t('ai.setupHelp')}<details><summary>{t('ai.setupDetails')}</summary><p>{t('ai.setupInstructions')}</p><code>OPENAI_API_KEY · AI_CONTENT_ACCESS_TOKEN · AI_CONTENT_ENABLED</code><p>{t('ai.independentAccount')}</p><a href="https://platform.openai.com/" target="_blank" rel="noopener noreferrer">OpenAI Platform ↗</a></details></div>}
    {!unlocked ? <form className="ai-card" onSubmit={e => { e.preventDefault(); execute(async () => { unlockAi(token); await loadRules(); setUnlocked(true); setToken(''); }); }}><h2>{t('ai.unlock')}</h2><p>{t('ai.unlockHelp')}</p><label>{t('ai.hubAccessToken')}<input type="password" autoComplete="off" value={token} onChange={e => setToken(e.target.value)} /></label><div className="ai-actions"><button className="ai-primary" disabled={busy || !token}>{t('ai.unlock')}</button></div></form> : <>
      <nav className="ai-tabs">{(selection ? ['prepare', 'jobs', 'rules'] : ['jobs', 'rules']).map(key => <button key={key} aria-selected={tab === key} onClick={() => setTab(key)}>{t(`ai.tabs.${key}`)}</button>)}<button onClick={() => { unlockAi(''); setUnlocked(false); setRules(null); setDetail(null); }}>{t('ai.lock')}</button></nav>
      {tab === 'prepare' && selection && <>
        <div className="ai-notice">{t('ai.prepareHelp')}</div>
        <div className="ai-card"><div className="ai-row"><h2 className="ai-grow">{t('ai.selectedProducts')}</h2><button onClick={() => { setAiFamilies(new Set(families.map(f => f.code))); batchRequestId.current = null; }}>{t('ai.aiAll')}</button><button onClick={() => { setAiFamilies(new Set()); batchRequestId.current = null; }}>{t('ai.aiNone')}</button></div>
          {families.map(f => <div className="ai-product" key={f.code}>{f.image ? <img src={catalogImageUrl(f.image)} alt="" /> : <span />}<div><strong>{f.name}</strong><div><small>{f.code} · {t('ai.selectedVariants', { count: f.product_ids.length })}</small></div><details><summary>{t('ai.variants')}</summary>{f.products.map(p => <div key={p.id}>{p.shop_code} · {p.variant_attributes.map(a => a.value).join(' / ')}</div>)}</details></div>
            <label>{t('ai.categoryProfile')}<select value={profiles[f.code] || 'general'} onChange={e => { setProfiles(old => ({ ...old, [f.code]: e.target.value })); batchRequestId.current = null; }}>{rules?.book.categories.map(c => <option key={c.id} value={c.id}>{c.name}</option>)}</select></label>
            <label className="ai-check"><input type="checkbox" checked={aiFamilies.has(f.code)} onChange={e => { setAiFamilies(old => { const next = new Set(old); e.target.checked ? next.add(f.code) : next.delete(f.code); return next; }); batchRequestId.current = null; }} />{t('ai.enrich')}</label></div>)}
          <label>{t('ai.researchMode')}<select value={research} onChange={e => { setResearch(e.target.value); batchRequestId.current = null; }}><option value="official">{t('ai.researchOfficial')}</option><option value="feed_only">{t('ai.researchFeed')}</option></select></label>
        </div>
        <div className="ai-card"><h2>{t('ai.targetShops')}</h2><div className="ai-toolbar">{shops.map(s => <label key={s.code} className="ai-check"><input type="checkbox" checked={targets.some(v => v.shop === s.code)} disabled={busy} onChange={e => e.target.checked ? execute(() => addTarget(s.code)) : (setTargets(old => old.filter(v => v.shop !== s.code)), batchRequestId.current = null)} />{s.name}</label>)}</div>
          {targets.map(target => <div className="ai-card" key={target.shop}><h3>{target.shop}</h3><div className="ai-grid"><label>{t('ai.fallbackCategory')}<select value={target.options.category_code || ''} onChange={e => changeTarget(target.shop, { options: { ...target.options, category_code: e.target.value || null } })}><option value="">{t('ai.noCategory')}</option>{targetOptions[target.shop]?.categories.map(c => <option key={c.code} value={c.code}>{c.names[target.options.language] || c.code} · {c.code}</option>)}</select></label><label>{t('ai.pricelist')}<select value={target.options.pricelist} onChange={e => changeTarget(target.shop, { options: { ...target.options, pricelist: e.target.value } })}>{targetOptions[target.shop]?.pricelists.map(p => <option key={p.name}>{p.name}</option>)}</select></label></div><AiPolicyFields value={target.policy} onChange={policy => changeTarget(target.shop, { policy })} /><small>{t('ai.mappingPriority')}</small></div>)}
          <p>{t('ai.automaticNotice')}</p><button className="ai-primary" disabled={busy || !families.length || !targets.length} onClick={() => execute(prepare)}>{busy ? t('common.loading') : t('ai.prepareBatch')}</button>
        </div>
      </>}
      {tab === 'jobs' && <><div className="ai-toolbar"><h2>{t('ai.recentJobs')}</h2><button disabled={busy || !jobs.some(j => j.status === 'estimate' && selectedJobs.has(j.id))} onClick={() => execute(async () => { for (const job of jobs.filter(j => j.status === 'estimate' && selectedJobs.has(j.id))) await aiRequest(`/jobs/${job.id}/action`, { action: 'start', expected_revision: job.revision }); setReload(v => v + 1); if (detail) setDetail(await aiRequest<AiJob>(`/jobs/${detail.id}`)); })}>{t('ai.startWaiting')}</button><button disabled={busy || !jobs.some(j => j.status === 'ready' && selectedJobs.has(j.id))} onClick={() => execute(async () => { for (const job of jobs.filter(j => j.status === 'ready' && selectedJobs.has(j.id))) await aiRequest(`/jobs/${job.id}/action`, { action: 'import', expected_revision: job.revision }); setReload(v => v + 1); })}>{t('ai.importSelectedReady')}</button><button onClick={() => setReload(v => v + 1)}>{t('ai.refresh')}</button></div>
        <p>{t('ai.jobPersistence')}</p>{!jobs.length && <div className="ai-card">{t('ai.noJobs')}</div>}
        {jobs.map(j => <div className="ai-card ai-row" key={j.id}><input type="checkbox" aria-label={`${t('ai.selectJob')} ${j.name} ${j.shop}`} checked={selectedJobs.has(j.id)} onChange={e => setSelectedJobs(old => { const next = new Set(old); e.target.checked ? next.add(j.id) : next.delete(j.id); return next; })} />{j.image && <img src={catalogImageUrl(j.image)} alt="" />}<div className="ai-grow"><strong>{j.name}</strong><div><small>{j.shop} · {j.code} · {j.use_ai ? 'AI' : t('ai.originalFeed')}</small></div></div><span className="ai-badge">{t(`ai.states.${j.status}`, { defaultValue: j.status })}</span>{j.policy.show_cost_estimate !== false && <small>{Number(j.actual_usd ?? j.estimate_usd).toFixed(3)} USD {j.actual_usd === null ? t('ai.estimated') : ''}</small>}<button disabled={busy} onClick={() => execute(async () => setDetail(await aiRequest<AiJob>(`/jobs/${j.id}`)))}>{t('ai.openJob')}</button></div>)}
        {detail && <AiJobDetail key={`${detail.id}:${detail.revision}`} job={detail} onChange={job => { setDetail(job); setReload(v => v + 1); }} />}
      </>}
      {tab === 'rules' && rules && <AiRuleEditor key={rules.published_id} rules={rules} onReload={() => setReload(v => v + 1)} onJob={showJob} />}
    </>}{error && <div role="alert" className="ai-notice ai-error">{error}</div>}
  </div>;
}
