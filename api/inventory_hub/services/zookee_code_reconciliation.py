"""Reconcile the owner's TOPEAK- → ZOO- administrative rename.

This service performs GETs only against shops. All mapped native IDs must
already exist under the new codes before one local identity is changed.
"""
from copy import deepcopy
from uuid import UUID
from sqlalchemy import select, text
from inventory_hub import config_io
from inventory_hub.db_models import Product, ProductIdentifier, IdentifierType, Shop
from inventory_hub.db_models_ext import ShopProduct, ShopProductContent
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
        .where(ShopProduct.product_id == product_id).order_by(ShopProduct.id))).all()
    override = await db.get(ProductEditorOverride,product_id,populate_existing=True)
    return product,mappings,override


def signature(product,mappings,override):
    return editor._digest([product.id,product.sku,override.revision if override else 0,
        [[m.id,code,m.external_id,m.external_code,m.variant_code,m.parent_code,m.is_variant,m.is_listed] for m,code in mappings]])


async def preview(db,supplier):
    configured(supplier)
    products = list(await db.scalars(select(Product).where(Product.sku.startswith(OLD)).order_by(Product.id)))
    return {'old_prefix':OLD,'new_prefix':NEW,'items':[{'product_id':p.id,'old_code':p.sku,'new_code':renamed(p.sku),'name':p.name} for p in products]}


async def barcode_set(db,product_id,override):
    values=set(await db.scalars(select(ProductIdentifier.value).where(ProductIdentifier.product_id == product_id,
        ProductIdentifier.identifier_type.in_((IdentifierType.ean,IdentifierType.upc)))))
    if override and isinstance((override.data.get('variant') or {}).get('eans'),list):
        values=set(override.data['variant']['eans'])
    return values


async def reconcile_one(db,supplier,product_id,request_id):
    configured(supplier)
    identifier=str(UUID(str(request_id)))
    digest=editor._digest(['zookee-code-reconciliation',product_id])
    previous=await db.get(ProductEditorSave,identifier)
    if previous:
        if previous.input_hash != digest: raise editor.EditorError('product_editor_request_reused')
        return previous.result
    product,mappings,override=await snapshot(db,product_id)
    old=product.sku; new=renamed(old)
    if old == new: raise editor.EditorError('code_rename_no_legacy_code')
    if not mappings: raise editor.EditorError('code_rename_mapping_missing')
    frozen=signature(product,mappings,override)
    observed=[]
    for mapping,shop in mappings:
        if not mapping.external_id or not str(mapping.external_id).isdigit():
            raise editor.EditorError('code_rename_native_id_missing')
        code=mapping.variant_code if mapping.is_variant else mapping.external_code
        if code not in (old,new): raise editor.EditorError('code_rename_mapping_changed')
        parent=renamed(mapping.parent_code or mapping.external_code) if mapping.is_variant else new
        target={'code':new,'variant_code':new if mapping.is_variant else None,'parent_code':parent}
        observed.append({'shop':shop,'mapping_id':mapping.id,'target':target,'native_id':str(mapping.external_id),
                         'fingerprint':publication.fingerprint(shop),'shop_id':mapping.shop_id})
    eans=await barcode_set(db,product_id,override)
    await db.rollback()
    # Every channel must already be renamed. A partial administrative rename
    # produces no local write and cannot silently sever another shop's link.
    for item in observed:
        remote=await transport.read_remote(item['shop'],item['target'],item['fingerprint'])
        identity=remote['identity']; native=identity['variant_id'] if identity['variant_code'] else identity['product_id']
        if str(native) != item['native_id']: raise editor.EditorError('code_rename_native_id_changed')
        remote_eans=set(verified_barcodes((remote['leaf'].get('ean'),)))
        if eans and remote_eans and not eans & remote_eans: raise editor.EditorError('code_rename_barcode_conflict')
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
    if not await db.scalar(select(ProductIdentifier.id).where(ProductIdentifier.product_id == product_id,
        ProductIdentifier.identifier_type == IdentifierType.internal_sku,ProductIdentifier.value == old)):
        db.add(ProductIdentifier(product_id=product_id,identifier_type=IdentifierType.internal_sku,value=old,is_primary=False,
                                notes='Previous canonical code; verified Zookee administrative rename'))
    for mapping,shop in mappings:
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
        before_data={'sku':old},after_data={'sku':new,'operation':'verified_code_reconciliation','native_identities':observed}))
    await db.commit()
    return result
