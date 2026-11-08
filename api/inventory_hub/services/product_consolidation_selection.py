"""Complete local selection for the operator's consolidation review form.

This is neither a preview nor an eligibility decision. It reads one PostgreSQL
snapshot without native calls, recovery, ownership redirects or stock readers.
The existing durable preview and apply remain the only identity/write gates.
"""
from collections import defaultdict
from decimal import Decimal

from sqlalchemy import select, text

from inventory_hub.db_models import Product, ProductIdentifier, Shop
from inventory_hub.db_models_ext import ProductVariantAttribute, ShopProduct
from inventory_hub.product_editor_models import ProductEditorOverride
from inventory_hub.services import product_links as links
from inventory_hub.services.canonical_products import aliases_for
from inventory_hub.services.product_identity import verified_barcodes


MAX_PRODUCTS = 10
MAX_MAPPINGS = 50
MAX_ID = 9007199254740991


async def inspect(db, product_ids):
    if (not isinstance(product_ids, list) or not 2 <= len(product_ids) <= MAX_PRODUCTS
            or any(type(value) is not int or not 0 < value <= MAX_ID for value in product_ids)
            or len(set(product_ids)) != len(product_ids)):
        raise links.LinkError('consolidation_invalid_request', 422)
    ids = sorted(product_ids)
    # The route owns a fresh request session. PostgreSQL itself rejects DML,
    # and every identity/mapping read sees the same snapshot without row locks.
    await db.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
    cards = list(await db.scalars(select(Product).where(Product.id.in_(ids)).order_by(Product.id)))
    if len(cards) != len(ids):
        raise links.LinkError('consolidation_product_missing', 404)
    rows = (await db.execute(select(ShopProduct, Shop).join(Shop, ShopProduct.shop_id == Shop.id)
        .where(ShopProduct.product_id.in_(ids)).order_by(ShopProduct.id).limit(MAX_MAPPINGS + 1))).all()
    if len(rows) > MAX_MAPPINGS:
        # No shortened mapping set can be mistaken for the complete review.
        raise links.LinkError('consolidation_selection_mapping_limit')
    aliases = await aliases_for(db, ids)
    alias_members = set(aliases) | set(aliases.values())
    unaliased = [identifier for identifier in ids if identifier not in alias_members]
    # This helper shares the merge's registered GTIN / effective axis rules.
    # Only unaliased cards reach it: selection never borrows another card's
    # identifiers or silently substitutes a canonical product for a chosen ID.
    facts = await links._facts(db, unaliased) if unaliased else {}
    blocked_ids = [identifier for identifier in ids if identifier in alias_members]
    eans, attributes = defaultdict(set), defaultdict(list)
    blocked_overrides = {}
    if blocked_ids:
        for identifier in await db.scalars(select(ProductIdentifier).where(ProductIdentifier.product_id.in_(blocked_ids))
                .order_by(ProductIdentifier.id)):
            if getattr(identifier.identifier_type, 'value', identifier.identifier_type) in ('ean', 'upc'):
                eans[identifier.product_id].update(verified_barcodes((identifier.value,)))
        for attribute in await db.scalars(select(ProductVariantAttribute)
                .where(ProductVariantAttribute.product_id.in_(blocked_ids))
                .order_by(ProductVariantAttribute.product_id, ProductVariantAttribute.attribute_name)):
            attributes[attribute.product_id].append({'name': attribute.attribute_name, 'value': attribute.attribute_value})
        blocked_overrides = dict((await db.execute(select(ProductEditorOverride.product_id, ProductEditorOverride.data)
            .where(ProductEditorOverride.product_id.in_(blocked_ids)))).all())
    products, mappings, blockers = [], [], []
    for card in cards:
        saved = blocked_overrides.get(card.id, {})
        variant = saved.get('variant', {})
        effective_attributes = variant.get('attributes', attributes[card.id])
        item = facts.get(card.id, {'name': saved.get('common', {}).get('name') or card.name,
            'eans': sorted(eans[card.id]),
            'attributes': effective_attributes if isinstance(effective_attributes, list) else attributes[card.id],
            'manual_eans': variant.get('eans') if isinstance(variant.get('eans'), list) else None})
        canonical_id = aliases.get(card.id, card.id if card.id in alias_members else None)
        products.append({'id': card.id, 'sku': card.sku, 'name': item.get('name', card.name),
            'is_active': card.is_active, 'item_type': card.item_type, 'stock_unit': card.stock_unit,
            'eans': item.get('eans', sorted(eans[card.id])),
            'attributes': item.get('attributes', attributes[card.id]),
            'manual_eans': item.get('manual_eans'), 'canonical_product_id': canonical_id})
        if canonical_id is not None:
            blockers.append({'code': 'consolidation_existing_alias_group_unsupported', 'product_id': card.id})
        if card.item_type != 'stock' or card.stock_unit != 'pcs':
            blockers.append({'code': 'consolidation_whole_piece_cards_required', 'product_id': card.id})
    for mapping, shop in rows:
        available = shop.is_active and shop.platform == 'upgates'
        mappings.append({'id': mapping.id, 'product_id': mapping.product_id,
            'shop_code': shop.code, 'shop_name': shop.name, 'shop_available': bool(available),
            'external_code': mapping.external_code, 'parent_code': mapping.parent_code,
            'is_variant': mapping.is_variant, 'external_product_id': mapping.external_product_id,
            'external_variant_id': mapping.external_variant_id, 'is_listed': mapping.is_listed,
            'shared_stock': mapping.shared_stock, 'sale_to_stock_factor': str(mapping.sale_to_stock_factor),
            'push_pending': mapping.push_pending})
        if mapping.push_pending or mapping.shared_stock or mapping.sale_to_stock_factor != Decimal(1):
            blockers.append({'code': 'consolidation_existing_mapping_mode_unsupported',
                'product_id': mapping.product_id, 'mapping_id': mapping.id, 'shop_code': shop.code})
        if not available:
            blockers.append({'code': 'consolidation_shop_unavailable',
                'product_id': mapping.product_id, 'mapping_id': mapping.id, 'shop_code': shop.code})
    return {'products': products, 'mappings': mappings, 'blockers': blockers}
