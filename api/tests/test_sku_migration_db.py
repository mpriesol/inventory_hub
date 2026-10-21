"""Read-only product migration reviews against disposable PostgreSQL schemas."""
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
import os
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from inventory_hub.db_models import IdentifierType, Product, ProductGroup, ProductIdentifier, Shop
from inventory_hub.db_models_ext import ShopOrder, ShopOrderItem, ShopProduct, ShopProductContent
from inventory_hub.product_import_models import ProductImportDraft
from inventory_hub.sku_migration_types import SkuMigrationPreviewRequest
from inventory_hub.services import sku_migration as service


TEST_URL = os.environ.get("CATALOG_TEST_DATABASE_URL", "")


@unittest.skipUnless(TEST_URL, "Set CATALOG_TEST_DATABASE_URL to an isolated localhost *_catalog_test DB")
class SkuMigrationDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        parsed = urlsplit(TEST_URL)
        if parsed.hostname not in ("localhost", "127.0.0.1") or not parsed.path.endswith("_catalog_test"):
            raise RuntimeError("SKU migration tests require a dedicated localhost *_catalog_test database")
        self.schema = "sku_migration_test_" + uuid4().hex
        root = Path(__file__).resolve().parents[2] / "infra/db-init"
        connection = await asyncpg.connect(TEST_URL)
        try:
            await connection.execute(f'CREATE SCHEMA "{self.schema}"')
            await connection.execute(f'SET search_path TO "{self.schema}"')
            await connection.execute((root / "001_schema.sql").read_text())
            await connection.execute("CREATE TYPE payment_status AS ENUM ('unpaid', 'partial', 'paid')")
            await connection.execute((root / "002_invoice_management.sql").read_text())
            for path in sorted(root.glob("*.sql")):
                if 4 <= int(path.name[:3]) <= 29:
                    await connection.execute(path.read_text())
        finally:
            await connection.close()
        self.engine = create_async_engine(TEST_URL.replace("postgresql://", "postgresql+asyncpg://", 1),
            poolclass=NullPool, connect_args={"server_settings": {"search_path": self.schema}})
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False, autoflush=False)
        async with self.transaction() as db:
            shop = await db.scalar(select(Shop).where(Shop.code == "biketrek"))
            self.shop_id = shop.id
            self.product = Product(sku="OLD-01", name="Fixture product", brand="Topeak")
            db.add(self.product)
            await db.flush()
            db.add_all([ProductIdentifier(product_id=self.product.id, value="5901234123457", identifier_type=IdentifierType.ean),
                ProductIdentifier(product_id=self.product.id, value="LEGACY-01", identifier_type=IdentifierType.internal_sku),
                ShopProduct(shop_id=self.shop_id, product_id=self.product.id, external_id="101", external_code="OLD-01", is_variant=False),
                ShopProductContent(shop_id=self.shop_id, external_code="OLD-01", data={"code": "OLD-01", "product_id": 101,
                    "variants_exists_yn": False, "set_yn": False, "ean": "5901234123457"})])

    async def asyncTearDown(self):
        await self.engine.dispose()
        connection = await asyncpg.connect(TEST_URL)
        try:
            await connection.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        finally:
            await connection.close()

    @asynccontextmanager
    async def transaction(self):
        async with self.sessions() as db:
            try:
                yield db
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    def request(self, *, remote=False, items=None):
        return SkuMigrationPreviewRequest(items=items or [{"product_id": self.product.id, "sku_identity": {
            "brand": "Topeak", "source_code": "LEAF-01", "code_kind": "manufacturer", "source_ref": "Fixture manufacturer specification",
            "leaf_confirmed": True}}], verify_remote=remote)

    async def preview(self, **kwargs):
        async with self.sessions() as db:
            # PostgreSQL itself rejects any accidental business write, including
            # alias registration, cache refresh and preview persistence.
            await db.execute(text("SET TRANSACTION READ ONLY"))
            return await service.preview(db, self.request(**kwargs))

    def remote(self, *, target=None):
        leaf = {"code": "OLD-01", "is_variant": False, "external_id": "101", "parent_id": "101",
            "parent_code": "OLD-01", "eans": ["5901234123457"], "attributes": []}
        return {"fingerprint": "a" * 64, "requests": 2, "leaves": [leaf], "occupied_codes": [
            {"code": "OLD-01", "kind": "product", "native_id": "101", "parent_id": "101", "parent_code": "OLD-01"},
            *([target] if target else [])]}

    async def test_cached_preview_is_read_only_and_cannot_claim_fresh_verification_or_apply(self):
        with patch.object(service.source, "inspect_codes", AsyncMock()) as remote:
            result = await self.preview()
        remote.assert_not_awaited()
        self.assertFalse(result["can_apply"])
        item = result["items"][0]
        self.assertEqual((item["status"], item["old_sku"], item["proposed_sku"]), ("review", "OLD-01", "TP-LEAF-01"))
        self.assertFalse(item["remote_verified"])
        self.assertEqual(item["mappings"][0]["remote_status"], "not_checked")
        self.assertEqual(item["impacts"]["stock_state"], "unconfirmed")
        self.assertIn("migration_supported_upgates_rename", result["required_checks"])
        self.assertEqual(len(item["identity"]["source_hash"]), 64)
        self.assertEqual(item["aliases"][1]["value"], "LEGACY-01")

    async def test_legacy_alias_occupied_by_another_card_blocks_without_merging(self):
        async with self.transaction() as db:
            other = Product(sku="OTHER-01", name="Another card", brand="Topeak")
            db.add(other)
            await db.flush()
            db.add(ProductIdentifier(product_id=other.id, identifier_type=IdentifierType.internal_sku, value="tp-leaf-01"))
            other_id = other.id
        item = (await self.preview())["items"][0]
        self.assertEqual(item["status"], "blocked")
        self.assertIn("migration_target_owned_by_other_card", item["reasons"])
        self.assertIn(other_id, item["candidate_product_ids"])

    async def test_two_products_proposing_same_target_are_both_blocked(self):
        async with self.transaction() as db:
            other = Product(sku="OTHER-01", name="Another card", brand="Topeak")
            db.add(other)
            await db.flush()
        items = [self.request().items[0].model_dump(), {**self.request().items[0].model_dump(), "product_id": other.id}]
        result = await self.preview(items=items)
        for item in result["items"]:
            self.assertIn("migration_batch_target_collision", item["reasons"])
            self.assertEqual(item["status"], "blocked")

    async def test_pos_and_cached_parent_are_explicitly_blocked_without_remote_reads(self):
        async with self.transaction() as db:
            group = ProductGroup(code="xTrek", name="POS fixture")
            db.add(group)
            await db.flush()
            product = await db.get(Product, self.product.id)
            product.group_id = group.id
            cache = await db.scalar(select(ShopProductContent).where(ShopProductContent.external_code == "OLD-01"))
            cache.data = {**cache.data, "variants_exists_yn": True}
        with patch.object(service.source, "inspect_codes", AsyncMock()) as remote:
            item = (await self.preview(remote=True))["items"][0]
        remote.assert_not_awaited()
        self.assertIn("migration_pos_separate_review", item["reasons"])
        self.assertIn("migration_parent_not_stock_leaf", item["reasons"])

    async def test_fresh_native_identity_is_review_only_and_target_parent_collision_blocks(self):
        with patch.object(service.source, "inspect_codes", AsyncMock(return_value=self.remote())) as read:
            result = await self.preview(remote=True)
        self.assertFalse(result["can_apply"])
        item = result["items"][0]
        self.assertTrue(item["remote_verified"])
        self.assertEqual(item["status"], "review")
        read.assert_awaited_once_with("biketrek", ["OLD-01", "TP-LEAF-01"], max_requests=8)
        occupied = {"code": "TP-LEAF-01", "kind": "parent", "native_id": "999", "parent_id": "999", "parent_code": "TP-LEAF-01"}
        with patch.object(service.source, "inspect_codes", AsyncMock(return_value=self.remote(target=occupied))):
            item = (await self.preview(remote=True))["items"][0]
        self.assertFalse(item["remote_verified"])
        self.assertIn("migration_remote_target_occupied", item["reasons"])

    async def test_open_order_and_draft_counts_do_not_expose_customer_or_document_content(self):
        async with self.transaction() as db:
            order = ShopOrder(shop_id=self.shop_id, external_id="fixture-order", order_date=datetime.now(timezone.utc),
                              customer_id="private-customer", status="new")
            db.add(order)
            await db.flush()
            db.add(ShopOrderItem(order_id=order.id, product_id=self.product.id, external_product_code="OLD-01", quantity=Decimal("1")))
            db.add(ProductImportDraft(id=uuid4().hex, request_hash="a" * 64, revision=1, supplier="manual", shop="biketrek",
                status="saved", document={"private": "do-not-return", "rows": [{"hub_product_id": self.product.id, "values": {"code": "OLD-01"}}]}))
        result = await self.preview()
        item = result["items"][0]
        self.assertEqual(item["impacts"]["counts"]["open_order_lines"], 1)
        self.assertEqual(item["impacts"]["counts"]["matching_import_drafts"], 1)
        self.assertIn("migration_operational_dependencies", item["reasons"])
        self.assertNotIn("private-customer", str(result))
        self.assertNotIn("do-not-return", str(result))
