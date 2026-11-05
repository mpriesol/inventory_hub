"""Apply an operator-verified manufacturer recode to an empty, exactly mapped xTrek leaf."""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl
from sqlalchemy import select, func, text
from inventory_hub.db_models import Product, ProductIdentifier, Shop
from inventory_hub.db_models_ext import ShopProduct, StockBalance
from inventory_hub.product_editor_models import ProductEditorOverride
from inventory_hub.services.atomer_review_targets import ReviewError
from inventory_hub.services.identifiers import ProductIdentifierService
from inventory_hub.services.product_identity import barcode_keys, verified_barcodes, IDENTITY_WRITE_LOCK
from inventory_hub.services import stock_publication_source as transport

Code = Annotated[str,Field(min_length=1,max_length=255)]


class BarcodeRow(BaseModel):
    model_config=ConfigDict(extra='forbid')
    code:Code
    revision:int=Field(ge=0)
    before:dict[Code,Code]
    old_code:Code
    old_ean:str=Field(max_length=255)
    product_id:int|None=Field(default=None,gt=0)
    variant_id:int|None=Field(default=None,gt=0)
    new_ean:Code
    card:int=Field(gt=0)
    manufacturer_or_supplier:HttpUrl
    evidence:str=Field(min_length=20,max_length=1200)
    decision:Literal['same_item_supplier_recode','fill_existing_card_barcode']
    names:dict[Annotated[str, Field(pattern=r"^PARAM_[0-9]+$")],Code]|None=Field(default=None,max_length=30)


class BarcodeFamily(BaseModel):
    model_config=ConfigDict(extra='forbid')
    run_id:int=Field(gt=0)
    source_sha:str=Field(pattern=r'^[0-9a-f]{64}$')
    rows:list[BarcodeRow]=Field(min_length=1,max_length=100)


async def eligible_card(db,entry,*,record=False):
    await db.execute(text('SELECT pg_advisory_xact_lock(:id)'),{'id':IDENTITY_WRITE_LOCK})
    card=entry['card']
    owner=(await db.execute(select(ShopProduct,Product).join(Shop,Shop.id==ShopProduct.shop_id)
        .join(Product,Product.id==ShopProduct.product_id).where(Shop.code=='xtrek',
        ShopProduct.external_variant_id==entry['vid']))).one_or_none()
    if not owner or owner[1].id!=card or owner[0].external_code!=entry['target'] or owner[0].parent_code!=entry['parent'] or owner[0].external_product_id!=entry['pid'] or owner[0].push_pending:
        raise ReviewError('recode_mapping_changed')
    if owner[1].sku!=entry['target'] or await db.scalar(select(Product.id).where(
            func.lower(Product.sku)==entry['code'].casefold(),Product.id!=card).limit(1)):
        raise ReviewError('recode_card_conflict')
    balance=await db.scalar(select(StockBalance.id).where(StockBalance.product_id==card,
        (StockBalance.qty_on_hand!=0)|(StockBalance.qty_reserved!=0)|(StockBalance.qty_quarantined!=0)).limit(1))
    if balance and entry['ean']:
        raise ReviewError('recode_stock_present')
    override=await db.get(ProductEditorOverride,card)
    if override and 'eans' in (override.data.get('variant') or {}):
        raise ReviewError('recode_barcode_override')
    old,new=entry['ean'],entry['new_ean']
    # Validate all UPC/EAN representations, retaining the original code as a scannable identifier.
    found=(await db.execute(select(ProductIdentifier.product_id, ProductIdentifier.value).where(
        ProductIdentifier.identifier_type.in_(ProductIdentifierService.BARCODE_TYPES),
        func.ltrim(ProductIdentifier.value,'0').in_([old.lstrip('0'),new.lstrip('0')])))).all()
    required = old or new
    if any(pid!=card for pid,_ in found) or not any(barcode_keys([v])==barcode_keys([required]) for _,v in found):
        raise ReviewError('recode_barcode_owner')
    if record and not any(barcode_keys([v])==barcode_keys([new]) for _,v in found):
        await ProductIdentifierService(db).add_identifier(card,new,identifier_type=ProductIdentifierService.classify_barcode(new),
            is_primary=False,notes='Overený nástupnícky kód dodávateľa, migrácia Atomer')


async def entry_for(db,request,row,source):
    values = {}
    if request.names is not None:
        if (request.decision!='fill_existing_card_barcode' or not source or source['conflict']
                or source['parent']!=row['parent'] or set(request.names)!=set(source['axes'])
                or len(set(request.names.values()))!=len(request.names) or any(not v or len(v)>255 for v in source['axes'].values())):
            raise ReviewError('variant_source_changed')
        values = {request.names[k]:v for k,v in source['axes'].items()}
    if request.decision=='fill_existing_card_barcode':
        if (row['reasons']!=['different_shop_link'] or row['hub_id']!=request.card or row['target_code']
                or request.old_ean or request.new_ean!=row['ean'] or not request.product_id or not request.variant_id
                or not row['parent'] or request.new_ean not in verified_barcodes([request.new_ean])
                or request.manufacturer_or_supplier.scheme!='https'):
            raise ReviewError('recode_evidence_changed')
        entry=dict(code=row['code'],parent=row['parent'],pid=str(request.product_id),vid=str(request.variant_id),
                   target=request.old_code,ean='',new_ean=request.new_ean,card=request.card,revision=request.revision,
                   before=request.before,values=values,proof=str(request.manufacturer_or_supplier),evidence=request.evidence)
        await eligible_card(db,entry)
        return entry
    if (row['reasons']!=['ean_differs','occupied_parameters'] or not source or source['conflict'] or not row['variant_id']
            or source['parent']!=row['parent'] or source['ean']!=request.new_ean or request.new_ean!=row['ean']
            or request.old_code!=row['target_code'] or request.old_ean!=row['target_ean']
            or request.old_ean not in verified_barcodes([request.old_ean]) or request.new_ean not in verified_barcodes([request.new_ean])
            or barcode_keys([request.old_ean])==barcode_keys([request.new_ean]) or row['hub_id'] not in (None,request.card)
            or request.manufacturer_or_supplier.scheme!='https'):
        raise ReviewError('recode_evidence_changed')
    entry=dict(code=row['code'],parent=row['target_parent'],pid=row['product_id'],vid=row['variant_id'],
               target=request.old_code,ean=request.old_ean,new_ean=request.new_ean,card=request.card,
               revision=request.revision,before=request.before,values={},proof=str(request.manufacturer_or_supplier),
               evidence=request.evidence)
    await eligible_card(db,entry)
    return entry


def protect_expected(protected, entries):
    out=deepcopy(protected)
    for item in entries:
        if 'new_ean' in item:
            observations=out['leaves'][item['vid']]
            try:
                quantities=[Decimal(str(r['stock'])) for r in observations if 'stock' in r]
            except (InvalidOperation,TypeError,ValueError):
                raise ReviewError('recode_stock_or_ean_changed') from None
            if item['ean'] and (not quantities or any(not q.is_finite() or q!=0 for q in quantities)):
                raise ReviewError('recode_stock_or_ean_changed')
            for observation in observations:
                if (observation.get('ean') or '')!=item['ean']:
                    raise ReviewError('recode_stock_or_ean_changed')
                observation['ean']=item['new_ean']
    return out


def check_changed_catalog(fingerprint, since, entries):
    """The frozen run already excludes another owner. Re-read products changed since its start."""
    expected={next(iter(barcode_keys([e['new_ean']]))):(e['pid'],e['vid']) for e in entries}
    if len(expected)!=len(entries):
        raise ReviewError('recode_barcode_owner')
    client=transport._client('xtrek',fingerprint)
    try:
        total=None
        for page in range(1,51):
            data=transport._request(client,'get','products',params={'last_update_time_from':since.isoformat(),
                'page':page,'current_page_items':100})
            products=data.get('products')
            if (not isinstance(products,list) or data.get('current_page')!=page or len(products)!=data.get('current_page_items')
                    or not transport._messages_ok(data.get('messages')) or type(data.get('number_of_items'))!=int
                    or type(data.get('number_of_pages'))!=int or data['number_of_pages']>50
                    or total is not None and total!=data['number_of_items']):
                raise ReviewError('recode_catalog_incomplete')
            total=data['number_of_items']
            for parent in products:
                for leaf in parent.get('variants') or [parent]:
                    for gtin in barcode_keys([leaf.get('ean')]):
                        if gtin in expected and expected[gtin]!=(str(parent.get('product_id')),str(leaf.get('variant_id'))):
                            raise ReviewError('recode_barcode_owner')
            if page>=data['number_of_pages']:
                if (page-1)*100+len(products)!=total:
                    raise ReviewError('recode_catalog_incomplete')
                return
        raise ReviewError('recode_catalog_incomplete')
    finally:
        client.session.close()
