"""Real registry uniqueness and cross-supplier reuse in isolated PostgreSQL."""
from current_schema_fixture import apply_current_core_schema
from contextlib import asynccontextmanager
import os
from pathlib import Path
import unittest
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from inventory_hub.db_models import IdentifierType, Product, ProductIdentifier, Supplier
from inventory_hub.sku_policy_models import ProductSkuIdentity, SkuBrand
from inventory_hub.sku_policy_types import BrandAliasCreate, BrandCreate, SkuIdentityInput
from inventory_hub.services import sku_policy as service
from test_sku_policy import evidence


TEST_URL = os.environ.get("CATALOG_TEST_DATABASE_URL", "")


@unittest.skipUnless(TEST_URL, "Set CATALOG_TEST_DATABASE_URL to an isolated localhost *_catalog_test DB")
class SkuPolicyDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        parsed = urlsplit(TEST_URL)
        if parsed.hostname not in ("localhost", "127.0.0.1") or not parsed.path.endswith("_catalog_test"):
            raise RuntimeError("SKU policy tests require a dedicated localhost *_catalog_test database")
        self.schema = "sku_policy_test_" + uuid4().hex
        self.sql_root = Path(__file__).resolve().parents[2] / "infra/db-init"
        connection = await asyncpg.connect(TEST_URL)
        try:
            await connection.execute(f'CREATE SCHEMA "{self.schema}"')
            await connection.execute(f'SET search_path TO "{self.schema}"')
            for filename in ("001_schema.sql", "029_sku_policy.sql"):
                await connection.execute((self.sql_root / filename).read_text())
            await apply_current_core_schema(connection)
        finally:
            await connection.close()
        self.engine = create_async_engine(TEST_URL.replace("postgresql://", "postgresql+asyncpg://", 1),
            poolclass=NullPool, connect_args={"server_settings": {"search_path": self.schema}})
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False, autoflush=False)
        async with self.transaction() as db:
            first, second = Supplier(code="first", name="First"), Supplier(code="second", name="Second")
            db.add_all([first, second])
            await db.flush()
            self.first, self.second = first.id, second.id

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

    async def add_product(self, db, sku="LEGACY-001", brand="Topeak", barcode=None):
        product = Product(sku=sku, name="Fixture leaf", brand=brand)
        db.add(product)
        await db.flush()
        if barcode:
            db.add(ProductIdentifier(product_id=product.id, value=barcode, identifier_type=IdentifierType.ean))
            await db.flush()
        return product

    async def test_registry_replay_unique_codes_and_explicit_aliases(self):
        async with self.transaction() as db:
            initial = await service.list_brands(db)
            self.assertEqual(len(initial["brands"]), 7)
            created = await service.create_brand(db, BrandCreate(name="Test Wheels", code="TW", source_ref="Fixture"))
            with self.assertRaisesRegex(service.SkuPolicyError, "sku_brand_code_taken"):
                await service.create_brand(db, BrandCreate(name="Another", code="TW", source_ref="Fixture"))
            with self.assertRaisesRegex(service.SkuPolicyError, "sku_brand_alias_taken"):
                await service.create_brand(db, BrandCreate(name="TEST  WHEELS", code="TX", source_ref="Fixture"))
            await service.add_brand_alias(db, created["id"], BrandAliasCreate(alias="TestWheels", source_ref="Fixture verified alias"))
            self.assertEqual(await service.registered_brand_id(db, "testwheels"), created["id"])
            self.assertIsNone(await service.registered_brand_id(db, "Test-Wheels"))
            connection = await db.connection()
            raw = await connection.get_raw_connection()
            await raw.driver_connection.execute((self.sql_root / "029_sku_policy.sql").read_text())
            self.assertEqual(await db.scalar(select(func.count()).select_from(SkuBrand)), 8)

    async def test_new_preview_keeps_leading_zeros_and_does_not_create_any_product(self):
        async with self.transaction() as db:
            value = await service.preview_sku(db, SkuIdentityInput(**evidence()))
            self.assertEqual((value["status"], value["sku"]), ("new", "TP-0001410"))
            self.assertEqual(await db.scalar(select(func.count()).select_from(Product)), 0)
            for changes, reason in (({"brand": "Unknown"}, "sku_brand_unregistered"),
                                    ({"code_kind": "supplier"}, "sku_supplier_scope_required"),
                                    ({"code_kind": "gtin", "source_code": "1234567890123"}, "sku_gtin_invalid")):
                blocked = await service.preview_sku(db, SkuIdentityInput(**evidence(**changes)))
                self.assertEqual(blocked["status"], "blocked")
                self.assertEqual(blocked["reasons"], [reason])

    async def test_registered_mpn_reuses_old_card_across_suppliers_without_rename(self):
        async with self.transaction() as db:
            product = await self.add_product(db)
            item = SkuIdentityInput(**evidence())
            await service.record_sku_identity(db, product, item, actor="fixture")
            await service.record_sku_identity(db, product, item, actor="fixture replay")
            result = await service.preview_sku(db, item, supplier_id=self.second, supplier_sku="SECOND-999")
            self.assertEqual((result["status"], result["product_id"], result["sku"]), ("existing", product.id, "LEGACY-001"))
            self.assertEqual(await db.scalar(select(func.count()).select_from(ProductSkuIdentity)), 1)
            proof = await db.scalar(select(ProductSkuIdentity))
            self.assertEqual((proof.source_code, proof.created_by), ("0001410", "fixture"))
            self.assertEqual(product.sku, "LEGACY-001")

    async def test_supplier_exception_does_not_reuse_unproven_equal_code_even_without_claim(self):
        async with self.transaction() as db:
            product = await self.add_product(db, sku="TP-0001410", barcode="5901234123457")
            item = SkuIdentityInput(**evidence(code_kind="supplier", supplier_id=self.second))
            conflict = await service.preview_sku(db, item, supplier_id=self.second, supplier_sku="0001410")
            self.assertEqual(conflict["reasons"], ["sku_source_scope_collision"])
            matched = await service.preview_sku(db, item, barcodes=["5901234123457"], supplier_id=self.second, supplier_sku="0001410")
            self.assertEqual((matched["status"], matched["product_id"]), ("existing", product.id))

    async def test_two_supplier_codes_are_separate_identity_scopes(self):
        async with self.transaction() as db:
            product = await self.add_product(db, sku="TP-0001410")
            first = SkuIdentityInput(**evidence(code_kind="supplier", supplier_id=self.first))
            await service.record_sku_identity(db, product, first)
            same = await service.preview_sku(db, first)
            self.assertEqual(same["status"], "existing")
            other = await service.preview_sku(db, SkuIdentityInput(**evidence(code_kind="supplier", supplier_id=self.second)))
            self.assertEqual(other["reasons"], ["sku_source_scope_collision"])

    async def test_mpn_cannot_disagree_with_existing_gtin_owner_brand_or_verified_mpn(self):
        async with self.transaction() as db:
            product = await self.add_product(db, barcode="5901234123457")
            await service.record_sku_identity(db, product, SkuIdentityInput(**evidence()))
            second = await self.add_product(db, sku="SECOND-001", barcode="4006381333931")
            conflict = await service.preview_sku(db, SkuIdentityInput(**evidence()), barcodes=["4006381333931"])
            self.assertEqual(conflict["status"], "conflict")
            self.assertEqual(set(conflict["candidate_product_ids"]), {product.id, second.id})
            conflict = await service.preview_sku(db, SkuIdentityInput(**evidence(source_code="OTHER-MPN")), barcodes=["5901234123457"])
            self.assertEqual(conflict["reasons"], ["sku_manufacturer_code_conflict"])
            conflict = await service.preview_sku(db, SkuIdentityInput(**evidence(brand="Shimano")), barcodes=["5901234123457"])
            self.assertEqual(conflict["reasons"], ["sku_brand_conflict"])

    async def test_case_collision_and_gtin_representation_are_not_lossy_code_normalization(self):
        async with self.transaction() as db:
            product = await self.add_product(db)
            await service.record_sku_identity(db, product, SkuIdentityInput(**evidence(source_code="Straße-01")))
            case = await service.preview_sku(db, SkuIdentityInput(**evidence(source_code="STRASSE-01")))
            self.assertEqual(case["reasons"], ["sku_source_case_collision"])
            gtin = SkuIdentityInput(**evidence(code_kind="gtin", source_code="727670929720"))
            await service.record_sku_identity(db, product, gtin)
            result = await service.preview_sku(db, SkuIdentityInput(**evidence(code_kind="gtin", source_code="0727670929720")))
            self.assertEqual((result["status"], result["sku"]), ("existing", "LEGACY-001"))

    async def test_service_and_unbranded_namespaces_require_documented_business_codes(self):
        async with self.transaction() as db:
            await service.create_brand(db, BrandCreate(name="Workshop services", code="SV", kind="service", source_ref="Fixture service price list"))
            item = SkuIdentityInput(**evidence(brand="Workshop services", source_code="REPAIR-01", code_kind="service"))
            result = await service.preview_sku(db, item)
            self.assertEqual((result["status"], result["sku"]), ("blocked", "SV-REPAIR-01"))
            self.assertEqual(result["reasons"], ["sku_item_type_conflict"])
            result = await service.preview_sku(db, item, item_type="service")
            self.assertEqual((result["status"], result["sku"]), ("new", "SV-REPAIR-01"))
            wrong = await service.preview_sku(db, SkuIdentityInput(**evidence(brand="Workshop services")))
            self.assertEqual(wrong["reasons"], ["sku_namespace_kind_mismatch"])
            missing = await service.preview_sku(db, None)
            self.assertEqual(missing["reasons"], ["sku_identity_required"])
            await service.create_brand(db, BrandCreate(name="Unbranded parts", code="NB", kind="unbranded", source_ref="Fixture documented catalogue"))
            result = await service.preview_sku(db, SkuIdentityInput(**evidence(
                brand="Unbranded parts", source_code="PART-01", code_kind="unbranded")))
            self.assertEqual((result["status"], result["reasons"]), ("blocked", ["sku_manufacturer_code_required"]))

    async def test_new_physical_cards_require_confirmed_manufacturer_code_but_existing_cards_keep_sku(self):
        async with self.transaction() as db:
            for kind, code in (("supplier", "SUP-77"), ("gtin", "5901234123457")):
                item = SkuIdentityInput(**evidence(code_kind=kind, source_code=code, supplier_id=self.first))
                result = await service.preview_sku(db, item)
                self.assertEqual((result["status"], result["reasons"]), ("blocked", ["sku_manufacturer_code_required"]))
            unverified = SkuIdentityInput(**evidence(leaf_confirmed=False, source_ref=""))
            result = await service.preview_sku(db, unverified)
            self.assertEqual(result["reasons"], ["sku_manufacturer_code_unverified"])
            old = await self.add_product(db, sku="OLD-SHOP-SKU", brand=None, barcode="4006381333931")
            found = await service.preview_sku(db, None, barcodes=["4006381333931"])
            self.assertEqual((found["status"], found["product_id"], found["sku"]), ("existing", old.id, "OLD-SHOP-SKU"))
            incomplete = SkuIdentityInput(brand='Unregistered feed name', source_code='unverified supplier field',
                                          code_kind='manufacturer')
            found = await service.preview_sku(db, incomplete, barcodes=['4006381333931'])
            self.assertEqual((found['status'], found['product_id'], found['sku']), ('existing', old.id, 'OLD-SHOP-SKU'))
            self.assertIsNone(old.brand)

    async def test_two_brands_with_same_raw_code_receive_distinct_automatic_skus(self):
        async with self.transaction() as db:
            first = SkuIdentityInput(**evidence(source_code="00042"))
            other = SkuIdentityInput(**evidence(brand="Shimano", source_code="00042"))
            one = await service.preview_sku(db, first)
            two = await service.preview_sku(db, other)
            self.assertEqual((one["status"], one["sku"]), ("new", "TP-00042"))
            self.assertEqual((two["status"], two["sku"]), ("new", "SH-00042"))
            card = await self.add_product(db, sku=one["sku"], brand="Topeak")
            db.add(ProductIdentifier(product_id=card.id, identifier_type=IdentifierType.supplier_sku,
                supplier_id=self.first, value="00042"))
            await db.flush()
            await service.record_sku_identity(db, card, first)
            same = await service.preview_sku(db, first, supplier_id=self.first, supplier_sku="00042")
            self.assertEqual((same["status"], same["sku"], same["product_id"]), ("existing", "TP-00042", card.id))
            await service.record_sku_identity(db, card, first)
            self.assertEqual(await db.scalar(select(func.count()).select_from(ProductSkuIdentity)), 1)

    async def test_new_raw_sku_alone_cannot_bypass_automatic_prefix_allocation(self):
        async with self.transaction() as db:
            blocked = await service.preview_sku(db, None, sku="00042")
            self.assertEqual((blocked["status"], blocked["reasons"]), ("blocked", ["sku_identity_required"]))
            old = await self.add_product(db, sku="OLD-42")
            existing = await service.preview_sku(db, None, sku="OLD-42")
            self.assertEqual((existing["status"], existing["sku"], existing["product_id"]), ("existing", "OLD-42", old.id))
