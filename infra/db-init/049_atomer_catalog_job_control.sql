-- Explicit, audited withdrawal of one proved-unsent catalog job. No data cleanup.
ALTER TABLE atomer_catalog_jobs
    ADD COLUMN IF NOT EXISTS operator_withdrawn boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS operator_control_revision bigint NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS operator_control_id uuid;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='atomer_catalog_jobs'::regclass
                   AND conname='ck_atomer_catalog_job_control') THEN
        ALTER TABLE atomer_catalog_jobs ADD CONSTRAINT ck_atomer_catalog_job_control CHECK (
            operator_control_revision >= 0 AND
            ((operator_control_revision=0) = (operator_control_id IS NULL)) AND
            (NOT operator_withdrawn OR (stage='excluded' AND source_card_stage='blocked'
                AND document IS NULL AND operator_control_id IS NOT NULL))
        );
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS atomer_catalog_job_controls (
    request_id uuid PRIMARY KEY,
    campaign text NOT NULL,
    family_key text NOT NULL,
    action text NOT NULL CHECK (action IN ('withdraw','replan')),
    request_hash text NOT NULL CHECK (request_hash ~ '^[a-f0-9]{64}$'),
    actor text NOT NULL CHECK (length(actor) BETWEEN 1 AND 200),
    status text NOT NULL DEFAULT 'completed' CHECK (status='completed'),
    request jsonb NOT NULL CHECK (jsonb_typeof(request)='object'),
    before_job jsonb NOT NULL CHECK (jsonb_typeof(before_job)='object'),
    after_job jsonb NOT NULL CHECK (jsonb_typeof(after_job)='object'),
    evidence jsonb NOT NULL CHECK (jsonb_typeof(evidence)='object'),
    result jsonb NOT NULL CHECK (jsonb_typeof(result)='object'),
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY(campaign,family_key) REFERENCES atomer_catalog_jobs(campaign,family_key)
);
CREATE INDEX IF NOT EXISTS ix_atomer_catalog_job_control_history
    ON atomer_catalog_job_controls(campaign,family_key,created_at);
DROP TRIGGER IF EXISTS tr_atomer_catalog_job_control_immutable ON atomer_catalog_job_controls;
CREATE TRIGGER tr_atomer_catalog_job_control_immutable BEFORE UPDATE OR DELETE ON atomer_catalog_job_controls
    FOR EACH ROW EXECUTE FUNCTION atomer_catalog_control_immutable();

CREATE OR REPLACE FUNCTION atomer_catalog_job_control_fence() RETURNS trigger AS $$
DECLARE audit atomer_catalog_job_controls%ROWTYPE;
BEGIN
    IF TG_OP='INSERT' THEN
        IF NEW.operator_withdrawn OR NEW.operator_control_revision<>0 OR NEW.operator_control_id IS NOT NULL THEN
            RAISE EXCEPTION 'catalog_job_control_required' USING ERRCODE='23514';
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP='DELETE' THEN
        IF OLD.operator_control_revision<>0 THEN
            RAISE EXCEPTION 'catalog_job_control_history_preserved' USING ERRCODE='23514';
        END IF;
        RETURN OLD;
    END IF;
    IF OLD.operator_withdrawn OR NEW.operator_withdrawn IS DISTINCT FROM OLD.operator_withdrawn
            OR NEW.operator_control_revision IS DISTINCT FROM OLD.operator_control_revision
            OR NEW.operator_control_id IS DISTINCT FROM OLD.operator_control_id THEN
        SELECT * INTO audit FROM atomer_catalog_job_controls
            WHERE request_id::text=current_setting('app.atomer_job_control_request',true)
              AND campaign=OLD.campaign AND family_key=OLD.family_key;
        IF NOT FOUND OR audit.before_job<>to_jsonb(OLD) OR audit.after_job<>to_jsonb(NEW)
                OR NEW.operator_control_id<>audit.request_id
                OR NEW.operator_control_revision<>OLD.operator_control_revision+1
                OR NEW.operator_withdrawn<>(audit.action='withdraw')
                OR NEW.operator_withdrawn=OLD.operator_withdrawn THEN
            RAISE EXCEPTION 'catalog_job_withdrawn_or_control_changed' USING ERRCODE='23514';
        END IF;
        IF NOT EXISTS (SELECT 1 FROM atomer_catalog_campaigns WHERE id=OLD.campaign
                         AND operator_paused AND state='paused') THEN
            RAISE EXCEPTION 'catalog_not_operator_paused' USING ERRCODE='23514';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS tr_atomer_catalog_job_control_fence ON atomer_catalog_jobs;
CREATE TRIGGER tr_atomer_catalog_job_control_fence BEFORE INSERT OR UPDATE OR DELETE ON atomer_catalog_jobs
    FOR EACH ROW EXECUTE FUNCTION atomer_catalog_job_control_fence();

CREATE OR REPLACE FUNCTION atomer_catalog_withdrawn_proof_fence() RETURNS trigger AS $$
DECLARE target_campaign text; target_family text; withdrawn boolean;
BEGIN
    IF TG_OP<>'INSERT' THEN
        SELECT operator_withdrawn INTO withdrawn FROM atomer_catalog_jobs
            WHERE campaign=OLD.campaign AND family_key=OLD.family_key FOR SHARE;
        IF withdrawn THEN
            RAISE EXCEPTION 'catalog_job_withdrawn_proof_preserved' USING ERRCODE='23514';
        END IF;
    END IF;
    IF TG_OP<>'DELETE' THEN
        -- The row lock serializes a new proof with withdrawal even if the proof
        -- did not exist at the final scan (including foreign-family rehomes).
        SELECT operator_withdrawn INTO withdrawn FROM atomer_catalog_jobs
            WHERE campaign=NEW.campaign AND family_key=NEW.family_key FOR SHARE;
        IF withdrawn THEN
            RAISE EXCEPTION 'catalog_job_withdrawn_proof_preserved' USING ERRCODE='23514';
        END IF;
        RETURN NEW;
    END IF;
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS tr_atomer_catalog_withdrawn_proof ON atomer_catalog_proofs;
CREATE TRIGGER tr_atomer_catalog_withdrawn_proof BEFORE INSERT OR UPDATE OR DELETE ON atomer_catalog_proofs
    FOR EACH ROW EXECUTE FUNCTION atomer_catalog_withdrawn_proof_fence();
DROP TRIGGER IF EXISTS tr_atomer_catalog_withdrawn_source_card ON atomer_catalog_source_cards;
CREATE TRIGGER tr_atomer_catalog_withdrawn_source_card BEFORE INSERT OR UPDATE OR DELETE ON atomer_catalog_source_cards
    FOR EACH ROW EXECUTE FUNCTION atomer_catalog_withdrawn_proof_fence();
