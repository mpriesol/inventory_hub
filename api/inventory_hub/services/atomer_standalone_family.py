"""Convert a known standalone into its own Atomer family, preserving its one prior sellable."""
import asyncio
from decimal import Decimal

from pydantic import Field
from sqlalchemy import select, text

from inventory_hub.db_models import Shop
from inventory_hub.db_models_ext import ShopProduct
from inventory_hub.product_editor_models import AtomerVariantRepair, ProductEditorOverride
from inventory_hub.services import atomer_card_batch as cards, atomer_catalog_engine as engine, atomer_conflict_resolution as conflicts
from inventory_hub.services import atomer_generations as gen, atomer_new_native as native, atomer_new_source as source
from inventory_hub.services import atomer_review as work, atomer_variant_repair as repair, atomer_proposals as proposals
from inventory_hub.services.catalog_import import _assert_payload
from inventory_hub.services.product_identity import IDENTITY_WRITE_LOCK, RemoteIdentity, barcode_keys, load_identity_index, verified_barcodes
from inventory_hub.services.product_publication_source import plain


class Member(gen.Strict):
    code: repair.Token
    ean: repair.Token
    source_id: str = Field(pattern=r'^[1-9][0-9]{0,19}$')
    revision: int = Field(ge=0)


class Family(gen.Strict):
    run_id: int = Field(gt=0)
    source_sha: str = Field(pattern=r'^[0-9a-f]{64}$')
    admin_sha: str = Field(pattern=r'^[0-9a-f]{64}$')
    parent: repair.Token
    native_code: str = Field(pattern=r'^[1-9][0-9]{0,19}$')
    product_id: int = Field(gt=0)
    card_id: int = Field(gt=0)
    link_id: int = Field(gt=0)
    prior_leaf: repair.Token
    observed_stock: int = Field(ge=0,le=999999)
    members: list[Member] = Field(min_length=2,max_length=12)


def validated(s):
    members={r.code:r for r in s.members}
    gen.require(len(members)==len(s.members) and s.prior_leaf in members and s.native_code not in members and s.parent not in members
                and s.parent!=s.native_code and len({r.source_id for r in s.members}) == len(members)
                and all(r.ean in verified_barcodes([r.ean]) for r in s.members)
                and len({r.ean.lstrip('0') for r in s.members})==len(members), 'standalone_selection_changed')
    return members


def secondary(row):
    rows=row.get('stocks')
    gen.require(isinstance(rows,list) and len({s.get('stock_id') for s in rows})==len(rows)
                and all(type(s.get('stock_id')) is int and s['stock_id']>0 and 'quantity' in s
                        and s['quantity'] is None and not s.get('position') for s in rows), 'standalone_secondary_stock_changed')
    return sorted((s['stock_id'],s.get('code'),s.get('position'),s['quantity']) for s in rows)


def structure(s,doc,before):
    members=validated(s)
    root=doc['payload']
    leaves=root.get('variants') or []
    wanted={v['code']:v for v in leaves}
    gen.require(root['code']==s.parent and set(wanted)==set(members) and len(leaves)==len(members) and not doc.get('deferred_leaves')
                and all(v.get('ean')==members[k].ean for k,v in wanted.items()) and not root.get('ean')
                and before['product_id']==s.product_id and before['code']==s.native_code and before['ean']==members[s.prior_leaf].ean
                and not before.get('variants') and before.get('variants_exists_yn') is False
                and not before.get('archived_yn') and not before.get('set_yn') and not before.get('in_set_yn')
                and not before.get('stock_position') and before.get('manufacturer')==root.get('manufacturer')
                and type(before.get('stock')) is not bool and before.get('stock') is not None
                and native._numeric(before['stock'])==Decimal(s.observed_stock), 'standalone_native_identity_changed')
    secondary(before)
    patch={'code':s.native_code,'variants':[dict(v) for v in leaves]}
    # Only the source content passes through the regular stock-free catalog validator.
    # Then explicitly carry the observed original quantity onto the exact same physical GTIN.
    _assert_payload({**patch,'active_yn':False})
    next(v for v in patch['variants'] if v['code']==s.prior_leaf)['stock']=s.observed_stock
    return patch


def observed(s,old,live,flat,doc):
    members=validated(s)
    by_code={v['code']:v for v in live.get('variants') or []}
    flat_by_code={v['code']:v for v in flat}
    gen.require(live['product_id']==s.product_id and live['code']==s.native_code and live.get('variants_exists_yn') is True
                and set(by_code)==set(flat_by_code)==set(members) and len(by_code)==len(live['variants'])==len(flat),
                'standalone_family_unconfirmed')
    before=native._preserved(old,set())['parent']; after=native._preserved(live,set())['parent']
    # These two derived fields change when a first variant is introduced. Everything else, including URL and SEO, is fixed.
    before.pop('variants_exists_yn',None); after.pop('variants_exists_yn',None)
    gen.require(before==after and native._numeric(live.get('variants_stock'))==Decimal(s.observed_stock), 'standalone_parent_or_total_changed')
    warehouse=secondary(old)
    desired={v['code']:v for v in doc['payload']['variants']}
    ids,stock={},{}
    for code,member in members.items():
        leaf,independent=by_code[code],flat_by_code[code]
        expected=desired[code]
        gen.require(type(leaf.get('variant_id')) is int and leaf['variant_id']>0 and leaf['variant_id']==independent.get('variant_id')
                    and all(v.get('product_id')==s.product_id and v.get('product_code')==s.native_code and v.get('ean')==member.ean
                    and v.get('active_yn') is expected['active_yn'] for v in (leaf,independent)), 'standalone_leaf_identity_changed')
        gen.require('stock' in leaf and 'stock' in independent and leaf['stock']==independent['stock'] and not leaf.get('stock_position')
                    and not independent.get('stock_position') and secondary(leaf)==secondary(independent)==warehouse, 'standalone_warehouse_changed')
        quantity=leaf['stock']
        if code==s.prior_leaf:
            gen.require(quantity is not None and native._numeric(quantity)==Decimal(s.observed_stock), 'standalone_quantity_changed')
        else:
            gen.require(quantity is None or native._numeric(quantity)==0, 'standalone_quantity_on_another_leaf')
        gen.require(native._parameters(leaf.get('parameters_new'))==native._parameters(expected['parameters'])
                    and bool(leaf.get('main_yn'))==bool(expected.get('main_yn'))
                    and native._numeric(leaf.get('weight'))==native._numeric(expected.get('weight'))
                    and native._price_same(leaf,expected['prices'],doc['payload']['vats']['sk'])
                    and ('availability_id' not in expected or leaf.get('availability_id')==expected['availability_id'])
                    and native._labels_match(leaf,expected.get('labels',[]),doc['flags'][code],doc.get('managed_label_ids'))
                    and not native._image_issues(leaf,expected.get('images',[]),True), 'standalone_variant_fields_unconfirmed')
        ids[code]=leaf['variant_id'];stock[code]={'stock':quantity,'stocks':leaf['stocks']}
    gen.require(len(set(ids.values()))==len(members), 'standalone_family_unconfirmed')
    return ids,stock


def read_native(s,fp,variants=False):
    with native._connection(fp) as client:
        docs={s.native_code:{'payload':{'code':s.native_code,'variants':[]}}}
        parents=native._read(client,docs,{s.native_code:{'product_id':s.product_id}})
        gen.require(set(parents)=={s.native_code}, 'standalone_native_identity_changed')
        flat=native._sparse(client,[s.product_id],'products/variants','variants','product_ids') if variants else []
    return plain(parents[s.native_code]),flat


def free_other_codes(s,products):
    members=validated(s)
    codes={s.parent.casefold(),s.native_code.casefold(),*(c.casefold() for c in members)}
    gtins=barcode_keys(v.ean for v in members.values())
    for parent in products:
        for row in (parent,*(parent.get('variants') or [])):
            if parent.get('product_id')==s.product_id and row is parent and parent.get('code')==s.native_code:
                continue
            gen.require((row.get('code') or '').casefold() not in codes and not (barcode_keys([row.get('ean')]) & gtins),
                        'standalone_native_identity_occupied')


def can_adopt(record, revision, command, detail, group, job, events):
    """Accept exact untouched auto-proposals, or their observed owners in this undelivered campaign job."""
    if record['revision']!=revision:
        return False
    status=record['status']
    if not record['instruction']:
        return status=='open' and revision==0 or status=='blocked' and command=='review_block'
    scope=record['scope'] if record['scope']!='default' else group['scope']
    wanted='\n'.join(proposals.INSTRUCTIONS[k] for k in proposals.suggestions(detail,scope))
    if record['instruction']!=wanted:
        return False
    if status=='queued':
        return revision==1 and command=='approve_suggestions'
    if (not job or job['stage'] not in ('pending','prepared','blocked','partial') or job['receipt'].get('dispatched')
            or job['receipt'].get('content_intent') or job['receipt'].get('identity_verified') or job['receipt'].get('product_id')
            or not job['guards']):
        return False
    claimed=revision if status=='working' else revision-1
    return (status,command) in (('working','claim'),('blocked','block')) and claimed==2 and (
        ['item',detail['code'],claimed] in job['guards']['own'] and events.get(1)=='approve_suggestions'
        and events.get(2)=='claim')


async def local_checks(db,s,phase):
    members=validated(s)
    head=await engine._head(db)
    gen.require((head['source_sha256'],head['admin_export_sha256'])==(s.source_sha,s.admin_sha), 'variant_source_changed')
    await work._lock_current_run(db,head['id'])
    gen.require(phase!='before' or head['id']==s.run_id, 'run_changed')
    await db.execute(text('SELECT pg_advisory_xact_lock(:k)'),{'k':IDENTITY_WRITE_LOCK})
    rows,group=await engine._family(db,s.run_id,'v:'+s.parent)
    gen.require(set(rows)==set(members) and group['revision']==0 and group['status']=='open' and group['scope']!='biketrek'
                and not group['instruction'] and not group['note'], 'review_changed')
    job=await engine._one(db,"""SELECT j.stage,j.guards,j.receipt FROM atomer_catalog_jobs j JOIN atomer_catalog_campaigns c
        ON c.id=j.campaign WHERE c.source_sha=:s AND c.admin_sha=:a AND j.family_key=:k FOR UPDATE OF j""",
        s=s.source_sha,a=s.admin_sha,k='v:'+s.parent)
    gen.require(not job or job['stage'] in ('pending','prepared','blocked','partial','excluded')
                and not job['receipt'].get('dispatched') and not job['receipt'].get('content_intent')
                and not job['receipt'].get('identity_verified') and not job['receipt'].get('product_id'), 'standalone_campaign_has_native_intent')
    saved_work={}
    for code,r in members.items():
        record=await work._record(db,'item',code); detail=rows[code]['detail']
        saved_work[code]={k:record[k] for k in ('status','revision','scope','instruction','result')}
        gen.require(not record['note'] and record['scope']!='biketrek' and record['decision']=='undecided'
                    and detail['kind']=='variant' and detail['parent']==s.parent and detail['ean']==r.ean, 'review_changed')
        if phase=='before':
            events=dict((await db.execute(text("SELECT revision,command FROM atomer_review_events WHERE kind='item' AND key=:c AND revision<=:v"),
                                         {'c':code,'v':r.revision})).all())
            gen.require(can_adopt(record,r.revision,rows[code]['command'],detail,group,job,events),'review_changed')
        else:
            released=phase=='complete'
            gen.require(record['revision']==r.revision+(2 if released else 1) and record['status']==('open' if released else 'working')
                        and rows[code]['command']==('standalone_family_verified' if released else 'standalone_family_claim'), 'review_changed')
        if code==s.prior_leaf:
            gen.require(detail.get('hub_id')==s.card_id and detail['product_id']==str(s.product_id) and not detail.get('variant_id')
                        and detail['target_code']==s.native_code and detail['target_ean']==r.ean
                        and set(detail['reasons'])=={'kind_differs','different_shop_link'}, 'standalone_review_identity_changed')
        else:
            gen.require(detail['state'] in ('needs_target','needs_both') and not detail.get('product_id') and not detail.get('reasons'),
                        'standalone_review_identity_changed')
    seen=(await db.execute(text("""SELECT code FROM atomer_progress_items WHERE run_id=:r
          AND ltrim(detail->>'ean','0')=ANY(CAST(:g AS text[]))"""),
          {'r':s.run_id,'g':[r.ean.lstrip('0') for r in s.members]})).scalars().all()
    gen.require(len(seen)==len(members) and set(seen)==set(members), 'standalone_source_gtin_occupied')
    subjects=[RemoteIdentity(0,r.code,barcodes=(r.ean,)) for r in s.members]
    index=await load_identity_index(db,0,subjects,local=True)
    resolutions={i.code:index.resolve(i) for i in subjects}
    gen.require(all(v.status!='conflict' for v in resolutions.values()) and resolutions[s.prior_leaf].product_id==s.card_id,
                'standalone_card_identity_changed')
    card_ids=[v.product_id for v in resolutions.values() if v.product_id]
    gen.require(len(card_ids)==len(set(card_ids)), 'standalone_card_identity_changed')
    owner=index.products[s.card_id]
    gen.require(owner.is_active and owner.item_type=='stock' and owner.sku==s.native_code
                and barcode_keys(index.product_barcodes.get(owner.id,[]))==barcode_keys([members[s.prior_leaf].ean]), 'standalone_card_identity_changed')
    override=await db.get(ProductEditorOverride,s.card_id)
    gen.require(not override or 'eans' not in ((override.data or {}).get('variant') or {}),'standalone_card_identity_changed')
    pairs=(await db.execute(select(ShopProduct,Shop.code).join(Shop).where(ShopProduct.product_id.in_(card_ids))
                            .with_for_update(of=ShopProduct))).all()
    xlinks=[link for link,shop in pairs if shop=='xtrek']
    gen.require(len(xlinks)==1 and xlinks[0].id==s.link_id and not xlinks[0].push_pending and xlinks[0].product_id==s.card_id
                and xlinks[0].external_product_id==str(s.product_id), 'standalone_mapping_changed')
    mapping=xlinks[0]
    if phase!='complete':
        gen.require(not mapping.is_variant and not mapping.external_variant_id and not mapping.parent_code and mapping.external_code==s.native_code,
                    'standalone_mapping_changed')
    activity=await conflicts.card_activity(db,card_ids)
    gen.require(all(not any(f[k] for k in ('reservations','orders','native_orders','stock_work'))
                and all(v['qty_reserved']==0 and v['qty_quarantined']==0 for v in f['balances']) for f in activity.values()),'standalone_card_in_use')
    for r in s.members:
        owner_id=resolutions[r.code].product_id
        if owner_id:
            balances=activity[owner_id]['balances']
            gen.require(all(v['qty_on_hand']==0 for v in balances) if r.code!=s.prior_leaf else
                        not balances or (len(balances)==1 and balances[0]['qty_on_hand']==s.observed_stock), 'standalone_physical_quantities_differ')
    pending=await db.scalar(text("""SELECT 1 FROM shop_order_items i JOIN shop_orders o ON o.id=i.order_id
           WHERE o.shop_id=:shop AND o.stock_state IN ('pending','reserved') AND i.external_product_code=ANY(CAST(:c AS text[])) LIMIT 1"""),
           {'shop':mapping.shop_id,'c':[s.native_code,*members]})
    fence=await db.scalar(text("""SELECT 1 FROM shop_sync_outbox WHERE product_id=ANY(CAST(:ids AS bigint[]))
           AND status IN ('pending','processing','failed') LIMIT 1"""),{'ids':card_ids})
    pin=await db.scalar(text("""SELECT 1 FROM atomer_review_checks WHERE run_id=:r AND detail->>'product_id'=:pid LIMIT 1"""),
                        {'r':s.run_id,'pid':str(s.product_id)})
    scheduled=await db.scalar(text("""SELECT 1 FROM (
          SELECT i.sku FROM stock_sync_items i JOIN stock_sync_runs r ON r.id=i.run_id
            WHERE r.shop_id=:shop AND r.status IN ('queued','running') AND i.status IN ('prepared','sending','uncertain')
          UNION ALL
          SELECT i.sku FROM stock_publication_items i JOIN stock_publication_batches b ON b.id=i.batch_id
            WHERE b.shop_id=:shop AND b.status IN ('queued','running') AND i.status IN ('prepared','sending','uncertain')
        ) active WHERE sku=ANY(CAST(:codes AS text[])) LIMIT 1"""),
        {'shop':mapping.shop_id,'codes':[s.native_code,*members,*(index.products[i].sku for i in card_ids)]})
    gen.require(not pending and not fence and not pin and not scheduled, 'standalone_card_in_use')
    other=[{'id':link.id,'card':link.product_id,'pid':link.external_product_id,'vid':link.external_variant_id,'sku':link.external_code}
           for link,shop in pairs if shop!='xtrek']
    return rows,mapping,plain(activity),sorted(other,key=lambda v:v['id']),saved_work


async def document(db,s,rows,fp):
    campaign=await engine._one(db,"""SELECT * FROM atomer_catalog_campaigns WHERE source_sha=:s AND admin_sha=:a
                                    AND shop_fingerprint=:f""",s=s.source_sha,a=s.admin_sha,f=fp)
    gen.require(campaign and campaign['options'], 'standalone_verified_registry_required')
    campaign={**campaign,'checked_run_id':s.run_id}
    index=await engine._source(db,campaign)
    raw,_=index.family(s.parent)
    members=validated(s)
    gen.require(set(raw)=={s.parent,*members} and source.named(raw[s.parent],'Interné číslo')==s.native_code
                and all(source.named(raw[c],'Interné číslo')==item.source_id for c,item in members.items()),
                'standalone_source_relationship_changed')
    guards={'rows':rows,'blocked':{},'excluded':{},'scope':{k:'xtrek' for k in rows}}
    roots=await engine._root_index(db,campaign)
    return await asyncio.to_thread(engine._make_document,index,'v:'+s.parent,rows,guards,campaign['options'],campaign['selection'],roots)


async def finish(db,s,job,fp,actor,apply):
    rows,mapping,activity,other,saved_work=await local_checks(db,s,'complete' if job.state=='verified' else 'claimed')
    gen.require(job.document['fingerprint']==fp and other==job.document['other_links'], 'standalone_mapping_changed')
    live,flat=await asyncio.to_thread(read_native,s,fp,True)
    try:
        ids,stocks=observed(s,job.document['before'],live,flat,job.document['source'])
    except work.ReviewError as exc:
        if apply and job.state!='verified':
            job.state='uncertain'
            job.document={**job.document,'last_read_issue':exc.code,'observed_product_id':live.get('product_id'),
                          'observed_variants':{v.get('code'):v.get('variant_id') for v in live.get('variants') or []},
                          'observed_stock':{'parent':conflicts.native_stock(live),
                                            'leaves':{v.get('code'):conflicts.native_stock(v) for v in live.get('variants') or []}}}
            await db.commit()
        return {'status':'uncertain','count':len(rows),'reason':exc.code}
    target=ids[s.prior_leaf]
    if job.state=='verified':
        gen.require(mapping.is_variant and mapping.external_variant_id==str(target) and mapping.parent_code==s.native_code
                    and mapping.external_code==s.prior_leaf and ids==job.document['variant_ids'], 'standalone_mapping_changed')
    elif apply:
        collision=await db.scalar(select(ShopProduct.id).where(ShopProduct.shop_id==mapping.shop_id,ShopProduct.is_variant.is_(True),
                 ShopProduct.external_variant_id.in_([str(i) for i in ids.values()])).limit(1))
        gen.require(not collision, 'standalone_mapping_changed')
        mapping.external_variant_id=str(target)
        mapping.parent_code=s.native_code
        mapping.external_code=s.prior_leaf
        mapping.is_variant=True
        job.state='verified'
        job.document={**job.document,'variant_ids':ids,'stock':stocks,'after_card_activity':activity,'verified_by':actor,
                      'transfer':{'link_id':mapping.id,'card_id':mapping.product_id,'before':[s.product_id,None,s.native_code],
                                  'after':[s.product_id,target,s.prior_leaf]}}
        for code,vid in ids.items():
            detail=f'{s.native_code}: xTrek {s.product_id}/{vid}, EAN {validated(s)[code].ean}, veľkosť overená.'
            if code==s.prior_leaf:
                detail+=f' Zachovaná karta {s.card_id}, väzba {s.link_id}, pôvodná zásoba {s.observed_stock}.'
            stage='queued' if job.document['work_before'][code]['instruction'] else 'open'
            await repair.outcome(db,code,actor,stage,detail,'standalone_family_verified')
        await db.commit()
    return {'status':'verified' if apply or job.state=='verified' else 'ready','count':len(ids),
            'reason':f'xTrek {s.product_id}, karta {s.card_id}, väzba {s.link_id}, pôvodná zásoba {s.observed_stock}.',
            'variant_ids':ids,'native_stocks':stocks}


async def process(db,s,mode,actor):
    validated(s)
    head=await engine._head(db)
    gen.require(head and (head['source_sha256'],head['admin_export_sha256'])==(s.source_sha,s.admin_sha), 'variant_source_changed')
    await work._lock_current_run(db,head['id'])
    await db.execute(text('SELECT pg_advisory_xact_lock(:k)'),{'k':IDENTITY_WRITE_LOCK})
    fp=repair.target_fingerprint('xtrek')
    key=repair.digest({'operation':'source_parent_conversion','product_id':s.product_id,'source_sha':s.source_sha,
                       'admin_sha':s.admin_sha,'fingerprint':fp})
    job=await db.get(AtomerVariantRepair,key,with_for_update=True)
    if job:
        gen.require(job.document['selection']==s.model_dump(mode='json') and job.state in ('sending','uncertain','verified'),
                    'standalone_existing_intent_changed')
        if job.state=='verified':
            mapping=await db.get(ShopProduct,s.link_id)
            ids=job.document['variant_ids']
            live,flat=await asyncio.to_thread(read_native,s,fp,True)
            wanted={(r.code,r.ean,ids[r.code]) for r in s.members}
            gen.require(mapping and mapping.product_id==s.card_id and mapping.is_variant and mapping.external_code==s.prior_leaf
                        and mapping.external_product_id==str(s.product_id) and mapping.external_variant_id==str(ids[s.prior_leaf])
                        and mapping.parent_code==s.native_code and live['code']==s.native_code and live['product_id']==s.product_id
                        and all({(v['code'],v.get('ean'),v['variant_id']) for v in leaves}==wanted for leaves in (live['variants'],flat)),
                        'standalone_mapping_changed')
            return {'status':'verified','count':len(ids),'variant_ids':ids,
                    'reason':f'Už prepojené, karta {s.card_id}, väzba {s.link_id}.', 'stock_at_conversion':job.document['stock']}
        return await finish(db,s,job,fp,actor,mode=='apply')
    rows,mapping,activity,other,saved_work=await local_checks(db,s,'before')
    await repair.require_target_available(db,'xtrek',s.native_code)
    await repair.require_target_available(db,'xtrek',s.parent)
    doc=await document(db,s,rows,fp)
    products=await cards.load_shop_catalog('xtrek')
    free_other_codes(s,products)
    before,_=await asyncio.to_thread(read_native,s,fp)
    patch=structure(s,doc,before)
    if mode=='preview':
        return {'status':'ready','count':len(rows),'reason':f'xTrek {s.product_id}, karta {s.card_id}, zásoba {s.observed_stock} iba pre {s.prior_leaf}.',
                'source_parent':s.parent,'original':s.native_code,'target_codes':[v['code'] for v in patch['variants']],
                'old_stock':conflicts.native_stock(before),'old_url':[(v.get('language'),v.get('url')) for v in before['descriptions']]}
    for code in rows:
        await repair.outcome(db,code,actor,'working','Prevod pôvodnej karty na doložený variant.','standalone_family_claim')
    job=AtomerVariantRepair(id=key,parent_code=s.native_code,state='sending',
        document={'operation':'source_parent_conversion','selection':s.model_dump(mode='json'),'fingerprint':fp,'source':doc,
                  'before':before,'patch':patch,'actor':actor,'previous_link':{'card_id':mapping.product_id,'id':mapping.id,
                  'pid':mapping.external_product_id,'vid':mapping.external_variant_id,'sku':mapping.external_code},'other_links':other,
                  'before_card_activity':activity,'work_before':saved_work})
    db.add(job)
    await db.commit()
    # Only this first claimant may dispatch. A timeout, restart or duplicate request goes straight to readback above.
    await local_checks(db,s,'claimed')
    job=await db.get(AtomerVariantRepair,key,with_for_update=True,populate_existing=True)
    if job.state=='verified':
        return await finish(db,s,job,fp,actor,True)
    gen.require(job.state=='sending' and repair.target_fingerprint('xtrek')==fp, 'standalone_existing_intent_changed')
    # A second concurrent request can only observe this intent. Confirm the entire source family and the original count again.
    free_other_codes(s,await cards.load_shop_catalog('xtrek'))
    now,_=await asyncio.to_thread(read_native,s,fp)
    gen.require(before==now, 'standalone_native_changed_before_write')
    try:
        message=await asyncio.to_thread(gen.send,[patch],fp)
    except native.transport.SourceError as exc:
        message=exc.code
    job.document={**job.document,'dispatch_response':message or 'ok'}
    # Keep the common inventory-write lock across dispatch, readback and rebinding.
    return await finish(db,s,job,fp,actor,True)
