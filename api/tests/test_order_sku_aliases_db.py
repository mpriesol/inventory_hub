"""Reserve/rename/issue/cancel continuity in a disposable PostgreSQL schema."""
import copy
import unittest
from uuid import uuid4
from unittest.mock import patch

import asyncpg
from sqlalchemy import delete, select, text, update

import test_order_stock_db as fixture
import test_order_sku_aliases as evidence
from test_order_stock import raw_line, raw_order
from inventory_hub.db_models import IdentifierType, Product, ProductIdentifier
from inventory_hub.db_models_ext import ShopProduct
from inventory_hub.product_editor_models import ProductEditorSave
from inventory_hub.services import order_sku_aliases, stock_native
from inventory_hub.stock_native_models import StockNativeEvent, StockNativeOrder


@unittest.skipUnless(fixture.TEST_URL, "Set CATALOG_TEST_DATABASE_URL to an isolated localhost *_catalog_test DB")
class OrderSkuAliasDatabaseTests(unittest.IsolatedAsyncioTestCase):
    client = fixture.OrderStockDatabaseTests.client
    put = fixture.OrderStockDatabaseTests.put
    configure = fixture.OrderStockDatabaseTests.configure
    preview = fixture.OrderStockDatabaseTests.preview
    apply = fixture.OrderStockDatabaseTests.apply
    process = fixture.OrderStockDatabaseTests.process
    seed = fixture.OrderStockDatabaseTests.seed
    balance = fixture.OrderStockDatabaseTests.balance
    movements = fixture.OrderStockDatabaseTests.movements
    physical_snapshot = fixture.OrderStockDatabaseTests.physical_snapshot

    async def asyncSetUp(self):
        await fixture.OrderStockDatabaseTests.asyncSetUp(self)
        connection = await asyncpg.connect(fixture.TEST_URL)
        try:
            await connection.execute(f'SET search_path TO "{self.schema}"')
            await connection.execute((self.sql_root / "012_product_editor.sql").read_text())
        finally:
            await connection.close()
        self.fingerprint_patch = patch.object(order_sku_aliases, "fingerprint", return_value=evidence.FINGERPRINT)
        self.fingerprint_patch.start()
        await self.seed("SKU-A", "10")

    async def asyncTearDown(self):
        self.fingerprint_patch.stop()
        await fixture.OrderStockDatabaseTests.asyncTearDown(self)

    async def mapping(self, *, variant=False):
        async with self.sessions() as db:
            row = ShopProduct(shop_id=self.shops["biketrek"], product_id=self.products["SKU-A"],
                external_code="PARENT" if variant else "SKU-A", parent_code="PARENT" if variant else None,
                variant_code="SKU-A" if variant else None, external_id="990" if variant else "100", is_variant=variant)
            db.add(row)
            await db.commit()
            self.mapping_id = row.id

    async def rename(self, *, variant=False, audited=True):
        async with self.sessions() as db:
            await db.execute(update(Product).where(Product.id == self.products["SKU-A"]).values(sku="TP-001"))
            await db.execute(update(ShopProduct).where(ShopProduct.id == self.mapping_id).values(
                **({"variant_code": "TP-001"} if variant else {"external_code": "TP-001"})))
            db.add(ProductIdentifier(product_id=self.products["SKU-A"], value="SKU-A",
                                     identifier_type=IdentifierType.internal_sku, notes="Old canonical SKU"))
            if audited:
                row = evidence.audit(old="SKU-A", variant=variant)
                row.id = None
                row.product_id = self.products["SKU-A"]
                row.request_id = str(uuid4())
                row.after_data["native_identities"][0].update(shop_id=self.shops["biketrek"], mapping_id=self.mapping_id)
                db.add(ProductEditorSave(request_id=row.request_id, input_hash="a" * 64, result={"status": "completed"}))
                await db.flush()
                db.add(row)
            await db.commit()

    async def events(self):
        async with self.sessions() as db:
            return list(await db.scalars(select(StockNativeEvent).order_by(StockNativeEvent.id)))

    async def test_reserved_order_keeps_old_code_then_issues_and_retries_once(self):
        await self.mapping()
        raw = raw_order(raw_line(product_id=100, quantity="2"))
        await self.process(raw)
        first_event = (await self.events())[0]
        immutable_facts = copy.deepcopy(first_event.facts)
        await self.rename()
        # The historical event is usable without rewriting its old mapping.
        async with self.sessions() as db:
            await stock_native._identities(db, self.shops["biketrek"], immutable_facts)
        raw["status_id"] = 8
        preview = await self.preview(raw)
        source = preview["source"]["lines"][0]
        self.assertEqual((source["code"], source["sku"], source["product_id"], source["matched_by"]),
                         ("SKU-A", "TP-001", self.products["SKU-A"], "verified_sku_alias"))
        result = await self.apply(preview)
        self.assertEqual(await self.apply(preview), result)
        self.assertEqual((await self.balance("SKU-A"))[:2], (fixture.D("8"), fixture.D("0")))
        self.assertEqual(len(await self.movements()), 1)
        self.assertEqual((await self.preview(raw))["action"], "noop")
        async with self.sessions() as db:
            self.assertEqual((await db.get(StockNativeEvent, first_event.id)).facts, immutable_facts)
            current = await db.scalar(select(StockNativeOrder))
            self.assertIsNone(current.error)
            self.assertEqual((await db.get(Product, self.products["SKU-A"])).sku, "TP-001")

    async def test_variant_keeps_parent_and_independent_option_set_id(self):
        await self.mapping(variant=True)
        raw = raw_order(raw_line(product_id=100, option_set_id=222, quantity="2"))
        await self.process(raw)
        await self.rename(variant=True)
        raw["status_id"] = 8
        preview = await self.preview(raw)
        proof = preview["source"]["lines"][0]["sku_alias_proof"]
        self.assertEqual((proof["native_id"], proof["parent_native_id"]), ("990", "100"))
        await self.apply(preview)
        async with self.sessions() as db:
            mapping = await db.get(ShopProduct, self.mapping_id)
            self.assertEqual((mapping.external_code, mapping.parent_code, mapping.external_id), ("PARENT", "PARENT", "990"))
            self.assertIsNone((await db.scalar(select(StockNativeOrder))).error)
        self.assertEqual(len(await self.movements()), 1)

    async def test_cancellation_after_rename_releases_reservation_without_receipt(self):
        await self.mapping()
        raw = raw_order(raw_line(product_id=100, quantity="2"))
        await self.process(raw)
        await self.rename()
        raw["status_id"] = 2
        await self.process(raw)
        self.assertEqual((await self.balance("SKU-A"))[:2], (fixture.D("10"), fixture.D("0")))
        self.assertEqual(await self.movements(), [])
        async with self.sessions() as db:
            self.assertIsNone((await db.scalar(select(StockNativeOrder))).error)

    async def test_already_issued_order_after_rename_stays_noop_with_original_history(self):
        await self.mapping()
        raw = raw_order(raw_line(product_id=100, quantity="2"), status_id=8)
        await self.process(raw)
        before = [(row.id, row.product_id, row.quantity, row.reference_id) for row in await self.movements()]
        await self.rename()
        preview = await self.preview(raw)
        self.assertEqual((preview["action"], preview["status"]), ("noop", "completed"))
        self.assertEqual([(row.id, row.product_id, row.quantity, row.reference_id) for row in await self.movements()], before)
        self.assertEqual((await self.balance("SKU-A"))[0], fixture.D("8"))

    async def test_missing_audit_or_cross_shop_alias_never_creates_stock_effect(self):
        await self.mapping()
        await self.rename(audited=False)
        raw = raw_order(raw_line(product_id=100, quantity="2"), status_id=8)
        before = await self.physical_snapshot()
        preview = await self.preview(raw, expect_ready=False)
        self.assertEqual(preview["source"]["lines"][0]["reasons"], ["order_sku_alias_unverified"])
        await self.preview(raw, shop="xtrek", expect_ready=False)
        self.assertEqual(await self.physical_snapshot(), before)

    async def test_alias_changed_after_preview_is_rechecked_before_apply(self):
        await self.mapping()
        await self.rename()
        raw = raw_order(raw_line(product_id=100, quantity="2"), status_id=8)
        preview = await self.preview(raw)
        before = await self.physical_snapshot()
        async with self.sessions() as db:
            await db.execute(delete(ProductIdentifier).where(ProductIdentifier.product_id == self.products["SKU-A"]))
            await db.commit()
        with self.assertRaises(fixture.service.OrderStockError):
            await self.apply(preview)
        self.assertEqual(await self.movements(), [])
        self.assertEqual((await self.balance("SKU-A"))[:2], (fixture.D("10"), fixture.D("0")))
        self.assertEqual((await self.physical_snapshot())["stock_balances"], before["stock_balances"])

    async def test_alias_resolution_is_valid_inside_a_read_only_transaction(self):
        await self.mapping()
        await self.rename()
        before = await self.physical_snapshot()
        source_line = fixture.source._line(raw_line(product_id=100))
        async with self.sessions() as db:
            await db.execute(text("SET TRANSACTION READ ONLY"))
            shop = await db.get(fixture.Shop, self.shops["biketrek"])
            result = await fixture.source.resolve_lines(db, shop, {"lines": [source_line]})
            self.assertEqual(result[0]["matched_by"], "verified_sku_alias")
            await db.commit()
        self.assertEqual(await self.physical_snapshot(), before)
