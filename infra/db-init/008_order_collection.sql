-- Opt-in metadata collection only. No policy, order, reservation or movement backfill.
CREATE TABLE IF NOT EXISTS order_collection_settings (
    shop_id BIGINT PRIMARY KEY REFERENCES shops(id) ON DELETE RESTRICT,
    enabled BOOLEAN NOT NULL DEFAULT false,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    target_fingerprint VARCHAR(64) NOT NULL,
    cursor_at TIMESTAMPTZ NOT NULL,
    reconcile_cursor_at TIMESTAMPTZ NOT NULL,
    reconcile_until_at TIMESTAMPTZ,
    last_reconciled_at TIMESTAMPTZ,
    next_poll_at TIMESTAMPTZ NOT NULL,
    retry_after_at TIMESTAMPTZ,
    last_started_at TIMESTAMPTZ,
    last_completed_at TIMESTAMPTZ,
    last_error VARCHAR(80),
    entries_seen INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    last_mode VARCHAR(16)
);
CREATE TABLE IF NOT EXISTS order_collection_runs (
    id VARCHAR(36) PRIMARY KEY,
    shop_id BIGINT NOT NULL REFERENCES shops(id) ON DELETE RESTRICT,
    settings_revision INTEGER NOT NULL,
    mode VARCHAR(16) NOT NULL CHECK (mode IN ('delta', 'reconcile')),
    status VARCHAR(16) NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
    started_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    from_at TIMESTAMPTZ NOT NULL,
    until_at TIMESTAMPTZ NOT NULL,
    pages INTEGER NOT NULL DEFAULT 0,
    observed_count INTEGER NOT NULL DEFAULT 0,
    error VARCHAR(80)
);
CREATE INDEX IF NOT EXISTS ix_order_collection_runs_shop ON order_collection_runs(shop_id, started_at DESC);
CREATE TABLE IF NOT EXISTS order_inbox (
    id BIGSERIAL PRIMARY KEY,
    shop_id BIGINT NOT NULL REFERENCES shops(id) ON DELETE RESTRICT,
    source_uuid VARCHAR(36) NOT NULL,
    order_number VARCHAR(100) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    deleted BOOLEAN NOT NULL,
    origin VARCHAR(40) NOT NULL,
    status_id BIGINT NOT NULL,
    observation_hash VARCHAR(64) NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL,
    review_reason VARCHAR(80),
    CONSTRAINT uq_order_inbox_uuid UNIQUE(shop_id, source_uuid),
    CONSTRAINT uq_order_inbox_number UNIQUE(shop_id, order_number)
);
CREATE INDEX IF NOT EXISTS ix_order_inbox_shop_updated ON order_inbox(shop_id, updated_at DESC);
