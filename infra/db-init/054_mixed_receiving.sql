-- Draft scan allocations are separate from confirmed physical receipts.
CREATE TABLE IF NOT EXISTS mixed_receiving_batches (
 id UUID PRIMARY KEY,
 warehouse_id BIGINT NOT NULL REFERENCES warehouses(id) ON DELETE RESTRICT,
 status VARCHAR(20) NOT NULL DEFAULT 'open' CHECK (status IN ('open','completed')),
 actor VARCHAR(100) NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 completed_at TIMESTAMPTZ,
 result JSONB
);
CREATE TABLE IF NOT EXISTS mixed_receiving_scans (
 id UUID PRIMARY KEY,
 batch_id UUID NOT NULL REFERENCES mixed_receiving_batches(id) ON DELETE RESTRICT,
 code VARCHAR(100) NOT NULL,
 quantity NUMERIC(12,3) NOT NULL CHECK (quantity > 0),
 session_id BIGINT NOT NULL REFERENCES receiving_sessions(id) ON DELETE RESTRICT,
 line_id BIGINT NOT NULL REFERENCES receiving_lines(id) ON DELETE RESTRICT,
 posting_revision INTEGER NOT NULL,
 status VARCHAR(20) NOT NULL DEFAULT 'active' CHECK (status IN ('active','cancelled')),
 assignment_history JSONB NOT NULL DEFAULT '[]'::jsonb,
 candidates JSONB NOT NULL DEFAULT '[]'::jsonb,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_mixed_receiving_scans_batch ON mixed_receiving_scans(batch_id,created_at,id);
CREATE INDEX IF NOT EXISTS ix_mixed_receiving_scans_line ON mixed_receiving_scans(line_id) WHERE status='active';
CREATE TABLE IF NOT EXISTS mixed_receiving_requests (
 request_id UUID PRIMARY KEY,
 batch_id UUID NOT NULL REFERENCES mixed_receiving_batches(id) ON DELETE RESTRICT,
 payload_hash VARCHAR(64) NOT NULL,
 result JSONB NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS mixed_receiving_postings (
 batch_id UUID NOT NULL REFERENCES mixed_receiving_batches(id) ON DELETE RESTRICT,
 session_id BIGINT NOT NULL REFERENCES receiving_sessions(id) ON DELETE RESTRICT,
 line_id BIGINT NOT NULL REFERENCES receiving_lines(id) ON DELETE RESTRICT,
 posting_revision INTEGER NOT NULL,
 purchase_quantity NUMERIC(12,3) NOT NULL CHECK (purchase_quantity > 0),
 movement_id BIGINT NOT NULL UNIQUE REFERENCES stock_movements(id) ON DELETE RESTRICT,
 PRIMARY KEY (batch_id,line_id)
);
CREATE INDEX IF NOT EXISTS ix_mixed_receiving_postings_session ON mixed_receiving_postings(session_id,posting_revision);
CREATE OR REPLACE FUNCTION mixed_receiving_immutable() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'Mixed receiving posting and request history is immutable'; END $$;
DO $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_trigger WHERE tgname='mixed_receiving_postings_immutable' AND tgrelid='mixed_receiving_postings'::regclass) THEN
  CREATE TRIGGER mixed_receiving_postings_immutable BEFORE UPDATE OR DELETE ON mixed_receiving_postings
   FOR EACH ROW EXECUTE FUNCTION mixed_receiving_immutable();
 END IF;
 IF NOT EXISTS(SELECT 1 FROM pg_trigger WHERE tgname='mixed_receiving_requests_immutable' AND tgrelid='mixed_receiving_requests'::regclass) THEN
  CREATE TRIGGER mixed_receiving_requests_immutable BEFORE UPDATE OR DELETE ON mixed_receiving_requests
   FOR EACH ROW EXECUTE FUNCTION mixed_receiving_immutable();
 END IF;
END $$;
