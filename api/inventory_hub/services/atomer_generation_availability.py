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
