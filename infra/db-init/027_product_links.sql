-- Existing-card links only. No cards, balances, prices or remote content change.
CREATE TABLE IF NOT EXISTS product_link_previews (
    id VARCHAR(36) PRIMARY KEY,
    shop_id BIGINT NOT NULL REFERENCES shops(id) ON DELETE RESTRICT,
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
CREATE INDEX IF NOT EXISTS idx_product_link_previews_shop_created
    ON product_link_previews(shop_id, created_at DESC);
