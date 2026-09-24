-- Persist successful scan operations atomically with their quantity increment.
-- NULL/legacy request IDs are not represented and retain their old behavior.
CREATE TABLE IF NOT EXISTS receiving_scan_requests (
    request_id UUID PRIMARY KEY,
    session_id BIGINT NOT NULL REFERENCES receiving_sessions(id) ON DELETE RESTRICT,
    scan_event_id BIGINT NOT NULL UNIQUE REFERENCES scan_events(id) ON DELETE RESTRICT,
    request_hash VARCHAR(64) NOT NULL,
    response JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Supplier invoice EANs may contain several barcodes separated by delimiters.
-- Widen only the supported legacy width; preserve all stored values and history.
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = 'receiving_lines'
        AND column_name = 'ean' AND character_maximum_length < 255) THEN
        -- This generated helper depends on EAN's type. Rebuild only this
        -- derived value with its original expression, in the same transaction.
        -- RESTRICT is deliberate: unknown dependencies must abort, never CASCADE.
        ALTER TABLE receiving_lines DROP COLUMN line_fingerprint;
        ALTER TABLE receiving_lines ALTER COLUMN ean TYPE VARCHAR(255);
        ALTER TABLE receiving_lines ADD COLUMN line_fingerprint VARCHAR(32)
            GENERATED ALWAYS AS (
                md5(COALESCE(ean, '') || '|' || COALESCE(supplier_sku, '') || '|' ||
                    ordered_qty::TEXT || '|' || COALESCE(unit_price::TEXT, ''))
            ) STORED;
    END IF;
END $$;
