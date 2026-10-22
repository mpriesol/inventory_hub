"""Real identity SELECTs against an isolated localhost test schema."""
from current_schema_fixture import apply_current_core_schema
import os
from pathlib import Path
import unittest
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from inventory_hub.db_models import IdentifierType, Product, ProductIdentifier
from inventory_hub.services.identifiers import IdentifierConflict, ProductIdentifierService
from inventory_hub.services.product_identity import RemoteIdentity, load_identity_index

TEST_URL = os.environ.get("CATALOG_TEST_DATABASE_URL", "")


@unittest.skipUnless(TEST_URL, "Dedicated localhost *_catalog_test database required")
class IdentityDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        parsed = urlsplit(TEST_URL)
        if parsed.hostname not in ("localhost", "127.0.0.1") or not parsed.path.endswith("_catalog_test"):
            raise RuntimeError("Identity tests require a dedicated localhost *_catalog_test database")
        self.schema = "identity_test_" + uuid4().hex
        connection = await asyncpg.connect(TEST_URL)
        try:
            await connection.execute(f'CREATE SCHEMA "{self.schema}"')
            await connection.execute(f'SET search_path TO "{self.schema}"')
            root = Path(__file__).resolve().parents[2] / "infra" / "db-init"
            await connection.execute((root / "001_schema.sql").read_text())
            await apply_current_core_schema(connection)
        finally:
            await connection.close()
        self.engine = create_async_engine(TEST_URL.replace("postgresql://", "postgresql+asyncpg://", 1),
            poolclass=NullPool, connect_args={"server_settings": {"search_path": self.schema}})
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False, autoflush=False)

    async def asyncTearDown(self):
        await self.engine.dispose()
        connection = await asyncpg.connect(TEST_URL)
        try:
            await connection.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        finally:
            await connection.close()

    async def test_registered_alias_loads_known_barcodes_and_does_not_mutate_card(self):
        async with self.sessions() as db:
            item = Product(sku="TP-TEST-001", name="Synthetic card")
            db.add(item)
            await db.flush()
            db.add_all([
                ProductIdentifier(product_id=item.id, identifier_type=IdentifierType.internal_sku,
                    value="OLD-001", notes="Verified prior Hub SKU; synthetic test"),
                ProductIdentifier(product_id=item.id, identifier_type=IdentifierType.ean, value="5901234123457"),
            ])
            await db.commit()
            incoming = RemoteIdentity(0, "OLD-001", barcodes=("4006381333931",))
            index = await load_identity_index(db, 0, [incoming], local=True)
            self.assertEqual(index.resolve(incoming).reasons, ["identifier_conflict"])
            self.assertEqual(index.resolve(RemoteIdentity(0, "OLD-001")).product_id, item.id)
            self.assertEqual(item.sku, "TP-TEST-001")
            self.assertEqual(await db.scalar(text("SELECT count(*) FROM stock_movements")), 0)
            self.assertEqual(await db.scalar(text("SELECT count(*) FROM shop_products")), 0)

    async def test_upc_ean_equivalence_reuses_card_but_existing_duplicate_blocks_both_lookups(self):
        upc, ean = "727670929720", "0727670929720"
        async with self.sessions() as db:
            first = Product(sku="FIRST", name="Synthetic first")
            db.add(first)
            await db.flush()
            db.add(ProductIdentifier(product_id=first.id, identifier_type=IdentifierType.upc, value=upc))
            await db.commit()
            incoming = RemoteIdentity(0, barcodes=(ean,))
            index = await load_identity_index(db, 0, [incoming], local=True)
            self.assertEqual(index.resolve(incoming).product_id, first.id)
            scanner = ProductIdentifierService(db)
            self.assertEqual((await scanner.find_product_by_barcode(ean)).id, first.id)

            second = Product(sku="SECOND", name="Synthetic existing duplicate")
            db.add(second)
            await db.flush()
            db.add(ProductIdentifier(product_id=second.id, identifier_type=IdentifierType.ean, value=ean))
            await db.commit()
            index = await load_identity_index(db, 0, [incoming], local=True)
            self.assertEqual(index.resolve(incoming).reasons, ["identifier_conflict"])
            self.assertEqual(index.resolve(incoming).candidate_product_ids, [first.id, second.id])
            with self.assertRaises(IdentifierConflict):
                await scanner.find_product_by_barcode(upc)
            self.assertEqual(set((await db.scalars(select(ProductIdentifier.value))).all()), {upc, ean})
            self.assertEqual(await db.scalar(text("SELECT count(*) FROM stock_movements")), 0)
