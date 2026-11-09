-- Explicit invoice handling and reversible archival. No stock or payment writes.
ALTER TABLE uploaded_invoices ADD COLUMN IF NOT EXISTS processing_revision BIGINT NOT NULL DEFAULT 0;
ALTER TABLE uploaded_invoices ADD COLUMN IF NOT EXISTS processing_status TEXT NOT NULL DEFAULT 'unprocessed';
ALTER TABLE uploaded_invoices ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='uploaded_invoices'::regclass AND conname='ck_invoice_processing_state') THEN
    ALTER TABLE uploaded_invoices ADD CONSTRAINT ck_invoice_processing_state CHECK (
      processing_revision >= 0 AND processing_status IN ('unprocessed','processed_without_stock'));
  END IF;
END $$;
CREATE INDEX IF NOT EXISTS idx_invoice_active_direction_date
  ON uploaded_invoices(direction,invoice_date,id) WHERE archived_at IS NULL;

CREATE TABLE IF NOT EXISTS invoice_processing_operations (
  request_id UUID PRIMARY KEY,
  invoice_id BIGINT NOT NULL REFERENCES uploaded_invoices(id) ON DELETE RESTRICT,
  revision BIGINT NOT NULL CHECK(revision>0),
  action TEXT NOT NULL CHECK(action IN ('process_without_stock','reopen','archive','restore')),
  note TEXT NOT NULL CHECK(length(btrim(note)) BETWEEN 1 AND 2000),
  actor TEXT NOT NULL,
  payload_hash VARCHAR(64) NOT NULL,
  before_data JSONB NOT NULL,
  after_data JSONB NOT NULL,
  result JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(invoice_id,revision)
);
CREATE OR REPLACE FUNCTION invoice_processing_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'invoice_processing_immutable';
END $$;
DROP TRIGGER IF EXISTS invoice_processing_immutable ON invoice_processing_operations;
CREATE TRIGGER invoice_processing_immutable BEFORE UPDATE OR DELETE ON invoice_processing_operations
  FOR EACH ROW EXECUTE FUNCTION invoice_processing_immutable();

COMMENT ON COLUMN uploaded_invoices.processing_status IS 'Manual document handling only. A posted receipt remains a separate stock fact.';
COMMENT ON COLUMN uploaded_invoices.archived_at IS 'Reversible exclusion from active invoice views; original files, receipts and settlements remain intact.';
COMMENT ON TABLE invoice_processing_operations IS 'Immutable reasoned invoice handling/archival audit and exact UUID replay results; never changes stock or payments.';

-- A malformed local source may have no invoice row yet. Retire its exact
-- supplier-scoped identity without replacing its document or inventing facts.
CREATE TABLE IF NOT EXISTS invoice_source_archive_events (
  id UUID PRIMARY KEY,
  supplier_id BIGINT NOT NULL REFERENCES suppliers(id) ON DELETE RESTRICT,
  source_key TEXT NOT NULL CHECK(length(btrim(source_key)) BETWEEN 1 AND 500),
  action TEXT NOT NULL CHECK(action IN ('archive','restore')),
  note TEXT NOT NULL CHECK(length(btrim(note)) BETWEEN 1 AND 2000),
  actor TEXT NOT NULL,
  request_hash VARCHAR(64) NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS idx_invoice_source_archive_latest
  ON invoice_source_archive_events(supplier_id,source_key,created_at DESC,id DESC);
CREATE OR REPLACE FUNCTION invoice_source_archive_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'invoice_source_archive_immutable';
END $$;
DROP TRIGGER IF EXISTS invoice_source_archive_immutable ON invoice_source_archive_events;
CREATE TRIGGER invoice_source_archive_immutable BEFORE UPDATE OR DELETE ON invoice_source_archive_events
  FOR EACH ROW EXECUTE FUNCTION invoice_source_archive_immutable();
