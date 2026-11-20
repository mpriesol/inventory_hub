-- Historical identities keep their original FK and allocated SKU.
ALTER TABLE sku_brands ADD COLUMN IF NOT EXISTS archived boolean NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS sku_brand_reserved_codes (
    code varchar(2) PRIMARY KEY CHECK (code ~ '^[A-Z]{2}$'),
    brand_id bigint NOT NULL REFERENCES sku_brands(id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_sku_brand_reserved_owner ON sku_brand_reserved_codes(brand_id);
