-- Durable manual overrides only; imported product facts and the stock ledger stay intact.
CREATE TABLE IF NOT EXISTS product_editor_overrides (
    product_id BIGINT PRIMARY KEY REFERENCES products(id) ON DELETE RESTRICT,
    revision INTEGER NOT NULL CHECK (revision > 0),
    data JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS product_editor_saves (
    request_id VARCHAR(36) PRIMARY KEY,
    input_hash VARCHAR(64) NOT NULL,
    result JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS product_editor_audit (
    id BIGSERIAL PRIMARY KEY,
    request_id VARCHAR(36) NOT NULL REFERENCES product_editor_saves(request_id) ON DELETE RESTRICT,
    product_id BIGINT NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
    revision INTEGER NOT NULL CHECK (revision > 0),
    before_data JSONB NOT NULL,
    after_data JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (product_id, revision)
);
CREATE INDEX IF NOT EXISTS ix_product_editor_audit_product ON product_editor_audit(product_id, id DESC);
CREATE OR REPLACE FUNCTION product_editor_natural_key(value TEXT) RETURNS TEXT[]
LANGUAGE SQL IMMUTABLE PARALLEL SAFE AS $$
    SELECT ARRAY(SELECT CASE WHEN part[1] ~ '^[0-9]+$'
        THEN lpad(length(ltrim(part[1], '0'))::text, 6, '0') || ':' || ltrim(part[1], '0')
        ELSE lower(part[1]) END
        FROM regexp_matches(coalesce(value, ''), '([0-9]+|[^0-9]+)', 'g') WITH ORDINALITY AS pieces(part, position)
        ORDER BY position)
$$;
