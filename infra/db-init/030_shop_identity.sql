-- One saleable representation per Hub card/shop, with typed native IDs.
-- Old external_id/variant_code columns are retained as migration evidence only;
-- runtime readers and writers use the normalized fields below.
DO $$
BEGIN
    -- The conversion is performed once. Re-running upgrades must never copy
    -- stale legacy columns over a mapping subsequently verified by the app.
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_schema = current_schema() AND table_name = 'shop_products'
                     AND column_name = 'external_product_id') THEN
        ALTER TABLE shop_products ADD COLUMN external_product_id VARCHAR(100);
        ALTER TABLE shop_products ADD COLUMN external_variant_id VARCHAR(100);
        IF EXISTS (SELECT 1 FROM shop_products
                   WHERE is_variant AND nullif(btrim(variant_code), '') IS NULL) THEN
            RAISE EXCEPTION 'shop_identity_variant_code_missing: reconcile the incomplete shop mapping before migration 030';
        END IF;
        UPDATE shop_products SET
            external_product_id = CASE WHEN NOT is_variant THEN nullif(nullif(btrim(external_id), ''), 'None') END,
            external_variant_id = CASE WHEN is_variant THEN nullif(nullif(btrim(external_id), ''), 'None') END,
            external_code = CASE WHEN is_variant THEN variant_code ELSE external_code END,
            parent_code = CASE WHEN is_variant THEN parent_code END;

        -- Native parent IDs are recovered only from the exact cached family and
        -- one matching leaf, never from SKU similarity or another shop.
        WITH candidates AS (
            SELECT sp.id, c.data->>'product_id' AS parent_id,
                   leaf.data->>'variant_id' AS variant_id,
                   count(*) OVER (PARTITION BY sp.id) AS matches
            FROM shop_products sp
            JOIN shop_product_content c ON c.shop_id = sp.shop_id AND c.external_code = sp.parent_code
              AND c.data->>'code' = sp.parent_code
            CROSS JOIN LATERAL jsonb_array_elements(CASE WHEN jsonb_typeof(c.data->'variants') = 'array'
                THEN c.data->'variants' ELSE '[]'::jsonb END) leaf(data)
            WHERE sp.is_variant AND leaf.data->>'code' = sp.external_code
              AND c.data->>'product_id' ~ '^[1-9][0-9]*$'
              AND leaf.data->>'variant_id' ~ '^[1-9][0-9]*$'
              AND length(c.data->>'product_id') <= 100 AND length(leaf.data->>'variant_id') <= 100
              AND (leaf.data->>'product_id' IS NULL OR leaf.data->>'product_id' = c.data->>'product_id')
              AND (sp.external_variant_id IS NULL OR sp.external_variant_id = leaf.data->>'variant_id')
        )
        UPDATE shop_products sp SET external_product_id = candidate.parent_id,
                                    external_variant_id = candidate.variant_id
        FROM candidates candidate WHERE candidate.id = sp.id AND candidate.matches = 1;

        UPDATE shop_products sp SET external_product_id = c.data->>'product_id'
        FROM shop_product_content c
        WHERE NOT sp.is_variant AND sp.external_product_id IS NULL
          AND c.shop_id = sp.shop_id AND c.external_code = sp.external_code
          AND c.data->>'code' = sp.external_code AND c.data->>'product_id' ~ '^[1-9][0-9]*$'
          AND length(c.data->>'product_id') <= 100
          AND c.data->'variants' = '[]'::jsonb;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'shop_products'::regclass
                   AND conname = 'chk_shop_products_identity_shape') THEN
        ALTER TABLE shop_products ADD CONSTRAINT chk_shop_products_identity_shape
            CHECK (is_variant OR (external_variant_id IS NULL AND parent_code IS NULL));
    END IF;
END $$;

-- A product native ID and a variant native ID may be numerically identical;
-- two Hub cards may never own the same native leaf within one shop.
CREATE UNIQUE INDEX IF NOT EXISTS uq_shop_products_native_product
    ON shop_products(shop_id, external_product_id)
    WHERE NOT is_variant AND external_product_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_shop_products_native_variant
    ON shop_products(shop_id, external_variant_id)
    WHERE is_variant AND external_variant_id IS NOT NULL;
