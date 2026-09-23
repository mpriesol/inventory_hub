-- Additive operational settings and opt-in local order processing. No shop is activated.
CREATE TABLE IF NOT EXISTS stock_warehouse_settings (
    warehouse_id BIGINT PRIMARY KEY REFERENCES warehouses(id) ON DELETE RESTRICT,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    "values" JSONB NOT NULL DEFAULT '{}'::jsonb,
    processing_paused BOOLEAN NOT NULL DEFAULT false,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS stock_shop_settings (
    shop_id BIGINT PRIMARY KEY REFERENCES shops(id) ON DELETE RESTRICT,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    overrides JSONB NOT NULL DEFAULT '{}'::jsonb,
    mode VARCHAR(16) NOT NULL DEFAULT 'manual' CHECK (mode IN ('manual', 'reserve', 'fulfill')),
    automation_starts_at TIMESTAMPTZ,
    issue_starts_at TIMESTAMPTZ,
    authorized_policy_revision INTEGER,
    target_fingerprint VARCHAR(64),
    processing_retry_after_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (mode = 'manual' OR (automation_starts_at IS NOT NULL AND authorized_policy_revision IS NOT NULL AND target_fingerprint IS NOT NULL)),
    CHECK (mode <> 'fulfill' OR issue_starts_at IS NOT NULL)
);
ALTER TABLE order_collection_settings ADD COLUMN IF NOT EXISTS manual_requested_at TIMESTAMPTZ;
ALTER TABLE order_collection_runs ADD COLUMN IF NOT EXISTS manual BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE order_collection_runs ADD COLUMN IF NOT EXISTS configuration_hash VARCHAR(64);
ALTER TABLE order_collection_runs ADD COLUMN IF NOT EXISTS configuration_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb;
CREATE TABLE IF NOT EXISTS order_processing_jobs (
    id BIGSERIAL PRIMARY KEY,
    shop_id BIGINT NOT NULL REFERENCES shops(id) ON DELETE RESTRICT,
    inbox_id BIGINT NOT NULL REFERENCES order_inbox(id) ON DELETE RESTRICT,
    source_uuid VARCHAR(36) NOT NULL,
    order_number VARCHAR(100) NOT NULL,
    observation_hash VARCHAR(64) NOT NULL,
    generation INTEGER NOT NULL DEFAULT 1 CHECK (generation > 0),
    status VARCHAR(16) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'running', 'completed', 'retry', 'review')),
    next_attempt_at TIMESTAMPTZ NOT NULL,
    last_started_at TIMESTAMPTZ,
    last_completed_at TIMESTAMPTZ,
    last_checked_at TIMESTAMPTZ,
    source_hash VARCHAR(64),
    configuration_hash VARCHAR(64),
    attempts INTEGER NOT NULL DEFAULT 0,
    error VARCHAR(80),
    result JSONB,
    audit_preview_id VARCHAR(36) REFERENCES order_stock_previews(id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_order_processing_jobs_source UNIQUE (shop_id, source_uuid)
);
CREATE INDEX IF NOT EXISTS ix_order_processing_jobs_due ON order_processing_jobs(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS ix_order_processing_jobs_shop ON order_processing_jobs(shop_id, next_attempt_at);
