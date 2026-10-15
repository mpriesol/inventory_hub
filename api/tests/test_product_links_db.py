"""Link-only writes, frozen selection and retries in an isolated PostgreSQL schema."""
from contextlib import asynccontextmanager
from copy import deepcopy
import asyncio
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

from inventory_hub.db_models import IdentifierType, Product, ProductIdentifier, Shop
from inventory_hub.db_models_ext import ShopProduct, ShopProductContent
from inventory_hub.product_link_types import ProductLinksApplyRequest, ProductLinksPreviewRequest
from inventory_hub.services import product_links as service
from inventory_hub.services.product_identity import IDENTITY_WRITE_LOCK


TEST_URL = os.environ.get("CATALOG_TEST_DATABASE_URL", "")
EAN_A = "5901234123457"
EAN_B = "4006381333931"


def leaf(code, external_id, *, eans=(), parent_code=None, **changes):
    return {"code": code, "parent_code": parent_code or code, "is_variant": bool(parent_code),
            "external_id": str(external_id), "parent_id": "800" if parent_code else str(external_id),
            "name": "Remote " + code, "eans": list(eans), "attributes": [], **changes}


@unittest.skipUnless(TEST_URL, "Set CATALOG_TEST_DATABASE_URL to an isolated localhost *_catalog_test DB")
class ProductLinksDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        parsed = urlsplit(TEST_URL)
        if parsed.hostname not in ("localhost", "127.0.0.1") or not parsed.path.endswith("_catalog_test"):
            raise RuntimeError("Product link tests require a dedicated localhost *_catalog_test database")
        self.schema = "product_links_test_" + uuid4().hex
        root = Path(__file__).resolve().parents[2] / "infra" / "db-init"
        connection = await asyncpg.connect(TEST_URL)
        try:
            await connection.execute(f'CREATE SCHEMA "{self.schema}"')
            await connection.execute(f'SET search_path TO "{self.schema}"')
            for name in ("001_schema.sql", "004_shop_product_content.sql", "012_product_editor.sql", "027_product_links.sql"):
                await connection.execute((root / name).read_text())
        finally:
            await connection.close()
        self.engine = create_async_engine(TEST_URL.replace("postgresql://", "postgresql+asyncpg://", 1), poolclass=NullPool,
            connect_args={"server_settings": {"search_path": self.schema}})
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False, autoflush=False)
        self.remote = [leaf("SHARED-A", 101, eans=[EAN_A]), leaf("SHARED-B", 102, eans=[EAN_B])]
        self.fingerprint = "a" * 64
        self.discover = AsyncMock(side_effect=self.discover_remote)
        self.verify = AsyncMock(side_effect=self.verify_remote)
        self.patches = [patch.object(service.source, "discover", self.discover),
                        patch.object(service.source, "verify", self.verify)]
        for patcher in self.patches:
            patcher.start()
        async with self.transaction() as db:
            self.shop_id = await db.scalar(select(Shop.id).where(Shop.code == "xtrek"))
            products = [Product(sku=code, name="Local " + code, brand="Retain brand", supplier_price="3.1250")
                        for code in ("SHARED-A", "SHARED-B", "NO-REMOTE")]
            db.add_all(products)
            await db.flush()
            self.ids = [row.id for row in products]
            db.add_all([ProductIdentifier(product_id=self.ids[0], identifier_type=IdentifierType.ean, value=EAN_A),
                        ProductIdentifier(product_id=self.ids[1], identifier_type=IdentifierType.ean, value=EAN_B)])
            await db.execute(text("""INSERT INTO stock_balances
                (product_id, warehouse_id, qty_on_hand, qty_reserved, avg_cost, total_value)
                SELECT :product, id, 7, 2, 3.1250, 21.8750 FROM warehouses WHERE code='hlavny-sklad'"""),
                {"product": self.ids[0]})
            await db.execute(text("""INSERT INTO stock_movements
                (idempotency_key, product_id, warehouse_id, movement_type, quantity, unit_cost,
                 balance_after, avg_cost_after, notes)
                SELECT 'fixture-existing-receipt', :product, id, 'RECEIVING_IN', 7, 3.1250, 7, 3.1250,
                       'Existing movement must remain unchanged' FROM warehouses WHERE code='hlavny-sklad'"""),
                {"product": self.ids[0]})

    async def asyncTearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()
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

    async def discover_remote(self, shop_code, codes, expected_fingerprint=None, **kwargs):
        self.assertEqual(shop_code, "xtrek")
        return {"fingerprint": self.fingerprint, "leaves": deepcopy(self.remote), "requests": 1}

    async def verify_remote(self, shop_code, leaves, expected_fingerprint, **kwargs):
        self.assertEqual(shop_code, "xtrek")
        wanted = {row["code"] for row in leaves}
        return {"fingerprint": self.fingerprint,
                "leaves": deepcopy([row for row in self.remote if row["code"] in wanted]), "requests": 1}

    def request(self, **changes):
        return ProductLinksPreviewRequest(**{"request_id": uuid4(), "shop_code": "xtrek", "product_ids": self.ids, **changes})

    async def preview(self, request=None):
        async with self.transaction() as db:
            return await service.preview(db, request or self.request())

    async def apply(self, preview, ids, **changes):
        request = ProductLinksApplyRequest(**{"preview_hash": preview["preview_hash"],
            "product_ids": ids, "confirmed": True, **changes})
        async with self.transaction() as db:
            return await service.apply(db, str(preview["id"]), request)

    async def snapshot(self):
        async with self.sessions() as db:
            return {table: await db.scalar(text(f"SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY t.id), '[]'::jsonb) FROM {table} t"))
                    for table in ("products", "product_identifiers", "product_supply_sources", "stock_balances",
                                  "stock_movements", "shop_sync_outbox", "shop_orders")}

    async def mappings(self):
        async with self.sessions() as db:
            return list((await db.scalars(select(ShopProduct).order_by(ShopProduct.product_id))).all())

    async def test_preview_is_link_only_and_same_request_replays_without_network(self):
        before = await self.snapshot()
        request = self.request()
        result = await self.preview(request)
        rows = {row["product_id"]: row for row in result["rows"]}
        self.assertEqual([rows[i]["status"] for i in self.ids], ["ready", "ready", "not_found"])
        self.assertEqual(result["status"], "prepared")
        self.assertEqual(await self.snapshot(), before)
        self.assertEqual(await self.mappings(), [])
        replay = await self.preview(request)
        self.assertEqual(replay["id"], result["id"])
        self.assertEqual(replay["preview_hash"], result["preview_hash"])
        self.discover.assert_awaited_once()
        with self.assertRaises(service.LinkError):
            await self.preview(request.model_copy(update={"product_ids": [self.ids[0]]}))
        self.assertEqual(await self.snapshot(), before)

    async def test_cached_scalar_and_array_ean_discover_different_code_with_bounded_fallback(self):
        self.remote = [leaf("REMOTE-ALIAS", 303, eans=[EAN_A], parent_code="REMOTE-FAMILY")]
        calls = []

        async def discover_requested(shop_code, codes, expected_fingerprint=None, **kwargs):
            self.assertEqual(shop_code, "xtrek")
            calls.append((list(codes), expected_fingerprint, kwargs.get("max_requests")))
            return {"fingerprint": self.fingerprint,
                    "leaves": deepcopy([row for row in self.remote if row["code"] in codes]), "requests": 2}

        self.discover.side_effect = discover_requested
        for eans in ([EAN_A, "unverified-token"], EAN_A):
            with self.subTest(eans=eans):
                async with self.transaction() as db:
                    cached = await db.scalar(select(ShopProductContent).where(ShopProductContent.shop_id == self.shop_id))
                    data = {"product_id": 800, "code": "REMOTE-FAMILY", "variants": [
                        {"variant_id": 303, "code": "REMOTE-ALIAS", "ean": eans}]}
                    if cached is None:
                        db.add(ShopProductContent(shop_id=self.shop_id, external_code="REMOTE-FAMILY", data=data))
                    else:
                        cached.data = data
                before = await self.snapshot()
                calls.clear()
                preview = await self.preview(self.request(product_ids=[self.ids[0]]))
                self.assertEqual(preview["rows"][0]["status"], "ready")
                self.assertEqual(preview["rows"][0]["match_by"], "validated_barcode")
                self.assertEqual(preview["rows"][0]["remote"]["code"], "REMOTE-ALIAS")
                self.assertEqual(calls, [(["SHARED-A"], None, 8), (["REMOTE-ALIAS"], self.fingerprint, 6)])
                self.assertEqual(preview["remote_requests"], 4)
                self.assertEqual(await self.mappings(), [])
                self.assertEqual(await self.snapshot(), before)

    async def test_apply_links_only_selected_subset_and_exact_replay_is_idempotent(self):
        before = await self.snapshot()
        preview = await self.preview()
        result = await self.apply(preview, [self.ids[0]])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["result"]["linked_product_ids"], [self.ids[0]])
        mappings = await self.mappings()
        self.assertEqual([(row.product_id, row.external_code, row.external_id) for row in mappings],
                         [(self.ids[0], "SHARED-A", "101")])
        self.assertFalse(mappings[0].push_pending)
        self.assertEqual(await self.snapshot(), before)
        replay = await self.apply(preview, [self.ids[0]])
        self.assertEqual(replay["result"], result["result"])
        self.assertEqual(len(await self.mappings()), 1)
        self.verify.assert_awaited_once()
        with self.assertRaises(service.LinkError):
            await self.apply(preview, [self.ids[1]])
        self.assertEqual(len(await self.mappings()), 1)

    async def test_selection_and_hash_must_belong_to_frozen_preview(self):
        preview = await self.preview(self.request(product_ids=[self.ids[0]]))
        for ids, changes in (([self.ids[1]], {}), ([self.ids[0]], {"preview_hash": "b" * 64})):
            with self.subTest(ids=ids, changes=changes), self.assertRaises(service.LinkError):
                await self.apply(preview, ids, **changes)
        self.assertEqual(await self.mappings(), [])
        self.verify.assert_not_awaited()
        # Invalid requests must not expire another operator's valid preview.
        completed = await self.apply(preview, [self.ids[0]])
        self.assertEqual(completed["status"], "completed")

    async def test_changed_remote_identity_rejects_whole_selected_batch(self):
        before = await self.snapshot()
        preview = await self.preview()
        self.remote[1]["external_id"] = "999"
        with self.assertRaises(service.LinkError):
            await self.apply(preview, self.ids[:2])
        self.assertEqual(await self.mappings(), [])
        self.assertEqual(await self.snapshot(), before)

    async def test_mapping_added_after_preview_cannot_be_overwritten(self):
        preview = await self.preview(self.request(product_ids=[self.ids[0]]))
        async with self.transaction() as db:
            db.add(ShopProduct(shop_id=self.shop_id, product_id=self.ids[0], external_code="EXISTING-ALIAS",
                               external_id="777", is_listed=True))
        with self.assertRaises(service.LinkError):
            await self.apply(preview, [self.ids[0]])
        mappings = await self.mappings()
        self.assertEqual([(row.external_code, row.external_id) for row in mappings], [("EXISTING-ALIAS", "777")])

    async def test_local_identity_edit_after_preview_rejects_whole_batch(self):
        preview = await self.preview(self.request(product_ids=self.ids[:2]))
        async with self.transaction() as db:
            product = await db.get(Product, self.ids[1])
            product.sku = "CHANGED-AFTER-PREVIEW"
        before = await self.snapshot()
        with self.assertRaises(service.LinkError) as changed:
            await self.apply(preview, self.ids[:2])
        self.assertEqual(changed.exception.code, "product_links_identity_changed")
        self.assertEqual(await self.mappings(), [])
        self.assertEqual(await self.snapshot(), before)

    async def test_conflicting_barcode_owner_cannot_be_overridden_by_manual_match(self):
        self.remote = [leaf("MANUAL-ALIAS", 103, eans=[EAN_B])]
        request = self.request(product_ids=[self.ids[0]], manual_matches=[{
            "product_id": self.ids[0], "code": "MANUAL-ALIAS", "parent_code": "MANUAL-ALIAS", "is_variant": False}])
        preview = await self.preview(request)
        self.assertEqual(preview["rows"][0]["status"], "conflict")
        with self.assertRaises(service.LinkError):
            await self.apply(preview, [self.ids[0]])
        self.assertEqual(await self.mappings(), [])

    async def test_explicit_different_code_links_without_rewriting_canonical_identity(self):
        before = await self.snapshot()
        self.remote = [leaf("SHOP-ALIAS", 103, eans=[EAN_A], parent_code="REMOTE-PARENT")]
        preview = await self.preview(self.request(product_ids=[self.ids[0]], manual_matches=[{
            "product_id": self.ids[0], "code": "SHOP-ALIAS", "parent_code": "REMOTE-PARENT", "is_variant": True}]))
        self.assertEqual(preview["rows"][0]["status"], "ready")
        await self.apply(preview, [self.ids[0]])
        mapping = (await self.mappings())[0]
        self.assertEqual((mapping.product_id, mapping.variant_code, mapping.parent_code, mapping.is_variant),
                         (self.ids[0], "SHOP-ALIAS", "REMOTE-PARENT", True))
        self.assertEqual(await self.snapshot(), before)

    async def test_existing_exact_mapping_is_preserved_and_reported_as_already_linked(self):
        async with self.transaction() as db:
            db.add(ShopProduct(shop_id=self.shop_id, product_id=self.ids[0], external_code="SHARED-A",
                               external_id="101", shop_stock="123", shop_price="49.99", is_listed=True))
        preview = await self.preview(self.request(product_ids=[self.ids[0]]))
        self.assertEqual(preview["rows"][0]["status"], "already_linked")
        await self.apply(preview, [self.ids[0]])
        mapping = (await self.mappings())[0]
        self.assertEqual(str(mapping.shop_stock), "123.000")
        self.assertEqual(str(mapping.shop_price), "49.99")
        self.assertEqual(len(await self.mappings()), 1)

    async def test_apply_holds_shared_identity_lock_until_mapping_commit(self):
        preview = await self.preview(self.request(product_ids=[self.ids[0]]))
        request = ProductLinksApplyRequest(**{"preview_hash": preview["preview_hash"],
            "product_ids": [self.ids[0]], "confirmed": True})
        async with self.transaction() as writer:
            flush = writer.flush
            async def check_locked_flush(*args, **kwargs):
                await flush(*args, **kwargs)
                async with self.sessions() as competing:
                    available = await competing.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"),
                                                       {"key": IDENTITY_WRITE_LOCK})
                    self.assertFalse(available, "Catalog/receiving cannot interleave a competing identity write")
                    await competing.rollback()
            with patch.object(writer, "flush", side_effect=check_locked_flush) as checked_flush:
                await service.apply(writer, str(preview["id"]), request)
                checked_flush.assert_called_once()
        async with self.sessions() as competing:
            available = await competing.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"),
                                               {"key": IDENTITY_WRITE_LOCK})
            self.assertTrue(available)
            await competing.rollback()

    async def test_failed_retry_expires_preview_before_older_verification_can_apply(self):
        before = await self.snapshot()
        preview = await self.preview(self.request(product_ids=[self.ids[0]]))
        started, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def overlapping_verify(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                started.set()
                await release.wait()
                return await self.verify_remote(*args, **kwargs)
            raise service.source.SourceError("source_identity_changed", 409)

        self.verify.side_effect = overlapping_verify
        pending = asyncio.create_task(self.apply(preview, [self.ids[0]]))
        try:
            await asyncio.wait_for(started.wait(), timeout=10)
            with self.assertRaises(service.LinkError) as failed:
                await self.apply(preview, [self.ids[0]])
            self.assertTrue(getattr(failed.exception, "known_unapplied", False))
        finally:
            release.set()
            outcomes = await asyncio.wait_for(asyncio.gather(pending, return_exceptions=True), timeout=10)
        self.assertIsInstance(outcomes[0], service.LinkError)
        self.assertEqual(outcomes[0].code, "product_links_preview_expired")
        self.assertEqual(await self.mappings(), [])
        self.assertEqual(await self.snapshot(), before)
        async with self.sessions() as db:
            durable = await service.get(db, preview["id"])
        self.assertEqual(durable["status"], "prepared")
        self.assertIsNone(durable["result"])
        self.assertLessEqual(durable["expires_at"], service.now().isoformat())
        with self.assertRaises(service.LinkError):
            await self.apply(preview, [self.ids[0]])
        self.assertEqual(self.verify.await_count, 2, "An expired preview cannot retry source validation or create links")

    async def test_completed_apply_result_wins_over_concurrent_failed_retry_fence(self):
        preview = await self.preview(self.request(product_ids=[self.ids[0]]))
        started, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def overlapping_verify(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                started.set()
                await release.wait()
                raise service.source.SourceError("source_identity_changed", 409)
            return await self.verify_remote(*args, **kwargs)

        self.verify.side_effect = overlapping_verify
        pending = asyncio.create_task(self.apply(preview, [self.ids[0]]))
        try:
            await asyncio.wait_for(started.wait(), timeout=10)
            completed = await self.apply(preview, [self.ids[0]])
            self.assertEqual(completed["status"], "completed")
        finally:
            release.set()
            outcomes = await asyncio.wait_for(asyncio.gather(pending, return_exceptions=True), timeout=10)
        self.assertEqual(outcomes[0], completed, "The failed verification must recover the atomically committed result")
        self.assertEqual(len(await self.mappings()), 1)
        replay = await self.apply(preview, [self.ids[0]])
        self.assertEqual(replay, completed)
        self.assertEqual(self.verify.await_count, 2)
