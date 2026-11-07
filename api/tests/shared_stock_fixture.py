"""Explicit synthetic sale mappings; never infer units from product titles."""
from decimal import Decimal
from sqlalchemy import select
from inventory_hub.db_models_ext import ShopProduct


async def shared_sale_mappings(db, shop_id, product_id):
    original = await db.scalar(select(ShopProduct).where(
        ShopProduct.shop_id == shop_id, ShopProduct.product_id == product_id))
    if original is None or not original.is_variant:
        raise AssertionError('Fixture requires an existing variant mapping')
    original.shared_stock = True
    original.sale_to_stock_factor = Decimal(1)
    original.sale_barcodes = []
    mappings = [original]
    for amount in (8, 100):
        mapping = ShopProduct(shop_id=shop_id, product_id=product_id,
            external_code=f'{original.external_code}-{amount}', parent_code=original.parent_code,
            external_product_id=original.external_product_id,
            external_variant_id=str(int(original.external_variant_id) + amount), is_variant=True,
            shared_stock=True, sale_to_stock_factor=Decimal(amount), sale_barcodes=[])
        db.add(mapping)
        mappings.append(mapping)
    await db.flush()
    return mappings


def remote_identity(mapping):
    return {'code': mapping.external_code, 'parent_code': mapping.parent_code,
            'variant_code': mapping.external_code, 'product_id': int(mapping.external_product_id),
            'variant_id': int(mapping.external_variant_id)}
