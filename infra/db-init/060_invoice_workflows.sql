ALTER TABLE uploaded_invoices ADD COLUMN IF NOT EXISTS payment_assumed_unpaid BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE uploaded_invoices ADD COLUMN IF NOT EXISTS payment_needs_review BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE uploaded_invoices ADD COLUMN IF NOT EXISTS issuer_name VARCHAR(255);
ALTER TABLE uploaded_invoices ADD COLUMN IF NOT EXISTS issuer_key VARCHAR(255);
ALTER TABLE uploaded_invoices ADD COLUMN IF NOT EXISTS is_service_invoice BOOLEAN NOT NULL DEFAULT FALSE;

-- Keep the constraint name checked by migration 033. The runner reapplies
-- earlier migrations on each deployment, including after service uploads.
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='uploaded_invoices'::regclass
                 AND conname='ck_invoice_archive_owner'
                 AND pg_get_constraintdef(oid) LIKE '%is_service_invoice%') THEN
    ALTER TABLE uploaded_invoices ADD CONSTRAINT ck_invoice_archive_owner_with_service CHECK (
      (direction='purchase' AND shop_id IS NULL AND
        ((supplier_id IS NOT NULL AND NOT is_service_invoice) OR
         (supplier_id IS NULL AND is_service_invoice AND issuer_name IS NOT NULL AND
           length(btrim(issuer_name)) > 0 AND issuer_key IS NOT NULL
           AND receiving_session_id IS NULL)))
      OR (direction='sale' AND supplier_id IS NULL AND shop_id IS NOT NULL AND NOT is_service_invoice));
    ALTER TABLE uploaded_invoices DROP CONSTRAINT IF EXISTS ck_invoice_archive_owner;
    ALTER TABLE uploaded_invoices RENAME CONSTRAINT ck_invoice_archive_owner_with_service TO ck_invoice_archive_owner;
  END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_invoice_service_source
  ON uploaded_invoices(issuer_key,source_key) WHERE is_service_invoice AND source_key IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_invoice_service_number
  ON uploaded_invoices(issuer_key,invoice_number) WHERE is_service_invoice AND invoice_number IS NOT NULL;

ALTER TABLE invoice_payments ADD COLUMN IF NOT EXISTS method TEXT
  CHECK(method IN ('cash','card_slsp','card_tb','transfer_slsp','transfer_tb'));

CREATE TABLE IF NOT EXISTS invoice_batch_payment_operations (
  request_id UUID PRIMARY KEY,
  payload_hash VARCHAR(64) NOT NULL,
  result JSONB NOT NULL,
  actor TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
