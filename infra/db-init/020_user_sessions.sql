-- Additive account/session storage. Existing operator-token access remains valid.
CREATE TABLE IF NOT EXISTS hub_users (
    id BIGSERIAL PRIMARY KEY,
    username VARCHAR(80) NOT NULL UNIQUE,
    display_name VARCHAR(120) NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'operator')),
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS hub_sessions (
    token_hash VARCHAR(64) PRIMARY KEY,
    user_id BIGINT REFERENCES hub_users(id),
    operator_fingerprint VARCHAR(64),
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((user_id IS NOT NULL) <> (operator_fingerprint IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS hub_sessions_user_idx ON hub_sessions(user_id);
CREATE INDEX IF NOT EXISTS hub_sessions_expiry_idx ON hub_sessions(expires_at);
CREATE TABLE IF NOT EXISTS hub_login_limits (
    key VARCHAR(64) PRIMARY KEY,
    failures INTEGER NOT NULL DEFAULT 0,
    window_start TIMESTAMPTZ NOT NULL DEFAULT now()
);
