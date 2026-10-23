"""Normalize populated legacy mapping rows once, preserving typed ownership."""
import json
import os
from pathlib import Path
import unittest
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg

TEST_URL = os.environ.get('CATALOG_TEST_DATABASE_URL', '')


@unittest.skipUnless(TEST_URL, 'Set CATALOG_TEST_DATABASE_URL to an isolated localhost *_catalog_test DB')
class ShopIdentityMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        parsed = urlsplit(TEST_URL)
        if parsed.hostname not in ('localhost', '127.0.0.1') or not parsed.path.endswith('_catalog_test'):
            raise RuntimeError('Shop identity tests require a dedicated localhost *_catalog_test database')
        self.schema = 'shop_identity_' + uuid4().hex
        self.db = await asyncpg.connect(TEST_URL)
        await self.db.execute(f'CREATE SCHEMA "{self.schema}"')
        await self.db.execute(f'SET search_path TO "{self.schema}"')
        root = Path(__file__).resolve().parents[2] / 'infra/db-init'
        for name in ('001_schema.sql', '004_shop_product_content.sql'):
            await self.db.execute((root / name).read_text())
        self.sql = (root / '030_shop_identity.sql').read_text()
        self.shop = await self.db.fetchval("SELECT id FROM shops WHERE code='biketrek'")
        self.ids = []
        for code in ('CARD-A', 'CARD-B', 'CARD-C'):
            self.ids.append(await self.db.fetchval('INSERT INTO products(sku,name) VALUES($1,$1) RETURNING id', code))

    async def asyncTearDown(self):
        try:
            await self.db.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        finally:
            await self.db.close()

    async def apply(self):
        async with self.db.transaction():
            await self.db.execute(self.sql)

    async def test_standalone_variant_normalization_and_rerun_preserve_new_identity(self):
        await self.db.execute('''INSERT INTO shop_products(shop_id,product_id,external_id,external_code,variant_code,parent_code,is_variant)
            VALUES($1,$2,'11','POS','LEAF','POS',true),($1,$3,'11','SIMPLE',NULL,NULL,false)''',
            self.shop, self.ids[0], self.ids[1])
        await self.db.execute('INSERT INTO shop_product_content(shop_id,external_code,data) VALUES($1,$2,$3::jsonb)',
            self.shop, 'POS', json.dumps({'code': 'POS', 'product_id': 7, 'variants': [{'code': 'LEAF', 'variant_id': 11}]}))
        await self.apply()
        rows = await self.db.fetch('SELECT external_product_id,external_variant_id,external_code,parent_code FROM shop_products ORDER BY product_id')
        self.assertEqual([tuple(row) for row in rows], [('7', '11', 'LEAF', 'POS'), ('11', None, 'SIMPLE', None)])
        # Later explicit verified identity changes cannot be undone by replaying old columns.
        await self.db.execute("UPDATE shop_products SET external_variant_id='12', external_code='CHANGED' WHERE product_id=$1", self.ids[0])
        await self.apply()
        self.assertEqual(await self.db.fetchval('SELECT external_variant_id FROM shop_products WHERE product_id=$1', self.ids[0]), '12')
        with self.assertRaises(asyncpg.UniqueViolationError):
            await self.db.execute('''INSERT INTO shop_products(shop_id,product_id,external_variant_id,external_code,parent_code,is_variant)
                VALUES($1,$2,'12','OTHER','POS',true)''', self.shop, self.ids[2])

    async def test_ambiguous_cached_parent_is_not_guessed(self):
        await self.db.execute('''INSERT INTO shop_products(shop_id,product_id,external_id,external_code,variant_code,parent_code,is_variant)
            VALUES($1,$2,'11','POS','LEAF','POS',true)''', self.shop, self.ids[0])
        await self.db.execute('INSERT INTO shop_product_content(shop_id,external_code,data) VALUES($1,$2,$3::jsonb)',
            self.shop, 'POS', json.dumps({'code': 'POS', 'product_id': 7,
                'variants': [{'code': 'LEAF', 'variant_id': 11}, {'code': 'LEAF', 'variant_id': 11}]}))
        await self.apply()
        row = await self.db.fetchrow('SELECT external_product_id,external_variant_id FROM shop_products')
        self.assertEqual(tuple(row), (None, '11'))

    async def test_duplicate_native_owners_become_unverified_without_deleting_evidence(self):
        await self.db.execute('''INSERT INTO shop_products(shop_id,product_id,external_id,external_code,is_variant)
            VALUES($1,$2,'11','A',false),($1,$3,'11','B',false),($1,$4,'12','C',false)''',
            self.shop, self.ids[0], self.ids[1], self.ids[2])
        await self.apply()
        rows = await self.db.fetch('SELECT external_id,external_product_id FROM shop_products ORDER BY product_id')
        self.assertEqual([tuple(row) for row in rows], [('11', None), ('11', None), ('12', '12')])
        # An explicit later verification establishes only one owner. Re-running
        # migration never restores the other conflicting legacy claim.
        await self.db.execute("UPDATE shop_products SET external_product_id='11' WHERE product_id=$1", self.ids[0])
        await self.apply()
        self.assertIsNone(await self.db.fetchval('SELECT external_product_id FROM shop_products WHERE product_id=$1', self.ids[1]))
        with self.assertRaises(asyncpg.UniqueViolationError):
            await self.db.execute("UPDATE shop_products SET external_product_id='11' WHERE product_id=$1", self.ids[1])
        self.assertEqual(await self.db.fetchval('SELECT count(*) FROM shop_products'), 3)

    async def test_duplicate_variant_owners_lose_both_native_ids_and_keep_every_row(self):
        await self.db.execute('''INSERT INTO shop_products(shop_id,product_id,external_id,external_code,variant_code,parent_code,is_variant)
            VALUES($1,$2,'11','POS','A','POS',true),($1,$3,'11','POS','B','POS',true),
                  ($1,$4,'11','SIMPLE',NULL,NULL,false)''', self.shop, *self.ids)
        await self.db.execute('INSERT INTO shop_product_content(shop_id,external_code,data) VALUES($1,$2,$3::jsonb)',
            self.shop, 'POS', json.dumps({'code': 'POS', 'product_id': 7,
                'variants': [{'code': 'A', 'variant_id': 11}, {'code': 'B', 'variant_id': 11}]}))
        await self.apply()
        rows = await self.db.fetch('SELECT external_id,external_product_id,external_variant_id FROM shop_products ORDER BY product_id')
        self.assertEqual([tuple(row) for row in rows], [('11', None, None), ('11', None, None), ('11', '11', None)])
        await self.db.execute("UPDATE shop_products SET external_product_id='7',external_variant_id='11' WHERE product_id=$1", self.ids[0])
        with self.assertRaises(asyncpg.UniqueViolationError):
            await self.db.execute("UPDATE shop_products SET external_product_id='7',external_variant_id='11' WHERE product_id=$1", self.ids[1])
        await self.apply()
        self.assertIsNone(await self.db.fetchval('SELECT external_variant_id FROM shop_products WHERE product_id=$1', self.ids[1]))
