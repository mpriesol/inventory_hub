-- Operator decisions belong to stable identities, independently of catalog runs.
CREATE TABLE IF NOT EXISTS atomer_review_targets (
    key text PRIMARY KEY,
    identity jsonb NOT NULL,
    imported_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS atomer_review_checks (
    run_id bigint NOT NULL REFERENCES atomer_progress_runs(id),
    key text NOT NULL REFERENCES atomer_review_targets(key),
    family_key text NOT NULL,
    detail jsonb NOT NULL,
    PRIMARY KEY (run_id, key)
);
CREATE INDEX IF NOT EXISTS ix_atomer_review_family ON atomer_review_checks(run_id, family_key);
CREATE TABLE IF NOT EXISTS atomer_review_work (
    kind text NOT NULL CHECK(kind IN ('family','item','extra')),
    key text NOT NULL,
    revision bigint NOT NULL CHECK(revision > 0),
    status text NOT NULL CHECK(status IN ('open','queued','working','blocked','done')),
    scope text NOT NULL CHECK(scope IN ('default','both','xtrek','biketrek')),
    decision text NOT NULL CHECK(decision IN ('undecided','yes','no')),
    note text NOT NULL,
    instruction text NOT NULL,
    result text NOT NULL,
    signature text NOT NULL DEFAULT '',
    updated_by text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(kind,key),
    CHECK(kind='extra' OR decision='undecided')
);
CREATE INDEX IF NOT EXISTS ix_atomer_review_queue ON atomer_review_work(status, updated_at);
CREATE TABLE IF NOT EXISTS atomer_review_events (
    id bigserial PRIMARY KEY,
    kind text NOT NULL,
    key text NOT NULL,
    revision bigint NOT NULL,
    command text NOT NULL,
    actor text NOT NULL,
    saved_at timestamptz NOT NULL DEFAULT now(),
    detail jsonb NOT NULL,
    UNIQUE(kind,key,revision)
);
-- Retain original private exports for unmapped fields, flags and later verification.
CREATE TABLE IF NOT EXISTS atomer_review_exports (
    sha256 text PRIMARY KEY, format text NOT NULL CHECK(format IN ('buyers','admin_csv')),
    compressed bytea NOT NULL, imported_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE atomer_progress_runs ADD COLUMN IF NOT EXISTS admin_export_sha256 text REFERENCES atomer_review_exports(sha256);
