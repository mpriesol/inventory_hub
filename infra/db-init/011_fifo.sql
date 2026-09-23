-- Additive FIFO layers. Existing balances remain legacy until explicit cutover.
ALTER TABLE stock_balances ALTER COLUMN avg_cost DROP NOT NULL;
ALTER TABLE stock_balances ALTER COLUMN total_value DROP NOT NULL;
ALTER TABLE stock_movements ALTER COLUMN avg_cost_after DROP NOT NULL;
ALTER TABLE stock_balances ADD COLUMN IF NOT EXISTS qty_quarantined NUMERIC(12,3) NOT NULL DEFAULT 0;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='chk_qty_quarantined_non_negative' AND conrelid='stock_balances'::regclass) THEN
        ALTER TABLE stock_balances ADD CONSTRAINT chk_qty_quarantined_non_negative CHECK(qty_quarantined>=0);
    END IF;
END $$;
CREATE TABLE IF NOT EXISTS fifo_cutovers (
 id VARCHAR(36) PRIMARY KEY, product_id BIGINT NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
 warehouse_id BIGINT NOT NULL REFERENCES warehouses(id) ON DELETE RESTRICT,
 request_hash VARCHAR(64) NOT NULL, preview_hash VARCHAR(64) NOT NULL, preview_data JSONB NOT NULL,
 status VARCHAR(16) NOT NULL DEFAULT 'prepared' CHECK(status IN ('prepared','completed')),
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), expires_at TIMESTAMPTZ NOT NULL,
 completed_at TIMESTAMPTZ, result JSONB
);
CREATE TABLE IF NOT EXISTS fifo_states (
 product_id BIGINT NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
 warehouse_id BIGINT NOT NULL REFERENCES warehouses(id) ON DELETE RESTRICT,
 revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>0), activated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 activation_kind VARCHAR(20) NOT NULL, cutover_id VARCHAR(36) REFERENCES fifo_cutovers(id) ON DELETE RESTRICT,
 PRIMARY KEY(product_id,warehouse_id)
);
CREATE TABLE IF NOT EXISTS fifo_layers (
 id BIGSERIAL PRIMARY KEY, product_id BIGINT NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
 warehouse_id BIGINT NOT NULL REFERENCES warehouses(id) ON DELETE RESTRICT,
 receipt_movement_id BIGINT REFERENCES stock_movements(id) ON DELETE RESTRICT,
 cutover_id VARCHAR(36) REFERENCES fifo_cutovers(id) ON DELETE RESTRICT,
 physical_received_at TIMESTAMPTZ NOT NULL,
 quantity_original NUMERIC(12,3) NOT NULL CHECK(quantity_original>0),
 quantity_remaining NUMERIC(12,3) NOT NULL CHECK(quantity_remaining>=0 AND quantity_remaining<=quantity_original),
 unit_cost NUMERIC(12,4), cost_status VARCHAR(16) NOT NULL CHECK(cost_status IN ('known','provisional','unknown')),
 cost_revision INTEGER NOT NULL DEFAULT 0 CHECK(cost_revision>=0),
 stock_status VARCHAR(16) NOT NULL DEFAULT 'available' CHECK(stock_status IN ('available','quarantine')),
 root_cost_layer_id BIGINT REFERENCES fifo_layers(id) ON DELETE RESTRICT,
 provenance JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 CHECK((cost_status='unknown' AND unit_cost IS NULL) OR (cost_status IN ('known','provisional') AND unit_cost>=0 AND unit_cost IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS ix_fifo_layers_pair_order ON fifo_layers(product_id,warehouse_id,physical_received_at,id);
CREATE INDEX IF NOT EXISTS ix_fifo_layers_root ON fifo_layers(root_cost_layer_id);
CREATE INDEX IF NOT EXISTS ix_fifo_layers_receipt ON fifo_layers(receipt_movement_id);
CREATE TABLE IF NOT EXISTS fifo_allocations (
 id BIGSERIAL PRIMARY KEY, issue_movement_id BIGINT NOT NULL REFERENCES stock_movements(id) ON DELETE RESTRICT,
 layer_id BIGINT NOT NULL REFERENCES fifo_layers(id) ON DELETE RESTRICT, sequence INTEGER NOT NULL CHECK(sequence>=0),
 quantity NUMERIC(12,3) NOT NULL CHECK(quantity>0), quantity_before NUMERIC(12,3) NOT NULL CHECK(quantity_before>=quantity),
 returned_quantity NUMERIC(12,3) NOT NULL DEFAULT 0 CHECK(returned_quantity>=0 AND returned_quantity<=quantity),
 unit_cost_at_issue NUMERIC(12,4), total_cost_at_issue NUMERIC(16,4), cost_status_at_issue VARCHAR(16) NOT NULL,
 unit_cost_current NUMERIC(12,4), total_cost_current NUMERIC(16,4), cost_status_current VARCHAR(16) NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 CONSTRAINT uq_fifo_allocation_sequence UNIQUE(issue_movement_id,sequence)
);
CREATE INDEX IF NOT EXISTS ix_fifo_allocations_layer ON fifo_allocations(layer_id);
CREATE TABLE IF NOT EXISTS fifo_returns (
 id BIGSERIAL PRIMARY KEY, request_id VARCHAR(36) NOT NULL UNIQUE, request_hash VARCHAR(64) NOT NULL,
 issue_movement_id BIGINT NOT NULL REFERENCES stock_movements(id) ON DELETE RESTRICT,
 quantity NUMERIC(12,3) NOT NULL CHECK(quantity>0), case_reference VARCHAR(200) NOT NULL, reason VARCHAR(1000) NOT NULL,
 condition VARCHAR(20) NOT NULL CHECK(condition IN ('good','damaged')), received_at TIMESTAMPTZ NOT NULL,
 created_by VARCHAR(100) NOT NULL, result JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS fifo_return_lines (
 id BIGSERIAL PRIMARY KEY, return_id BIGINT NOT NULL REFERENCES fifo_returns(id) ON DELETE RESTRICT,
 allocation_id BIGINT NOT NULL REFERENCES fifo_allocations(id) ON DELETE RESTRICT,
 layer_id BIGINT NOT NULL REFERENCES fifo_layers(id) ON DELETE RESTRICT,
 movement_id BIGINT NOT NULL REFERENCES stock_movements(id) ON DELETE RESTRICT,
 quantity NUMERIC(12,3) NOT NULL CHECK(quantity>0), unit_cost_at_return NUMERIC(12,4), cost_status_at_return VARCHAR(16) NOT NULL,
 CONSTRAINT uq_fifo_return_allocation UNIQUE(return_id,allocation_id)
);
CREATE TABLE IF NOT EXISTS fifo_releases (
 id BIGSERIAL PRIMARY KEY, request_id VARCHAR(36) NOT NULL UNIQUE, request_hash VARCHAR(64) NOT NULL,
 source_layer_id BIGINT NOT NULL REFERENCES fifo_layers(id) ON DELETE RESTRICT,
 target_warehouse_id BIGINT NOT NULL REFERENCES warehouses(id) ON DELETE RESTRICT,
 quantity NUMERIC(12,3) NOT NULL CHECK(quantity>0), reason VARCHAR(1000) NOT NULL,
 created_by VARCHAR(100) NOT NULL, result JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS fifo_cost_revisions (
 id BIGSERIAL PRIMARY KEY, request_id VARCHAR(36) NOT NULL UNIQUE, request_hash VARCHAR(64) NOT NULL,
 root_layer_id BIGINT NOT NULL REFERENCES fifo_layers(id) ON DELETE RESTRICT, revision INTEGER NOT NULL CHECK(revision>0),
 previous_cost NUMERIC(12,4), previous_status VARCHAR(20) NOT NULL, new_cost NUMERIC(12,4), new_status VARCHAR(20) NOT NULL,
 reason VARCHAR(1000) NOT NULL, document_reference VARCHAR(200) NOT NULL, created_by VARCHAR(100) NOT NULL,
 impact JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), CONSTRAINT uq_fifo_cost_revision UNIQUE(root_layer_id,revision)
);

CREATE TABLE IF NOT EXISTS fifo_receipts (
 id VARCHAR(36) PRIMARY KEY, product_id BIGINT NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
 warehouse_id BIGINT NOT NULL REFERENCES warehouses(id) ON DELETE RESTRICT,
 request_hash VARCHAR(64) NOT NULL, preview_hash VARCHAR(64) NOT NULL, preview_data JSONB NOT NULL,
 status VARCHAR(16) NOT NULL DEFAULT 'prepared' CHECK(status IN ('prepared','completed')),
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), expires_at TIMESTAMPTZ NOT NULL,
 completed_at TIMESTAMPTZ, result JSONB
);

-- PostgreSQL 16 cannot ALTER a generated expression. Recreate only this derived
-- column and its known views/index; RESTRICT rejects unknown external dependents.
DO $$ BEGIN
 IF EXISTS (SELECT 1 FROM pg_attribute a JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
            WHERE a.attrelid='stock_balances'::regclass AND a.attname='qty_available'
              AND position('qty_quarantined' IN pg_get_expr(d.adbin,d.adrelid))=0) THEN
    DROP VIEW IF EXISTS v_product_inventory;
    DROP VIEW IF EXISTS v_stock_alerts;
    ALTER TABLE stock_balances DROP COLUMN qty_available;
    ALTER TABLE stock_balances ADD COLUMN qty_available NUMERIC(12,3)
      GENERATED ALWAYS AS (qty_on_hand-qty_reserved-qty_quarantined) STORED;
 END IF;
END $$;
CREATE INDEX IF NOT EXISTS idx_stock_balances_low_stock ON stock_balances(warehouse_id,qty_available)
 WHERE qty_available<=min_quantity;
CREATE OR REPLACE VIEW v_product_inventory AS
SELECT
    p.id AS product_id, p.sku, p.name, p.brand,
    p.supplier_id, s.code AS supplier_code,
    w.id AS warehouse_id, w.code AS warehouse_code,
    COALESCE(sb.qty_on_hand, 0) AS qty_on_hand,
    COALESCE(sb.qty_reserved, 0) AS qty_reserved,
    COALESCE(sb.qty_available, 0) AS qty_available,
    sb.avg_cost AS avg_cost,
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
