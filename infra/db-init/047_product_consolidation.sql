-- Audited identity-only retirement. No data is consolidated by this upgrade.
-- Original products, identifiers, balances and immutable ledger rows remain.
CREATE TABLE IF NOT EXISTS product_consolidations (
 id VARCHAR(36) PRIMARY KEY, request_hash VARCHAR(64) NOT NULL,
 preview_hash VARCHAR(64) NOT NULL, document JSONB NOT NULL,
 status VARCHAR(16) NOT NULL DEFAULT 'prepared',
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), expires_at TIMESTAMPTZ NOT NULL,
 completed_at TIMESTAMPTZ, result JSONB,
 CONSTRAINT chk_product_consolidation_status CHECK(status IN ('prepared','completed','expired'))
);
CREATE TABLE IF NOT EXISTS product_canonical_aliases (
 retired_product_id BIGINT PRIMARY KEY REFERENCES products(id) ON DELETE RESTRICT,
 canonical_product_id BIGINT NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
 consolidation_id VARCHAR(36) NOT NULL REFERENCES product_consolidations(id) ON DELETE RESTRICT,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 CONSTRAINT chk_product_canonical_alias_distinct CHECK(retired_product_id<>canonical_product_id)
);
CREATE INDEX IF NOT EXISTS idx_product_canonical_alias_keeper ON product_canonical_aliases(canonical_product_id);

CREATE OR REPLACE FUNCTION hub_consolidation_audit_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='DELETE' OR OLD.status<>'prepared' THEN
  RAISE EXCEPTION 'consolidation_audit_immutable' USING ERRCODE='23514';
 END IF;
 IF (NEW.id,NEW.request_hash,NEW.preview_hash,NEW.document,NEW.created_at) IS DISTINCT FROM
    (OLD.id,OLD.request_hash,OLD.preview_hash,OLD.document,OLD.created_at)
  OR NEW.expires_at>OLD.expires_at
  OR (NEW.status='completed' AND (NEW.completed_at IS NULL OR NEW.result IS NULL))
  OR (NEW.status<>'completed' AND (NEW.completed_at IS NOT NULL OR NEW.result IS NOT NULL)) THEN
  RAISE EXCEPTION 'consolidation_audit_changed' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_consolidation_audit_guard ON product_consolidations;
CREATE TRIGGER trg_consolidation_audit_guard BEFORE UPDATE OR DELETE ON product_consolidations
 FOR EACH ROW EXECUTE FUNCTION hub_consolidation_audit_guard();

ALTER TABLE shop_products ADD COLUMN IF NOT EXISTS retired_at TIMESTAMPTZ;
ALTER TABLE shop_products ADD COLUMN IF NOT EXISTS retired_by_consolidation_id VARCHAR(36)
 REFERENCES product_consolidations(id) ON DELETE RESTRICT;
DO $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_constraint WHERE conrelid='shop_products'::regclass
  AND conname='chk_shop_product_retirement') THEN
  ALTER TABLE shop_products ADD CONSTRAINT chk_shop_product_retirement CHECK(
   (retired_at IS NULL)=(retired_by_consolidation_id IS NULL));
 END IF;
END $$;

-- Replacing only indexes is transactional. No ownership row is deleted.
DO $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_index WHERE indexrelid=to_regclass('uq_shop_products_single')
               AND pg_get_expr(indpred,indrelid) LIKE '%retired_at%') THEN
  DROP INDEX IF EXISTS uq_shop_products_single;
  CREATE UNIQUE INDEX uq_shop_products_single ON shop_products(shop_id,product_id)
   WHERE NOT shared_stock AND retired_at IS NULL;
 END IF;
 IF NOT EXISTS(SELECT 1 FROM pg_index WHERE indexrelid=to_regclass('uq_shop_products_native_product')
               AND pg_get_expr(indpred,indrelid) LIKE '%retired_at%') THEN
  DROP INDEX IF EXISTS uq_shop_products_native_product;
  CREATE UNIQUE INDEX uq_shop_products_native_product ON shop_products(shop_id,external_product_id)
   WHERE NOT is_variant AND external_product_id IS NOT NULL AND retired_at IS NULL;
 END IF;
 IF NOT EXISTS(SELECT 1 FROM pg_index WHERE indexrelid=to_regclass('uq_shop_products_native_variant')
               AND pg_get_expr(indpred,indrelid) LIKE '%retired_at%') THEN
  DROP INDEX IF EXISTS uq_shop_products_native_variant;
  CREATE UNIQUE INDEX uq_shop_products_native_variant ON shop_products(shop_id,external_variant_id)
   WHERE is_variant AND external_variant_id IS NOT NULL AND retired_at IS NULL;
 END IF;
END $$;

CREATE OR REPLACE FUNCTION hub_shared_sale_group_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE selected_shop bigint; selected_product bigint; row_count bigint; all_shared boolean;
BEGIN
 selected_shop := CASE WHEN TG_OP='DELETE' THEN OLD.shop_id ELSE NEW.shop_id END;
 selected_product := CASE WHEN TG_OP='DELETE' THEN OLD.product_id ELSE NEW.product_id END;
 PERFORM pg_advisory_xact_lock(643128702193);
 PERFORM id FROM products WHERE id=selected_product FOR SHARE;
 SELECT count(*),bool_and(shared_stock) INTO row_count,all_shared FROM shop_products
  WHERE shop_id=selected_shop AND product_id=selected_product AND retired_at IS NULL;
 IF row_count>1 AND NOT all_shared THEN
  RAISE EXCEPTION 'shared_sale_group_requires_explicit_all_rows' USING ERRCODE='23514';
 END IF;
 IF EXISTS(SELECT 1 FROM shop_products sp JOIN products p ON p.id=sp.product_id
  WHERE sp.shop_id=selected_shop AND sp.product_id=selected_product AND sp.retired_at IS NULL AND sp.shared_stock
   AND (p.item_type<>'stock' OR p.stock_unit<>'pcs' OR sp.external_product_id IS NULL
    OR sp.external_code IS NULL OR sp.external_code=''
    OR (sp.is_variant AND (sp.external_variant_id IS NULL OR sp.parent_code IS NULL)))) THEN
  RAISE EXCEPTION 'shared_sale_group_requires_verified_piece_stock' USING ERRCODE='23514';
 END IF;
 RETURN NULL;
END $$;
CREATE OR REPLACE FUNCTION hub_shared_card_unit_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF (NEW.item_type<>'stock' OR NEW.stock_unit<>'pcs') AND EXISTS(
  SELECT 1 FROM shop_products WHERE product_id=NEW.id AND shared_stock AND retired_at IS NULL) THEN
  RAISE EXCEPTION 'shared_sale_card_requires_piece_base_unit' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION hub_canonical_alias_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP<>'INSERT' THEN
  RAISE EXCEPTION 'canonical_alias_immutable' USING ERRCODE='23514';
 END IF;
 PERFORM pg_advisory_xact_lock(643128702193);
 PERFORM id FROM products WHERE id IN(NEW.retired_product_id,NEW.canonical_product_id) ORDER BY id FOR UPDATE;
 IF EXISTS(SELECT 1 FROM product_canonical_aliases
   WHERE retired_product_id=NEW.canonical_product_id OR canonical_product_id=NEW.retired_product_id) THEN
  RAISE EXCEPTION 'canonical_alias_one_hop_required' USING ERRCODE='23514';
 END IF;
 IF NOT EXISTS(SELECT 1 FROM products former JOIN products keeper ON keeper.id=NEW.canonical_product_id
  WHERE former.id=NEW.retired_product_id AND NOT former.is_active AND keeper.is_active
   AND former.item_type='stock' AND keeper.item_type='stock' AND former.stock_unit='pcs' AND keeper.stock_unit='pcs') THEN
  RAISE EXCEPTION 'canonical_alias_stock_unit_required' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_canonical_alias_guard ON product_canonical_aliases;
CREATE TRIGGER trg_canonical_alias_guard BEFORE INSERT OR UPDATE OR DELETE ON product_canonical_aliases
 FOR EACH ROW EXECUTE FUNCTION hub_canonical_alias_guard();

CREATE OR REPLACE FUNCTION hub_canonical_alias_complete_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF EXISTS(SELECT 1 FROM shop_products WHERE product_id=NEW.retired_product_id AND retired_at IS NULL)
  OR NOT EXISTS(SELECT 1 FROM product_consolidations c WHERE c.id=NEW.consolidation_id AND c.status='completed'
   AND c.document->'request'->>'mode'='identity_only_preserve_unconfirmed_history'
   AND c.document->'request'->>'same_physical_retail_unit_confirmed'='true'
   AND (c.document->'request'->>'keeper_product_id')::bigint=NEW.canonical_product_id
   AND c.document->'request'->'retired_product_ids' @> jsonb_build_array(NEW.retired_product_id)) THEN
  RAISE EXCEPTION 'canonical_alias_incomplete_review' USING ERRCODE='23514';
 END IF;
 RETURN NULL;
END $$;
DROP TRIGGER IF EXISTS trg_canonical_alias_complete_guard ON product_canonical_aliases;
CREATE CONSTRAINT TRIGGER trg_canonical_alias_complete_guard AFTER INSERT ON product_canonical_aliases
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION hub_canonical_alias_complete_guard();

CREATE OR REPLACE FUNCTION hub_retired_product_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF EXISTS(SELECT 1 FROM product_canonical_aliases WHERE retired_product_id=OLD.id) THEN
  RAISE EXCEPTION 'product_retired' USING ERRCODE='23514';
 END IF;
 IF TG_OP='UPDATE' AND (NEW.item_type,NEW.stock_unit,NEW.sku) IS DISTINCT FROM (OLD.item_type,OLD.stock_unit,OLD.sku)
  AND EXISTS(SELECT 1 FROM product_canonical_aliases WHERE canonical_product_id=OLD.id) THEN
  RAISE EXCEPTION 'canonical_identity_requires_review' USING ERRCODE='23514';
 END IF;
 RETURN CASE WHEN TG_OP='DELETE' THEN OLD ELSE NEW END;
END $$;

-- Raw SQL consumers use the same current-ownership boundary as the ORM.
-- Keep the existing output contract of this legacy diagnostic view.
CREATE OR REPLACE VIEW v_shop_sync_status AS
 SELECT sh.code AS shop_code,sh.name AS shop_name,sh.platform,sh.oversell_mode,
  p.sku,p.name AS product_name,sp.external_id,sp.external_code,
  spa.qty_available AS inventory_available,spa.availability_code,spa.availability_label,
  spa.sync_required,spa.last_synced_at
 FROM shop_products sp JOIN shops sh ON sp.shop_id=sh.id JOIN products p ON sp.product_id=p.id
 LEFT JOIN shop_product_availability spa ON sp.shop_id=spa.shop_id AND sp.product_id=spa.product_id
 WHERE sp.is_listed AND sp.retired_at IS NULL;
DO $$ BEGIN
 IF to_regclass('stock_sync_dirty') IS NOT NULL THEN
  EXECUTE $function$
   CREATE OR REPLACE FUNCTION stock_sync_mark_dirty(p_product BIGINT,p_warehouse BIGINT)
   RETURNS VOID LANGUAGE plpgsql AS $body$
   BEGIN
    INSERT INTO stock_sync_dirty(shop_id,product_id,warehouse_id)
    SELECT DISTINCT sp.shop_id,p_product,p_warehouse FROM shop_products sp
    JOIN stock_sync_settings ss ON ss.shop_id=sp.shop_id AND ss.warehouse_id=p_warehouse
    JOIN shops s ON s.id=sp.shop_id JOIN products p ON p.id=sp.product_id
    WHERE sp.product_id=p_product AND sp.is_listed AND sp.retired_at IS NULL AND p.item_type='stock'
      AND s.is_active AND s.platform='upgates' AND ss.enabled AND ss.authorized AND ss.protocol='delta_v1'
    ORDER BY sp.shop_id
    ON CONFLICT(shop_id,product_id,warehouse_id) DO UPDATE
     SET generation=stock_sync_dirty.generation+1,changed_at=now(),
         next_attempt_at=LEAST(stock_sync_dirty.next_attempt_at,now());
   END $body$;
  $function$;
 END IF;
END $$;
DROP TRIGGER IF EXISTS trg_retired_product_guard ON products;
CREATE TRIGGER trg_retired_product_guard BEFORE UPDATE OR DELETE ON products
 FOR EACH ROW EXECUTE FUNCTION hub_retired_product_guard();

CREATE OR REPLACE FUNCTION hub_retired_mapping_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP<>'INSERT' AND OLD.retired_at IS NOT NULL THEN
  RAISE EXCEPTION 'shop_mapping_retired' USING ERRCODE='23514';
 END IF;
 IF TG_OP='INSERT' AND NEW.retired_at IS NOT NULL THEN
  RAISE EXCEPTION 'shop_mapping_retirement_requires_existing_row' USING ERRCODE='23514';
 END IF;
 IF TG_OP='UPDATE' AND NEW.retired_at IS NOT NULL THEN
  IF (to_jsonb(OLD)-'retired_at'-'retired_by_consolidation_id'-'updated_at') IS DISTINCT FROM
     (to_jsonb(NEW)-'retired_at'-'retired_by_consolidation_id'-'updated_at')
   OR EXISTS(SELECT 1 FROM shop_order_items WHERE sale_mapping_id=OLD.id)
   OR NOT EXISTS(SELECT 1 FROM product_consolidations c WHERE c.id=NEW.retired_by_consolidation_id
      AND c.status='prepared' AND EXISTS(SELECT 1 FROM jsonb_array_elements(c.document->'local'->'mappings') m
                                       WHERE (m->>'id')::bigint=OLD.id)) THEN
   RAISE EXCEPTION 'shop_mapping_retirement_unreviewed' USING ERRCODE='23514';
  END IF;
 END IF;
 RETURN CASE WHEN TG_OP='DELETE' THEN OLD ELSE NEW END;
END $$;
DROP TRIGGER IF EXISTS trg_retired_mapping_guard ON shop_products;
CREATE TRIGGER trg_retired_mapping_guard BEFORE INSERT OR UPDATE OR DELETE ON shop_products
 FOR EACH ROW EXECUTE FUNCTION hub_retired_mapping_guard();

-- Defense in depth for direct/bulk writers and already-loaded ORM instances.
-- The shared stock service also returns a domain conflict after its Product
-- lock. Here every installed typed product dependency rejects retired IDs.
CREATE OR REPLACE FUNCTION hub_retired_reference_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE old_id bigint; new_id bigint; selected_id bigint;
BEGIN
 IF TG_OP<>'INSERT' THEN old_id := (to_jsonb(OLD)->>TG_ARGV[0])::bigint; END IF;
 IF TG_OP<>'DELETE' THEN new_id := (to_jsonb(NEW)->>TG_ARGV[0])::bigint; END IF;
 FOR selected_id IN SELECT DISTINCT v FROM unnest(ARRAY[old_id,new_id]) v WHERE v IS NOT NULL ORDER BY v LOOP
  PERFORM id FROM products WHERE id=selected_id FOR SHARE;
  IF EXISTS(SELECT 1 FROM product_canonical_aliases WHERE retired_product_id=selected_id) THEN
   RAISE EXCEPTION 'product_retired' USING ERRCODE='23514';
  END IF;
 END LOOP;
 RETURN CASE WHEN TG_OP='DELETE' THEN OLD ELSE NEW END;
END $$;
DO $$
DECLARE r record; trigger_name text;
BEGIN
 FOR r IN SELECT DISTINCT c.conrelid::regclass AS relation,a.attname AS column_name
  FROM pg_constraint c JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=c.conkey[1]
  WHERE c.contype='f' AND c.confrelid='products'::regclass AND cardinality(c.conkey)=1
   AND c.conrelid<>'product_canonical_aliases'::regclass LOOP
  trigger_name := 'trg_retired_reference_' || r.column_name;
  EXECUTE format('DROP TRIGGER IF EXISTS %I ON %s',trigger_name,r.relation);
  EXECUTE format('CREATE TRIGGER %I BEFORE INSERT OR UPDATE OR DELETE ON %s FOR EACH ROW EXECUTE FUNCTION hub_retired_reference_guard(%L)',
                 trigger_name,r.relation,r.column_name);
 END LOOP;
END $$;
