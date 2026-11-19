-- Reusable local dictionaries. No stock, shop authority or legacy quantities change.
CREATE TABLE IF NOT EXISTS availability_values (
 id BIGSERIAL PRIMARY KEY,
 label VARCHAR(100) NOT NULL CHECK (length(btrim(label)) > 0),
 is_active BOOLEAN NOT NULL DEFAULT true,
 sort_order INTEGER NOT NULL DEFAULT 0,
 revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS availability_values_label ON availability_values(lower(btrim(label)));
INSERT INTO availability_values(label, sort_order) VALUES
 ('SKLADOM',0),('do 5 dní',10),('overíme',20),('Na predobjednanie',30) ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS setting_catalog_requests (
 request_id VARCHAR(36) PRIMARY KEY,
 input_hash VARCHAR(64) NOT NULL,
 result JSONB NOT NULL,
 actor VARCHAR(100) NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Only desired labels change. Reuse the opt-in delta publisher wakeup; no
-- quantities, stock confirmations or authority settings are changed here.
CREATE OR REPLACE FUNCTION product_availability_override_dirty() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE target_warehouse BIGINT;
BEGIN
 IF (NEW.data #>> '{availability,label}') IS NOT DISTINCT FROM
   (CASE WHEN TG_OP='UPDATE' THEN OLD.data #>> '{availability,label}' ELSE NULL END) THEN RETURN NEW; END IF;
 FOR target_warehouse IN SELECT DISTINCT warehouse_id FROM stock_sync_settings
   WHERE enabled AND authorized AND protocol='delta_v1' ORDER BY warehouse_id LOOP
   PERFORM stock_sync_mark_dirty(NEW.product_id,target_warehouse);
 END LOOP;
 RETURN NEW;
END $$;
DO $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='product_availability_override_dirty_trigger'
   AND tgrelid='product_editor_overrides'::regclass) THEN
   CREATE TRIGGER product_availability_override_dirty_trigger AFTER INSERT OR UPDATE OF data
   ON product_editor_overrides FOR EACH ROW EXECUTE FUNCTION product_availability_override_dirty();
 END IF;
END $$;
