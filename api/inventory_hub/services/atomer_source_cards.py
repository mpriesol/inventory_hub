"""Additive card-only preparation from a claimed leaf of the pinned full Atomer export."""
from collections import defaultdict

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from inventory_hub.db_models import Product, ProductIdentifier, Shop
from inventory_hub.services import atomer_review as review
from inventory_hub.services.identifiers import ProductIdentifierService
from inventory_hub.services.product_identity import IDENTITY_WRITE_LOCK, RemoteIdentity, barcode_keys


def physical_subject(row,archived):
    from inventory_hub.services.atomer_card_batch import require
    code=row['code']
    require(isinstance(archived,dict) and archived.get('code')==code and
            set(archived.get('sku_owners') or ())=={code} and
            archived.get('parent')==(row.get('parent') if row['kind']=='variant' else None) and
            (archived.get('ean') or '')==(row.get('ean') or '') and archived.get('native_id') and
            isinstance(archived.get('product_active'),bool),'atomer_source_card_identity_unverified')
    state=row.get('source_state') or {}
    require(state.get('product_origin')=='admin_csv' and state.get('product') is archived['product_active'] and
            (row['kind']=='standalone' and archived.get('variant_active') is None or
             row['kind']=='variant' and state.get('variant_origin')=='admin_csv' and
             isinstance(archived.get('variant_active'),bool) and state.get('variant') is archived['variant_active']),
            'atomer_source_card_visibility_unverified')
    permitted={code,archived.get('parent')}  # The technical parent is never a physical sellable.
    require(len(code)<=100 and code==code.strip() and code and all(ord(c)>=32 and ord(c)!=127 for c in code)
            and not (set(archived.get('gtin_owners') or ())|set(archived.get('corrected_gtin_owners') or ()))-permitted,
            'atomer_source_card_identity_not_unique')
    require(row['state'] in ('needs_both','needs_target') and not row['reasons'] and not row.get('candidates')
            and row.get('bike_state')!='review' and row.get('product_id') is None,'atomer_source_card_requires_review')


async def _cached_native(db,shop_ids,codes,gtins):
    # Parent-level stock listings with variants are never included as individual articles.
    result=await db.execute(text("""
        SELECT s.shop_id,s.data->>'code',s.data->>'product_id',leaf->>'code',leaf->>'variant_id',leaf->'ean'
        FROM shop_product_content s
        CROSS JOIN LATERAL jsonb_array_elements(CASE
            WHEN jsonb_typeof(s.data->'variants')='array' AND s.data->'variants'!='[]'::jsonb
                THEN s.data->'variants' ELSE jsonb_build_array(s.data) END) AS parts(leaf)
        WHERE s.shop_id=ANY(CAST(:shops AS bigint[])) AND (
            lower(leaf->>'code')=ANY(CAST(:codes AS text[])) OR EXISTS (
               SELECT 1 FROM regexp_split_to_table(coalesce(leaf->>'ean',''),'[^0-9]+') AS tokens(val)
               WHERE val=ANY(CAST(:gtins AS text[]))))
    """),dict(shops=shop_ids,codes=list(codes),gtins=list(gtins)))
    by_code,by_gtin=defaultdict(list),defaultdict(list)
    for shop,parent,pid,code,vid,ean in result:
        values=tuple(str(x) for x in (ean if isinstance(ean,list) else [ean]) if x)
        record=RemoteIdentity(shop,code or '',vid is not None,vid or pid,parent if vid is not None else None,
                              values,external_product_id=pid)
        by_code[record.code.casefold()].append(record)
        for gtin in barcode_keys(values):
            by_gtin[gtin].append(record)
    return by_code,by_gtin


async def source_cards(db,run_id,details_by_code,*,actor,scopes=None,barcode_corrections=None,zero_barcode_sentinels=None):
    """Return card_ready or blocked for each exact subject. The worker saves the receipt; no remote calls."""
    from inventory_hub.services import atomer_card_batch as cards
    codes=list(details_by_code)
    if not actor or not 0<len(codes)<=cards.MAX_BATCH:
        raise cards.atomer_review_targets.ReviewError('invalid_card_batch',422)
    scopes=scopes or {}
    corrections=barcode_corrections or {}
    zero_ids=zero_barcode_sentinels or {}
    await review._lock_current_run(db,run_id)
    await db.execute(text('SELECT pg_advisory_xact_lock(:key)'),dict(key=IDENTITY_WRITE_LOCK))
    subjects=await cards._subjects(db,run_id,codes)
    shops={s.code:s for s in await db.scalars(select(Shop).where(Shop.code.in_(cards.SHOPS),Shop.is_active.is_(True),
                                                                Shop.platform=='upgates'))}
    if set(shops)!=set(cards.SHOPS):
        raise cards.atomer_review_targets.ReviewError('shops_unavailable')
    local,indexes,_=await cards._indices(db,shops)
    index_by_id={shops[s].id:v for s,v in indexes.items()}
    duplicates=await cards._read_source_collisions(db,run_id,set(codes),corrections)
    results={};ready={}
    for code,detail in details_by_code.items():
        try:
            cards.require(code in subjects and code not in duplicates,'atomer_duplicate_or_missing_subject')
            row,item,family=subjects[code]
            cards.require('working' in (item['status'],family['status']),'work_not_claimed')
            cards.require(cards.effective_scope(item,family,scopes.get(code))!='biketrek','biketrek_only_requires_native')
            a=detail.get('source_card')
            physical_subject(row,a)
            if code in zero_ids:
                cards.require(a.get('parent') is None and a['native_id']==zero_ids[code],'atomer_zero_barcode_subject_changed')
            source=cards.barcode_subject(row,corrections.get(code),zero_ids.get(code))
            ready[code]=source,a
        except cards.CardConflict as err:
            results[code]=cards.fail(err)
    if not ready:
        return results
    all_eans={val for source,_ in ready.values() for val in ProductIdentifierService.gtin_equivalents(source.get('ean') or '')}
    by_code,by_gtin=await _cached_native(db,[s.id for s in shops.values()],{k.casefold() for k in ready},all_eans)
    other=defaultdict(set)
    for val,owner in (await db.execute(select(ProductIdentifier.value,ProductIdentifier.product_id).where(
                     ProductIdentifier.value.in_(set(ready)|all_eans)))).all():
        other[val].add(owner)
    for code,owner in await db.execute(text("""
        SELECT source_code_fold,product_id FROM product_sku_identities WHERE source_code_fold=ANY(CAST(:codes AS text[]))
    """),dict(codes=[s.casefold() for s in ready])):
        other[code].add(owner)
    claims={};claimed=defaultdict(set)
    for code,(source,a) in ready.items():
        try:
            ean=source.get('ean') or ''
            gtins=barcode_keys([ean])
            match=lambda index:cards._owner(index.resolve(RemoteIdentity(index.shop_id,code,barcodes=(ean,))))
            owners={o for index in [local,*indexes.values()] if (o:=match(index))}
            if source.get('hub_id'):
                cards.require(source['hub_id'] in local.products,'atomer_card_owner_changed')
                owners.add(source['hub_id'])
            nearby=set(by_code.get(code.casefold(),()))
            for gtin in gtins:
                nearby.update(by_gtin.get(gtin,()))
            physical=defaultdict(set)
            for spec in nearby:
                cards.require(spec.code and spec.external_product_id and spec.external_id,'cached_native_identity_incomplete')
                physical[spec.shop_id].add((spec.external_product_id,spec.external_id))
                cards.agree_eans(ean,spec)
                observed=cards._owner(index_by_id[spec.shop_id].resolve(spec))
                if observed:
                    owners.add(observed)
            cards.require(all(len(v)==1 for v in physical.values()) and len(owners)<=1,'source_card_multiple_owners')
            owner=next(iter(owners),None)
            identifiers={code,code.casefold(),*ProductIdentifierService.gtin_equivalents(ean)}
            cards.require(all(not other[v]-owners for v in identifiers),'source_card_other_registered_identifier')
            if owner:
                p=local.products[owner]
                cards.require(p.is_active and p.item_type=='stock' and
                              (p.sku==code or p.created_from_source!='atomer'),'source_card_owner_unavailable')
                cards.require(not gtins or not gtins.isdisjoint(barcode_keys(local.product_barcodes.get(owner,()))),
                              'existing_card_gtin_requires_verification')
                current_links=[m for index in indexes.values() for m in index.by_product.get(owner,())]
                cards.require(all(m.external_code and m.external_product_id and (not m.is_variant or m.external_variant_id)
                                  and not m.push_pending for m in current_links),'source_card_pending_or_incomplete_link')
                claimed[('card',owner)].add(code)
            fixed=(source.get('barcode_evidence') or {}).get('required_card_id')
            cards.require(fixed is None or owner==fixed,'confirmed_corrected_card_gtin_required')
            for k in gtins:
                claimed[('ean',k)].add(code)
            claims[code]=owner
        except cards.CardConflict as err:
            results[code]=cards.fail(err)
    for hits in claimed.values():
        if len(hits)>1:
            for code in hits:
                claims.pop(code,None)
                results[code]=cards.fail('one_physical_leaf_claimed_twice')
    created=set()
    for code,owner in claims.items():
        if owner:
            continue
        source,a=ready[code]
        try:
            async with db.begin_nested():
                name=a.get('name') or a.get('parent_name') or code
                brand=a.get('brand') or None
                weight=a.get('weight_g')
                cards.require(len(name)<=500 and (not brand or len(brand)<=100) and
                              (weight is None or type(weight) is int and 0<=weight<2**31),'source_card_metadata_invalid')
                product=Product(sku=code,name=name,brand=brand,weight_g=weight,group_id=None,created_from_source='atomer')
                db.add(product)
                await db.flush()
                if source.get('ean'):
                    await ProductIdentifierService(db).add_identifier(product.id,source['ean'],is_primary=True)
            claims[code]=product.id
            created.add(code)
        except (IntegrityError,cards.CardConflict) as err:
            results[code]=cards.fail(str(err) if isinstance(err,cards.CardConflict) else 'source_card_concurrent_identity')
    actual={p.id:p for p in await db.scalars(select(Product).where(Product.id.in_([v for v in claims.values() if v]))
                                                  .execution_options(populate_existing=True))}
    barcodes=defaultdict(list)
    for pid,val in await db.execute(select(ProductIdentifier.product_id,ProductIdentifier.value).where(
            ProductIdentifier.product_id.in_(list(actual)),ProductIdentifier.identifier_type.in_(('ean','upc')))):
        barcodes[pid].append(val)
    for code,pid in claims.items():
        if code in results:
            continue
        source,a=ready[code]
        p=actual.get(pid)
        values=sorted(barcodes[pid])
        if not p or code in created and p.sku!=code or source.get('ean') and not barcode_keys([source['ean']])&barcode_keys(values):
            results[code]=cards.fail('source_card_readback_failed')
            continue
        results[code]=dict(status='card_ready',action='created' if code in created else 'reused',created=code in created,
                           card_id=pid,hub_sku=p.sku,ean=source.get('ean') or None,gtins=values,
                           source_native_id=a['native_id'],source_ean=a.get('ean') or None)
        if source.get('barcode_evidence'):
            results[code]['barcode_evidence']=source['barcode_evidence']
    return results
