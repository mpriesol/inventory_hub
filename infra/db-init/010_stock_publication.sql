-- Controlled maintenance publication, disabled by default. No stock data changes.
CREATE TABLE IF NOT EXISTS stock_publication_policies (
    shop_id BIGINT PRIMARY KEY REFERENCES shops(id) ON DELETE RESTRICT,
    enabled BOOLEAN NOT NULL DEFAULT false,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    target_fingerprint VARCHAR(64),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (NOT enabled OR target_fingerprint IS NOT NULL)
);
CREATE TABLE IF NOT EXISTS stock_publication_holds (
    id VARCHAR(36) PRIMARY KEY,
    shop_id BIGINT NOT NULL REFERENCES shops(id) ON DELETE RESTRICT,
    warehouse_id BIGINT NOT NULL REFERENCES warehouses(id) ON DELETE RESTRICT,
    active BOOLEAN NOT NULL DEFAULT true,
    assertions JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at TIMESTAMPTZ,
    close_result JSONB
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_stock_publication_active_hold
    ON stock_publication_holds(warehouse_id) WHERE active = true;
CREATE TABLE IF NOT EXISTS stock_publication_batches (
    id VARCHAR(36) PRIMARY KEY,
    shop_id BIGINT NOT NULL REFERENCES shops(id) ON DELETE RESTRICT,
    hold_id VARCHAR(36) NOT NULL REFERENCES stock_publication_holds(id) ON DELETE RESTRICT,
    request_hash VARCHAR(64) NOT NULL,
    preview_hash VARCHAR(64) NOT NULL,
    preview_data JSONB NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'prepared'
        CHECK (status IN ('prepared','queued','running','completed','blocked','cancelled','uncertain')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    queued_at TIMESTAMPTZ,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    error VARCHAR(80),
    result JSONB
);
CREATE INDEX IF NOT EXISTS ix_stock_publication_batches_shop
    ON stock_publication_batches(shop_id, created_at);
CREATE TABLE IF NOT EXISTS stock_publication_items (
    id BIGSERIAL PRIMARY KEY,
    batch_id VARCHAR(36) NOT NULL REFERENCES stock_publication_batches(id) ON DELETE RESTRICT,
    position INTEGER NOT NULL CHECK (position >= 0),
    sku VARCHAR(100) NOT NULL,
    target JSONB NOT NULL,
    quantity VARCHAR(32) NOT NULL,
    before_quantity VARCHAR(32),
    after_quantity VARCHAR(32),
    status VARCHAR(16) NOT NULL DEFAULT 'prepared'
        CHECK (status IN ('prepared','sending','verified','failed','uncertain','cancelled')),
    attempt_id VARCHAR(36),
    attempt_started_at TIMESTAMPTZ,
    attempt_completed_at TIMESTAMPTZ,
    verified_at TIMESTAMPTZ,
    error VARCHAR(80),
    observation JSONB,
    acknowledgement JSONB,
    resolution JSONB,
    CONSTRAINT uq_stock_publication_item_position UNIQUE(batch_id, position),
    CONSTRAINT uq_stock_publication_item_sku UNIQUE(batch_id, sku)
);
