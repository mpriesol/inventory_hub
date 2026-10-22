-- Current supplier relationships live in product_supply_sources only.
-- Keep creation provenance and historical product caches intact for inspection.
-- Bind the migration marker to this database namespace, including isolated tests.
SELECT set_config('search_path', quote_ident(current_schema()), true);
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

SELECT pg_advisory_xact_lock(643128702193);

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM product_supply_sources
        GROUP BY supplier_product_id HAVING count(DISTINCT product_id) > 1
    ) THEN
        RAISE EXCEPTION 'supplier_source_multiple_products: resolve existing supply-source owners before migration 032';
    END IF;
    IF EXISTS (
        SELECT 1 FROM product_supply_sources links
        JOIN supplier_products source ON source.id = links.supplier_product_id
        JOIN product_identifiers alias ON alias.supplier_id = source.supplier_id
            AND alias.value = source.supplier_sku AND alias.identifier_type = 'supplier_sku'
        WHERE alias.product_id <> links.product_id
    ) THEN
        RAISE EXCEPTION 'supplier_source_alias_conflict: resolve current source and scoped identifier owners before migration 032';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM schema_migrations WHERE version = 32) THEN
        -- One-time conversion of explicit evidence; a code prefix or product
        -- name is never evidence. Opposing evidence is left unlinked for review.
        WITH evidence AS (
            SELECT id AS product_id, source_supplier_product_id AS supplier_product_id
            FROM products WHERE source_supplier_product_id IS NOT NULL
            UNION
            SELECT alias.product_id, source.id
            FROM product_identifiers alias
            JOIN supplier_products source ON source.supplier_id = alias.supplier_id
                AND source.supplier_sku = alias.value
            WHERE alias.identifier_type = 'supplier_sku'
            UNION
            SELECT product_id, supplier_product_id FROM product_supply_sources
        ), sole_owner AS (
            SELECT supplier_product_id, min(product_id) AS product_id
            FROM evidence GROUP BY supplier_product_id
            HAVING count(DISTINCT product_id) = 1
        ), sole_offer AS (
            SELECT evidence.product_id, source.supplier_id
            FROM evidence JOIN supplier_products source ON source.id = evidence.supplier_product_id
            GROUP BY evidence.product_id, source.supplier_id
            HAVING count(DISTINCT evidence.supplier_product_id) = 1
        )
        INSERT INTO product_supply_sources (product_id, supplier_product_id)
        SELECT owner.product_id, owner.supplier_product_id
        FROM sole_owner owner
        JOIN supplier_products source ON source.id = owner.supplier_product_id
        JOIN sole_offer offer ON offer.product_id = owner.product_id
            AND offer.supplier_id = source.supplier_id
        ON CONFLICT (product_id, supplier_product_id) DO NOTHING;

        -- Mirror verified current relations atomically for invoice/scanner
        -- lookup. Existing aliases and operator source policies stay unchanged.
        INSERT INTO product_identifiers
            (product_id, identifier_type, value, supplier_id, is_primary, notes)
        SELECT links.product_id, 'supplier_sku', source.supplier_sku, source.supplier_id,
            false, 'Migration 032: explicit supplier relationship'
        FROM product_supply_sources links
        JOIN supplier_products source ON source.id = links.supplier_product_id
        ON CONFLICT (supplier_id, value) WHERE identifier_type = 'supplier_sku' DO NOTHING;

        INSERT INTO schema_migrations (version) VALUES (32);
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_supply_source_owner
    ON product_supply_sources (supplier_product_id);

COMMENT ON VIEW v_product_with_identifiers IS
    'Legacy inspection view; product supplier cache columns are not current authority. Use product_supply_sources and supplier_availability_observations. No active application consumer.';
COMMENT ON VIEW v_product_inventory IS
    'Legacy inspection view; supplier cache fields and unconfirmed quantities are not current stock or supplier authority. Use application stock and availability projections. No active application consumer.';
COMMENT ON VIEW v_stock_alerts IS
    'Legacy inspection view; supplier cache fields and unconfirmed quantities are not current stock or supplier authority. Use application stock and availability projections. No active application consumer.';
