-- Selected manual merchandising changes; no stock or supplier mutations.
CREATE TABLE IF NOT EXISTS product_editor_publications (
    id VARCHAR(36) PRIMARY KEY,
    product_id BIGINT NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
    shop_id BIGINT NOT NULL REFERENCES shops(id) ON DELETE RESTRICT,
    state VARCHAR(30) NOT NULL CHECK (state IN ('ready', 'sending', 'completed', 'uncertain', 'rejected', 'resolved')),
    document JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_editor_publication_inflight
 ON product_editor_publications(product_id, shop_id) WHERE state IN ('sending', 'uncertain');
CREATE INDEX IF NOT EXISTS ix_editor_publication_history
 ON product_editor_publications(product_id, shop_id, created_at DESC);
