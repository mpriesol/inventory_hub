"""Product pulls keep shop snapshots separate from physical inventory."""
import os
import unittest
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from inventory_hub.database import get_session
from inventory_hub.db_models import MovementType, Product, ProductGroup, ProductIdentifier, Warehouse, Shop
from inventory_hub.db_models_ext import ShopProduct, ShopProductContent, StockBalance, StockMovement, ProductVariantAttribute
from inventory_hub.routers import upgates_sync
from inventory_hub.services.product_identity import IdentityIndex


META = {"source": "cache", "pulled_at": "2026-09-23T00:00:00", "cache_age_s": 0}
PRODUCTS = [
    {"code": "PULL-SIMPLE", "product_id": 101, "ean": "5901234123457", "stock": 7, "prices": [{"price_with_vat": 123}],
     "descriptions": [{"language": "sk", "title": "Test product"}]},
    {"code": "PULL-GROUP", "product_id": 201, "descriptions": [{"language": "sk", "title": "Test variants"}],
     "variants": [
         {"code": "PULL-M", "variant_id": 301, "ean": "4006381333931", "stock": 9, "prices": [{"price_with_vat": 246}]},
         {"code": "PULL-L", "variant_id": 302, "ean": "9780201379624", "stock": 2, "prices": [{"price_with_vat": 369}]},
     ]},
]


class UpgatesPullEndpointTests(unittest.TestCase):
    def setUp(self):
        self.db = MagicMock(spec=AsyncSession)
        app = FastAPI()
        app.include_router(upgates_sync.router)

        async def session():
            yield self.db

        app.dependency_overrides[get_session] = session
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_legacy_stock_request_is_rejected_before_shop_lookup_network_or_writes(self):
        with patch.object(upgates_sync, "_get_shop", AsyncMock()) as shop, \
                patch.object(upgates_sync, "_fetch_upgates_products") as fetch:
            for include_stock in (True, "true", "false", 1, None, []):
                with self.subTest(include_stock=include_stock):
                    response = self.client.post("/shops/biketrek/upgates/products/import", json={
                        "all": True, "include_stock": include_stock,
                    })
                    self.assertEqual(response.status_code, 400)
                    self.assertIn("include_stock", response.json()["detail"])
            shop.assert_not_awaited()
            fetch.assert_not_called()
            self.db.execute.assert_not_called()
            self.db.add.assert_not_called()
            self.db.flush.assert_not_called()

    def test_omitted_or_false_stock_option_accepts_product_only_requests(self):
        with patch.object(upgates_sync, "_get_shop", AsyncMock(return_value=SimpleNamespace(id=1))), \
                patch.object(upgates_sync, "_fetch_upgates_products", return_value=([], META)) as fetch, \
                patch.object(upgates_sync, "load_identity_index", AsyncMock(return_value=IdentityIndex(1))):
            for payload in ({"all": True}, {"all": True, "include_stock": False}):
                response = self.client.post("/shops/biketrek/upgates/products/import", json=payload)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["stock_initialized"], 0)
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(self.db.execute.await_count, 2, "Only the shared transaction lock is acquired")
            self.db.add.assert_not_called()


TEST_URL = os.environ.get("CATALOG_TEST_DATABASE_URL", "")


@unittest.skipUnless(TEST_URL, "Set CATALOG_TEST_DATABASE_URL to an isolated localhost *_catalog_test DB")
class UpgatesPullDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        parsed = urlsplit(TEST_URL)
        if parsed.hostname not in ("localhost", "127.0.0.1") or not parsed.path.endswith("_catalog_test"):
            raise RuntimeError("Pull tests require a dedicated localhost *_catalog_test database")
        self.schema = "upgates_pull_test_" + uuid4().hex
        connection = await asyncpg.connect(TEST_URL)
        try:
            await connection.execute(f'CREATE SCHEMA "{self.schema}"')
            await connection.execute(f'SET search_path TO "{self.schema}"')
            root = Path(__file__).resolve().parents[2]
            for name in ("001_schema.sql", "004_shop_product_content.sql"):
                await connection.execute((root / "infra" / "db-init" / name).read_text())
        finally:
            await connection.close()
        self.engine = create_async_engine(
            TEST_URL.replace("postgresql://", "postgresql+asyncpg://", 1), poolclass=NullPool,
            connect_args={"server_settings": {"search_path": self.schema}},
        )
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False, autoflush=False)
        self.remote = deepcopy(PRODUCTS)
        self.fetcher = patch.object(upgates_sync, "_fetch_upgates_products", return_value=(self.remote, META))
        self.fetch = self.fetcher.start()

    async def asyncTearDown(self):
        self.fetcher.stop()
        await self.engine.dispose()

    async def pull(self, shop="biketrek", **options):
        async with self.sessions() as db:
            result = await upgates_sync.import_upgates_products(shop, {"all": True, **options}, db)
            await db.commit()
            self.assertEqual(result["stock_initialized"], 0)
            return result

    async def stock_snapshot(self):
        async with self.sessions() as db:
            return {
                table: (await db.execute(text(f"SELECT * FROM {table} ORDER BY id"))).mappings().all()
                for table in ("stock_balances", "stock_movements")
            }

    async def test_repeated_two_shop_pull_keeps_quantities_and_prices_as_snapshots_only(self):
        # Product-only pulls work before a default physical warehouse is configured.
        async with self.sessions() as db:
            await db.execute(update(Warehouse).values(is_default=False))
            await db.commit()
        first = await self.pull()
        self.assertEqual((first["created_products"], first["created_variants"]), (2, 2))
        await self.pull()
        await self.pull(update_existing=True)
        await self.pull("xtrek", update_existing=True, include_stock=False)
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(Product)), 3)
            self.assertEqual(await db.scalar(select(func.count()).select_from(ShopProductContent)), 4)
            mappings = (await db.execute(select(ShopProduct))).scalars().all()
            self.assertEqual(len(mappings), 6)
            for mapping in mappings:
                expected = {
                    "PULL-SIMPLE": (Decimal("7"), Decimal("123")),
                    "PULL-M": (Decimal("9"), Decimal("246")),
                    "PULL-L": (Decimal("2"), Decimal("369")),
                }[mapping.variant_code or mapping.external_code]
                self.assertEqual((mapping.shop_stock, mapping.shop_price), expected)
        self.assertEqual(await self.stock_snapshot(), {"stock_balances": [], "stock_movements": []})

    async def test_pull_preserves_existing_ledger_and_acquisition_values_exactly(self):
        async with self.sessions() as db:
            product = Product(sku="PULL-SIMPLE", name="Already received")
            db.add(product)
            await db.flush()
            warehouse = await db.scalar(select(Warehouse.id))
            movement = StockMovement(
                idempotency_key="fixture-receipt", product_id=product.id, warehouse_id=warehouse,
                movement_type=MovementType.RECEIVING_IN, quantity=Decimal("3"),
                unit_cost=Decimal("8.50"), balance_after=Decimal("3"), avg_cost_after=Decimal("8.50"),
            )
            db.add(movement)
            await db.flush()
            db.add(StockBalance(
                product_id=product.id, warehouse_id=warehouse, qty_on_hand=Decimal("3"),
                qty_reserved=Decimal("1"), avg_cost=Decimal("8.50"), total_value=Decimal("25.50"),
                last_purchase_price=Decimal("8.50"), last_movement_id=movement.id,
            ))
            await db.commit()
        before = await self.stock_snapshot()
        await self.pull(update_existing=True)
        await self.pull("xtrek", update_existing=True)
        self.assertEqual(await self.stock_snapshot(), before)

    async def test_second_shop_alias_links_by_ean_without_overwriting_canonical_content(self):
        self.remote[1]["variants"][0]["parameters"] = [{"name": "Veľkosť", "value": "M"}]
        await self.pull()
        async with self.sessions() as db:
            before_products = (await db.execute(text("SELECT * FROM products ORDER BY id"))).mappings().all()
            before_groups = (await db.execute(text("SELECT * FROM product_groups ORDER BY id"))).mappings().all()
            before_attributes = (await db.execute(text("SELECT * FROM product_variant_attributes ORDER BY id"))).mappings().all()
        for parent in self.remote:
            parent["code"] = "XT-" + parent["code"]
            parent["manufacturer"] = "Different remote brand"
            parent["descriptions"][0]["title"] = "Different remote title"
            for variant in parent.get("variants", []):
                variant["code"] = "XT-" + variant["code"]
                variant["parameters"] = [{"name": "Veľkosť", "value": "NEW"}]
        async with self.sessions() as db:
            preview = await upgates_sync.preview_upgates_products("xtrek", False, db)
        self.assertEqual(preview["already_in_db"], 0)
        self.assertEqual([p["identity_status"] for p in preview["new_products"]], ["identified", "identified"])
        result = await self.pull("xtrek")
        self.assertEqual((result["linked_products"], result["created_products"], result["conflict_count"]), (3, 0, 0))
        await self.pull("xtrek", update_existing=True)
        async with self.sessions() as db:
            self.assertEqual((await db.execute(text("SELECT * FROM products ORDER BY id"))).mappings().all(), before_products)
            self.assertEqual((await db.execute(text("SELECT * FROM product_groups ORDER BY id"))).mappings().all(), before_groups)
            self.assertEqual((await db.execute(text("SELECT * FROM product_variant_attributes ORDER BY id"))).mappings().all(), before_attributes)
            mappings = (await db.execute(select(ShopProduct))).scalars().all()
            self.assertEqual(len(mappings), 6)
            self.assertTrue(all(row.external_id for row in mappings))
        self.assertEqual(await self.stock_snapshot(), {"stock_balances": [], "stock_movements": []})

    async def test_same_sku_without_matching_valid_barcode_is_conflict_not_a_join(self):
        await self.pull()
        for value in (None, "00012345", "012345678905"):
            with self.subTest(ean=value):
                remote = deepcopy(PRODUCTS[0])
                remote["ean"] = value
                self.fetch.return_value = ([remote], META)
                result = await self.pull("xtrek")
                self.assertEqual(result["conflicts"][0]["reasons"], ["unmapped_sku_collision"])
                self.assertEqual(result["content_saved"], 0)
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(Product)), 3)
            self.assertEqual(await db.scalar(select(func.count()).select_from(ShopProduct)), 3)

    async def test_partial_mapped_variant_family_stays_visible_and_adds_missing_sibling(self):
        self.remote[1]["variants"] = self.remote[1]["variants"][:1]
        await self.pull()
        self.remote[1]["variants"].append(deepcopy(PRODUCTS[1]["variants"][1]))
        async with self.sessions() as db:
            preview = await upgates_sync.preview_upgates_products("biketrek", False, db)
        self.assertEqual(preview["already_in_db"], 1)
        self.assertEqual(preview["new_products"][0]["identity_status"], "partial")
        result = await self.pull()
        self.assertEqual((result["created_products"], result["created_variants"], result["conflict_count"]), (1, 1, 0))

    async def test_update_existing_only_reports_changed_mapped_identity(self):
        await self.pull()
        self.remote[0]["ean"] = "012345678905"
        async with self.sessions() as db:
            result = await upgates_sync.import_upgates_products("biketrek", {"update_existing": True}, db)
            await db.commit()
        self.assertEqual(result["conflict_count"], 1)
        self.assertEqual(result["conflicts"][0]["reasons"], ["mapping_identifier_conflict"])
        self.assertEqual(result["content_saved"], 1)

    async def test_duplicate_remote_code_skips_affected_families_and_imports_other_family(self):
        self.remote.append({"code": "PULL-M", "product_id": 444})
        result = await self.pull()
        self.assertEqual((result["conflict_count"], result["created_products"]), (2, 1))
        async with self.sessions() as db:
            self.assertEqual((await db.execute(select(Product.sku))).scalars().all(), ["PULL-SIMPLE"])
            self.assertEqual(await db.scalar(select(func.count()).select_from(ShopProductContent)), 1)

    async def test_late_variant_failure_rolls_back_entire_family_including_content_and_group(self):
        self.remote[1]["variants"][1]["parameters"] = [
            {"name": "Size", "value": "L"}, {"name": "Size", "value": "XL"},
        ]
        result = await self.pull()
        self.assertEqual((result["created_products"], result["content_saved"], result["conflict_count"]), (1, 1, 1))
        self.assertEqual(result["conflicts"][0]["reasons"], ["identity_changed"])
        async with self.sessions() as db:
            self.assertEqual((await db.execute(select(Product.sku))).scalars().all(), ["PULL-SIMPLE"])
            self.assertEqual(await db.scalar(select(func.count()).select_from(ProductGroup)), 0)
            self.assertEqual(await db.scalar(select(func.count()).select_from(ProductIdentifier)), 1)
            self.assertEqual(await db.scalar(select(func.count()).select_from(ShopProductContent)), 1)

    async def test_unrelated_group_with_same_parent_code_is_not_reused(self):
        async with self.sessions() as db:
            db.add(ProductGroup(code="PULL-GROUP", name="Unrelated canonical family"))
            await db.commit()
        result = await self.pull()
        self.assertEqual(result["conflicts"][0]["reasons"], ["group_identity_conflict"])
        async with self.sessions() as db:
            self.assertEqual((await db.execute(select(Product.sku))).scalars().all(), ["PULL-SIMPLE"])
            self.assertEqual(await db.scalar(select(ProductGroup.name)), "Unrelated canonical family")

    async def test_legacy_variant_mapping_parent_code_still_resolves_and_refreshes(self):
        await self.pull()
        async with self.sessions() as db:
            await db.execute(update(ShopProduct).where(ShopProduct.is_variant.is_(True)).values(external_code="PULL-GROUP"))
            await db.commit()
        result = await self.pull(update_existing=True)
        self.assertEqual((result["created_products"], result["conflict_count"], result["updated_products"]), (0, 0, 2))


class LegacyPushIdentityTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def row(value=None, rows=None):
        result = MagicMock()
        result.scalar_one_or_none.return_value = value
        result.scalars.return_value.all.return_value = rows or []
        return result

    async def test_alias_family_is_blocked_before_any_remote_post(self):
        product = SimpleNamespace(id=1, sku="CANONICAL")
        mapping = SimpleNamespace(shop_id=1, parent_code=None, external_code="ALIAS")
        content = SimpleNamespace(data={"code": "ALIAS", "stock": 999})
        db = MagicMock(spec=AsyncSession)
        db.execute.side_effect = [self.row(product), self.row(mapping), self.row(content), self.row(rows=[product])]
        client = MagicMock()
        with patch.object(upgates_sync, "_get_shop", AsyncMock(return_value=SimpleNamespace(id=2))), \
                patch.object(upgates_sync.UpgatesClient, "from_shop", return_value=client):
            result = await upgates_sync.push_products_to_shop("xtrek", {"skus": ["CANONICAL"]}, db)
        self.assertEqual(result["pushed_products"], 0)
        self.assertEqual(result["skipped"][0]["reason"], "identity_alias_push_blocked")
        client.post.assert_not_called()

    async def test_existing_target_variant_mapping_blocks_parent_before_post(self):
        product = SimpleNamespace(id=1, sku="V-M")
        mapping = SimpleNamespace(shop_id=1, parent_code="PARENT", external_code="V-M")
        content = SimpleNamespace(data={"code": "PARENT", "variants": [{"code": "V-M", "stock": 999}]})
        db = MagicMock(spec=AsyncSession)
        db.execute.side_effect = [self.row(product), self.row(mapping), self.row(content), self.row(rows=[product]), self.row(99)]
        client = MagicMock()
        with patch.object(upgates_sync, "_get_shop", AsyncMock(return_value=SimpleNamespace(id=2))), \
                patch.object(upgates_sync.UpgatesClient, "from_shop", return_value=client):
            result = await upgates_sync.push_products_to_shop("xtrek", {"skus": ["V-M"]}, db)
        self.assertEqual(result["pushed_products"], 0)
        client.post.assert_not_called()
        query = str(db.execute.call_args.args[0])
        self.assertIn("shop_products.parent_code", query)
        self.assertIn("shop_products.product_id", query)

    async def test_push_builder_never_keeps_remote_parent_or_variant_quantity_snapshots(self):
        payload = upgates_sync._build_push_payload({"code": "P", "stock": 999, "stocks": [{"stock": 999}],
            "variants": [{"code": "V", "stock": 999, "stock_increment": 4}]}, {"V": 3})
        self.assertNotIn("stock", payload)
        self.assertNotIn("stocks", payload)
        self.assertEqual(payload["variants"][0], {"code": "V", "stock": 3})
