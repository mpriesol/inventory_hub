-- Controlled order reservations/issues. No orders or stock are backfilled.
ALTER TABLE stock_movements ADD COLUMN IF NOT EXISTS total_cost NUMERIC(16,4);
COMMENT ON COLUMN stock_movements.total_cost IS 'Explicit nonnegative cost consumed by a controlled SALE_OUT; historical values remain unknown (NULL)';
ALTER TABLE shop_orders ADD COLUMN IF NOT EXISTS stock_state VARCHAR(16) NOT NULL DEFAULT 'unmanaged';
ALTER TABLE shop_orders ADD COLUMN IF NOT EXISTS stock_revision INTEGER NOT NULL DEFAULT 0;
ALTER TABLE shop_orders ADD COLUMN IF NOT EXISTS stock_warehouse_id BIGINT REFERENCES warehouses(id) ON DELETE RESTRICT;
ALTER TABLE shop_orders ADD COLUMN IF NOT EXISTS stock_source_uuid VARCHAR(100);
ALTER TABLE shop_orders ADD COLUMN IF NOT EXISTS stock_snapshot JSONB;
ALTER TABLE shop_orders ADD COLUMN IF NOT EXISTS stock_source_hash VARCHAR(64);
ALTER TABLE shop_orders ADD COLUMN IF NOT EXISTS stock_issued_at TIMESTAMPTZ;
ALTER TABLE shop_orders ADD COLUMN IF NOT EXISTS stock_issue_result JSONB;
ALTER TABLE shop_order_items ADD COLUMN IF NOT EXISTS stock_managed BOOLEAN NOT NULL DEFAULT false;
-- This workflow does not import financial prices. NULL means unknown, never a fabricated zero.
ALTER TABLE shop_orders ALTER COLUMN currency DROP NOT NULL;
ALTER TABLE shop_order_items ALTER COLUMN unit_price DROP NOT NULL;
ALTER TABLE shop_order_items ALTER COLUMN total_price DROP NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_stock_order_item_key ON shop_order_items(order_id, external_item_id) WHERE stock_managed = true;
CREATE UNIQUE INDEX IF NOT EXISTS uq_stock_order_source_uuid ON shop_orders(shop_id, stock_source_uuid) WHERE stock_source_uuid IS NOT NULL;

CREATE TABLE IF NOT EXISTS order_stock_policies (
    shop_id BIGINT PRIMARY KEY REFERENCES shops(id) ON DELETE RESTRICT,
    warehouse_id BIGINT NOT NULL REFERENCES warehouses(id) ON DELETE RESTRICT,
    starts_at TIMESTAMPTZ NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    status_actions JSONB NOT NULL,
    status_hash VARCHAR(64) NOT NULL,
    statuses JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS order_stock_previews (
    id VARCHAR(36) PRIMARY KEY,
    shop_id BIGINT NOT NULL REFERENCES shops(id) ON DELETE RESTRICT,
    order_id BIGINT NOT NULL REFERENCES shop_orders(id) ON DELETE RESTRICT,
    request_hash VARCHAR(64) NOT NULL,
    preview_hash VARCHAR(64) NOT NULL,
    preview_data JSONB NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'prepared',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    result JSONB,
    CONSTRAINT chk_order_stock_preview_status CHECK (status IN ('prepared', 'completed')),
    CONSTRAINT chk_order_stock_preview_result CHECK (
        (status = 'prepared' AND result IS NULL AND completed_at IS NULL) OR
        (status = 'completed' AND result IS NOT NULL AND completed_at IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS ix_order_stock_previews_order ON order_stock_previews(order_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_order_stock_previews_shop ON order_stock_previews(shop_id, created_at DESC);
