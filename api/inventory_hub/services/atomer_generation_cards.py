"""Finish a verified native generation only after its exact shop mapping exists."""
import asyncio

from sqlalchemy import func, select, text

from inventory_hub.db_models import Product, ProductIdentifier, Shop
from inventory_hub.db_models_ext import ProductVariantAttribute, ShopProduct
from inventory_hub.product_editor_models import AtomerVariantRepair, ProductEditorOverride
from inventory_hub.services import atomer_family_batch as batch, atomer_generations as gen, atomer_generation_availability as availability
from inventory_hub.services import atomer_review as work, atomer_variant_repair as repair
from inventory_hub.services.identifiers import ProductIdentifierService
from inventory_hub.services.product_identity import IDENTITY_WRITE_LOCK


def missing_axes(wanted, existing):
    known = {n:v for n,v in existing}
    gen.require(all(n in wanted and v==wanted[n] for n,v in known.items()), "generation_card_axes_differ")
    return [(n,v) for n,v in wanted.items() if n not in known]


async def finish_cards(db, selection, mode, actor, *, record_pending=False):
    gen.require(selection.phase=="extend","generation_selection_invalid")
    await work._lock_current_run(db,selection.run_id)
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"),{"key":IDENTITY_WRITE_LOCK})
    fp = repair.target_fingerprint("xtrek")
    identities, jobs = [], {}
    for family in selection.families:
        job = await db.get(AtomerVariantRepair,gen.doc_id(selection,family),with_for_update=True)
        gen.require(job and job.state in (("verified","uncertain","blocked") if record_pending else ("verified",)) and job.document["fingerprint"]==fp,"generation_unverified")
        identities.append(job.document["identity"])
        jobs[family.parent] = job
    observations = await asyncio.to_thread(batch.read_batch,identities,fp)
    finished = []
    for family in selection.families:
        doc = jobs[family.parent].document
        pending = availability.verify_except_availability(doc,observations[family.product_id]) if record_pending else {}
        if pending:
            ids = doc["observed_ids"]
        else:
            ids, issues = gen.observe(doc,observations[family.product_id])
            gen.require(ids==doc.get("observed_ids") and not issues,"generation_native_result_changed")
        for item in family.rows:
            record = await work._record(db,"item",item.code)
            group = await work._record(db,"family","v:"+family.parent)
            gen.require(not record["instruction"] and record["scope"]!="biketrek" and group["status"]=="open"
                        and group["revision"]==family.family_revision and group["scope"]!="biketrek","review_changed")
            vid = ids[item.code]
            owner = (await db.execute(select(ShopProduct,Product).join(Shop,Shop.id==ShopProduct.shop_id)
                .join(Product,Product.id==ShopProduct.product_id).where(Shop.code=="xtrek",
                ShopProduct.external_product_id==str(family.product_id),ShopProduct.external_variant_id==str(vid)))).one_or_none()
            gen.require(owner and owner[0].is_variant and owner[0].external_code==item.code and owner[1].sku==item.code
                        and owner[0].parent_code==family.parent and not owner[0].push_pending,"generation_card_mapping_missing")
            card = owner[1].id
            other = await db.scalar(select(ShopProduct.id).where(ShopProduct.product_id==card,
                ShopProduct.shop_id!=owner[0].shop_id).limit(1))
            gen.require(other is None,"generation_card_shared")
            owners = set(await db.scalars(select(ProductIdentifier.product_id).where(
                ProductIdentifier.identifier_type.in_(ProductIdentifierService.BARCODE_TYPES),
                func.ltrim(ProductIdentifier.value,"0")==item.ean.lstrip("0"))))
            gen.require(owners=={card},"generation_card_ean_changed")
            override = await db.get(ProductEditorOverride,card)
            gen.require(not override or not ({"eans","attributes"} & set((override.data or {}).get("variant") or {})),
                        "generation_card_override")
            existing = (await db.execute(select(ProductVariantAttribute.attribute_name,ProductVariantAttribute.attribute_value)
                        .where(ProductVariantAttribute.product_id==card))).all()
            extra = missing_axes(doc["expected_axes"][item.code],existing)
            note = (f"xTrek {family.product_id}/{vid} · karta Hub {card} · {item.code} · EAN {item.ean}. "
                    f"Generácia {item.generation}. Natívna rodina, parametre, ceny a väzba overené.")
            status="done"
            claimed=jobs[family.parent].state!="verified"
            if item.code in pending:
                actual,wanted=pending[item.code]
                status="blocked"
                note+=f" xTrek uvádza dostupnosť {actual}; Atomer vyžaduje {wanted}. Treba overiť natívne ID dostupnosti xTrek."
            if record["status"]==status and record["result"]==note and not extra:
                finished.append((item.code,card))
                continue
            gen.require(record["status"]==("working" if claimed else "open") and record["revision"]==item.revision+(1 if claimed else 2),"review_changed")
            if mode=="apply":
                for name,value in extra:
                    db.add(ProductVariantAttribute(product_id=card,attribute_name=name,attribute_value=value,
                           display_order=list(doc["expected_axes"][item.code]).index(name)))
                await repair.outcome(db,item.code,actor,status,note,"generation_card_"+status)
            finished.append((item.code,card))
        if record_pending and pending and mode=="apply":
            job=jobs[family.parent]
            job.state="blocked"
            job.document={**job.document,"availability_outstanding":pending}
    if mode=="apply":
        await db.commit()
    blocked=record_pending and any(j.state=="blocked" for j in jobs.values())
    return {"status":("blocked" if blocked else "verified") if mode=="apply" else "ready","count":len(finished),
            "reason":", ".join(f"{code} → {card}" for code,card in finished)}
