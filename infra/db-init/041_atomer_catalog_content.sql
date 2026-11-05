-- A separate durable intent for changes to an existing xTrek family.
DO $$
BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='atomer_catalog_jobs'::regclass
    AND conname='atomer_catalog_jobs_stage_check' AND pg_get_constraintdef(oid) LIKE '%fidelity%') THEN
   ALTER TABLE atomer_catalog_jobs DROP CONSTRAINT IF EXISTS atomer_catalog_jobs_stage_check;
   ALTER TABLE atomer_catalog_jobs ADD CONSTRAINT atomer_catalog_jobs_stage_check CHECK(stage IN
    ('pending','prepared','sending','observe','cards','related','fidelity','updating','verified','partial','blocked','excluded'));
 END IF;
END $$;
CREATE INDEX IF NOT EXISTS ix_atomer_catalog_content_next ON atomer_catalog_jobs(campaign,priority,next_at)
 WHERE stage IN ('fidelity','updating');

-- Older workers verified local/native identity and cards before content auditing
-- was available. Preserve their receipts and re-open only that separate check.
WITH reopened AS (
 UPDATE atomer_catalog_jobs j SET stage='fidelity',next_at=now(),updated_at=now(),receipt=receipt ||
     '{"adopt_content":true,"content_requested":true}'::jsonb
 WHERE j.stage IN ('verified','partial') AND j.document IS NULL AND NOT j.receipt ? 'content_requested'
 AND EXISTS (SELECT 1 FROM atomer_catalog_proofs p WHERE p.campaign=j.campaign AND p.family_key=j.family_key
       AND p.state='verified' AND p.evidence->>'verification_scope'='identity_and_card'
       AND p.evidence->>'scope' IS DISTINCT FROM 'biketrek' AND p.evidence->'xtrek'->>'product_id' IS NOT NULL)
 RETURNING campaign,family_key
), pending_checks AS (
 UPDATE atomer_catalog_proofs p SET state='partial',evidence=p.evidence ||
      '{"content_pending":true}'::jsonb,checked_at=now()
 FROM reopened r WHERE p.campaign=r.campaign AND p.family_key=r.family_key AND p.state='verified'
   AND p.evidence->>'scope' IS DISTINCT FROM 'biketrek' RETURNING p.campaign
)
UPDATE atomer_catalog_campaigns c SET state='running',next_at=now(),updated_at=now()
WHERE c.state IN ('done','review') AND c.id IN (SELECT campaign FROM pending_checks)
AND NOT EXISTS (SELECT 1 FROM atomer_catalog_campaigns a WHERE a.id<>c.id AND a.state IN ('running','waiting'))
AND c.updated_at=(SELECT max(updated_at) FROM atomer_catalog_campaigns);
