-- 004_shop_product_content.sql
-- Vault for the COMPLETE product content pulled from a shop (Upgates).
-- One row per shop + external (parent) product code, storing the raw API
-- payload losslessly: descriptions, images incl. titles, prices, metas,
-- SEO, labels, parameters, variants... Used as the source when transferring
-- products to another shop (xTrek on Upgates, or Atomer export).
-- Additive migration: safe to run on an existing database.

CREATE TABLE IF NOT EXISTS shop_product_content (
    id BIGSERIAL PRIMARY KEY,
    shop_id BIGINT NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
    external_code VARCHAR(100) NOT NULL,
    data JSONB NOT NULL,
    pulled_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_shop_product_content UNIQUE (shop_id, external_code)
);

CREATE INDEX IF NOT EXISTS idx_shop_product_content_shop
    ON shop_product_content (shop_id);
