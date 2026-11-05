-- Private source-to-card receipts continue independently of a shop's API quota.
ALTER TABLE atomer_catalog_jobs ADD COLUMN IF NOT EXISTS source_card_stage text NOT NULL DEFAULT 'pending';
ALTER TABLE atomer_catalog_jobs ADD COLUMN IF NOT EXISTS source_card_next_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE atomer_catalog_jobs ADD COLUMN IF NOT EXISTS source_card_attempts integer NOT NULL DEFAULT 0;
ALTER TABLE atomer_catalog_jobs ADD COLUMN IF NOT EXISTS source_card_problem text NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS ix_atomer_source_card_next ON atomer_catalog_jobs(campaign,priority,source_card_next_at)
 WHERE source_card_stage='pending';
CREATE TABLE IF NOT EXISTS atomer_catalog_source_cards (
 campaign text NOT NULL, family_key text NOT NULL, code text NOT NULL,
 run_id bigint NOT NULL REFERENCES atomer_progress_runs(id),
 state text NOT NULL CHECK(state IN ('card_ready','blocked')),
 card_id bigint, created boolean, evidence jsonb NOT NULL, checked_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(campaign,code), FOREIGN KEY(campaign,family_key) REFERENCES atomer_catalog_jobs(campaign,family_key),
 CHECK(state<>'card_ready' OR card_id IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS ix_atomer_source_card_ids ON atomer_catalog_source_cards(campaign,card_id) WHERE card_id IS NOT NULL;
