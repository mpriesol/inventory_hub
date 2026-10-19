"""Reconcile the owner's TOPEAK- → ZOO- administrative rename.

This service performs GETs only against shops. All mapped native IDs must
already exist under the new codes before one local identity is changed.
"""
from copy import deepcopy
from uuid import UUID
import re
from sqlalchemy import select, text, exists, or_, and_, func
from inventory_hub import config_io
from inventory_hub.db_models import Product, ProductIdentifier, IdentifierType, Shop, Supplier, SupplierProduct
from inventory_hub.db_models_ext import ShopProduct, ShopProductContent, ProductSupplySource
from inventory_hub.product_editor_models import ProductEditorOverride, ProductEditorSave, ProductEditorAudit
from inventory_hub.stock_sync_models import StockSyncItem
from inventory_hub.stock_publication_models import StockPublicationItem
from inventory_hub.services import product_editor as editor, product_publication as publication
from inventory_hub.services import product_publication_source as transport
from inventory_hub.services.product_identity import verified_barcodes
from inventory_hub.services.merchandising_write_guard import require_target_available
from inventory_hub.supplier_prefix import get_supplier_prefix

OLD, NEW = 'TOPEAK-', 'ZOO-'


def renamed(code):
    return NEW + code[len(OLD):] if code and code.startswith(OLD) else code


def configured(supplier):
    if supplier != 'zookee' or get_supplier_prefix(config_io.load_supplier(supplier, write_back_on_load=False)) != NEW:
        raise editor.EditorError('code_rename_configuration_invalid',422)


async def snapshot(db, product_id):
    product = await db.get(Product,product_id,populate_existing=True)
    if product is None:
        raise editor.EditorError('product_editor_product_missing',404)
    mappings = (await db.execute(select(ShopProduct,Shop.code).join(Shop,Shop.id == ShopProduct.shop_id)
        .where(ShopProduct.product_id == product_id).order_by(ShopProduct.id)
        .execution_options(populate_existing=True))).all()
    override = await db.get(ProductEditorOverride,product_id,populate_existing=True)
    return product,mappings,override


def signature(product,mappings,override):
    return editor._digest([product.id,product.sku,override.revision if override else 0,
        [[m.id,code,m.external_id,m.external_code,m.variant_code,m.parent_code,m.is_variant,m.is_listed] for m,code in mappings]])


async def preview(db,supplier):
    configured(supplier)
    products = list(await db.scalars(select(Product).where(or_(Product.sku.startswith(OLD), repair_filter(supplier))).order_by(Product.id)))
    return {'old_prefix':OLD,'new_prefix':NEW,'items':[{'product_id':p.id,'old_code':p.sku,'new_code':renamed(p.sku),'name':p.name} for p in products]}


def repair_filter(supplier):
    """Only existing, explicitly scoped Zookee BIKETREK links may repair IDs."""
    cfg = config_io.load_supplier(supplier, write_back_on_load=False)
    policy = (cfg.get('adapter_settings') or {}).get('existing_card_matching') or {}
    brands = sorted(set(policy.get('brands') or []) & {'SENSOR', 'TOPEAK'})
    if policy.get('enabled') is not True or policy.get('shop_code') != 'biketrek' or not brands:
        return False
    bindings = exists(select(ProductSupplySource.id).where(ProductSupplySource.product_id == Product.id,
        ProductSupplySource.supplier_product_id == SupplierProduct.id, ProductSupplySource.is_active.is_(True))
        .correlate(Product, SupplierProduct))
    any_binding = exists(select(ProductSupplySource.id).where(ProductSupplySource.product_id == Product.id).correlate(Product))
    linked = exists(select(SupplierProduct.id).join(Supplier, Supplier.id == SupplierProduct.supplier_id).where(
        Supplier.code == supplier, Supplier.is_active.is_(True), SupplierProduct.is_active.is_(True),
        SupplierProduct.is_discontinued.is_(False), func.upper(SupplierProduct.brand) == func.upper(Product.brand),
        or_(bindings, and_(~any_binding, Product.source_supplier_product_id == SupplierProduct.id))).correlate(Product))
    missing = exists(select(ShopProduct.id).join(Shop, Shop.id == ShopProduct.shop_id).where(
        ShopProduct.product_id == Product.id, Shop.code == 'biketrek', Shop.is_active.is_(True),
        ShopProduct.is_listed.is_(True), or_(ShopProduct.external_id.is_(None), ShopProduct.external_id == '')).correlate(Product))
    return and_(func.upper(Product.brand).in_(brands), missing, linked)


async def can_repair(db, supplier, product_id):
    return bool(await db.scalar(select(Product.id).where(Product.id == product_id, repair_filter(supplier))))


async def barcode_set(db,product_id,override):
    values=set(await db.scalars(select(ProductIdentifier.value).where(ProductIdentifier.product_id == product_id,
        ProductIdentifier.identifier_type.in_((IdentifierType.ean,IdentifierType.upc)))))
    if override and isinstance((override.data.get('variant') or {}).get('eans'),list):
        values=set(override.data['variant']['eans'])
    return values


def native_id(value):
    value = str(value)
    return value if re.fullmatch(r'[1-9][0-9]{0,19}', value) else None


async def cached_identity(db, mapping, old, new, *, lock=False):
    """Recover missing legacy IDs only from the exact original cached leaf.

    Project identities in SQL: a POS parent can contain thousands of siblings.
    Neither a matching EAN nor the freshly renamed remote code supplies an ID.
    """
    parent = mapping.parent_code or mapping.external_code
    rows = (await db.execute(text("""
        SELECT c.id, c.external_code, c.data->>'code' AS parent_code,
          c.data->>'product_id' AS parent_id, c.data->'set_yn' AS is_set,
          c.data->'variants_exists_yn' AS has_variants,
          jsonb_array_length(CASE WHEN jsonb_typeof(c.data->'variants')='array'
            THEN c.data->'variants' ELSE '[]'::jsonb END) AS variant_count,
          CASE WHEN :variant THEN jsonb_array_length(v.matches) ELSE 1 END AS leaf_count,
          leaf.value->>'code' AS leaf_code, leaf.value->>'product_id' AS leaf_parent_id,
          leaf.value->>'variant_id' AS variant_id, leaf.value->>'ean' AS ean
        FROM shop_product_content c
        CROSS JOIN LATERAL (SELECT jsonb_path_query_array(c.data,
          '$.variants[*] ? (@.code == $old || @.code == $new)',
          jsonb_build_object('old', CAST(:old AS text), 'new', CAST(:new AS text))) AS matches) v
        CROSS JOIN LATERAL (SELECT CASE WHEN :variant THEN v.matches->0 ELSE c.data END AS value) leaf
        WHERE c.shop_id=:shop AND c.external_code IN (:parent, :new_parent)
        """ + (' FOR SHARE OF c' if lock else '')), {'shop': mapping.shop_id, 'parent': parent, 'new_parent': renamed(parent),
                 'old': old, 'new': new, 'variant': mapping.is_variant})).mappings().all()
    if len(rows) != 1:
        raise editor.EditorError('code_rename_cache_identity_missing' if not rows else 'code_rename_cache_conflict')
    row = rows[0]
    if (row['parent_code'] != row['external_code'] or not native_id(row['parent_id'])
            or row['leaf_count'] != 1):
        raise editor.EditorError('code_rename_cache_identity_missing')
    if (row['leaf_code'] not in (old, new) or row['is_set'] is True
            or (not mapping.is_variant and (row['variant_count'] or row['has_variants'] is True))
            or (row['leaf_parent_id'] is not None and row['leaf_parent_id'] != row['parent_id'])):
        raise editor.EditorError('code_rename_cache_conflict')
    recovered = native_id(row['variant_id']) if mapping.is_variant else row['parent_id']
    if not recovered:
        raise editor.EditorError('code_rename_cache_identity_missing')
    return {'cache_id': row['id'], 'parent_id': row['parent_id'], 'native_id': recovered,
            'eans': sorted(verified_barcodes((row['ean'],)))}


async def reconcile_one(db,supplier,product_id,request_id,*,remote_reader=None):
    configured(supplier)
    identifier=str(UUID(str(request_id)))
    digest=editor._digest(['zookee-code-reconciliation',product_id])
    previous=await db.get(ProductEditorSave,identifier)
    if previous:
        if previous.input_hash != digest: raise editor.EditorError('product_editor_request_reused')
        return previous.result
    product,mappings,override=await snapshot(db,product_id)
    old=product.sku; new=renamed(old)
    if old == new and not await can_repair(db, supplier, product_id):
        raise editor.EditorError('code_rename_no_legacy_code')
    if not mappings: raise editor.EditorError('code_rename_mapping_missing')
    frozen=signature(product,mappings,override)
    eans=await barcode_set(db,product_id,override)
    observed=[]
    for mapping,shop in mappings:
        code=mapping.variant_code if mapping.is_variant else mapping.external_code
        if code not in (old,new): raise editor.EditorError('code_rename_mapping_changed')
        cached = None
        if mapping.external_id is None or mapping.external_id == '':
            cached = await cached_identity(db, mapping, old, new)
            if eans and cached['eans'] and not eans.intersection(cached['eans']):
                raise editor.EditorError('code_rename_barcode_conflict')
        elif not native_id(mapping.external_id):
            raise editor.EditorError('code_rename_native_id_missing')
        parent=mapping.parent_code or mapping.external_code
        parent=(renamed(parent) if old != new else parent) if mapping.is_variant else new
        target={'code':new,'variant_code':new if mapping.is_variant else None,'parent_code':parent}
        observed.append({'shop':shop,'mapping_id':mapping.id,'target':target,
                         'native_id':cached['native_id'] if cached else str(mapping.external_id), 'cached_identity': cached,
                         'fingerprint':publication.fingerprint(shop),'shop_id':mapping.shop_id})
    await db.rollback()
    # Every channel must already be renamed. A partial administrative rename
    # produces no local write and cannot silently sever another shop's link.
    for item in observed:
        remote=await (remote_reader or transport.read_remote)(item['shop'],item['target'],item['fingerprint'])
        identity=remote['identity']; native=identity['variant_id'] if identity['variant_code'] else identity['product_id']
        if str(native) != item['native_id']: raise editor.EditorError('code_rename_native_id_changed')
        if item['cached_identity'] and str(identity['product_id']) != item['cached_identity']['parent_id']:
            raise editor.EditorError('code_rename_native_id_changed')
        remote_eans=set(verified_barcodes((remote['leaf'].get('ean'),)))
        if eans and remote_eans and not eans & remote_eans: raise editor.EditorError('code_rename_barcode_conflict')
        cached_eans = set((item['cached_identity'] or {}).get('eans', []))
        if cached_eans and remote_eans and not cached_eans.intersection(remote_eans):
            raise editor.EditorError('code_rename_barcode_conflict')
        item['verified_identity']=identity
    await db.execute(text('SELECT pg_advisory_xact_lock(:key)'),{'key':editor.IDENTITY_WRITE_LOCK})
    await db.execute(text('SELECT pg_advisory_xact_lock(:key)'),{'key':int(editor._digest(identifier)[:15],16)})
    previous=await db.get(ProductEditorSave,identifier,populate_existing=True)
    if previous:
        if previous.input_hash != digest: raise editor.EditorError('product_editor_request_reused')
        return previous.result
    product,mappings,override=await snapshot(db,product_id)
    if signature(product,mappings,override) != frozen or await barcode_set(db,product_id,override) != eans:
        raise editor.EditorError('product_editor_changed')
    if old == new and not await can_repair(db, supplier, product_id):
        raise editor.EditorError('product_editor_changed')
    for mapping, _ in mappings:
        evidence = next(item for item in observed if item['mapping_id'] == mapping.id)
        if publication.fingerprint(evidence['shop']) != evidence['fingerprint']:
            raise editor.EditorError('product_publication_target_changed')
        if evidence['cached_identity'] and await cached_identity(db, mapping, old, new, lock=True) != evidence['cached_identity']:
            raise editor.EditorError('product_editor_changed')
        if await db.scalar(select(ShopProduct.id).where(ShopProduct.shop_id == mapping.shop_id,
                ShopProduct.product_id != product_id, ShopProduct.is_variant == mapping.is_variant,
                ShopProduct.external_id == evidence['native_id']).limit(1)):
            raise editor.EditorError('product_editor_identity_conflict')
    if (await db.scalar(select(Product.id).where(Product.sku == new,Product.id != product_id))
        or await db.scalar(select(ProductIdentifier.id).where(ProductIdentifier.value.in_((old,new)),ProductIdentifier.product_id != product_id))):
        raise editor.EditorError('product_editor_identity_conflict')
    if (await db.scalar(select(StockSyncItem.id).where(StockSyncItem.sku.in_((old,new)),StockSyncItem.status.in_(('sending','uncertain'))).limit(1))
        or await db.scalar(select(StockPublicationItem.id).where(StockPublicationItem.sku.in_((old,new)),StockPublicationItem.status.in_(('sending','uncertain'))).limit(1))):
        raise editor.EditorError('code_rename_publication_inflight')
    for mapping,shop in mappings:
        await require_target_available(db,shop,mapping.parent_code or mapping.external_code)
        await require_target_available(db,shop,renamed(mapping.parent_code or mapping.external_code))
    product.sku=new
    if old != new and not await db.scalar(select(ProductIdentifier.id).where(ProductIdentifier.product_id == product_id,
        ProductIdentifier.identifier_type == IdentifierType.internal_sku,ProductIdentifier.value == old)):
        db.add(ProductIdentifier(product_id=product_id,identifier_type=IdentifierType.internal_sku,value=old,is_primary=False,
                                notes='Previous canonical code; verified Zookee administrative rename'))
    for mapping,shop in mappings:
        mapping.external_id = next(item['native_id'] for item in observed if item['mapping_id'] == mapping.id)
        if old == new:
            continue
        old_parent=mapping.parent_code or mapping.external_code
        new_parent=renamed(old_parent)
        if mapping.is_variant:
            mapping.variant_code=new
            mapping.parent_code=new_parent
            mapping.external_code=new if mapping.external_code == old else renamed(mapping.external_code)
        else: mapping.external_code=new
        # Preserve the cached source fields and its original timestamp. Only
        # identity fields proven by native-ID GETs are reconciled.
        caches=list(await db.scalars(select(ShopProductContent).where(ShopProductContent.shop_id == mapping.shop_id,
            ShopProductContent.external_code.in_({old_parent,new_parent})).with_for_update()))
        if len(caches)>1: raise editor.EditorError('code_rename_cache_conflict')
        cache=caches[0] if caches else None
        if cache:
            existing=await db.scalar(select(ShopProductContent.id).where(ShopProductContent.shop_id == mapping.shop_id,
                ShopProductContent.external_code == new_parent,ShopProductContent.id != cache.id))
            if existing: raise editor.EditorError('code_rename_cache_conflict')
            data=deepcopy(cache.data)
            if mapping.is_variant:
                for leaf in data.get('variants',[]):
                    if str(leaf.get('variant_id')) == str(mapping.external_id) and leaf.get('code') == old:
                        leaf['code']=new
            if data.get('code') == old_parent: data['code']=new_parent
            cache.external_code=new_parent;cache.data=data
    revision=(override.revision if override else 0)+1
    if override is None: db.add(ProductEditorOverride(product_id=product_id,revision=revision,data={}))
    else: override.revision=revision;override.updated_at=editor.now()
    result={'request_id':identifier,'product_id':product_id,'old_code':old,'new_code':new,'status':'completed','external_write_enabled':False,
            'verified_shops':[item['shop'] for item in observed]}
    db.add(ProductEditorSave(request_id=identifier,input_hash=digest,result=result));await db.flush()
    db.add(ProductEditorAudit(request_id=identifier,product_id=product_id,revision=revision,
        before_data={'sku':old},after_data={'sku':new,'operation':'verified_code_reconciliation' if old != new else 'verified_native_id_repair','native_identities':observed}))
    await db.commit()
    return result


async def reconcile_batch(db, supplier, rows):
    """Batch identity reads through the existing bounded shop-link reader."""
    from inventory_hub.services import product_link_source as reader
    configured(supplier)
    pending, results, codes = [], [], {}
    for row in rows:
        identifier = str(row.request_id)
        previous = await db.get(ProductEditorSave, identifier)
        if previous:
            if previous.input_hash != editor._digest(['zookee-code-reconciliation', row.product_id]):
                raise editor.EditorError('product_editor_request_reused')
            results.append(previous.result)
            continue
        product, mappings, _ = await snapshot(db, row.product_id)
        new = renamed(product.sku)
        if new == product.sku and not await can_repair(db, supplier, row.product_id):
            results.append({'product_id': row.product_id, 'status': 'failed', 'error': 'code_rename_no_legacy_code'})
            continue
        try:
            if not mappings:
                raise editor.EditorError('code_rename_mapping_missing')
            for mapping, _ in mappings:
                if mapping.external_id in (None, ''):
                    await cached_identity(db, mapping, product.sku, new)
                elif not native_id(mapping.external_id):
                    raise editor.EditorError('code_rename_native_id_missing')
        except editor.EditorError as error:
            results.append({'product_id': row.product_id, 'status': 'failed', 'error': error.code})
            continue
        pending.append(row)
        for _, shop in mappings:
            codes.setdefault(shop, set()).add(new)
    await db.rollback()
    observed, failures, fingerprints = {}, {}, {}
    for shop, values in codes.items():
        try:
            fingerprint = publication.fingerprint(shop)
            fingerprints[shop] = fingerprint
            response = await reader.discover(shop, sorted(values), fingerprint)
            observed[shop] = {leaf['code']: leaf for leaf in response['leaves']}
        except (reader.SourceError, editor.EditorError) as error:
            failures[shop] = error

    async def from_batch(shop, target, fingerprint):
        if fingerprints.get(shop) != fingerprint:
            raise editor.EditorError('product_publication_target_changed')
        if shop in failures:
            raise editor.EditorError(failures[shop].code, failures[shop].status)
        leaf = observed.get(shop, {}).get(target['code'])
        if (not leaf or leaf['parent_code'] != target['parent_code']
                or bool(leaf['is_variant']) != bool(target['variant_code'])):
            raise editor.EditorError('code_rename_target_not_verified')
        identity = {**target, 'product_id': int(leaf['parent_id']),
                    'variant_id': int(leaf['external_id']) if leaf['is_variant'] else None}
        return {'identity': identity, 'options': None, 'leaf': {'ean': ';'.join(leaf['eans'])}}

    for row in pending:
        try:
            result = await reconcile_one(db, supplier, row.product_id, row.request_id, remote_reader=from_batch)
        except (editor.EditorError, transport.source.SourceError, publication.CatalogError) as error:
            await db.rollback()
            result = {'product_id': row.product_id, 'status': 'failed', 'error': error.code}
        results.append(result)
    return {'results': results}
