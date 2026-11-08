"""Opt-in, read-only supplier proof for a keeper without a registered barcode.

The original barcode remains registered to the selected former card. This
module never adds, transfers or deletes an identifier: the existing canonical
alias exposes that preserved evidence only after consolidation succeeds.
"""
from copy import deepcopy
from datetime import datetime
import hashlib
from typing import Literal

from pydantic import Field, StrictBool, StrictInt, StrictStr, ValidationError, field_validator
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from inventory_hub.db_models import Supplier, SupplierProduct
from inventory_hub.db_models_ext import ProductSupplySource
from inventory_hub.product_link_types import LinkInput
from inventory_hub.services import catalog, product_links as links
from inventory_hub.services.identifiers import ProductIdentifierService
from inventory_hub.services.product_identity import barcode_keys, verified_barcodes


FLAG = 'use_keeper_supplier_gtin'
LOCAL_KEY = 'keeper_supplier_identity'
PROOF_KEY = 'supplier_gtin_proof'
INPUT_TABLES = 'suppliers, supplier_products, product_supply_sources, product_identifiers'


class AxisValue(LinkInput):
    name: StrictStr = Field(min_length=1, max_length=255)
    value: StrictStr = Field(min_length=1, max_length=500)


class SupplierIdentity(LinkInput):
    keeper_product_id: StrictInt = Field(gt=0)
    supplier_id: StrictInt = Field(gt=0)
    supplier_code: StrictStr = Field(min_length=1, max_length=50)
    supplier_product_id: StrictInt = Field(gt=0)
    supply_source_id: StrictInt = Field(gt=0)
    supplier_alias_id: StrictInt = Field(gt=0)
    supplier_sku: StrictStr = Field(min_length=1, max_length=100)
    manufacturer_sku: StrictStr = Field(min_length=1, max_length=100)
    shop_sku: StrictStr = Field(min_length=1, max_length=100)
    brand: StrictStr = Field(min_length=1, max_length=100)
    eans: list[StrictStr] = Field(min_length=1, max_length=20)
    supplier_active: StrictBool
    source_active: StrictBool
    source_discontinued: StrictBool
    link_active: StrictBool
    orderable: StrictBool
    pack_size: StrictInt = Field(ge=1, le=1)
    source_attributes: list[AxisValue] = Field(max_length=20)
    source_axes: dict[StrictStr, StrictStr] = Field(max_length=20)
    last_seen_at: StrictStr
    source_hash: StrictStr = Field(pattern=r'^[a-f0-9]{64}$')
    supplier_hash: StrictStr = Field(pattern=r'^[a-f0-9]{64}$')
    relation_hash: StrictStr = Field(pattern=r'^[a-f0-9]{64}$')
    alias_hash: StrictStr = Field(pattern=r'^[a-f0-9]{64}$')

    @field_validator('last_seen_at')
    @classmethod
    def timestamp(cls, value):
        if datetime.fromisoformat(value).tzinfo is None:
            raise ValueError('Supplier observation must have a timezone')
        return value


class RegisteredBarcode(LinkInput):
    identifier_id: StrictInt = Field(gt=0)
    product_id: StrictInt = Field(gt=0)
    identifier_type: StrictStr = Field(pattern=r'^(ean|upc)$')
    value: StrictStr = Field(min_length=8, max_length=14)


class NativeIdentity(LinkInput):
    shop_code: StrictStr = Field(pattern=r'^[a-z0-9][a-z0-9_-]{0,49}$')
    mapping_id: StrictInt | None = Field(default=None, gt=0)
    original_product_id: StrictInt | None = Field(default=None, gt=0)
    code: StrictStr = Field(min_length=1, max_length=100)
    parent_code: StrictStr = Field(min_length=1, max_length=100)
    is_variant: StrictBool
    native_product_id: StrictInt = Field(gt=0)
    native_variant_id: StrictInt | None = Field(default=None, gt=0)
    supplier_code: StrictStr | None = Field(default=None, min_length=1, max_length=100)
    eans: list[StrictStr] = Field(max_length=20)
    attributes: list[AxisValue] = Field(min_length=1, max_length=20)
    axes: dict[StrictStr, StrictStr] = Field(min_length=1, max_length=20)


class LocalAxes(LinkInput):
    product_id: StrictInt = Field(gt=0)
    attributes: list[AxisValue] = Field(max_length=20)
    axes: dict[StrictStr, StrictStr] = Field(max_length=20)


class SupplierGtinProof(LinkInput):
    axis_rule: Literal['exact_except_size_xxxl_3xl']
    source: SupplierIdentity
    verified_gtin_key: StrictStr = Field(pattern=r'^[0-9]{14}$')
    registered_identifiers: list[RegisteredBarcode] = Field(min_length=1, max_length=200)
    native_leaves: list[NativeIdentity] = Field(min_length=2, max_length=70)
    local_axes: list[LocalAxes] = Field(min_length=2, max_length=10)
    physical_axes: dict[StrictStr, StrictStr] = Field(min_length=1, max_length=20)


def need(condition, code='consolidation_supplier_identity_unverified'):
    if not condition:
        raise links.LinkError(code)


def typed(model, value):
    try:
        return model.model_validate(value).model_dump(mode='json')
    except (ValidationError, ValueError, TypeError):
        raise links.LinkError('consolidation_supplier_identity_unverified') from None


def physical_axes(values):
    """Only this fully corroborated branch accepts the named apparel size pair.

    Keep raw attributes/axes in its proof; never change the shared normalizer,
    colors, other sizes or any stored supplier/local/native value.
    """
    result = links.axes(values)
    if result.get('size') == 'xxxl':
        result['size'] = '3xl'
    return result


async def lock_inputs(db):
    # After the normal identity/Product lock order, before rereading source
    # facts. NOWAIT avoids a feed writer holding DML while waiting for Product.
    try:
        await db.execute(text('LOCK TABLE ' + INPUT_TABLES + ' IN SHARE MODE NOWAIT'))
    except DBAPIError as error:
        if getattr(error.orig, 'sqlstate', None) == '55P03':
            raise links.LinkError('consolidation_supplier_identity_writer_busy') from None
        raise


def exact_barcodes(values):
    result = []
    for raw in values:
        need(isinstance(raw, str) and bool(raw), 'consolidation_supplier_gtin_unverified')
        parts = ProductIdentifierService.split_compound_ean(raw)
        need(parts and all(value in verified_barcodes([value]) for value, _ in parts),
             'consolidation_supplier_gtin_unverified')
        result.extend(value for value, _ in parts)
    return sorted(set(result))


async def snapshot(db, keeper, facts, identifiers, *, lock=False):
    need(not facts['eans'] and not facts['manual_eans'] and not any(
        r['product_id'] == keeper.id and r['type'] in ('ean', 'upc', 'unverified_barcode')
        for r in identifiers), 'consolidation_supplier_gtin_keeper_not_empty')
    query = select(ProductSupplySource).where(ProductSupplySource.product_id == keeper.id).order_by(ProductSupplySource.id).limit(2)
    if lock:
        query = query.with_for_update(read=True)
    relations = list(await db.scalars(query.execution_options(populate_existing=True)))
    need(len(relations) == 1, 'consolidation_supplier_source_ambiguous')
    relation = relations[0]
    source = await db.get(SupplierProduct, relation.supplier_product_id,
        with_for_update={'read': True} if lock else None, populate_existing=True)
    supplier = await db.get(Supplier, source.supplier_id,
        with_for_update={'read': True} if lock else None, populate_existing=True) if source else None
    need(source is not None and supplier is not None and supplier.is_active and source.is_active
         and not source.is_discontinued and relation.is_active and relation.orderable
         and relation.pack_size == 1, 'consolidation_supplier_source_inactive_or_pack')
    aliases = [r for r in identifiers if r['type'] == 'supplier_sku' and r['supplier_id'] == supplier.id
        and r['value'] == source.supplier_sku and r['product_id'] == keeper.id]
    need(len(aliases) == 1, 'consolidation_supplier_alias_unverified')
    try:
        parsed = catalog.public_product(source, detail=True)
    except (ValidationError, ValueError, TypeError):
        raise links.LinkError('consolidation_supplier_catalog_unverified') from None
    need(parsed.supplier == supplier.code and parsed.code == source.supplier_sku
         and parsed.group_code == source.supplier_group_code and parsed.shop_code == keeper.sku
         and source.manufacturer_sku == source.supplier_sku == parsed.manufacturer_code
         and not parsed.import_blockers, 'consolidation_supplier_article_changed')
    need(keeper.brand and source.brand and parsed.brand
         and links.folded(keeper.brand) == links.folded(source.brand) == links.folded(parsed.brand),
         'consolidation_supplier_brand_conflict')
    need(source.last_seen_at is not None and parsed.fetched_at == source.last_seen_at,
         'consolidation_supplier_snapshot_stale')
    row_eans, catalog_eans = exact_barcodes([source.ean]), exact_barcodes(parsed.eans)
    need(catalog_eans and len(barcode_keys(row_eans)) == 1
         and barcode_keys(row_eans) == barcode_keys(catalog_eans), 'consolidation_supplier_gtin_unverified')
    hash_sql = '''SELECT to_jsonb(s)::text AS supplier, to_jsonb(p)::text AS source,
        to_jsonb(r)::text AS relation, to_jsonb(i)::text AS alias
        FROM product_supply_sources r JOIN supplier_products p ON p.id=r.supplier_product_id
        JOIN suppliers s ON s.id=p.supplier_id JOIN product_identifiers i ON i.id=:alias
        WHERE r.id=:relation'''
    if lock:
        # Product locks stop new alias edits. A row lock also rejects an alias
        # update committed after the RR snapshot but before that Product lock.
        hash_sql += ' FOR SHARE OF i'
    hashes = (await db.execute(text(hash_sql),
        {'alias': aliases[0]['id'], 'relation': relation.id})).mappings().one_or_none()
    need(hashes is not None, 'consolidation_context_changed')
    value = {'keeper_product_id': keeper.id, 'supplier_id': supplier.id, 'supplier_code': supplier.code,
        'supplier_product_id': source.id, 'supply_source_id': relation.id, 'supplier_alias_id': aliases[0]['id'],
        'supplier_sku': source.supplier_sku, 'manufacturer_sku': source.manufacturer_sku,
        'shop_sku': parsed.shop_code, 'brand': links.folded(source.brand),
        'eans': sorted(set(row_eans + catalog_eans)),
        'supplier_active': supplier.is_active, 'source_active': source.is_active,
        'source_discontinued': source.is_discontinued, 'link_active': relation.is_active,
        'orderable': relation.orderable, 'pack_size': relation.pack_size,
        'source_attributes': [a.model_dump() for a in parsed.variant_attributes],
        'source_axes': links.axes([a.model_dump() for a in parsed.variant_attributes]),
        'last_seen_at': source.last_seen_at.isoformat(),
        **{key + '_hash': hashlib.sha256(value.encode()).hexdigest() for key, value in hashes.items()}}
    return typed(SupplierIdentity, value)


def prove(raw, local, native):
    """Pure frozen lineage check, shared by runtime and the journal parser."""
    source = typed(SupplierIdentity, local[LOCAL_KEY])
    keeper_id = raw['keeper_product_id']
    keeper = next(p for p in local['products'] if p['id'] == keeper_id)
    need(source['keeper_product_id'] == keeper_id and source['shop_sku'] == keeper['sku'])
    need(not keeper['identity']['eans'] and not keeper['identity']['manual_eans']
         and not any(r['product_id'] == keeper_id and r['type'] in ('ean', 'upc', 'unverified_barcode')
                     for r in local['identifiers']), 'consolidation_supplier_gtin_keeper_not_empty')
    need(source['manufacturer_sku'] == source['supplier_sku'])
    need(source['supplier_active'] and source['source_active'] and not source['source_discontinued']
         and source['link_active'] and source['orderable'] and source['pack_size'] == 1)
    need(source['source_axes'] == links.axes(source['source_attributes']))
    gtins = barcode_keys(source['eans'])
    need(len(gtins) == 1 and all(value in verified_barcodes([value]) for value in source['eans']),
         'consolidation_supplier_gtin_unverified')
    registered = [r for r in local['identifiers'] if r['type'] in ('ean', 'upc')]
    need(registered and all(r['product_id'] in raw['retired_product_ids']
         and r['value'] in verified_barcodes([r['value']]) for r in registered)
         and barcode_keys(r['value'] for r in registered) == gtins,
         'consolidation_supplier_registered_gtin_conflict')
    alias = [r for r in local['identifiers'] if r['id'] == source['supplier_alias_id']]
    need(len(alias) == 1 and alias[0]['product_id'] == keeper_id and alias[0]['type'] == 'supplier_sku'
         and alias[0]['supplier_id'] == source['supplier_id'] and alias[0]['value'] == source['supplier_sku'])
    mappings = {m['id']: m for m in local['mappings']}
    targets = [(a['target'], mappings[a['mapping_id']]) for a in raw['mapping_actions'] if a['target']]
    targets += [(t, None) for t in raw['additional_sales']]
    native_rows, common_axes, keeper_article, corroborated = [], None, False, False
    for target, mapping in targets:
        leaves = [leaf for leaf in native[target['shop_code']]['leaves'] if
            leaf['code'] == target['code'] and leaf['parent_code'] == target['parent_code']
            and leaf['is_variant'] is target['is_variant'] and leaf['parent_id'] == str(target['native_product_id'])
            and leaf['external_id'] == str(target['native_variant_id'] if target['is_variant'] else target['native_product_id'])]
        need(len(leaves) == 1, 'consolidation_supplier_native_unverified')
        leaf = leaves[0]
        current_axes = physical_axes(leaf['attributes'])
        need(current_axes and (common_axes is None or common_axes == current_axes),
             'consolidation_supplier_native_axes_conflict')
        common_axes = current_axes
        need(not leaf.get('invalid_barcodes') and barcode_keys(leaf['eans']) <= gtins,
             'consolidation_supplier_native_gtin_conflict')
        supplier_code = leaf.get('supplier_code')
        need(supplier_code is None or supplier_code == source['supplier_sku'],
             'consolidation_supplier_native_article_conflict')
        is_keeper = mapping is not None and mapping['product_id'] == keeper_id
        keeper_article |= is_keeper and supplier_code == source['supplier_sku'] and leaf['code'] == keeper['sku']
        corroborated |= bool(barcode_keys(leaf['eans']) == gtins) and mapping is not None \
            and mapping['product_id'] in raw['retired_product_ids']
        native_rows.append({'shop_code': target['shop_code'], 'mapping_id': mapping['id'] if mapping else None,
            'original_product_id': mapping['product_id'] if mapping else None, 'code': leaf['code'],
            'parent_code': leaf['parent_code'], 'is_variant': leaf['is_variant'],
            'native_product_id': int(leaf['parent_id']),
            'native_variant_id': int(leaf['external_id']) if leaf['is_variant'] else None,
            'supplier_code': supplier_code, 'eans': sorted(leaf['eans']),
            'attributes': deepcopy(leaf['attributes']), 'axes': links.axes(leaf['attributes'])})
    need(keeper_article and corroborated and len(native_rows) >= 2,
         'consolidation_supplier_native_corroboration_missing')
    local_axes = [{'product_id': p['id'], 'attributes': deepcopy(p['identity']['attributes']),
        'axes': links.axes(p['identity']['attributes'])} for p in local['products']]
    for values in [source['source_attributes'], *(p['attributes'] for p in local_axes)]:
        need(all(common_axes.get(key) == value for key, value in physical_axes(values).items()),
             'consolidation_supplier_physical_axes_conflict')
    result = {'axis_rule': 'exact_except_size_xxxl_3xl', 'source': source, 'verified_gtin_key': next(iter(gtins)),
        'registered_identifiers': [{'identifier_id': r['id'], 'product_id': r['product_id'],
            'identifier_type': r['type'], 'value': r['value']} for r in registered],
        'native_leaves': native_rows, 'local_axes': local_axes, 'physical_axes': common_axes}
    return typed(SupplierGtinProof, result)


def validate_frozen(raw, local, native, identity):
    value = typed(SupplierGtinProof, identity[PROOF_KEY])
    need(value == prove(raw, local, native), 'consolidation_supplier_frozen_proof_changed')
    need(identity['verified_gtin_keys'] == [value['verified_gtin_key']]
         and identity['physical_axes'] == value['physical_axes']
         and type(identity['same_complete_retail_unit_factor']) is int
         and identity['same_complete_retail_unit_factor'] == 1,
         'consolidation_supplier_frozen_proof_changed')
    return deepcopy(value)
