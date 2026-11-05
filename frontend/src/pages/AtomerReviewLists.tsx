import React,{useEffect,useState} from 'react';
import {useTranslation} from 'react-i18next';
import {Decision,Extra,readQueue,readTargets,Subject} from '../api/atomerProgress';

export function AtomerReviewLists({view,initialQuery,revision,onOpen}:{
  view:'remove'|'ai'; initialQuery:string;revision:number;onOpen:(s:Subject,run:number,decision?:Decision)=>void}) {
  const {t}=useTranslation(), m=(k:string)=>t('atomerProgress.'+k);
  const [search,setSearch]=useState(initialQuery), [decision,setDecision]=useState(''), [stage,setStage]=useState('queued');
  const [offset,setOffset]=useState(0),[busy,setBusy]=useState(true),[error,setError]=useState('');
  const [data,setData]=useState<{run_id:number; total:number; extras:Extra[]; tasks:Subject[]}|null>(null);
  useEffect(()=>{
    const controller=new AbortController();
    setBusy(true);setError('');
    const params=new URLSearchParams({offset:String(offset),limit:'25'});
    const timer=window.setTimeout(async()=>{
      try {
        if (view==='remove') {
          params.set('search',search.trim());params.set('decision',decision);
          const r=await readTargets(params,controller.signal);
          if (!controller.signal.aborted) setData({run_id:r.run_id,total:r.total,extras:r.items,tasks:[]});
        } else {
          params.set('status',stage);
          const r=await readQueue(params,controller.signal);
          if (!controller.signal.aborted) setData({run_id:r.run_id,total:r.total,extras:[],tasks:r.items});
        }
      } catch(e:any){if(!controller.signal.aborted){setError(e?.code||'failed');setData(null);}}
      finally {if(!controller.signal.aborted)setBusy(false);}
    },150);
    return ()=>{window.clearTimeout(timer);controller.abort();};
  },[view,revision,search,decision,stage,offset]);
  function openTarget(r:Extra,d?:Decision) {
    if(data) onOpen({kind:'extra',key:r.key,name:r.name,work:r.work,target:r,suggestions:['inspectExtra']},data.run_id,d);
  }
  const isRemove=view==='remove';
  return <section className="review-list">
    <p className="progress-meta">{m(isRemove?'removeIntro':'queueIntro')}</p>
    <div className="progress-filters">{isRemove ? <>
      <label>{m('search')}<input type="search" value={search} onChange={e=>{setSearch(e.target.value);setOffset(0);}} placeholder={m('searchHint')}/></label>
      <label>{m('delete')}<select value={decision} onChange={e=>{setDecision(e.target.value);setOffset(0);}}>
        <option value="">{m('all')}</option>{['undecided','yes','no'].map(k=><option key={k} value={k}>{m('decision.'+k)}</option>)}</select></label>
    </>:<label>{m('queueStatus')}<select value={stage} onChange={e=>{setStage(e.target.value);setOffset(0);}}>
      {['queued','working','blocked','done'].map(k=><option key={k} value={k}>{m('workStatus.'+k)}</option>)}</select></label>}</div>
    {isRemove && data?.extras.some(r=>r.warnings.includes('admin_absence_unknown')) && <p className="progress-warning">{m('warn.admin_absence_unknown')}</p>}
    {error && <p role="alert" className="progress-warning">{m('errors.failed')}</p>}
    <div className="progress-grid" aria-busy={busy}><table><thead><tr>
      {(isRemove?[m('remoteVariant'),m('delete'),m('reviewCheck'),m('card'),m('workTitle')]:[m('subjectLabel'),m('instruction'),m('lastResult'),m('workTitle')]).map(h=><th key={h}>{h}</th>)}</tr></thead>
      <tbody>{isRemove?data?.extras.map(r=><tr key={r.key}>
        <td><strong>{r.name}</strong><small>{Object.values(r.axes).flat().join(' / ')}</small><small>{r.code} · EAN {r.ean||'—'}</small>
          <div className="progress-shop-links">{r.shop_url&&<a href={r.shop_url} target="_blank" rel="noreferrer">{m('web')}</a>}
          {r.shop_admin_url&&<a href={r.shop_admin_url} target="_blank" rel="noreferrer">{m('admin')}</a>}</div>
          <small>ID {r.product_id} / {r.variant_id}</small></td>
        <td><select aria-label={m('delete')+' '+r.code} value={r.work.decision} disabled={busy||r.work.status==='working'}
          onChange={e=>openTarget(r,e.target.value as Decision)}>
          {['undecided','no','yes'].map(k=><option value={k} key={k} disabled={k==='yes'&&r.check!=='eligible'}>{m('decision.'+k)}</option>)}</select></td>
        <td>{m('checks.'+r.check)}{r.warnings.filter(k=>k!=='admin_absence_unknown').map(k=><small className="progress-warning" key={k}>{m('warn.'+k)}</small>)}
          {!!r.alternatives.length&&<small>{r.alternatives.join(', ')}</small>}<small>{m('stockX')} {r.stock??'—'}</small></td>
        <td>{r.card_ids.length?r.card_ids.map(n=>'#'+n).join(', '):'—'}</td>
        <td><button disabled={busy} onClick={()=>openTarget(r)}>{m('edit')}</button><small>{m('workStatus.'+r.work.status)}</small></td>
      </tr>):data?.tasks.map(s=><tr key={s.kind+s.key}>
        <td><strong>{s.name}</strong><small>{m('subject.'+s.kind)} · {s.key}</small><small>{m('scopes.'+s.work.scope)}</small></td>
        <td className="review-pre">{s.work.instruction||m('approvedRemoval')}{s.work.note&&<small>{m('note')} · {s.work.note}</small>}</td>
        <td className="review-pre">{['target_recheck_required','admin_source_required'].includes(s.work.result)?m('errors.'+s.work.result):s.work.result||'—'}</td>
        <td><button disabled={busy} onClick={()=>data&&onOpen(s,data.run_id)}>{m('openTask')}</button></td></tr>)}</tbody></table></div>
    {data?.total===0 && !busy && <p className="review-empty">{m(isRemove?'emptyTargets':'emptyQueue')}</p>}
    {data && data.total>0 && <div className="progress-pager"><span>{offset+1}–{Math.min(offset+25,data.total)} / {data.total}</span>
      <button disabled={busy||offset===0} onClick={()=>setOffset(n=>Math.max(n-25,0))}>{m('previous')}</button>
      <button disabled={busy||offset+25>=data.total} onClick={()=>setOffset(n=>n+25)}>{m('next')}</button></div>}
  </section>;
}
