"""Advisory product candidates against disposable, read-only PostgreSQL queries."""
from contextlib import asynccontextmanager
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from inventory_hub.db_models import IdentifierType, Product, ProductIdentifier, Supplier, SupplierProduct
from inventory_hub.db_models_ext import ProductSupplySource, ProductVariantAttribute
from inventory_hub.product_editor_models import ProductEditorOverride
from inventory_hub.services import product_candidates as service
from inventory_hub.services.product_identity import RemoteIdentity, load_identity_index


TEST_URL = os.environ.get("CATALOG_TEST_DATABASE_URL", "")
EAN_ZERO = "0123456789012"
EAN_OTHER = "5901234123457"
EAN_MANUAL = "4006381333931"


@unittest.skipUnless(TEST_URL, "Set CATALOG_TEST_DATABASE_URL to an isolated localhost *_catalog_test DB")
class ProductCandidatesDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        parsed = urlsplit(TEST_URL)
        if parsed.hostname not in ("localhost", "127.0.0.1") or not parsed.path.endswith("_catalog_test"):
            raise RuntimeError("Product candidate tests require a dedicated localhost *_catalog_test database")
        self.schema = "product_candidates_test_" + uuid4().hex
        sql_root = Path(__file__).resolve().parents[2] / "infra" / "db-init"
        connection = await asyncpg.connect(TEST_URL)
        try:
            await connection.execute(f'CREATE SCHEMA "{self.schema}"')
            await connection.execute(f'SET search_path TO "{self.schema}"')
            for filename in ("001_schema.sql", "012_product_editor.sql"):
                await connection.execute((sql_root / filename).read_text())
        finally:
            await connection.close()
        self.engine = create_async_engine(TEST_URL.replace("postgresql://", "postgresql+asyncpg://", 1),
            poolclass=NullPool, connect_args={"server_settings": {"search_path": self.schema}})
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False, autoflush=False)
        async with self.transaction() as db:
            suppliers = [Supplier(code=code, name=code) for code in ("candidates-a", "candidates-b")]
            db.add_all(suppliers)
            await db.flush()
            self.supplier_a, self.supplier_b = (row.id for row in suppliers)
            products = [
                Product(sku="CANONICAL-A", name="Imported legacy saddle", brand="Imported brand"),
                Product(sku="CANONICAL-B", name="Unrelated handlebar", brand="Other brand"),
                Product(sku="INACTIVE-C", name="Retired pump", is_active=False),
            ]
            db.add_all(products)
            await db.flush()
            self.product_a, self.product_b, self.inactive = (row.id for row in products)
            db.add_all([
                ProductIdentifier(product_id=self.product_a, identifier_type=IdentifierType.ean, value=EAN_ZERO),
                ProductIdentifier(product_id=self.product_b, identifier_type=IdentifierType.ean, value=EAN_OTHER),
                ProductIdentifier(product_id=self.product_a, identifier_type=IdentifierType.supplier_sku,
                                  supplier_id=self.supplier_a, value="SHARED-SUPPLIER-CODE"),
                ProductIdentifier(product_id=self.product_b, identifier_type=IdentifierType.supplier_sku,
                                  supplier_id=self.supplier_b, value="SHARED-SUPPLIER-CODE"),
                ProductVariantAttribute(product_id=self.product_a, attribute_name="Size", attribute_value="S"),
                ProductEditorOverride(product_id=self.product_a, revision=1, data={
                    "common": {"name": "Alpine trail helmet", "brand": "Manual brand"},
                    "variant": {"attributes": [{"name": "Size", "value": "XL"}], "eans": [EAN_MANUAL]},
                }),
            ])
            await db.execute(text("""INSERT INTO stock_balances
                (product_id, warehouse_id, qty_on_hand, qty_reserved, avg_cost, total_value)
                SELECT :product, id, 7, 2, 3.1250, 21.8750 FROM warehouses WHERE code='hlavny-sklad'"""),
                {"product": self.product_a})

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

    async def candidates(self, *queries, limit=5):
        async with self.sessions() as db:
            # PostgreSQL itself rejects writes, including flushes or hidden updates.
            await db.execute(text("SET TRANSACTION READ ONLY"))
            result = await service.find_candidates(db, list(queries), limit=limit)
            self.assertFalse(db.new)
            self.assertFalse(db.dirty)
            self.assertFalse(db.deleted)
            await db.commit()
            return result

    async def snapshot(self):
        tables = ("products", "product_identifiers", "product_variant_attributes", "supplier_products",
                  "product_supply_sources", "stock_balances", "stock_movements", "shop_products",
                  "shop_sync_outbox", "product_editor_overrides", "product_editor_saves", "product_editor_audit")
        async with self.sessions() as db:
            return {table: await db.scalar(text(
                f"SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text), '[]'::jsonb) FROM {table} t"))
                    for table in tables}

    async def test_effective_manual_name_brand_and_attributes_are_searched_and_displayed(self):
        result = await self.candidates(service.CandidateQuery(key="manual", name="Alpine trail helmet"))
        candidate = next(row for row in result["manual"]["candidates"] if row["product_id"] == self.product_a)
        self.assertEqual(candidate["name"], "Alpine trail helmet")
        self.assertEqual(candidate["brand"], "Manual brand")
        self.assertEqual(candidate["attributes"], [{"name": "Size", "value": "XL"}])
        self.assertIn("similar_name", candidate["reasons"])
        self.assertEqual(candidate["eans"], [EAN_ZERO])
        self.assertEqual(candidate["manual_eans"], [EAN_MANUAL])
        old = await self.candidates(service.CandidateQuery(key="old", name="Imported legacy saddle"))
        self.assertNotIn(self.product_a, [row["product_id"] for row in old["old"]["candidates"]])

    async def test_upc_query_finds_equivalent_ean_as_exact_evidence(self):
        async with self.transaction() as db:
            db.add(ProductIdentifier(product_id=self.product_b, identifier_type=IdentifierType.ean,
                                     value="0727670929720"))
        before = await self.snapshot()
        result = await self.candidates(service.CandidateQuery(key="upc", eans=("727670929720",)))
        candidate = result["upc"]["candidates"][0]
        self.assertEqual(candidate["product_id"], self.product_b)
        self.assertIn("exact_ean", candidate["reasons"])
        self.assertNotIn("ean_conflict", candidate["warnings"])
        self.assertIn("0727670929720", candidate["eans"])
        self.assertNotIn("727670929720", candidate["eans"])
        self.assertEqual(await self.snapshot(), before)

    async def test_manual_empty_values_do_not_fall_back_to_imported_values(self):
        async with self.transaction() as db:
            override = await db.get(ProductEditorOverride, self.product_a)
            override.data = {"common": {"brand": ""}, "variant": {"attributes": [], "eans": []}}
        result = await self.candidates(service.CandidateQuery(key="empty", eans=(EAN_ZERO,)))
        candidate = result["empty"]["candidates"][0]
        self.assertEqual(candidate["product_id"], self.product_a)
        self.assertEqual(candidate["brand"], "")
        self.assertEqual(candidate["attributes"], [])
        self.assertEqual(candidate["manual_eans"], [])
        self.assertEqual(candidate["eans"], [EAN_ZERO])
        self.assertIn("exact_ean", candidate["reasons"])

    async def test_name_search_folds_german_accents_and_retains_literal_unicode_matches(self):
        async with self.transaction() as db:
            product = await db.get(Product, self.product_b)
            product.name = "Größe Münch helmet"
        result = await self.candidates(service.CandidateQuery(key="accented", name="Grosse Munch helmet"))
        self.assertIn(self.product_b, [row["product_id"] for row in result["accented"]["candidates"]])

    async def test_valid_barcode_preserves_leading_zero_and_invalid_barcode_is_not_exact(self):
        async with self.transaction() as db:
            db.add_all([
                ProductIdentifier(product_id=self.product_b, identifier_type=IdentifierType.upc,
                                  value="123456789012"),
                # Legacy registry rows are not proof that a checksum is valid.
                ProductIdentifier(product_id=self.product_b, identifier_type=IdentifierType.ean,
                                  value="5901234123458"),
                ProductIdentifier(product_id=self.product_b, identifier_type=IdentifierType.unverified_barcode,
                                  value="123456"),
            ])
        result = await self.candidates(
            service.CandidateQuery(key="zero", eans=(EAN_ZERO,)),
            service.CandidateQuery(key="bad-checksum", eans=("5901234123458",)),
            service.CandidateQuery(key="unverified", eans=("123456",)),
        )
        # The UPC is an equivalent representation of EAN_ZERO. Both existing
        # owners must remain visible, without stripping either stored value or
        # choosing one duplicate as authoritative.
        candidates = {row["product_id"]: row for row in result["zero"]["candidates"]}
        self.assertEqual(set(candidates), {self.product_a, self.product_b})
        self.assertEqual(candidates[self.product_a]["eans"], [EAN_ZERO])
        self.assertIn("123456789012", candidates[self.product_b]["eans"])
        self.assertNotIn(EAN_ZERO, candidates[self.product_b]["eans"])
        self.assertTrue(all("exact_ean" in row["reasons"] for row in candidates.values()))
        async with self.sessions() as db:
            await db.execute(text("SET TRANSACTION READ ONLY"))
            incoming = RemoteIdentity(0, barcodes=(EAN_ZERO,))
            index = await load_identity_index(db, 0, [incoming], local=True)
            resolved = index.resolve(incoming)
            self.assertEqual(resolved.reasons, ["identifier_conflict"])
            self.assertEqual(set(resolved.candidate_product_ids), {self.product_a, self.product_b})
        for key in ("bad-checksum", "unverified"):
            self.assertFalse(any("exact_ean" in row["reasons"] for row in result[key]["candidates"]))

    async def test_supplier_aliases_are_scoped_and_never_guessed_without_supplier(self):
        result = await self.candidates(
            service.CandidateQuery(key="a", supplier_id=self.supplier_a, supplier_sku="SHARED-SUPPLIER-CODE"),
            service.CandidateQuery(key="b", supplier_id=self.supplier_b, supplier_sku="SHARED-SUPPLIER-CODE"),
            service.CandidateQuery(key="none", supplier_sku="SHARED-SUPPLIER-CODE"),
        )
        for key, expected in (("a", self.product_a), ("b", self.product_b)):
            matches = [row["product_id"] for row in result[key]["candidates"] if "supplier_sku" in row["reasons"]]
            self.assertEqual(matches, [expected])
        self.assertFalse(any("supplier_sku" in row["reasons"] for row in result["none"]["candidates"]))

    async def test_supplier_source_links_and_legacy_source_are_scoped_without_orphan_candidates(self):
        async with self.transaction() as db:
            sources = [SupplierProduct(supplier_id=supplier, supplier_sku=code, name="Supplier offer")
                       for supplier, code in ((self.supplier_a, "SOURCE-LINK"), (self.supplier_b, "SOURCE-LINK"),
                                              (self.supplier_a, "SOURCE-LEGACY"), (self.supplier_a, "ORPHAN-OFFER"))]
            db.add_all(sources)
            await db.flush()
            db.add_all([
                ProductSupplySource(product_id=self.product_a, supplier_product_id=sources[0].id),
                ProductSupplySource(product_id=self.product_b, supplier_product_id=sources[1].id),
            ])
            product = await db.get(Product, self.inactive)
            product.source_supplier_product_id = sources[2].id
        result = await self.candidates(
            service.CandidateQuery(key="a", supplier_id=self.supplier_a, supplier_sku="SOURCE-LINK"),
            service.CandidateQuery(key="b", supplier_id=self.supplier_b, supplier_sku="SOURCE-LINK"),
            service.CandidateQuery(key="legacy", supplier_id=self.supplier_a, supplier_sku="SOURCE-LEGACY"),
            service.CandidateQuery(key="orphan", supplier_id=self.supplier_a, supplier_sku="ORPHAN-OFFER"),
        )
        for key, expected in (("a", self.product_a), ("b", self.product_b), ("legacy", self.inactive)):
            matches = [row for row in result[key]["candidates"] if "supplier_sku" in row["reasons"]]
            self.assertEqual([row["product_id"] for row in matches], [expected])
        self.assertFalse(result["legacy"]["candidates"][0]["is_active"])
        self.assertEqual(result["orphan"]["candidates"], [])

    async def test_queries_leave_products_identity_links_and_stock_unchanged(self):
        before = await self.snapshot()
        result = await self.candidates(
            service.CandidateQuery(key="exact", sku="CANONICAL-A"),
            service.CandidateQuery(key="name", name="Alpine trail helmet", brand="Manual brand",
                                   attributes=(("Size", "XL"),)),
            service.CandidateQuery(key="similar", sku="canonical a"),
            service.CandidateQuery(key="inactive", sku="INACTIVE-C"),
        )
        self.assertIn("exact_sku", result["exact"]["candidates"][0]["reasons"])
        similar = next(row for row in result["similar"]["candidates"] if row["product_id"] == self.product_a)
        self.assertIn("similar_sku", similar["reasons"])
        self.assertNotIn("exact_sku", similar["reasons"])
        self.assertFalse(result["inactive"]["candidates"][0]["is_active"])
        self.assertEqual(await self.snapshot(), before)

    async def test_read_does_not_autoflush_unrelated_caller_changes(self):
        before = await self.snapshot()
        async with self.sessions(autoflush=True) as db:
            await db.execute(text("SET TRANSACTION READ ONLY"))
            pending = Product(sku="UNSAVED-CALLER-CARD", name="Pending unrelated work")
            db.add(pending)
            result = await service.find_candidates(db, [service.CandidateQuery(key="exact", sku="CANONICAL-A")])
            self.assertEqual(result["exact"]["candidates"][0]["product_id"], self.product_a)
            self.assertIsNone(pending.id)
            self.assertIn(pending, db.new)
            await db.rollback()
        self.assertEqual(await self.snapshot(), before)

    async def test_fuzzy_pool_truncation_cannot_hide_exact_barcode_or_sku_matches(self):
        async with self.transaction() as db:
            db.add_all([Product(sku=f"TRAIL-{number:04d}", name="Shared trail helmet") for number in range(8)])
            await db.flush()
            exact = Product(sku="LATE-EXACT", name="Shared trail helmet")
            db.add(exact)
            await db.flush()
            exact_id = exact.id
            db.add(ProductIdentifier(product_id=exact_id, identifier_type=IdentifierType.ean, value=EAN_MANUAL))
        with patch.object(service, "FUZZY_POOL_LIMIT", 2):
            result = await self.candidates(
                service.CandidateQuery(key="ean", eans=(EAN_MANUAL,), name="Shared trail helmet"),
                service.CandidateQuery(key="sku", sku="LATE-EXACT", name="Shared trail helmet"),
                limit=1,
            )
        for key, reason in (("ean", "exact_ean"), ("sku", "exact_sku")):
            self.assertEqual([row["product_id"] for row in result[key]["candidates"]], [exact_id])
            self.assertIn(reason, result[key]["candidates"][0]["reasons"])
            self.assertTrue(result[key]["truncated"])

    async def test_result_limit_and_self_exclusion_are_applied_per_query(self):
        async with self.transaction() as db:
            db.add_all([Product(sku=f"LIMIT-{number}", name="Shared workshop helmet") for number in range(6)])
        result = await self.candidates(
            service.CandidateQuery(key="limited", name="Shared workshop helmet"),
            service.CandidateQuery(key="excluded", sku="CANONICAL-A", eans=(EAN_ZERO,),
                                   exclude_product_id=self.product_a),
            limit=2,
        )
        self.assertEqual(len(result["limited"]["candidates"]), 2)
        self.assertTrue(result["limited"]["truncated"])
        self.assertNotIn(self.product_a, [row["product_id"] for row in result["excluded"]["candidates"]])
