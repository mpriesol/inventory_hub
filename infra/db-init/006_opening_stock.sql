-- Additive, rerunnable opening-stock workflow. Existing stock/history is untouched.
CREATE TABLE IF NOT EXISTS opening_stock_batches (
    id VARCHAR(36) PRIMARY KEY,
    warehouse_id BIGINT NOT NULL REFERENCES warehouses(id) ON DELETE RESTRICT,
    warehouse_code VARCHAR(50) NOT NULL,
    source_reference VARCHAR(255) NOT NULL,
    operator_name VARCHAR(100) NOT NULL,
    counted_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    status VARCHAR(16) NOT NULL DEFAULT 'prepared',
    input_hash VARCHAR(64) NOT NULL,
    preview_hash VARCHAR(64) NOT NULL,
    preview_data JSONB NOT NULL,
    result JSONB,
    CONSTRAINT chk_opening_batch_status CHECK (status IN ('prepared', 'completed')),
    CONSTRAINT chk_opening_batch_expiry CHECK (expires_at > created_at),
    CONSTRAINT chk_opening_batch_result CHECK (
        (status = 'prepared' AND completed_at IS NULL AND result IS NULL) OR
        (status = 'completed' AND completed_at IS NOT NULL AND result IS NOT NULL)
    ),
    CONSTRAINT chk_opening_input_hash CHECK (input_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT chk_opening_preview_hash CHECK (preview_hash ~ '^[0-9a-f]{64}$')
);
CREATE INDEX IF NOT EXISTS ix_opening_batches_created ON opening_stock_batches(created_at DESC, id);
CREATE INDEX IF NOT EXISTS ix_opening_batches_warehouse ON opening_stock_batches(warehouse_id);

CREATE TABLE IF NOT EXISTS opening_stock_lines (
    id BIGSERIAL PRIMARY KEY,
    batch_id VARCHAR(36) NOT NULL REFERENCES opening_stock_batches(id) ON DELETE RESTRICT,
    line_number INTEGER NOT NULL,
    product_id BIGINT NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
    sku VARCHAR(100) NOT NULL,
    quantity NUMERIC(12,3) NOT NULL,
    unit_cost NUMERIC(12,4) NOT NULL,
    value NUMERIC(16,4) NOT NULL,
    unit VARCHAR(2) NOT NULL,
    movement_id BIGINT UNIQUE REFERENCES stock_movements(id) ON DELETE RESTRICT,
    CONSTRAINT uq_opening_line_number UNIQUE (batch_id, line_number),
    CONSTRAINT uq_opening_line_product UNIQUE (batch_id, product_id),
    CONSTRAINT chk_opening_line_number CHECK (line_number > 0),
    CONSTRAINT chk_opening_quantity CHECK (quantity > 0 AND quantity <= 999999999 AND quantity = trunc(quantity)),
    CONSTRAINT chk_opening_cost CHECK (unit_cost >= 0),
    CONSTRAINT chk_opening_unit CHECK (unit = 'ks'),
    CONSTRAINT chk_opening_value CHECK (value >= 0 AND value = round(quantity * unit_cost, 4))
);
CREATE INDEX IF NOT EXISTS ix_opening_lines_product ON opening_stock_lines(product_id);
