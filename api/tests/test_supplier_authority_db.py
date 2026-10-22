"""One-time relationship conversion preserves ownership and does not revive links."""
import os
from pathlib import Path
import unittest
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg


TEST_URL = os.environ.get('CATALOG_TEST_DATABASE_URL', '')


@unittest.skipUnless(TEST_URL, 'Set CATALOG_TEST_DATABASE_URL to an isolated localhost *_catalog_test DB')
class SupplierAuthorityMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        parsed = urlsplit(TEST_URL)
        if parsed.hostname not in ('localhost', '127.0.0.1') or not parsed.path.endswith('_catalog_test'):
            raise RuntimeError('Supplier authority tests require a dedicated localhost *_catalog_test database')
        self.db = await asyncpg.connect(TEST_URL)
        self.schema = 'supplier_authority_' + uuid4().hex
        await self.db.execute(f'CREATE SCHEMA "{self.schema}"')
        await self.db.execute(f'SET search_path TO "{self.schema}"')
        root = Path(__file__).resolve().parents[2] / 'infra' / 'db-init'
        await self.db.execute((root / '001_schema.sql').read_text())
        self.sql = (root / '032_supplier_authority.sql').read_text()
        self.supplier = await self.db.fetchval("INSERT INTO suppliers(code,name) VALUES('test','Supplier') RETURNING id")

    async def asyncTearDown(self):
        await self.db.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        await self.db.close()

    async def source(self, code):
        return await self.db.fetchval('INSERT INTO supplier_products(supplier_id,supplier_sku,name) VALUES($1,$2,$2) RETURNING id',
                                      self.supplier, code)

    async def product(self, sku, source=None):
        return await self.db.fetchval('INSERT INTO products(sku,name,source_supplier_product_id) VALUES($1,$1,$2) RETURNING id', sku, source)

    async def alias(self, product, code):
        await self.db.execute("INSERT INTO product_identifiers(product_id,identifier_type,value,supplier_id) VALUES($1,'supplier_sku',$2,$3)",
                              product, code, self.supplier)

    async def test_unambiguous_provenance_and_scoped_alias_convert_once_without_stock(self):
        first_source, second_source = await self.source('001'), await self.source('002')
        first, second = await self.product('RETAIN-1', first_source), await self.product('RETAIN-2')
        await self.alias(second, '002')
        await self.db.execute(self.sql)
        links = await self.db.fetch('SELECT product_id,supplier_product_id FROM product_supply_sources ORDER BY product_id')
        self.assertEqual([tuple(row) for row in links], [(first, first_source), (second, second_source)])
        self.assertEqual(await self.db.fetchval("SELECT value FROM product_identifiers WHERE product_id=$1", first), '001')
        self.assertEqual(await self.db.fetchval('SELECT count(*) FROM stock_movements'), 0)
        self.assertEqual(await self.db.fetchval('SELECT count(*) FROM stock_balances'), 0)
        # A later deliberate unlink is not undone by the deployment runner.
        await self.db.execute('DELETE FROM product_supply_sources WHERE product_id=$1', first)
        await self.db.execute(self.sql)
        self.assertEqual(await self.db.fetchval('SELECT count(*) FROM product_supply_sources WHERE product_id=$1', first), 0)
        self.assertEqual(await self.db.fetchval('SELECT source_supplier_product_id FROM products WHERE id=$1', first), first_source)

    async def test_ambiguous_provenance_is_retained_but_never_becomes_current_ownership(self):
        source = await self.source('SHARED')
        await self.product('ONE', source)
        await self.product('TWO', source)
        await self.db.execute(self.sql)
        self.assertEqual(await self.db.fetchval('SELECT count(*) FROM product_supply_sources'), 0)
        self.assertEqual(await self.db.fetchval('SELECT count(*) FROM products WHERE source_supplier_product_id=$1', source), 2)

    async def test_opposing_alias_and_provenance_remain_unlinked(self):
        source = await self.source('SHARED')
        await self.product('PROVENANCE', source)
        other = await self.product('ALIAS')
        await self.alias(other, 'SHARED')
        await self.db.execute(self.sql)
        self.assertEqual(await self.db.fetchval('SELECT count(*) FROM product_supply_sources'), 0)
        self.assertEqual(await self.db.fetchval('SELECT product_id FROM product_identifiers'), other)

    async def test_current_disabled_policy_is_preserved_and_second_owner_rejected(self):
        source = await self.source('CURRENT')
        first, second = await self.product('ONE', source), await self.product('TWO')
        await self.db.execute('INSERT INTO product_supply_sources(product_id,supplier_product_id,is_active,orderable,priority) VALUES($1,$2,false,false,42)', first, source)
        await self.db.execute(self.sql)
        row = await self.db.fetchrow('SELECT is_active,orderable,priority FROM product_supply_sources')
        self.assertEqual(tuple(row), (False, False, 42))
        with self.assertRaises(asyncpg.UniqueViolationError):
            await self.db.execute('INSERT INTO product_supply_sources(product_id,supplier_product_id) VALUES($1,$2)', second, source)

    async def test_existing_opposing_current_owners_fail_without_partial_conversion(self):
        source = await self.source('CONFLICT')
        first, second = await self.product('ONE'), await self.product('TWO')
        for product in (first, second):
            await self.db.execute('INSERT INTO product_supply_sources(product_id,supplier_product_id) VALUES($1,$2)', product, source)
        with self.assertRaisesRegex(asyncpg.RaiseError, 'supplier_source_multiple_products'):
            await self.db.execute(self.sql)
        self.assertEqual(await self.db.fetchval('SELECT count(*) FROM product_supply_sources'), 2)
        self.assertIsNone(await self.db.fetchval("SELECT to_regclass('schema_migrations')"))

    async def test_opposing_current_alias_fails_before_owner_can_be_overwritten(self):
        source = await self.source('CONFLICT')
        first, second = await self.product('ONE'), await self.product('TWO')
        await self.db.execute('INSERT INTO product_supply_sources(product_id,supplier_product_id) VALUES($1,$2)', first, source)
        await self.alias(second, 'CONFLICT')
        with self.assertRaisesRegex(asyncpg.RaiseError, 'supplier_source_alias_conflict'):
            await self.db.execute(self.sql)
        self.assertEqual(await self.db.fetchval('SELECT product_id FROM product_identifiers'), second)
