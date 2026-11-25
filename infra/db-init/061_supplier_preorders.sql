CREATE TABLE IF NOT EXISTS supplier_preorders (
  id UUID PRIMARY KEY,
  supplier_id BIGINT NOT NULL REFERENCES suppliers(id) ON DELETE RESTRICT,
  reference VARCHAR(100) NOT NULL CHECK(length(btrim(reference)) > 0),
  ordered_on DATE NOT NULL,
  request_hash VARCHAR(64) NOT NULL,
  approved_by TEXT NOT NULL,
  approved_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(supplier_id,reference)
);

CREATE TABLE IF NOT EXISTS supplier_preorder_lines (
  id BIGSERIAL PRIMARY KEY,
  preorder_id UUID NOT NULL REFERENCES supplier_preorders(id) ON DELETE RESTRICT,
  product_id BIGINT NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
  ordered_quantity NUMERIC(12,3) NOT NULL CHECK(ordered_quantity > 0),
  expected_on DATE NOT NULL,
  UNIQUE(preorder_id,product_id)
);

CREATE INDEX IF NOT EXISTS idx_supplier_preorder_product ON supplier_preorder_lines(product_id,preorder_id);

CREATE TABLE IF NOT EXISTS supplier_preorder_invoices (
  invoice_id BIGINT PRIMARY KEY REFERENCES uploaded_invoices(id) ON DELETE RESTRICT,
  preorder_id UUID NOT NULL REFERENCES supplier_preorders(id) ON DELETE RESTRICT,
  linked_by TEXT NOT NULL,
  linked_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_supplier_preorder_invoices ON supplier_preorder_invoices(preorder_id);
