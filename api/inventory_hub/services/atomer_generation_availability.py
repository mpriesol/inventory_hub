"""Resolve a generation's remaining availability only against a native shop option."""
import asyncio
from copy import deepcopy

from sqlalchemy import text
from inventory_hub.product_editor_models import AtomerVariantRepair
from inventory_hub.services import atomer_generations as gen, atomer_family_batch as batch
from inventory_hub.services import atomer_generation_recovery as recovery, atomer_review as work, atomer_variant_repair as repairs
from inventory_hub.services import migration_upgates_export as export, stock_publication_source as transport
from inventory_hub.services.product_identity import IDENTITY_WRITE_LOCK


def registry(kind, native_id=None):
    client=transport._client("xtrek",repairs.target_fingerprint("xtrek"))
    path="availabilities" if native_id is None else "availabilities/"+str(native_id)
    options=[]
    try:
        for page in range(1,11):
            params={} if native_id else {"page":page}
            if kind!="default" and not native_id:
                params["type"]=kind
            data=export._read(client,path,params)
            rows=data.get("availabilities")
            if not isinstance(rows,list):
                return {"ok":False,"error":"invalid_availability_registry"}
            for row in rows:
                names=[d["name"] for d in row.get("descriptions") or []
                       if isinstance(d,dict) and d.get("language")=="sk" and isinstance(d.get("name"),str)]
                if len(names)==1 and type(row.get("id")) is int and row["id"]>0:
                    options.append({k:row.get(k) for k in ("id","type","delivery_days","dispatch_days","default_yn")}|
                                   {"name":names[0]})
            pages=data.get("number_of_pages")
            if type(pages) is not int or pages>10:
                return {"ok":False,"error":"invalid_availability_pagination"}
            if page>=pages:
                return {"ok":True,"options":options}
        return {"ok":False,"error":"incomplete_availability_registry"}
    except export.ExportError as exc:
        return {"ok":False,"error":str(exc)}
    finally:
        client.session.close()


def verify_except_availability(doc, native):
    by_code={r["code"]:r for r in native["variants"]}
    checked=deepcopy(doc)
    pending={}
    for code,expected in checked["additions"].items():
        leaf=by_code.get(code)
        gen.require(leaf and isinstance(leaf.get("availability"),str),"generation_availability_unknown")
        actual=leaf["availability"]
        if actual.casefold()!=expected["availability"].casefold():
            pending[code]=(actual,expected["availability"])
            expected["availability"]=actual
    ids,rest=gen.observe(checked,native)
    gen.require(ids==doc["observed_ids"] and not rest,"generation_other_fields_unverified")
    return pending


async def bind(db, selection, mode, actor, options):
    gen.require(selection.phase=="extend" and options and len(options)<=10,"generation_selection_invalid")
    await work._lock_current_run(db,selection.run_id)
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"),{"key":IDENTITY_WRITE_LOCK})
    fp=repairs.target_fingerprint("xtrek")
    jobs=[]
    for f in selection.families:
        j=await db.get(AtomerVariantRepair,gen.doc_id(selection,f),with_for_update=True)
        gen.require(j and j.document["fingerprint"]==fp and j.state in ("uncertain","verified"),"generation_unverified")
        if j.state=="uncertain":
            await recovery.owned(db,j.document)
            jobs.append(j)
    count=sum(len(f.rows) for f in selection.families)
    if not jobs:
        return dict(status="verified",count=count)
    observations=await asyncio.to_thread(batch.read_batch,[j.document["identity"] for j in jobs],fp)
    payload=[]
    needed=set()
    for j in jobs:
        doc=j.document
        pending=verify_except_availability(doc,observations[doc["identity"]["product_id"]])
        if not pending or doc.get("availability_binding_sent"):
            continue
        leaves=[]
        for code,(old,wanted) in pending.items():
            gen.require(wanted in options,"generation_availability_option_required")
            leaves.append({"code":code,"availability_id":options[wanted]})
            needed.add(wanted)
        payload.append({"code":doc["identity"]["parent_code"],"variants":leaves})
    for label in sorted(needed):
        result=await asyncio.to_thread(registry,"default",options[label])
        valid=[o for o in result.get("options",[]) if o["id"]==options[label] and o["name"].casefold()==label.casefold()]
        gen.require(result["ok"] and len(valid)==1,"generation_availability_option_unconfirmed")
    if mode=="preview":
        return dict(status="ready",count=count)
    if payload:
        for j in jobs:
            j.document={**j.document,"availability_binding_sent":True,"availability_bindings":options}
        await db.commit()
        await work._lock_current_run(db,selection.run_id)
        for j in jobs:
            await db.refresh(j,with_for_update=True)
            await recovery.owned(db,j.document)
        try:
            await asyncio.to_thread(gen.send,payload,fp)
        except transport.SourceError as exc:
            if exc.status==429 and exc.request_sent is False:
                for j in jobs:
                    j.document={**j.document,"availability_binding_sent":False}
                await db.commit()
                raise
        observations=await asyncio.to_thread(batch.read_batch,[j.document["identity"] for j in jobs],fp)
    results=[await gen.finish(db,j,observations[j.document["identity"]["product_id"]],actor) for j in jobs]
    await db.commit()
    return next((r for r in results if r["status"]!="verified"),dict(status="verified",count=count))


async def after_stock_policy_change(db, request, mode, actor):
    """A separately authorized, one-time attempt on a previously verified, linked generation."""
    selection = request.selection
    await work._lock_current_run(db,request.run_id)
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"),{"key":IDENTITY_WRITE_LOCK})
    fp = repairs.target_fingerprint("xtrek")
    run = (await db.execute(text("SELECT source_sha256,admin_export_sha256 FROM atomer_progress_runs WHERE id=:run"),
                            {"run":request.run_id})).one()
    original = await db.scalar(text("SELECT admin_export_sha256 FROM atomer_progress_runs WHERE id=:run"),
                               {"run":selection.run_id})
    gen.require(run[0]==selection.source_sha and original and original==run[1] and selection.phase=="extend",
                "variant_source_changed")
    jobs, owners, payload, labels = [], {}, [], {}
    expected = {r.code for f in selection.families for r in f.rows}
    gen.require(set(request.revisions)==expected,"review_changed")
    for family in selection.families:
        job=await db.get(AtomerVariantRepair,gen.doc_id(selection,family),with_for_update=True)
        gen.require(job and job.state in ("verified","blocked") and job.document["fingerprint"]==fp,
                    "generation_unverified")
        doc=job.document
        group=await work._record(db,"family","v:"+family.parent)
        gen.require(group["status"]=="open" and group["scope"]!="biketrek"
                    and group["revision"]==family.family_revision,"review_changed")
        for item in family.rows:
            state=await work._record(db,"item",item.code)
            expected_rev=request.revisions[item.code]
            vid=doc["observed_ids"][item.code]
            closed=state["status"]=="done" and state["revision"]==expected_rev+1 and doc.get("stock_policy_receipt")
            gen.require(closed or (state["status"]=="blocked" and state["revision"]==expected_rev
                        and not state["instruction"] and not state["note"] and state["scope"]!="biketrek"),"review_changed")
            card=await db.scalar(text("""
               SELECT p.id FROM products p JOIN shop_products s ON s.product_id=p.id JOIN shops sh ON sh.id=s.shop_id
               WHERE sh.code='xtrek' AND s.retired_at IS NULL AND s.is_variant AND NOT s.push_pending AND p.sku=:code AND s.external_code=:code
               AND s.external_product_id=:pid AND s.external_variant_id=:vid AND s.parent_code=:parent
               AND EXISTS(SELECT 1 FROM product_identifiers b WHERE b.product_id=p.id AND ltrim(b.value,'0')=ltrim(:ean,'0')
                    AND b.identifier_type IN ('ean','unverified_barcode','upc'))
               AND EXISTS(SELECT 1 FROM atomer_progress_items i WHERE i.run_id=:run AND i.code=:code
                    AND i.detail->>'ean'=:ean AND i.detail->>'hub_id'=p.id::text)
            """),dict(code=item.code,pid=str(family.product_id),vid=str(vid),parent=family.parent,ean=item.ean,run=request.run_id))
            gen.require(card,"generation_card_mapping_missing")
            owners[item.code]=card
        jobs.append(job)
    observations=await asyncio.to_thread(batch.read_batch,[j.document["identity"] for j in jobs],fp)
    for job in jobs:
        doc=job.document
        pending=verify_except_availability(doc,observations[doc['identity']['product_id']])
        if not pending or doc.get('stock_policy_attempt'):
            continue
        rows=[]
        for code,(_,wanted) in pending.items():
            option=(doc.get('availability_bindings') or {}).get(wanted)
            gen.require(type(option) is int and option>0 and code in expected,'generation_availability_option_required')
            labels[wanted]=option
            rows.append(dict(code=code,availability_id=option))
        payload.append(dict(code=doc['identity']['parent_code'],variants=rows))
    for label,opt in labels.items():
        res=await asyncio.to_thread(registry,'default',opt)
        gen.require(res['ok'] and any(x['id']==opt and x['name'].casefold()==label.casefold() for x in res['options']),
                    'generation_availability_option_unconfirmed')
    if mode=='preview':
        return dict(status='ready',count=len(owners))
    if payload:
        for job in jobs:
            job.document={**job.document,'stock_policy_attempt':True}
        await db.commit()
        # Reacquire the operator lock and compare revisions before dispatch. If the process
        # stops after recording its intent, a later invocation performs observation only.
        await work._lock_current_run(db,request.run_id)
        await db.execute(text("SELECT pg_advisory_xact_lock(:key)"),{"key":IDENTITY_WRITE_LOCK})
        for family in selection.families:
            group=await work._record(db,'family','v:'+family.parent)
            gen.require(group['status']=='open' and group['revision']==family.family_revision,'review_changed')
            for item in family.rows:
                rec=await work._record(db,'item',item.code)
                gen.require(rec['status']=='blocked' and rec['revision']==request.revisions[item.code],'review_changed')
        try:
            await asyncio.to_thread(gen.send,payload,fp)
        except transport.SourceError as exc:
            if exc.status==429 and exc.request_sent is False:
                for job in jobs:
                    job.document={**job.document,'stock_policy_attempt':False}
                await db.commit()
                raise
        observations=await asyncio.to_thread(batch.read_batch,[j.document['identity'] for j in jobs],fp)
    outstanding=[]
    for job in jobs:
        doc=job.document
        pending=verify_except_availability(doc,observations[doc['identity']['product_id']])
        for code,e in doc['additions'].items():
            if code in pending:
                outstanding.append(f'{code}: {pending[code][0]} → {pending[code][1]}')
                continue
            rec=await work._record(db,'item',code)
            if rec['status']=='done':
                continue
            note=(f"xTrek {doc['identity']['product_id']}/{doc['observed_ids'][code]}, karta Hub {owners[code]}, "
                  f"{code}, EAN {e['ean']}. Generácia a ostatné polia overené. Dostupnosť potvrdená cez API: {e['availability']}.")
            await repairs.outcome(db,code,actor,'done',note,'generation_availability_verified')
        job.state='blocked' if pending else 'verified'
        job.document={**doc,'availability_outstanding':pending,'stock_policy_receipt':True}
    await db.commit()
    return dict(status='blocked' if outstanding else 'verified',count=len(owners),reason=', '.join(outstanding))
