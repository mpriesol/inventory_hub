-- Retained local effects can be handed to manual identity work. No job is
-- transitioned by this migration; no identity, proof or stock row is changed.
ALTER TABLE atomer_catalog_job_controls
    DROP CONSTRAINT IF EXISTS atomer_catalog_job_controls_action_check;
ALTER TABLE atomer_catalog_job_controls ADD CONSTRAINT atomer_catalog_job_controls_action_check
    CHECK (action IN ('withdraw','replan','handoff'));

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
                OR NEW.operator_withdrawn<>(audit.action IN ('withdraw','handoff'))
                OR NEW.operator_withdrawn=OLD.operator_withdrawn THEN
            RAISE EXCEPTION 'catalog_job_withdrawn_or_control_changed' USING ERRCODE='23514';
        END IF;
        IF NOT EXISTS (SELECT 1 FROM atomer_catalog_campaigns WHERE id=OLD.campaign
                         AND operator_paused AND state='paused') THEN
            RAISE EXCEPTION 'catalog_not_operator_paused' USING ERRCODE='23514';
        END IF;
        IF audit.action='handoff' AND (
                OLD.stage<>'pending' OR OLD.document IS NOT NULL OR OLD.receipt<>'{}'::jsonb
                OR OLD.operator_control_revision<>0 OR OLD.operator_withdrawn
                OR NEW.stage<>'excluded' OR NEW.source_card_stage<>'blocked'
                OR (to_jsonb(OLD)-ARRAY['stage','source_card_stage','operator_withdrawn',
                    'operator_control_revision','operator_control_id','updated_at']) IS DISTINCT FROM
                   (to_jsonb(NEW)-ARRAY['stage','source_card_stage','operator_withdrawn',
                    'operator_control_revision','operator_control_id','updated_at'])) THEN
            RAISE EXCEPTION 'catalog_handoff_history_preserved' USING ERRCODE='23514';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- This independent trigger survives reinstalling migration049's older generic
-- control function. An older API/worker cannot replan a handed-off job during
-- upgrade or rollback, even with an otherwise valid new control audit.
CREATE OR REPLACE FUNCTION atomer_catalog_handoff_terminal_fence() RETURNS trigger AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM atomer_catalog_job_controls
               WHERE request_id=OLD.operator_control_id AND action='handoff') THEN
        RAISE EXCEPTION 'catalog_handoff_terminal' USING ERRCODE='23514';
    END IF;
    IF TG_OP='DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS tr_atomer_catalog_handoff_terminal ON atomer_catalog_jobs;
CREATE TRIGGER tr_atomer_catalog_handoff_terminal BEFORE UPDATE OR DELETE ON atomer_catalog_jobs
    FOR EACH ROW EXECUTE FUNCTION atomer_catalog_handoff_terminal_fence();
