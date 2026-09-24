-- No schedules or stock authority are activated by this migration.
CREATE TABLE IF NOT EXISTS stock_sync_warehouse_settings (
 warehouse_id BIGINT PRIMARY KEY REFERENCES warehouses(id) ON DELETE RESTRICT,
 revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
 interval_seconds INTEGER NOT NULL DEFAULT 300 CHECK (interval_seconds BETWEEN 60 AND 86400),
 batch_size INTEGER NOT NULL DEFAULT 20 CHECK (batch_size BETWEEN 1 AND 100),
 max_order_age_seconds INTEGER NOT NULL DEFAULT 900 CHECK (max_order_age_seconds BETWEEN 60 AND 86400),
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS stock_sync_settings (
 shop_id BIGINT PRIMARY KEY REFERENCES shops(id) ON DELETE RESTRICT,
 warehouse_id BIGINT NOT NULL REFERENCES warehouses(id) ON DELETE RESTRICT,
 revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
 enabled BOOLEAN NOT NULL DEFAULT FALSE, authorized BOOLEAN NOT NULL DEFAULT FALSE,
 interval_seconds INTEGER CHECK (interval_seconds BETWEEN 60 AND 86400),
 batch_size INTEGER CHECK (batch_size BETWEEN 1 AND 100),
 max_order_age_seconds INTEGER CHECK (max_order_age_seconds BETWEEN 60 AND 86400),
 target_fingerprint VARCHAR(64), authority_confirmed_at TIMESTAMPTZ, authority_snapshot JSONB,
 next_run_at TIMESTAMPTZ, last_started_at TIMESTAMPTZ, last_completed_at TIMESTAMPTZ,
 last_error VARCHAR(100), retry_after_at TIMESTAMPTZ,
 CHECK (NOT enabled OR authorized)
);
CREATE TABLE IF NOT EXISTS stock_sync_runs (
 id VARCHAR(36) PRIMARY KEY, shop_id BIGINT NOT NULL REFERENCES shops(id) ON DELETE RESTRICT,
 warehouse_id BIGINT NOT NULL REFERENCES warehouses(id) ON DELETE RESTRICT,
 target_fingerprint VARCHAR(64) NOT NULL, settings_revision INTEGER NOT NULL,
 trigger VARCHAR(16) NOT NULL CHECK (trigger IN ('manual','automatic')),
 status VARCHAR(16) NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','completed','partial','failed','uncertain')),
 selected_skus JSONB, cursor_product_id BIGINT NOT NULL DEFAULT 0, max_product_id BIGINT NOT NULL DEFAULT 0,
 started_at TIMESTAMPTZ NOT NULL DEFAULT now(), completed_at TIMESTAMPTZ, last_batch_at TIMESTAMPTZ, error VARCHAR(100)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_stock_sync_active_run ON stock_sync_runs(shop_id) WHERE status IN ('queued','running');
CREATE INDEX IF NOT EXISTS ix_stock_sync_runs_shop ON stock_sync_runs(shop_id,started_at);
CREATE TABLE IF NOT EXISTS stock_sync_items (
 id BIGSERIAL PRIMARY KEY, run_id VARCHAR(36) NOT NULL REFERENCES stock_sync_runs(id) ON DELETE RESTRICT,
 shop_id BIGINT NOT NULL REFERENCES shops(id) ON DELETE RESTRICT,
 warehouse_id BIGINT NOT NULL REFERENCES warehouses(id) ON DELETE RESTRICT,
 sku VARCHAR(100) NOT NULL, target JSONB, desired JSONB, before JSONB, after JSONB,
 status VARCHAR(16) NOT NULL DEFAULT 'prepared' CHECK (status IN ('prepared','sending','verified','skipped','failed','uncertain')),
 error VARCHAR(100), attempt_started_at TIMESTAMPTZ, verified_at TIMESTAMPTZ, resolution JSONB,
 UNIQUE(run_id,sku)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_stock_sync_target_fence ON stock_sync_items(shop_id,sku) WHERE status IN ('sending','uncertain');
CREATE INDEX IF NOT EXISTS ix_stock_sync_items_run ON stock_sync_items(run_id,id);
