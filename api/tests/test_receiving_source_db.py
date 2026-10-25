"""Real PostgreSQL races, legacy source validation and immutable receipt replay."""
import asyncio
import json
from decimal import Decimal as D
from pathlib import Path
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import select, func

import test_receiving_db as fixture
from test_zookee import pdf, isdoc
from inventory_hub.adapters.sk_profi_bike_invoice import save_receiving_invoice
from inventory_hub.adapters.zookee_invoice import parse_invoice_pdf
from inventory_hub.db_models import Supplier, ProductIdentifier, IdentifierType, ReceivingStatus
from inventory_hub.db_models_ext import ReceivingSession, ReceivingLine, StockMovement
from inventory_hub.routers import receiving_db as receiving


@unittest.skipUnless(fixture.TEST_URL, "Set CATALOG_TEST_DATABASE_URL to an isolated localhost *_catalog_test DB")
class ReceivingSourceDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixture.ReceivingDatabaseTests.asyncSetUp(self)
        self.root = Path(self.config_folder.name)
        self.root_patch = patch.object(receiving.settings, "INVENTORY_DATA_ROOT", self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        config = self.root / "suppliers/zookee/config.json"
        config.parent.mkdir(parents=True)
        config.write_text('{"product_code_prefix":"ZOO-"}')
        async with self.sessions() as db:
            supplier = Supplier(code="zookee", name="Synthetic Zookee")
            db.add(supplier)
            db.add(ProductIdentifier(product_id=self.product_id, value="5901234123457",
                                     identifier_type=IdentifierType.ean, is_primary=True))
            await db.commit()
            self.zookee_id = supplier.id
        saved = save_receiving_invoice(pdf(isdoc()), self.root, "zookee", parser=parse_invoice_pdf)
        self.path = self.root / saved["csv_path"]
        self.index_path = self.root / "suppliers/zookee/invoices/index.latest.json"

    asyncTearDown = fixture.ReceivingDatabaseTests.asyncTearDown

    async def create(self, invoice="9990001"):
        async with self.sessions() as db:
            try:
                result = await receiving.create_session("zookee", receiving.CreateSessionRequest(
                    invoice_id=invoice, warehouse_code="receipt-test"), db)
                await db.commit()
                return result
            except BaseException:
                await db.rollback()
                raise

    def alias(self):
        alias = self.path.with_name("renamed.csv")
        alias.write_bytes(self.path.read_bytes())
        index = json.loads(self.index_path.read_text())
        index["zookee:renamed"] = {**index["zookee:9990001"], "csv_path": alias.relative_to(self.root).as_posix()}
        self.index_path.write_text(json.dumps(index))

    async def count(self, model):
        async with self.sessions() as db:
            return await db.scalar(select(func.count()).select_from(model))

    async def test_renamed_document_and_concurrent_creation_leave_one_receipt(self):
        self.alias()
        results = await asyncio.gather(self.create(), self.create("renamed"), return_exceptions=True)
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1, results)
        error = next(result for result in results if isinstance(result, HTTPException))
        self.assertEqual(error.detail["code"], "receiving_source_duplicate")
        self.assertIn("session_id", error.detail)
        self.assertEqual(await self.count(ReceivingSession), 1)
        self.assertEqual(await self.count(StockMovement), 0)

    async def test_changed_source_does_not_replace_existing_rows(self):
        created = await self.create()
        self.path.write_text(self.path.read_text().replace("10.0000", "11.0000"))
        with self.assertRaises(HTTPException) as raised:
            await self.create()
        self.assertEqual(raised.exception.detail["code"], "receiving_source_changed")
        async with self.sessions() as db:
            row = await db.scalar(select(ReceivingLine).where(ReceivingLine.session_id == created["session_id"]))
            self.assertEqual(row.unit_price, D(10))
        self.assertEqual(await self.count(StockMovement), 0)

    async def test_distinct_real_invoices_with_identical_rows_create_two_receipts(self):
        first = await self.create()
        saved = save_receiving_invoice(pdf(isdoc().replace(b"9990001", b"9990002")), self.root,
                                      "zookee", parser=parse_invoice_pdf)
        self.assertEqual((self.root / saved["csv_path"]).read_bytes(), self.path.read_bytes())
        second = await self.create("9990002")
        self.assertNotEqual(first["session_id"], second["session_id"])
        self.assertEqual(await self.count(ReceivingSession), 2)
        self.assertEqual(await self.count(StockMovement), 0)

    async def test_null_hash_legacy_receipt_blocks_alias_without_rewriting_history(self):
        created = await self.create()
        async with self.sessions() as db:
            receipt = await db.get(ReceivingSession, created["session_id"])
            receipt.source_hash, receipt.session_data = None, {}
            await db.commit()
        self.alias()
        with self.assertRaises(HTTPException) as raised:
            await self.create("renamed")
        self.assertEqual(raised.exception.detail["code"], "receiving_source_duplicate")
        async with self.sessions() as db:
            self.assertIsNone((await db.get(ReceivingSession, created["session_id"])).source_hash)
        self.assertEqual(await self.count(StockMovement), 0)

    async def test_legacy_finalize_verifies_source_and_completed_replay_needs_no_file(self):
        created = await self.create()
        identifier = created["session_id"]
        async with self.sessions() as db:
            receipt = await db.get(ReceivingSession, identifier)
            receipt.source_hash, receipt.session_data = None, {}
            receipt.status = ReceivingStatus.in_progress
            line = await db.scalar(select(ReceivingLine).where(ReceivingLine.session_id == identifier))
            line.received_qty, line.status = D(3), "matched"
            await db.commit()
        async with self.sessions() as db:
            result = await receiving.finalize_session("zookee", identifier, receiving.FinalizeRequest(), db)
        self.path.unlink()
        async with self.sessions() as db:
            replay = await receiving.finalize_session("zookee", identifier, receiving.FinalizeRequest(), db)
        self.assertEqual(result, replay)
        self.assertEqual(await self.count(StockMovement), 1)

    async def test_unverified_old_active_receipt_cannot_finalize(self):
        created = await self.create()
        identifier = created["session_id"]
        async with self.sessions() as db:
            receipt = await db.get(ReceivingSession, identifier)
            receipt.source_hash, receipt.session_data, receipt.invoice_file_path = None, {}, None
            line = await db.scalar(select(ReceivingLine).where(ReceivingLine.session_id == identifier))
            line.received_qty, line.status = D(3), "matched"
            await db.commit()
        async with self.sessions() as db:
            with self.assertRaises(HTTPException) as raised:
                await receiving.finalize_session("zookee", identifier, receiving.FinalizeRequest(), db)
            await db.rollback()
        self.assertEqual(raised.exception.detail["code"], "receiving_source_unverified")
        self.assertEqual(await self.count(StockMovement), 0)
