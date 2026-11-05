"""Correct a misplaced variant barcode after proving both distinct source and manufacturer identities."""
import asyncio

from pydantic import Field, HttpUrl
from sqlalchemy import func, or_, select, text

from inventory_hub.db_models import Product, ProductIdentifier, Shop
from inventory_hub.db_models_ext import ShopProduct
from inventory_hub.product_editor_models import AtomerVariantRepair, ProductEditorOverride
from inventory_hub.services import atomer_card_batch as cards, atomer_conflict_resolution as conflicts, atomer_generations as gen
from inventory_hub.services import atomer_recode as recode, atomer_review as work, atomer_source_state as state
from inventory_hub.services import atomer_variant_repair as repair
from inventory_hub.services.identifiers import ProductIdentifierService
from inventory_hub.services.product_identity import IDENTITY_WRITE_LOCK, RemoteIdentity, barcode_keys, verified_barcodes
from inventory_hub.services.product_publication_source import plain


class ManufacturerVariant(gen.Strict):
    code: repair.Token
    source_id: str = Field(pattern=r'^[1-9][0-9]{0,19}$')
    source_visible: bool
    size: repair.Token
    supplier_code: repair.Token
    manufacturer_id: str = Field(pattern=r'^[1-9][0-9]{0,19}$')
    ean: repair.Token
    revision: int = Field(ge=1)


class Selection(gen.Strict):
    run_id: int = Field(gt=0)
    source_sha: str = Field(pattern=r'^[0-9a-f]{64}$')
    admin_sha: str = Field(pattern=r'^[0-9a-f]{64}$')
    parent: repair.Token
    product_id: int = Field(gt=0)
    variant_id: int = Field(gt=0)
    card_id: int = Field(gt=0)
    link_id: int = Field(gt=0)
    identifier_id: int = Field(gt=0)
    size_axis: repair.Token
    native_axes: repair.Axis = Field(min_length=1,max_length=15)
    native_visible: bool
    current: ManufacturerVariant
    sibling: ManufacturerVariant
    evidence_url: HttpUrl
    evidence_sha: str = Field(pattern=r'^[0-9a-f]{64}$')


def validate(s):
    leaves=(s.current,s.sibling)
    gen.require(all(len({getattr(v,k) for v in leaves})==2 for k in ('code','source_id','supplier_code','manufacturer_id','size'))
                and all(v.ean in verified_barcodes([v.ean]) and v.code==v.code.strip() and len(v.code)<=100 for v in leaves)
                and len(barcode_keys(v.ean for v in leaves))==2 and s.native_axes.get(s.size_axis)==s.current.size
                and s.evidence_url.scheme=='https' and not s.evidence_url.username and not s.evidence_url.password,
                'variant_barcode_evidence_changed')


def ident(s):
    return {'product_id':s.product_id,'parent_code':s.parent,'variant_id':s.variant_id,'variant_code':s.current.code,'code':s.current.code}


def projection(s,observation,ean):
    protected,axes=repair.projection(observation)
    rows=protected['leaves'].get(str(s.variant_id),[])
    gen.require(observation['parent']['code']==s.parent and observation['parent']['product_id']==s.product_id
                and observation['parent'].get('variants_exists_yn') is True and not observation['parent'].get('archived_yn')
                and len(rows)==2 and axes.get(str(s.variant_id))==s.native_axes and all(
                    v['code']==s.current.code and v.get('ean')==ean and v.get('active_yn') is s.native_visible
                    and conflicts.native_stock(v)['quantity']=='0' and isinstance(v.get('stocks'),list)
                    and all(st.get('quantity') in (0,None) for st in v['stocks']) for v in rows), 'variant_barcode_native_changed')
    for v in observation['variants']:
        if v['variant_id']!=s.variant_id:
            gen.require(v['code'] not in (s.current.code,s.sibling.code) and not (barcode_keys([v.get('ean')]) & barcode_keys([s.current.ean,s.sibling.ean])),
                        'variant_barcode_native_occupied')
    return protected,axes


def readback(s,backup,after):
    wanted,axes=projection(s,backup,s.sibling.ean)
    wanted=recode.protect_expected(wanted,[{'vid':str(s.variant_id),'ean':s.sibling.ean,'new_ean':s.current.ean}])
    protected,actual_axes=projection(s,after,s.current.ean)
    gen.require(protected==wanted and axes==actual_axes,'variant_barcode_other_fields_changed')


def native_owners(s,shop,parents,corrected=False):
    wanted=barcode_keys([s.current.ean,s.sibling.ean])
    found=[]
    for product in parents:
        for leaf in (product,*(product.get('variants') or [])):
            if leaf['code'].casefold() in {s.current.code.casefold(),s.sibling.code.casefold()} or barcode_keys([leaf.get('ean')])&wanted:
                found.append((product['product_id'],leaf.get('variant_id'),leaf['code'],leaf.get('ean')))
    expected=[(s.product_id,s.variant_id,s.current.code,s.current.ean if corrected else s.sibling.ean)] if shop=='xtrek' else []
    gen.require(found==expected,'variant_barcode_competing_native_owner')


async def lock_source(db,s,initial=False):
    head=(await db.execute(text('SELECT id,source_sha256,admin_export_sha256 FROM atomer_progress_runs ORDER BY id DESC LIMIT 1'))).one_or_none()
    gen.require(head and (head[1],head[2])==(s.source_sha,s.admin_sha),'variant_source_changed')
    await work._lock_current_run(db,head[0])
    gen.require(not initial or head[0]==s.run_id,'run_changed')
    await db.execute(text('SELECT pg_advisory_xact_lock(:key)'),{'key':IDENTITY_WRITE_LOCK})


async def checks(db,s,claimed=False):
    await lock_source(db,s,initial=not claimed)
    archive=await state.admin_index(db,s.admin_sha)
    leaves=(s.current,s.sibling)
    admin={}
    for v in leaves:
        a=archive.get(v.code) or {}
        gen.require(a.get('internal_id')==v.source_id and a.get('parent')==s.parent and not a.get('conflict')
                    and a.get('visibility') is v.source_visible and a.get('ean')==s.sibling.ean and a.get('has_catalog_code'),
                    'variant_barcode_source_changed')
        admin[v.code]=a
        detail=await db.scalar(text('SELECT detail FROM atomer_progress_items WHERE run_id=:r AND code=:c'),{'r':s.run_id,'c':v.code})
        gen.require(detail and detail.get('state')=='review' and set(detail.get('reasons') or [])=={'different_shop_link','target_claimed'}
                    and detail.get('kind')=='variant' and detail.get('ean')==s.sibling.ean and detail.get('parent')==s.parent
                    and detail.get('product_id')==str(s.product_id) and detail.get('variant_id')==str(s.variant_id)
                    and detail.get('target_code')==s.current.code and detail.get('target_ean')==s.sibling.ean
                    and set(detail.get('candidates') or [detail.get('hub_id')])=={s.card_id}, 'variant_barcode_review_changed')
        item=await work._record(db,'item',v.code)
        command=await db.scalar(text("SELECT command FROM atomer_review_events WHERE kind='item' AND key=:c AND revision=:v"),
                                {'c':v.code,'v':item['revision']})
        gen.require(item['status']==('working' if claimed else 'blocked') and item['revision']==v.revision+int(claimed)
                    and command==('variant_barcode_owners_claim' if claimed else 'review_block')
                    and not item['note'] and not item['instruction'] and item['scope']!='biketrek' and item['decision']=='undecided',
                    'review_changed')
    group=await work._record(db,'family','v:'+s.parent)
    gen.require(group['status']=='open' and group['revision']==0 and group['scope']!='biketrek' and not group['note']
                and not group['instruction'] and group['decision']=='undecided','review_changed')
    aliases={k for k,v in archive.items() if barcode_keys([v.get('ean'),*(v.get('all_eans') or [])])&barcode_keys([s.current.ean,s.sibling.ean])}
    gen.require(aliases=={s.current.code,s.sibling.code},'variant_barcode_ambiguous_source')
    await repair.require_target_available(db,'xtrek',s.parent)
    blocked=await db.scalar(text("SELECT 1 FROM atomer_review_checks WHERE run_id=:r AND key=:k"),{'r':s.run_id,'k':f'{s.product_id}/{s.variant_id}'})
    gen.require(not blocked,'recode_target_protected')
    card=await db.get(Product,s.card_id,with_for_update=True)
    gen.require(card and card.sku==s.current.code and card.is_active and card.item_type=='stock','variant_barcode_card_changed')
    candidate=await db.scalar(select(Product.id).where(func.lower(Product.sku)==s.sibling.code.casefold()))
    gen.require(candidate is None,'variant_barcode_sibling_card_appeared')
    links=(await db.execute(select(ShopProduct,Shop.code).join(Shop).where(ShopProduct.product_id==s.card_id).with_for_update(of=ShopProduct))).all()
    gen.require(len(links)==1 and links[0][1]=='xtrek','variant_barcode_mapping_changed')
    link=links[0][0]
    gen.require(link.id==s.link_id and link.external_code==s.current.code and link.external_product_id==str(s.product_id)
                and link.external_variant_id==str(s.variant_id) and link.is_variant and link.parent_code==s.parent
                and not link.push_pending,'variant_barcode_mapping_changed')
    override=await db.get(ProductEditorOverride,s.card_id)
    gen.require(not override or 'eans' not in ((override.data or {}).get('variant') or {}),'recode_barcode_override')
    rows=list(await db.scalars(select(ProductIdentifier).where(ProductIdentifier.identifier_type.in_(ProductIdentifierService.BARCODE_TYPES),
        or_(ProductIdentifier.product_id==card.id,func.ltrim(ProductIdentifier.value,'0').in_([v.ean.lstrip('0') for v in leaves]))).with_for_update()))
    gen.require(len(rows)==1 and rows[0].id==s.identifier_id and rows[0].is_primary and rows[0].product_id==card.id
                and rows[0].value==s.sibling.ean and rows[0].supplier_id is None,'variant_barcode_owner_changed')
    extra=await db.scalar(text("""SELECT 1 FROM product_sku_identities WHERE source_code_fold=lower(:code)
            UNION ALL SELECT 1 FROM product_identifiers WHERE lower(value)=lower(:code) LIMIT 1"""),{'code':s.sibling.code})
    gen.require(not extra,'variant_barcode_sibling_card_appeared')
    f=(await conflicts.card_activity(db,[card.id]))[card.id]
    gen.require(not any(f[k] for k in ('orders','native_orders','reservations','stock_work')) and all(
        b['qty_on_hand']==b['qty_reserved']==b['qty_quarantined']==0 for b in f['balances']),'recode_card_in_use')
    pending=await db.scalar(text("""SELECT 1 FROM shop_order_items i JOIN shop_orders o ON o.id=i.order_id
            WHERE o.shop_id=:shop AND o.stock_state IN ('pending','reserved') AND i.external_product_code=ANY(CAST(:codes AS text[]))
            UNION ALL SELECT 1 FROM shop_sync_outbox WHERE product_id=:card AND status IN ('pending','processing','failed')
            UNION ALL SELECT 1 FROM stock_sync_items i JOIN stock_sync_runs r ON r.id=i.run_id
               WHERE r.shop_id=:shop AND r.status IN ('queued','running') AND i.status IN ('prepared','sending','uncertain') AND i.sku=ANY(CAST(:codes AS text[]))
            UNION ALL SELECT 1 FROM stock_publication_items i JOIN stock_publication_batches b ON b.id=i.batch_id
               WHERE b.shop_id=:shop AND b.status IN ('queued','running') AND i.status IN ('prepared','sending','uncertain') AND i.sku=ANY(CAST(:codes AS text[]))
            LIMIT 1"""),{'shop':link.shop_id,'card':card.id,'codes':[s.parent,s.current.code,s.sibling.code]})
    gen.require(not pending,'recode_card_in_use')
    return rows[0],admin[s.sibling.code],plain(f)


async def global_check(s,corrected):
    for shop in ('xtrek','biketrek'):
        try:
            parents=await cards.load_shop_catalog(shop)
        except cards.export.ExportError as error:
            if str(error) in ('upgates_export_budget_deferred','upgates_export_http_429'):
                raise gen.transport.SourceError('stock_publication_rate_limited',429,60,request_sent=False) from None
            raise work.ReviewError('variant_barcode_global_catalog_unavailable') from None
        native_owners(s,shop,parents,corrected)


def effective_sources(catalog,admin,remote,index,links,proofs):
    """Match both original rows through a verified correction only while the real identities still agree."""
    if not proofs:
        return catalog,{}
    by_code={r['code']:r for r in catalog}
    corrections={}
    for doc in proofs:
        s=Selection.model_validate(doc['selection'])
        cur,other=s.current,s.sibling
        selected=((cur,s.card_id),(other,doc['new_card']))
        if not all(v.code in by_code and by_code[v.code]['ean']==other.ean and by_code[v.code]['parent']==s.parent
                   and not by_code[v.code].get('conflict') and admin.get(v.code,{}).get('internal_id')==v.source_id
                   and not admin[v.code].get('conflict') and admin[v.code]['visibility'] is v.source_visible
                   and (card:=index.products.get(card_id)) and card.sku==v.code and card.is_active
                   and barcode_keys(index.product_barcodes.get(card_id,()))==barcode_keys([v.ean])
                   and index.resolve(RemoteIdentity(index.shop_id,v.code,barcodes=(v.ean,))).product_id==card_id
                   for v,card_id in selected):
            continue
        native=[r for r in remote if r['code']==cur.code or barcode_keys([r['ean']])&barcode_keys([cur.ean])]
        bound=[r for r in links if r.product_id==s.card_id]
        if len(native)!=1 or len(bound)!=1:
            continue
        target,link=native[0],bound[0]
        if ((target['pid'],target['vid'],target['parent'],target['code'],target['ean'])!=
                (str(s.product_id),str(s.variant_id),s.parent,cur.code,cur.ean) or
                (link.id,link.external_product_id,link.external_variant_id,link.external_code,link.parent_code)!=
                (s.link_id,str(s.product_id),str(s.variant_id),cur.code,s.parent) or not link.is_variant or link.push_pending):
            continue
        competing=[r for r in remote if r['code']==other.code or barcode_keys([r['ean']])&barcode_keys([other.ean])]
        if len(competing)>1 or any(r['code']!=other.code or r['parent']!=s.parent or r['ean']!=other.ean for r in competing):
            continue
        by_code[cur.code]={**by_code[cur.code],'ean':cur.ean}
        corrections[cur.code]={'atomer_ean':other.ean,'checked_ean':cur.ean,'manufacturer_id':cur.manufacturer_id,
                               'manufacturer_or_supplier':str(s.evidence_url),'existing_card':s.card_id,'sibling_card':doc['new_card'],
                               'journal_id':doc.get('journal_id')}
    return list(by_code.values()),corrections


async def verify(db,s,job,fp,actor,apply):
    former,other,facts=await checks(db,s,True)
    now=await asyncio.to_thread(repair.read_family,ident(s),fp)
    try:
        readback(s,job.document['before'],now)
    except work.ReviewError as error:
        if apply:
            job.document={**job.document,'last_read_issue':error.code,'native_observed':[
                {'variant_id':v['variant_id'],'code':v['code'],'ean':v.get('ean')} for v in now['variants'] if v['variant_id']==s.variant_id]}
            await db.commit()
        return {'status':'uncertain','count':2,'reason':error.code}
    await global_check(s,True)
    if not apply:
        return {'status':'ready','count':2,'reason':'GTIN pôvodného variantu potvrdený, čaká zaradenie dvoch identít skladu.'}
    # Move the unchanged identifier to its correct, distinct sibling, and give the existing stocked-item ID its correct code.
    name,brand=other.get('name'),other.get('brand')
    gen.require(name and len(name)<=500 and brand and len(brand)<=100,'variant_barcode_source_changed')
    sibling=Product(sku=s.sibling.code,name=name,brand=brand,group_id=None,created_from_source='atomer')
    db.add(sibling)
    await db.flush()
    former.product_id=sibling.id
    await db.flush()
    new=await ProductIdentifierService(db).add_identifier(s.card_id,s.current.ean,is_primary=True,
        notes='Oprava zámeny veľkostí podľa registra výrobcu')
    observed=dict((await db.execute(select(ProductIdentifier.product_id,ProductIdentifier.value).where(
        ProductIdentifier.id.in_((former.id,new.id)),ProductIdentifier.is_primary.is_(True)))).all())
    gen.require(observed=={s.card_id:s.current.ean,sibling.id:s.sibling.ean}
                and not await db.scalar(select(ShopProduct.id).where(ShopProduct.product_id==sibling.id).limit(1)),
                'variant_barcode_card_readback_failed')
    job.state='verified'
    job.document={**job.document,'existing_card':s.card_id,'new_card':sibling.id,'identifiers':{s.current.code:new.id,s.sibling.code:former.id},
                  'after_activity':facts,'finished_by':actor}
    for v,card in ((s.current,s.card_id),(s.sibling,sibling.id)):
        message=f'{v.code} / {v.ean} · karta {card}.'
        if v==s.current:
            message+=f' xTrek {s.product_id}/{s.variant_id}, väzba {s.link_id}; opravený EAN.'
        else:
            message+=' Čaká vytvorenie zdrojového variantu xTrek so zachovanou viditeľnosťou.'
        await repair.outcome(db,v.code,actor,'open',message,'variant_barcode_owners_verified')
    await db.commit()
    return {'status':'verified','count':2,'reason':f'xTrek {s.product_id}/{s.variant_id} · karty {s.card_id}, {sibling.id}.',
            'cards':{s.current.code:s.card_id,s.sibling.code:sibling.id},'journal_id':job.id,'completed_native_variants':1}


async def dispatch(db,s,job,fp,actor,apply):
    await checks(db,s,True)
    await global_check(s,False)
    before=await asyncio.to_thread(repair.read_family,ident(s),fp)
    gen.require(projection(s,before,s.sibling.ean)==projection(s,job.document['before'],s.sibling.ean),'variant_barcode_native_changed')
    if not apply:
        return {'status':'ready','count':2,'reason':'Nedotknutý pôvodný variant a priradené veľkosti boli znova overené.'}
    key=job.id
    job.state='sending'
    await db.commit()
    await lock_source(db,s)
    job=await db.get(AtomerVariantRepair,key,with_for_update=True,populate_existing=True)
    gen.require(job.state=='sending' and job.document['fingerprint']==fp==repair.target_fingerprint('xtrek'),'variant_barcode_intent_changed')
    await checks(db,s,True)
    patch={'code':s.parent,'variants':[{'code':s.current.code,'ean':s.current.ean}]}
    try:
        message=await asyncio.to_thread(gen.send,[patch],fp)
    except gen.transport.SourceError as error:
        if error.status==429 and error.request_sent is False:
            job.state='prepared'
            job.document={**job.document,'dispatch':'rate_limited_before_request'}
            await db.commit()
            raise
        message=error.code
    job.document={**job.document,'dispatch':message or 'ok'}
    return await verify(db,s,job,fp,actor,True)


async def process(db,s,mode,actor):
    validate(s)
    await lock_source(db,s)
    fp=repair.target_fingerprint('xtrek')
    key=repair.digest({'operation':'misassigned_variant_barcode','source_sha':s.source_sha,'admin_sha':s.admin_sha,'parent':s.parent,
                       'variant_id':s.variant_id,'fingerprint':fp})
    job=await db.get(AtomerVariantRepair,key,with_for_update=True)
    if job:
        gen.require(s.model_dump(mode='json')==job.document['selection'] and job.state in ('prepared','sending','verified'),'variant_barcode_intent_changed')
        if job.state=='prepared':
            return await dispatch(db,s,job,fp,actor,mode=='apply')
        if job.state=='verified':
            result={s.current.code:s.card_id,s.sibling.code:job.document['new_card']}
            now=await asyncio.to_thread(repair.read_family,ident(s),fp)
            gen.require([v['ean'] for v in now['variants'] if v['variant_id']==s.variant_id and v['code']==s.current.code]==[s.current.ean],'variant_barcode_native_changed')
            stored=dict((await db.execute(select(ProductIdentifier.product_id,ProductIdentifier.value).where(
                ProductIdentifier.id.in_(job.document['identifiers'].values())))).all())
            gen.require(stored=={result[v.code]:v.ean for v in (s.current,s.sibling)},'variant_barcode_owner_changed')
            return {'status':'verified','count':2,'reason':'Už prepojené','cards':result,'journal_id':job.id,'completed_native_variants':1}
        return await verify(db,s,job,fp,actor,mode=='apply')
    _,_,facts=await checks(db,s)
    await global_check(s,False)
    before=await asyncio.to_thread(repair.read_family,ident(s),fp)
    projection(s,before,s.sibling.ean)
    if mode=='preview':
        return {'status':'ready','count':2,'reason':f'{s.current.code} / {s.current.ean}, {s.sibling.code} / {s.sibling.ean}.',
                'current':ident(s),'current_card':s.card_id,'quantity':0}
    for v in (s.current,s.sibling):
        await repair.outcome(db,v.code,actor,'working','Oprava zameneného EAN podľa evidencie výrobcu.','variant_barcode_owners_claim')
    job=AtomerVariantRepair(id=key,parent_code=s.parent,state='prepared',document={'operation':'misassigned_variant_barcode',
        'selection':s.model_dump(mode='json'),'source_sha':s.source_sha,'admin_sha':s.admin_sha,'fingerprint':fp,
        'before':before,'before_activity':facts,'actor':actor})
    db.add(job)
    await db.commit()
    await lock_source(db,s)
    job=await db.get(AtomerVariantRepair,key,with_for_update=True,populate_existing=True)
    if job.state=='prepared':
        return await dispatch(db,s,job,fp,actor,True)
    return await process(db,s,mode,actor)
