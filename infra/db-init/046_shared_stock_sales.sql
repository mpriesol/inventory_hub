-- Explicit sale representations of one physical stock card. Existing balances,
-- movements, FIFO allocations and native-event facts are never rewritten.
ALTER TABLE shop_products ADD COLUMN IF NOT EXISTS shared_stock BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE shop_products ADD COLUMN IF NOT EXISTS sale_to_stock_factor NUMERIC(12,3) NOT NULL DEFAULT 1;
ALTER TABLE shop_products ADD COLUMN IF NOT EXISTS sale_barcodes JSONB;
ALTER TABLE shop_order_items ADD COLUMN IF NOT EXISTS sale_quantity NUMERIC(12,3);
ALTER TABLE shop_order_items ADD COLUMN IF NOT EXISTS sale_to_stock_factor NUMERIC(12,3) NOT NULL DEFAULT 1;
ALTER TABLE shop_order_items ADD COLUMN IF NOT EXISTS sale_mapping_id BIGINT;

DO $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='shop_products'::regclass AND conname='chk_shop_product_sale_factor') THEN
  ALTER TABLE shop_products ADD CONSTRAINT chk_shop_product_sale_factor CHECK (
   sale_to_stock_factor >= 1 AND sale_to_stock_factor <= 1000000
   AND sale_to_stock_factor=trunc(sale_to_stock_factor) AND (shared_stock OR sale_to_stock_factor=1)
   AND (NOT shared_stock OR (sale_barcodes IS NOT NULL AND jsonb_typeof(sale_barcodes)='array')));
 END IF;
 IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='shop_order_items'::regclass AND conname='chk_order_item_sale_snapshot') THEN
  ALTER TABLE shop_order_items ADD CONSTRAINT chk_order_item_sale_snapshot CHECK (
   sale_to_stock_factor >= 1 AND sale_to_stock_factor <= 1000000
   AND sale_to_stock_factor=trunc(sale_to_stock_factor)
   AND (sale_quantity IS NULL OR (sale_quantity>0 AND quantity=sale_quantity*sale_to_stock_factor))
   AND (sale_mapping_id IS NULL OR (sale_mapping_id>0 AND sale_quantity IS NOT NULL)));
 END IF;
END $$;

-- Ordinary writers retain their one-card-per-shop uniqueness guard. A
-- deferred cross-row guard below forbids mixing ordinary and shared rows.
CREATE UNIQUE INDEX IF NOT EXISTS uq_shop_products_single ON shop_products(shop_id,product_id) WHERE NOT shared_stock;
ALTER TABLE shop_products DROP CONSTRAINT IF EXISTS uq_shop_products;

CREATE OR REPLACE FUNCTION hub_shared_sale_group_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE selected_shop bigint; selected_product bigint; row_count bigint; all_shared boolean;
BEGIN
 selected_shop := CASE WHEN TG_OP='DELETE' THEN OLD.shop_id ELSE NEW.shop_id END;
 selected_product := CASE WHEN TG_OP='DELETE' THEN OLD.product_id ELSE NEW.product_id END;
 PERFORM pg_advisory_xact_lock(643128702193);
 -- Serialize direct group writes against an edit of the card's physical unit.
 PERFORM id FROM products WHERE id=selected_product FOR SHARE;
 SELECT count(*),bool_and(shared_stock) INTO row_count,all_shared FROM shop_products
  WHERE shop_id=selected_shop AND product_id=selected_product;
 IF row_count>1 AND NOT all_shared THEN
  RAISE EXCEPTION 'shared_sale_group_requires_explicit_all_rows' USING ERRCODE='23514';
 END IF;
 IF EXISTS (SELECT 1 FROM shop_products sp JOIN products p ON p.id=sp.product_id
  WHERE sp.shop_id=selected_shop AND sp.product_id=selected_product AND sp.shared_stock
   AND (p.item_type<>'stock' OR p.stock_unit<>'pcs' OR sp.external_product_id IS NULL
    OR sp.external_code IS NULL OR sp.external_code=''
    OR (sp.is_variant AND (sp.external_variant_id IS NULL OR sp.parent_code IS NULL)))) THEN
  RAISE EXCEPTION 'shared_sale_group_requires_verified_piece_stock' USING ERRCODE='23514';
 END IF;
 RETURN NULL;
END $$;
DROP TRIGGER IF EXISTS trg_shared_sale_group_guard ON shop_products;
CREATE CONSTRAINT TRIGGER trg_shared_sale_group_guard AFTER INSERT OR UPDATE OR DELETE ON shop_products
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION hub_shared_sale_group_guard();

CREATE OR REPLACE FUNCTION hub_shared_sale_history_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF (TG_OP='DELETE' AND OLD.shared_stock) OR (TG_OP='UPDATE' AND
   (OLD.shared_stock OR NEW.shared_stock) AND
   (OLD.shared_stock,OLD.sale_to_stock_factor,OLD.sale_barcodes,OLD.shop_id,OLD.product_id,
    OLD.external_product_id,OLD.external_variant_id,OLD.external_code,OLD.is_variant,OLD.parent_code)
   IS DISTINCT FROM
   (NEW.shared_stock,NEW.sale_to_stock_factor,NEW.sale_barcodes,NEW.shop_id,NEW.product_id,
    NEW.external_product_id,NEW.external_variant_id,NEW.external_code,NEW.is_variant,NEW.parent_code)) THEN
  IF EXISTS (SELECT 1 FROM shop_order_items i JOIN shop_orders o ON o.id=i.order_id
   WHERE i.stock_managed AND o.shop_id=OLD.shop_id AND i.product_id=OLD.product_id
    AND (i.sale_mapping_id=OLD.id OR i.sale_mapping_id IS NULL)) THEN
   RAISE EXCEPTION 'shared_sale_mapping_has_order_history' USING ERRCODE='23514';
  END IF;
 END IF;
 RETURN CASE WHEN TG_OP='DELETE' THEN OLD ELSE NEW END;
END $$;
DROP TRIGGER IF EXISTS trg_shared_sale_history_guard ON shop_products;
CREATE TRIGGER trg_shared_sale_history_guard BEFORE UPDATE OR DELETE ON shop_products
 FOR EACH ROW EXECUTE FUNCTION hub_shared_sale_history_guard();

CREATE OR REPLACE FUNCTION hub_shared_card_unit_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF (NEW.item_type<>'stock' OR NEW.stock_unit<>'pcs') AND EXISTS(
  SELECT 1 FROM shop_products WHERE product_id=NEW.id AND shared_stock) THEN
  RAISE EXCEPTION 'shared_sale_card_requires_piece_base_unit' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_shared_card_unit_guard ON products;
CREATE TRIGGER trg_shared_card_unit_guard BEFORE UPDATE OF item_type,stock_unit ON products
 FOR EACH ROW EXECUTE FUNCTION hub_shared_card_unit_guard();

CREATE TABLE IF NOT EXISTS shared_stock_sale_previews (
 id VARCHAR(36) PRIMARY KEY,
 shop_id BIGINT NOT NULL REFERENCES shops(id) ON DELETE RESTRICT,
 product_id BIGINT NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
 request_hash VARCHAR(64) NOT NULL,
 preview_hash VARCHAR(64) NOT NULL,
 document JSONB NOT NULL,
 status VARCHAR(16) NOT NULL DEFAULT 'prepared',
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 expires_at TIMESTAMPTZ NOT NULL,
 completed_at TIMESTAMPTZ,
 result JSONB,
 CONSTRAINT chk_shared_sale_preview_status CHECK(status IN ('prepared','completed','expired'))
);

-- Feature test schemas may omit the publication tables. Production has these
-- from015/025/026; the runtime upgrade runs after those existing migrations.
DO $$ BEGIN
 IF to_regclass('stock_sync_checkpoints') IS NOT NULL THEN
  ALTER TABLE stock_sync_checkpoints ADD COLUMN IF NOT EXISTS sale_key VARCHAR(32) NOT NULL DEFAULT '';
  ALTER TABLE stock_sync_checkpoints ADD COLUMN IF NOT EXISTS sale_to_stock_factor NUMERIC(12,3) NOT NULL DEFAULT 1;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='stock_sync_checkpoints'::regclass
   AND contype='p' AND pg_get_constraintdef(oid) LIKE '%sale_key%') THEN
   ALTER TABLE stock_sync_checkpoints DROP CONSTRAINT stock_sync_checkpoints_pkey;
   ALTER TABLE stock_sync_checkpoints ADD CONSTRAINT stock_sync_checkpoints_pkey PRIMARY KEY(shop_id,sku,sale_key);
  END IF;
 END IF;
 IF to_regclass('stock_native_lines') IS NOT NULL THEN
  ALTER TABLE stock_native_lines ADD COLUMN IF NOT EXISTS sale_key VARCHAR(32) NOT NULL DEFAULT '';
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='stock_native_lines'::regclass
   AND contype='p' AND pg_get_constraintdef(oid) LIKE '%sale_key%') THEN
   ALTER TABLE stock_native_lines DROP CONSTRAINT stock_native_lines_pkey;
   ALTER TABLE stock_native_lines ADD CONSTRAINT stock_native_lines_pkey PRIMARY KEY(order_id,product_id,sale_key);
  END IF;
 END IF;
 IF to_regclass('stock_sync_items') IS NOT NULL THEN
  ALTER TABLE stock_sync_items ADD COLUMN IF NOT EXISTS sale_key VARCHAR(32) NOT NULL DEFAULT '';
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='stock_sync_items'::regclass
   AND conname='stock_sync_items_run_id_sku_key' AND pg_get_constraintdef(oid) LIKE '%sale_key%') THEN
   ALTER TABLE stock_sync_items DROP CONSTRAINT stock_sync_items_run_id_sku_key;
   ALTER TABLE stock_sync_items ADD CONSTRAINT stock_sync_items_run_id_sku_key UNIQUE(run_id,sku,sale_key);
  END IF;
  DROP INDEX IF EXISTS uq_stock_sync_target_fence;
  CREATE UNIQUE INDEX uq_stock_sync_target_fence ON stock_sync_items(shop_id,sku,sale_key) WHERE status IN ('sending','uncertain');
 END IF;
 IF to_regclass('stock_publication_items') IS NOT NULL THEN
  ALTER TABLE stock_publication_items ADD COLUMN IF NOT EXISTS sale_key VARCHAR(32) NOT NULL DEFAULT '';
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='stock_publication_items'::regclass
   AND conname='uq_stock_publication_item_sku' AND pg_get_constraintdef(oid) LIKE '%sale_key%') THEN
   ALTER TABLE stock_publication_items DROP CONSTRAINT uq_stock_publication_item_sku;
   ALTER TABLE stock_publication_items ADD CONSTRAINT uq_stock_publication_item_sku UNIQUE(batch_id,sku,sale_key);
  END IF;
 END IF;
END $$;
