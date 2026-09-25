-- An empty registry intentionally leaves every pre-existing balance unconfirmed.
-- No products, balances, layers or immutable movements are changed by deployment.
CREATE TABLE IF NOT EXISTS stock_tracking (
 product_id BIGINT NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
 warehouse_id BIGINT NOT NULL REFERENCES warehouses(id) ON DELETE RESTRICT,
 started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 source_type VARCHAR(32) NOT NULL,
 source_id VARCHAR(100) NOT NULL,
 operator_name VARCHAR(100) NOT NULL,
 historical_last_movement_id BIGINT NOT NULL DEFAULT 0,
 historical_last_layer_id BIGINT NOT NULL DEFAULT 0,
 archived_balance JSONB NOT NULL,
 PRIMARY KEY (product_id, warehouse_id)
);
COMMENT ON TABLE stock_tracking IS 'Explicit start of current physical stock; prior balances and movements are historical, never inferred to be current stock.';
