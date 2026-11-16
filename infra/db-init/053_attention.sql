-- Shared operational state. No business records, stock or settings are modified.
CREATE TABLE IF NOT EXISTS hub_attention_rules (
    kind VARCHAR(100) NOT NULL,
    scope VARCHAR(100) NOT NULL DEFAULT '*',
    revision INTEGER NOT NULL DEFAULT 1,
    enabled BOOLEAN NOT NULL DEFAULT true,
    min_occurrences INTEGER NOT NULL DEFAULT 1 CHECK (min_occurrences BETWEEN 1 AND 100),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (kind, scope)
);
CREATE TABLE IF NOT EXISTS hub_attention_items (
    id BIGSERIAL PRIMARY KEY,
    kind VARCHAR(100) NOT NULL,
    scope VARCHAR(100) NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    last_sequence BIGINT NOT NULL CHECK (last_sequence > 0),
    active BOOLEAN NOT NULL,
    occurrences INTEGER NOT NULL DEFAULT 0 CHECK (occurrences >= 0),
    episode INTEGER NOT NULL DEFAULT 0,
    details JSONB NOT NULL DEFAULT '{}',
    first_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at TIMESTAMPTZ,
    ignored_at TIMESTAMPTZ,
    UNIQUE (kind, scope)
);
CREATE INDEX IF NOT EXISTS idx_hub_attention_active ON hub_attention_items(active, last_seen DESC, id DESC);
