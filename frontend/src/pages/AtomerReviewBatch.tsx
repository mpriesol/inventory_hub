import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { hubRequest } from '../api/access';

type Family = { operation?:'verified_barcode'; run_id:number; source_sha:string; rows:{code:string}[] };
type GenerationSelection = { run_id:number; phase:'label'|'extend'; families:{rows:{code:string}[]}[] };
type CatalogSelection = {run_id:number;source_sha:string;admin_sha:string;scope:"catalog"|"families";families?:string[]};
type Plan = { sourceCardBrands?:{run_id:number;items:{code:string}[]}; unreportedStock?:{selection:GenerationSelection;stock_id:number;observed_ids:Record<string,number>}; cardActivity?:{run_id:number;revisions:Record<string,number>}; barcodeOwners?:{run_id:number;pairs:{subject:string}[]}; reopenBarcodes?:{families:Family[]}; generationObservation?:GenerationSelection; catalog?:CatalogSelection; approvedProposals?: { run_id:number; action:"accept"|"verify" }; changedStockPolicy?:{run_id:number;selection:GenerationSelection;revisions:Record<string,number>}; generations?:GenerationSelection; finishGenerationCards?:boolean; generationRecovery?:"fields"|"adopt"; pendingGenerationCards?:boolean; availabilityOptions?:Record<string,number>; packs?:{families:Family[]}[]; families?:Family[]; blocks?:{run_id:number;items:Record<string,unknown>} };
type Result = { retry_after?:number; status:string; count:number; reason?:string };
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
      const families=[...(p.families||[]),...(p.packs||[]).flatMap(pack=>pack.families),...(p.reopenBarcodes?.families||[])];
      if (p.reopenBarcodes && (Object.keys(p).length!==1 || !p.reopenBarcodes.families.length || p.reopenBarcodes.families.length>25)) throw Error();
      if (p.sourceCardBrands && (Object.keys(p).length!==1 || p.sourceCardBrands.run_id!==run || !p.sourceCardBrands.items?.length || p.sourceCardBrands.items.length>20)) throw Error();
      if (p.barcodeOwners && (Object.keys(p).length!==1 || p.barcodeOwners.run_id!==run || !p.barcodeOwners.pairs?.length || p.barcodeOwners.pairs.length>12)) throw Error();
      if (p.cardActivity && (Object.keys(p).length!==1 || p.cardActivity.run_id!==run || !Object.keys(p.cardActivity.revisions).length)) throw Error();
      if (p.unreportedStock && (Object.keys(p).length!==1 || p.unreportedStock.selection.run_id!==run || p.unreportedStock.selection.phase!=='extend')) throw Error();
      const gen=p.generations;
      if (gen && (gen.run_id!==run || !['label','extend'].includes(gen.phase) ||
          !Array.isArray(gen.families) || !gen.families.length || gen.families.some(f=>!f.rows?.length))) throw Error();
      if (p.generationObservation && p.generationObservation.run_id!==run) throw Error();
      if ((p.catalog && (p.catalog.run_id!==run || !['catalog','families'].includes(p.catalog.scope))) ||
          (p.approvedProposals && (p.approvedProposals.run_id!==run || !['accept','verify'].includes(p.approvedProposals.action))) ||
          (p.changedStockPolicy && p.changedStockPolicy.run_id!==run)) throw Error();
      if (!(families.length||p.blocks||gen||p.approvedProposals||p.changedStockPolicy||p.catalog||p.generationObservation||p.barcodeOwners||p.cardActivity||p.unreportedStock||p.sourceCardBrands) || families.length>200 ||
          p.packs?.some(pack=>!pack.families.length||pack.families.length>25) ||
          families.some(f=>f.run_id!==run||!Array.isArray(f.rows)||!f.rows.length) || (p.blocks && p.blocks.run_id!==run)) throw Error();
      setPlan(p);
    } catch {setLog([m('invalid')]);}
  }
  async function execute(preview:boolean) {
    if (!plan||busy) return;
    setBusy(true);setLog([]);setReady(false);
    try {
      if (plan.generationObservation) {
        const observation=await hubRequest<unknown>(base+'/generation-observation',plan.generationObservation);
        setLog([JSON.stringify(observation,null,2)]);
        return;
      }
      if (plan.cardActivity) {
        const observation=await hubRequest<unknown>(base+'/card-activity',plan.cardActivity);
        setLog([JSON.stringify(observation,null,2)]);
        return;
      }
      const requests=(plan.families||[]).map(({operation,...selection})=>({
        path:operation==='verified_barcode'?'/verified-barcodes/':'/variant-parameters/',
        label:selection.rows[0].code,body:selection as unknown
      }));
      if (plan.reopenBarcodes) requests.push({path:'/review-batch/reopen/',label:plan.reopenBarcodes.families[0].rows[0].code,body:plan.reopenBarcodes});
      if (plan.sourceCardBrands) requests.push({path:'/source-card-brands/',label:plan.sourceCardBrands.items[0].code,body:plan.sourceCardBrands});
      if (plan.barcodeOwners) requests.push({path:'/barcode-owners/',label:plan.barcodeOwners.pairs[0].subject,body:plan.barcodeOwners});
      if (plan.unreportedStock) requests.push({path:'/generation-unreported-stock/',label:plan.unreportedStock.selection.families[0].rows[0].code,body:plan.unreportedStock});
      if (plan.catalog) requests.push({path:'/catalog-engine/',label:m('catalog'),body:plan.catalog});
      if (plan.approvedProposals) requests.push({path:'/approved-proposals/'+plan.approvedProposals.action+'/',label:m('title'),body:{run_id:run}});
      if (plan.changedStockPolicy) requests.push({path:'/generation-stock-policy/',label:plan.changedStockPolicy.selection.families[0].rows[0].code,body:plan.changedStockPolicy});
      for (const pack of plan.packs||[]) requests.push({
        path:'/review-batch/',label:pack.families[0].rows[0].code,
        body:{families:pack.families.map(({operation,...family})=>family)}
      });
      if (plan.generations && plan.availabilityOptions) requests.push({path:'/generation-availability/',label:plan.generations.families[0].rows[0].code,body:{selection:plan.generations,options:plan.availabilityOptions}});
      else if (plan.generations) requests.push({path:plan.pendingGenerationCards?'/generation-card-review/':plan.generationRecovery?'/generation-recovery/'+plan.generationRecovery+'/':plan.finishGenerationCards?'/generation-cards/':'/variant-generations/',label:plan.generations.families[0].rows[0].code,body:plan.generations});
      for (const {path,label,body} of requests) {
        const send=()=>hubRequest<Result>(base+path+(preview?'preview':'apply'),body);
        let r:Result;
        for (;;) {
          try {
            r=await send();
            if (!r.retry_after || r.status!=='uncertain') break;
            setLog(v=>[...v,label+' · '+m('rateWait')+' '+r.retry_after+' s']);
            await new Promise(resolve=>window.setTimeout(resolve,((r.retry_after||60)+1)*1000));
          } catch(e) {
            const err=e as {code?:string;detail?:{retry_after?:number}};
            if (err.code!=='stock_publication_rate_limited') throw e;
            const sec=Math.max(5,Math.min(86400,Number(err.detail?.retry_after)||60));
            setLog(v=>[...v,label+' · '+m('rateWait')+' '+sec+' s']);
            await new Promise(resolve=>window.setTimeout(resolve,(sec+1)*1000));
          }
        }
        const resultLabel=!preview&&r.status==='verified'&&(path==='/catalog-engine/'||path==='/approved-proposals/accept/')?'registered':r.status;
        setLog(v=>[...v,label+' · '+m(resultLabel)+' ('+r.count+') '+(r.reason||'')]);
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
