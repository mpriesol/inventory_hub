-- Read-only supplier fetches. No automation or stock authority is activated.
CREATE TABLE IF NOT EXISTS supplier_availability_settings (
    supplier_id bigint PRIMARY KEY REFERENCES suppliers(id) ON DELETE RESTRICT,
    revision integer NOT NULL DEFAULT 1 CHECK (revision > 0),
    enabled boolean NOT NULL DEFAULT false,
    feed_key varchar(50) NOT NULL,
    interval_seconds integer NOT NULL DEFAULT 3600 CHECK (interval_seconds BETWEEN 300 AND 604800),
    freshness_seconds integer NOT NULL DEFAULT 21600 CHECK (freshness_seconds BETWEEN 300 AND 2592000),
    min_coverage_percent integer NOT NULL DEFAULT 100 CHECK (min_coverage_percent BETWEEN 1 AND 100),
    next_run_at timestamptz NOT NULL DEFAULT now(),
    manual_requested_at timestamptz,
    last_started_at timestamptz,
    last_success_at timestamptz,
    last_error varchar(100),
    last_item_count integer NOT NULL DEFAULT 0,
    running_run_id bigint REFERENCES supplier_feed_runs(id) ON DELETE RESTRICT,
    CHECK (freshness_seconds >= interval_seconds)
);
CREATE TABLE IF NOT EXISTS supplier_availability_observations (
    supplier_id bigint NOT NULL REFERENCES suppliers(id) ON DELETE RESTRICT,
    supplier_sku varchar(100) NOT NULL,
    feed_id bigint NOT NULL REFERENCES supplier_feeds(id) ON DELETE RESTRICT,
    run_id bigint NOT NULL REFERENCES supplier_feed_runs(id) ON DELETE RESTRICT,
    available boolean,
    quantity numeric(12,3),
    quantity_kind varchar(12) NOT NULL CHECK (quantity_kind IN ('exact','minimum','boolean','unknown')),
    raw jsonb NOT NULL DEFAULT '{}',
    observed_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    PRIMARY KEY (supplier_id, supplier_sku),
    CHECK (quantity IS NULL OR quantity >= 0),
    CHECK (expires_at >= observed_at),
    CHECK ((quantity_kind IN ('exact','minimum') AND quantity IS NOT NULL) OR
           (quantity_kind IN ('boolean','unknown') AND quantity IS NULL))
);
CREATE INDEX IF NOT EXISTS ix_supplier_availability_expiry ON supplier_availability_observations(expires_at);
