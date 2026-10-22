-- Item definitions belong to the Hub card; invoice quantities retain their
-- original purchase unit. No old quantity, cost or pack_size is reinterpreted.
ALTER TABLE products ADD COLUMN IF NOT EXISTS item_type varchar(20) NOT NULL DEFAULT 'stock';
ALTER TABLE products ADD COLUMN IF NOT EXISTS stock_unit varchar(20) NOT NULL DEFAULT 'pcs';
ALTER TABLE receiving_lines ADD COLUMN IF NOT EXISTS purchase_unit varchar(20);
ALTER TABLE receiving_lines ADD COLUMN IF NOT EXISTS conversion_stock_unit varchar(20);
ALTER TABLE receiving_lines ADD COLUMN IF NOT EXISTS purchase_to_stock_factor numeric(12,3) NOT NULL DEFAULT 1;

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'products_item_type_valid' AND conrelid = 'products'::regclass) THEN
    ALTER TABLE products ADD CONSTRAINT products_item_type_valid CHECK (item_type IN ('stock', 'service'));
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'products_stock_unit_valid' AND conrelid = 'products'::regclass) THEN
    ALTER TABLE products ADD CONSTRAINT products_stock_unit_valid CHECK (stock_unit IN ('pcs', 'pair', 'pack', 'm'));
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'receiving_purchase_unit_valid' AND conrelid = 'receiving_lines'::regclass) THEN
    ALTER TABLE receiving_lines ADD CONSTRAINT receiving_purchase_unit_valid
      CHECK (purchase_unit IS NULL OR purchase_unit IN ('pcs', 'pair', 'pack', 'm'));
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'receiving_purchase_factor_valid' AND conrelid = 'receiving_lines'::regclass) THEN
    ALTER TABLE receiving_lines ADD CONSTRAINT receiving_purchase_factor_valid
      CHECK (purchase_to_stock_factor > 0 AND purchase_to_stock_factor <= 999999.999
             AND (purchase_unit IS NOT NULL OR purchase_to_stock_factor = 1));
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'receiving_conversion_stock_unit_valid' AND conrelid = 'receiving_lines'::regclass) THEN
    ALTER TABLE receiving_lines ADD CONSTRAINT receiving_conversion_stock_unit_valid
      CHECK ((conversion_stock_unit IS NULL AND purchase_unit IS NULL)
          OR (conversion_stock_unit IS NOT NULL AND conversion_stock_unit IN ('pcs', 'pair', 'pack', 'm') AND purchase_unit IS NOT NULL));
  END IF;
END $$;

-- Widen the known opening-unit contract without rewriting any saved preview or
-- ledger row. Existing ks batches keep the same quantities and apply guards.
DO $$ BEGIN
  IF to_regclass('opening_stock_lines') IS NOT NULL THEN
    ALTER TABLE opening_stock_lines ALTER COLUMN unit TYPE varchar(20);
    ALTER TABLE opening_stock_lines DROP CONSTRAINT IF EXISTS chk_opening_quantity;
    ALTER TABLE opening_stock_lines ADD CONSTRAINT chk_opening_quantity
      CHECK (quantity > 0 AND quantity <= 999999999.999 AND (unit = 'm' OR quantity = trunc(quantity)));
    ALTER TABLE opening_stock_lines DROP CONSTRAINT IF EXISTS chk_opening_unit;
    ALTER TABLE opening_stock_lines ADD CONSTRAINT chk_opening_unit CHECK (unit IN ('ks', 'pair', 'pack', 'm'));
  END IF;
END $$;

CREATE OR REPLACE FUNCTION inventory_hub_guard_item_definition() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.item_type IS DISTINCT FROM OLD.item_type OR NEW.stock_unit IS DISTINCT FROM OLD.stock_unit THEN
    IF EXISTS (SELECT 1 FROM stock_movements WHERE product_id = OLD.id)
       OR EXISTS (SELECT 1 FROM stock_balances WHERE product_id = OLD.id AND
                  (qty_on_hand <> 0 OR qty_reserved <> 0 OR qty_quarantined <> 0))
       OR EXISTS (SELECT 1 FROM reservations WHERE product_id = OLD.id) THEN
      RAISE EXCEPTION 'item_definition_locked' USING ERRCODE = '23514';
    END IF;
    IF to_regclass('stock_tracking') IS NOT NULL THEN
      IF EXISTS (SELECT 1 FROM stock_tracking WHERE product_id = OLD.id) THEN
        RAISE EXCEPTION 'item_definition_locked' USING ERRCODE = '23514';
      END IF;
    END IF;
  END IF;
  RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS guard_item_definition ON products;
CREATE TRIGGER guard_item_definition BEFORE UPDATE OF item_type, stock_unit ON products
  FOR EACH ROW EXECUTE FUNCTION inventory_hub_guard_item_definition();

CREATE OR REPLACE FUNCTION inventory_hub_require_stock_item() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE kind varchar(20);
BEGIN
  SELECT item_type INTO kind FROM products WHERE id = NEW.product_id FOR SHARE;
  IF kind = 'service' THEN
    RAISE EXCEPTION 'stock_item_required' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS require_stock_item ON stock_movements;
CREATE TRIGGER require_stock_item BEFORE INSERT ON stock_movements
  FOR EACH ROW EXECUTE FUNCTION inventory_hub_require_stock_item();
DROP TRIGGER IF EXISTS require_stock_item ON stock_balances;
CREATE TRIGGER require_stock_item BEFORE INSERT OR UPDATE ON stock_balances
  FOR EACH ROW EXECUTE FUNCTION inventory_hub_require_stock_item();
DROP TRIGGER IF EXISTS require_stock_item ON reservations;
CREATE TRIGGER require_stock_item BEFORE INSERT OR UPDATE ON reservations
  FOR EACH ROW EXECUTE FUNCTION inventory_hub_require_stock_item();
