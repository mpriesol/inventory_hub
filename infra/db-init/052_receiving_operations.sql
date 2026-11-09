-- Additive recovery audit. No receipt, balance, layer or historical movement is changed.
CREATE TABLE IF NOT EXISTS receiving_operations (
    id VARCHAR(36) PRIMARY KEY,
    session_id BIGINT NOT NULL REFERENCES receiving_sessions(id) ON DELETE RESTRICT,
    action VARCHAR(16) NOT NULL CHECK (action IN ('reverse','reopen','archive','restore')),
    request_hash VARCHAR(64) NOT NULL,
    preview_hash VARCHAR(64) NOT NULL,
    preview_data JSONB NOT NULL,
    actor VARCHAR(100) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'prepared' CHECK (status IN ('prepared','blocked','completed')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    result JSONB,
    CHECK ((status = 'completed') = (completed_at IS NOT NULL AND result IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_receiving_operations_session
    ON receiving_operations(session_id, created_at DESC);

CREATE OR REPLACE FUNCTION preserve_receiving_operation() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'receiving_operation_history_immutable';
    END IF;
    IF OLD.status = 'completed' OR ROW(NEW.id, NEW.session_id, NEW.action, NEW.request_hash,
        NEW.preview_hash, NEW.preview_data, NEW.actor, NEW.created_at, NEW.expires_at)
        IS DISTINCT FROM ROW(OLD.id, OLD.session_id, OLD.action, OLD.request_hash,
        OLD.preview_hash, OLD.preview_data, OLD.actor, OLD.created_at, OLD.expires_at) THEN
        RAISE EXCEPTION 'receiving_operation_history_immutable';
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS receiving_operation_immutable ON receiving_operations;
CREATE TRIGGER receiving_operation_immutable BEFORE UPDATE OR DELETE ON receiving_operations
    FOR EACH ROW EXECUTE FUNCTION preserve_receiving_operation();
