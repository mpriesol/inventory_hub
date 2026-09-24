-- Persist successful scan operations atomically with their quantity increment.
-- NULL/legacy request IDs are not represented and retain their old behavior.
CREATE TABLE IF NOT EXISTS receiving_scan_requests (
    request_id UUID PRIMARY KEY,
    session_id BIGINT NOT NULL REFERENCES receiving_sessions(id) ON DELETE RESTRICT,
    scan_event_id BIGINT NOT NULL UNIQUE REFERENCES scan_events(id) ON DELETE RESTRICT,
    request_hash VARCHAR(64) NOT NULL,
    response JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Supplier invoice EANs may contain several barcodes separated by delimiters.
-- Widen only the supported legacy width; preserve all stored values and history.
DO $$
DECLARE
    view_oid OID;
    view_definition TEXT;
    view_owner OID;
    view_acl ACLITEM[];
    view_options TEXT[];
    view_comment TEXT;
    column_grants JSONB;
    column_comments JSONB;
    grant_row RECORD;
    item JSONB;
    grantee_sql TEXT;
    target_schema TEXT := current_schema();
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
        WHERE table_schema = target_schema AND table_name = 'receiving_lines'
        AND column_name = 'ean' AND character_maximum_length < 255) THEN
        -- Migration 002 owns this one known dependent view. Preserve its actual
        -- definition and access contract instead of silently recreating defaults.
        SELECT c.oid, pg_get_viewdef(c.oid, true), c.relowner,
               COALESCE(c.relacl, acldefault('r', c.relowner)), c.reloptions,
               obj_description(c.oid, 'pg_class')
        INTO view_oid, view_definition, view_owner, view_acl, view_options, view_comment
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = target_schema AND c.relname = 'v_invoice_lines_detail' AND c.relkind = 'v';

        IF view_oid IS NOT NULL THEN
            SELECT COALESCE(jsonb_agg(jsonb_build_object('column', a.attname,
                'grantee', x.grantee, 'grantor', x.grantor,
                'privilege', x.privilege_type, 'grantable', x.is_grantable)), '[]'::jsonb)
            INTO column_grants
            FROM pg_attribute a CROSS JOIN LATERAL aclexplode(a.attacl) x
            WHERE a.attrelid = view_oid AND a.attnum > 0 AND NOT a.attisdropped;
            SELECT COALESCE(jsonb_agg(jsonb_build_object('column', a.attname,
                'comment', col_description(view_oid, a.attnum))), '[]'::jsonb)
            INTO column_comments FROM pg_attribute a
            WHERE a.attrelid = view_oid AND a.attnum > 0 AND NOT a.attisdropped;

            -- A custom grant chain or extra view behavior needs explicit review;
            -- do not flatten grantors or discard a user's rules/triggers/defaults.
            IF EXISTS (SELECT 1 FROM aclexplode(view_acl) x WHERE x.grantor <> view_owner)
                OR EXISTS (SELECT 1 FROM jsonb_array_elements(column_grants) x
                           WHERE (x->>'grantor')::OID <> view_owner)
                OR EXISTS (SELECT 1 FROM pg_rewrite WHERE ev_class = view_oid AND rulename <> '_RETURN')
                OR EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = view_oid AND NOT tgisinternal)
                OR EXISTS (SELECT 1 FROM pg_attrdef WHERE adrelid = view_oid)
                OR EXISTS (SELECT 1 FROM pg_seclabel WHERE classoid = 'pg_class'::regclass AND objoid = view_oid)
            THEN
                RAISE EXCEPTION 'Migration 013: custom v_invoice_lines_detail metadata requires manual review';
            END IF;
            EXECUTE format('DROP VIEW %I.v_invoice_lines_detail RESTRICT', target_schema);
        END IF;

        -- This generated helper also depends on EAN's type. Rebuild only this
        -- derived value with its original expression in the same transaction.
        -- Unknown dependencies abort and roll back; never use CASCADE.
        ALTER TABLE receiving_lines DROP COLUMN line_fingerprint;
        ALTER TABLE receiving_lines ALTER COLUMN ean TYPE VARCHAR(255);
        ALTER TABLE receiving_lines ADD COLUMN line_fingerprint VARCHAR(32)
            GENERATED ALWAYS AS (
                md5(COALESCE(ean, '') || '|' || COALESCE(supplier_sku, '') || '|' ||
                    ordered_qty::TEXT || '|' || COALESCE(unit_price::TEXT, ''))
            ) STORED;

        IF view_oid IS NOT NULL THEN
            EXECUTE format('CREATE VIEW %I.v_invoice_lines_detail %s AS %s', target_schema,
                CASE WHEN view_options IS NULL THEN '' ELSE 'WITH (' || array_to_string(view_options, ', ') || ')' END,
                view_definition);
            EXECUTE format('ALTER VIEW %I.v_invoice_lines_detail OWNER TO %I',
                target_schema, pg_get_userbyid(view_owner));
            -- A new view may inherit newer default grants. Remove them before
            -- restoring the saved effective ACL, including PUBLIC and grant options.
            FOR grant_row IN
                SELECT DISTINCT x.grantee FROM pg_class c
                CROSS JOIN LATERAL aclexplode(COALESCE(c.relacl, acldefault('r', c.relowner))) x
                WHERE c.oid = to_regclass(format('%I.v_invoice_lines_detail', target_schema))
            LOOP
                grantee_sql := CASE WHEN grant_row.grantee = 0 THEN 'PUBLIC'
                                    ELSE quote_ident(pg_get_userbyid(grant_row.grantee)) END;
                EXECUTE format('REVOKE ALL PRIVILEGES ON TABLE %I.v_invoice_lines_detail FROM %s', target_schema, grantee_sql);
            END LOOP;
            FOR grant_row IN SELECT * FROM aclexplode(view_acl) LOOP
                grantee_sql := CASE WHEN grant_row.grantee = 0 THEN 'PUBLIC'
                                    ELSE quote_ident(pg_get_userbyid(grant_row.grantee)) END;
                EXECUTE format('GRANT %s ON TABLE %I.v_invoice_lines_detail TO %s%s',
                    grant_row.privilege_type, target_schema, grantee_sql,
                    CASE WHEN grant_row.is_grantable THEN ' WITH GRANT OPTION' ELSE '' END);
            END LOOP;
            FOR item IN SELECT * FROM jsonb_array_elements(column_grants) LOOP
                grantee_sql := CASE WHEN (item->>'grantee')::OID = 0 THEN 'PUBLIC'
                                    ELSE quote_ident(pg_get_userbyid((item->>'grantee')::OID)) END;
                EXECUTE format('GRANT %s (%I) ON TABLE %I.v_invoice_lines_detail TO %s%s',
                    item->>'privilege', item->>'column', target_schema, grantee_sql,
                    CASE WHEN (item->>'grantable')::BOOLEAN THEN ' WITH GRANT OPTION' ELSE '' END);
            END LOOP;
            EXECUTE format('COMMENT ON VIEW %I.v_invoice_lines_detail IS %L', target_schema, view_comment);
            FOR item IN SELECT * FROM jsonb_array_elements(column_comments) LOOP
                EXECUTE format('COMMENT ON COLUMN %I.v_invoice_lines_detail.%I IS %L',
                    target_schema, item->>'column', item->>'comment');
            END LOOP;
        END IF;
    END IF;
END $$;
