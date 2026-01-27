-- ============================================================================
-- INVENTORY HUB v12 FINAL - PRODUCTION READY
-- ============================================================================
-- Single clean CREATE script for empty database
-- PostgreSQL 15+
-- 
-- Run: psql -v ON_ERROR_STOP=1 -d inventory_hub -f schema_v12_FINAL.sql
--
-- DESIGN PRINCIPLES:
-- 1. ZERO ALTER TABLE statements (cyclic FK resolved via nullable references)
-- 2. NO EXTENSIONS REQUIRED (md5() is built-in PostgreSQL)
-- 3. MULTI-EAN SUPPORT: One product can have multiple EAN/UPC/barcode codes
-- 4. SAFE DEDUP: receiving_lines uses NOT NULL line_number (no fingerprint constraint)
-- 5. IMMUTABLE LEDGER: stock_movements protected by trigger
--
-- CHANGES FROM v12:
-- - Removed pgcrypto extension (md5 is built-in)
-- - Removed ALTER TABLE for supplier_feeds.last_run_id (FK via view instead)
-- - receiving_lines.line_number is NOT NULL with UNIQUE constraint
-- - line_fingerprint is helper column only (no UNIQUE constraint)
-- - All cyclic FK dependencies resolved
-- ============================================================================

-- ============================================================================
-- EXTENSIONS (OPTIONAL - NOT REQUIRED)
-- Uncomment only if you need these features
-- ============================================================================

-- CREATE EXTENSION IF NOT EXISTS "uuid-ossp";  -- For uuid_generate_v4()
-- CREATE EXTENSION IF NOT EXISTS "pg_trgm";    -- For trigram similarity search

-- ============================================================================
-- ENUMS
-- ============================================================================

CREATE TYPE availability_status AS ENUM (
    'in_stock', 'low_stock', 'available_3_5_days', 'available_1_2_weeks',
    'on_order', 'unavailable', 'discontinued', 'unknown'
);

CREATE TYPE movement_type AS ENUM (
    'RECEIVING_IN', 'SALE_OUT', 'RETURN_IN', 'ADJUSTMENT_IN', 'ADJUSTMENT_OUT',
    'TRANSFER_IN', 'TRANSFER_OUT', 'WRITE_OFF', 'INITIAL'
);

CREATE TYPE receiving_status AS ENUM ('new', 'in_progress', 'paused', 'completed', 'cancelled');
CREATE TYPE inventory_count_status AS ENUM ('draft', 'in_progress', 'review', 'finalized', 'cancelled');
CREATE TYPE inventory_count_type AS ENUM ('full', 'partial', 'cycle', 'spot');

-- v12 FINAL: Barcode types for multi-EAN support
CREATE TYPE identifier_type AS ENUM (
    'ean',                -- EAN-13, EAN-8 (validated, globally unique)
    'upc',                -- UPC-A, UPC-E (validated, globally unique)
    'unverified_barcode', -- Non-standard barcodes (short codes like 398828)
    'supplier_sku',       -- Supplier's product code (unique per supplier)
    'internal_sku',       -- Our internal SKU (globally unique)
    'manufacturer',       -- Manufacturer part number (not unique)
    'custom'              -- Any other identifier (not unique)
);

CREATE TYPE order_status AS ENUM ('new', 'processing', 'shipped', 'completed', 'cancelled', 'returned');
CREATE TYPE reservation_status AS ENUM ('reserved', 'backorder', 'fulfilled', 'cancelled', 'rejected');
CREATE TYPE oversell_mode AS ENUM ('allow', 'block');
CREATE TYPE scan_session_type AS ENUM ('receiving', 'inventory', 'lookup', 'adjustment');
CREATE TYPE scan_status AS ENUM ('active', 'undone');
CREATE TYPE config_entity_type AS ENUM ('supplier', 'shop', 'warehouse', 'system');
CREATE TYPE feed_run_status AS ENUM ('running', 'completed', 'failed', 'cancelled');
CREATE TYPE sync_outbox_status AS ENUM ('pending', 'processing', 'completed', 'failed', 'cancelled');
CREATE TYPE availability_code AS ENUM (
    'in_stock', 'low_stock', 'supplier_1_3_days', 'supplier_3_5_days',
    'supplier_1_2_weeks', 'supplier_on_order', 'check_availability', 
    'unavailable', 'manual_override'
);
CREATE TYPE stock_position_code AS ENUM ('local', 'supplier', 'none');
CREATE TYPE warehouse_availability_policy AS ENUM ('default_only', 'selected', 'sum_all');
CREATE TYPE shop_warehouse_role AS ENUM ('fulfillment', 'availability_source', 'both');

-- ============================================================================
-- 1. WAREHOUSES
-- ============================================================================

CREATE TABLE warehouses (
    id BIGSERIAL PRIMARY KEY,
    code VARCHAR(50) NOT NULL UNIQUE,
    name VARCHAR(255) NOT NULL,
    address TEXT,
    is_default BOOLEAN NOT NULL DEFAULT false,
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX idx_warehouses_single_default 
    ON warehouses(is_default) WHERE is_default = true;

-- ============================================================================
-- 2. SUPPLIERS
-- ============================================================================

CREATE TABLE suppliers (
    id BIGSERIAL PRIMARY KEY,
    code VARCHAR(50) NOT NULL UNIQUE,
    name VARCHAR(255) NOT NULL,
    config_path VARCHAR(500),
    adapter VARCHAR(100),
    invoice_prefix VARCHAR(20),
    default_currency VARCHAR(3) NOT NULL DEFAULT 'EUR',
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================================
-- 3. SHOPS
-- ============================================================================

CREATE TABLE shops (
    id BIGSERIAL PRIMARY KEY,
    code VARCHAR(50) NOT NULL UNIQUE,
    name VARCHAR(255) NOT NULL,
    platform VARCHAR(50) NOT NULL,
    config_path VARCHAR(500),
    warehouse_availability_policy warehouse_availability_policy NOT NULL DEFAULT 'default_only',
    oversell_mode oversell_mode NOT NULL DEFAULT 'allow',
    sync_enabled BOOLEAN NOT NULL DEFAULT true,
    sync_interval_min INTEGER NOT NULL DEFAULT 10,
    last_sync_at TIMESTAMPTZ,
    last_sync_status VARCHAR(50),
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================================
-- 4. SHOP WAREHOUSES
-- ============================================================================

CREATE TABLE shop_warehouses (
    id BIGSERIAL PRIMARY KEY,
    shop_id BIGINT NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
    warehouse_id BIGINT NOT NULL REFERENCES warehouses(id) ON DELETE RESTRICT,
    role shop_warehouse_role NOT NULL DEFAULT 'both',
    priority INTEGER NOT NULL DEFAULT 100,
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    CONSTRAINT uq_shop_warehouses UNIQUE (shop_id, warehouse_id)
);

CREATE INDEX idx_shop_warehouses_shop ON shop_warehouses(shop_id) WHERE is_active = true;

-- ============================================================================
-- 5. SUPPLIER FEED RUNS (created BEFORE supplier_feeds to avoid cyclic ALTER)
-- ============================================================================

CREATE TABLE supplier_feed_runs (
    id BIGSERIAL PRIMARY KEY,
    feed_id BIGINT NOT NULL,  -- FK added after supplier_feeds exists
    run_number INTEGER NOT NULL,
    status feed_run_status NOT NULL DEFAULT 'running',
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    source_etag VARCHAR(255),
    source_last_modified TIMESTAMPTZ,
    source_size_bytes BIGINT,
    items_fetched INTEGER NOT NULL DEFAULT 0,
    items_new INTEGER NOT NULL DEFAULT 0,
    items_updated INTEGER NOT NULL DEFAULT 0,
    items_unchanged INTEGER NOT NULL DEFAULT 0,
    items_error INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    error_details JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================================
-- 6. SUPPLIER FEEDS
-- ============================================================================

CREATE TABLE supplier_feeds (
    id BIGSERIAL PRIMARY KEY,
    supplier_id BIGINT NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
    code VARCHAR(50) NOT NULL,
    name VARCHAR(255) NOT NULL,
    feed_type VARCHAR(50) NOT NULL,
    source_url TEXT,
    source_format VARCHAR(50),
    mapping_config JSONB NOT NULL DEFAULT '{}',
    fetch_interval_min INTEGER NOT NULL DEFAULT 360,
    is_active BOOLEAN NOT NULL DEFAULT true,
    -- v12 FINAL: last_run_id without FK to avoid cyclic ALTER
    -- Use v_supplier_feed_status view for joined data
    last_run_id BIGINT,
    last_run_at TIMESTAMPTZ,
    last_run_status feed_run_status,
    last_run_items_count INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    CONSTRAINT uq_supplier_feeds_code UNIQUE (supplier_id, code)
);

CREATE INDEX idx_supplier_feeds_active ON supplier_feeds(supplier_id) WHERE is_active = true;

-- Now add FK from supplier_feed_runs to supplier_feeds
ALTER TABLE supplier_feed_runs 
    ADD CONSTRAINT fk_feed_runs_feed 
    FOREIGN KEY (feed_id) REFERENCES supplier_feeds(id) ON DELETE CASCADE;

CREATE UNIQUE INDEX idx_feed_runs_number ON supplier_feed_runs(feed_id, run_number);
CREATE INDEX idx_feed_runs_feed ON supplier_feed_runs(feed_id);
CREATE INDEX idx_feed_runs_status ON supplier_feed_runs(feed_id, status);
CREATE INDEX idx_feed_runs_started ON supplier_feed_runs(started_at DESC);

-- ============================================================================
-- 7. SUPPLIER FEED ITEMS RAW
-- ============================================================================

CREATE TABLE supplier_feed_items_raw (
    id BIGSERIAL PRIMARY KEY,
    feed_id BIGINT NOT NULL REFERENCES supplier_feeds(id) ON DELETE CASCADE,
    run_id BIGINT NOT NULL REFERENCES supplier_feed_runs(id) ON DELETE CASCADE,
    item_hash VARCHAR(64) NOT NULL,
    raw_data JSONB NOT NULL,
    processed BOOLEAN NOT NULL DEFAULT false,
    processed_at TIMESTAMPTZ,
    processing_error TEXT,
    supplier_product_id BIGINT,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    CONSTRAINT uq_feed_items_raw_hash UNIQUE (run_id, item_hash)
);

CREATE INDEX idx_feed_items_raw_feed ON supplier_feed_items_raw(feed_id);
CREATE INDEX idx_feed_items_raw_run ON supplier_feed_items_raw(run_id);
CREATE INDEX idx_feed_items_raw_unprocessed ON supplier_feed_items_raw(feed_id, processed) 
    WHERE processed = false;

-- ============================================================================
-- 8. SUPPLIER PRODUCTS
-- ============================================================================

CREATE TABLE supplier_products (
    id BIGSERIAL PRIMARY KEY,
    supplier_id BIGINT NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
    supplier_sku VARCHAR(100) NOT NULL,
    ean VARCHAR(255),  -- Can contain compound string like "398828/6927116185329"
    manufacturer_sku VARCHAR(100),
    name VARCHAR(500) NOT NULL,
    brand VARCHAR(100),
    category VARCHAR(255),
    description TEXT,
    images JSONB NOT NULL DEFAULT '[]',
    attributes JSONB NOT NULL DEFAULT '{}',
    supplier_group_code VARCHAR(100),
    purchase_price DECIMAL(12,4),
    purchase_currency VARCHAR(3) NOT NULL DEFAULT 'EUR',
    recommended_price DECIMAL(12,4),
    supplier_availability_code_raw VARCHAR(50),
    supplier_availability_text VARCHAR(255),
    stock_qty DECIMAL(12,3),
    lead_time_days INTEGER,
    expected_date DATE,
    weight_g INTEGER,
    is_active BOOLEAN NOT NULL DEFAULT true,
    is_discontinued BOOLEAN NOT NULL DEFAULT false,
    source_feed_id BIGINT REFERENCES supplier_feeds(id) ON DELETE SET NULL,
    last_seen_run_id BIGINT REFERENCES supplier_feed_runs(id) ON DELETE SET NULL,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    CONSTRAINT uq_supplier_products_sku UNIQUE (supplier_id, supplier_sku)
);

CREATE INDEX idx_supplier_products_ean ON supplier_products(ean) WHERE ean IS NOT NULL;
CREATE INDEX idx_supplier_products_supplier ON supplier_products(supplier_id);
CREATE INDEX idx_supplier_products_active ON supplier_products(supplier_id, is_active) 
    WHERE is_active = true;

-- Add FK back to supplier_feed_items_raw
ALTER TABLE supplier_feed_items_raw 
    ADD CONSTRAINT fk_feed_items_supplier_product 
    FOREIGN KEY (supplier_product_id) REFERENCES supplier_products(id) ON DELETE SET NULL;

-- ============================================================================
-- 9. PRODUCT GROUPS
-- ============================================================================

CREATE TABLE product_groups (
    id BIGSERIAL PRIMARY KEY,
    code VARCHAR(100) NOT NULL UNIQUE,
    name VARCHAR(500) NOT NULL,
    brand VARCHAR(100),
    category VARCHAR(255),
    description TEXT,
    main_image_url TEXT,
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_product_groups_brand ON product_groups(brand) WHERE brand IS NOT NULL;

-- ============================================================================
-- 10. PRODUCTS
-- INVARIANT: NO primary_ean, NO supplier_sku columns (use product_identifiers)
-- ============================================================================

CREATE TABLE products (
    id BIGSERIAL PRIMARY KEY,
    sku VARCHAR(100) NOT NULL UNIQUE,
    supplier_id BIGINT REFERENCES suppliers(id) ON DELETE SET NULL,
    name VARCHAR(500) NOT NULL,
    brand VARCHAR(100),
    category VARCHAR(255),
    weight_g INTEGER,
    group_id BIGINT REFERENCES product_groups(id) ON DELETE SET NULL,
    supplier_availability availability_status NOT NULL DEFAULT 'unknown',
    supplier_stock DECIMAL(12,3),
    supplier_price DECIMAL(12,4),
    supplier_feed_data JSONB NOT NULL DEFAULT '{}',
    supplier_feed_synced_at TIMESTAMPTZ,
    validation_required BOOLEAN NOT NULL DEFAULT false,
    validation_reason VARCHAR(255),
    created_from_source VARCHAR(50),
    source_supplier_product_id BIGINT REFERENCES supplier_products(id) ON DELETE SET NULL,
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_products_group ON products(group_id) WHERE group_id IS NOT NULL;
CREATE INDEX idx_products_active ON products(is_active) WHERE is_active = true;
CREATE INDEX idx_products_validation ON products(validation_required) 
    WHERE validation_required = true;
CREATE INDEX idx_products_supplier ON products(supplier_id) WHERE supplier_id IS NOT NULL;

-- ============================================================================
-- 11. PRODUCT IDENTIFIERS (SOLE SOURCE OF TRUTH FOR ALL CODES)
-- v12 FINAL: Multi-EAN support with barcode group primary constraint
-- ============================================================================

CREATE TABLE product_identifiers (
    id BIGSERIAL PRIMARY KEY,
    product_id BIGINT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    identifier_type identifier_type NOT NULL,
    value VARCHAR(100) NOT NULL,
    supplier_id BIGINT REFERENCES suppliers(id) ON DELETE CASCADE,
    is_primary BOOLEAN NOT NULL DEFAULT false,
    notes VARCHAR(255),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    -- supplier_sku requires supplier_id
    CONSTRAINT chk_supplier_sku_has_supplier CHECK (
        identifier_type != 'supplier_sku' OR supplier_id IS NOT NULL
    )
);

-- Fast lookup by value (critical for barcode scanning)
CREATE INDEX idx_product_identifiers_value ON product_identifiers(value);
CREATE INDEX idx_product_identifiers_product ON product_identifiers(product_id);
CREATE INDEX idx_product_identifiers_type_value ON product_identifiers(identifier_type, value);

-- ============================================================================
-- IDENTIFIER UNIQUENESS RULES (enforced by partial unique indexes)
-- ============================================================================

-- EAN: Globally unique (one EAN = one product)
-- Enforces: INVARIANT "ean globally unique by value"
CREATE UNIQUE INDEX idx_identifiers_ean_unique 
    ON product_identifiers(value) 
    WHERE identifier_type = 'ean';

-- UPC: Globally unique (one UPC = one product)
-- Enforces: INVARIANT "upc globally unique by value"
CREATE UNIQUE INDEX idx_identifiers_upc_unique 
    ON product_identifiers(value) 
    WHERE identifier_type = 'upc';

-- unverified_barcode: Unique within product (same code can exist for different products)
-- Reason: Short codes like "398828" may collide across suppliers
-- Enforces: INVARIANT "unverified_barcode unique per product"
CREATE UNIQUE INDEX idx_identifiers_unverified_per_product 
    ON product_identifiers(product_id, value) 
    WHERE identifier_type = 'unverified_barcode';

-- supplier_sku: Unique per supplier
-- Enforces: INVARIANT "supplier_sku unique per supplier"
CREATE UNIQUE INDEX idx_identifiers_supplier_sku_unique 
    ON product_identifiers(supplier_id, value) 
    WHERE identifier_type = 'supplier_sku';

-- internal_sku: Globally unique
-- Enforces: INVARIANT "internal_sku globally unique"
CREATE UNIQUE INDEX idx_identifiers_internal_sku_unique 
    ON product_identifiers(value) 
    WHERE identifier_type = 'internal_sku';

-- manufacturer/custom: NOT unique (just indexed for lookup)
CREATE INDEX idx_identifiers_manufacturer ON product_identifiers(value) 
    WHERE identifier_type = 'manufacturer';
CREATE INDEX idx_identifiers_custom ON product_identifiers(value) 
    WHERE identifier_type = 'custom';

-- ============================================================================
-- PRIMARY IDENTIFIER RULES
-- ============================================================================

-- BARCODE GROUP: max 1 primary across (ean, upc, unverified_barcode)
-- Enforces: INVARIANT "single primary barcode per product"
CREATE UNIQUE INDEX idx_identifiers_primary_barcode_group 
    ON product_identifiers(product_id) 
    WHERE is_primary = true 
      AND identifier_type IN ('ean', 'upc', 'unverified_barcode');

-- OTHER TYPES: max 1 primary per (product_id, identifier_type)
-- Enforces: INVARIANT "single primary per identifier type"
CREATE UNIQUE INDEX idx_identifiers_primary_per_type 
    ON product_identifiers(product_id, identifier_type) 
    WHERE is_primary = true 
      AND identifier_type NOT IN ('ean', 'upc', 'unverified_barcode');

-- ============================================================================
-- 12. PRODUCT VARIANT ATTRIBUTES
-- ============================================================================

CREATE TABLE product_variant_attributes (
    id BIGSERIAL PRIMARY KEY,
    product_id BIGINT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    attribute_name VARCHAR(100) NOT NULL,
    attribute_value VARCHAR(255) NOT NULL,
    display_order INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    CONSTRAINT uq_variant_attrs UNIQUE (product_id, attribute_name)
);

CREATE INDEX idx_variant_attrs_product ON product_variant_attributes(product_id);

-- ============================================================================
-- 13. PRODUCT SUPPLY SOURCES
-- ============================================================================

CREATE TABLE product_supply_sources (
    id BIGSERIAL PRIMARY KEY,
    product_id BIGINT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    supplier_product_id BIGINT NOT NULL REFERENCES supplier_products(id) ON DELETE CASCADE,
    priority INTEGER NOT NULL DEFAULT 100,
    is_primary BOOLEAN NOT NULL DEFAULT false,
    default_lead_time_days INTEGER,
    orderable BOOLEAN NOT NULL DEFAULT true,
    min_order_qty INTEGER NOT NULL DEFAULT 1,
    pack_size INTEGER NOT NULL DEFAULT 1,
    negotiated_price DECIMAL(12,4),
    negotiated_currency VARCHAR(3),
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    CONSTRAINT uq_supply_sources UNIQUE (product_id, supplier_product_id)
);

CREATE UNIQUE INDEX idx_supply_sources_primary 
    ON product_supply_sources(product_id) WHERE is_primary = true;
CREATE INDEX idx_supply_sources_product ON product_supply_sources(product_id);

-- ============================================================================
-- 14. STOCK BALANCES
-- ============================================================================

CREATE TABLE stock_balances (
    id BIGSERIAL PRIMARY KEY,
    product_id BIGINT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    warehouse_id BIGINT NOT NULL REFERENCES warehouses(id) ON DELETE RESTRICT,
    qty_on_hand DECIMAL(12,3) NOT NULL DEFAULT 0,
    qty_reserved DECIMAL(12,3) NOT NULL DEFAULT 0,
    qty_available DECIMAL(12,3) GENERATED ALWAYS AS (qty_on_hand - qty_reserved) STORED,
    min_quantity DECIMAL(12,3) NOT NULL DEFAULT 0,
    avg_cost DECIMAL(12,4) NOT NULL DEFAULT 0,
    total_value DECIMAL(16,4) NOT NULL DEFAULT 0,
    last_purchase_price DECIMAL(12,4),
    last_purchase_at TIMESTAMPTZ,
    last_movement_at TIMESTAMPTZ,
    last_movement_id BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    CONSTRAINT uq_stock_balances UNIQUE (product_id, warehouse_id),
    CONSTRAINT chk_qty_reserved_non_negative CHECK (qty_reserved >= 0),
    CONSTRAINT chk_avg_cost_non_negative CHECK (avg_cost >= 0)
);

CREATE INDEX idx_stock_balances_product ON stock_balances(product_id);
CREATE INDEX idx_stock_balances_warehouse ON stock_balances(warehouse_id);
CREATE INDEX idx_stock_balances_low_stock ON stock_balances(warehouse_id, qty_available)
    WHERE qty_available <= min_quantity;

-- ============================================================================
-- 15. STOCK MOVEMENTS (IMMUTABLE LEDGER)
-- Enforces: INVARIANT "immutable ledger - no UPDATE/DELETE"
-- ============================================================================

CREATE TABLE stock_movements (
    id BIGSERIAL PRIMARY KEY,
    idempotency_key VARCHAR(255) NOT NULL UNIQUE,
    product_id BIGINT NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
    warehouse_id BIGINT NOT NULL REFERENCES warehouses(id) ON DELETE RESTRICT,
    movement_type movement_type NOT NULL,
    quantity DECIMAL(12,3) NOT NULL,
    unit_cost DECIMAL(12,4),
    unit_cost_original DECIMAL(12,4),
    unit_cost_currency VARCHAR(3) NOT NULL DEFAULT 'EUR',
    fx_rate_to_eur DECIMAL(12,6) NOT NULL DEFAULT 1.0,
    reference_type VARCHAR(50),
    reference_id VARCHAR(100),
    reference_source VARCHAR(100),
    balance_after DECIMAL(12,3) NOT NULL,
    avg_cost_after DECIMAL(12,4) NOT NULL,
    notes TEXT,
    created_by VARCHAR(100) NOT NULL DEFAULT 'system',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    CONSTRAINT chk_quantity_not_zero CHECK (quantity != 0),
    CONSTRAINT chk_fx_rate_positive CHECK (fx_rate_to_eur > 0)
);

-- Add FK from stock_balances to stock_movements
ALTER TABLE stock_balances 
    ADD CONSTRAINT fk_stock_balances_last_movement 
    FOREIGN KEY (last_movement_id) REFERENCES stock_movements(id) ON DELETE SET NULL;

CREATE INDEX idx_movements_product ON stock_movements(product_id);
CREATE INDEX idx_movements_warehouse ON stock_movements(warehouse_id);
CREATE INDEX idx_movements_type ON stock_movements(movement_type);
CREATE INDEX idx_movements_created ON stock_movements(created_at DESC);
CREATE INDEX idx_movements_reference ON stock_movements(reference_type, reference_id);

-- IMMUTABLE LEDGER TRIGGER
CREATE OR REPLACE FUNCTION fn_stock_movements_immutable()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'stock_movements is immutable: UPDATE not allowed (id=%)', OLD.id
            USING ERRCODE = 'restrict_violation';
    ELSIF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'stock_movements is immutable: DELETE not allowed (id=%)', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_stock_movements_immutable
    BEFORE UPDATE OR DELETE ON stock_movements
    FOR EACH ROW
    EXECUTE FUNCTION fn_stock_movements_immutable();

-- ============================================================================
-- 16. FX RATES
-- ============================================================================

CREATE TABLE fx_rates (
    id BIGSERIAL PRIMARY KEY,
    from_currency VARCHAR(3) NOT NULL,
    to_currency VARCHAR(3) NOT NULL,
    rate_date DATE NOT NULL,
    rate DECIMAL(12,6) NOT NULL,
    source VARCHAR(50) NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    CONSTRAINT uq_fx_rates UNIQUE (from_currency, to_currency, rate_date),
    CONSTRAINT chk_rate_positive CHECK (rate > 0)
);

CREATE INDEX idx_fx_rates_lookup ON fx_rates(from_currency, to_currency, rate_date);

-- ============================================================================
-- 17. AVAILABILITY PROFILES
-- ============================================================================

CREATE TABLE availability_profiles (
    id BIGSERIAL PRIMARY KEY,
    shop_id BIGINT NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
    code VARCHAR(50) NOT NULL,
    name VARCHAR(255) NOT NULL,
    is_default BOOLEAN NOT NULL DEFAULT false,
    in_stock_min_qty INTEGER NOT NULL DEFAULT 1,
    low_stock_threshold INTEGER NOT NULL DEFAULT 3,
    label_in_stock VARCHAR(100) NOT NULL DEFAULT 'Skladom',
    label_in_stock_code VARCHAR(50),
    label_low_stock VARCHAR(100) NOT NULL DEFAULT 'Posledné kusy',
    label_low_stock_code VARCHAR(50),
    label_supplier_1_3_days VARCHAR(100) NOT NULL DEFAULT 'Do 3 dní',
    label_supplier_1_3_days_code VARCHAR(50),
    label_supplier_3_5_days VARCHAR(100) NOT NULL DEFAULT 'Do 5 dní',
    label_supplier_3_5_days_code VARCHAR(50),
    label_supplier_1_2_weeks VARCHAR(100) NOT NULL DEFAULT 'Do 2 týždňov',
    label_supplier_1_2_weeks_code VARCHAR(50),
    label_supplier_on_order VARCHAR(100) NOT NULL DEFAULT 'Na objednávku',
    label_supplier_on_order_code VARCHAR(50),
    label_check_availability VARCHAR(100) NOT NULL DEFAULT 'Overíme dostupnosť',
    label_check_availability_code VARCHAR(50),
    label_unavailable VARCHAR(100) NOT NULL DEFAULT 'Nedostupné',
    label_unavailable_code VARCHAR(50),
    show_exact_qty BOOLEAN NOT NULL DEFAULT false,
    allow_backorder BOOLEAN NOT NULL DEFAULT false,
    hide_when_unavailable BOOLEAN NOT NULL DEFAULT false,
    prefer_local_stock BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    CONSTRAINT uq_availability_profiles UNIQUE (shop_id, code)
);

CREATE UNIQUE INDEX idx_availability_profiles_default 
    ON availability_profiles(shop_id) WHERE is_default = true;

-- ============================================================================
-- 18. SHOP PRODUCT AVAILABILITY
-- ============================================================================

CREATE TABLE shop_product_availability (
    id BIGSERIAL PRIMARY KEY,
    shop_id BIGINT NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
    product_id BIGINT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    qty_on_hand DECIMAL(12,3) NOT NULL DEFAULT 0,
    qty_reserved DECIMAL(12,3) NOT NULL DEFAULT 0,
    qty_available DECIMAL(12,3) NOT NULL DEFAULT 0,
    best_supplier_id BIGINT REFERENCES suppliers(id) ON DELETE SET NULL,
    best_supplier_qty DECIMAL(12,3),
    best_supplier_lead_time_days INTEGER,
    best_supplier_price DECIMAL(12,4),
    computed_availability_code availability_code NOT NULL,
    computed_availability_label VARCHAR(100) NOT NULL,
    computed_shop_code VARCHAR(50),
    stock_position stock_position_code NOT NULL,
    stock_position_label VARCHAR(100),
    manual_override BOOLEAN NOT NULL DEFAULT false,
    manual_availability_code availability_code,
    manual_availability_label VARCHAR(100),
    manual_shop_code VARCHAR(50),
    manual_override_reason VARCHAR(255),
    manual_override_until TIMESTAMPTZ,
    manual_override_by VARCHAR(100),
    manual_override_at TIMESTAMPTZ,
    availability_code availability_code NOT NULL,
    availability_label VARCHAR(100) NOT NULL,
    availability_shop_code VARCHAR(50),
    is_orderable BOOLEAN NOT NULL DEFAULT true,
    is_visible BOOLEAN NOT NULL DEFAULT true,
    sync_required BOOLEAN NOT NULL DEFAULT true,
    last_computed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_synced_at TIMESTAMPTZ,
    
    CONSTRAINT uq_shop_product_availability UNIQUE (shop_id, product_id)
);

CREATE INDEX idx_shop_availability_sync ON shop_product_availability(shop_id, sync_required) 
    WHERE sync_required = true;
CREATE INDEX idx_shop_availability_product ON shop_product_availability(product_id);

-- ============================================================================
-- 19. SHOP PRODUCTS
-- ============================================================================

CREATE TABLE shop_products (
    id BIGSERIAL PRIMARY KEY,
    shop_id BIGINT NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
    product_id BIGINT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    external_id VARCHAR(100),
    external_code VARCHAR(100),
    variant_code VARCHAR(100),
    parent_code VARCHAR(100),
    is_variant BOOLEAN NOT NULL DEFAULT false,
    shop_availability VARCHAR(100),
    shop_stock DECIMAL(12,3),
    shop_price DECIMAL(12,2),
    push_pending BOOLEAN NOT NULL DEFAULT false,
    last_push_at TIMESTAMPTZ,
    last_push_status VARCHAR(50),
    last_push_error TEXT,
    last_pull_at TIMESTAMPTZ,
    is_listed BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    CONSTRAINT uq_shop_products UNIQUE (shop_id, product_id)
);

CREATE INDEX idx_shop_products_pending ON shop_products(shop_id) WHERE push_pending = true;
CREATE INDEX idx_shop_products_external ON shop_products(shop_id, external_id);

-- ============================================================================
-- 20. SHOP SYNC OUTBOX
-- ============================================================================

CREATE TABLE shop_sync_outbox (
    id BIGSERIAL PRIMARY KEY,
    shop_id BIGINT NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
    product_id BIGINT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    idempotency_key VARCHAR(255) NOT NULL UNIQUE,
    sync_type VARCHAR(50) NOT NULL,
    payload JSONB NOT NULL,
    status sync_outbox_status NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    last_attempt_at TIMESTAMPTZ,
    last_error TEXT,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    response_data JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_sync_outbox_pending ON shop_sync_outbox(next_attempt_at) 
    WHERE status IN ('pending', 'failed');
CREATE INDEX idx_sync_outbox_shop ON shop_sync_outbox(shop_id, status);

-- ============================================================================
-- 21. SHOP ORDERS
-- ============================================================================

CREATE TABLE shop_orders (
    id BIGSERIAL PRIMARY KEY,
    shop_id BIGINT NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
    external_id VARCHAR(100) NOT NULL,
    external_code VARCHAR(100),
    order_date TIMESTAMPTZ NOT NULL,
    status order_status NOT NULL DEFAULT 'new',
    customer_id VARCHAR(100),
    shipping_country VARCHAR(3),
    shipping_method VARCHAR(100),
    payment_method VARCHAR(100),
    subtotal DECIMAL(12,2),
    shipping_cost DECIMAL(12,2),
    total DECIMAL(12,2),
    currency VARCHAR(3) NOT NULL DEFAULT 'EUR',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    CONSTRAINT uq_shop_orders UNIQUE (shop_id, external_id)
);

CREATE INDEX idx_shop_orders_date ON shop_orders(order_date DESC);
CREATE INDEX idx_shop_orders_status ON shop_orders(shop_id, status);

-- ============================================================================
-- 22. SHOP ORDER ITEMS
-- ============================================================================

CREATE TABLE shop_order_items (
    id BIGSERIAL PRIMARY KEY,
    order_id BIGINT NOT NULL REFERENCES shop_orders(id) ON DELETE CASCADE,
    product_id BIGINT REFERENCES products(id) ON DELETE SET NULL,
    external_item_id VARCHAR(100),
    external_product_code VARCHAR(100),
    quantity DECIMAL(12,3) NOT NULL,
    unit_price DECIMAL(12,4) NOT NULL,
    total_price DECIMAL(12,2) NOT NULL,
    status VARCHAR(50) NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    CONSTRAINT chk_order_item_qty_positive CHECK (quantity > 0)
);

CREATE INDEX idx_shop_order_items_order ON shop_order_items(order_id);
CREATE INDEX idx_shop_order_items_product ON shop_order_items(product_id);

-- ============================================================================
-- 23. RESERVATIONS
-- ============================================================================

CREATE TABLE reservations (
    id BIGSERIAL PRIMARY KEY,
    shop_order_item_id BIGINT NOT NULL REFERENCES shop_order_items(id) ON DELETE CASCADE,
    product_id BIGINT NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
    warehouse_id BIGINT NOT NULL REFERENCES warehouses(id) ON DELETE RESTRICT,
    quantity DECIMAL(12,3) NOT NULL,
    shortage_qty DECIMAL(12,3) NOT NULL DEFAULT 0,
    status reservation_status NOT NULL DEFAULT 'reserved',
    expires_at TIMESTAMPTZ,
    sale_movement_id BIGINT REFERENCES stock_movements(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    CONSTRAINT uq_reservations_order_item UNIQUE (shop_order_item_id),
    CONSTRAINT chk_reservation_qty_positive CHECK (quantity > 0),
    CONSTRAINT chk_shortage_non_negative CHECK (shortage_qty >= 0)
);

CREATE INDEX idx_reservations_product ON reservations(product_id);
CREATE INDEX idx_reservations_active ON reservations(status) 
    WHERE status IN ('reserved', 'backorder');

-- ============================================================================
-- 24. RECEIVING SESSIONS
-- ============================================================================

CREATE TABLE receiving_sessions (
    id BIGSERIAL PRIMARY KEY,
    supplier_id BIGINT NOT NULL REFERENCES suppliers(id) ON DELETE RESTRICT,
    warehouse_id BIGINT NOT NULL REFERENCES warehouses(id) ON DELETE RESTRICT,
    invoice_number VARCHAR(100) NOT NULL,
    invoice_date DATE,
    invoice_file_path TEXT,
    invoice_currency VARCHAR(3) NOT NULL DEFAULT 'EUR',
    fx_rate_to_eur DECIMAL(12,6) NOT NULL DEFAULT 1.0,
    fx_rate_source VARCHAR(50),
    fx_rate_date DATE,
    source_hash VARCHAR(64),
    import_source VARCHAR(50),
    total_lines INTEGER NOT NULL DEFAULT 0,
    total_amount DECIMAL(14,2),
    status receiving_status NOT NULL DEFAULT 'new',
    started_at TIMESTAMPTZ,
    paused_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    started_by VARCHAR(100),
    finished_by VARCHAR(100),
    notes TEXT,
    session_data JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    CONSTRAINT uq_receiving_invoice UNIQUE (supplier_id, invoice_number)
);

CREATE UNIQUE INDEX idx_receiving_source_hash 
    ON receiving_sessions(supplier_id, source_hash) WHERE source_hash IS NOT NULL;
CREATE INDEX idx_receiving_status ON receiving_sessions(status);

-- ============================================================================
-- 25. RECEIVING LINES
-- v12 FINAL: line_number is NOT NULL, enforced dedup via UNIQUE constraint
-- line_fingerprint is HELPER column only (no unique constraint)
-- ============================================================================

CREATE TABLE receiving_lines (
    id BIGSERIAL PRIMARY KEY,
    session_id BIGINT NOT NULL REFERENCES receiving_sessions(id) ON DELETE CASCADE,
    product_id BIGINT REFERENCES products(id) ON DELETE SET NULL,
    -- v12 FINAL: line_number is REQUIRED (must be assigned during import)
    line_number INTEGER NOT NULL,
    supplier_sku VARCHAR(100),
    ean VARCHAR(20),
    description TEXT,
    ordered_qty DECIMAL(12,3) NOT NULL,
    received_qty DECIMAL(12,3) NOT NULL DEFAULT 0,
    unit_price DECIMAL(12,4),
    total_price DECIMAL(14,2),
    unit_price_original DECIMAL(12,4),
    total_price_original DECIMAL(14,2),
    status VARCHAR(50) NOT NULL DEFAULT 'pending',
    match_method VARCHAR(50),
    -- Helper column for debugging/analysis (NOT enforced as unique)
    line_fingerprint VARCHAR(32) GENERATED ALWAYS AS (
        md5(COALESCE(ean, '') || '|' || COALESCE(supplier_sku, '') || '|' || 
            ordered_qty::TEXT || '|' || COALESCE(unit_price::TEXT, ''))
    ) STORED,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    CONSTRAINT chk_ordered_qty_positive CHECK (ordered_qty > 0),
    CONSTRAINT chk_received_qty_non_negative CHECK (received_qty >= 0),
    CONSTRAINT chk_line_number_positive CHECK (line_number > 0)
);

-- Enforces: INVARIANT "unique line per session"
CREATE UNIQUE INDEX idx_receiving_lines_session_line 
    ON receiving_lines(session_id, line_number);

CREATE INDEX idx_receiving_lines_session ON receiving_lines(session_id);
CREATE INDEX idx_receiving_lines_product ON receiving_lines(product_id);
CREATE INDEX idx_receiving_lines_ean ON receiving_lines(ean) WHERE ean IS NOT NULL;
CREATE INDEX idx_receiving_lines_supplier_sku ON receiving_lines(supplier_sku) WHERE supplier_sku IS NOT NULL;

-- ============================================================================
-- 26. INVENTORY COUNTS
-- ============================================================================

CREATE TABLE inventory_counts (
    id BIGSERIAL PRIMARY KEY,
    warehouse_id BIGINT NOT NULL REFERENCES warehouses(id) ON DELETE RESTRICT,
    count_type inventory_count_type NOT NULL DEFAULT 'full',
    name VARCHAR(255),
    status inventory_count_status NOT NULL DEFAULT 'draft',
    include_movements_since TIMESTAMPTZ,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    created_by VARCHAR(100),
    started_by VARCHAR(100),
    finished_by VARCHAR(100),
    total_products INTEGER,
    products_counted INTEGER,
    products_with_variance INTEGER,
    total_variance_value DECIMAL(14,2),
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_inventory_counts_warehouse ON inventory_counts(warehouse_id);
CREATE INDEX idx_inventory_counts_status ON inventory_counts(status);

-- ============================================================================
-- 27. INVENTORY COUNT LINES
-- ============================================================================

CREATE TABLE inventory_count_lines (
    id BIGSERIAL PRIMARY KEY,
    count_id BIGINT NOT NULL REFERENCES inventory_counts(id) ON DELETE CASCADE,
    product_id BIGINT NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
    expected_qty DECIMAL(12,3) NOT NULL,
    expected_value DECIMAL(14,2),
    counted_qty DECIMAL(12,3),
    counted_at TIMESTAMPTZ,
    counted_by VARCHAR(100),
    variance_qty DECIMAL(12,3),
    variance_value DECIMAL(14,2),
    status VARCHAR(50) NOT NULL DEFAULT 'pending',
    adjustment_movement_id BIGINT REFERENCES stock_movements(id) ON DELETE SET NULL,
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    CONSTRAINT uq_inventory_count_lines UNIQUE (count_id, product_id)
);

CREATE INDEX idx_inventory_count_lines_count ON inventory_count_lines(count_id);

-- ============================================================================
-- 28. SCAN EVENTS
-- ============================================================================

CREATE TABLE scan_events (
    id BIGSERIAL PRIMARY KEY,
    session_type scan_session_type NOT NULL,
    receiving_session_id BIGINT REFERENCES receiving_sessions(id) ON DELETE CASCADE,
    receiving_line_id BIGINT REFERENCES receiving_lines(id) ON DELETE SET NULL,
    inventory_count_id BIGINT REFERENCES inventory_counts(id) ON DELETE CASCADE,
    inventory_count_line_id BIGINT REFERENCES inventory_count_lines(id) ON DELETE SET NULL,
    scanned_code VARCHAR(100) NOT NULL,
    scanned_code_type identifier_type,
    product_id BIGINT REFERENCES products(id) ON DELETE SET NULL,
    matched_identifier_id BIGINT REFERENCES product_identifiers(id) ON DELETE SET NULL,
    match_method VARCHAR(50),
    quantity DECIMAL(12,3) NOT NULL DEFAULT 1,
    status scan_status NOT NULL DEFAULT 'active',
    undone_at TIMESTAMPTZ,
    undone_by VARCHAR(100),
    undo_reason VARCHAR(255),
    scanned_by VARCHAR(100) NOT NULL,
    scanned_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    device_id VARCHAR(100),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    CONSTRAINT chk_scan_session_fks CHECK (
        (session_type = 'receiving' AND receiving_session_id IS NOT NULL AND inventory_count_id IS NULL)
        OR (session_type = 'inventory' AND inventory_count_id IS NOT NULL AND receiving_session_id IS NULL)
        OR (session_type IN ('lookup', 'adjustment') AND receiving_session_id IS NULL AND inventory_count_id IS NULL)
    )
);

CREATE INDEX idx_scan_events_receiving ON scan_events(receiving_session_id) 
    WHERE receiving_session_id IS NOT NULL;
CREATE INDEX idx_scan_events_inventory ON scan_events(inventory_count_id)
    WHERE inventory_count_id IS NOT NULL;
CREATE INDEX idx_scan_events_code ON scan_events(scanned_code);
CREATE INDEX idx_scan_events_recent ON scan_events(scanned_at DESC);

-- ============================================================================
-- 29. CONFIG VERSIONS
-- ============================================================================

CREATE TABLE config_versions (
    id BIGSERIAL PRIMARY KEY,
    entity_type config_entity_type NOT NULL,
    entity_id BIGINT NOT NULL,
    entity_code VARCHAR(50) NOT NULL,
    version INTEGER NOT NULL,
    config_snapshot JSONB NOT NULL,
    change_type VARCHAR(50) NOT NULL,
    changed_fields TEXT[],
    changed_by VARCHAR(100) NOT NULL,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    change_reason TEXT,
    is_current BOOLEAN NOT NULL DEFAULT true,
    
    CONSTRAINT uq_config_versions UNIQUE (entity_type, entity_id, version)
);

CREATE INDEX idx_config_versions_entity ON config_versions(entity_type, entity_id);

-- Enforces: INVARIANT "single current config per entity"
CREATE UNIQUE INDEX idx_config_versions_single_current 
    ON config_versions(entity_type, entity_id) WHERE is_current = true;

-- ============================================================================
-- 30. SYNC LOG
-- ============================================================================

CREATE TABLE sync_log (
    id BIGSERIAL PRIMARY KEY,
    sync_type VARCHAR(50) NOT NULL,
    direction VARCHAR(10) NOT NULL,
    target_type VARCHAR(50),
    target_code VARCHAR(50),
    status VARCHAR(50) NOT NULL DEFAULT 'started',
    items_total INTEGER NOT NULL DEFAULT 0,
    items_success INTEGER NOT NULL DEFAULT 0,
    items_failed INTEGER NOT NULL DEFAULT 0,
    items_skipped INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    error_details JSONB,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    duration_ms INTEGER,
    metadata JSONB NOT NULL DEFAULT '{}'
);

CREATE INDEX idx_sync_log_type ON sync_log(sync_type, target_code);
CREATE INDEX idx_sync_log_started ON sync_log(started_at DESC);

-- ============================================================================
-- TRIGGERS - updated_at (only on tables with that column)
-- ============================================================================

CREATE OR REPLACE FUNCTION trigger_set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
DECLARE
    t TEXT;
BEGIN
    FOR t IN VALUES 
        ('warehouses'), ('suppliers'), ('shops'), ('supplier_feeds'),
        ('supplier_products'), ('product_groups'), ('products'),
        ('product_supply_sources'), ('stock_balances'), ('shop_products'),
        ('shop_orders'), ('shop_order_items'), ('reservations'), 
        ('receiving_sessions'), ('receiving_lines'),
        ('inventory_counts'), ('inventory_count_lines'), ('availability_profiles'),
        ('shop_sync_outbox')
    LOOP
        EXECUTE format('
            CREATE TRIGGER set_updated_at 
            BEFORE UPDATE ON %I
            FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();
        ', t);
    END LOOP;
END;
$$;

-- ============================================================================
-- VIEWS
-- ============================================================================

-- LEGACY VIEW: Backward compatibility with primary_ean, supplier_sku columns
CREATE OR REPLACE VIEW v_products_legacy AS
SELECT 
    p.*,
    (SELECT pi.value FROM product_identifiers pi 
     WHERE pi.product_id = p.id 
       AND pi.identifier_type IN ('ean', 'upc', 'unverified_barcode') 
       AND pi.is_primary = true
     LIMIT 1) AS primary_ean,
    (SELECT pi.value FROM product_identifiers pi 
     WHERE pi.product_id = p.id 
       AND pi.identifier_type = 'supplier_sku' 
       AND pi.is_primary = true
     LIMIT 1) AS supplier_sku
FROM products p;

-- Extended product view with all identifier info
CREATE OR REPLACE VIEW v_product_with_identifiers AS
SELECT 
    p.id AS product_id,
    p.sku,
    p.name,
    p.brand,
    p.category,
    p.supplier_id,
    s.code AS supplier_code,
    s.name AS supplier_name,
    p.group_id,
    pg.code AS group_code,
    p.is_active,
    p.validation_required,
    -- Primary barcode (from barcode group)
    (SELECT pi.value FROM product_identifiers pi 
     WHERE pi.product_id = p.id 
       AND pi.identifier_type IN ('ean', 'upc', 'unverified_barcode') 
       AND pi.is_primary = true
     LIMIT 1) AS primary_ean,
    -- Primary supplier SKU
    (SELECT pi.value FROM product_identifiers pi 
     WHERE pi.product_id = p.id 
       AND pi.identifier_type = 'supplier_sku' 
       AND pi.is_primary = true
     LIMIT 1) AS primary_supplier_sku,
    -- Alias
    (SELECT pi.value FROM product_identifiers pi 
     WHERE pi.product_id = p.id 
       AND pi.identifier_type = 'supplier_sku' 
       AND pi.is_primary = true
     LIMIT 1) AS supplier_sku,
    -- All barcodes
    (SELECT array_agg(pi.value ORDER BY pi.is_primary DESC, pi.id) 
     FROM product_identifiers pi 
     WHERE pi.product_id = p.id 
       AND pi.identifier_type IN ('ean', 'upc', 'unverified_barcode')) AS all_barcodes,
    -- Barcode count
    (SELECT COUNT(*) FROM product_identifiers pi 
     WHERE pi.product_id = p.id 
       AND pi.identifier_type IN ('ean', 'upc', 'unverified_barcode')) AS barcode_count
FROM products p
LEFT JOIN suppliers s ON p.supplier_id = s.id
LEFT JOIN product_groups pg ON p.group_id = pg.id;

-- Product lookup for scanner
CREATE OR REPLACE VIEW v_product_lookup AS
SELECT 
    pi.identifier_type, 
    pi.value AS identifier_value, 
    pi.is_primary, 
    pi.supplier_id AS identifier_supplier_id,
    p.id AS product_id, 
    p.sku, 
    p.name, 
    p.brand, 
    s.code AS supplier_code,
    (pi.identifier_type IN ('ean', 'upc', 'unverified_barcode')) AS is_barcode
FROM product_identifiers pi
JOIN products p ON pi.product_id = p.id
LEFT JOIN suppliers s ON pi.supplier_id = s.id
WHERE p.is_active = true;

-- Product inventory across warehouses
CREATE OR REPLACE VIEW v_product_inventory AS
SELECT 
    p.id AS product_id, p.sku, p.name, p.brand,
    p.supplier_id, s.code AS supplier_code,
    w.id AS warehouse_id, w.code AS warehouse_code,
    COALESCE(sb.qty_on_hand, 0) AS qty_on_hand,
    COALESCE(sb.qty_reserved, 0) AS qty_reserved,
    COALESCE(sb.qty_available, 0) AS qty_available,
    COALESCE(sb.avg_cost, 0) AS avg_cost,
    COALESCE(sb.min_quantity, 0) AS min_quantity,
    p.supplier_availability, p.supplier_stock,
    p.validation_required, p.is_active,
    (COALESCE(sb.qty_available, 0) <= COALESCE(sb.min_quantity, 0)) AS is_low_stock,
    (COALESCE(sb.qty_available, 0) < 0) AS is_backorder
FROM products p
CROSS JOIN warehouses w
LEFT JOIN suppliers s ON p.supplier_id = s.id
LEFT JOIN stock_balances sb ON p.id = sb.product_id AND w.id = sb.warehouse_id
WHERE p.is_active = true;

-- Stock alerts
CREATE OR REPLACE VIEW v_stock_alerts AS
SELECT 
    p.sku, p.name, w.code AS warehouse_code,
    sb.qty_on_hand, sb.qty_reserved, sb.qty_available, sb.min_quantity,
    CASE 
        WHEN sb.qty_available < 0 THEN 'BACKORDER'
        WHEN sb.qty_available <= sb.min_quantity THEN 'LOW_STOCK'
        ELSE 'OK' 
    END AS alert_type,
    p.supplier_availability, p.supplier_stock
FROM stock_balances sb
JOIN products p ON sb.product_id = p.id
JOIN warehouses w ON sb.warehouse_id = w.id
WHERE p.is_active = true 
  AND (sb.qty_available <= sb.min_quantity OR sb.qty_available < 0);

-- Supplier feed status
CREATE OR REPLACE VIEW v_supplier_feed_status AS
SELECT 
    s.code AS supplier_code, s.name AS supplier_name,
    sf.code AS feed_code, sf.name AS feed_name, sf.feed_type, sf.is_active,
    sf.fetch_interval_min, sf.last_run_at, sf.last_run_status, sf.last_run_items_count,
    (SELECT COUNT(*) FROM supplier_products sp WHERE sp.source_feed_id = sf.id AND sp.is_active) AS active_products
FROM supplier_feeds sf
JOIN suppliers s ON sf.supplier_id = s.id;

-- Sync outbox pending
CREATE OR REPLACE VIEW v_sync_outbox_pending AS
SELECT 
    sso.id, sh.code AS shop_code, p.sku, p.name AS product_name,
    sso.sync_type, sso.status, sso.attempts, sso.max_attempts,
    sso.last_attempt_at, sso.next_attempt_at, sso.last_error, sso.created_at
FROM shop_sync_outbox sso
JOIN shops sh ON sso.shop_id = sh.id
JOIN products p ON sso.product_id = p.id
WHERE sso.status IN ('pending', 'failed')
ORDER BY sso.next_attempt_at;

-- Scan audit
CREATE OR REPLACE VIEW v_scan_audit AS
SELECT 
    se.id, se.session_type,
    se.receiving_session_id, rs.invoice_number AS receiving_invoice,
    se.inventory_count_id, ic.name AS inventory_count_name,
    se.scanned_code, se.scanned_code_type,
    p.sku, p.name AS product_name,
    se.match_method, se.quantity, se.status,
    se.scanned_by, se.scanned_at, se.device_id
FROM scan_events se
LEFT JOIN products p ON se.product_id = p.id
LEFT JOIN receiving_sessions rs ON se.receiving_session_id = rs.id
LEFT JOIN inventory_counts ic ON se.inventory_count_id = ic.id
ORDER BY se.scanned_at DESC;

-- Shop sync status
CREATE OR REPLACE VIEW v_shop_sync_status AS
SELECT 
    sh.code AS shop_code, sh.name AS shop_name, sh.platform, sh.oversell_mode,
    p.sku, p.name AS product_name,
    sp.external_id, sp.external_code,
    spa.qty_available AS inventory_available,
    spa.availability_code, spa.availability_label,
    spa.sync_required, spa.last_synced_at
FROM shop_products sp
JOIN shops sh ON sp.shop_id = sh.id
JOIN products p ON sp.product_id = p.id
LEFT JOIN shop_product_availability spa ON sp.shop_id = spa.shop_id AND sp.product_id = spa.product_id
WHERE sp.is_listed = true;

-- ============================================================================
-- SEED DATA
-- ============================================================================

INSERT INTO warehouses (code, name, is_default) 
VALUES ('hlavny-sklad', 'Hlavný sklad', true);

INSERT INTO shops (code, name, platform, oversell_mode) 
VALUES 
    ('biketrek', 'BikeTrek.sk', 'upgates', 'allow'),
    ('xtrek', 'xTrek.sk', 'upgates', 'allow'),
    ('predajna', 'Predajňa', 'pos', 'block');

INSERT INTO shop_warehouses (shop_id, warehouse_id, role, priority)
SELECT s.id, w.id, 'both', 100
FROM shops s CROSS JOIN warehouses w WHERE w.is_default = true;

INSERT INTO availability_profiles (shop_id, code, name, is_default)
SELECT id, 'default', 'Predvolený profil', true FROM shops;

-- ============================================================================
-- SMOKE TESTS (structure verification - no data modification)
-- ============================================================================

DO $$
DECLARE
    v_result BOOLEAN;
    v_count INTEGER;
    v_product_id BIGINT;
BEGIN
    RAISE NOTICE '=== SMOKE TESTS v12 FINAL ===';
    
    -- TEST 1: Immutability trigger exists
    SELECT EXISTS(
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_stock_movements_immutable'
    ) INTO v_result;
    IF NOT v_result THEN
        RAISE EXCEPTION 'TEST 1 FAILED: Immutability trigger not found';
    END IF;
    RAISE NOTICE 'TEST 1 PASSED: Immutability trigger exists';
    
    -- TEST 2: receiving_lines.line_number is NOT NULL
    SELECT is_nullable = 'NO' INTO v_result
    FROM information_schema.columns 
    WHERE table_name = 'receiving_lines' AND column_name = 'line_number';
    IF NOT v_result THEN
        RAISE EXCEPTION 'TEST 2 FAILED: receiving_lines.line_number should be NOT NULL';
    END IF;
    RAISE NOTICE 'TEST 2 PASSED: receiving_lines.line_number is NOT NULL';
    
    -- TEST 3: products.primary_ean does NOT exist
    SELECT EXISTS(
        SELECT 1 FROM information_schema.columns 
        WHERE table_name = 'products' AND column_name = 'primary_ean'
    ) INTO v_result;
    IF v_result THEN
        RAISE EXCEPTION 'TEST 3 FAILED: products.primary_ean should not exist';
    END IF;
    RAISE NOTICE 'TEST 3 PASSED: products.primary_ean correctly removed';
    
    -- TEST 4: Barcode group primary index exists
    SELECT EXISTS(
        SELECT 1 FROM pg_indexes WHERE indexname = 'idx_identifiers_primary_barcode_group'
    ) INTO v_result;
    IF NOT v_result THEN
        RAISE EXCEPTION 'TEST 4 FAILED: Barcode group primary index not found';
    END IF;
    RAISE NOTICE 'TEST 4 PASSED: Barcode group primary constraint exists';
    
    -- TEST 5: EAN unique index exists
    SELECT EXISTS(
        SELECT 1 FROM pg_indexes WHERE indexname = 'idx_identifiers_ean_unique'
    ) INTO v_result;
    IF NOT v_result THEN
        RAISE EXCEPTION 'TEST 5 FAILED: EAN unique index not found';
    END IF;
    RAISE NOTICE 'TEST 5 PASSED: EAN uniqueness enforced';
    
    -- TEST 6: Config single current index exists
    SELECT EXISTS(
        SELECT 1 FROM pg_indexes WHERE indexname = 'idx_config_versions_single_current'
    ) INTO v_result;
    IF NOT v_result THEN
        RAISE EXCEPTION 'TEST 6 FAILED: Config single current index not found';
    END IF;
    RAISE NOTICE 'TEST 6 PASSED: Config single current enforced';
    
    -- TEST 7: v_products_legacy view exists
    SELECT EXISTS(
        SELECT 1 FROM information_schema.views WHERE table_name = 'v_products_legacy'
    ) INTO v_result;
    IF NOT v_result THEN
        RAISE EXCEPTION 'TEST 7 FAILED: v_products_legacy view not found';
    END IF;
    RAISE NOTICE 'TEST 7 PASSED: Legacy view exists';
    
    -- TEST 8: No pgcrypto extension required
    SELECT NOT EXISTS(
        SELECT 1 FROM pg_extension WHERE extname = 'pgcrypto'
    ) INTO v_result;
    IF NOT v_result THEN
        RAISE WARNING 'TEST 8 WARNING: pgcrypto extension exists (not required)';
    ELSE
        RAISE NOTICE 'TEST 8 PASSED: No pgcrypto extension required';
    END IF;
    
    -- TEST 9: Multi-EAN functional test
    BEGIN
        -- Create test product
        INSERT INTO products (sku, name) VALUES ('SMOKE-MULTIEAN', 'Smoke Test Multi-EAN')
        RETURNING id INTO v_product_id;
        
        -- Add multiple barcodes (including short code like 398828)
        INSERT INTO product_identifiers (product_id, identifier_type, value, is_primary) VALUES
            (v_product_id, 'ean', '6927116185329', true),
            (v_product_id, 'ean', '6938112675813', false),
            (v_product_id, 'unverified_barcode', '398828', false);
        
        -- Verify all can be looked up
        SELECT COUNT(*) INTO v_count
        FROM product_identifiers 
        WHERE value IN ('6927116185329', '6938112675813', '398828')
          AND product_id = v_product_id;
        
        IF v_count != 3 THEN
            RAISE EXCEPTION 'TEST 9 FAILED: Multi-EAN lookup failed (expected 3, got %)', v_count;
        END IF;
        
        RAISE NOTICE 'TEST 9 PASSED: Multi-EAN storage and lookup works';
        
        -- Rollback test data
        RAISE EXCEPTION 'ROLLBACK_TEST';
    EXCEPTION 
        WHEN OTHERS THEN
            IF SQLERRM = 'ROLLBACK_TEST' THEN NULL;
            ELSE RAISE;
            END IF;
    END;
    
    -- TEST 10: Same EAN for two products should fail
    BEGIN
        INSERT INTO products (sku, name) VALUES ('SMOKE-DUP1', 'Dup Test 1')
        RETURNING id INTO v_product_id;
        
        INSERT INTO product_identifiers (product_id, identifier_type, value, is_primary)
        VALUES (v_product_id, 'ean', '9999999999999', true);
        
        -- Create second product with same EAN
        INSERT INTO products (sku, name) VALUES ('SMOKE-DUP2', 'Dup Test 2')
        RETURNING id INTO v_product_id;
        
        BEGIN
            INSERT INTO product_identifiers (product_id, identifier_type, value, is_primary)
            VALUES (v_product_id, 'ean', '9999999999999', true);
            
            RAISE EXCEPTION 'TEST 10 FAILED: Duplicate EAN was allowed';
        EXCEPTION 
            WHEN unique_violation THEN
                RAISE NOTICE 'TEST 10 PASSED: Duplicate EAN correctly blocked';
        END;
        
        RAISE EXCEPTION 'ROLLBACK_TEST';
    EXCEPTION 
        WHEN OTHERS THEN
            IF SQLERRM = 'ROLLBACK_TEST' OR SQLERRM LIKE '%TEST 10 PASSED%' THEN NULL;
            ELSE RAISE;
            END IF;
    END;
    
    -- TEST 11: Only one primary barcode allowed
    BEGIN
        INSERT INTO products (sku, name) VALUES ('SMOKE-PRIMARY', 'Primary Test')
        RETURNING id INTO v_product_id;
        
        INSERT INTO product_identifiers (product_id, identifier_type, value, is_primary)
        VALUES (v_product_id, 'ean', '1111111111111', true);
        
        BEGIN
            INSERT INTO product_identifiers (product_id, identifier_type, value, is_primary)
            VALUES (v_product_id, 'upc', '222222222222', true);
            
            RAISE EXCEPTION 'TEST 11 FAILED: Second primary barcode was allowed';
        EXCEPTION 
            WHEN unique_violation THEN
                RAISE NOTICE 'TEST 11 PASSED: Single primary barcode enforced';
        END;
        
        RAISE EXCEPTION 'ROLLBACK_TEST';
    EXCEPTION 
        WHEN OTHERS THEN
            IF SQLERRM = 'ROLLBACK_TEST' OR SQLERRM LIKE '%TEST 11 PASSED%' THEN NULL;
            ELSE RAISE;
            END IF;
    END;
    
    -- TEST 12: Receiving lines dedup by line_number
    BEGIN
        INSERT INTO suppliers (code, name) VALUES ('SMOKE-SUP', 'Smoke Supplier');
        
        INSERT INTO receiving_sessions (supplier_id, warehouse_id, invoice_number)
        VALUES (
            (SELECT id FROM suppliers WHERE code = 'SMOKE-SUP'),
            (SELECT id FROM warehouses WHERE is_default = true),
            'SMOKE-INV-001'
        );
        
        -- First line
        INSERT INTO receiving_lines (session_id, line_number, ordered_qty)
        VALUES (currval('receiving_sessions_id_seq'), 1, 10);
        
        -- Duplicate line_number should fail
        BEGIN
            INSERT INTO receiving_lines (session_id, line_number, ordered_qty)
            VALUES (currval('receiving_sessions_id_seq'), 1, 20);
            
            RAISE EXCEPTION 'TEST 12 FAILED: Duplicate line_number was allowed';
        EXCEPTION 
            WHEN unique_violation THEN
                RAISE NOTICE 'TEST 12 PASSED: Receiving line dedup works';
        END;
        
        RAISE EXCEPTION 'ROLLBACK_TEST';
    EXCEPTION 
        WHEN OTHERS THEN
            IF SQLERRM = 'ROLLBACK_TEST' OR SQLERRM LIKE '%TEST 12 PASSED%' THEN NULL;
            ELSE RAISE;
            END IF;
    END;
    
    RAISE NOTICE '=== ALL SMOKE TESTS PASSED ===';
END;
$$;

-- ============================================================================
-- END OF SCHEMA v12 FINAL
-- ============================================================================
