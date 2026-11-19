ALTER TABLE supplier_availability_settings
    ADD COLUMN IF NOT EXISTS missing_visibility VARCHAR(8) NOT NULL DEFAULT 'keep'
        CHECK (missing_visibility IN ('keep', 'archive')),
    ADD COLUMN IF NOT EXISTS missing_availability VARCHAR(100) NOT NULL DEFAULT 'overíme';

CREATE TABLE IF NOT EXISTS supplier_availability_changes (
    id BIGSERIAL PRIMARY KEY,
    supplier_id BIGINT NOT NULL REFERENCES suppliers(id) ON DELETE RESTRICT,
    supplier_sku VARCHAR(100) NOT NULL,
    feed_id BIGINT NOT NULL REFERENCES supplier_feeds(id) ON DELETE RESTRICT,
    changed_at TIMESTAMPTZ NOT NULL,
    before_state VARCHAR(16),
    after_state VARCHAR(16) NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_supplier_availability_changes_source
    ON supplier_availability_changes (supplier_id, supplier_sku, changed_at DESC, id DESC);

CREATE OR REPLACE FUNCTION supplier_availability_change_state(value BOOLEAN, evidence JSONB)
RETURNS TEXT LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE WHEN evidence->>'source' = 'supplier_feed_absence' THEN 'missing'
                WHEN evidence->>'source' = 'husky_public_stock' THEN 'unknown'
                WHEN value IS TRUE THEN 'available'
                WHEN value IS FALSE THEN 'unavailable'
                ELSE 'unknown' END
$$;

CREATE OR REPLACE FUNCTION record_supplier_availability_change()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    previous TEXT;
    current_state TEXT;
BEGIN
    IF TG_OP = 'UPDATE' AND OLD.feed_id = NEW.feed_id THEN
        previous := supplier_availability_change_state(OLD.available, OLD.raw);
    END IF;
    current_state := supplier_availability_change_state(NEW.available, NEW.raw);
    IF previous IS DISTINCT FROM current_state THEN
        INSERT INTO supplier_availability_changes
            (supplier_id, supplier_sku, feed_id, changed_at, before_state, after_state)
        VALUES (NEW.supplier_id, NEW.supplier_sku, NEW.feed_id, NEW.observed_at, previous, current_state);
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS supplier_availability_record_change ON supplier_availability_observations;
CREATE TRIGGER supplier_availability_record_change
    AFTER INSERT OR UPDATE OF available, raw, feed_id ON supplier_availability_observations
    FOR EACH ROW EXECUTE FUNCTION record_supplier_availability_change();
