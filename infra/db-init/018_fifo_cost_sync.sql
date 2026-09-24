-- Additive: no existing costs, stock, orders or opt-in flags are changed.
CREATE TABLE IF NOT EXISTS fifo_cost_warehouse_settings (
    warehouse_id BIGINT PRIMARY KEY REFERENCES warehouses(id) ON DELETE RESTRICT,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    interval_seconds INTEGER NOT NULL DEFAULT 300 CHECK (interval_seconds BETWEEN 60 AND 86400),
    batch_size INTEGER NOT NULL DEFAULT 20 CHECK (batch_size BETWEEN 1 AND 100)
);
CREATE TABLE IF NOT EXISTS fifo_cost_settings (
    shop_id BIGINT PRIMARY KEY REFERENCES shops(id) ON DELETE RESTRICT,
    warehouse_id BIGINT NOT NULL REFERENCES warehouses(id) ON DELETE RESTRICT,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    product_cost_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    order_cost_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    interval_seconds INTEGER CHECK (interval_seconds BETWEEN 60 AND 86400),
    batch_size INTEGER CHECK (batch_size BETWEEN 1 AND 100),
    target_fingerprint VARCHAR(64) NOT NULL,
    orders_since TIMESTAMPTZ,
    next_run_at TIMESTAMPTZ, retry_after_at TIMESTAMPTZ, last_completed_at TIMESTAMPTZ, last_batch_at TIMESTAMPTZ,
    last_error VARCHAR(100),
    scan_active BOOLEAN NOT NULL DEFAULT FALSE,
    scan_manual BOOLEAN NOT NULL DEFAULT FALSE,
    product_cursor BIGINT NOT NULL DEFAULT 0, product_max BIGINT NOT NULL DEFAULT 0,
    order_cursor BIGINT NOT NULL DEFAULT 0, order_max BIGINT NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS fifo_cost_publications (
    id VARCHAR(36) PRIMARY KEY,
    shop_id BIGINT NOT NULL REFERENCES shops(id) ON DELETE RESTRICT,
    warehouse_id BIGINT NOT NULL REFERENCES warehouses(id) ON DELETE RESTRICT,
    kind VARCHAR(16) NOT NULL CHECK (kind IN ('product','order')),
    target_key VARCHAR(100) NOT NULL, subject VARCHAR(100) NOT NULL,
    status VARCHAR(16) NOT NULL CHECK (status IN ('prepared','queued','sending','verified','uncertain','failed','skipped','resolved')),
    automatic BOOLEAN NOT NULL DEFAULT FALSE,
    settings_revision INTEGER NOT NULL,
    target_fingerprint VARCHAR(64) NOT NULL, source_hash VARCHAR(64) NOT NULL,
    source JSONB NOT NULL, target JSONB, document JSONB, before JSONB, after JSONB,
    error VARCHAR(100), created_at TIMESTAMPTZ NOT NULL DEFAULT now(), expires_at TIMESTAMPTZ,
    attempt_started_at TIMESTAMPTZ, verified_at TIMESTAMPTZ, resolution JSONB,
    CHECK (status NOT IN ('sending','uncertain','verified') OR document IS NOT NULL),
    CHECK (status NOT IN ('sending','uncertain') OR attempt_started_at IS NOT NULL)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_fifo_cost_target_fence ON fifo_cost_publications(shop_id,target_key)
    WHERE status IN ('queued','sending','uncertain');
CREATE INDEX IF NOT EXISTS idx_fifo_cost_history ON fifo_cost_publications(shop_id,created_at);
CREATE INDEX IF NOT EXISTS idx_fifo_cost_cache ON fifo_cost_publications(shop_id,target_key,source_hash);
