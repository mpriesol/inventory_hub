"""Verify a documented existing card for an additional xTrek model without moving its prior stock or BikeTrek link."""
import asyncio
from types import SimpleNamespace

from sqlalchemy import func, select, text

from inventory_hub.db_models import Product, ProductIdentifier, Shop
from inventory_hub.db_models_ext import ShopProduct
from inventory_hub.product_editor_models import ProductEditorOverride
from inventory_hub.services import atomer_conflict_resolution as conflicts, atomer_generations as gen
from inventory_hub.services import atomer_variant_repair as repair
from inventory_hub.services.identifiers import ProductIdentifierService
from inventory_hub.services.product_identity import IDENTITY_WRITE_LOCK, barcode_keys


async def check(db,item,*,target=None,read_bike=False):
    pin=item.preserved_card
    await db.execute(text('SELECT pg_advisory_xact_lock(:key)'),{'key':IDENTITY_WRITE_LOCK})
    product=await db.get(Product,pin.id,with_for_update=True)
    gen.require(product and product.is_active and product.item_type=='stock' and product.sku==item.code,
                'generation_existing_card_changed')
    owners=set(await db.scalars(select(ProductIdentifier.product_id).where(
        ProductIdentifier.identifier_type.in_(ProductIdentifierService.BARCODE_TYPES),func.ltrim(ProductIdentifier.value,'0')==item.ean.lstrip('0'))))
    values=list(await db.scalars(select(ProductIdentifier.value).where(ProductIdentifier.product_id==pin.id,
                            ProductIdentifier.identifier_type.in_(ProductIdentifierService.BARCODE_TYPES))))
    gen.require(owners=={pin.id} and values==[item.ean],'generation_existing_card_barcode_changed')
    override=await db.get(ProductEditorOverride,pin.id)
    gen.require(not override or not {'eans','attributes'}&set((override.data or {}).get('variant') or {}),
                'generation_card_override')
    links=(await db.execute(select(ShopProduct,Shop.code).join(Shop).where(ShopProduct.product_id==pin.id)
                             .with_for_update(of=ShopProduct))).all()
    bike=[s for s,shop in links if shop=='biketrek']
    xtrek=[s for s,shop in links if shop=='xtrek']
    gen.require(len(links)==len(bike)+len(xtrek) and len(bike)==1 and bike[0].id==pin.bike_link
                and bike[0].external_code==item.code and bike[0].external_product_id==pin.bike_product
                and bike[0].external_variant_id==pin.bike_variant and bike[0].parent_code==pin.bike_parent
                and bike[0].is_variant and not bike[0].push_pending, 'generation_existing_card_mapping_changed')
    gen.require(len(xtrek)<=1 and (not xtrek or target and (xtrek[0].external_product_id,xtrek[0].external_variant_id,
        xtrek[0].parent_code,xtrek[0].external_code,xtrek[0].is_variant,xtrek[0].push_pending)==(*target,item.code,True,False)),
        'generation_existing_card_mapping_changed')
    physical=(await conflicts.card_activity(db,[pin.id]))[pin.id]
    gen.require(not any(physical[k] for k in ('reservations','orders','native_orders','stock_work')) and
                all(v['qty_reserved']==v['qty_quarantined']==0 for v in physical['balances']) and
                sum(v['qty_on_hand'] for v in physical['balances'])==pin.on_hand,'generation_existing_card_activity_changed')
    if read_bike:
        v=SimpleNamespace(id=pin.id,sku=item.code,product_id=pin.bike_product,variant_id=pin.bike_variant)
        observed=(await asyncio.to_thread(conflicts.native_pair_links,[v],repair.target_fingerprint('biketrek')))[v.id]
        gen.require(observed['parent_code']==pin.bike_parent and
                    (not observed['ean'] or barcode_keys([observed['ean']])==barcode_keys([item.ean])),'generation_bike_native_changed')
    return xtrek[0] if xtrek else None


async def attach(db,selection,jobs,observations,shop,mode):
    """Bind only the chosen verified sibling native IDs. Leave shared card attributes and stock intact."""
    results={}
    for family in selection.families:
        job=jobs[family.parent]
        gen.require(job.state=='verified','generation_unverified')
        ids,issues=gen.observe(job.document,observations[family.product_id])
        gen.require(not issues and ids==job.document.get('observed_ids'),'generation_native_result_changed')
        for item in family.rows:
            target=(str(family.product_id),str(ids[item.code]),family.parent)
            link=await check(db,item,target=target,read_bike=True)
            owned=await db.scalar(select(ShopProduct.id).where(ShopProduct.shop_id==shop.id,ShopProduct.is_variant.is_(True),
                                      ShopProduct.external_variant_id==target[1]))
            gen.require(owned==(link.id if link else None),'generation_existing_card_mapping_changed')
            if not link and mode=='apply':
                link=ShopProduct(product_id=item.preserved_card.id,shop_id=shop.id,external_product_id=target[0],
                                 external_variant_id=target[1],parent_code=target[2],external_code=item.code,is_variant=True,push_pending=False)
                db.add(link)
                await db.flush()
            results[item.code]={'card_id':item.preserved_card.id,'bike_link':item.preserved_card.bike_link,
                                'xtrek_link':link.id if link else None,'product_id':family.product_id,'variant_id':ids[item.code]}
    if mode=='apply':
        for family in selection.families:
            recorded={v.code:results[v.code] for v in family.rows}
            job=jobs[family.parent]
            old=job.document.get('preserved_card_links')
            gen.require(old is None or old==recorded,'generation_existing_card_mapping_changed')
            job.document={**job.document,'preserved_card_links':recorded}
        await db.commit()
    return {'status':'verified' if mode=='apply' else 'ready','count':len(results),'links':results,
            'reason':', '.join(f"{code} → karta {r['card_id']}, xTrek {r['product_id']}/{r['variant_id']}" for code,r in results.items())}
