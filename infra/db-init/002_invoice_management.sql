-- ============================================================================
-- INVOICE MANAGEMENT MODULE - Database Extensions
-- ============================================================================
-- Run after 001_schema.sql
-- psql -v ON_ERROR_STOP=1 -d inventory_hub -f 002_invoice_management.sql
--
-- PURPOSE:
-- Extends receiving_sessions and receiving_lines with:
-- - Payment tracking (paid/unpaid/partial)
-- - VAT handling for reverse charge invoices
-- - Product matching metadata
-- ============================================================================

-- ============================================================================
-- NEW ENUM: Payment Status
-- ============================================================================

DO $$ 
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'payment_status') THEN
        CREATE TYPE payment_status AS ENUM ('unpaid', 'partial', 'paid');
    END IF;
END $$;

-- ============================================================================
-- EXTEND: receiving_sessions
-- ============================================================================

-- Payment tracking
ALTER TABLE receiving_sessions 
    ADD COLUMN IF NOT EXISTS payment_status payment_status NOT NULL DEFAULT 'unpaid';

ALTER TABLE receiving_sessions 
    ADD COLUMN IF NOT EXISTS due_date DATE;

ALTER TABLE receiving_sessions 
    ADD COLUMN IF NOT EXISTS paid_at TIMESTAMPTZ;

ALTER TABLE receiving_sessions 
    ADD COLUMN IF NOT EXISTS paid_amount DECIMAL(14,2);

-- VAT handling
ALTER TABLE receiving_sessions 
    ADD COLUMN IF NOT EXISTS vat_rate DECIMAL(5,2) NOT NULL DEFAULT 23.00;

ALTER TABLE receiving_sessions 
    ADD COLUMN IF NOT EXISTS vat_included BOOLEAN NOT NULL DEFAULT true;

ALTER TABLE receiving_sessions 
    ADD COLUMN IF NOT EXISTS total_without_vat DECIMAL(14,2);

ALTER TABLE receiving_sessions 
    ADD COLUMN IF NOT EXISTS computed_vat DECIMAL(14,2);

ALTER TABLE receiving_sessions 
    ADD COLUMN IF NOT EXISTS total_with_vat DECIMAL(14,2);

-- Supplier info cache (denormalized for faster queries)
ALTER TABLE receiving_sessions 
    ADD COLUMN IF NOT EXISTS supplier_country VARCHAR(2);

-- ============================================================================
-- EXTEND: receiving_lines
-- ============================================================================

-- VAT per line (may differ from session default)
ALTER TABLE receiving_lines 
    ADD COLUMN IF NOT EXISTS vat_rate DECIMAL(5,2);

ALTER TABLE receiving_lines 
    ADD COLUMN IF NOT EXISTS unit_price_with_vat DECIMAL(12,4);

ALTER TABLE receiving_lines 
    ADD COLUMN IF NOT EXISTS total_price_with_vat DECIMAL(14,2);

-- Product matching
ALTER TABLE receiving_lines 
    ADD COLUMN IF NOT EXISTS is_new_product BOOLEAN NOT NULL DEFAULT false;

ALTER TABLE receiving_lines 
    ADD COLUMN IF NOT EXISTS matched_supplier_product_id BIGINT REFERENCES supplier_products(id) ON DELETE SET NULL;

-- Product image caching
ALTER TABLE receiving_lines 
    ADD COLUMN IF NOT EXISTS product_image_url TEXT;

ALTER TABLE receiving_lines 
    ADD COLUMN IF NOT EXISTS product_image_cached_path TEXT;

-- ============================================================================
-- INDEXES for Invoice Management queries
-- ============================================================================

CREATE INDEX IF NOT EXISTS idx_receiving_sessions_payment 
    ON receiving_sessions(payment_status);

CREATE INDEX IF NOT EXISTS idx_receiving_sessions_due_date 
    ON receiving_sessions(due_date) 
    WHERE due_date IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_receiving_sessions_invoice_date 
    ON receiving_sessions(invoice_date DESC) 
    WHERE invoice_date IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_receiving_lines_new_product 
    ON receiving_lines(session_id) 
    WHERE is_new_product = true;

CREATE INDEX IF NOT EXISTS idx_receiving_lines_supplier_product 
    ON receiving_lines(matched_supplier_product_id) 
    WHERE matched_supplier_product_id IS NOT NULL;

-- ============================================================================
-- VIEW: v_invoices_unified
-- Unified view of all invoices across suppliers for the Invoice Management UI
-- ============================================================================

CREATE OR REPLACE VIEW v_invoices_unified AS
SELECT 
    rs.id,
    rs.invoice_number,
    rs.invoice_date,
    rs.due_date,
    rs.invoice_currency AS currency,
    rs.total_without_vat,
    rs.computed_vat AS vat_amount,
    rs.total_with_vat,
    rs.total_amount,  -- original total from import
    rs.vat_rate,
    rs.vat_included,
    rs.payment_status,
    rs.paid_at,
    rs.paid_amount,
    rs.status AS receiving_status,
    rs.total_lines AS items_count,
    rs.supplier_id,
    s.code AS supplier_code,
    s.name AS supplier_name,
    s.default_currency AS supplier_currency,
    rs.warehouse_id,
    w.code AS warehouse_code,
    w.name AS warehouse_name,
    rs.invoice_file_path,
    rs.source_hash,
    rs.import_source,
    rs.created_at,
    rs.updated_at,
    rs.started_at,
    rs.finished_at,
    -- Computed fields
    CASE 
        WHEN rs.due_date IS NOT NULL AND rs.due_date < CURRENT_DATE AND rs.payment_status != 'paid'
        THEN true 
        ELSE false 
    END AS is_overdue,
    CASE 
        WHEN rs.due_date IS NOT NULL 
        THEN rs.due_date - CURRENT_DATE 
        ELSE NULL 
    END AS days_until_due,
    -- Line statistics (subquery for performance)
    (SELECT COUNT(*) FROM receiving_lines rl WHERE rl.session_id = rs.id AND rl.is_new_product = true) AS new_products_count,
    (SELECT COUNT(*) FROM receiving_lines rl WHERE rl.session_id = rs.id AND rl.product_id IS NOT NULL) AS matched_products_count
FROM receiving_sessions rs
JOIN suppliers s ON rs.supplier_id = s.id
JOIN warehouses w ON rs.warehouse_id = w.id;

-- ============================================================================
-- VIEW: v_invoice_lines_detail
-- Detailed view of invoice lines with product matching info
-- ============================================================================

CREATE OR REPLACE VIEW v_invoice_lines_detail AS
SELECT 
    rl.id,
    rl.session_id,
    rl.line_number,
    rl.ean,
    rl.supplier_sku,
    rl.description,
    rl.ordered_qty,
    rl.received_qty,
    rl.unit_price,
    rl.total_price,
    rl.unit_price_with_vat,
    rl.total_price_with_vat,
    rl.vat_rate AS line_vat_rate,
    rl.status AS line_status,
    rl.is_new_product,
    rl.product_image_url,
    -- Linked product info
    rl.product_id,
    p.sku AS product_sku,
    p.name AS product_name,
    p.brand AS product_brand,
    -- Supplier product info (from feed)
    rl.matched_supplier_product_id,
    sp.name AS supplier_product_name,
    sp.images AS supplier_product_images,
    sp.purchase_price AS supplier_feed_price,
    -- Session context
    rs.invoice_number,
    rs.supplier_id,
    s.code AS supplier_code,
    s.name AS supplier_name
FROM receiving_lines rl
JOIN receiving_sessions rs ON rl.session_id = rs.id
JOIN suppliers s ON rs.supplier_id = s.id
LEFT JOIN products p ON rl.product_id = p.id
LEFT JOIN supplier_products sp ON rl.matched_supplier_product_id = sp.id;

-- ============================================================================
-- FUNCTION: Calculate VAT for invoice
-- Handles reverse charge (adds VAT) and normal invoices (extracts VAT)
-- ============================================================================

CREATE OR REPLACE FUNCTION fn_calculate_invoice_vat(
    p_session_id BIGINT
) RETURNS TABLE (
    total_without_vat DECIMAL(14,2),
    computed_vat DECIMAL(14,2),
    total_with_vat DECIMAL(14,2)
) AS $$
DECLARE
    v_vat_rate DECIMAL(5,2);
    v_vat_included BOOLEAN;
    v_total_amount DECIMAL(14,2);
BEGIN
    -- Get session VAT settings
    SELECT vat_rate, vat_included, COALESCE(total_amount, 0)
    INTO v_vat_rate, v_vat_included, v_total_amount
    FROM receiving_sessions
    WHERE id = p_session_id;
    
    IF NOT FOUND THEN
        RETURN;
    END IF;
    
    IF v_vat_included THEN
        -- VAT is already included in prices (normal SK invoice)
        -- Extract VAT from total
        total_with_vat := v_total_amount;
        total_without_vat := ROUND(v_total_amount / (1 + v_vat_rate / 100), 2);
        computed_vat := total_with_vat - total_without_vat;
    ELSE
        -- Reverse charge - VAT needs to be added (CZ/foreign invoices)
        total_without_vat := v_total_amount;
        computed_vat := ROUND(v_total_amount * v_vat_rate / 100, 2);
        total_with_vat := total_without_vat + computed_vat;
    END IF;
    
    RETURN NEXT;
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- TRIGGER: Auto-calculate VAT when invoice is updated
-- ============================================================================

CREATE OR REPLACE FUNCTION fn_receiving_session_vat_trigger()
RETURNS TRIGGER AS $$
DECLARE
    v_calc RECORD;
BEGIN
    -- Only recalculate if relevant fields changed
    IF TG_OP = 'INSERT' OR 
       OLD.total_amount IS DISTINCT FROM NEW.total_amount OR
       OLD.vat_rate IS DISTINCT FROM NEW.vat_rate OR
       OLD.vat_included IS DISTINCT FROM NEW.vat_included THEN
        
        SELECT * INTO v_calc FROM fn_calculate_invoice_vat(NEW.id);
        
        IF FOUND THEN
            NEW.total_without_vat := v_calc.total_without_vat;
            NEW.computed_vat := v_calc.computed_vat;
            NEW.total_with_vat := v_calc.total_with_vat;
        END IF;
    END IF;
    
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Drop existing trigger if exists, then create
DROP TRIGGER IF EXISTS trg_receiving_session_vat ON receiving_sessions;

CREATE TRIGGER trg_receiving_session_vat
    BEFORE INSERT OR UPDATE ON receiving_sessions
    FOR EACH ROW
    EXECUTE FUNCTION fn_receiving_session_vat_trigger();

-- ============================================================================
-- INITIAL DATA UPDATE: Calculate VAT for existing sessions
-- ============================================================================

UPDATE receiving_sessions rs
SET 
    total_without_vat = calc.total_without_vat,
    computed_vat = calc.computed_vat,
    total_with_vat = calc.total_with_vat
FROM (
    SELECT 
        s.id,
        CASE 
            WHEN s.vat_included THEN ROUND(COALESCE(s.total_amount, 0) / (1 + s.vat_rate / 100), 2)
            ELSE COALESCE(s.total_amount, 0)
        END AS total_without_vat,
        CASE 
            WHEN s.vat_included THEN COALESCE(s.total_amount, 0) - ROUND(COALESCE(s.total_amount, 0) / (1 + s.vat_rate / 100), 2)
            ELSE ROUND(COALESCE(s.total_amount, 0) * s.vat_rate / 100, 2)
        END AS computed_vat,
        CASE 
            WHEN s.vat_included THEN COALESCE(s.total_amount, 0)
            ELSE COALESCE(s.total_amount, 0) + ROUND(COALESCE(s.total_amount, 0) * s.vat_rate / 100, 2)
        END AS total_with_vat
    FROM receiving_sessions s
) calc
WHERE rs.id = calc.id
  AND rs.total_with_vat IS NULL;

-- ============================================================================
-- COMMENT: Documentation
-- ============================================================================

COMMENT ON COLUMN receiving_sessions.payment_status IS 'Payment status: unpaid, partial, paid';
COMMENT ON COLUMN receiving_sessions.vat_included IS 'True = VAT included in prices (SK), False = reverse charge (CZ/foreign)';
COMMENT ON COLUMN receiving_sessions.total_without_vat IS 'Net amount (without VAT)';
COMMENT ON COLUMN receiving_sessions.computed_vat IS 'VAT amount (calculated or extracted)';
COMMENT ON COLUMN receiving_sessions.total_with_vat IS 'Gross amount (with VAT)';
COMMENT ON COLUMN receiving_lines.is_new_product IS 'True if product does not exist in products table';
COMMENT ON COLUMN receiving_lines.matched_supplier_product_id IS 'Link to supplier_products (from feed) for price/image lookup';

COMMENT ON VIEW v_invoices_unified IS 'Unified view of all invoices for Invoice Management UI';
COMMENT ON VIEW v_invoice_lines_detail IS 'Detailed invoice lines with product matching info';

-- ============================================================================
-- END OF MIGRATION
-- ============================================================================
