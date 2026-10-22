"""Batch, read-only Hub identities for catalog display, never source snapshots.

The historical supplier-prefix proposal and cached shop index are deliberately
not evidence. Only current supplier-scoped identifiers, validated barcodes and
stored source bindings can identify a physical card.
"""
from collections import defaultdict

from sqlalchemy import select

from inventory_hub.db_models_ext import ProductSupplySource
from inventory_hub.services.product_candidates import _load_cards
from inventory_hub.services.product_identity import RemoteIdentity, barcode_keys, load_identity_index, verified_barcodes
from inventory_hub.services.product_links import LinkError, axes
from inventory_hub.services.sku_policy import brand_key
from inventory_hub.sku_policy_models import SkuBrandAlias


def _contradictions(product, source, card, brands):
    errors = []
    if product.code != source.supplier_sku:
        errors.append('supplier_source_identity_changed')
    record_eans, catalog_eans = barcode_keys((source.ean,)), barcode_keys(product.eans)
    if record_eans and catalog_eans and not record_eans & catalog_eans:
        errors.append('supplier_source_ean_conflict')
    source_brands = [brand_key(value) for value in (product.brand, source.brand) if value]
    local_brand = brand_key(card['brand']) if card.get('brand') else None
    for left in source_brands:
        for right in [*source_brands, *([local_brand] if local_brand else [])]:
            if left != right and (brands.get(left) is None or brands.get(left) != brands.get(right)):
                errors.append('sku_brand_conflict')
    try:
        incoming, stored = axes([item.model_dump() for item in product.variant_attributes]), axes(card['attributes'])
        if any(incoming[key] != stored[key] for key in incoming.keys() & stored.keys()):
            errors.append('variant_axes_conflict')
    except LinkError:
        errors.append('variant_axes_ambiguous')
    manual, incoming_eans = barcode_keys(card.get('manual_eans')), record_eans | catalog_eans
    if manual and incoming_eans and not manual & incoming_eans:
        errors.append('manual_barcode_conflict')
    return list(dict.fromkeys(errors))


async def attach(db, products, sources):
    """Attach display fields to one page/detail batch without writes or N+1 reads."""
    if not products:
        return
    source_ids = {product.id for product in products}
    owners = defaultdict(set)
    for source_id, product_id in (await db.execute(select(
            ProductSupplySource.supplier_product_id, ProductSupplySource.product_id)
            .where(ProductSupplySource.supplier_product_id.in_(source_ids)))).all():
        owners[source_id].add(product_id)
    identities = {}
    for product in products:
        source = sources[product.id]
        bindings = owners[product.id]
        identities[product.id] = RemoteIdentity(shop_id=0, supplier_id=source.supplier_id,
            supplier_sku=source.supplier_sku,
            barcodes=verified_barcodes((source.ean, *product.eans)),
            expected_product_id=next(iter(bindings)) if len(bindings) == 1 else None)
    index = await load_identity_index(db, 0, list(identities.values()), local=True)
    resolutions = {source_id: index.resolve(identity) for source_id, identity in identities.items()}
    cards = await _load_cards(db, {result.product_id for result in resolutions.values() if result.product_id})
    brand_keys = {brand_key(value) for product in products for value in (product.brand, sources[product.id].brand) if value}
    brand_keys.update(brand_key(card['brand']) for card in cards.values() if card.get('brand'))
    brands = dict((await db.execute(select(SkuBrandAlias.alias_key, SkuBrandAlias.brand_id)
                                   .where(SkuBrandAlias.alias_key.in_(brand_keys)))).all()) if brand_keys else {}
    for product in products:
        result, source = resolutions[product.id], sources[product.id]
        product.hub_identity_status = 'unassigned'
        product.hub_product_id = product.hub_sku = None
        errors = list(result.reasons)
        if len(owners[product.id]) > 1:
            errors.append('supplier_source_multiple_owners')
        card = cards.get(result.product_id)
        if card:
            errors.extend(_contradictions(product, source, card, brands))
        if errors or result.status == 'conflict':
            product.hub_identity_status = 'conflict'
        elif card and result.status in ('identified', 'mapped'):
            product.hub_identity_status = 'assigned'
            product.hub_product_id, product.hub_sku = card['product_id'], card['sku']
        product.hub_identity_reasons = list(dict.fromkeys(errors))
