"""Fill missing catalog images from explicitly linked, cached shop identities.

This is a read-only projection. Supplier snapshots and existing photographs stay
intact; no shop API request or product/stock write is made.
"""
from collections import defaultdict
from urllib.parse import urlsplit

from sqlalchemy import text

from inventory_hub import config_io
from inventory_hub.services.product_identity import verified_barcodes


IMAGE_SQL = """
SELECT source.id AS source_id, source.ean AS source_ean, CASE WHEN sp.is_variant THEN sp.external_variant_id ELSE sp.external_product_id END AS external_id,
 sp.is_variant, p.sku, p.brand, sp.external_code, sp.parent_code,
 c.pulled_at, c.data->>'code' AS cached_parent_code,
 c.data->>'product_id' AS parent_id, leaf.value->>'code' AS leaf_code,
 leaf.value->>'product_id' AS leaf_parent_id,
 leaf.value->>'variant_id' AS variant_id, leaf.value->>'ean' AS leaf_ean,
 jsonb_path_query_array(leaf.value, '$.images[0 to 19]') AS images,
 leaf.value->'image' AS image,
 CASE WHEN c.data->'add_images_into_variants_yn' = 'true'::jsonb
      THEN jsonb_path_query_array(c.data, '$.images[0 to 19]') ELSE '[]'::jsonb END AS parent_images,
 CASE WHEN sp.is_variant THEN jsonb_array_length(v.matches) ELSE 1 END AS leaf_count
FROM supplier_products source
JOIN suppliers supplier ON supplier.id = source.supplier_id
JOIN products p ON (
 EXISTS (SELECT 1 FROM product_supply_sources binding
         WHERE binding.product_id = p.id AND binding.supplier_product_id = source.id
           AND binding.is_active))
JOIN shop_products sp ON sp.product_id = p.id AND sp.is_listed
JOIN shops shop ON shop.id = sp.shop_id AND shop.code = :shop AND shop.is_active
JOIN shop_product_content c ON c.shop_id = sp.shop_id
 AND c.external_code = CASE WHEN sp.is_variant THEN sp.parent_code ELSE sp.external_code END
LEFT JOIN LATERAL (SELECT jsonb_path_query_array(c.data,
 '$.variants[*] ? (@.code == $code)', jsonb_build_object('code', sp.external_code)) AS matches) v ON sp.is_variant
JOIN LATERAL (SELECT CASE WHEN sp.is_variant THEN v.matches->0 ELSE c.data END AS value) leaf ON true
WHERE source.id = ANY(:ids) AND supplier.code = :supplier AND source.is_active
 AND upper(source.brand) = ANY(:brands) AND upper(p.brand) = upper(source.brand)
"""


def image_urls(images, single=None):
    values = list(images) if isinstance(images, list) else []
    if single:
        values.append(single)
    values.sort(key=lambda item: not (isinstance(item, dict) and item.get('main_yn') is True))
    result = []
    for item in values[:20]:
        value = item.get('url') if isinstance(item, dict) else item
        if not isinstance(value, str) or len(value) > 2000 or any(ord(c) < 32 for c in value):
            continue
        try:
            parsed = urlsplit(value)
            valid = parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password
        except ValueError:
            valid = False
        if valid and value not in result:
            result.append(value)
    return result


def verified_images(row):
    code = row['external_code']
    parent = row['parent_code'] if row['is_variant'] else row['external_code']
    native = row['variant_id'] if row['is_variant'] else row['parent_id']
    if (row['leaf_count'] != 1 or not code or row['leaf_code'] != code
            or not parent or row['cached_parent_code'] != parent
            or not native or not str(native).isdigit() or str(native) != row['external_id']
            or not row['parent_id'] or not str(row['parent_id']).isdigit()
            or (row['leaf_parent_id'] and row['leaf_parent_id'] != row['parent_id'])):
        return []
    source_eans = set(verified_barcodes((row['source_ean'],)))
    shop_eans = set(verified_barcodes((row['leaf_ean'],)))
    if source_eans and shop_eans and not source_eans.intersection(shop_eans):
        return []
    return image_urls(row['images'], row['image']) or image_urls(row['parent_images'])


async def attach(db, supplier, products):
    missing = {p.id for p in products if not p.images}
    if not missing:
        return
    cfg = config_io.load_supplier(supplier, write_back_on_load=False)
    policy = (cfg.get('adapter_settings') or {}).get('catalog_image_fallback') or {}
    if policy.get('enabled') is not True:
        return
    rows = (await db.execute(text(IMAGE_SQL), {'supplier': supplier, 'shop': policy['shop_code'],
        'brands': policy['brands'], 'ids': sorted(missing)})).mappings().all()
    candidates = defaultdict(dict)
    for row in rows:
        urls = verified_images(row)
        if urls:
            candidates[row['source_id']][(row['is_variant'], row['external_id'])] = (urls, row)
    for product in products:
        matches = candidates.get(product.id, {})
        if product.images or len(matches) != 1:
            continue
        urls, row = next(iter(matches.values()))
        product.images = urls
        product.mapping_provenance['image_fallback'] = {'shop_code': policy['shop_code'], 'code': row['leaf_code'],
            'native_id': row['external_id'], 'pulled_at': row['pulled_at'].isoformat()}
        product.warnings = [w for w in product.warnings if w != 'missing_images']
        if 'images_from_linked_shop' not in product.warnings:
            product.warnings.append('images_from_linked_shop')
