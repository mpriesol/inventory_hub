"""Additive card-only preparation from a claimed leaf of the pinned full Atomer export."""
import asyncio
from collections import defaultdict
from threading import Lock

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from inventory_hub.db_models import Product, ProductIdentifier, Shop
from inventory_hub.services import atomer_review as review
from inventory_hub.services.identifiers import ProductIdentifierService
from inventory_hub.services.product_identity import IDENTITY_WRITE_LOCK, RemoteIdentity, barcode_keys

_ARCHIVE_LOCK=Lock()
_ARCHIVE_INDEX={}


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
    eligible=(row['state'] in ('needs_both','needs_target') and row.get('product_id') is None or
              row['state']=='needs_card' and row.get('product_id') and not row.get('hub_id') and row.get('target_code')==code)
    require(eligible and not row['reasons'] and not row.get('candidates') and row.get('bike_state')!='review',
            'atomer_source_card_requires_review')


async def _cached_native(db,shop_ids,codes,gtins):
    # Parent-level stock listings with variants are never included as individual articles.
    result=await db.execute(text("""
        SELECT s.shop_id,s.data->>'code',s.data->>'product_id',leaf->>'code',leaf->>'variant_id',leaf->'ean',
            s.data->'variants_exists_yn',s.data->'set_yn',leaf->>'product_id',leaf->>'product_code',s.pulled_at
        FROM shop_product_content s
        CROSS JOIN LATERAL jsonb_array_elements(CASE
            WHEN jsonb_typeof(s.data->'variants')='array' AND s.data->'variants'!='[]'::jsonb
                THEN s.data->'variants' ELSE jsonb_build_array(s.data) END) AS parts(leaf)
        WHERE s.shop_id=ANY(CAST(:shops AS bigint[])) AND (
            lower(leaf->>'code')=ANY(CAST(:codes AS text[])) OR EXISTS (
               SELECT 1 FROM regexp_split_to_table(coalesce(leaf->>'ean',''),'[^0-9]+') AS tokens(val)
               WHERE val=ANY(CAST(:gtins AS text[]))))
    """),dict(shops=shop_ids,codes=list(codes),gtins=list(gtins)))
    by_code,by_gtin=defaultdict(list),defaultdict(list); proven={}
    for shop,parent,pid,code,vid,ean,has_variants,is_set,leaf_pid,leaf_parent,checked in result:
        values=tuple(str(x) for x in (ean if isinstance(ean,list) else [ean]) if x)
        record=RemoteIdentity(shop,code or '',vid is not None,vid or pid,parent if vid is not None else None,
                              values,external_product_id=pid)
        if checked and is_set is not True and (vid is None and has_variants is False and code==parent
                 or vid and has_variants is True and leaf_pid==pid and leaf_parent==parent):
            proven[record]=('shop_product_content',checked.isoformat())
        by_code[record.code.casefold()].append(record)
        for gtin in barcode_keys(values):
            by_gtin[gtin].append(record)
    return by_code,by_gtin,proven


def _archived_candidates(shop,shop_id,codes,gtins):
    from inventory_hub.services import atomer_card_batch as cards
    fp=cards.target_fingerprint(shop)
    directory=cards.pull._cache_path(shop).parent/'atomer-card-catalog-v1'
    with _ARCHIVE_LOCK:
        saved=_ARCHIVE_INDEX.get(shop)
        previous=saved[2] if saved and saved[:2]==(shop_id,fp) else None
        parents,checked=cards.checkpoint.archived_parents(directory,fp,previous=previous,identities_only=True)
        if not checked:
            return {},set()
        if parents is not None:
            by_code,by_gtin=defaultdict(set),defaultdict(set)
            for pid,parent in parents.items():
                variants=parent.get('variants') or []
                for v in variants or [parent]:
                    vid=str(v['variant_id']) if v.get('variant_id') is not None else None
                    raw=v.get('ean'); values=raw if isinstance(raw,list) else [raw] if raw else []
                    spec=RemoteIdentity(shop_id,v.get('code') or '',bool(variants),vid or pid,
                               parent.get('code') if variants else None,tuple(map(str,values)),external_product_id=pid)
                    safe=(bool(spec.code) and parent.get('set_yn') is not True and
                          parent.get('variants_exists_yn') is bool(variants) and (not variants or
                          vid and str(v.get('product_id'))==pid and v.get('product_code')==parent.get('code')))
                    entry=(spec,bool(safe))
                    by_code[spec.code.casefold()].add(entry)
                    for key in barcode_keys(spec.barcodes):
                        by_gtin[key].add(entry)
            _ARCHIVE_INDEX[shop]=(shop_id,fp,checked,by_code,by_gtin)
        _,_,_,by_code,by_gtin=_ARCHIVE_INDEX[shop]
        hits={entry for lookup,keyset in ((by_code,codes),(by_gtin,gtins)) for key in keyset for entry in lookup.get(key,())}
        return {r:('certified_shop_checkpoint',checked) for r,ok in hits if ok},{r for r,ok in hits if not ok}


def cached_target(source,shop_id,nearby,proofs):
    from inventory_hub.services import atomer_card_batch as cards
    pid,vid=cards.native_key((source['product_id'],source.get('variant_id')))
    variant=source['kind']=='variant'
    candidates={r for r in nearby if r.shop_id==shop_id}
    cards.require(len(candidates)==1,'source_card_cached_target_missing_or_ambiguous')
    r=candidates.pop(); gtin=barcode_keys([source.get('ean')]); other=source.get('target_ean')
    cards.require(r in proofs and r.code==source['code']==source.get('target_code') and
                  r.is_variant==variant==(vid is not None) and r.external_product_id==pid and r.external_id==(vid or pid) and
                  (not variant or r.parent_code==source.get('parent')==source.get('target_parent')) and
                  len(gtin)==1 and gtin==barcode_keys([other])==barcode_keys(r.barcodes),
                  'source_card_cached_target_not_exact')
    origin,checked=proofs[r]
    return dict(product_id=pid,variant_id=vid,sku=r.code,gtins=sorted(gtin),source=origin,observed_at=checked)


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
            if row['state']=='needs_card':
                cards.native_key((row['product_id'],row.get('variant_id')))
            if code in zero_ids:
                cards.require(a.get('parent') is None and a['native_id']==zero_ids[code],'atomer_zero_barcode_subject_changed')
            source=cards.barcode_subject(row,corrections.get(code),zero_ids.get(code),a['native_id'])
            ready[code]=source,a
        except cards.CardConflict as err:
            results[code]=cards.fail(err)
    if not ready:
        return results
    all_eans={val for source,_ in ready.values() for val in ProductIdentifierService.gtin_equivalents(source.get('ean') or '')}
    by_code,by_gtin,cached_proofs=await _cached_native(db,[s.id for s in shops.values()],{k.casefold() for k in ready},all_eans)
    invalid_native=set(); folded={k.casefold() for k in ready}; normalized=barcode_keys(all_eans)
    for s in cards.SHOPS:
        candidates,invalid=await asyncio.to_thread(_archived_candidates,s,shops[s].id,folded,normalized)
        invalid_native.update(invalid);cached_proofs.update(candidates)
        for rec in set(candidates)|invalid:
            by_code[rec.code.casefold()].append(rec)
            for value in barcode_keys(rec.barcodes):
                by_gtin[value].append(rec)
    other=defaultdict(set)
    for val,owner in (await db.execute(select(ProductIdentifier.value,ProductIdentifier.product_id).where(
                     ProductIdentifier.value.in_(set(ready)|all_eans)))).all():
        other[val].add(owner)
    for code,owner in await db.execute(text("""
        SELECT source_code_fold,product_id FROM product_sku_identities WHERE source_code_fold=ANY(CAST(:codes AS text[]))
    """),dict(codes=[s.casefold() for s in ready])):
        other[code].add(owner)
    # Only this migration's own as-yet blank cards can receive a subsequently
    # proven manufacturer's GTIN. A historic SKU match alone is insufficient.
    prior={}
    if corrections:
        for code,owner,iid,raw in await db.execute(text("""
            SELECT a.code,a.card_id,original->>'source_native_id',original->>'source_ean'
            FROM atomer_catalog_source_cards a JOIN products p ON p.id=a.card_id
            JOIN atomer_progress_runs original_run ON original_run.id=a.run_id
            JOIN atomer_progress_runs current_run ON current_run.id=:run AND (original_run.id=current_run.id
              OR original_run.source_sha256=current_run.source_sha256
                 AND original_run.admin_export_sha256=current_run.admin_export_sha256)
            CROSS JOIN LATERAL (VALUES (coalesce(a.evidence->'creation_evidence',a.evidence))) AS proof(original)
            WHERE a.code=ANY(CAST(:codes AS text[])) AND a.created
              AND original->>'status'='card_ready' AND original->>'card_id'=CAST(p.id AS text)
              AND original->>'hub_sku'=p.sku AND original->'gtins'='[]'::jsonb
              AND p.sku=a.code AND p.created_from_source='atomer'
              AND NOT EXISTS (SELECT 1 FROM product_identifiers i WHERE i.product_id=p.id
                  AND i.identifier_type IN ('ean','upc','unverified_barcode'))
              AND NOT EXISTS (SELECT 1 FROM product_editor_overrides o WHERE o.product_id=p.id AND (o.data->'variant') ? 'eans')
        """),dict(run=run_id,codes=list(corrections))):
            prior[(code,owner)]=(iid,raw or '')
    for value,owner in await db.execute(text("""
        SELECT val,o.product_id FROM product_editor_overrides o CROSS JOIN LATERAL
          jsonb_array_elements_text(CASE WHEN jsonb_typeof(o.data->'variant'->'eans')='array'
                                         THEN o.data->'variant'->'eans' ELSE '[]'::jsonb END) vals(val)
        WHERE val=ANY(CAST(:gtins AS text[]))
    """),dict(gtins=list(all_eans))):
        other[value].add(owner)
    claims={};claimed=defaultdict(set);enrich=set();target_proofs={}
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
            cards.require(not nearby&invalid_native,'source_card_cached_native_kind_unverified')
            if source['state']=='needs_card':
                target_proofs[code]=cached_target(source,shops['xtrek'].id,nearby,cached_proofs)
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
                existing=barcode_keys(local.product_barcodes.get(owner,()))
                can_add=(gtins and not existing and code in corrections and
                         not corrections[code].get('required_card_id') and
                         prior.get((code,owner))==(a['native_id'],a.get('ean') or ''))
                cards.require(not gtins or not existing.isdisjoint(gtins) or can_add,
                              'existing_card_gtin_requires_verification')
                if can_add:
                    enrich.add(code)
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
        source,a=ready[code]
        try:
            if owner:
                if code in enrich:
                    async with db.begin_nested():
                        await db.execute(select(Product.id).where(Product.id==owner).with_for_update())
                        existing=await db.scalar(select(ProductIdentifier.id).where(ProductIdentifier.product_id==owner,
                                ProductIdentifier.identifier_type.in_(('ean','upc','unverified_barcode'))).limit(1))
                        cards.require(existing is None,'source_card_concurrent_identity')
                        await ProductIdentifierService(db).add_identifier(owner,source['ean'],is_primary=True)
                continue
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
        if code in target_proofs:
            results[code]['cached_native_target']=target_proofs[code]
        if code in enrich:
            results[code]['gtin_added']=source['ean']
    return results
