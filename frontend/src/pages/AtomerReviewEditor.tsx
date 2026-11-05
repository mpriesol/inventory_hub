import React, {useEffect, useRef, useState} from 'react';
import {useTranslation} from 'react-i18next';
import {Command, Decision, editWork, ProgressRow, Scope, Subject} from '../api/atomerProgress';

export function suggestionsFor(row:ProgressRow, scope:Scope='default') {
  const suggestions:string[] = [];
  if (row.card_state==='review' || row.reasons.length) suggestions.push('identity');
  if (row.card_state==='missing') suggestions.push('card');
  if (scope!=='biketrek') {
    if (row.state==='needs_link') suggestions.push('linkX');
    if (row.state==='needs_target' || row.state==='needs_both') suggestions.push('targetX');
  }
  if (scope!=='xtrek' && row.bike_state==='review') suggestions.push('checkB');
  if ((scope==='both' || scope==='biketrek') && row.bike_state==='unlinked') suggestions.push('linkB');
  if (!suggestions.length) suggestions.push('verify');
  return suggestions;
}

export function AtomerReviewEditor({subject,run,onClose,onSaved,initialDecision}:{
  subject:Subject; run:number; onClose:()=>void; onSaved:()=>void; initialDecision?:Decision}) {
  const {t}=useTranslation(), m=(k:string)=>t('atomerProgress.'+k);
  const w=subject.work, target=subject.target, isFamily=subject.kind==='family';
  const [scope,setScope]=useState<Scope>(w.scope), [decision,setDecision]=useState<Decision>(initialDecision??w.decision);
  const [note,setNote]=useState(w.note), [instruction,setInstruction]=useState(w.instruction);
  const [result,setResult]=useState(''), [busy,setBusy]=useState(false), [error,setError]=useState('');
  const dialog=useRef<HTMLDialogElement>(null);
  useEffect(()=>{
    const el=dialog.current;
    if (el && typeof el.showModal==='function') el.showModal(); else el?.setAttribute('open','');
    return ()=>{ if (el?.open && typeof el.close==='function') el.close(); };
  },[]);
  async function submit(command:Command) {
    if (busy) return;
    setBusy(true); setError('');
    try {
      const fields = command==='save'||command==='done' ? {scope,decision,note,instruction} :
        command==='complete'||command==='block' ? {result} : {};
      await editWork({kind:subject.kind,key:subject.key,revision:w.revision,run_id:run,command,...fields});
      onSaved();
    } catch (e:any) {setError(e?.code || 'failed');} finally {setBusy(false);}
  }
  const tasks=subject.suggestions?.length?subject.suggestions:subject.kind==='extra'?['inspectExtra']:['verify'];
  const changed=decision!==w.decision||scope!==w.scope||note!==w.note||instruction!==w.instruction;
  const working=w.status==='working';
  return <dialog ref={dialog} className="review-dialog" aria-labelledby="atomer-work-title" onCancel={e=>{e.preventDefault();if(!busy)onClose();}}>
    <div className="review-dialog-head"><div><small>{m('subject.'+subject.kind)} · {subject.key}</small>
      <h2 id="atomer-work-title">{subject.name}</h2></div><button type="button" onClick={onClose} disabled={busy} aria-label={m('close')}>×</button></div>
    <div className="review-status-line"><span className={'review-badge status-'+w.status}>{m('workStatus.'+w.status)}</span>
      {isFamily && <span>{m('wholeFamily')}</span>}</div>
    {target && <div className="review-evidence"><strong>{target.code}</strong> · ID {target.product_id} / {target.variant_id}
      <div>{m('checks.'+target.check)} · EAN {target.ean||'—'}</div>
      {target.warnings.map(k=><div className="progress-warning" key={k}>{m('warn.'+k)}</div>)}</div>}
    {w.result && <div className="review-receipt"><strong>{m('lastResult')}</strong><p>{['target_recheck_required','admin_source_required'].includes(w.result)?m('errors.'+w.result):w.result}</p></div>}
    <div className="review-fields">
      {target ? <label>{m('delete')}<select value={decision} disabled={working||busy} onChange={e=>setDecision(e.target.value as Decision)}>
        {(['undecided','no','yes'] as const).map(k=><option key={k} value={k} disabled={k==='yes'&&target.check!=='eligible'}>{m('decision.'+k)}</option>)}</select>
        <small>{m('deletionHelp')}</small></label> :
        <label>{m('scopeLabel')}<select value={scope} disabled={working||busy} onChange={e=>setScope(e.target.value as Scope)}>
          {(['default','both','xtrek','biketrek'] as const).map(k=><option key={k} value={k}>{m('scopes.'+k)}</option>)}</select></label>}
      <label>{m('note')}<textarea rows={2} maxLength={4000} value={note} onChange={e=>setNote(e.target.value)} disabled={busy} /></label>
      <fieldset disabled={working||busy}><legend>{m('suggested')}</legend><div className="review-suggestions">
        {tasks.map(k=><button key={k} type="button" onClick={()=>setInstruction(v=>[v.trim(),m('instructions.'+k)].filter(Boolean).join('\n').slice(0,4000))}>{m('suggestions.'+k)}</button>)}</div></fieldset>
      <label>{m('instruction')}<textarea rows={3} maxLength={4000} value={instruction} onChange={e=>setInstruction(e.target.value)} disabled={working||busy} />
        <small>{m('instructionHelp')}</small></label>
      {working && <label>{m('receipt')}<textarea rows={2} maxLength={4000} value={result} onChange={e=>setResult(e.target.value)} disabled={busy}/></label>}
    </div>
    {error && <p role="alert" className="progress-warning">{t('atomerProgress.errors.'+error,{defaultValue:m('errors.failed')})}</p>}
    <div className="review-actions"><button type="button" disabled={busy||!changed} onClick={()=>void submit('save')} className="review-primary">{m('save')}</button>
      {w.status!=='done' && !working && decision!=='yes' && <button disabled={busy} onClick={()=>void submit('done')}>{m('finish')}</button>}
      {w.status!=='open' && <button disabled={busy||changed} onClick={()=>void submit('reopen')}>{m(w.status==='done'?'reopen':'withdraw')}</button>}
      {w.status==='queued' && <button disabled={busy||changed} onClick={()=>void submit('claim')}>{m('claim')}</button>}
      {(w.status==='blocked'||w.status==='open') && (!!instruction||decision==='yes') && <button disabled={busy||changed} onClick={()=>void submit('retry')}>{m('retry')}</button>}
      {working && <><button disabled={busy||changed||!result.trim()} onClick={()=>void submit('complete')}>{m('verifiedDone')}</button>
        <button disabled={busy||changed||!result.trim()} onClick={()=>void submit('block')}>{m('blocked')}</button></>}
    </div>
  </dialog>;
}
