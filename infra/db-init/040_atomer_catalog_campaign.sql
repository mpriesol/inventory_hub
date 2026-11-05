-- Operator-pinned catalog campaigns. Only a registered campaign can dispatch work.
CREATE TABLE IF NOT EXISTS atomer_catalog_campaigns (
 id text PRIMARY KEY,
 run_id bigint NOT NULL REFERENCES atomer_progress_runs(id),
 source_sha text NOT NULL, admin_sha text NOT NULL,
 shop_fingerprint text NOT NULL, actor text NOT NULL,
 selection jsonb NOT NULL, options jsonb NOT NULL DEFAULT '{}',
 state text NOT NULL CHECK(state IN ('running','waiting','review','done','source_changed','paused')),
 next_at timestamptz NOT NULL DEFAULT now(), problem text NOT NULL DEFAULT '',
 failures integer NOT NULL DEFAULT 0, checked_run_id bigint,
 updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_atomer_catalog_active ON atomer_catalog_campaigns ((true))
WHERE state IN ('running','waiting');
CREATE TABLE IF NOT EXISTS atomer_catalog_jobs (
 campaign text NOT NULL REFERENCES atomer_catalog_campaigns(id),
 family_key text NOT NULL, priority integer NOT NULL, leaf_count integer NOT NULL CHECK(leaf_count>0),
 stage text NOT NULL DEFAULT 'pending' CHECK(stage IN
 ('pending','prepared','sending','observe','cards','related','verified','partial','blocked','excluded')),
 guards jsonb, document jsonb, receipt jsonb NOT NULL DEFAULT '{}', problem text NOT NULL DEFAULT '',
 attempts integer NOT NULL DEFAULT 0, next_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(campaign,family_key)
);
CREATE INDEX IF NOT EXISTS ix_atomer_catalog_next ON atomer_catalog_jobs(campaign,priority,next_at)
WHERE stage IN ('pending','prepared','sending','observe','cards','related');
CREATE TABLE IF NOT EXISTS atomer_catalog_proofs (
 campaign text NOT NULL, family_key text NOT NULL, code text NOT NULL,
 state text NOT NULL CHECK(state IN ('verified','partial','blocked','excluded')), evidence jsonb NOT NULL,
 checked_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(campaign,code),
 FOREIGN KEY (campaign,family_key) REFERENCES atomer_catalog_jobs(campaign,family_key)
);
CREATE INDEX IF NOT EXISTS ix_atomer_catalog_family_proofs ON atomer_catalog_proofs(campaign,family_key);
