-- ============================================================================
-- UPLOADED INVOICES TABLE - Separate from receiving_sessions
-- ============================================================================
-- Run after 002_invoice_management.sql
-- psql -v ON_ERROR_STOP=1 -d inventory_hub -f 003_uploaded_invoices.sql
--
-- PURPOSE:
-- Stores uploaded invoices BEFORE they go through receiving process.
-- receiving_sessions is only created when "Príjem" is started.
-- ============================================================================

-- ============================================================================
-- TABLE: uploaded_invoices
-- ============================================================================

CREATE TABLE IF NOT EXISTS uploaded_invoices (
    id BIGSERIAL PRIMARY KEY,
    
    -- Basic info
    supplier_id BIGINT NOT NULL REFERENCES suppliers(id) ON DELETE RESTRICT,
    original_filename VARCHAR(500) NOT NULL,
    stored_filename VARCHAR(500) NOT NULL,
    file_path TEXT NOT NULL,
    file_size_bytes BIGINT,
    file_type VARCHAR(50),  -- pdf, csv, xlsx, etc.
    
    -- Parsed data (may be NULL if not parsed yet)
    invoice_number VARCHAR(100),
    invoice_date DATE,
    due_date DATE,
    currency VARCHAR(3) DEFAULT 'EUR',
    total_amount DECIMAL(14,2),
    total_without_vat DECIMAL(14,2),
    vat_amount DECIMAL(14,2),
    total_with_vat DECIMAL(14,2),
    vat_rate DECIMAL(5,2) DEFAULT 23.00,
    vat_included BOOLEAN DEFAULT true,
    items_count INTEGER DEFAULT 0,
    
    -- Status
    payment_status payment_status NOT NULL DEFAULT 'unpaid',
    paid_at TIMESTAMPTZ,
    paid_amount DECIMAL(14,2),
    is_parsed BOOLEAN NOT NULL DEFAULT false,
    parse_error TEXT,
    
    -- Link to receiving (NULL until "Príjem" is started)
    receiving_session_id BIGINT REFERENCES receiving_sessions(id) ON DELETE SET NULL,
    
    -- Metadata
    notes TEXT,
    uploaded_by VARCHAR(100),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    -- Constraints
    CONSTRAINT uq_uploaded_invoice_file UNIQUE (supplier_id, stored_filename)
);

-- ============================================================================
-- TABLE: uploaded_invoice_lines
-- Parsed line items from invoice (optional, populated by parser)
-- ============================================================================

CREATE TABLE IF NOT EXISTS uploaded_invoice_lines (
    id BIGSERIAL PRIMARY KEY,
    invoice_id BIGINT NOT NULL REFERENCES uploaded_invoices(id) ON DELETE CASCADE,
    line_number INTEGER NOT NULL,
    
    -- Product identification
    ean VARCHAR(50),
    supplier_sku VARCHAR(100),
    product_name TEXT,
    
    -- Pricing
    quantity DECIMAL(12,3) NOT NULL DEFAULT 1,
    unit VARCHAR(20) DEFAULT 'ks',
    unit_price DECIMAL(12,4),
    discount_percent DECIMAL(5,2),
    total_price DECIMAL(14,2),
    vat_rate DECIMAL(5,2),
    unit_price_with_vat DECIMAL(12,4),
    total_price_with_vat DECIMAL(14,2),
    
    -- Product matching
    matched_product_id BIGINT REFERENCES products(id) ON DELETE SET NULL,
    matched_supplier_product_id BIGINT REFERENCES supplier_products(id) ON DELETE SET NULL,
    is_new_product BOOLEAN DEFAULT false,
    
    -- Raw data (for debugging)
    raw_data JSONB,
    
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    CONSTRAINT uq_invoice_line UNIQUE (invoice_id, line_number)
);

-- ============================================================================
-- INDEXES
-- ============================================================================

CREATE INDEX IF NOT EXISTS idx_uploaded_invoices_supplier 
    ON uploaded_invoices(supplier_id);

CREATE INDEX IF NOT EXISTS idx_uploaded_invoices_invoice_number 
    ON uploaded_invoices(invoice_number) 
    WHERE invoice_number IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_uploaded_invoices_invoice_date 
    ON uploaded_invoices(invoice_date DESC) 
    WHERE invoice_date IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_uploaded_invoices_payment_status 
    ON uploaded_invoices(payment_status);

CREATE INDEX IF NOT EXISTS idx_uploaded_invoices_due_date 
    ON uploaded_invoices(due_date) 
    WHERE due_date IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_uploaded_invoices_receiving 
    ON uploaded_invoices(receiving_session_id) 
    WHERE receiving_session_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_uploaded_invoice_lines_invoice 
    ON uploaded_invoice_lines(invoice_id);

CREATE INDEX IF NOT EXISTS idx_uploaded_invoice_lines_ean 
    ON uploaded_invoice_lines(ean) 
    WHERE ean IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_uploaded_invoice_lines_supplier_sku 
    ON uploaded_invoice_lines(supplier_sku) 
    WHERE supplier_sku IS NOT NULL;

-- ============================================================================
-- TRIGGER: Update updated_at
-- ============================================================================

CREATE OR REPLACE FUNCTION fn_uploaded_invoices_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at := NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_uploaded_invoices_updated_at ON uploaded_invoices;

CREATE TRIGGER trg_uploaded_invoices_updated_at
    BEFORE UPDATE ON uploaded_invoices
    FOR EACH ROW
    EXECUTE FUNCTION fn_uploaded_invoices_updated_at();

-- ============================================================================
-- VIEW: v_uploaded_invoices
-- ============================================================================

CREATE OR REPLACE VIEW v_uploaded_invoices AS
SELECT 
    ui.id,
    ui.supplier_id,
    s.code AS supplier_code,
    s.name AS supplier_name,
    ui.original_filename,
    ui.stored_filename,
    ui.file_path,
    ui.file_size_bytes,
    ui.file_type,
    ui.invoice_number,
    ui.invoice_date,
    ui.due_date,
    ui.currency,
    ui.total_amount,
    ui.total_without_vat,
    ui.vat_amount,
    ui.total_with_vat,
    ui.vat_rate,
    ui.vat_included,
    ui.items_count,
    ui.payment_status,
    ui.paid_at,
    ui.paid_amount,
    ui.is_parsed,
    ui.parse_error,
    ui.receiving_session_id,
    ui.notes,
    ui.created_at,
    ui.updated_at,
    -- Computed fields
    CASE 
        WHEN ui.due_date IS NOT NULL AND ui.due_date < CURRENT_DATE AND ui.payment_status != 'paid'
        THEN true 
        ELSE false 
    END AS is_overdue,
    CASE 
        WHEN ui.due_date IS NOT NULL 
        THEN ui.due_date - CURRENT_DATE 
        ELSE NULL 
    END AS days_until_due,
    -- Receiving status
    CASE 
        WHEN ui.receiving_session_id IS NOT NULL THEN rs.status::text
        ELSE 'not_started'
    END AS receiving_status
FROM uploaded_invoices ui
JOIN suppliers s ON ui.supplier_id = s.id
LEFT JOIN receiving_sessions rs ON ui.receiving_session_id = rs.id;

-- ============================================================================
-- COMMENTS
-- ============================================================================

COMMENT ON TABLE uploaded_invoices IS 'Uploaded invoices - separate from receiving workflow';
COMMENT ON TABLE uploaded_invoice_lines IS 'Parsed line items from uploaded invoices';
COMMENT ON COLUMN uploaded_invoices.receiving_session_id IS 'Link to receiving_sessions - NULL until Príjem is started';
COMMENT ON COLUMN uploaded_invoices.is_parsed IS 'True if invoice content was successfully parsed';

-- ============================================================================
-- END OF MIGRATION
-- ============================================================================
