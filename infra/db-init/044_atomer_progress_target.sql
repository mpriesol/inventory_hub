-- Complete private xTrek observations reused only with their original source and account.
CREATE TABLE IF NOT EXISTS atomer_progress_target_snapshots (
    digest text PRIMARY KEY CHECK (length(digest) = 64),
    source_sha text NOT NULL REFERENCES atomer_progress_sources(sha256),
    admin_sha text NOT NULL REFERENCES atomer_review_exports(sha256),
    shop_fingerprint text NOT NULL CHECK (length(shop_fingerprint) = 64),
    observed_at timestamptz NOT NULL,
    item_count integer NOT NULL CHECK (item_count > 0),
    compressed bytea NOT NULL
);
