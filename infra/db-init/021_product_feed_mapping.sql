-- Mapping definitions are database-owned; original uploaded/downloaded files remain artifacts.
CREATE TABLE IF NOT EXISTS product_feed_mappings (
    id BIGSERIAL PRIMARY KEY,
    supplier_id BIGINT NOT NULL REFERENCES suppliers(id),
    feed_key VARCHAR(50) NOT NULL,
    shop_code VARCHAR(50) NOT NULL DEFAULT '',
    revision INTEGER NOT NULL CHECK (revision > 0),
    definition JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_product_feed_mapping_scope UNIQUE (supplier_id, feed_key, shop_code)
);
CREATE TABLE IF NOT EXISTS product_feed_mapping_revisions (
    id BIGSERIAL PRIMARY KEY,
    mapping_id BIGINT NOT NULL REFERENCES product_feed_mappings(id),
    revision INTEGER NOT NULL CHECK (revision > 0),
    definition JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_product_feed_mapping_revision UNIQUE (mapping_id, revision)
);
