"""The last ten actual availability changes and the latest confirmed shop sync."""
from sqlalchemy import text
from inventory_hub.services.canonical_products import aliases_for


HISTORY = """
    WITH supplier_changes AS (
        SELECT 'supplier' AS kind, s.name AS source, c.changed_at,
            c.before_state AS before, c.after_state AS after, c.id::text AS event_id
        FROM product_supply_sources l
        JOIN supplier_products p ON p.id = l.supplier_product_id
        JOIN suppliers s ON s.id = p.supplier_id
        JOIN LATERAL (
            SELECT id, changed_at, before_state, after_state
            FROM supplier_availability_changes
            WHERE supplier_id = p.supplier_id AND supplier_sku = p.supplier_sku
            ORDER BY changed_at DESC, id DESC LIMIT 10
        ) c ON true
        WHERE l.product_id = :product_id
    ), shop_changes AS (
        SELECT 'shop' AS kind, s.name AS source, pub.updated_at AS changed_at,
            pub.document->'before'->>'availability' AS before,
            pub.document->'after'->>'availability' AS after, pub.id AS event_id
        FROM product_editor_publications pub JOIN shops s ON s.id = pub.shop_id
        WHERE pub.product_id = :product_id AND pub.state = 'completed'
            AND pub.document->'before'->>'availability' <>
                pub.document->'after'->>'availability'
        UNION ALL
        SELECT 'shop', s.name, i.verified_at, i.before->>'availability',
            i.desired->>'availability', i.id::text
        FROM stock_sync_items i JOIN shops s ON s.id = i.shop_id
        JOIN products p ON p.sku = i.sku
        WHERE p.id = :product_id AND i.status = 'verified'
            AND i.before->>'availability' <> i.desired->>'availability'
    )
    SELECT * FROM (
        SELECT * FROM supplier_changes
        UNION ALL
        SELECT * FROM shop_changes
    ) changes ORDER BY changed_at DESC, kind, event_id DESC LIMIT 10
"""


SHOP_SYNC = """
    WITH mapped AS (
        SELECT sp.shop_id, sp.external_code AS code, sp.external_product_id AS parent_id,
            sp.external_variant_id AS variant_id, p.sku
        FROM shop_products sp JOIN products p ON p.id = sp.product_id
        WHERE p.id = :product_id AND sp.retired_at IS NULL
    ), exact_syncs AS (
        SELECT pub.shop_id, pub.updated_at AS synced_at
        FROM product_editor_publications pub
        WHERE pub.product_id = :product_id AND pub.state = 'completed'
          AND EXISTS (SELECT 1 FROM mapped m WHERE m.shop_id = pub.shop_id
            AND m.code = pub.document->'identity'->>'code'
            AND m.parent_id = pub.document->'identity'->>'product_id'
            AND m.variant_id IS NOT DISTINCT FROM pub.document->'identity'->>'variant_id')
        UNION ALL
        SELECT i.shop_id, i.verified_at
        FROM stock_sync_items i
        JOIN mapped m ON m.shop_id = i.shop_id AND m.sku = i.sku AND m.code = i.target->>'code'
            AND m.parent_id = i.target->>'product_id'
            AND m.variant_id IS NOT DISTINCT FROM i.target->>'variant_id'
        WHERE i.status = 'verified' AND (i.before IS NOT NULL OR i.attempt_started_at IS NOT NULL)
        UNION ALL
        SELECT b.shop_id, i.verified_at
        FROM stock_publication_items i JOIN stock_publication_batches b ON b.id = i.batch_id
        JOIN mapped m ON m.shop_id = b.shop_id AND m.sku = i.sku AND m.code = i.target->>'code'
            AND m.parent_id = i.target->>'product_id'
            AND m.variant_id IS NOT DISTINCT FROM i.target->>'variant_id'
        WHERE i.status = 'verified'
    )
    SELECT s.code AS shop_code, s.name AS shop_name, max(t.synced_at) AS last_sync_at
    FROM shops s LEFT JOIN exact_syncs t ON t.shop_id = s.id
    WHERE s.is_active AND s.platform = 'upgates' AND s.id IN (SELECT shop_id FROM mapped)
    GROUP BY s.id, s.code, s.name ORDER BY s.code
"""


async def status(db, product_id):
    from inventory_hub.services.product_editor import EditorError
    aliases = await aliases_for(db, [product_id])
    canonical = aliases.get(product_id, product_id)
    if await db.scalar(text("SELECT id FROM products WHERE id = :id"), {"id": canonical}) is None:
        raise EditorError("product_editor_product_missing", 404)
    params = {"product_id": canonical}
    changes = (await db.execute(text(HISTORY), params)).mappings()
    shops = (await db.execute(text(SHOP_SYNC), params)).mappings()
    return {"changes": [dict(row) for row in changes], "shops": [dict(row) for row in shops]}
