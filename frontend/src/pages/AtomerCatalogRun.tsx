import React, {useEffect, useState} from 'react';
import {useTranslation} from 'react-i18next';
import {hubRequest} from '../api/access';

type Family = {family_key:string; reason:string|null; leaves:number};
type Counts = Record<'families'|'total'|'verified'|'pending'|'partial'|'blocked'|'excluded',number>;
type Run = {campaign_id?:string; state:string; counts:Counts; next_retry_at?:string; updated_at?:string;
  last_problem?:string; blocked:Family[]; recent:Family[]};
type ShopTarget = {product_id?:string|number;variant_id?:string|number|null};
type Proof = {code:string;state:string;evidence:{card_id?:number;ean?:string;xtrek?:ShopTarget|string|null;
  biketrek?:ShopTarget|string|null;reason?:string}};
function nativeId(shop:Proof['evidence']['xtrek']) {
  if (!shop) return '—';
  if (typeof shop==='string') return shop;
  return [shop.product_id,shop.variant_id].filter(x=>x!=null).join(' / ') || '—';
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
  return <section className="atomer-campaign" aria-label={m('title')}>
    <header><h2>{m('title')}</h2><span className={run.state==='review'||run.state==='source_changed'?'progress-warning':'progress-meta'}>
      {m('states.'+run.state)}</span></header>
    <p><strong>{number(c.verified)} / {number(c.total)}</strong> {m('verifiedLeaves')} · {number(c.families)} {m('families')}</p>
    <progress value={c.verified} max={Math.max(c.total,1)} aria-label={m('verifiedLeaves')}/>
    <div className="campaign-counts">{(['pending','partial','blocked','excluded'] as const).map(k=><span key={k}>{m(k)} <strong>{number(c[k])}</strong></span>)}</div>
    {run.next_retry_at&&<p className="progress-meta">{m('next')} {date(run.next_retry_at)}</p>}
    {run.last_problem&&<p className="progress-warning">{run.last_problem}</p>}
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
        <tbody>{proofs.map(p=><tr key={p.code}><td>{p.code}</td><td>{p.evidence.ean||'—'}</td><td>{p.evidence.card_id??'—'}</td>
          <td>{nativeId(p.evidence.xtrek)}</td><td>{nativeId(p.evidence.biketrek)}</td><td>{p.evidence.reason||m('proof.'+p.state)}</td></tr>)}</tbody>
      </table></div>}
    </details>}
    {failed&&<p className="progress-warning">{m('unavailable')}</p>}
  </section>;
}
