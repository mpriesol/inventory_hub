"""Product pulls keep shop snapshots separate from physical inventory."""
import os
import json
import unittest
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from inventory_hub.database import get_session
from inventory_hub import config_io
from inventory_hub.db_models import IdentifierType, MovementType, Product, ProductGroup, ProductIdentifier, Warehouse, Shop, Supplier, SupplierProduct
from inventory_hub.db_models_ext import ShopProduct, ShopProductContent, StockBalance, StockMovement, ProductVariantAttribute, ProductSupplySource
from inventory_hub.routers import stock, upgates_sync
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
            for name in ("001_schema.sql", "004_shop_product_content.sql", "007_order_stock.sql", "011_fifo.sql", "012_product_editor.sql", "014_supplier_availability.sql"):
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

    async def test_shop_pull_attaches_exact_supplier_source_without_creating_stock(self):
        with TemporaryDirectory() as folder, patch.object(config_io, "DATA_ROOT", Path(folder)):
            config_path = config_io.supplier_path("paul-lange")
            config_path.parent.mkdir(parents=True)
            config_path.write_text(json.dumps({"product_code_prefix": "PL-"}))
            async with self.sessions() as db:
                supplier = Supplier(code="paul-lange", name="Paul Lange")
                db.add(supplier)
                await db.flush()
                source = SupplierProduct(supplier_id=supplier.id, supplier_sku="001234", name="Supplier item",
                    ean="5901234123457")
                db.add(source)
                await db.commit()
                source_id = source.id
            self.remote[:] = [{**PRODUCTS[0], "code": "PL-001234"}]
            before = await self.stock_snapshot()
            result = await self.pull()
            self.assertEqual(result["supplier_links"]["linked"], 1)
            self.assertEqual(await self.stock_snapshot(), before)
            async with self.sessions() as db:
                product = await db.scalar(select(Product).where(Product.sku == "PL-001234"))
                link = await db.scalar(select(ProductSupplySource).where(ProductSupplySource.product_id == product.id))
                self.assertEqual(link.supplier_product_id, source_id)
            second = await self.pull(update_existing=True)
            self.assertEqual(second["supplier_links"]["linked"], 0)

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

    async def test_shared_sku_links_without_ean_but_conflicting_verified_ean_blocks(self):
        await self.pull()
        remote = deepcopy(PRODUCTS[0])
        remote["ean"] = "012345678905"
        self.fetch.return_value = ([remote], META)
        result = await self.pull("xtrek")
        self.assertEqual(result["conflicts"][0]["reasons"], ["identifier_conflict"])
        self.assertEqual(result["content_saved"], 0)
        for value in (None, "00012345"):
            with self.subTest(ean=value):
                remote["ean"] = value
                result = await self.pull("xtrek", update_existing=True)
                self.assertEqual(result["conflict_count"], 0)
                self.assertEqual(result["content_saved"], 1)
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(Product)), 3)
            self.assertEqual(await db.scalar(select(func.count()).select_from(ShopProduct)), 4)

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

    async def test_late_variant_failure_rolls_back_entire_family_including_identifiers_and_content(self):
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
        self.assertEqual(result["conflict_count"], 0)
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(Product)), 3)
            self.assertEqual(await db.scalar(select(func.count()).select_from(Product).where(Product.group_id.is_not(None))), 0)
            self.assertEqual(await db.scalar(select(ProductGroup.name)), "Unrelated canonical family")

    async def test_pos_umbrella_links_different_existing_groups_and_new_leaf_without_regrouping(self):
        async with self.sessions() as db:
            groups = [ProductGroup(code="SHOES", name="Shoes"), ProductGroup(code="LIGHTS", name="Lights")]
            db.add_all(groups)
            await db.flush()
            products = [Product(sku="SHOE-42", name="Original shoe", group_id=groups[0].id),
                        Product(sku="LIGHT-BLACK", name="Original light", group_id=groups[1].id)]
            db.add_all(products)
            await db.flush()
            db.add(ProductVariantAttribute(product_id=products[0].id, attribute_name="Size", attribute_value="42"))
            await db.commit()
            originals = {p.sku: (p.id, p.name, p.group_id) for p in products}
            before_groups = (await db.execute(text("SELECT * FROM product_groups ORDER BY id"))).mappings().all()
            before_attrs = (await db.execute(text("SELECT * FROM product_variant_attributes ORDER BY id"))).mappings().all()
        self.fetch.return_value = ([{"code": "POS-ALL", "product_id": 100,
                                    "variants": [{"code": "SHOE-42", "variant_id": 101},
                                                 {"code": "LIGHT-BLACK", "variant_id": 102},
                                                 {"code": "NEW-PART", "variant_id": 103}]}], META)
        result = await self.pull()
        self.assertEqual((result["linked_products"], result["created_variants"], result["conflict_count"]), (2, 1, 0))
        await self.pull(update_existing=True)
        async with self.sessions() as db:
            products = (await db.scalars(select(Product))).all()
            for p in products:
                if p.sku in originals:
                    self.assertEqual((p.id, p.name, p.group_id), originals[p.sku])
                else:
                    self.assertIsNone(p.group_id)
            self.assertEqual((await db.execute(text("SELECT * FROM product_groups ORDER BY id"))).mappings().all(), before_groups)
            self.assertEqual((await db.execute(text("SELECT * FROM product_variant_attributes ORDER BY id"))).mappings().all(), before_attrs)
            self.assertEqual(set((await db.scalars(select(ShopProduct.parent_code))).all()), {"POS-ALL"})
        self.assertEqual(await self.stock_snapshot(), {"stock_balances": [], "stock_movements": []})

    async def test_split_families_first_then_pos_umbrella_share_leaves_and_preserve_parent_namespaces(self):
        self.fetch.return_value = ([{"code": "SAME-PARENT", "product_id": 1,
                                    "variants": [{"code": "SHOE-42", "variant_id": 11}]},
                                   {"code": "LIGHTS", "product_id": 2,
                                    "variants": [{"code": "LIGHT-BLACK", "variant_id": 12}]}], META)
        await self.pull("xtrek")
        async with self.sessions() as db:
            original_ids = dict((await db.execute(select(Product.sku, Product.id))).all())
        self.fetch.return_value = ([{"code": "SAME-PARENT", "product_id": 7,
                                    "variants": [{"code": "SHOE-42", "variant_id": 71},
                                                 {"code": "LIGHT-BLACK", "variant_id": 72}]}], META)
        result = await self.pull("biketrek")
        self.assertEqual((result["linked_products"], result["created_products"], result["conflict_count"]), (2, 0, 0))
        async with self.sessions() as db:
            self.assertEqual(dict((await db.execute(select(Product.sku, Product.id))).all()), original_ids)
            rows = (await db.execute(select(Shop.code, Product.sku, ShopProduct.parent_code)
                                    .join(ShopProduct, Shop.id == ShopProduct.shop_id)
                                    .join(Product, Product.id == ShopProduct.product_id))).all()
            self.assertEqual(set(rows), {("xtrek", "SHOE-42", "SAME-PARENT"), ("xtrek", "LIGHT-BLACK", "LIGHTS"),
                                         ("biketrek", "SHOE-42", "SAME-PARENT"), ("biketrek", "LIGHT-BLACK", "SAME-PARENT")})
            self.assertEqual(await db.scalar(select(func.count()).select_from(ProductGroup)), 0)
        self.assertEqual(await self.stock_snapshot(), {"stock_balances": [], "stock_movements": []})

    async def test_same_parent_text_in_two_shops_keeps_images_in_its_own_shop(self):
        expected = {}
        for position, shop in enumerate(("biketrek", "xtrek")):
            sku, image = f"{shop}-LEAF", f"https://example.invalid/{shop}.jpg"
            self.fetch.return_value = ([{"code": "SAME-PARENT", "product_id": 1,
                                        "images": [{"url": image, "main_yn": True}],
                                        "variants": [{"code": sku, "variant_id": position + 2}]}], META)
            result = await self.pull(shop)
            self.assertEqual(result["conflict_count"], 0)
            expected[sku] = image
        async with self.sessions() as db:
            images = await stock._image_urls_by_product(db)
            ids = dict((await db.execute(select(Product.sku, Product.id))).all())
            self.assertEqual(images, {ids[sku]: image for sku, image in expected.items()})
            for sku, image in expected.items():
                detail = await stock.product_detail(sku, db)
                self.assertEqual(detail["image_url"], image)
                self.assertEqual(detail["shops"][0]["parent_code"], "SAME-PARENT")

    async def test_missing_verified_barcodes_are_appended_without_replacing_existing_primary(self):
        async with self.sessions() as db:
            product = Product(sku="PULL-SIMPLE", name="Existing without verified barcode")
            db.add(product)
            await db.flush()
            db.add(ProductIdentifier(product_id=product.id, value="12345", identifier_type=IdentifierType.unverified_barcode,
                                     is_primary=True))
            await db.commit()
        self.fetch.return_value = ([deepcopy(PRODUCTS[0])], META)
        first = await self.pull()
        self.assertEqual((first["linked_products"], first["created_products"], first["conflict_count"]), (1, 0, 0))
        # A confirmed existing barcode plus an additional UPC supplies consistent
        # evidence. The old primary and both verified values survive repetitions.
        self.fetch.return_value[0][0]["ean"] = "5901234123457/012345678905"
        await self.pull(update_existing=True)
        await self.pull("xtrek")
        async with self.sessions() as db:
            rows = (await db.execute(select(ProductIdentifier.value, ProductIdentifier.identifier_type,
                                            ProductIdentifier.is_primary))).all()
            self.assertEqual(set(rows), {("12345", IdentifierType.unverified_barcode, True),
                                         ("5901234123457", IdentifierType.ean, False),
                                         ("012345678905", IdentifierType.upc, False)})

    async def test_new_barcode_for_existing_leaf_rolls_back_with_failing_family(self):
        async with self.sessions() as db:
            db.add(Product(sku="PULL-M", name="Existing without barcode"))
            await db.commit()
        remote = deepcopy(PRODUCTS[1])
        remote["variants"][1]["parameters"] = [{"name": "Size", "value": "L"}, {"name": "Size", "value": "XL"}]
        self.fetch.return_value = ([remote], META)
        result = await self.pull()
        self.assertEqual(result["conflict_count"], 1)
        async with self.sessions() as db:
            self.assertEqual((await db.scalars(select(Product.sku))).all(), ["PULL-M"])
            for model in (ProductIdentifier, ShopProduct, ShopProductContent):
                self.assertEqual(await db.scalar(select(func.count()).select_from(model)), 0)

    async def test_2500_variant_pos_first_then_split_shop_uses_batches_and_preserves_stock(self):
        def barcode(position):
            prefix = str(400000000000 + position)
            total = sum(int(digit) * (1 if i % 2 == 0 else 3) for i, digit in enumerate(prefix))
            return prefix + str((-total) % 10)

        leaves = [{"code": f"SHARED-{position:04}", "variant_id": position + 10000,
                   "ean": barcode(position) if position % 2 == 0 else None,
                   "parameters": [{"name": "Item", "value": str(position)}], "stock": position}
                  for position in range(2500)]
        umbrella = [{"code": "POS-ALL", "product_id": 1, "variants": leaves}]
        split = [{"code": f"XT-PARENT-{start // 100}", "product_id": start // 100 + 1,
                  "variants": leaves[start:start + 100]} for start in range(0, len(leaves), 100)]
        statements = []

        def count_sql(_connection, _cursor, statement, _parameters, _context, _many):
            statements.append(statement.split(None, 1)[0])

        event.listen(self.engine.sync_engine, "before_cursor_execute", count_sql)
        try:
            self.fetch.return_value = (umbrella, META)
            first = await self.pull()
            self.assertEqual((first["created_variants"], first["conflict_count"]), (2500, 0))
            self.assertLess(len(statements), 120, "Initial creation must batch products, attributes, EANs and mappings")
            async with self.sessions() as db:
                ids = dict((await db.execute(select(Product.sku, Product.id))).all())
                warehouse = await db.scalar(select(Warehouse.id))
                movement = StockMovement(idempotency_key="large-family-opening", product_id=ids["SHARED-0000"],
                                         warehouse_id=warehouse, movement_type=MovementType.RECEIVING_IN,
                                         quantity=Decimal("7"), unit_cost=Decimal("4"), balance_after=Decimal("7"),
                                         avg_cost_after=Decimal("4"))
                db.add(movement)
                await db.flush()
                db.add(StockBalance(product_id=ids["SHARED-0000"], warehouse_id=warehouse,
                                    qty_on_hand=Decimal("7"), qty_reserved=Decimal("2"), avg_cost=Decimal("4"),
                                    total_value=Decimal("28"), last_movement_id=movement.id))
                await db.commit()
            before = await self.stock_snapshot()
            statements.clear()
            self.fetch.return_value = (split, META)
            second = await self.pull("xtrek")
            self.assertEqual((second["linked_products"], second["created_products"], second["conflict_count"]), (2500, 0, 0))
            self.assertLess(len(statements), 350, "Resolving split families must not query once per leaf")
            await self.pull("xtrek", update_existing=True)
            self.fetch.return_value = (umbrella, META)
            await self.pull(update_existing=True)
            async with self.sessions() as db:
                self.assertEqual(dict((await db.execute(select(Product.sku, Product.id))).all()), ids)
                self.assertEqual(await db.scalar(select(func.count()).select_from(ShopProduct)), 5000)
                self.assertEqual(await db.scalar(select(func.count()).select_from(ProductIdentifier)), 1250)
                self.assertEqual(await db.scalar(select(func.count()).select_from(ProductVariantAttribute)), 2500)
                self.assertEqual(await db.scalar(select(func.count()).select_from(ProductGroup)), 0)
                self.assertEqual(await db.scalar(select(func.count()).select_from(Product).where(Product.group_id.is_not(None))), 0)
                parent_counts = (await db.execute(select(Shop.code, func.count(func.distinct(ShopProduct.parent_code)))
                                               .join(ShopProduct, Shop.id == ShopProduct.shop_id).group_by(Shop.code))).all()
                self.assertEqual(dict(parent_counts), {"biketrek": 1, "xtrek": 25})
            self.assertEqual(await self.stock_snapshot(), before)
        finally:
            event.remove(self.engine.sync_engine, "before_cursor_execute", count_sql)

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

    async def test_one_selected_sku_does_not_push_thousands_of_umbrella_siblings(self):
        products = [SimpleNamespace(id=position + 1, sku=f"POS-{position:04}") for position in range(2500)]
        mapping = SimpleNamespace(shop_id=1, parent_code="POS-ALL", external_code=products[0].sku)
        content = SimpleNamespace(data={"code": "POS-ALL", "variants": [{"code": p.sku} for p in products]})
        db = MagicMock(spec=AsyncSession)
        db.execute.side_effect = [self.row(products[0]), self.row(mapping), self.row(content), self.row(rows=products)]
        client = MagicMock()
        with patch.object(upgates_sync, "_get_shop", AsyncMock(return_value=SimpleNamespace(id=2))), \
                patch.object(upgates_sync.UpgatesClient, "from_shop", return_value=client):
            result = await upgates_sync.push_products_to_shop("xtrek", {"skus": [products[0].sku]}, db)
        self.assertEqual(result["pushed_products"], 0)
        self.assertEqual(result["skipped"][0]["reason"], "selection_expands_family")
        client.post.assert_not_called()
        self.assertEqual(db.execute.await_count, 4, "No per-sibling stock queries are needed for rejected expansion")

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
