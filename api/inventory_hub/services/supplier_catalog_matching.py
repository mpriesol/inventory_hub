"""Explicitly configured existing-shop matching by exact supplier codes or EAN.

Requires the same verified brand and a listed product in the configured shop.
No title similarity, prefix guessing, product creation or stock writes.
"""
from collections import defaultdict
from sqlalchemy import select, func
from inventory_hub.db_models import SupplierProduct, Shop
from inventory_hub.db_models_ext import ShopProduct, ShopProductContent
from inventory_hub.services.product_identity import verified_barcodes
from inventory_hub.services import product_links
from inventory_hub import config_io


async def suggestions(db, supplier, products):
    cfg = config_io.load_supplier(supplier.code, write_back_on_load=False)
    policy = (cfg.get('adapter_settings') or {}).get('existing_card_matching') or {}
    if policy.get('enabled') is not True:
        return {}, {}
    brands = {brand.upper() for brand in policy['brands']}
    selected = {p.id:p for p in products if p.is_active and (p.brand or '').upper() in brands}
    if not selected:
        return {}, {}
    mappings = list(await db.scalars(select(ShopProduct).join(Shop, Shop.id == ShopProduct.shop_id)
        .where(Shop.code == policy['shop_code'], ShopProduct.is_listed.is_(True), ShopProduct.product_id.in_(selected))))
    if not mappings:
        return {}, {}
    facts = await product_links._facts(db, [m.product_id for m in mappings])
    sources = list(await db.scalars(select(SupplierProduct).where(SupplierProduct.supplier_id == supplier.id,
        SupplierProduct.is_active.is_(True), SupplierProduct.is_discontinued.is_(False), func.upper(SupplierProduct.brand).in_(brands))))
    by_code = {source.supplier_sku:source for source in sources}
    by_ean = defaultdict(list)
    for source in sources:
        for ean in verified_barcodes((source.ean,)):
            by_ean[ean].append(source)
    cache = {(row.shop_id,row.external_code):row.data for row in await db.scalars(select(ShopProductContent).where(
        ShopProductContent.shop_id.in_({m.shop_id for m in mappings}),
        ShopProductContent.external_code.in_({m.parent_code or m.external_code for m in mappings})))}
    result, issues = {}, {}
    for mapping in mappings:
        product = selected[mapping.product_id]
        # Existing cache evidence must identify the exact mapped native leaf.
        document = cache.get((mapping.shop_id,mapping.parent_code or mapping.external_code)) or {}
        leaves = document.get('variants', []) if mapping.is_variant else [document]
        leaves = [leaf for leaf in leaves if isinstance(leaf,dict) and leaf.get('code') == product.sku]
        supplier_code = None
        if len(leaves) == 1 and mapping.external_id:
            leaf = leaves[0]
            native = leaf.get('variant_id' if mapping.is_variant else 'product_id')
            if str(native) == str(mapping.external_id):
                supplier_code = leaf.get('code_supplier')
        exact = by_code.get(supplier_code) if isinstance(supplier_code,str) else None
        eans = facts[product.id]['manual_eans']
        eans = verified_barcodes(eans) if eans is not None else facts[product.id]['eans']
        candidates = {source.id:source for ean in eans for source in by_ean.get(ean, [])
                      if (source.brand or '').upper() == (product.brand or '').upper()}
        if exact and (exact.brand or '').upper() == (product.brand or '').upper():
            source_eans = verified_barcodes((exact.ean,))
            if eans and source_eans and not set(eans) & set(source_eans):
                issues[product.id] = 'supplier_code_barcode_conflict'
                continue
            candidates[exact.id] = exact
        if len(candidates) == 1:
            result[product.id] = next(iter(candidates.values())).supplier_sku
        elif len(candidates) > 1:
            issues[product.id] = 'supplier_candidate_ambiguous'
    return result, issues
