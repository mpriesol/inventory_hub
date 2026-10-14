-- Existing authority never silently upgrades to a different stock protocol.
ALTER TABLE stock_sync_settings ADD COLUMN IF NOT EXISTS protocol VARCHAR(32) NOT NULL DEFAULT 'legacy_absolute';
ALTER TABLE stock_sync_runs ADD COLUMN IF NOT EXISTS protocol VARCHAR(32) NOT NULL DEFAULT 'legacy_absolute';
ALTER TABLE stock_sync_items ADD COLUMN IF NOT EXISTS delta_snapshot JSONB;
CREATE TABLE IF NOT EXISTS stock_sync_checkpoints (
 shop_id BIGINT NOT NULL REFERENCES shops(id) ON DELETE RESTRICT,
 sku VARCHAR(100) NOT NULL,
 product_id BIGINT NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
 warehouse_id BIGINT NOT NULL REFERENCES warehouses(id) ON DELETE RESTRICT,
 revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
 target_fingerprint VARCHAR(64) NOT NULL, target JSONB NOT NULL,
 local_quantity NUMERIC(24,0) NOT NULL CHECK (local_quantity >= 0),
 native_quantity NUMERIC(24,0) NOT NULL,
 metadata_values JSONB NOT NULL,
 hold_id VARCHAR(36) NOT NULL REFERENCES stock_publication_holds(id) ON DELETE RESTRICT,
 baseline_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 last_audit_at TIMESTAMPTZ NOT NULL DEFAULT now(), last_observation JSONB,
 PRIMARY KEY(shop_id,sku)
);
CREATE INDEX IF NOT EXISTS ix_stock_sync_checkpoint_product ON stock_sync_checkpoints(shop_id,warehouse_id,product_id);
CREATE TABLE IF NOT EXISTS stock_sync_baseline_events (
 id BIGSERIAL PRIMARY KEY,
 shop_id BIGINT NOT NULL REFERENCES shops(id) ON DELETE RESTRICT,
 hold_id VARCHAR(36) NOT NULL REFERENCES stock_publication_holds(id) ON DELETE RESTRICT,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 evidence JSONB NOT NULL
);

CREATE OR REPLACE FUNCTION stock_sync_baseline_event_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'stock_sync_baseline_events are immutable';
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS stock_sync_baseline_event_immutable ON stock_sync_baseline_events;
CREATE TRIGGER stock_sync_baseline_event_immutable BEFORE UPDATE OR DELETE ON stock_sync_baseline_events
FOR EACH ROW EXECUTE FUNCTION stock_sync_baseline_event_immutable();
