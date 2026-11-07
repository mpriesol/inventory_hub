"""Reconcile physical Atomer leaves to refreshed native shop identities in one Hub transaction."""
import asyncio
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from urllib.parse import urlsplit

from sqlalchemy import select, text

from inventory_hub.db_models import Product, ProductIdentifier, Shop
from inventory_hub.db_models_ext import ShopProduct
from inventory_hub.routers import upgates_sync as pull
from inventory_hub.services import atomer_catalog_observation as observation, atomer_review as work
from inventory_hub.services import atomer_catalog_checkpoint as checkpoint
from inventory_hub.services import atomer_review_targets, migration_upgates_export as export
from inventory_hub.services import product_publication_source as publication
from inventory_hub.services.product_identity import (
    IDENTITY_WRITE_LOCK, IdentityIndex, RemoteIdentity, barcode_keys, verified_barcodes,
)
from inventory_hub.services.stock_publication_source import _client
from inventory_hub.services.atomer_source_cards import source_cards
from inventory_hub.services.order_collection import target_fingerprint

SHOPS = ('xtrek', 'biketrek')
MAX_BATCH = 2000
_CATALOG_LOCKS = {s: Lock() for s in SHOPS}


class ObservedCatalog(list):
    """Pin a complete catalog to the start of its native read, also for negative lookup receipts."""
    def __init__(self,parents,checked,fingerprint):
        super().__init__(parents)
        self.checked=checked
        self.fingerprint=fingerprint


class CardConflict(ValueError):
    """A specific source, identity, shop-scope or ownership problem on one physical leaf."""


def require(ok, reason):
    if not ok:
        raise CardConflict(reason)


def _catalog(shop):
    # A persisted catalog must still pass a live delta, cardinality and read-window check.
    fp = target_fingerprint(shop)
    with _CATALOG_LOCKS[shop]:
        stamp = datetime.now(timezone.utc)
        client = _client(shop,fp)
        try:
            directory=pull._cache_path(shop).parent/'atomer-card-catalog-v1'
            return ObservedCatalog(checkpoint.collect(client,directory,fp),stamp,fp)
        finally:
            client.session.close()


async def load_catalogs(shops=SHOPS):
    """Return complete per-shop parent lists. Selected identities are always reread before use."""
    results=await asyncio.gather(*(asyncio.to_thread(_catalog,s) for s in shops))
    return dict(zip(shops,results))


async def load_shop_catalog(shop):
    """Current complete per-shop GTIN/SKU index for corrections before a native create."""
    if shop not in SHOPS:
        raise atomer_review_targets.ReviewError('shops_unavailable')
    return await asyncio.to_thread(_catalog,shop)


def _selected(shop, ids):
    if not ids:
        return {}
    client = _client(shop,target_fingerprint(shop))
    try:
        current = {}
        ordered = sorted(map(int,ids))
        for i in range(0,len(ordered),100):
            batch = ordered[i:i+100]
            filters = dict(product_ids=';'.join(map(str,batch)))
            parents = observation.indexed(observation.page_rows(client,'products',filters),'product_id')
            if set(parents)!=set(batch):
                raise export.ExportError('upgates_export_catalog_changed')
            variants = [pid for pid,parent in parents.items() if parent.get('variants_exists_yn')]
            if variants:
                # The ordinary product resource omits variant axes.
                params=observation.indexed(observation.page_rows(client,'products/parameters',{
                    'product_ids':';'.join(map(str,variants))}),'product_id')
                if set(params)!=set(variants):
                    raise export.ExportError('upgates_export_family_incomplete')
                for pid in variants:
                    parent, detail = parents[pid],params[pid]
                    native = observation.indexed(parent.get('variants') or [],'variant_id')
                    axes = observation.indexed(detail.get('variants') or [],'variant_id')
                    if parent.get('code')!=detail.get('code') or set(native)!=set(axes):
                        raise export.ExportError('upgates_export_catalog_changed')
                    for vid, leaf in native.items():
                        if leaf.get('code')!=axes[vid].get('code') or not isinstance(axes[vid].get('parameters'),list):
                            raise export.ExportError('upgates_export_catalog_changed')
                        leaf['parameters']=axes[vid]['parameters']
            current.update(parents)
        return publication.plain({str(k):v for k,v in current.items()})
    finally:
        client.session.close()


def _large_variants(shop, families, wanted):
    """Verify a large parent header and just the selected physical codes with their native axes."""
    from inventory_hub.services.atomer_family_batch import collect_pages
    codes={}
    for pid,vid in wanted:
        leaves=[v for v in families[pid]['variants'] if str(v.get('variant_id'))==vid]
        if len(leaves)!=1 or not leaves[0].get('code') or ';' in leaves[0]['code'] or leaves[0]['code'] in codes:
            raise export.ExportError('upgates_export_family_incomplete')
        codes[leaves[0]['code']]=(pid,vid)
    client=_client(shop,target_fingerprint(shop))
    try:
        headers={}
        parents=sorted(families)
        for i in range(0,len(parents),100):
            group=parents[i:i+100]
            listed=observation.indexed(collect_pages(client,'products','products',
                {'product_ids':';'.join(group),'variants_yn':'false'}),'product_id')
            if set(listed)!={int(v) for v in group}:
                raise export.ExportError('upgates_export_family_incomplete')
            for k,header in listed.items():
                pid=str(k); cached=families[pid]
                if (header.get('code')!=cached.get('code') or header.get('variants_exists_yn') is not True or
                        header.get('variants') or not header.get('last_update_time') or
                        header['last_update_time']!=cached.get('last_update_time')):
                    raise export.ExportError('upgates_export_catalog_changed')
                headers[pid]=header
        fresh={}
        ordered=sorted(codes)
        for i in range(0,len(ordered),40):
            group=ordered[i:i+40]
            records=collect_pages(client,'products/variants','variants',{'variant_codes':';'.join(group)})
            for key,leaf in observation.indexed(records,'variant_id').items():
                code=leaf.get('code'); pid=str(leaf.get('product_id')); k=(pid,str(key))
                if (code not in group or codes[code]!=k or k in fresh or leaf.get('product_code')!=headers[pid]['code'] or
                        not isinstance(leaf.get('parameters'),list)):
                    raise export.ExportError('upgates_export_family_incomplete')
                fresh[k]=leaf
        if set(fresh)!=wanted:
            raise export.ExportError('upgates_export_family_incomplete')
        # Retain certified siblings in the global uniqueness index. Only keys in
        # `fresh` have been read separately and may be used to change a Hub link.
        for pid,header in headers.items():
            header['variants']=[fresh.get((pid,str(v['variant_id'])),v) for v in families[pid]['variants']]
        return headers
    finally:
        client.session.close()


def _read_candidates(shop,catalog,keys):
    pids={pid for pid,vid in keys}
    large={str(p['product_id']):p for f in catalog.families if (p:=f['payload']).get('product_id') is not None
           and str(p['product_id']) in pids and len(p.get('variants') or ())>100}
    selected={key for key in keys if key[0] in large}
    ids=pids-large.keys()
    parents=_selected(shop,ids) if ids else {}
    observed={(pid,str(v['variant_id']) if v.get('variant_id') is not None else None)
              for pid,p in parents.items() for v in p.get('variants') or [p]}
    if large:
        parents.update(_large_variants(shop,large,selected))
        observed.update(selected)
    return parents,observed


@dataclass
class Catalog:
    families: list
    by_id: dict
    by_sku: dict
    by_gtin: dict
    supplier_codes: dict
    parent_codes: set

    @classmethod
    def build(cls,shop_id,products):
        families=pull._pull_families(shop_id,products)
        by_id, by_sku, by_gtin = defaultdict(list), defaultdict(set), defaultdict(set)
        parent_codes=set()
        supplier_codes=defaultdict(set)
        for fam in families:
            parent=fam['payload']
            if parent.get('variants'):
                parent_codes.add(str(parent.get('code') or '').casefold())
            for identity, leaf in fam['leaves']:
                key=(identity.external_product_id,identity.external_id if identity.is_variant else None)
                by_id[key].append((fam,identity,leaf))
                by_sku[identity.code.casefold()].add(key)
                if leaf.get('code_supplier'):
                    supplier_codes[str(leaf['code_supplier']).strip().casefold()].add(key)
                for gtin in barcode_keys(identity.barcodes):
                    by_gtin[gtin].add(key)
        return cls(families,by_id,by_sku,by_gtin,supplier_codes,parent_codes)

    def candidates(self,codes,eans):
        selected=set()
        for code in codes:
            if code:
                selected.update(self.by_sku.get(code.casefold(),()))
        for gtin in barcode_keys(eans):
            selected.update(self.by_gtin.get(gtin,()))
        return selected

    def get(self,key):
        matches=self.by_id.get(key,[])
        require(len(matches)==1,'native_id_missing_or_ambiguous')
        family,identity,leaf=matches[0]
        parent=family['payload']
        require(identity.external_product_id and identity.external_id and
                bool(parent.get('variants')) is bool(parent.get('variants_exists_yn')) and
                not parent.get('set_yn') and bool(parent.get('code')),'native_leaf_type_unknown')
        if identity.is_variant:
            require(leaf.get('product_id') is not None and str(leaf['product_id'])==identity.external_product_id
                    and leaf.get('product_code')==identity.parent_code,'native_parent_changed')
        # The ordinary family import must hold the whole family if any two
        # siblings share a GTIN. Here an existing card selects just one physical
        # leaf, often in a till parent with thousands of unrelated articles.
        reasons=family['reasons']-{'remote_barcode_duplicate'}
        if any(self.by_gtin[gtin]!={key} for gtin in barcode_keys(identity.barcodes)):
            reasons=reasons|{'remote_barcode_duplicate'}
        require(not reasons,','.join(sorted(reasons)))
        return family,identity,leaf


def native_key(pair):
    pid,vid=pair
    require(str(pid).isdigit() and int(pid)>0 and (vid is None or str(vid).isdigit() and int(vid)>0),
            'native_id_missing_or_ambiguous')
    return str(pid),str(vid) if vid is not None else None


def raw_codes(lookup):
    return {str(v).strip() for v in lookup if v is not None and str(v).strip()}


def barcode_subject(row,proof=None,zero_id=None,source_id=None):
    """Use the original SKU for placeholders. Only independent, source-pinned evidence replaces a GTIN."""
    code,raw=row['code'],row.get('ean') or ''
    if proof is not None:
        ean=proof.get('ean') if isinstance(proof,dict) else None
        try:
            url=urlsplit(str(proof.get('manufacturer_or_supplier') or '')) if isinstance(proof,dict) else None
        except ValueError:
            raise CardConflict('barcode_correction_evidence_changed') from None
        card=proof.get('required_card_id') if isinstance(proof,dict) else None
        archive=proof.get('required_atomer_id') if isinstance(proof,dict) else None
        if archive is not None:
            require(not raw and isinstance(archive,str) and archive.isascii() and archive.isdigit() and
                    int(archive)>0 and archive==source_id,'barcode_correction_source_id_changed')
        require((raw or type(card) is int and card>0 or archive) and raw not in verified_barcodes([raw]) and isinstance(ean,str) and
                ean in verified_barcodes([ean]) and proof.get('before')==raw and
                proof.get('parent')==(row.get('parent') or code) and url.scheme=='https' and
                url.hostname and not url.username and not url.password and len(str(proof.get('evidence') or ''))>=20,
                'barcode_correction_evidence_changed')
        return {**row,'ean':ean,'barcode_evidence':dict(proof)}
    if zero_id is not None:
        require(raw=='0' and row.get('kind')=='standalone' and row.get('family_key')=='s:'+code
                and isinstance(zero_id,str) and zero_id.isascii() and zero_id.isdigit() and int(zero_id)>0,
                'atomer_zero_barcode_subject_changed')
        return {**row,'ean':'','barcode_evidence':dict(before=raw,type='source_zero_sentinel',native_id=zero_id)}
    if raw==code and any(c.isalpha() for c in raw):
        return {**row,'ean':'','barcode_evidence':{'before':raw,'type':'source_repeats_sku'}}
    require(not raw or raw in verified_barcodes([raw]),'atomer_source_ean_unverified')
    return row


def agree_eans(source,identity):
    incoming=raw_codes([source])
    native=raw_codes(identity.barcodes)
    if incoming and native:
        a,b=barcode_keys(incoming),barcode_keys(native)
        require(a==b if a and b else incoming==native,'native_ean_differs')


def choose_x(row,catalog,anchor):
    if anchor is not None:
        key=native_key(anchor)
    else:
        require(row['product_id'] and not row['reasons'],'xtrek_native_target_required')
        key=native_key((row['product_id'],row['variant_id']))
    family,identity,leaf=catalog.get(key)
    expected_code=row.get('target_code') if anchor is None else row['code']
    require(identity.code==expected_code and identity.is_variant==(row['kind']=='variant'),
            'xtrek_code_or_kind_changed')
    if anchor is None:
        require((identity.parent_code or '')==(row.get('target_parent') or ''),'xtrek_parent_changed')
        if row.get('target_ean') and (row.get('barcode_evidence') or {}).get('before')!=row['target_ean']:
            agree_eans(row['target_ean'],identity)
    elif identity.is_variant:
        require(identity.parent_code==row['parent'],'xtrek_parent_changed')
    agree_eans(row['ean'],identity)
    if row['ean'] and not identity.barcodes:
        raise CardConflict('xtrek_source_ean_missing')
    proofs=catalog.candidates((row['code'],identity.code),[row['ean'],*identity.barcodes])
    require(proofs=={key},'xtrek_identity_not_unique')
    require(identity.code==row['code'] or bool(barcode_keys([row['ean']]) & barcode_keys(identity.barcodes)),
            'xtrek_exact_code_or_ean_required')
    return key


def choose_bike(row,catalog,x_identity=None,stored=None,card_codes=(),card_eans=()):
    witness=raw_codes([row['code'],row.get('hub_sku'),x_identity.code if x_identity else None,*card_codes])
    eans=raw_codes([row.get('ean'),*(x_identity.barcodes if x_identity else ()),*card_eans])
    candidates=catalog.candidates(witness,eans)
    if stored:
        candidates.update(catalog.candidates([stored.external_code],[]))
        if stored.external_product_id and (not stored.is_variant or stored.external_variant_id):
            candidates.add(native_key((stored.external_product_id,stored.external_variant_id)))
    if not candidates:
        hints={key for code in witness for key in catalog.supplier_codes.get(code.casefold(),())}
        if hints:
            ids=', '.join(f"{pid}/{vid or '-'}" for pid,vid in sorted(hints,key=lambda k:(k[0],k[1] or ''))[:5])
            raise CardConflict('biketrek_supplier_code_requires_physical_identity ('+ids+')')
        return None
    require(len(candidates)==1,'biketrek_multiple_native_candidates')
    key=next(iter(candidates))
    _,identity,_=catalog.get(key)
    if stored:
        require(stored.external_code==identity.code and stored.is_variant==identity.is_variant and
                (not stored.external_product_id or stored.external_product_id==key[0]) and
                (not stored.external_variant_id or stored.external_variant_id==key[1]) and
                (not identity.is_variant or stored.parent_code==identity.parent_code),
                'biketrek_stored_native_id_changed')
    for known in [row.get('ean'),*(x_identity.barcodes if x_identity else ())]:
        if (row.get('barcode_evidence') or {}).get('before')!=known:
            agree_eans(known,identity)
    if barcode_keys(card_eans) and barcode_keys(identity.barcodes):
        require(not barcode_keys(card_eans).isdisjoint(barcode_keys(identity.barcodes)),
                'biketrek_stored_card_ean_differs')
    exact_gtin=bool(barcode_keys(eans) & barcode_keys(identity.barcodes))
    # GTIN can bridge shop-specific grouping (including old till variants).
    # Without it a variant must also have the same physical parent or an existing binding.
    require(exact_gtin or stored or (identity.code in witness and
            (row['kind']=='variant')==identity.is_variant and
            (not identity.is_variant or identity.parent_code==row['parent'])),
            'biketrek_exact_identity_required')
    return key


def effective_scope(item,family,requested=None):
    for record in (item,family):
        if record.get('status') in ('done','blocked'):
            raise CardConflict('operator_closed_or_blocked')
    scopes={r.get('scope') for r in (item,family) if r.get('scope') not in ('both','default',None)}
    require(len(scopes)<=1,'shop_scope_conflict')
    if requested not in (None,'both','default'):
        require(requested in SHOPS and (not scopes or requested in scopes),'shop_scope_changed')
        scopes.add(requested)
    return next(iter(scopes), 'both' if requested=='both' or any(
        r.get('scope')=='both' for r in (item,family)) else 'default')


async def _subjects(db,run,codes):
    found=(await db.execute(text("""
        SELECT i.detail,to_jsonb(w),to_jsonb(f) FROM atomer_progress_items i
        LEFT JOIN atomer_review_work w ON w.kind='item' AND w.key=i.code
        LEFT JOIN atomer_review_work f ON f.kind='family' AND f.key=i.family_key
        WHERE i.run_id=:run AND i.code=ANY(CAST(:codes AS text[]))
    """),dict(run=run,codes=codes))).all()
    return {d['code']:(d,w or work.EMPTY,f or work.EMPTY) for d,w,f in found}


async def existing_codes(db,run_id,limit=500,after=''):
    """Keyset cursor for already-present native targets; fresh eligibility is checked at execution."""
    await work._lock_current_run(db,run_id)
    return list(await db.scalars(text("""
        SELECT i.code FROM atomer_progress_items i
        LEFT JOIN atomer_review_work w ON w.kind='item' AND w.key=i.code
        LEFT JOIN atomer_review_work f ON f.kind='family' AND f.key=i.family_key
        WHERE i.run_id=:run AND i.code>:after AND i.state IN ('linked','needs_link','needs_card')
          AND coalesce(w.status,'open') NOT IN ('done','blocked')
          AND coalesce(f.status,'open') NOT IN ('done','blocked')
        ORDER BY i.code LIMIT :limit
    """),dict(run=run_id,after=after,limit=min(MAX_BATCH,max(1,limit)))))


def fail(reason):
    return dict(status='blocked',reason=str(reason),card_id=None,hub_sku=None,xtrek=None,biketrek=None)


def dto(identity,link_id=None):
    return dict(product_id=identity.external_product_id,variant_id=identity.external_id if identity.is_variant else None,
                code=identity.code,link_id=link_id)


async def _indices(db,shops):
    products=list(await db.scalars(select(Product)))
    identifiers=list(await db.scalars(select(ProductIdentifier)))
    mappings=list(await db.scalars(select(ShopProduct).where(ShopProduct.shop_id.in_([s.id for s in shops.values()]))))
    from inventory_hub.services.canonical_products import aliases_for
    redirects=await aliases_for(db,[row.id for row in products])
    indexes={s:IdentityIndex(shop.id,products,[m for m in mappings if m.shop_id==shop.id],identifiers,
                            canonical_aliases=redirects) for s,shop in shops.items()}
    aliases=defaultdict(set)
    local=IdentityIndex(0,products,(),identifiers,canonical_aliases=redirects)
    for code,owners in local.exact_internal_sku.items():
        for owner in owners:
            aliases[owner].add(code)
    return local,indexes,aliases


async def _read_source_collisions(db, run_id, codes, corrections=None):
    # Compare all source rows, even those outside this batch. One canonical card may
    # never be silently claimed by two source sellables in different worker batches.
    rows=await db.execute(text("SELECT code,detail->>'ean' FROM atomer_progress_items WHERE run_id=:run"),
                          {'run':run_id})
    by_code,by_gtin=defaultdict(set),defaultdict(set)
    for code,ean in rows:
        by_code[code.casefold()].add(code)
        for k in barcode_keys([ean,(corrections or {}).get(code,{}).get('ean')]):
            by_gtin[k].add(code)
    blocked=set()
    for hits in [*by_code.values(),*by_gtin.values()]:
        if len(hits)>1:
            blocked.update(hits.intersection(codes))
    return blocked


def _owner(resolution):
    require(resolution.status!='conflict',','.join(resolution.reasons))
    return resolution.product_id


def _plan(row,scope,catalogs,local,indexes,new_id,aliases):
    owner=_owner(local.resolve(RemoteIdentity(0,row['code'],barcodes=(row['ean'],))))
    if row.get('hub_id') is not None:
        require(row['hub_id'] in local.products and (owner is None or owner==row['hub_id']),
                'atomer_card_owner_changed')
        owner=row['hub_id']
    corrected_card=(row.get('barcode_evidence') or {}).get('required_card_id')
    if corrected_card is not None:
        require(type(corrected_card) is int and owner==corrected_card and
                not barcode_keys([row['ean']]).isdisjoint(barcode_keys(local.product_barcodes.get(owner,()))),
                'confirmed_corrected_card_gtin_required')
    keys={}
    x_identity=None
    if scope!='biketrek':
        key=choose_x(row,catalogs['xtrek'],new_id)
        keys['xtrek']=key
        _,x_identity,_=catalogs['xtrek'].get(key)
        x_owner=_owner(indexes['xtrek'].resolve(x_identity))
        require(not owner or not x_owner or x_owner==owner,'xtrek_card_owner_conflict')
        owner=owner or x_owner
    if scope!='xtrek':
        saved=indexes['biketrek'].by_product.get(owner,[])
        require(len(saved)<=1,'biketrek_multiple_card_links')
        card_codes=({local.products[owner].sku}|aliases.get(owner,set())) if owner else set()
        key=choose_bike(row,catalogs['biketrek'],x_identity,saved[0] if saved else None,
                        card_codes,local.product_barcodes.get(owner,()))
        if key:
            keys['biketrek']=key
            _,identity,_=catalogs['biketrek'].get(key)
            bike_owner=_owner(indexes['biketrek'].resolve(identity))
            require(not owner or not bike_owner or bike_owner==owner,'biketrek_card_owner_conflict')
            owner=owner or bike_owner
        elif scope=='biketrek':
            raise CardConflict('biketrek_native_target_required')
    require(keys,'native_target_required')
    if owner:
        p=local.products[owner]
        require(p.item_type=='stock' and p.is_active,'card_not_active_stock')
        require(not any(getattr(mapping,'shared_stock',False) for shop_code in keys
                        for mapping in indexes[shop_code].by_product.get(owner,[])),
                'shared_stock_sale_requires_group_workflow')
        if (row.get('barcode_evidence') or {}).get('required_atomer_id'):
            require(not barcode_keys([row['ean']]).isdisjoint(barcode_keys(local.product_barcodes.get(owner,()))),
                    'confirmed_corrected_card_gtin_required')
        if row['ean'] and local.product_barcodes.get(owner):
            incoming=barcode_keys([row['ean']])
            require(not incoming or not incoming.isdisjoint(barcode_keys(local.product_barcodes[owner])),
                    'atomer_card_ean_differs')
    proof=None
    if scope!='xtrek' and 'biketrek' not in keys:
        proof=dict(status='no_exact_catalog_match',
                   skus=sorted(raw_codes([row['code'],row.get('hub_sku'),x_identity.code if x_identity else None,*card_codes])),
                   gtins=sorted(barcode_keys([row.get('ean'),*(x_identity.barcodes if x_identity else ()),
                                              *local.product_barcodes.get(owner,())])),
                   indexed_leaves=len(catalogs['biketrek'].by_id))
    return {'scope':scope,'card':owner,'keys':keys,'biketrek_check':proof}


def _reject_collisions(plans,results):
    all_claims=defaultdict(set)
    for code,plan in plans.items():
        if plan['card']:
            all_claims[('card',plan['card'])].add(code)
        for shop,key in plan['keys'].items():
            all_claims[(shop,key)].add(code)
    for hits in all_claims.values():
        if len(hits)>1:
            for code in hits:
                results[code]=fail('one_physical_leaf_claimed_twice')
                plans.pop(code,None)


async def _write_links(db,shop,selected,cat,results,apply):
    """Use the same locked facts, conflicts and metadata as the existing-card linking workflow."""
    from inventory_hub.services import product_links as links, product_link_source as source
    facts=await links._facts(db,sorted(selected),lock=apply)
    choices={}
    for pid,(code,key) in selected.items():
        try:
            family,identity,obj=cat.get(key)
            remote=source._leaf(family['payload'],obj,variant=bool(identity.is_variant))
            choices[pid]=(remote,'atomer_exact',1)
        except (source.SourceError, CardConflict) as err:
            results[code]['status']='blocked'
            results[code]['reason']=getattr(err,'code',str(err))
    active={pid:facts[pid] for pid in choices if pid in facts}
    for pid in set(choices)-set(active):
        results[selected[pid][0]].update(status='blocked',reason='card_missing')
    if not active:
        return
    checked=await links._rows(db,shop,active,{pid:choices[pid] for pid in active})
    saved=[]
    for r in checked:
        pid=r['product_id']; code,key=selected[pid]
        receipt=results[code]
        if r['errors']:
            receipt.update(status='blocked',reason=', '.join(r['errors']))
            continue
        remote=r['remote']
        if apply and r['status']!='already_linked':
            # Incomplete pre-existing mapping may be completed after the guard in
            # _rows. Never reassign an existing mapping or change its previous identity.
            if r.get('unverified_mapping'):
                rec=await db.get(ShopProduct,r['unverified_mapping']['id'])
                rec.external_product_id=remote['parent_id']
                rec.external_variant_id=remote['external_id'] if remote['is_variant'] else None
            else:
                rec=ShopProduct(shop_id=shop.id,product_id=pid,external_product_id=remote['parent_id'],
                        external_variant_id=remote['external_id'] if remote['is_variant'] else None,
                        external_code=remote['code'],parent_code=remote['parent_code'] if remote['is_variant'] else None,
                        is_variant=remote['is_variant'],is_listed=True,push_pending=False,last_pull_at=links.now())
                db.add(rec)
            saved.append(remote)
        receipt[shop.code]={**dto(cat.get(key)[1]),'parent':remote['parent_code'] if remote['is_variant'] else None,
                            'shop_url':remote.get('shop_url'),'shop_admin_url':remote.get('shop_admin_url')}
    if saved:
        await db.flush()
        await links._cache_metadata(db,shop.id,saved)


async def reconcile(db,run_id,codes,*,apply,actor,native_ids=None,catalogs=None,include_managed=False,scopes=None,
                    barcode_corrections=None,zero_barcode_sentinels=None,confirmed_source_ids=None):
    """Return per-code link receipts. The caller owns commit, work claim and its durable receipt.

    native_ids binds newly created leaves to stable product/variant IDs. An existing
    leaf uses the native IDs in the published run. Both are verified by direct reads.
    """
    codes=list(codes)
    if not codes or len(codes)>MAX_BATCH or len(set(codes))!=len(codes):
        raise atomer_review_targets.ReviewError('invalid_card_batch',422)
    if not actor:
        raise atomer_review_targets.ReviewError('operator_required',403)
    native_ids=native_ids or {}; scopes=scopes or {}; corrections=barcode_corrections or {}
    zero_ids=zero_barcode_sentinels or {}
    source_ids=confirmed_source_ids or {}
    if (set(native_ids)|set(scopes)|set(corrections)|set(zero_ids)|set(source_ids))-set(codes) or any(not isinstance(v,dict) for v in corrections.values()):
        raise atomer_review_targets.ReviewError('invalid_card_batch',422)
    await work._lock_current_run(db,run_id)
    subjects=await _subjects(db,run_id,codes)
    results={}; usable={}; ids={s:set() for s in SHOPS}
    duplicates=await _read_source_collisions(db,run_id,set(codes),corrections)
    for code in codes:
        try:
            require(code in subjects,'source_subject_missing')
            require(code not in duplicates,'atomer_duplicate_physical_identity')
            row,item,family=subjects[code]
            row=barcode_subject(row,corrections.get(code),zero_ids.get(code),source_ids.get(code))
            subjects[code]=(row,item,family)
            scope=effective_scope(item,family,scopes.get(code))
            states={item['status'],family['status']}
            if include_managed:
                require('working' in states,'work_not_claimed')
            else:
                require(states=={'open'} and not item['revision'] and not family['revision'],'operator_manages_subject')
            require(not row['reasons'] or (code in native_ids and
                    set(row['reasons'])<={'occupied_parameters','ean_differs'}),'atomer_review_required')
            usable[code]=scope
        except CardConflict as err:
            results[code]=fail(err)
    if not usable:
        return results
    needed=tuple(s for s in SHOPS if any(scope not in SHOPS or scope==s for scope in usable.values()))
    base=catalogs if catalogs is not None else await load_catalogs(needed)
    shops={s.code:s for s in await db.scalars(select(Shop).where(Shop.code.in_(SHOPS),Shop.is_active.is_(True),
                                                Shop.platform=='upgates'))}
    if set(shops)!=set(SHOPS) or not set(needed)<=set(base):
        raise atomer_review_targets.ReviewError('shops_unavailable')
    await db.execute(text('SELECT pg_advisory_xact_lock(:key)'),{'key':IDENTITY_WRITE_LOCK})
    local,indexes,aliases=await _indices(db,shops)
    initial={s:Catalog.build(shops[s].id,base[s]) for s in needed}
    for code,scope in list(usable.items()):
        row=subjects[code][0]
        try:
            if scope!='biketrek':
                anchor=native_ids.get(code) or (row.get('product_id'),row.get('variant_id'))
                ids['xtrek'].add(native_key(anchor))
            if scope!='xtrek':
                source_owner=_owner(local.resolve(RemoteIdentity(0,code,barcodes=(row.get('ean'),))))
                owners={i for i in (row.get('hub_id'),source_owner) if i}
                witnesses={code,row.get('hub_sku'),row.get('target_code')}|{local.products[i].sku for i in owners if i in local.products}
                card_gtins=set()
                for owner in owners:
                    witnesses.update(aliases.get(owner,()))
                    card_gtins.update(local.product_barcodes.get(owner,()))
                candidates=initial['biketrek'].candidates(witnesses,[row.get('ean'),row.get('target_ean'),*card_gtins])
                for owner in owners:
                    for saved in indexes['biketrek'].by_product.get(owner,[]):
                        candidates.update(initial['biketrek'].candidates([saved.external_code],[]))
                        if saved.external_product_id and (not saved.is_variant or saved.external_variant_id):
                            ids['biketrek'].add(native_key((saved.external_product_id,saved.external_variant_id)))
                ids['biketrek'].update(candidates)
        except CardConflict as err:
            usable.pop(code,None);results[code]=fail(err)
    if not usable:
        return results
    fresh=await asyncio.gather(*(asyncio.to_thread(_read_candidates,s,initial[s],ids[s]) for s in needed))
    direct={s:parents for s,(parents,_) in zip(needed,fresh)}
    observed={s:keys for s,(_,keys) in zip(needed,fresh)}
    # A newly read xTrek barcode or an existing xTrek mapping may reveal an
    # old BIKETREK SKU that the pinned report did not yet know.
    x_direct=Catalog.build(shops['xtrek'].id,list(direct.get('xtrek',{}).values()))
    additional=set()
    for code,scope in usable.items():
        if scope in SHOPS:
            continue
        row=subjects[code][0]
        try:
            x_key=choose_x(row,x_direct,native_ids.get(code))
            _,identity,_=x_direct.get(x_key)
            owner=_owner(indexes['xtrek'].resolve(identity)) or row.get('hub_id')
            witness=raw_codes([code,identity.code,row.get('hub_sku')])
            eans=raw_codes([row.get('ean'),*identity.barcodes])
            if owner in local.products:
                witness.add(local.products[owner].sku)
                witness.update(aliases.get(owner,()))
                eans.update(local.product_barcodes.get(owner,()))
                for m in indexes['biketrek'].by_product.get(owner,()):
                    if m.external_product_id and (not m.is_variant or m.external_variant_id):
                        additional.add(native_key((m.external_product_id,m.external_variant_id)))
            additional.update(initial['biketrek'].candidates(witness,eans))
        except CardConflict:
            continue  # The complete index below records this leaf's precise reason.
    missing=additional-observed.get('biketrek',set())
    if missing:
        additions,keys=await asyncio.to_thread(_read_candidates,'biketrek',initial['biketrek'],missing)
        for pid,addition in additions.items():
            if pid in direct['biketrek']:
                previous={str(v['variant_id']):v for v in direct['biketrek'][pid]['variants']}
                addition['variants']=[previous[str(v['variant_id'])] if (pid,str(v['variant_id'])) in observed['biketrek']
                                      else v for v in addition['variants']]
            direct['biketrek'][pid]=addition
        observed['biketrek'].update(keys)
    current={}
    for s in needed:
        if isinstance(base[s],ObservedCatalog) and base[s].fingerprint!=target_fingerprint(s):
            raise atomer_review_targets.ReviewError('target_connection_changed')
        merged={str(p['product_id']):p for p in base[s]}
        merged.update(direct[s])
        current[s]=Catalog.build(shops[s].id,list(merged.values()))
    plans={}
    for code,scope in usable.items():
        row=subjects[code][0]
        try:
            plan=_plan(row,scope,current,local,indexes,native_ids.get(code),aliases)
            for s,key in plan['keys'].items():
                require(key in observed[s],'native_target_requires_fresh_read')
            plans[code]=plan
        except CardConflict as err:
            results[code]=fail(err)
    _reject_collisions(plans,results)
    for code,plan in plans.items():
        card=plan['card']
        results[code]=dict(status='ready',reason='',hub_sku=local.products[card].sku if card else code,
                           card_id=card,xtrek=None,biketrek=None)
        if card and apply:
            results[code]['card_action']='reused'
        if plan['biketrek_check']:
            results[code]['biketrek_check']=plan['biketrek_check']
            if isinstance(base['biketrek'],ObservedCatalog):
                results[code]['biketrek_check']['checked_at']=base['biketrek'].checked.isoformat()
        if 'barcode_evidence' in subjects[code][0]:
            results[code]['barcode_evidence']=subjects[code][0]['barcode_evidence']
        if not card:
            # Existing-card links are preflighted by _rows; a new card has no axes,
            # override or prior mapping. The ordinary import will recheck its family.
            for s,key in plan['keys'].items():
                results[code][s]=dto(current[s].get(key)[1])
    if apply:
        for s in SHOPS:
            new={code:p['keys'][s] for code,p in plans.items() if not p['card']
                 and (s=='xtrek' and 'xtrek' in p['keys'] or s=='biketrek' and 'xtrek' not in p['keys'])}
            if not new:
                continue
            tokens={}; canonical={}
            for code,key in new.items():
                f,identity,leaf=current[s].get(key)
                token=pull._leaf_selection(f,identity,leaf)
                tokens[identity.code]=token['token']
                canonical[identity.code]=code
            created_ids=set()
            report=await pull.register_observed_products(db,shops[s],list(direct[s].values()),selected_leaves=tokens,
                                                         canonical_skus=canonical,created_ids=created_ids)
            conflicts={c['code']:','.join(c['reasons']) for c in report['conflicts']}
            new_cards={p.sku:p for p in await db.scalars(select(Product).where(Product.sku.in_(list(new))))}
            for code,key in new.items():
                parent=current[s].get(key)[0]['code']
                if code not in new_cards or parent in conflicts:
                    results[code]=fail(conflicts.get(parent,'card_creation_unverified'))
                else:
                    card=new_cards[code];plans[code]['card']=card.id
                    results[code].update(card_id=card.id,hub_sku=card.sku,
                                         card_action='created' if card.id in created_ids else 'reused')
    for s in SHOPS:
        selected={p['card']:(code,p['keys'][s]) for code,p in plans.items() if p['card'] and s in p['keys']
                  and results[code]['status']!='blocked'}
        if selected:
            await _write_links(db,shops[s],selected,current[s],results,apply)
    if apply:
        await db.flush()
        pids={p['card'] for p in plans.values() if p['card']}
        saved={(m.shop_id,m.product_id):m for m in await db.scalars(select(ShopProduct).where(
            ShopProduct.shop_id.in_([s.id for s in shops.values()]),ShopProduct.product_id.in_(pids))
            .execution_options(populate_existing=True))}
        for code,p in plans.items():
            rec=results[code]
            was_blocked=rec['status']=='blocked'
            for s,key in p['keys'].items():
                m=saved.get((shops[s].id,p['card']))
                _,identity,_=current[s].get(key)
                if (not m or (m.external_product_id,m.external_variant_id)!=key or m.external_code!=identity.code
                    or m.is_variant!=identity.is_variant or m.parent_code!=identity.parent_code or not m.is_listed):
                    rec[s]=None
                    if not was_blocked:
                        rec.update(status='blocked',reason='card_mapping_readback_failed')
                    continue
                rec[s]={**(rec.get(s) or {}),**dto(identity,m.id)}
            if rec['status']!='blocked':
                rec['status']='verified'
    return results
