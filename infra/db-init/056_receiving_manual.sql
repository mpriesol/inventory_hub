-- Reversible presentation cleanup retains immutable requests and stock history.
ALTER TABLE mixed_receiving_batches ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ;
ALTER TABLE mixed_receiving_batches ADD COLUMN IF NOT EXISTS archive_reason VARCHAR(255);
ALTER TABLE mixed_receiving_batches ADD COLUMN IF NOT EXISTS archived_by VARCHAR(100);

-- Manual exceptions have a real stock identity, never a fabricated invoice.
CREATE TABLE IF NOT EXISTS mixed_receiving_manual_scans (
 id UUID PRIMARY KEY,
 batch_id UUID NOT NULL REFERENCES mixed_receiving_batches(id) ON DELETE RESTRICT,
 product_id BIGINT NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
 code VARCHAR(100) NOT NULL,
 quantity NUMERIC(12,3) NOT NULL CHECK (quantity > 0),
 stock_unit VARCHAR(20) NOT NULL,
 unit_cost NUMERIC(12,4),
 cost_status VARCHAR(20) NOT NULL CHECK (cost_status IN ('known','provisional','unknown')),
 reason VARCHAR(255) NOT NULL,
 status VARCHAR(20) NOT NULL DEFAULT 'active' CHECK (status IN ('active','cancelled')),
 assignment_history JSONB NOT NULL DEFAULT '[]'::jsonb,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 CHECK ((cost_status='unknown' AND unit_cost IS NULL) OR
        (cost_status<>'unknown' AND unit_cost IS NOT NULL AND unit_cost>=0))
);
CREATE INDEX IF NOT EXISTS ix_mixed_receiving_manual_batch ON mixed_receiving_manual_scans(batch_id,created_at,id);
CREATE TABLE IF NOT EXISTS mixed_receiving_manual_postings (
 scan_id UUID PRIMARY KEY REFERENCES mixed_receiving_manual_scans(id) ON DELETE RESTRICT,
 movement_id BIGINT NOT NULL UNIQUE REFERENCES stock_movements(id) ON DELETE RESTRICT
);
DO $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_trigger WHERE tgname='mixed_receiving_manual_postings_immutable'
   AND tgrelid='mixed_receiving_manual_postings'::regclass) THEN
  CREATE TRIGGER mixed_receiving_manual_postings_immutable BEFORE UPDATE OR DELETE ON mixed_receiving_manual_postings
   FOR EACH ROW EXECUTE FUNCTION mixed_receiving_immutable();
 END IF;
END $$;
