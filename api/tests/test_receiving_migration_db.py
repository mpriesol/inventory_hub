"""Migration 013 against populated pre-upgrade schema, including migration 002 views."""
import os
import unittest
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg


TEST_URL = os.environ.get("CATALOG_TEST_DATABASE_URL", "")


@unittest.skipUnless(TEST_URL, "Set CATALOG_TEST_DATABASE_URL to an isolated localhost *_catalog_test DB")
class ReceivingMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        parsed = urlsplit(TEST_URL)
        if parsed.hostname not in ("localhost", "127.0.0.1") or not parsed.path.endswith("_catalog_test"):
            raise RuntimeError("Receiving migration tests require a dedicated localhost *_catalog_test database")
        self.schema = "receiving_migration_" + uuid4().hex
        self.db = await asyncpg.connect(TEST_URL)
        await self.db.execute(f'CREATE SCHEMA "{self.schema}"')
        await self.db.execute(f'SET search_path TO "{self.schema}"')
        root = Path(__file__).resolve().parents[2] / "infra" / "db-init"
        await self.db.execute((root / "001_schema.sql").read_text())
        await self.db.execute("CREATE TYPE payment_status AS ENUM ('unpaid', 'partial', 'paid')")
        await self.db.execute((root / "002_invoice_management.sql").read_text())
        self.migration = (root / "013_receiving_scan_requests.sql").read_text()
        supplier_id = await self.db.fetchval("INSERT INTO suppliers(code, name) VALUES ('migration', 'Migration') RETURNING id")
        warehouse_id = await self.db.fetchval("INSERT INTO warehouses(code, name) VALUES ('migration', 'Migration') RETURNING id")
        session_id = await self.db.fetchval("INSERT INTO receiving_sessions(supplier_id, warehouse_id, invoice_number) "
            "VALUES ($1, $2, 'MIGRATION-001') RETURNING id", supplier_id, warehouse_id)
        self.line_id = await self.db.fetchval("INSERT INTO receiving_lines(session_id, line_number, supplier_sku, "
            "ean, description, ordered_qty, received_qty, unit_price) "
            "VALUES ($1, 1, 'REAL-CODE', '5901234123457', 'Existing physical receipt', 2, 1, 45.50) RETURNING id", session_id)

    async def asyncTearDown(self):
        try:
            # Only this test's random schema in the guarded disposable database.
            await self.db.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        finally:
            await self.db.close()

    async def apply(self):
        # Mirrors the explicit transaction used by receiving_scan_migrate.py.
        async with self.db.transaction():
            await self.db.execute(self.migration)

    async def line_snapshot(self):
        return dict(await self.db.fetchrow("SELECT * FROM receiving_lines WHERE id=$1", self.line_id))

    async def view_snapshot(self):
        metadata = await self.db.fetchrow("SELECT pg_get_viewdef(c.oid, true) AS definition, c.relowner AS owner, "
            "c.reloptions AS options, obj_description(c.oid, 'pg_class') AS comment "
            "FROM pg_class c WHERE c.oid='v_invoice_lines_detail'::regclass")
        grants = await self.db.fetch("SELECT x.* FROM pg_class c "
            "CROSS JOIN LATERAL aclexplode(COALESCE(c.relacl, acldefault('r', c.relowner))) x "
            "WHERE c.oid='v_invoice_lines_detail'::regclass ORDER BY grantor, grantee, privilege_type, is_grantable")
        column_grants = await self.db.fetch("SELECT a.attname, x.* FROM pg_attribute a "
            "CROSS JOIN LATERAL aclexplode(a.attacl) x WHERE a.attrelid='v_invoice_lines_detail'::regclass "
            "ORDER BY a.attname, grantor, grantee, privilege_type, is_grantable")
        column_comments = await self.db.fetch("SELECT attname, col_description(attrelid, attnum) AS comment "
            "FROM pg_attribute WHERE attrelid='v_invoice_lines_detail'::regclass AND attnum > 0 "
            "AND NOT attisdropped ORDER BY attname")
        return dict(metadata), [dict(row) for row in grants], [dict(row) for row in column_grants], [dict(row) for row in column_comments]

    async def test_populated_upgrade_preserves_row_fingerprint_view_metadata_and_permissions(self):
        await self.db.execute("GRANT SELECT ON v_invoice_lines_detail TO PUBLIC")
        await self.db.execute("GRANT UPDATE (ean) ON v_invoice_lines_detail TO CURRENT_USER WITH GRANT OPTION")
        await self.db.execute("GRANT SELECT (supplier_sku) ON v_invoice_lines_detail TO PUBLIC")
        await self.db.execute("COMMENT ON VIEW v_invoice_lines_detail IS 'Operator''s invoice detail'")
        await self.db.execute("COMMENT ON COLUMN v_invoice_lines_detail.ean IS 'Original EAN source'")
        await self.db.execute("ALTER VIEW v_invoice_lines_detail SET (security_barrier=true)")
        before_line, before_view = await self.line_snapshot(), await self.view_snapshot()
        # New default grants must not leak onto the restored existing view.
        await self.db.execute(f'ALTER DEFAULT PRIVILEGES IN SCHEMA "{self.schema}" GRANT DELETE ON TABLES TO PUBLIC')
        self.assertEqual(await self.db.fetchval("SELECT ean FROM v_invoice_lines_detail WHERE id=$1", self.line_id), "5901234123457")
        await self.apply()
        self.assertEqual(await self.line_snapshot(), before_line)
        self.assertEqual(await self.view_snapshot(), before_view)
        self.assertEqual(await self.db.fetchval("SELECT ean FROM v_invoice_lines_detail WHERE id=$1", self.line_id), "5901234123457")
        await self.apply()
        self.assertEqual(await self.line_snapshot(), before_line)
        self.assertEqual(await self.view_snapshot(), before_view)
        # The restored view must expose the full widened source, not a 20-char cast.
        compound = "/".join(["5901234123457", "4006381333931"] * 5)
        await self.db.execute("UPDATE receiving_lines SET ean=$1 WHERE id=$2", compound, self.line_id)
        self.assertEqual(await self.db.fetchval("SELECT ean FROM v_invoice_lines_detail WHERE id=$1", self.line_id), compound)

    async def test_unknown_direct_dependency_aborts_and_restores_original_schema_and_known_view(self):
        await self.db.execute("CREATE VIEW extra_receiving_ean_dependency AS SELECT id, ean FROM receiving_lines")
        before_line, before_view = await self.line_snapshot(), await self.view_snapshot()
        with self.assertRaises(asyncpg.FeatureNotSupportedError):
            await self.apply()
        self.assertEqual(await self.line_snapshot(), before_line)
        self.assertEqual(await self.view_snapshot(), before_view)
        self.assertEqual(await self.db.fetchval("SELECT ean FROM extra_receiving_ean_dependency WHERE id=$1", self.line_id), "5901234123457")
        self.assertEqual(await self.db.fetchval("SELECT ean FROM v_invoice_lines_detail WHERE id=$1", self.line_id), "5901234123457")
        self.assertEqual(await self.db.fetchval("SELECT character_maximum_length FROM information_schema.columns "
            "WHERE table_schema=current_schema() AND table_name='receiving_lines' AND column_name='ean'"), 20)
        self.assertIsNone(await self.db.fetchval("SELECT to_regclass('receiving_scan_requests')"))


    async def test_unknown_view_above_known_view_blocks_drop_without_removing_either(self):
        await self.db.execute("CREATE VIEW extra_invoice_detail_dependency AS SELECT id, ean FROM v_invoice_lines_detail")
        before_line, before_view = await self.line_snapshot(), await self.view_snapshot()
        with self.assertRaises(asyncpg.DependentObjectsStillExistError):
            await self.apply()
        self.assertEqual(await self.line_snapshot(), before_line)
        self.assertEqual(await self.view_snapshot(), before_view)
        self.assertEqual(await self.db.fetchval("SELECT ean FROM extra_invoice_detail_dependency WHERE id=$1", self.line_id), "5901234123457")
        self.assertIsNone(await self.db.fetchval("SELECT to_regclass('receiving_scan_requests')"))
