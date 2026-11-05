"""Material Atomer read-back of existing, exactly bound xTrek identities."""
import asyncio
from hashlib import sha256

from inventory_hub.services import atomer_catalog_engine as engine, atomer_catalog_recheck as recheck
from inventory_hub.services.atomer_review_targets import ReviewError


def targets(job,rows):
    guards=job['guards']
    keys=engine._selected(guards)
    cards=job['receipt'].get('cards',{})
    root=(job.get('document') or {}).get('payload',{}).get('code')
    pids=set()
    parents=set()
    codes={}
    ids={}
    selected=set()
    for k in keys:
        if engine._scope_of(guards,k)=='biketrek':
            continue
        evidence=cards.get(k,{}).get('evidence',{})
        leaf=evidence.get('xtrek') or {}
        if evidence.get('status')!='verified' or not evidence.get('card_id') or not leaf.get('product_id'):
            continue
        detail=rows[k]['detail']
        code=leaf.get('code')
        pid=int(leaf['product_id']); vid=leaf.get('variant_id')
        if not code or pid<1 or bool(vid)!=(detail['kind']=='variant'):
            raise ReviewError('catalog_bound_leaf_changed')
        # When the report has since been refreshed it is an independent native
        # identity check; a replaced link cannot silently acquire correction rights.
        if detail.get('product_id') and (str(detail['product_id'])!=str(pid) or
                str(detail.get('variant_id') or '')!=str(vid or '') or detail.get('target_code')!=code):
            raise ReviewError('catalog_bound_leaf_changed')
        parent=(detail.get('target_parent') or root) if vid else code
        if not parent:
            raise ReviewError('catalog_bound_parent_missing')
        selected.add(k);pids.add(pid);parents.add(parent)
        if vid:
            ids[k]=int(vid);codes[k]=code
    if not selected:
        return set(),None
    if len(pids)!=1 or len(parents)!=1 or len(set(ids.values()))!=len(ids) or len(set(codes.values()))!=len(codes):
        raise ReviewError('catalog_bound_family_ambiguous')
    return selected,dict(product_id=pids.pop(),product_code=parents.pop(),variant_ids=ids,variant_codes=codes,
                         allow_parent_write=selected==set(rows))


def safe_result(result):
    return {k:result[k] for k in ('status','identity_verified','verified_fields','product_id','variants','issues','deferred')
            if k in result}


async def conclude(db,c,j,result):
    receipt=j['receipt']
    summary=safe_result(result)
    may_close=summary.get('identity_verified') is True and summary.get('verified_fields') is True
    reason='; '.join(str(x) for x in (summary.get('issues') or []))[:3800] or 'Kontrola obsahu podľa Atomeru si vyžaduje doplnenie.'
    proofs={}
    for k in engine._selected(j['guards']):
        record=receipt.get('cards',{}).get(k,{})
        old=record.get('evidence') or {}
        if old.get('status')!='verified' or not old.get('card_id'):
            proofs[k]=(record.get('state','blocked'),old or dict(reason='Chýba overená identita skladovej karty.'))
        elif engine._scope_of(j['guards'],k)=='biketrek':
            proofs[k]=('verified',old)
        elif may_close and k in receipt.get('content_codes',[]):
            verified={key:val for key,val in old.items() if key not in ('reason','content_pending')}
            proofs[k]=('verified',{**verified,'verification_scope':'identity_card_and_mapped_content',
                                    'content_product_id':summary['product_id']})
        else:
            proofs[k]=('partial',{**old,'reason':reason,'verification_scope':'identity_and_card','content_pending':True})
    await engine._job(db,c['id'],j['family_key'],j['stage'],receipt={**receipt,'content_result':summary})
    await engine._finish(db,c,j,proofs)


async def prepare(db,c,jobs):
    from inventory_hub.services import atomer_existing_content as native
    index=options=roots=None
    entries={}
    for j in jobs:
        if j['receipt'].get('adopt_content'):
            # Previous-version material verification was missing. The still-open
            # campaign owns these previous automatic completions, not later edits.
            j['guards']=await recheck.claim_again(db,c,j,c['actor'],commands={'complete','block'})
            j['receipt']={**j['receipt'],'adopt_content':False}
            await engine._job(db,c['id'],j['family_key'],'fidelity',guards=j['guards'],receipt=j['receipt'])
        if not await engine._checked(db,c,[j]):
            continue
        rows,_=await engine._family(db,c['checked_run_id'],j['family_key'])
        try:
            chosen,anchors=targets(j,rows)
            if not chosen:
                await conclude(db,c,j,dict(issues=['Chýbajú natívne ID na kontrolu xTreku.']))
                continue
            if index is None:
                index=await engine._source(db,c)
                options=await engine._options(db,c,index)
                roots=await engine._root_index(db,c)
            guards={**j['guards'],'blocked':{**j['guards']['blocked'],**{k:'samostatná položka' for k in rows if k not in chosen}}}
            doc=await asyncio.to_thread(engine._make_document,index,j['family_key'],rows,guards,options,c['selection'],roots)
        except (ReviewError,ValueError,ArithmeticError) as error:
            if getattr(error,'code',None) in ('catalog_source_changed','catalog_connection_changed','new_category_mapping_changed','new_registry_binding_changed'):
                raise
            await conclude(db,c,j,dict(issues=[getattr(error,'code','new_source_value_invalid')]))
            continue
        code=doc['payload']['code']
        if code in entries:
            raise ReviewError('catalog_bound_family_ambiguous')
        j['document']=doc
        j['receipt']={**j['receipt'],'content_codes':sorted(chosen),'content_anchors':anchors}
        entries[code]=j
    if not entries:
        return
    docs=[j['document'] for j in entries.values()]
    expected={code:j['receipt']['content_anchors'] for code,j in entries.items()}
    plans=await asyncio.to_thread(native.prepare_content,docs,c['shop_fingerprint'],expected)
    outbound={}
    for code,j in entries.items():
        plan=plans[code]
        if plan['status']!='ready':
            await engine._job(db,c['id'],j['family_key'],'fidelity',document=j['document'])
            await conclude(db,c,j,plan)
            continue
        # Fingerprint the exact native preimage and requested sparse mutation.
        # A recheck cannot replay an indistinguishable earlier outbound intent.
        signature=sha256(engine._json([plan.get('before'),plan['payload']]).encode()).hexdigest()
        attempted=j['receipt'].get('content_attempts') or []
        if signature in attempted:
            await conclude(db,c,j,{**plan,'verified_fields':False,'issues':['Nezávislá kontrola nepotvrdila predošlý zápis rovnakých polí.']})
            continue
        intent={k:v for k,v in plan.items() if k!='native'}
        j['receipt']={**j['receipt'],'content_attempts':[*attempted,signature],'content_intent':intent}
        await engine._job(db,c['id'],j['family_key'],'updating',document=j['document'],receipt=j['receipt'])
        outbound[code]=j
    if not outbound:
        return
    checked_run=c['checked_run_id']
    await db.commit()
    await engine._lock_current(db,c)
    checked=await engine._checked(db,c,list(outbound.values()))
    if c['checked_run_id']!=checked_run:
        for j in checked:
            await before_dispatch(db,c,j)
        return
    if checked:
        try:
            scoped={j['document']['payload']['code']:j['receipt']['content_intent'] for j in checked}
            # Any result, failure or disconnect is followed by independent GETs.
            await asyncio.to_thread(native.send_content,scoped,c['shop_fingerprint'])
        except Exception as error:
            if engine._budget_error(error) and getattr(error,'request_sent',None) is False:
                for j in checked:
                    await before_dispatch(db,c,j)
                await db.commit()
                raise


async def before_dispatch(db,c,j):
    receipt=dict(j['receipt'])
    receipt.pop('content_intent',None)
    receipt['content_attempts']=receipt.get('content_attempts',[])[:-1]
    await engine._job(db,c['id'],j['family_key'],'fidelity',receipt=receipt)


async def observe(db,c,jobs):
    from inventory_hub.services import atomer_existing_content as native
    jobs=await engine._checked(db,c,jobs)
    if not jobs:
        return
    docs=[j['document'] for j in jobs]
    plans={j['document']['payload']['code']:j['receipt']['content_intent'] for j in jobs}
    seen=await asyncio.to_thread(native.observe_content,docs,c['shop_fingerprint'],plans)
    for j in jobs:
        code=j['document']['payload']['code']
        result=seen[code]
        if result.get('identity_verified') and (j['receipt'].get('rebuild_after_observation') or
                result.get('tranche_verified') is True and result.get('remaining_content') is True):
            receipt={**j['receipt'],'rebuild_after_observation':False,'content_result':safe_result(result)}
            receipt.pop('content_intent',None)
            await engine._job(db,c['id'],j['family_key'],'fidelity',receipt=receipt)
        elif result.get('identity_verified') and 'images' in result.get('deferred',[]) and j['attempts']<4:
            await engine._job(db,c['id'],j['family_key'],'updating',delay=120*(1+j['attempts']),increment=True,
                              problem='Čaká sa na načítanie galérie v xTreku.')
        else:
            await conclude(db,c,j,result)
