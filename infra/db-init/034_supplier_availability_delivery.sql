-- Last confirmed supplier label is independent of each newly downloaded feed.
-- No stock, authority, enabled flags or historical observations are changed.
CREATE TABLE IF NOT EXISTS supplier_availability_deliveries (
    product_id bigint NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
    shop_id bigint NOT NULL REFERENCES shops(id) ON DELETE RESTRICT,
    desired jsonb NOT NULL,
    confirmed jsonb,
    confirmed_label varchar(100),
    confirmed_at timestamptz,
    publication_id varchar(36) NOT NULL REFERENCES product_editor_publications(id) ON DELETE RESTRICT,
    state varchar(30) NOT NULL,
    retry_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (product_id, shop_id)
);
CREATE INDEX IF NOT EXISTS ix_supplier_availability_delivery_publication
    ON supplier_availability_deliveries(publication_id);

-- Slow down already-enabled automatic sources. Do not enable disabled sources,
-- accelerate weekly schedules, or restart manual-only Sloger. Existing expired
-- observations retain their original expiry; only the next download gets new TTL.
WITH changed AS (
    UPDATE supplier_availability_settings s
       SET interval_seconds = 86400,
           freshness_seconds = GREATEST(freshness_seconds, 172800),
           next_run_at = CASE WHEN manual_requested_at IS NOT NULL OR last_started_at IS NULL
                              THEN next_run_at ELSE GREATEST(next_run_at, last_started_at + interval '1 day') END,
           revision = revision + 1
      FROM suppliers supplier
     WHERE s.supplier_id = supplier.id AND supplier.code <> 'sloger'
       AND s.enabled AND s.interval_seconds < 86400
    RETURNING s.supplier_id, s.feed_key
)
UPDATE supplier_feeds f SET fetch_interval_min = 1440
  FROM changed c WHERE f.supplier_id = c.supplier_id AND f.code = c.feed_key;

ALTER TABLE supplier_availability_settings ALTER COLUMN interval_seconds SET DEFAULT 86400;
ALTER TABLE supplier_availability_settings ALTER COLUMN freshness_seconds SET DEFAULT 172800;
