-- Local, manual Atomer CSV exports; no inventory changes or remote writes.
CREATE TABLE IF NOT EXISTS atomer_availability_settings (
 id integer PRIMARY KEY CHECK (id=1), revision integer NOT NULL DEFAULT 1,
 enabled boolean NOT NULL DEFAULT true,
 warehouse_id bigint NOT NULL REFERENCES warehouses(id) ON DELETE RESTRICT,
 admin_complete boolean NOT NULL DEFAULT false,
 next_run_at timestamptz, last_started_at timestamptz, last_completed_at timestamptz,
 last_error varchar(100), last_request_id varchar(36), last_request_hash varchar(64)
);
CREATE TABLE IF NOT EXISTS atomer_availability_runs (
 id varchar(36) PRIMARY KEY, request_hash varchar(64) NOT NULL,
 warehouse_id bigint NOT NULL REFERENCES warehouses(id) ON DELETE RESTRICT,
 trigger varchar(16) NOT NULL, receiving_batch_id varchar(36), product_ids jsonb,
 status varchar(16) NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','fetching','ready','exported','failed')),
 revision integer NOT NULL DEFAULT 1, created_at timestamptz NOT NULL DEFAULT now(),
 source_fetched_at timestamptz, source_sha256 varchar(64), completed_at timestamptz,
 error varchar(100), document jsonb NOT NULL DEFAULT '{}', export_hash varchar(64),
 csv_content bytea, csv_sha256 varchar(64)
);
CREATE INDEX IF NOT EXISTS atomer_availability_run_recent ON atomer_availability_runs(created_at DESC);
CREATE INDEX IF NOT EXISTS atomer_availability_run_pending ON atomer_availability_runs(created_at) WHERE status IN ('queued','fetching');
CREATE TABLE IF NOT EXISTS atomer_availability_admin_sources (
 id integer PRIMARY KEY CHECK(id=1), source_sha256 varchar(64) NOT NULL,
 imported_at timestamptz NOT NULL DEFAULT now(), complete boolean NOT NULL, content bytea NOT NULL
);
