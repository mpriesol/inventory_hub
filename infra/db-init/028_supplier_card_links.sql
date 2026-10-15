-- Explicit local supplier associations; no historical matching or stock writes.
CREATE TABLE IF NOT EXISTS supplier_card_link_previews (
    id VARCHAR(36) PRIMARY KEY,
    product_id BIGINT NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
    supplier_product_id BIGINT NOT NULL REFERENCES supplier_products(id) ON DELETE RESTRICT,
    request_hash VARCHAR(64) NOT NULL,
    preview_hash VARCHAR(64) NOT NULL,
    document JSONB NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'prepared' CHECK (status IN ('prepared', 'completed')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    result JSONB,
    CHECK ((status = 'prepared' AND result IS NULL AND completed_at IS NULL)
        OR (status = 'completed' AND result IS NOT NULL AND completed_at IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_supplier_card_link_previews_product_created
    ON supplier_card_link_previews(product_id, created_at DESC);
