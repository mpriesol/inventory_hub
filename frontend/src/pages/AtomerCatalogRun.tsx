import React, {useEffect, useState} from 'react';
import {useTranslation} from 'react-i18next';
import {Link} from 'react-router-dom';
import {hubRequest} from '../api/access';

type Family = {family_key:string; reason:string|null; leaves:number};
type Counts = Record<'families'|'total'|'verified'|'pending'|'partial'|'blocked'|'excluded',number> &
  Partial<Record<'hub_cards'|'cards_created'|'cards_reused'|'native_families'|'manual_exclusions',number>>;
type Run = {campaign_id?:string; state:string; counts:Counts; next_retry_at?:string; updated_at?:string;
  last_problem?:string; blocked:Family[]; recent:Family[]};
type ShopTarget = {product_id?:string|number;variant_id?:string|number|null};
type Proof = {code:string;state:string;evidence:{card_id?:number;hub_sku?:string;ean?:string;xtrek?:ShopTarget|string|null;
  biketrek?:ShopTarget|string|null;reason?:string}};
function nativeId(shop:Proof['evidence']['xtrek']) {
  if (!shop) return '—';
  if (typeof shop==='string') return shop;
  return [shop.product_id,shop.variant_id].filter(x=>x!=null).join(' / ') || '—';
}

function problemKey(problem: string) {
  if (/rate_limited|budget_deferred|http[_ ]429|Limit Upgates/i.test(problem)) return 'quota';
  if (/registry_binding_changed|category_mapping_changed/.test(problem)) return 'registryChanged';
  if (/source_unavailable|changes_unavailable/.test(problem)) return 'upstream';
  if (/source_changed|variant_source_changed/.test(problem)) return 'sourceChanged';
  return '';
}

export function AtomerCatalogRun({revision,onFamily}:{revision:number;onFamily:(key:string)=>void}) {
  const {t,i18n}=useTranslation();
  const [run,setRun]=useState<Run|null>(null);
  const [failed,setFailed]=useState(false);
  const [family,setFamily]=useState('');
  const [proofs,setProofs]=useState<Proof[]|null>(null);
  const [proofError,setProofError]=useState(false);
  useEffect(()=>{
    setProofs(null);setProofError(false);
    if (!family) return;
    const controller=new AbortController();
    void hubRequest<{items:Proof[]}>('/api/migration/atomer-progress/catalog-engine/family?'+new URLSearchParams({family}),
      undefined,controller.signal).then(response=>{if (!controller.signal.aborted) setProofs(response.items);})
      .catch(()=>{if (!controller.signal.aborted) setProofError(true);});
    return ()=>controller.abort();
  },[family,run?.updated_at]);
  const m=(key:string)=>t('atomerProgress.engine.'+key);
  const number=(n:number)=>Number(n).toLocaleString(i18n.language);
  const date=(s:string)=>new Date(s).toLocaleString(i18n.language,{timeZone:'Europe/Bratislava',dateStyle:'short',timeStyle:'short'});
  useEffect(()=>{
    const controller=new AbortController();
    let timer:number|undefined;
    async function read() {
      let delay=30000;
      try {
        const current=await hubRequest<Run>('/api/migration/atomer-progress/catalog-engine',undefined,controller.signal);
        if (controller.signal.aborted) return;
        setRun(current); setFailed(false);
        if (current.state==='running') delay=12000;
      } catch {
        if (!controller.signal.aborted) setFailed(true);
      } finally {
        if (!controller.signal.aborted) timer=window.setTimeout(read,delay);
      }
    }
    void read();
    return ()=>{controller.abort();window.clearTimeout(timer);};
  },[revision]);
  if (!run?.campaign_id) return failed?<p className="progress-warning">{m('unavailable')}</p>:null;
  const c=run.counts;
  const issue=run.last_problem && problemKey(run.last_problem);
  return <section className="atomer-campaign" aria-label={m('title')}>
    <header><h2>{m('title')}</h2><span className={run.state==='review'||run.state==='source_changed'?'progress-warning':'progress-meta'}>
      {m('states.'+run.state)}</span></header>
    <p><strong>{number(c.verified)} / {number(c.total)}</strong> {m('verifiedLeaves')} · {number(c.families)} {m('families')}</p>
    <progress value={c.verified} max={Math.max(c.total,1)} aria-label={m('verifiedLeaves')}/>
    <div className="campaign-counts">{(['pending','partial','blocked','excluded'] as const).map(k=><span key={k}>
      {m(k)} <strong>{number(k==='excluded' ? c.manual_exclusions??c[k] : c[k])}</strong></span>)}</div>
    {c.hub_cards!=null&&<dl className="campaign-milestones">
      <div><dt>{m('hubCards')}</dt><dd>{number(c.hub_cards)}</dd></div>
      {c.cards_created!=null&&<div><dt>{m('cardsCreated')}</dt><dd>{number(c.cards_created)}</dd></div>}
      {c.cards_reused!=null&&<div><dt>{m('cardsReused')}</dt><dd>{number(c.cards_reused)}</dd></div>}
      {c.native_families!=null&&<div><dt>{m('nativeFamilies')}</dt><dd>{number(c.native_families)}</dd></div>}
    </dl>}
    {run.next_retry_at&&<p className="progress-meta">{m('next')} {date(run.next_retry_at)}</p>}
    {run.last_problem && (issue
      ? <details className="progress-warning"><summary>{m('problems.'+issue)}</summary>
          <small>{run.last_problem}</small></details>
      : <p className="progress-warning">{run.last_problem}</p>)}
    {!!run.blocked?.length&&<details><summary>{m('showIssues')}</summary>
      <ul>{run.blocked.map(f=><li key={f.family_key}><button className="review-count-link" onClick={()=>onFamily(f.family_key)}>{f.family_key.replace(/^[vs]:/,'')}</button>
        {' · '}{f.reason} ({number(f.leaves)})</li>)}</ul>
    </details>}
    {!!run.recent?.length&&<details><summary>{m('receipts')}</summary>
      <label>{m('product')} <select value={family} onChange={e=>setFamily(e.target.value)}>
        <option value="">{m('choose')}</option>
        {[...new Set([family,...run.recent.map(r=>r.family_key),...run.blocked.map(r=>r.family_key)])].filter(Boolean).map(k=>
          <option key={k} value={k}>{k.replace(/^[vs]:/,'')}</option>)}</select></label>
      {family&&<button className="review-count-link" onClick={()=>onFamily(family)}>{m('openProduct')}</button>}
      {family&&proofError&&<p className="progress-warning">{m('unavailable')}</p>}
      {family&&proofs?.length===0&&<p>{m('awaitingProof')}</p>}
      {!!proofs?.length&&<div className="campaign-proof-table"><table>
        <thead><tr>{['SKU','EAN',m('card'),'xTrek','BIKETREK',m('result')].map(k=><th key={k}>{k}</th>)}</tr></thead>
        <tbody>{proofs.map(p=><tr key={p.code}><td>{p.code}</td><td>{p.evidence.ean||'—'}</td>
          <td>{p.evidence.hub_sku && p.evidence.card_id
            ? <Link to={'/products/'+encodeURIComponent(p.evidence.hub_sku)}>{p.evidence.card_id}</Link>
            : p.evidence.card_id??'—'}</td>
          <td>{nativeId(p.evidence.xtrek)}</td><td>{nativeId(p.evidence.biketrek)}</td><td>{p.evidence.reason||m('proof.'+p.state)}</td></tr>)}</tbody>
      </table></div>}
    </details>}
    {failed&&<p className="progress-warning">{m('unavailable')}</p>}
  </section>;
}
