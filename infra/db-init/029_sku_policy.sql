-- Additive registry only. No product renames, historical links or stock writes.
CREATE TABLE IF NOT EXISTS sku_brands (
    id BIGSERIAL PRIMARY KEY,
    code VARCHAR(2) NOT NULL UNIQUE CHECK (code ~ '^[A-Z]{2}$'),
    name VARCHAR(100) NOT NULL,
    kind VARCHAR(16) NOT NULL CHECK (kind IN ('brand', 'service', 'unbranded')),
    source_ref TEXT NOT NULL,
    created_by VARCHAR(100) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS sku_brand_aliases (
    alias_key VARCHAR(300) PRIMARY KEY,
    brand_id BIGINT NOT NULL REFERENCES sku_brands(id) ON DELETE RESTRICT,
    alias VARCHAR(100) NOT NULL,
    source_ref TEXT NOT NULL,
    created_by VARCHAR(100) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_sku_brand_aliases_brand ON sku_brand_aliases(brand_id);
CREATE TABLE IF NOT EXISTS product_sku_identities (
    id BIGSERIAL PRIMARY KEY,
    product_id BIGINT NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
    brand_id BIGINT NOT NULL REFERENCES sku_brands(id) ON DELETE RESTRICT,
    source_code VARCHAR(97) NOT NULL CHECK (source_code <> '' AND source_code = btrim(source_code)),
    source_code_fold VARCHAR(300) NOT NULL,
    code_kind VARCHAR(16) NOT NULL CHECK (code_kind IN ('manufacturer', 'supplier', 'gtin', 'service', 'unbranded')),
    supplier_id BIGINT REFERENCES suppliers(id) ON DELETE RESTRICT,
    identity_key VARCHAR(64) NOT NULL UNIQUE,
    source_ref TEXT NOT NULL CHECK (source_ref <> ''),
    brand_raw VARCHAR(100) NOT NULL,
    allocated_sku VARCHAR(100) NOT NULL,
    created_by VARCHAR(100) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (code_kind <> 'supplier' OR supplier_id IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_product_sku_identities_product ON product_sku_identities(product_id);
CREATE INDEX IF NOT EXISTS idx_product_sku_identities_brand_code
    ON product_sku_identities(brand_id, source_code_fold);

-- Initial technical abbreviations; intentionally no association to historical cards.
INSERT INTO sku_brands (code, name, kind, source_ref, created_by)
VALUES ('TP', 'Topeak', 'brand', 'Initial SKU policy registry', 'migration:029'),
       ('CT', 'Continental', 'brand', 'Initial SKU policy registry', 'migration:029'),
       ('SH', 'Shimano', 'brand', 'Initial SKU policy registry', 'migration:029'),
       ('FO', 'Force', 'brand', 'Initial SKU policy registry', 'migration:029'),
       ('NF', 'Northfinder', 'brand', 'Initial SKU policy registry', 'migration:029'),
       ('SW', 'Schwalbe', 'brand', 'Initial SKU policy registry', 'migration:029'),
       ('BB', 'BBB Cycling', 'brand', 'Initial SKU policy registry', 'migration:029')
ON CONFLICT (code) DO NOTHING;
INSERT INTO sku_brand_aliases (alias_key, brand_id, alias, source_ref, created_by)
SELECT lower(name), id, name, source_ref, created_by FROM sku_brands
WHERE created_by = 'migration:029'
ON CONFLICT (alias_key) DO NOTHING;
INSERT INTO sku_brand_aliases (alias_key, brand_id, alias, source_ref, created_by)
SELECT 'bbb', id, 'BBB', 'BBB Cycling official brand spelling', 'migration:029'
FROM sku_brands WHERE code = 'BB' AND name = 'BBB Cycling'
ON CONFLICT (alias_key) DO NOTHING;
