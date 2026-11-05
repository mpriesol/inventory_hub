-- A committed family intent survives a lost HTTP reply. Original documents stay in the Hub.
CREATE TABLE IF NOT EXISTS atomer_variant_repairs (
    id varchar(64) PRIMARY KEY,
    parent_code varchar(100) NOT NULL,
    state varchar(20) NOT NULL,
    document jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_atomer_repair_inflight ON atomer_variant_repairs(lower(parent_code))
WHERE state IN ('sending','uncertain');
