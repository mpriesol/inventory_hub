"""Correct observed platform-side effects on existing native IDs; never create another variant."""
import asyncio
from copy import deepcopy
from decimal import Decimal

from sqlalchemy import select, text
from inventory_hub.db_models import Shop
from inventory_hub.product_editor_models import AtomerVariantRepair
from inventory_hub.services import atomer_generations as gen, atomer_review as work
from inventory_hub.services import atomer_family_batch as batch, atomer_variant_repair as repair
from inventory_hub.services import stock_publication_source as transport
from inventory_hub.services.product_identity import IDENTITY_WRITE_LOCK


def price_input(rows):
    gen.require(len(rows)==1 and rows[0].get("language")=="sk" and rows[0].get("currency")=="EUR",
                "generation_price_configuration")
    r=rows[0]
    lists=r.get("pricelists") or []
    gen.require(len(lists)==1 and lists[0].get("price_sale") is None and not lists[0].get("product_discount"),
                "generation_price_configuration")
    return dict(language="sk",price_common=float(gen.publication.money(r.get("price_common") or 0)),
                pricelists=[dict(name=lists[0]["name"],price_original=float(gen.publication.money(lists[0]["price_original"])))])


def check_prices(before, current, additions):
    prior, remote=price_input(before),price_input(current)
    if before==current:
        return None
    allowed={gen.publication.money(e["gross"]) for e in additions.values()}
    common={gen.publication.money(e["common"]) for e in additions.values()}
    allowed.add(gen.publication.money(prior["pricelists"][0]["price_original"]))
    common.add(gen.publication.money(prior["price_common"]))
    gen.require(gen.publication.money(remote["pricelists"][0]["price_original"]) in allowed
                and gen.publication.money(remote["price_common"]) in common, "generation_unrelated_price_change")
    copy=deepcopy(current)
    copy[0]["price_common"]=before[0]["price_common"]
    row,original=copy[0]["pricelists"][0],before[0]["pricelists"][0]
    for key in ("price_original","price_with_vat","price_without_vat"):
        row[key]=original[key]
    gen.require(copy==before,"generation_unrelated_price_change")
    return [prior]


def availability_ids(fp, required):
    client=transport._client("xtrek",fp)
    found={}
    try:
        for kind in ("Custom","OnRequest","InStock","NotAvailable"):
            if not required-set(found):
                break
            try:
                response=transport._request(client,"get","availabilities",params={"type":kind,"page":1})
            except transport.SourceError as err:
                if err.status==429:
                    raise
                continue
            rows=response.get("availabilities") or []
            for row in rows:
                names=[v.get("name") for v in row.get("descriptions") or [] if v.get("language")=="sk" and v.get("name")]
                if len(names)==1 and names[0].casefold() in required:
                    k=names[0].casefold()
                    gen.require(k not in found or found[k]==row["id"],"generation_availability_ambiguous")
                    found[k]=row["id"]
        return found
    finally:
        client.session.close()


def recover_fields(doc, observation, registry):
    protected,axes=repair.projection(observation)
    original=doc["before_protected"]
    old_ids=set(original["leaves"])
    gen.require(all(protected["leaves"].get(vid)==original["leaves"][vid] for vid in old_ids),
                "generation_original_leaf_changed")
    parent,backup=protected["parent"].copy(),original["parent"].copy()
    now_prices,old_prices=parent.pop("prices"),backup.pop("prices")
    gen.require(parent==backup,"generation_original_parent_changed")
    expected=deepcopy(doc["additions"])
    prices=check_prices(old_prices,now_prices,expected)
    product={"code":doc["identity"]["parent_code"]}
    if prices:
        product["prices"]=prices
    native={v["code"]:v for v in observation["variants"]}
    ids={code:native[code]["variant_id"] for code in expected if code in native}
    gen.require(ids==doc.get("observed_ids") and len(ids)==len(expected) and len(native)==len(old_ids)+len(ids),
                "generation_recovery_identity_changed")
    assets={}
    for leaf in [observation["parent"],*observation["variants"]]:
        for img in leaf.get("images") or []:
            file=gen.image_name(img.get("url"))
            if img.get("file_id") and "-" in file:
                assets.setdefault(file.split("-",1)[-1],img["file_id"])
    leaves,issues=[],[]
    for code, exp in expected.items():
        leaf=native[code]
        gen.require(leaf["ean"]==exp["ean"] and leaf["active_yn"] is exp["visible"] and repair.axis_values(leaf)==exp["axes"]
                    and transport._quantity(leaf.get("stock"))=="0", "generation_recovery_identity_changed")
        prices_now=price_input(leaf["prices"])
        gen.require(gen.publication.money(prices_now["pricelists"][0]["price_original"])==Decimal(exp["gross"])
                    and gen.publication.money(prices_now["price_common"])==Decimal(exp["common"]), "generation_new_price_changed")
        node=gen.etree.fromstring(doc["sources"][code]["raw"].encode())
        grams=gen.shipping_grams(node.findtext("WEIGHT"))
        exp["weight"]=str(grams)
        patch={"code":code}
        if gen.publication.money(leaf.get("weight"))!=grams:
            gen.require(gen.publication.money(leaf.get("weight")) in (0,gen.publication.money(node.findtext("WEIGHT"))),
                        "generation_unrelated_weight_change")
            patch["weight"]=int(grams)
        if (leaf.get("availability") or "").casefold()!=exp["availability"].casefold():
            label=exp["availability"]
            if label.casefold() in registry:
                patch["availability_id"]=registry[label.casefold()]
            else:
                patch["availability"]=label
        images=leaf.get("images") or []
        urls=exp["images"]
        missing=[u for i,u in enumerate(urls) if not any(
            gen.same_image(v.get("url"),u)
            and (i or v.get("main_yn")) for v in images)]
        if missing:
            # A queued URL is already being fetched. The existing family file is also a verified
            # match for the source image. Be explicit about the primary image on older APIs.
            main=gen.image_name(urls[0])
            if urls[0] in missing and main in assets:
                patch["image"]={"file_id":assets[main]}
            issues.append(code+": "+str(len(missing))+" obrázkov ešte neoverených")
        if len(patch)>1:
            leaves.append(patch)
    if leaves:
        product["variants"]=leaves
    return product,expected,issues


async def owned(db, doc, *, verified=False):
    for item in doc["family"]["rows"]:
        saved=await work._record(db,"item",item["code"])
        group=await work._record(db,"family","v:"+doc["family"]["parent"])
        gen.require(saved["status"]==("open" if verified else "working")
                    and saved["revision"]==item["revision"]+(2 if verified else 1) and saved["scope"]!="biketrek"
                    and not saved["instruction"] and group["status"]=="open" and group["scope"]!="biketrek"
                    and group["revision"]==doc["family"]["family_revision"],"review_changed")


async def process(db, selection, mode, actor, adopt=False):
    gen.require(selection.phase=="extend","generation_selection_invalid")
    await work._lock_current_run(db,selection.run_id)
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"),{"key":IDENTITY_WRITE_LOCK})
    fp=repair.target_fingerprint("xtrek")
    jobs=[]
    for family in selection.families:
        job=await db.get(AtomerVariantRepair,gen.doc_id(selection,family),with_for_update=True)
        gen.require(job is not None and job.document["fingerprint"]==fp and job.state in ("uncertain","sending","verified"),
                    "generation_unverified")
        if job.state!="verified" or adopt:
            await owned(db,job.document,verified=job.state=="verified")
        jobs.append(job)
    obs=await asyncio.to_thread(batch.read_batch,[j.document["identity"] for j in jobs],fp)
    count=sum(len(f.rows) for f in selection.families)
    if adopt:
        from inventory_hub.routers import upgates_sync as pull
        shop=await db.scalar(select(Shop).where(Shop.code=="xtrek"))
        candidates=[]
        for j in jobs:
            doc=j.document
            observation=obs[doc["identity"]["product_id"]]
            if doc.get('unreported_stock'):
                ids, issues = gen.observe(doc, observation)
                gen.require(not issues and ids == doc.get('observed_ids'), 'generation_native_result_changed')
            [family]=pull._pull_families(shop.id,[observation["parent"]])
            native={v["code"]:v for v in observation["variants"]}
            for identity,leaf in family["leaves"]:
                if identity.code not in doc["additions"]:
                    continue
                expected=doc["additions"][identity.code]
                gen.require(str(doc.get("observed_ids",{}).get(identity.code))==identity.external_id
                    and native[identity.code].get("ean")==expected["ean"]
                    and repair.axis_values(native[identity.code])==expected["axes"],"generation_recovery_identity_changed")
                candidates.append(pull._leaf_selection(family,identity,leaf))
        gen.require(len(candidates)==count,"generation_native_members_differ")
        if mode=="preview":
            return dict(status="ready",count=count)
        selected = pull._parse_leaf_selection({'leaves': candidates})
        result=await pull.register_observed_products(db, shop, [o['parent'] for o in obs.values()], selected_leaves=selected)
        gen.require(not result.get("conflicts"),"generation_card_mapping_missing")
        return dict(status="verified",count=count,reason=f"Karty: {result.get('created_variants',0)}, väzby: {result.get('linked_products',0)}")
    fresh=[j for j in jobs if j.state!="verified" and not j.document.get("correction_sent")]
    if not fresh:
        results=[await gen.finish(db,j,obs[j.document["identity"]["product_id"]],actor) for j in jobs if j.state!="verified"]
        await db.commit()
        return next((r for r in results if r["status"]!="verified"),dict(status="verified",count=count))
    require_labels={exp["availability"].casefold() for j in fresh for code,exp in j.document["additions"].items()
                    if any(v["code"]==code and str(v.get("availability") or "").casefold()!=exp["availability"].casefold()
                           for v in obs[j.document["identity"]["product_id"]]["variants"])}
    registry=await asyncio.to_thread(availability_ids,fp,require_labels) if require_labels else {}
    payload,pending=[],[]
    for job in fresh:
        doc=job.document
        prod,exp,issues=recover_fields(doc,obs[doc["identity"]["product_id"]],registry)
        pending.extend(issues)
        if len(prod)>1:
            payload.append(prod)
        if mode=="apply":
            job.document={**doc,"additions":exp,"correction_sent":True,"correction_backup":obs[doc["identity"]["product_id"]],
                          "correction_values":prod}
    if mode=="preview":
        return dict(status="ready",count=count,reason="; ".join(pending))
    await db.commit()
    await work._lock_current_run(db,selection.run_id)
    for job in fresh:
        await db.refresh(job,with_for_update=True)
        await owned(db,job.document)
    try:
        upstream=await asyncio.to_thread(gen.send,payload,fp) if payload else ""
    except transport.SourceError as err:
        if err.status==429 and err.request_sent is False:
            for job in fresh:
                job.document={**job.document,"correction_sent":False}
            await db.commit()
            raise
        upstream=err.code
    after=await asyncio.to_thread(batch.read_batch,[j.document["identity"] for j in fresh],fp)
    results=[await gen.finish(db,j,after[j.document["identity"]["product_id"]],actor,False,upstream) for j in fresh]
    await db.commit()
    return next((r for r in results if r["status"]!="verified"),dict(status="verified",count=count))
