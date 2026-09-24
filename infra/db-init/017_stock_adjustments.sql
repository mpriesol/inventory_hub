-- Additive physical recount workflow. No balance, setting or history is changed.
CREATE TABLE IF NOT EXISTS stock_adjustments (
    id VARCHAR(36) PRIMARY KEY,
    product_id BIGINT NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
    warehouse_id BIGINT NOT NULL REFERENCES warehouses(id) ON DELETE RESTRICT,
    request_hash VARCHAR(64) NOT NULL,
    preview_hash VARCHAR(64) NOT NULL,
    preview_data JSONB NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'prepared' CHECK (status IN ('prepared', 'completed')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    result JSONB
);
CREATE INDEX IF NOT EXISTS idx_stock_adjustments_scope ON stock_adjustments(product_id, warehouse_id, created_at DESC);
