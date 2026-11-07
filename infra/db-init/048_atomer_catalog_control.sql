-- Reversible operator pause. No job, intent, receipt, quota or stock is changed.
ALTER TABLE atomer_catalog_campaigns
    ADD COLUMN IF NOT EXISTS operator_paused boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS operator_control_revision bigint NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS operator_pause_previous_state text;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conrelid='atomer_catalog_campaigns'::regclass
                     AND conname='ck_atomer_catalog_operator_control') THEN
        ALTER TABLE atomer_catalog_campaigns ADD CONSTRAINT ck_atomer_catalog_operator_control CHECK (
            operator_control_revision >= 0 AND
            (NOT operator_paused OR state='paused') AND
            (operator_paused = (operator_pause_previous_state IS NOT NULL)) AND
            (operator_pause_previous_state IS NULL OR operator_pause_previous_state IN ('running','waiting','paused'))
        );
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS atomer_catalog_controls (
    request_id uuid PRIMARY KEY,
    campaign text NOT NULL REFERENCES atomer_catalog_campaigns(id),
    request_hash text NOT NULL CHECK (request_hash ~ '^[a-f0-9]{64}$'),
    action text NOT NULL CHECK (action IN ('pause','resume')),
    actor text NOT NULL CHECK (length(actor) BETWEEN 1 AND 200),
    status text NOT NULL DEFAULT 'completed' CHECK (status='completed'),
    request jsonb NOT NULL CHECK (jsonb_typeof(request)='object'),
    result jsonb NOT NULL CHECK (jsonb_typeof(result)='object'),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_atomer_catalog_control_history ON atomer_catalog_controls(campaign,created_at);

CREATE OR REPLACE FUNCTION atomer_catalog_control_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'catalog_control_immutable' USING ERRCODE='23514';
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS tr_atomer_catalog_control_immutable ON atomer_catalog_controls;
CREATE TRIGGER tr_atomer_catalog_control_immutable BEFORE UPDATE OR DELETE ON atomer_catalog_controls
    FOR EACH ROW EXECUTE FUNCTION atomer_catalog_control_immutable();
