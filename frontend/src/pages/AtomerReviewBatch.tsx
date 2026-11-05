import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { hubRequest } from '../api/access';

type Family = { run_id:number; source_sha:string; rows:{code:string}[] };
type Plan = { families?:Family[]; blocks?:{run_id:number;items:Record<string,unknown>} };
type Result = { status:string; count:number; reason?:string };
const base = '/api/migration/atomer-progress';

export function AtomerReviewBatch({run,onSaved}:{run:number;onSaved:()=>void}) {
  const {t} = useTranslation();
  const m=(key:string)=>t('atomerProgress.batch.'+key);
  const [plan,setPlan]=useState<Plan|null>(null), [busy,setBusy]=useState(false);
  const [log,setLog]=useState<string[]>([]), [ready,setReady]=useState(false);
  async function load(file?:File) {
    setPlan(null);setReady(false);setLog([]);
    if (!file) return;
    try {
      if (file.size>1000000) throw Error();
      const p:Plan=JSON.parse(await file.text());
      if (!(p.families?.length||p.blocks) || (p.families||[]).length>200 ||
          p.families?.some(f=>f.run_id!==run||!Array.isArray(f.rows)||!f.rows.length) || (p.blocks && p.blocks.run_id!==run)) throw Error();
      setPlan(p);
    } catch {setLog([m('invalid')]);}
  }
  async function execute(preview:boolean) {
    if (!plan||busy) return;
    setBusy(true);setLog([]);setReady(false);
    try {
      for (const family of plan.families||[]) {
        const r=await hubRequest<Result>(base+'/variant-parameters/'+(preview?'preview':'apply'),family);
        setLog(v=>[...v,family.rows[0].code+' · '+m(r.status)+' ('+r.count+') '+(r.reason||'')]);
        if (!['ready','verified'].includes(r.status)) return;
      }
      if (plan.blocks) {
        if (!preview) await hubRequest<Result>(base+'/review-blocks',plan.blocks);
        setLog(v=>[...v,m(preview?'toBlock':'blocked')+' ('+Object.keys(plan.blocks!.items).length+')']);
      }
      setReady(preview);
    } catch (e) {
      setLog(v=>[...v,String((e as {code?:string}).code||m('error'))]);
    } finally {setBusy(false);if(!preview)onSaved();}
  }
  return <details><summary>{m('title')}</summary>
    <label>{m('plan')} <input type="file" accept=".json,application/json" disabled={busy}
      onChange={e=>void load(e.target.files?.[0])}/></label>
    <button type="button" disabled={!plan||busy} onClick={()=>void execute(true)}>{m('preview')}</button>
    <button type="button" disabled={!ready||busy} onClick={()=>void execute(false)}>{m('apply')}</button>
    {!!log.length&&<ul aria-live="polite">{log.map((s,i)=><li key={i}>{s}</li>)}</ul>}
  </details>;
}
