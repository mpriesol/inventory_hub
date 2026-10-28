-- Transactional wakeups only. This migration enables no shop or stock writer.
CREATE TABLE IF NOT EXISTS stock_sync_dirty (
 shop_id BIGINT NOT NULL REFERENCES shops(id) ON DELETE RESTRICT,
 product_id BIGINT NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
 warehouse_id BIGINT NOT NULL REFERENCES warehouses(id) ON DELETE RESTRICT,
 generation BIGINT NOT NULL DEFAULT 1 CHECK (generation > 0),
 acknowledged_generation BIGINT NOT NULL DEFAULT 0,
 dispatched_generation BIGINT,
 run_id VARCHAR(36) REFERENCES stock_sync_runs(id) ON DELETE RESTRICT,
 dispatched_sku VARCHAR(100),
 next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 changed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 last_error VARCHAR(100),
 PRIMARY KEY (shop_id, product_id, warehouse_id),
 CHECK (acknowledged_generation >= 0 AND acknowledged_generation <= generation)
);
CREATE INDEX IF NOT EXISTS ix_stock_sync_dirty_due ON stock_sync_dirty(next_attempt_at, shop_id)
 WHERE generation > acknowledged_generation;

CREATE OR REPLACE FUNCTION stock_sync_mark_dirty(p_product BIGINT, p_warehouse BIGINT)
RETURNS VOID LANGUAGE plpgsql AS $$
BEGIN
 INSERT INTO stock_sync_dirty (shop_id, product_id, warehouse_id)
 SELECT DISTINCT sp.shop_id, p_product, p_warehouse
 FROM shop_products sp
 JOIN stock_sync_settings ss ON ss.shop_id = sp.shop_id AND ss.warehouse_id = p_warehouse
 JOIN shops s ON s.id = sp.shop_id
 JOIN products p ON p.id = sp.product_id
 WHERE sp.product_id = p_product AND sp.is_listed AND p.item_type = 'stock'
   AND s.is_active AND s.platform = 'upgates' AND ss.enabled AND ss.authorized AND ss.protocol = 'delta_v1'
 ORDER BY sp.shop_id
 ON CONFLICT (shop_id, product_id, warehouse_id) DO UPDATE
 SET generation = stock_sync_dirty.generation + 1, changed_at = now(),
     next_attempt_at = LEAST(stock_sync_dirty.next_attempt_at, now());
END $$;

CREATE OR REPLACE FUNCTION stock_sync_balance_dirty() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP = 'UPDATE' AND NEW.product_id = OLD.product_id AND NEW.warehouse_id = OLD.warehouse_id
    AND NEW.qty_available IS NOT DISTINCT FROM OLD.qty_available THEN
   RETURN NEW;
 END IF;
 IF TG_OP <> 'INSERT' THEN
   PERFORM stock_sync_mark_dirty(OLD.product_id, OLD.warehouse_id);
 END IF;
 IF TG_OP <> 'DELETE' AND (TG_OP = 'INSERT' OR NEW.product_id <> OLD.product_id
                           OR NEW.warehouse_id <> OLD.warehouse_id) THEN
   PERFORM stock_sync_mark_dirty(NEW.product_id, NEW.warehouse_id);
 END IF;
 RETURN NULL;
END $$;

CREATE OR REPLACE FUNCTION stock_sync_native_dirty() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE target_warehouse BIGINT;
BEGIN
 IF TG_OP = 'UPDATE' AND NEW.product_id = OLD.product_id AND NEW.order_id = OLD.order_id
    AND NEW.quantity IS NOT DISTINCT FROM OLD.quantity THEN
   RETURN NEW;
 END IF;
 IF TG_OP <> 'INSERT' THEN
   SELECT warehouse_id INTO target_warehouse FROM stock_native_orders WHERE order_id = OLD.order_id;
   IF target_warehouse IS NOT NULL THEN
     PERFORM stock_sync_mark_dirty(OLD.product_id, target_warehouse);
   END IF;
 END IF;
 IF TG_OP <> 'DELETE' THEN
   SELECT warehouse_id INTO target_warehouse FROM stock_native_orders WHERE order_id = NEW.order_id;
   IF target_warehouse IS NOT NULL THEN
     PERFORM stock_sync_mark_dirty(NEW.product_id, target_warehouse);
   END IF;
 END IF;
 RETURN NULL;
END $$;

DO $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'stock_sync_balance_dirty_trigger'
                AND tgrelid = 'stock_balances'::regclass) THEN
   CREATE TRIGGER stock_sync_balance_dirty_trigger AFTER INSERT OR UPDATE OR DELETE ON stock_balances
   FOR EACH ROW EXECUTE FUNCTION stock_sync_balance_dirty();
 END IF;
 IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'stock_sync_native_dirty_trigger'
                AND tgrelid = 'stock_native_lines'::regclass) THEN
   CREATE TRIGGER stock_sync_native_dirty_trigger AFTER INSERT OR UPDATE OR DELETE ON stock_native_lines
   FOR EACH ROW EXECUTE FUNCTION stock_sync_native_dirty();
 END IF;
END $$;
