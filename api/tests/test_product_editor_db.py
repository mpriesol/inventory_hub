"""Manual product overrides and source isolation in disposable local PostgreSQL."""
from current_schema_fixture import apply_current_core_schema
import asyncio
import copy
import json
import os
import unittest
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
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
from inventory_hub.product_editor_types import ProductEditorSaveRequest, ProductCardCreateRequest
from inventory_hub.services import product_editor as service
from stock_tracking_fixture import confirmed_inventory


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
            await connection.execute((self.sql_root / "019_stock_tracking.sql").read_text())
            await connection.execute("CREATE TYPE payment_status AS ENUM ('unpaid', 'partial', 'paid')")
            for filename in ("002_invoice_management.sql", "004_shop_product_content.sql", "007_order_stock.sql",
                             "011_fifo.sql", "012_product_editor.sql", "014_supplier_availability.sql", "016_product_publication.sql", "017_stock_adjustments.sql", "018_fifo_cost_sync.sql", "029_sku_policy.sql"):
                await connection.execute((self.sql_root / filename).read_text())
            await apply_current_core_schema(connection)
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
                db.add(ShopProduct(
                    shop_id=self.shops['biketrek'],
                    product_id=self.products[sku],
                    external_code=sku,
                    parent_code='xTrek',
                    is_variant=True,
                    shop_price=D('199.99')))
            for sku in ("BIKE-2", "BIKE-10"):
                db.add(ShopProduct(
                    shop_id=self.shops['xtrek'],
                    product_id=self.products[sku],
                    external_code=sku,
                    parent_code='REAL-FRAMES',
                    is_variant=True,
                    shop_price=D('209.99')))
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
                    db.add(confirmed_inventory(self.products[sku], warehouse.id))
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

    async def test_create_card_replay_conflict_and_no_stock_or_shop_side_effects(self):
        before = await self.snapshot()
        payload = ProductCardCreateRequest(request_id=uuid4(), name="Local demo", is_active=False,
            sku_identity={"brand": "Topeak", "source_code": "000-DEMO", "code_kind": "manufacturer",
                "source_ref": "Synthetic manufacturer label for this concrete unit", "leaf_confirmed": True})
        async with self.sessions() as db:
            result = await service.create_card(db, payload)
        row = result["results"][0]["row"]
        self.assertFalse(row["is_active"])
        self.assertEqual(row["sku"], "TP-000-DEMO")
        self.assertTrue(result["created"])
        self.assertEqual(row["revision"], 1)
        self.assertFalse(row["supplier_linked"])
        self.assertFalse(row["stock"]["known"])
        async with self.sessions() as db:
            self.assertEqual(await service.create_card(db, payload), result)
        for changed, code in ((payload.model_copy(update={"name": "Changed"}), "product_editor_request_reused"),
                              (payload.model_copy(update={"request_id": uuid4()}), "sku_identity_conflict")):
            async with self.sessions() as db:
                with self.assertRaises(service.EditorError) as error:
                    await service.create_card(db, changed)
                self.assertEqual(error.exception.code, code)
        after = await self.snapshot()
        self.assertEqual(len(after.pop("products")), len(before.pop("products")) + 1)
        self.assertEqual(after, before)
        async with self.sessions() as db:
            audits = (await db.scalars(select(ProductEditorAudit).where(ProductEditorAudit.product_id == row["id"]))).all()
            self.assertEqual(len(audits), 1)
            self.assertFalse(audits[0].after_data["is_active"])

    async def test_unreviewed_manual_sku_cannot_create_a_card(self):
        before = await self.snapshot()
        async with self.sessions() as db:
            with self.assertRaises(service.EditorError) as error:
                await service.create_card(db, ProductCardCreateRequest(request_id=uuid4(),
                    sku="OLD-000123", name="Unreviewed identity"))
        self.assertEqual(error.exception.code, "sku_identity_required")
        self.assertEqual(await self.snapshot(), before)

    async def test_item_definition_can_change_before_stock_but_not_reinterpret_history(self):
        row = await self.detail("NO-STOCK")
        self.assertFalse(row["item_definition_locked"])
        saved = await self.save([self.change(row, item_type="service")])
        service_row = saved["results"][0]["row"]
        self.assertEqual(service_row["item_type"], "service")
        self.assertFalse(service_row["stock"]["managed"])
        self.assertFalse(service_row["stock"]["known"])
        self.assertIsNone(service_row["stock"]["qty_on_hand"])
        row = await self.detail("BIKE-2")
        self.assertTrue(row["item_definition_locked"])
        for values in ({"item_type": "service"}, {"stock_unit": "pair"}):
            rejected = await self.save([self.change(row, **values)])
            self.assertEqual(rejected["results"][0]["errors"][0]["code"], "item_definition_locked")
            from sqlalchemy.exc import IntegrityError
            async with self.sessions() as db:
                with self.assertRaises(IntegrityError):
                    async with db.begin_nested():
                        await db.execute(update(Product).where(Product.id == row["id"]).values(**values))
        self.assertEqual((await self.detail("BIKE-2"))["stock"]["qty_on_hand"], row["stock"]["qty_on_hand"])

    async def test_service_card_creation_keeps_brand_policy_but_has_no_physical_stock(self):
        request = ProductCardCreateRequest(request_id=uuid4(), name="Workshop service", item_type="service",
            sku_identity={"brand": "Topeak", "source_code": "SERVICE-01", "code_kind": "manufacturer",
                "source_ref": "Reviewed workshop card identity", "leaf_confirmed": True})
        async with self.sessions() as db:
            result = await service.create_card(db, request)
        row = result["results"][0]["row"]
        self.assertEqual((row["sku"], row["item_type"]), ("TP-SERVICE-01", "service"))
        self.assertFalse(row["stock"]["managed"])
        self.assertFalse(row["stock"]["known"])
        async with self.sessions() as db:
            self.assertEqual(await service.create_card(db, request), result)
            self.assertEqual(await db.scalar(select(func.count()).select_from(StockMovement)
                .where(StockMovement.product_id == row["id"])), 0)

    async def test_manual_lookup_reuses_legacy_card_without_renaming_or_overwriting_it(self):
        before = await self.snapshot()
        async with self.sessions() as db:
            result = await service.create_card(db, ProductCardCreateRequest(request_id=uuid4(),
                sku="BIKE-2", is_active=False))
        self.assertFalse(result["created"])
        row = result["results"][0]["row"]
        self.assertEqual(row["id"], self.products["BIKE-2"])
        self.assertEqual(row["sku"], "BIKE-2")
        self.assertEqual(row["common"]["name"], "Žltá vidlica 2")
        self.assertTrue(row["is_active"])
        self.assertEqual(row["revision"], 1)
        self.assertEqual(await self.snapshot(), before)

    async def test_manual_creation_reuses_verified_identity_and_adds_second_supplier_without_feed(self):
        from inventory_hub.sku_policy_models import ProductSkuIdentity
        identity = {"brand": "Topeak", "source_code": "000-UNIT", "code_kind": "manufacturer",
            "source_ref": "Synthetic manufacturer leaf code on label", "leaf_confirmed": True}
        async with self.sessions() as db:
            first = await service.create_card(db, ProductCardCreateRequest(request_id=uuid4(), name="Original name",
                sku_identity=identity, eans=["4006381333931"], supplier_id=self.supplier_id, supplier_sku="001-A"))
        product_id = first["results"][0]["product_id"]
        async with self.transaction() as db:
            other = Supplier(code="manual-no-feed", name="Supplier without filesystem feed configuration")
            db.add(other)
            await db.flush()
            other_id = other.id
        async with self.sessions() as db:
            second = await service.create_card(db, ProductCardCreateRequest(request_id=uuid4(), name="Do not overwrite",
                is_active=False, sku_identity=identity, supplier_id=other_id, supplier_sku="0007"))
        self.assertTrue(second["reused"])
        self.assertEqual(second["results"][0]["product_id"], product_id)
        row = second["results"][0]["row"]
        self.assertEqual(row["sku"], "TP-000-UNIT")
        self.assertTrue(row["is_active"])
        self.assertEqual(row["common"]["name"], "Original name")
        self.assertFalse(row["stock"]["known"])
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(ProductSupplySource).where(
                ProductSupplySource.product_id == product_id)), 2)
            self.assertEqual(await db.scalar(select(func.count()).select_from(ProductSkuIdentity).where(
                ProductSkuIdentity.product_id == product_id)), 1)
            self.assertEqual(await db.scalar(select(func.count()).select_from(StockMovement).where(
                StockMovement.product_id == product_id)), 0)
            self.assertEqual(await db.scalar(select(func.count()).select_from(ShopProduct).where(
                ShopProduct.product_id == product_id)), 0)
        rename = await self.save([self.change(row, variant={"sku": "TP-ARBITRARY"})], warehouse=None)
        self.assertEqual(rename["results"][0]["errors"][0]["code"], "sku_registered_rename_blocked")

    async def test_manual_identity_contradicting_existing_ean_cannot_steal_supplier_source(self):
        before = await self.snapshot()
        async with self.sessions() as db:
            with self.assertRaises(service.EditorError) as error:
                await service.create_card(db, ProductCardCreateRequest(request_id=uuid4(), name="Conflicting card",
                    sku_identity={"brand": "Topeak", "source_code": "OTHER-LEAF", "code_kind": "manufacturer",
                        "source_ref": "Synthetic manufacturer label", "leaf_confirmed": True},
                    eans=["5901234123457"], supplier_id=self.supplier_id, supplier_sku="ALTERNATIVE-222"))
        self.assertEqual(error.exception.code, "sku_identity_conflict")
        self.assertIn("sku_brand_conflict", error.exception.reasons)
        self.assertEqual(await self.snapshot(), before)

    async def test_manual_supplier_binding_rejects_contradictory_brand_or_variant_even_with_same_ean(self):
        for code, brand, axes, reason in (
                ("BRAND-CONFLICT", "Shimano", [], "sku_brand_conflict"),
                ("SIZE-CONFLICT", "Trek", [{"name": "Veľkosť", "value": "3"}], "variant_axes_conflict")):
            async with self.transaction() as db:
                db.add(SupplierProduct(supplier_id=self.supplier_id, supplier_sku=code,
                    name="Contradictory supplier facts", brand=brand, ean="5901234123457",
                    attributes={"catalog": {"brand": brand, "variant_attributes": axes}}))
            before = await self.snapshot()
            async with self.sessions() as db:
                with self.assertRaises(service.EditorError) as error:
                    await service.create_card(db, ProductCardCreateRequest(request_id=uuid4(), name="Existing card",
                        sku="BIKE-2", supplier_id=self.supplier_id, supplier_sku=code, eans=["5901234123457"]))
            self.assertEqual(error.exception.code, reason)
            self.assertEqual(await self.snapshot(), before)

    async def test_activity_change_retains_links_and_uses_revision_audit(self):
        before = await self.snapshot()
        row = await self.detail()
        result = await self.save([self.change(row, is_active=False)])
        changed = result["results"][0]["row"]
        self.assertEqual(result["results"][0]["status"], "saved")
        self.assertFalse(changed["is_active"])
        self.assertTrue(changed["supplier_linked"])
        self.assertEqual(changed["revision"], row["revision"] + 1)
        conflict = await self.save([self.change(row, is_active=True)])
        self.assertEqual(conflict["results"][0]["status"], "conflict")
        after = await self.snapshot()
        before.pop("products"); after.pop("products")
        self.assertEqual(after, before)
        self.assertFalse((await self.detail())["is_active"])

    async def test_activity_and_supplier_binding_filters_combine_before_pagination(self):
        async with self.transaction() as db:
            for active in (True, False):
                for linked in (True, False):
                    product = Product(sku=f"FILTER-{active}-{linked}", name="Filter", is_active=active)
                    db.add(product); await db.flush()
                    if linked:
                        source = SupplierProduct(supplier_id=self.supplier_id,
                            supplier_sku=f"FILTER-SOURCE-{active}", name="Source")
                        db.add(source); await db.flush()
                        # A paused link still exists; this filter is not crawler eligibility.
                        db.add(ProductSupplySource(product_id=product.id, supplier_product_id=source.id, is_active=False))
                    else:
                        db.add(ProductIdentifier(product_id=product.id, identifier_type=IdentifierType.supplier_sku,
                            supplier_id=self.supplier_id, value=f"ALIAS-{active}"))
        for active in (True, False):
            for linked in (True, False):
                result = await self.listing(q="FILTER-", activity="active" if active else "inactive",
                    supplier_link="linked" if linked else "unlinked", stock_scope="unconfirmed", warehouse_code="editor-central", page_size=25)
                self.assertEqual(result["total"], 1)
                self.assertEqual(result["items"][0]["sku"], f"FILTER-{active}-{linked}")
                self.assertEqual(result["items"][0]["supplier_linked"], linked)
        self.assertEqual((await self.listing(q="FILTER-", stock_scope="confirmed", warehouse_code="editor-central"))["total"], 0)
        async with self.transaction() as db:
            product = await db.get(Product, self.products["BIKE-2"])
            await db.execute(text("DELETE FROM product_supply_sources WHERE product_id = :id"), {"id": product.id})
        self.assertEqual((await self.listing(q="BIKE-2", supplier_link="linked"))["total"], 0,
            "Creation provenance and supplier aliases are not current supply links")
        self.assertEqual((await self.listing(q="BIKE-2", supplier_link="unlinked"))["total"], 1)

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

    async def test_supplier_code_search_uses_explicit_links_and_ignores_provenance_without_duplicates(self):
        async with self.transaction() as db:
            legacy_source = SupplierProduct(supplier_id=self.supplier_id, supplier_sku="RAW-ŽLTÁ-001", name="Legacy source")
            linked_source = SupplierProduct(supplier_id=self.supplier_id, supplier_sku="RAW-ŽLTÁ-002", name="Linked source")
            other_source = SupplierProduct(supplier_id=self.supplier_id, supplier_sku="RAW-ŽLTÁ-003", name="Second linked source")
            orphan = SupplierProduct(supplier_id=self.supplier_id, supplier_sku="RAW-ORPHAN", name="No local product")
            wildcard = SupplierProduct(supplier_id=self.supplier_id, supplier_sku="RAW-100%_CODE", name="Literal wildcard")
            db.add_all([legacy_source, linked_source, other_source, orphan, wildcard])
            await db.flush()
            legacy = Product(sku="PL-LEGACY-SEARCH", name="Legacy only", source_supplier_product_id=legacy_source.id)
            linked = Product(sku="PL-LINKED-SEARCH", name="Explicit only")
            literal = Product(sku="PL-LITERAL-SEARCH", name="Literal code only", source_supplier_product_id=wildcard.id)
            db.add_all([legacy, linked, literal])
            await db.flush()
            db.add_all([ProductSupplySource(product_id=linked.id, supplier_product_id=linked_source.id),
                ProductSupplySource(product_id=linked.id, supplier_product_id=other_source.id),
                ProductSupplySource(product_id=literal.id, supplier_product_id=wildcard.id)])
        for query, expected in (("raw-zlta-001", set()), ("raw-zlta-002", {"PL-LINKED-SEARCH"}),
                                ("raw-zlta", {"PL-LINKED-SEARCH"}),
                                ("PL-LEGACY-SEARCH", {"PL-LEGACY-SEARCH"}),
                                ("PL-LINKED-SEARCH", {"PL-LINKED-SEARCH"}), ("raw-orphan", set()),
                                ("raw-missing", set()), ("100%_CODE", {"PL-LITERAL-SEARCH"}), ("100X_CODE", set())):
            with self.subTest(query=query):
                result = await self.listing(q=query)
                self.assertEqual({row["sku"] for row in result["items"]}, expected)
                self.assertEqual(result["total"], len(expected))

    async def test_unmatched_sku_search_does_not_rescan_supplier_catalog_per_product(self):
        async with self.transaction() as db:
            db.add_all([Product(sku=f"PERF-PRODUCT-{number}", name="Unrelated product") for number in range(1000)])
            db.add_all([SupplierProduct(supplier_id=self.supplier_id, supplier_sku=f"PERF-SOURCE-{number}",
                name="Unrelated supplier item") for number in range(1000)])
        async with self.sessions() as db:
            count_statements = []
            original_scalar = db.scalar

            async def capture(statement, *args, **kwargs):
                count_statements.append(statement)
                return await original_scalar(statement, *args, **kwargs)

            with patch.object(db, "scalar", side_effect=capture):
                result = await service.list_products(db, q="PL-NOT-IN-SUPPLIER-CATALOG")
            self.assertEqual(result["total"], 0)
            count_sql = str(count_statements[0].compile(dialect=db.bind.dialect, compile_kwargs={"literal_binds": True}))
            plan = await db.scalar(text("EXPLAIN (ANALYZE, FORMAT JSON) " + count_sql))
            if isinstance(plan, str):
                plan = json.loads(plan)
            nodes, supplier_scans = [plan[0]["Plan"]], []
            while nodes:
                node = nodes.pop()
                if node.get("Relation Name") == "supplier_products":
                    supplier_scans.append(node)
                nodes.extend(node.get("Plans", []))
            self.assertTrue(supplier_scans, "The query plan must inspect supplier identities")
            catalog_size = await db.scalar(select(func.count()).select_from(SupplierProduct))
            visited = sum((node["Actual Rows"] + node.get("Rows Removed by Filter", 0)) * node["Actual Loops"]
                for node in supplier_scans)
            # Permit alternative index/join plans, but not a catalog-sized scan
            # for each of the 1,000 unrelated products. Avoid timing thresholds.
            self.assertLessEqual(visited, catalog_size * 3, supplier_scans)

    async def test_brand_and_shop_filters_use_exact_canonical_mapping_scope(self):
        self.assertEqual((await self.listing(brand="Trek"))["total"], 2)
        self.assertEqual({row["sku"] for row in (await self.listing(shop_code="xtrek"))["items"]}, {"BIKE-2", "BIKE-10"})
        self.assertEqual({row["sku"] for row in (await self.listing(shop_code="biketrek"))["items"]},
                         {"BIKE-2", "BIKE-10", "PART-1"})

    async def test_brand_alias_filter_includes_every_case_without_rewriting_products(self):
        async with self.transaction() as db:
            db.add(Product(sku='BRAND-ALIAS', name='Alias', brand=' SHIMANO '))
        self.assertEqual({r['sku'] for r in (await self.listing(brand='shimano'))['items']}, {'PART-1', 'BRAND-ALIAS'})
        async with self.transaction() as db:
            options = await service.options(db)
            self.assertEqual(sum(b.casefold() == 'shimano' for b in options['brands']), 1)
            self.assertEqual(await db.scalar(select(Product.brand).where(Product.sku == 'BRAND-ALIAS')), ' SHIMANO ')

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

    async def test_family_color_and_apparel_size_order_is_applied_before_pagination(self):
        sizes = ["XS", "S", "M", "L", "XL", "XXL", "2", "10"]
        expected = [(color, size) for color in ("Blue", "Green", "Red", "White") for size in sizes]
        async with self.transaction() as db:
            family = ProductGroup(code="AXIS-FAMILY", name="Axis family")
            db.add(family)
            await db.flush()
            # Deliberately reversed insertion/SKU ordering and size before colour
            # in display_order must not override the declared family axis order.
            for index, (color, size) in enumerate(reversed(expected)):
                product = Product(sku=f"AXIS-{index + 1}", name="Axis fixture", group_id=family.id)
                db.add(product)
                await db.flush()
                db.add_all([
                    ProductVariantAttribute(product_id=product.id, attribute_name=" Veľkosť ", attribute_value=size, display_order=0),
                    ProductVariantAttribute(product_id=product.id, attribute_name="Colour", attribute_value=color, display_order=1),
                ])
        first = await self.listing(q="AXIS-", sort="group_sku", page_size=25)
        second = await self.listing(q="AXIS-", sort="group_sku", page=2, page_size=25)
        def axes(row):
            values = {attribute["name"].strip(): attribute["value"] for attribute in row["attributes"]}
            return values["Colour"], values["Veľkosť"]
        self.assertEqual(first["total"], 32)
        self.assertEqual([axes(row) for row in first["items"] + second["items"]], expected)
        descending = await self.listing(q="AXIS-", sort="group_sku", direction="desc", page_size=100)
        self.assertEqual([axes(row) for row in descending["items"]], list(reversed(expected)))

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
