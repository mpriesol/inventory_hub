-- Canonical archive of externally issued purchase and sales documents.
-- Requires 001, 002 and 003. No document is issued and no stock is changed.
ALTER TABLE uploaded_invoices ALTER COLUMN supplier_id DROP NOT NULL;
ALTER TABLE uploaded_invoices ADD COLUMN IF NOT EXISTS direction VARCHAR(10) NOT NULL DEFAULT 'purchase';
ALTER TABLE uploaded_invoices ADD COLUMN IF NOT EXISTS shop_id BIGINT REFERENCES shops(id) ON DELETE RESTRICT;
ALTER TABLE uploaded_invoices ADD COLUMN IF NOT EXISTS source_kind VARCHAR(16) NOT NULL DEFAULT 'upload';
ALTER TABLE uploaded_invoices ADD COLUMN IF NOT EXISTS source_key TEXT;
ALTER TABLE uploaded_invoices ADD COLUMN IF NOT EXISTS source_hash VARCHAR(64);
ALTER TABLE uploaded_invoices ADD COLUMN IF NOT EXISTS source_data JSONB NOT NULL DEFAULT '{}';
ALTER TABLE uploaded_invoices ADD COLUMN IF NOT EXISTS source_seen_at TIMESTAMPTZ;
ALTER TABLE uploaded_invoices ADD COLUMN IF NOT EXISTS document_type VARCHAR(16) NOT NULL DEFAULT 'invoice';
ALTER TABLE uploaded_invoices ADD COLUMN IF NOT EXISTS source_payment_status TEXT;
ALTER TABLE uploaded_invoices ADD COLUMN IF NOT EXISTS source_paid_amount NUMERIC(14,2);
ALTER TABLE uploaded_invoices ADD COLUMN IF NOT EXISTS source_paid_at TIMESTAMPTZ;
ALTER TABLE uploaded_invoices ADD COLUMN IF NOT EXISTS source_paid_date DATE;
ALTER TABLE uploaded_invoices ADD COLUMN IF NOT EXISTS payment_revision BIGINT NOT NULL DEFAULT 0;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='uploaded_invoices'::regclass AND conname='ck_invoice_archive_owner') THEN
    ALTER TABLE uploaded_invoices ADD CONSTRAINT ck_invoice_archive_owner CHECK (
      (direction='purchase' AND supplier_id IS NOT NULL AND shop_id IS NULL)
      OR (direction='sale' AND supplier_id IS NULL AND shop_id IS NOT NULL));
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='uploaded_invoices'::regclass AND conname='ck_invoice_archive_source') THEN
    ALTER TABLE uploaded_invoices ADD CONSTRAINT ck_invoice_archive_source CHECK (
      source_kind IN ('upload','supplier','shop') AND document_type IN ('invoice','credit_note','receipt'));
  END IF;
END $$;
CREATE UNIQUE INDEX IF NOT EXISTS uq_invoice_archive_supplier_source
  ON uploaded_invoices(source_kind,supplier_id,source_key) WHERE direction='purchase' AND source_key IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_invoice_archive_shop_source
  ON uploaded_invoices(source_kind,shop_id,source_key) WHERE direction='sale' AND source_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_invoice_archive_direction_date ON uploaded_invoices(direction,invoice_date,id);
ALTER TABLE uploaded_invoice_lines ALTER COLUMN ean TYPE VARCHAR(255);
ALTER TABLE uploaded_invoice_lines ADD COLUMN IF NOT EXISTS barcode_values TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE uploaded_invoice_lines ADD COLUMN IF NOT EXISTS source_line_key TEXT;
CREATE INDEX IF NOT EXISTS idx_invoice_line_barcodes ON uploaded_invoice_lines USING GIN(barcode_values);
CREATE INDEX IF NOT EXISTS idx_invoice_line_product ON uploaded_invoice_lines(matched_product_id) WHERE matched_product_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS invoice_files (
  id BIGSERIAL PRIMARY KEY,
  invoice_id BIGINT NOT NULL REFERENCES uploaded_invoices(id) ON DELETE RESTRICT,
  role VARCHAR(16) NOT NULL CHECK(role IN ('original','parsed')),
  filename TEXT NOT NULL,
  file_path TEXT NOT NULL,
  file_type VARCHAR(50),
  sha256 VARCHAR(64) NOT NULL,
  size_bytes BIGINT NOT NULL CHECK(size_bytes>=0),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(invoice_id,sha256)
);

CREATE TABLE IF NOT EXISTS invoice_payment_operations (
  request_id UUID PRIMARY KEY,
  invoice_id BIGINT NOT NULL REFERENCES uploaded_invoices(id) ON DELETE RESTRICT,
  payload_hash VARCHAR(64) NOT NULL,
  result JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS invoice_payments (
  id BIGSERIAL PRIMARY KEY,
  invoice_id BIGINT NOT NULL REFERENCES uploaded_invoices(id) ON DELETE RESTRICT,
  kind TEXT NOT NULL CHECK(kind IN ('opening','payment','refund','reversal')),
  amount NUMERIC(14,2) NOT NULL,
  currency VARCHAR(3) NOT NULL,
  paid_at TIMESTAMPTZ,
  note TEXT,
  actor TEXT NOT NULL,
  reverses_payment_id BIGINT REFERENCES invoice_payments(id) ON DELETE RESTRICT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CHECK((kind='reversal')=(reverses_payment_id IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_invoice_payment_reversal ON invoice_payments(reverses_payment_id) WHERE reverses_payment_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_invoice_payment_opening ON invoice_payments(invoice_id) WHERE kind='opening';
CREATE INDEX IF NOT EXISTS idx_invoice_payments_invoice ON invoice_payments(invoice_id,id);
CREATE OR REPLACE FUNCTION invoice_payment_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'invoice_payment_immutable';
END $$;
DROP TRIGGER IF EXISTS invoice_payment_immutable ON invoice_payments;
CREATE TRIGGER invoice_payment_immutable BEFORE UPDATE OR DELETE ON invoice_payments
  FOR EACH ROW EXECUTE FUNCTION invoice_payment_immutable();

COMMENT ON TABLE uploaded_invoices IS 'Canonical archive of documents issued externally; never a stock movement or Hub invoice issuer.';
COMMENT ON COLUMN uploaded_invoices.source_hash IS 'Immutable source facts hash. A changed source conflicts; payment observations are separate.';
COMMENT ON COLUMN uploaded_invoice_lines.barcode_values IS 'Verified invoice barcodes and normalized GTIN keys for indexed traceability; not inferred catalog ownership.';
