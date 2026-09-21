-- Additive content workflow. No changes to inventory, identifiers or stock.
CREATE TABLE IF NOT EXISTS ai_rule_versions (
    id BIGSERIAL PRIMARY KEY,
    book JSONB NOT NULL,
    note TEXT NOT NULL,
    origin TEXT NOT NULL DEFAULT 'operator',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS ai_rule_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    published_id BIGINT NOT NULL REFERENCES ai_rule_versions(id)
);
CREATE TABLE IF NOT EXISTS ai_content_batches (
    id VARCHAR(32) PRIMARY KEY,
    request_hash VARCHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS ai_content_jobs (
    id VARCHAR(32) PRIMARY KEY,
    batch_id VARCHAR(32) NOT NULL REFERENCES ai_content_batches(id),
    kind TEXT NOT NULL DEFAULT 'product',
    status TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    context JSONB NOT NULL,
    output JSONB,
    checks JSONB NOT NULL DEFAULT '{}',
    events JSONB NOT NULL DEFAULT '[]',
    usage JSONB NOT NULL DEFAULT '{}',
    reserved_usd NUMERIC(14,6) NOT NULL DEFAULT 0 CHECK (reserved_usd >= 0),
    actual_usd NUMERIC(14,6),
    preview_id VARCHAR(32),
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_ai_jobs_batch ON ai_content_jobs(batch_id);
CREATE INDEX IF NOT EXISTS ix_ai_jobs_queue ON ai_content_jobs(status, created_at);
CREATE TABLE IF NOT EXISTS ai_content_revisions (
    job_id VARCHAR(32) NOT NULL REFERENCES ai_content_jobs(id),
    revision INTEGER NOT NULL,
    content JSONB NOT NULL,
    decision TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (job_id, revision)
);
