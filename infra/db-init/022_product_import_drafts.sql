-- Additive durable snapshots; no stock, shop listing or existing-product changes.
CREATE TABLE IF NOT EXISTS product_import_drafts (
    id varchar(32) PRIMARY KEY,
    request_hash varchar(64) NOT NULL,
    revision integer NOT NULL CHECK (revision > 0),
    supplier varchar(50) NOT NULL,
    shop varchar(50) NOT NULL,
    status varchar(30) NOT NULL,
    document jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_product_import_drafts_updated ON product_import_drafts (updated_at DESC);
