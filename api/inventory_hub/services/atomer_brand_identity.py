"""Recognise a source label and a card manufacturer for one proven physical item."""
import asyncio
from typing import Literal

from pydantic import Field, HttpUrl
from sqlalchemy import select, text

from inventory_hub.db_models import Shop
from inventory_hub.db_models_ext import ShopProduct
from inventory_hub.product_editor_models import AtomerVariantRepair, ProductEditorOverride
from inventory_hub.services import atomer_conflict_resolution as conflicts
from inventory_hub.services import atomer_generations as gen, atomer_review as work, atomer_source_state as state
from inventory_hub.services import atomer_variant_repair as repair
from inventory_hub.services.product_identity import IDENTITY_WRITE_LOCK, RemoteIdentity, barcode_keys, load_identity_index, verified_barcodes


class Item(gen.Strict):
    code: repair.Token
    source_id: repair.Token
    revision: int = Field(gt=0)
    source_brand: repair.Token
    card_brand: repair.Token
    ean: repair.Token
    id: int = Field(gt=0)
    sku: repair.Token
    link: int = Field(gt=0)
    product_id: str = Field(pattern=r'^[1-9][0-9]{0,19}$')
    variant_id: Literal[None] = None
    manufacturer_or_supplier: HttpUrl
    evidence: str = Field(min_length=40, max_length=1500)


class Selection(gen.Strict):
    run_id: int = Field(gt=0)
    source_sha: str = Field(pattern=r'^[0-9a-f]{64}$')
    admin_sha: str = Field(pattern=r'^[0-9a-f]{64}$')
    items: list[Item] = Field(min_length=1, max_length=20)


def matches(proof, source, card, effective_brand, eans, links, native):
    """Apply a receipt only while both physical identifiers and its saved shop link still agree."""
    if (not proof or len(links) != 1 or not card.is_active or card.item_type != 'stock' or source.get('conflict')
            or source.get('parent') not in (None, '', source['code']) or proof['code'] != source['code']
            or proof['ean'] != source['ean'] or proof['source_brand'] != source['brand']
            or proof['card_brand'] != effective_brand or proof['id'] != card.id or proof['sku'] != card.sku
            or barcode_keys(eans) != barcode_keys([proof['ean']]) or not barcode_keys(eans)):
        return False
    link = links[0]
    target = native.get((proof['product_id'], None))
    return bool(link.id == proof['link'] and link.external_product_id == proof['product_id'] and not link.is_variant
                and link.external_variant_id is None and not link.parent_code and link.external_code == proof['sku']
                and target and target['code'] == proof['sku'] and (target.get('ean') or '') == proof['native_ean'])


async def register(db, selection, mode, actor):
    items = selection.items
    gen.require(all(len({getattr(item, k) for item in items}) == len(items) for k in ('code','id','sku','ean','product_id','link'))
                and all(r.source_brand.casefold() != r.card_brand.casefold() and r.ean in verified_barcodes([r.ean])
                        and r.manufacturer_or_supplier.scheme == 'https' and not r.manufacturer_or_supplier.username
                        and not r.manufacturer_or_supplier.password for r in items), 'brand_evidence_invalid')
    await work._lock_current_run(db, selection.run_id)
    await db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': IDENTITY_WRITE_LOCK})
    head = (await db.execute(text('SELECT source_sha256,admin_export_sha256 FROM atomer_progress_runs WHERE id=:r'),
                             {'r': selection.run_id})).one()
    gen.require(tuple(head) == (selection.source_sha, selection.admin_sha), 'variant_source_changed')
    admin = await state.admin_index(db, selection.admin_sha)
    current = await asyncio.to_thread(conflicts.native_pair_links, items, repair.target_fingerprint('biketrek'))
    identities = [RemoteIdentity(0, r.code, barcodes=(r.ean,)) for r in items]
    local = await load_identity_index(db, 0, identities, local=True)
    pending = []
    for r, identity in zip(items, identities):
        detail = await db.scalar(text('SELECT detail FROM atomer_progress_items WHERE run_id=:r AND code=:c'),
                                 {'r': selection.run_id, 'c': r.code})
        source = admin.get(r.code)
        gen.require(detail and detail['kind'] == 'standalone' and detail['reasons'] == ['card_brand_differs']
                    and not detail.get('product_id') and detail['hub_id'] == r.id and detail['ean'] == r.ean
                    and source and source['internal_id'] == r.source_id and source['has_catalog_code']
                    and source['parent'] == '' and source.get('visibility') is not None and not source.get('conflict')
                    and source['ean'] == r.ean and source['brand'] == r.source_brand, 'brand_identity_source_changed')
        duplicates = [c for c, entry in admin.items() if r.ean.lstrip('0') == (entry.get('ean') or '').lstrip('0')
                      or (r.ean in (entry.get('all_eans') or []))]
        gen.require(duplicates == [r.code], 'brand_identity_source_changed')
        card = local.products.get(r.id)
        gen.require(card and local.resolve(identity).product_id == r.id and card.brand == r.card_brand, 'brand_identity_card_changed')
        override = await db.get(ProductEditorOverride, r.id)
        data = (override.data if override else {}) or {}
        links = (await db.execute(select(ShopProduct, Shop.code).join(Shop).where(ShopProduct.product_id == r.id)
                                  .with_for_update(of=ShopProduct))).all()
        gen.require(len(links) == 1 and links[0][1] == 'biketrek' and not links[0][0].push_pending, 'brand_identity_card_changed')
        observed = current[r.id]
        native_ean = observed.get('ean') or ''
        gen.require(not native_ean or barcode_keys([native_ean]) == barcode_keys([r.ean]), 'brand_identity_native_changed')
        item = {**r.model_dump(mode='json'), 'native_ean': native_ean}
        gen.require(matches(item, {**source, 'code':r.code}, card, (data.get('common') or {}).get('brand') or card.brand,
                            (data.get('variant') or {}).get('eans', local.product_barcodes.get(r.id, [])), [links[0][0]],
                            {(r.product_id,None):observed}), 'brand_identity_card_changed')
        gen.require(not await db.scalar(text("SELECT 1 FROM atomer_review_checks WHERE run_id=:r AND detail->'card_ids' @> CAST(:cards AS jsonb) LIMIT 1"),
                                        {'r':selection.run_id,'cards':f'[{r.id}]'}), 'brand_identity_card_changed')
        receipt = {'operation':'source_card_brand', 'source_sha':selection.source_sha,'admin_sha':selection.admin_sha, 'item':item}
        key = repair.digest(receipt)
        previous = await db.get(AtomerVariantRepair, key, with_for_update=True)
        group = await work._record(db, 'family', detail['family_key'])
        saved = await work._record(db, 'item', r.code)
        command = await db.scalar(text("SELECT command FROM atomer_review_events WHERE kind='item' AND key=:c AND revision=:v"),
                                  {'c':r.code,'v':saved['revision']})
        gen.require(group['revision'] == 0 and group['status'] == 'open'
                    and all(not v['instruction'] and not v['note'] and v['decision']=='undecided'
                            and v['scope'] != 'biketrek' for v in (group,saved)), 'review_changed')
        if previous:
            gen.require(previous.state == 'verified' and command == 'brand_identity_verified' and saved['status']=='open'
                        and saved['revision']==r.revision+1, 'review_changed')
            continue
        gen.require(saved['status']=='blocked' and command=='review_block' and saved['revision']==r.revision, 'review_changed')
        pending.append((r,key,receipt))
    if mode == 'apply':
        for r,key,receipt in pending:
            db.add(AtomerVariantRepair(id=key, parent_code=r.code[:100], state='verified',
                                       document={**receipt,'run_id':selection.run_id,'actor':actor}))
            result = f"{r.code} / {r.ean}: Atomer {r.source_brand}, karta {r.id} {r.card_brand}, BIKETREK {r.product_id}."
            await repair.outcome(db,r.code,actor,'open',result,'brand_identity_verified')
        await db.commit()
    return {'status':'ready' if mode=='preview' else 'verified', 'count':len(items),
            'reason':', '.join(f'{r.code} / {r.ean} / {r.id}' for r in items)}
