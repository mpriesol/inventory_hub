-- Create-only category intents and independent full-registry proofs.
CREATE TABLE IF NOT EXISTS atomer_catalog_category_restores (
    campaign text NOT NULL REFERENCES atomer_catalog_campaigns(id),
    path text NOT NULL,
    stage text NOT NULL DEFAULT 'pending' CHECK (stage IN ('pending','sending','verified','blocked')),
    intent jsonb, proof jsonb, ack jsonb,
    reads integer NOT NULL DEFAULT 0,
    next_at timestamptz NOT NULL DEFAULT now(),
    problem text NOT NULL DEFAULT '',
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(campaign,path)
);
CREATE INDEX IF NOT EXISTS ix_atomer_category_due ON atomer_catalog_category_restores(campaign,next_at)
WHERE stage IN ('pending','sending');
