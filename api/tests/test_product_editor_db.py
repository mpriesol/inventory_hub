"""Manual product overrides and source isolation in disposable local PostgreSQL."""
import asyncio
import copy
import json
import os
import unittest
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from inventory_hub.db_models import IdentifierType, MovementType, Product, ProductGroup, ProductIdentifier, Shop, Supplier, SupplierProduct, Warehouse
from inventory_hub.db_models_ext import ProductSupplySource, ProductVariantAttribute, ShopProduct, ShopProductContent, StockBalance, StockMovement
from inventory_hub.product_editor_models import ProductEditorAudit, ProductEditorOverride, ProductEditorSave
from inventory_hub.fifo_models import FifoLayer, FifoState
from inventory_hub.product_editor_types import ProductEditorSaveRequest
from inventory_hub.services import product_editor as service


TEST_URL = os.environ.get("CATALOG_TEST_DATABASE_URL", "")
D = Decimal


@unittest.skipUnless(TEST_URL, "Set CATALOG_TEST_DATABASE_URL to an isolated localhost *_catalog_test DB")
class ProductEditorDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        parsed = urlsplit(TEST_URL)
        if parsed.hostname not in ("localhost", "127.0.0.1") or not parsed.path.endswith("_catalog_test"):
            raise RuntimeError("Product editor tests require a dedicated localhost *_catalog_test database")
        self.schema = "product_editor_test_" + uuid4().hex
        self.sql_root = Path(__file__).resolve().parents[2] / "infra" / "db-init"
        connection = await asyncpg.connect(TEST_URL)
        try:
            await connection.execute(f'CREATE SCHEMA "{self.schema}"')
            await connection.execute(f'SET search_path TO "{self.schema}"')
            await connection.execute((self.sql_root / "001_schema.sql").read_text())
            await connection.execute("CREATE TYPE payment_status AS ENUM ('unpaid', 'partial', 'paid')")
            for filename in ("002_invoice_management.sql", "004_shop_product_content.sql", "007_order_stock.sql",
                             "011_fifo.sql", "012_product_editor.sql"):
                await connection.execute((self.sql_root / filename).read_text())
        finally:
            await connection.close()
        self.engine = create_async_engine(TEST_URL.replace("postgresql://", "postgresql+asyncpg://", 1),
            poolclass=NullPool, connect_args={"server_settings": {"search_path": self.schema}})
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False, autoflush=False)
        async with self.transaction() as db:
            self.shops = {row.code: row.id for row in (await db.scalars(select(Shop))).all()}
            warehouse = Warehouse(code="editor-central", name="Editor central")
            other = Warehouse(code="editor-other", name="Other warehouse")
            supplier = Supplier(code="editor-supplier", name="Editor supplier")
            frames = ProductGroup(code="REAL-FRAMES", name="Rámy", main_image_url="https://images.example.test/frame.jpg")
            brakes = ProductGroup(code="REAL-BRAKES", name="Brzdy")
            db.add_all([warehouse, other, supplier, frames, brakes])
            await db.flush()
            self.warehouse_id, self.other_id, self.supplier_id = warehouse.id, other.id, supplier.id
            source = SupplierProduct(supplier_id=supplier.id, supplier_sku="SUP-ŽLTÁ-2", name="Supplier source",
                                     images=[{"url": "https://images.example.test/supplier.jpg"}])
            db.add(source)
            await db.flush()
            products = [
                Product(sku="BIKE-2", name="Žltá vidlica 2", brand="Trek", group_id=frames.id, source_supplier_product_id=source.id),
                Product(sku="BIKE-10", name="Žltá vidlica 10", brand="Trek", group_id=frames.id),
                Product(sku="PART-1", name="Brzda", brand="Shimano", group_id=brakes.id),
                *[Product(sku=sku, name=sku, brand="Other") for sku in ("NO-STOCK", "ZERO", "UNKNOWN", "UNVERIFIED")],
            ]
            db.add_all(products)
            await db.flush()
            self.products = {row.sku: row.id for row in products}
            db.add_all([
                ProductIdentifier(product_id=self.products["BIKE-2"], identifier_type=IdentifierType.ean,
                                  value="5901234123457", is_primary=True),
                ProductIdentifier(product_id=self.products["BIKE-2"], identifier_type=IdentifierType.supplier_sku,
                                  supplier_id=supplier.id, value="ALTERNATIVE-222"),
                ProductSupplySource(product_id=self.products["BIKE-2"], supplier_product_id=source.id, is_primary=True),
                ProductVariantAttribute(product_id=self.products["BIKE-2"], attribute_name="Veľkosť", attribute_value="2"),
                ProductVariantAttribute(product_id=self.products["BIKE-10"], attribute_name="Veľkosť", attribute_value="10"),
            ])
            for sku in ("BIKE-2", "BIKE-10", "PART-1"):
                db.add(ShopProduct(shop_id=self.shops["biketrek"], product_id=self.products[sku], external_code="xTrek",
                    parent_code="xTrek", variant_code=sku, is_variant=True, shop_price=D("199.99")))
            for sku in ("BIKE-2", "BIKE-10"):
                db.add(ShopProduct(shop_id=self.shops["xtrek"], product_id=self.products[sku], external_code="REAL-FRAMES",
                    parent_code="REAL-FRAMES", variant_code=sku, is_variant=True, shop_price=D("209.99")))
            umbrella = {"active_yn": True, "descriptions": [{"language": "sk", "title": "POS umbrella"}],
                "variants": [{"code": sku, "active_yn": True, "descriptions": [{"language": "sk", "title": "Observed " + sku}]}
                             for sku in ("BIKE-2", "BIKE-10", "PART-1", "REMOTE-ONLY")]}
            db.add(ShopProductContent(shop_id=self.shops["biketrek"], external_code="xTrek", data=umbrella))
            db.add(ShopProductContent(shop_id=self.shops["xtrek"], external_code="REAL-FRAMES", data=copy.deepcopy(umbrella)))
            for sku, quantity, cost, evidence in (("BIKE-2", "5", "80", True), ("ZERO", "0", "0", True),
                                                  ("UNKNOWN", "2", None, True), ("UNVERIFIED", "0", "0", False)):
                db.add(StockBalance(product_id=self.products[sku], warehouse_id=warehouse.id, qty_on_hand=D(quantity),
                    qty_reserved=D("1") if sku == "BIKE-2" else D("0"), avg_cost=D(cost) if cost is not None else None,
                    total_value=D(quantity) * D(cost) if cost is not None else None))
                if evidence:
                    moved = D(quantity) if D(quantity) else D("1")
                    db.add(StockMovement(idempotency_key=uuid4().hex, product_id=self.products[sku], warehouse_id=warehouse.id,
                        movement_type=MovementType.INITIAL, quantity=moved, unit_cost=D(cost) if cost is not None else None,
                        balance_after=moved, avg_cost_after=D(cost) if cost is not None else None))
                    if not D(quantity):
                        db.add(StockMovement(idempotency_key=uuid4().hex, product_id=self.products[sku], warehouse_id=warehouse.id,
                            movement_type=MovementType.SALE_OUT, quantity=-moved, unit_cost=D(cost),
                            balance_after=D("0"), avg_cost_after=D(cost)))

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

    async def detail(self, sku="BIKE-2", warehouse="editor-central"):
        async with self.sessions() as db:
            return await service.detail(db, self.products[sku], warehouse)

    async def listing(self, **filters):
        async with self.sessions() as db:
            return await service.list_products(db, **filters)

    def change(self, row, **fields):
        return {"product_id": row["id"], "expected_revision": row["revision"], "snapshot_hash": row["snapshot_hash"], **fields}

    async def save(self, changes, *, request_id=None, warehouse="editor-central"):
        async with self.transaction() as db:
            return await service.save(db, ProductEditorSaveRequest(request_id=request_id or uuid4(),
                warehouse_code=warehouse, confirmed=True, changes=changes))

    async def snapshot(self):
        async with self.sessions() as db:
            return {table: (await db.execute(text(f"SELECT * FROM {table} ORDER BY id"))).mappings().all()
                    for table in ("products", "product_groups", "product_identifiers", "product_variant_attributes",
                        "supplier_products", "product_supply_sources", "shop_products", "shop_product_content",
                        "stock_balances", "stock_movements", "reservations", "shop_order_items", "shop_sync_outbox")}

    async def test_lists_all_canonical_products_including_missing_stock_without_expanding_pos_parent(self):
        before = await self.snapshot()
        result = await self.listing(warehouse_code="editor-central")
        self.assertEqual(result["total"], len(self.products))
        self.assertEqual({row["sku"] for row in result["items"]}, set(self.products))
        rows = {row["sku"]: row for row in result["items"]}
        self.assertEqual(rows["BIKE-2"]["group"]["code"], "REAL-FRAMES")
        self.assertEqual(rows["PART-1"]["group"]["code"], "REAL-BRAKES")
        self.assertFalse(rows["NO-STOCK"]["stock"]["known"])
        self.assertIsNone(rows["NO-STOCK"]["stock"]["qty_on_hand"])
        self.assertFalse(result["external_write_enabled"])
        self.assertEqual(await self.snapshot(), before)

    async def test_search_is_accent_insensitive_across_name_ean_and_supplier_codes(self):
        for query, expected in (("zlta vidlica", {"BIKE-2", "BIKE-10"}), ("5901234123457", {"BIKE-2"}),
                                ("sup-zlta-2", {"BIKE-2"}), ("alternative-222", {"BIKE-2"})):
            result = await self.listing(q=query)
            self.assertEqual({row["sku"] for row in result["items"]}, expected, query)
        self.assertEqual((await self.listing(q="REMOTE-ONLY"))["total"], 0)

    async def test_brand_and_shop_filters_use_exact_canonical_mapping_scope(self):
        self.assertEqual((await self.listing(brand="Trek"))["total"], 2)
        self.assertEqual({row["sku"] for row in (await self.listing(shop_code="xtrek"))["items"]}, {"BIKE-2", "BIKE-10"})
        self.assertEqual({row["sku"] for row in (await self.listing(shop_code="biketrek"))["items"]},
                         {"BIKE-2", "BIKE-10", "PART-1"})

    async def test_natural_sku_sort_and_pagination_have_stable_nonoverlapping_pages(self):
        async with self.transaction() as db:
            db.add_all([Product(sku=f"PAGE-{number}", name="Page product") for number in range(1, 31)])
        first = await self.listing(q="PAGE-", sort="sku", page=1, page_size=25)
        second = await self.listing(q="PAGE-", sort="sku", page=2, page_size=25)
        self.assertEqual((first["total"], second["total"]), (30, 30))
        self.assertEqual([row["sku"] for row in first["items"] + second["items"]], [f"PAGE-{n}" for n in range(1, 31)])
        descending = await self.listing(q="PAGE-", sort="sku", direction="desc", page_size=25)
        self.assertEqual(descending["items"][0]["sku"], "PAGE-30")

    async def test_unknown_quantity_unknown_cost_and_verified_zero_are_distinct(self):
        missing, unverified = await self.detail("NO-STOCK"), await self.detail("UNVERIFIED")
        self.assertFalse(missing["stock"]["known"])
        self.assertFalse(unverified["stock"]["known"])
        self.assertIsNone(unverified["stock"]["qty_available"])
        zero, unknown = await self.detail("ZERO"), await self.detail("UNKNOWN")
        self.assertTrue(zero["stock"]["known"])
        self.assertEqual(D(zero["stock"]["qty_available"]), D("0"))
        self.assertEqual(D(zero["stock"]["avg_cost"]), D("0"))
        self.assertTrue(unknown["stock"]["known"])
        self.assertEqual(D(unknown["stock"]["qty_on_hand"]), D("2"))
        self.assertIsNone(unknown["stock"]["avg_cost"])
        self.assertIsNone(unknown["stock"]["total_value"])

    async def test_selected_variant_observations_and_image_do_not_include_umbrella_siblings(self):
        marker = "UNRELATED-SIBLING-PRIVATE-CONTENT"
        async with self.transaction() as db:
            content = await db.scalar(select(ShopProductContent).where(ShopProductContent.shop_id == self.shops["biketrek"]))
            data = copy.deepcopy(content.data)
            data["variants"][2]["images"] = [{"main_yn": True, "url": "https://images.example.test/selected-brake.jpg"}]
            data["variants"][3]["description"] = marker * 1000
            content.data = data
        row = await self.detail("PART-1")
        shop = next(item for item in row["shops"] if item["shop_code"] == "biketrek")
        self.assertEqual(shop["observed"]["name"], "Observed PART-1")
        self.assertEqual(row["image_url"], "https://images.example.test/selected-brake.jpg")
        self.assertNotIn(marker, json.dumps(row))
        self.assertNotIn("REMOTE-ONLY", json.dumps(row))

    async def test_flat_parent_observation_fallback_uses_only_bounded_safe_image(self):
        async with self.transaction() as db:
            content = await db.scalar(select(ShopProductContent).where(ShopProductContent.shop_id == self.shops["biketrek"]))
            content.data = {"active_yn": False, "descriptions": [{"language": "sk", "title": "Parent fallback"}],
                            "images": [{"main_yn": True, "url": "https://images.example.test/parent.jpg"}]}
        row = await self.detail("PART-1")
        shop = next(item for item in row["shops"] if item["shop_code"] == "biketrek")
        self.assertEqual(shop["observed"]["name"], "Parent fallback")
        self.assertFalse(shop["observed"]["visible"])
        self.assertEqual(row["image_url"], "https://images.example.test/parent.jpg")
        async with self.transaction() as db:
            content = await db.scalar(select(ShopProductContent).where(ShopProductContent.shop_id == self.shops["biketrek"]))
            content.data = {"images": [{"url": "https://user:secret@images.example.test/private.jpg"}]}
        self.assertIsNone((await self.detail("PART-1"))["image_url"])

    async def test_provisional_fifo_cost_is_labeled_incomplete_instead_of_presented_as_final(self):
        from datetime import datetime, timezone
        at = datetime.now(timezone.utc)
        async with self.transaction() as db:
            db.add(FifoState(product_id=self.products["BIKE-2"], warehouse_id=self.warehouse_id,
                            revision=1, activated_at=at, activation_kind="test_documented"))
            db.add(FifoLayer(product_id=self.products["BIKE-2"], warehouse_id=self.warehouse_id,
                physical_received_at=at, quantity_original=D("5"), quantity_remaining=D("5"),
                unit_cost=D("80"), cost_status="provisional", stock_status="available", cost_revision=0,
                provenance={"document": "Synthetic estimate"}))
        stock = (await self.detail())["stock"]
        self.assertTrue(stock["known"])
        self.assertEqual(D(stock["qty_available"]), D("4"))
        self.assertFalse(stock["valuation_complete"])
        self.assertEqual((stock["avg_cost"], stock["total_value"]), (None, None))
        self.assertEqual((D(stock["known_value"]), D(stock["provisional_value"]), D(stock["provisional_qty"])),
                         (D("0"), D("400"), D("5")))

    async def test_save_preserves_imported_facts_stock_and_outbox_while_recording_audit(self):
        row = await self.detail()
        before = await self.snapshot()
        result = await self.save([self.change(row, common={"name": "Manual name", "brand": "Manual brand"},
            variant={"sale_price_gross": "123.45", "vat_rate": "23", "note": "Variant note"})])
        saved = result["results"][0]
        self.assertEqual((saved["status"], saved["row"]["revision"]), ("saved", 1))
        self.assertEqual(saved["row"]["common"]["name"], "Manual name")
        self.assertEqual(saved["row"]["variant"]["sale_price_gross"], "123.45")
        self.assertEqual(await self.snapshot(), before)
        detail = await self.detail()
        self.assertEqual(len(detail["audit"]), 1)
        self.assertEqual(detail["audit"][0]["before"]["common"]["name"], "Žltá vidlica 2")
        self.assertEqual(detail["audit"][0]["after"]["common"]["name"], "Manual name")

    async def test_imported_source_change_rejects_stale_snapshot_without_overwriting_new_source(self):
        row = await self.detail()
        async with self.transaction() as db:
            await db.execute(update(Product).where(Product.id == row["id"]).values(name="Newly imported name"))
        before = await self.snapshot()
        result = await self.save([self.change(row, common={"name": "Stale manual change"})])
        self.assertEqual(result["results"][0]["status"], "conflict")
        self.assertEqual(result["results"][0]["row"]["common"]["name"], "Newly imported name")
        self.assertEqual(await self.snapshot(), before)
        self.assertEqual((await self.detail())["revision"], 0)

    async def test_manual_override_survives_future_pull_and_null_reset_restores_latest_inheritance(self):
        row = await self.detail()
        await self.save([self.change(row, common={"name": "Manual title"})])
        async with self.transaction() as db:
            await db.execute(update(Product).where(Product.id == row["id"]).values(name="Later imported title"))
        current = await self.detail()
        self.assertEqual(current["common"]["name"], "Manual title")
        self.assertNotEqual(current["snapshot_hash"], row["snapshot_hash"])
        result = await self.save([self.change(current, common={"name": None})])
        self.assertEqual(result["results"][0]["row"]["common"]["name"], "Later imported title")
        self.assertNotIn("common", result["results"][0]["row"]["overrides"])

    async def test_per_row_invalid_and_conflicting_edits_do_not_roll_back_valid_rows(self):
        first, second, third = await self.detail("BIKE-2"), await self.detail("BIKE-10"), await self.detail("PART-1")
        before = await self.snapshot()
        stale = self.change(third, common={"name": "Stale"})
        stale["expected_revision"] = 99
        result = await self.save([self.change(first, common={"name": "Saved row"}),
            self.change(second, variant={"sale_price_gross": 123.45}), stale])
        self.assertEqual([row["status"] for row in result["results"]], ["saved", "invalid", "conflict"])
        self.assertEqual((await self.detail("BIKE-2"))["revision"], 1)
        self.assertEqual((await self.detail("BIKE-10"))["revision"], 0)
        self.assertEqual((await self.detail("PART-1"))["revision"], 0)
        self.assertEqual(await self.snapshot(), before)

    async def test_request_replay_is_exact_and_changed_request_id_payload_is_rejected(self):
        row = await self.detail()
        identifier = uuid4()
        changes = [self.change(row, common={"internal_note": "Durable note"})]
        first = await self.save(changes, request_id=identifier)
        self.assertEqual(await self.save(changes, request_id=identifier), first)
        async with self.sessions() as db:
            self.assertEqual(await service.get_save(db, str(identifier)), first)
            self.assertEqual(await db.scalar(select(func.count()).select_from(ProductEditorAudit)), 1)
        with self.assertRaises(service.EditorError) as raised:
            await self.save([self.change(row, common={"internal_note": "Changed note"})], request_id=identifier)
        self.assertEqual(raised.exception.code, "product_editor_request_reused")

    async def test_concurrent_same_revision_saves_have_one_winner_and_one_conflict(self):
        row = await self.detail()
        results = await asyncio.wait_for(asyncio.gather(
            self.save([self.change(row, common={"name": "First"})]),
            self.save([self.change(row, common={"name": "Second"})])), timeout=5)
        self.assertEqual(sorted(result["results"][0]["status"] for result in results), ["conflict", "saved"])
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(ProductEditorAudit)), 1)

    async def test_location_and_minimum_require_explicit_warehouse_and_create_no_balance(self):
        row = await self.detail("NO-STOCK", warehouse=None)
        rejected = await self.save([self.change(row, warehouse={"location": "A-2", "min_quantity": "3"})], warehouse=None)
        self.assertEqual(rejected["results"][0]["status"], "invalid")
        row = await self.detail("NO-STOCK")
        before = await self.snapshot()
        result = await self.save([self.change(row, warehouse={"location": "A-2", "min_quantity": "3"})])
        self.assertEqual(result["results"][0]["row"]["warehouse"], {"code": "editor-central", "location": "A-2", "min_quantity": "3"})
        other = await self.detail("NO-STOCK", warehouse="editor-other")
        self.assertIsNone(other["warehouse"]["location"])
        self.assertIsNone(other["warehouse"]["min_quantity"])
        self.assertEqual(await self.snapshot(), before)

    async def test_snapshot_from_one_warehouse_cannot_authorize_edits_in_another(self):
        row = await self.detail("NO-STOCK", warehouse="editor-central")
        result = await self.save([self.change(row, warehouse={"location": "Wrong scope"})], warehouse="editor-other")
        self.assertEqual(result["results"][0]["status"], "conflict")
        self.assertIsNone((await self.detail("NO-STOCK", warehouse="editor-other"))["warehouse"]["location"])

    async def test_shop_overrides_are_unpublished_and_fallback_preserves_observed_price(self):
        row = await self.detail()
        before = await self.snapshot()
        result = await self.save([self.change(row, common={"name": "Common name"}, variant={"sale_price_gross": "150"},
            shops={"biketrek": {"name": "BIKETREK title", "sale_price_gross": "145", "visible": False}})])
        shops = {shop["shop_code"]: shop for shop in result["results"][0]["row"]["shops"]}
        self.assertEqual(shops["biketrek"]["effective"], {"name": "BIKETREK title", "sale_price_gross": "145.00", "visible": False})
        self.assertEqual(shops["xtrek"]["effective"]["name"], "Common name")
        self.assertEqual(shops["xtrek"]["effective"]["sale_price_gross"], "150.00")
        self.assertTrue(all(shop["state"] == "saved_unpublished" for shop in shops.values()))
        self.assertEqual(D(shops["biketrek"]["observed"]["price"]), D("199.99"))
        self.assertEqual(await self.snapshot(), before)
        current = await self.detail()
        reset = await self.save([self.change(current, shops={"biketrek": {"name": None, "sale_price_gross": None, "visible": None}})])
        shop = next(item for item in reset["results"][0]["row"]["shops"] if item["shop_code"] == "biketrek")
        self.assertEqual(shop["effective"], {"name": "Common name", "sale_price_gross": "150.00", "visible": True})

    async def test_editing_one_shared_sku_never_changes_siblings_under_the_pos_umbrella(self):
        row = await self.detail("BIKE-2")
        sibling = await self.detail("BIKE-10")
        other_group = await self.detail("PART-1")
        await self.save([self.change(row, common={"name": "Selected SKU only"})])
        self.assertEqual(await self.detail("BIKE-10"), sibling)
        self.assertEqual(await self.detail("PART-1"), other_group)

    async def test_duplicate_product_patches_are_rejected_without_order_dependent_winner(self):
        row = await self.detail()
        result = await self.save([self.change(row, common={"name": "First"}), self.change(row, common={"name": "Second"})])
        self.assertEqual([entry["status"] for entry in result["results"]], ["invalid", "invalid"])
        self.assertEqual((await self.detail())["revision"], 0)

    async def test_migration_rerun_preserves_overrides_request_result_and_audit(self):
        row = await self.detail()
        saved = await self.save([self.change(row, common={"name": "Persistent override"})])
        before = await self.detail()
        connection = await asyncpg.connect(TEST_URL)
        try:
            await connection.execute(f'SET search_path TO "{self.schema}"')
            await connection.execute((self.sql_root / "012_product_editor.sql").read_text())
        finally:
            await connection.close()
        self.assertEqual(await self.detail(), before)
        async with self.sessions() as db:
            self.assertEqual(await service.get_save(db, saved["request_id"]), saved)
