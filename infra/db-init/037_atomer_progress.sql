CREATE TABLE IF NOT EXISTS atomer_progress_sources (
    sha256 text PRIMARY KEY CHECK (length(sha256) = 64),
    source_rows integer NOT NULL CHECK (source_rows > 0),
    identities jsonb NOT NULL,
    imported_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS atomer_progress_runs (
    id bigserial PRIMARY KEY,
    source_sha256 text NOT NULL REFERENCES atomer_progress_sources(sha256),
    checked_at timestamptz NOT NULL,
    totals jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS atomer_progress_items (
    run_id bigint NOT NULL REFERENCES atomer_progress_runs(id),
    code text NOT NULL,
    kind text NOT NULL,
    state text NOT NULL,
    family_key text NOT NULL,
    detail jsonb NOT NULL,
    PRIMARY KEY (run_id, code)
);
CREATE INDEX IF NOT EXISTS ix_atomer_progress_filter ON atomer_progress_items(run_id, kind, state, code);
CREATE INDEX IF NOT EXISTS ix_atomer_progress_family ON atomer_progress_items(run_id, family_key, code);
