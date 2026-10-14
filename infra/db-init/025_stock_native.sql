-- No historical inference, stock mutation or authority activation.
CREATE TABLE IF NOT EXISTS stock_native_events (
    id BIGSERIAL PRIMARY KEY,
    order_id BIGINT NOT NULL REFERENCES shop_orders(id) ON DELETE RESTRICT,
    revision INTEGER NOT NULL CHECK (revision > 0),
    source_hash VARCHAR(64) NOT NULL,
    facts JSONB NOT NULL,
    error VARCHAR(100),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (order_id, revision)
);
CREATE TABLE IF NOT EXISTS stock_native_orders (
    order_id BIGINT PRIMARY KEY REFERENCES shop_orders(id) ON DELETE RESTRICT,
    shop_id BIGINT NOT NULL REFERENCES shops(id) ON DELETE RESTRICT,
    warehouse_id BIGINT NOT NULL REFERENCES warehouses(id) ON DELETE RESTRICT,
    revision INTEGER NOT NULL CHECK (revision > 0),
    order_revision INTEGER NOT NULL CHECK (order_revision >= 0),
    event_id BIGINT NOT NULL REFERENCES stock_native_events(id) ON DELETE RESTRICT,
    source_hash VARCHAR(64) NOT NULL,
    source_updated_at TIMESTAMPTZ NOT NULL,
    status_id BIGINT NOT NULL,
    status_type VARCHAR(50) NOT NULL,
    canceled_seen BOOLEAN NOT NULL DEFAULT false,
    error VARCHAR(100)
);
CREATE INDEX IF NOT EXISTS ix_stock_native_scope ON stock_native_orders (shop_id, warehouse_id);
CREATE TABLE IF NOT EXISTS stock_native_lines (
    order_id BIGINT NOT NULL REFERENCES stock_native_orders(order_id) ON DELETE RESTRICT,
    product_id BIGINT NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
    quantity NUMERIC(24,0) NOT NULL CHECK (quantity <= 0),
    PRIMARY KEY (order_id, product_id)
);
CREATE INDEX IF NOT EXISTS ix_stock_native_product ON stock_native_lines (product_id, order_id);

CREATE OR REPLACE FUNCTION stock_native_event_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'stock_native_events are immutable';
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS stock_native_event_immutable ON stock_native_events;
CREATE TRIGGER stock_native_event_immutable BEFORE UPDATE OR DELETE ON stock_native_events
FOR EACH ROW EXECUTE FUNCTION stock_native_event_immutable();
