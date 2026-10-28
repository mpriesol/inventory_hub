-- Notification hints only. No existing shop, collector or stock authority is enabled.
CREATE TABLE IF NOT EXISTS order_webhook_settings (
    shop_id BIGINT PRIMARY KEY REFERENCES shops(id) ON DELETE RESTRICT,
    enabled BOOLEAN NOT NULL DEFAULT false,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    target_fingerprint VARCHAR(64) NOT NULL,
    secret_hash VARCHAR(64) NOT NULL,
    public_base_url VARCHAR(500) NOT NULL,
    catchup_interval_seconds INTEGER NOT NULL DEFAULT 3600 CHECK (catchup_interval_seconds BETWEEN 300 AND 86400),
    registration_status VARCHAR(16) NOT NULL DEFAULT 'pending' CHECK (registration_status IN ('pending','running','registered','failed','disabled')),
    registered_events JSONB NOT NULL DEFAULT '{}',
    registered_at TIMESTAMPTZ,
    registration_checked_at TIMESTAMPTZ,
    next_registration_at TIMESTAMPTZ NOT NULL,
    registration_attempts INTEGER NOT NULL DEFAULT 0,
    last_received_at TIMESTAMPTZ,
    last_completed_at TIMESTAMPTZ,
    last_error VARCHAR(80)
);
CREATE TABLE IF NOT EXISTS order_webhook_events (
    shop_id BIGINT NOT NULL REFERENCES shops(id) ON DELETE RESTRICT,
    order_number VARCHAR(100) NOT NULL,
    event VARCHAR(10) NOT NULL CHECK (event IN ('create','update','delete')),
    notification_at TIMESTAMPTZ NOT NULL,
    notification_hash VARCHAR(64) NOT NULL,
    generation INTEGER NOT NULL DEFAULT 1 CHECK (generation > 0),
    status VARCHAR(16) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','retry','completed','review')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NOT NULL,
    last_received_at TIMESTAMPTZ NOT NULL,
    last_completed_at TIMESTAMPTZ,
    last_error VARCHAR(80),
    PRIMARY KEY (shop_id, order_number)
);
CREATE INDEX IF NOT EXISTS ix_order_webhook_events_due ON order_webhook_events(status, next_attempt_at);
