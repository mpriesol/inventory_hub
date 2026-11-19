"""Mixed receipts use real isolated PostgreSQL, FIFO and invoice reservations."""
import asyncio
from datetime import date
from decimal import Decimal as D
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import uuid4

import asyncpg
import httpx
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError
from sqlalchemy import select, text

import test_receiving_db as fixture
from test_zookee import pdf, isdoc
from test_northfinder_invoice import invoice_pages as northfinder_pages
from inventory_hub.adapters import northfinder_invoice
from inventory_hub.adapters.sk_profi_bike_invoice import save_receiving_invoice
from inventory_hub.adapters.zookee_invoice import parse_invoice_pdf
from inventory_hub.access import operator_access
from inventory_hub.database import get_session
from inventory_hub import config_io
from inventory_hub.db_models import IdentifierType, MovementType, Product, ReceivingStatus, Supplier, SupplierProduct
from inventory_hub.db_models_ext import ReceivingLine, ReceivingSession, StockBalance, StockMovement
from inventory_hub.fifo_models import FifoLayer
from inventory_hub.receiving_operation_types import ReceivingOperationApply, ReceivingOperationPreview
from inventory_hub.mixed_receiving_types import BatchScan, BatchFinalize, BatchManualScan
from inventory_hub.routers import mixed_receiving as routes
from inventory_hub.routers import receiving_db as receiving
from inventory_hub.services import fifo, receiving_operations, invoice_processing, invoice_upload, mixed_receiving
from inventory_hub.services.identifiers import ProductIdentifierService


class MixedReceivingInputTests(unittest.TestCase):
    def test_manual_exception_requires_confirmation_documented_reason_and_nullable_cost(self):
        payload = {'request_id': uuid4(), 'code': '4006381333931', 'confirmed_without_invoice': True,
                   'reason': 'Invoice not available'}
        self.assertIsNone(BatchManualScan(**payload).unit_cost)
        for change in ({'confirmed_without_invoice': False}, {'confirmed_without_invoice': 1},
                       {'reason': '  '}, {'unit_cost': '0'}, {'cost_status': 'known'},
                       {'unit_cost': '-1', 'cost_status': 'known'}, {'product_id': 99}):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                BatchManualScan(**{**payload, **change})

    def test_final_confirmation_is_explicit_true_and_does_not_accept_extra_flags(self):
        payload = {"request_id": uuid4(), "preview_hash": "a" * 64, "confirmed": True}
        BatchFinalize(**payload)
        for change in ({"confirmed": 1}, {"confirmed": False}, {"confirmed": "true"},
                       {"ignore_identity": True}, {"preview_hash": "not-a-preview"}):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                BatchFinalize(**{**payload, **change})

    def test_scans_require_unique_operation_identity_and_exact_positive_quantity(self):
        payload = {"request_id": uuid4(), "code": "4006381333931", "qty": "1"}
        BatchScan(**payload)
        for change in ({"request_id": "EAN-4006381333931"}, {"qty": "0"}, {"qty": "-1"},
                       {"qty": "NaN"}, {"qty": "0.0001"}, {"code": " "}, {"line_id": 0},
                       {"received": True}):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                BatchScan(**{**payload, **change})


@unittest.skipUnless(fixture.TEST_URL, "Set CATALOG_TEST_DATABASE_URL to an isolated localhost *_catalog_test DB")
class MixedReceivingDatabaseTests(unittest.IsolatedAsyncioTestCase):
    make_session = fixture.ReceivingDatabaseTests.make_session
    legacy_finalize = fixture.ReceivingDatabaseTests.finalize

    async def asyncSetUp(self):
        await fixture.ReceivingDatabaseTests.asyncSetUp(self)
        connection = await asyncpg.connect(fixture.TEST_URL)
        try:
            await connection.execute(f'SET search_path TO "{self.schema}"')
            root = Path(__file__).resolve().parents[2] / "infra" / "db-init"
            for filename in ("015_stock_sync.sql", "025_stock_native.sql", "026_stock_delta.sql",
                             "046_shared_stock_sales.sql", "036_stock_sync_dirty.sql", "054_mixed_receiving.sql",
                             "056_receiving_manual.sql"):
                await connection.execute((root / filename).read_text())
        finally:
            await connection.close()
        async with self.sessions() as db:
            await ProductIdentifierService(db).add_identifier(self.product_id, "4006381333931")
            await db.commit()
        self.source_root = patch.object(receiving.settings, "INVENTORY_DATA_ROOT", Path(self.config_folder.name))
        self.source_root.start()
        app = FastAPI()
        app.include_router(routes.router)

        async def session():
            async with self.sessions() as db:
                try:
                    yield db
                    await db.commit()
                except Exception:
                    await db.rollback()
                    raise

        app.dependency_overrides[get_session] = session
        app.dependency_overrides[operator_access] = lambda: None
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        self.source_root.stop()
        await fixture.ReceivingDatabaseTests.asyncTearDown(self)

    async def request(self, method, path, payload=None, expected=200):
        response = await self.client.request(method, path, json=payload)
        self.assertEqual(response.status_code, expected, response.text)
        return response.json()

    async def invoice(self, ordered="3", received="0", price="5", invoice_date="2026-11-10", supplier_id=None, **extra):
        identifier = await self.make_session([{"product_id": self.product_id, "supplier_sku": "ITEM",
            "ean": "4006381333931", "ordered_qty": D(ordered), "received_qty": D(received),
            "unit_price": D(price) if price is not None else None, **extra}], supplier_id=supplier_id)
        async with self.sessions() as db:
            receipt = await db.get(ReceivingSession, identifier)
            receipt.invoice_date = date.fromisoformat(invoice_date) if invoice_date else None
            await db.commit()
            return identifier, receipt.invoice_number

    async def batch(self):
        return await self.request("POST", "/receiving/batches", {
            "request_id": str(uuid4()), "warehouse_code": "receipt-test"})

    async def scan(self, batch_id, qty="1", request_id=None, expected=200, **choice):
        return await self.request("POST", f"/receiving/batches/{batch_id}/scan", {
            "request_id": str(request_id or uuid4()), "code": "4006381333931", "qty": qty, **choice}, expected=expected)

    async def confirm(self, batch_id, request_id=None, preview_hash=None, expected=200):
        if preview_hash is None:
            preview = await self.request("POST", f"/receiving/batches/{batch_id}/preview", {})
            preview_hash = preview["preview_hash"]
        payload = {"request_id": str(request_id or uuid4()), "preview_hash": preview_hash, "confirmed": True}
        return await self.request("POST", f"/receiving/batches/{batch_id}/finalize", payload, expected=expected), payload

    async def balance(self):
        async with self.sessions() as db:
            row = await db.scalar(select(StockBalance).where(StockBalance.product_id == self.product_id,
                StockBalance.warehouse_id == self.warehouse_id))
            return (row.qty_on_hand, row.total_value) if row else None

    async def receipt(self, identifier):
        async with self.sessions() as db:
            row = await db.get(ReceivingSession, identifier)
            lines = list(await db.scalars(select(ReceivingLine).where(ReceivingLine.session_id == identifier)
                .order_by(ReceivingLine.line_number)))
            return row.status, [line.received_qty for line in lines]

    async def movements(self):
        async with self.sessions() as db:
            return list((await db.execute(text("SELECT * FROM stock_movements ORDER BY id"))).mappings())

    async def test_two_partial_batches_post_only_one_then_two_and_close_when_complete(self):
        receipt_id, _ = await self.invoice()
        first = await self.batch()
        await self.scan(first["id"])
        self.assertIsNone(await self.balance(), "A staged scan is not a stock receipt")
        self.assertEqual((await self.receipt(receipt_id))[1], [D(0)])
        await self.confirm(first["id"])
        self.assertEqual(await self.balance(), (D(1), D(5)))
        self.assertEqual(await self.receipt(receipt_id), (ReceivingStatus.in_progress, [D(1)]))

        second = await self.batch()
        await self.scan(second["id"], qty="2")
        result, payload = await self.confirm(second["id"])
        replay = await self.request("POST", f'/receiving/batches/{second["id"]}/finalize', payload)
        self.assertEqual(replay["result"], result["result"])
        self.assertEqual(await self.balance(), (D(3), D(15)))
        self.assertEqual(await self.receipt(receipt_id), (ReceivingStatus.completed, [D(3)]))
        self.assertEqual([row["quantity"] for row in await self.movements()], [D(1), D(2)])
        async with self.sessions() as db:
            self.assertEqual(list(await db.scalars(select(FifoLayer.quantity_original).order_by(FifoLayer.id))), [D(1), D(2)])

    async def test_completed_partial_legacy_invoice_receives_only_the_remainder(self):
        receipt_id, _ = await self.invoice(received="1")
        await self.legacy_finalize(receipt_id, force=True)
        original = await self.movements()
        batch = await self.batch()
        await self.scan(batch["id"], qty="2")
        self.assertEqual(await self.movements(), original)
        await self.confirm(batch["id"])
        movements = await self.movements()
        self.assertEqual(movements[:len(original)], original)
        self.assertEqual([row["quantity"] for row in movements], [D(1), D(2)])
        self.assertEqual(await self.receipt(receipt_id), (ReceivingStatus.completed, [D(3)]))
        self.assertEqual(await self.balance(), (D(3), D(15)))

    async def test_legacy_unposted_scans_block_staging_until_the_original_draft_is_reset(self):
        receipt_id, _ = await self.invoice(received="1")
        batch = await self.batch()
        rejected = await self.scan(batch["id"], expected=409)
        self.assertEqual(rejected["detail"]["code"], "mixed_receiving_legacy_pending")
        summary = await self.request("GET", f'/receiving/batches/{batch["id"]}')
        self.assertEqual(summary["scans"], [])
        self.assertEqual((await self.receipt(receipt_id))[1], [D(1)])
        self.assertIsNone(await self.balance())
        async with self.sessions() as db:
            await receiving.reset_all_items("receipt-test", receipt_id, db)
            await db.commit()
        await self.scan(batch["id"])
        await self.confirm(batch["id"])
        self.assertEqual(await self.balance(), (D(1), D(5)))
        self.assertEqual([row["quantity"] for row in await self.movements()], [D(1)])
        self.assertEqual((await self.receipt(receipt_id))[1], [D(1)])
        self.assertNotEqual((await self.receipt(receipt_id))[0], ReceivingStatus.completed)

    async def test_first_scan_of_a_real_parsed_original_creates_and_selects_the_correct_csv_line(self):
        root = Path(self.config_folder.name)
        config = root / "suppliers/zookee/config.json"
        config.parent.mkdir(parents=True)
        config.write_text('{"product_code_prefix":"ZOO-"}')
        async with self.sessions() as db:
            db.add(Supplier(code="zookee", name="Synthetic Zookee"))
            await ProductIdentifierService(db).add_identifier(self.product_id, "5901234123457")
            await db.commit()
        saved = save_receiving_invoice(pdf(isdoc()), root, "zookee", parser=parse_invoice_pdf)
        self.assertTrue((root / saved["csv_path"]).is_file())
        async with self.sessions() as db:
            self.assertIsNone(await db.scalar(select(ReceivingSession.id)))
        batch = await self.batch()
        scanned = await self.request("POST", f'/receiving/batches/{batch["id"]}/scan', {
            "request_id": str(uuid4()), "code": "5901234123457", "qty": "1"})
        scan = scanned["summary"]["scans"][0]
        self.assertEqual((scan["supplier"], scan["invoice_no"], scan["line_number"]), ("zookee", "9990001", 1))
        self.assertEqual(D(scan["unit_price"]), D(10))
        self.assertIsNone(await self.balance())
        await self.confirm(batch["id"])
        self.assertEqual(await self.balance(), (D(1), D(10)))

    async def test_northfinder_pdf_scan_and_fifo_use_line_discount_and_exclude_separate_charges(self):
        config = Path(self.config_folder.name) / 'suppliers/northfinder/config.json'
        config.parent.mkdir(parents=True)
        config.write_text('{"product_code_prefix":"NF-"}')
        invoice = northfinder_invoice.parse_invoice_pages(northfinder_pages())
        raw = b'%PDF-1.7\nSynthetic Northfinder invoice\n%%EOF'
        with patch.object(northfinder_invoice, 'parse_invoice_pdf', return_value=invoice):
            async with self.sessions() as db:
                supplier = Supplier(code='northfinder', name='Northfinder fixture')
                db.add(supplier)
                await db.flush()
                code = invoice.receiving_lines[0].sku
                db.add(SupplierProduct(supplier_id=supplier.id, supplier_sku=code,
                    ean='4006381333931', name='Synthetic receipt item'))
                await ProductIdentifierService(db).add_identifier(self.product_id, code, IdentifierType.supplier_sku, supplier.id)
                await db.commit()
                saved = await invoice_upload.upload(db, supplier_code='northfinder', filename='invoice.pdf', content=raw)
                self.assertTrue(saved['processing']['receiving_ready'])
            self.assertIsNone(await self.balance())
            batch = await self.batch()
            scanned = await self.scan(batch['id'], qty='2')
            scan = scanned['summary']['scans'][0]
            self.assertEqual((scan['supplier'], scan['invoice_no'], D(scan['unit_price'])),
                             ('northfinder', invoice.number, D('18')))
            self.assertIsNone(await self.balance())
            await self.confirm(batch['id'])
            self.assertEqual(await self.balance(), (D(2), D(36)))
            self.assertEqual(len(await self.movements()), 1)
            async with self.sessions() as db:
                self.assertEqual(list(await db.scalars(select(ReceivingLine.supplier_sku).order_by(ReceivingLine.line_number))),
                                 [line.sku for line in invoice.receiving_lines])

    async def test_scan_uuid_replay_and_simultaneous_replay_reserve_one_unit(self):
        await self.invoice()
        batch = await self.batch()
        identifier = uuid4()
        responses = await asyncio.gather(self.scan(batch["id"], request_id=identifier),
            self.scan(batch["id"], request_id=identifier))
        self.assertEqual({row["scan_id"] for row in responses}, {str(identifier)})
        summary = await self.request("GET", f'/receiving/batches/{batch["id"]}')
        self.assertEqual(len([row for row in summary["scans"] if row["status"] == "active"]), 1)
        self.assertEqual(D(summary["total_qty"]), D(1))
        await self.scan(batch["id"], qty="2", request_id=identifier, expected=409)
        await self.confirm(batch["id"])
        replay_after_completion = await self.scan(batch["id"], request_id=identifier)
        self.assertTrue(replay_after_completion["replayed"])
        self.assertEqual(await self.balance(), (D(1), D(5)))

    async def test_simultaneous_batches_cannot_reserve_the_same_last_unit(self):
        await self.invoice(ordered="1")
        first, second = await self.batch(), await self.batch()
        async def raw_scan(batch):
            return await self.client.post(f'/receiving/batches/{batch["id"]}/scan', json={
                "request_id": str(uuid4()), "code": "4006381333931", "qty": "1"})
        results = await asyncio.gather(raw_scan(first), raw_scan(second))
        self.assertEqual(sorted(row.status_code for row in results), [200, 409])
        summaries = [await self.request("GET", f'/receiving/batches/{batch["id"]}') for batch in (first, second)]
        self.assertEqual(sum(D(row["total_qty"]) for row in summaries), D(1))
        self.assertIsNone(await self.balance())

    async def test_newest_invoice_is_selected_and_reassignment_moves_only_the_reservation(self):
        older_id, older_number = await self.invoice(invoice_date="2026-11-09")
        newer_id, _ = await self.invoice(invoice_date="2026-11-10")
        batch = await self.batch()
        scan = await self.scan(batch["id"])
        active = next(row for row in scan["summary"]["scans"] if row["status"] == "active")
        self.assertEqual(active["session_id"], newer_id)
        payload = {"request_id": str(uuid4()), "supplier": "receipt-test", "invoice_no": older_number}
        moved = await self.request("POST", f'/receiving/batches/{batch["id"]}/scans/{scan["scan_id"]}/reassign', payload)
        replay = await self.request("POST", f'/receiving/batches/{batch["id"]}/scans/{scan["scan_id"]}/reassign', payload)
        self.assertEqual(moved["scan_id"], replay["scan_id"])
        await self.confirm(batch["id"])
        self.assertEqual((await self.receipt(older_id))[1], [D(1)])
        self.assertEqual((await self.receipt(newer_id))[1], [D(0)])
        self.assertEqual(await self.balance(), (D(1), D(5)))

    async def test_cancel_releases_reservation_and_replay_does_not_cancel_a_later_scan(self):
        await self.invoice(ordered="1")
        batch = await self.batch()
        scan = await self.scan(batch["id"])
        payload = {"request_id": str(uuid4())}
        path = f'/receiving/batches/{batch["id"]}/scans/{scan["scan_id"]}/cancel'
        await self.request("POST", path, payload)
        await self.scan(batch["id"])
        await self.request("POST", path, payload)
        summary = await self.request("GET", f'/receiving/batches/{batch["id"]}')
        self.assertEqual(D(summary["total_qty"]), D(1))
        await self.confirm(batch["id"])
        self.assertEqual(await self.balance(), (D(1), D(5)))

    async def test_missing_or_tied_dates_require_explicit_invoice_choice(self):
        first_id, first_no = await self.invoice(invoice_date=None)
        await self.invoice(invoice_date="2026-11-10")
        batch = await self.batch()
        request_id = uuid4()
        error = await self.scan(batch["id"], request_id=request_id, expected=409)
        self.assertEqual(error["detail"]["code"], "mixed_receiving_invoice_choice")
        chosen = await self.scan(batch["id"], request_id=request_id, supplier="receipt-test", invoice_no=first_no)
        self.assertEqual(chosen["summary"]["scans"][0]["session_id"], first_id)

    async def test_equal_document_dates_do_not_invent_an_invoice_order(self):
        await self.invoice(invoice_date="2026-11-10")
        await self.invoice(invoice_date="2026-11-10")
        batch = await self.batch()
        error = await self.scan(batch["id"], expected=409)
        self.assertEqual(error["detail"]["code"], "mixed_receiving_invoice_choice")
        self.assertEqual(len(error["detail"]["candidates"]), 2)
        self.assertEqual(await self.movements(), [])

    async def test_unreadable_unrelated_csv_requires_choice_but_allows_a_verified_invoice(self):
        _, number = await self.invoice(ordered="1")
        broken = Path(self.config_folder.name) / "suppliers/receipt-test/invoices/csv/broken.csv"
        broken.parent.mkdir(parents=True)
        broken.write_text("This is not a valid receiving CSV\n")
        batch = await self.batch()
        identifier = uuid4()
        response = await self.scan(batch["id"], request_id=identifier, expected=409)
        self.assertEqual(response["detail"]["code"], "mixed_receiving_invoice_choice")
        self.assertEqual(response["detail"]["unreadable_invoices"], 1)
        self.assertEqual(len(response["detail"]["candidates"]), 1)
        await self.scan(batch["id"], request_id=identifier, supplier="receipt-test", invoice_no=number)
        await self.confirm(batch["id"])
        self.assertEqual(await self.balance(), (D(1), D(5)))

    async def test_one_batch_handles_two_suppliers_without_combining_their_invoices(self):
        first_id, first_no = await self.invoice(ordered="1")
        async with self.sessions() as db:
            supplier = Supplier(code="mixed-other", name="Other receiving supplier")
            db.add(supplier)
            await db.commit()
            second_supplier_id = supplier.id
        path = config_io.supplier_path("mixed-other")
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"product_code_prefix": "OTHER-"}))
        second_id, second_no = await self.invoice(ordered="2", supplier_id=second_supplier_id,
            invoice_date="2026-11-11", price="7")
        batch = await self.batch()
        await self.scan(batch["id"], supplier="receipt-test", invoice_no=first_no)
        await self.scan(batch["id"], supplier="mixed-other", invoice_no=second_no)
        await self.confirm(batch["id"])
        self.assertEqual(await self.balance(), (D(2), D(12)))
        self.assertEqual(await self.receipt(first_id), (ReceivingStatus.completed, [D(1)]))
        self.assertEqual(await self.receipt(second_id), (ReceivingStatus.in_progress, [D(1)]))
        self.assertEqual({row["reference_source"] for row in await self.movements()},
            {f"receipt-test:{first_no}", f"mixed-other:{second_no}"})

    async def test_explicit_purchase_factor_keeps_invoice_units_and_fifo_piece_cost(self):
        receipt_id, _ = await self.invoice(ordered="2", price="6", purchase_unit="pack",
            purchase_to_stock_factor=D(3), conversion_stock_unit="pcs")
        batch = await self.batch()
        await self.scan(batch["id"])
        await self.confirm(batch["id"])
        self.assertEqual(await self.balance(), (D(3), D(6)))
        self.assertEqual((await self.receipt(receipt_id))[1], [D(1)])
        movement = (await self.movements())[0]
        self.assertEqual((movement["quantity"], movement["unit_cost"]), (D(3), D(2)))

    async def test_explicit_service_line_does_not_block_autoclose_or_create_physical_stock(self):
        async with self.sessions() as db:
            shipping = Product(sku="MIXED-SHIPPING", name="Shipping service", item_type="service")
            db.add(shipping)
            await db.commit()
            shipping_id = shipping.id
        receipt_id = await self.make_session([
            {"product_id": self.product_id, "ean": "4006381333931", "ordered_qty": D(1), "unit_price": D(5)},
            {"product_id": shipping_id, "supplier_sku": "SHIPPING", "description": "Shipping service",
             "ordered_qty": D(1), "unit_price": D(4)},
        ])
        batch = await self.batch()
        scanned = await self.scan(batch["id"])
        invoice = scanned["summary"]["invoices"][0]
        self.assertTrue(invoice["complete_after"])
        self.assertEqual(D(invoice["service_qty"]), D(1))
        await self.confirm(batch["id"])
        self.assertEqual(await self.receipt(receipt_id), (ReceivingStatus.completed, [D(1), D(0)]))
        self.assertEqual(await self.balance(), (D(1), D(5)))
        movements = await self.movements()
        self.assertEqual([(row["product_id"], row["quantity"]) for row in movements], [(self.product_id, D(1))])
        async with self.sessions() as db:
            self.assertIsNone(await db.scalar(select(StockBalance).where(StockBalance.product_id == shipping_id)))
            receipt = await db.get(ReceivingSession, receipt_id)
            self.assertEqual(receipt.session_data["finalize_result"]["skipped_lines"], [
                {"line_number": 2, "product_id": shipping_id, "reason": "service_outside_stock"}])

    async def test_unknown_line_is_not_assumed_to_be_service_when_deciding_autoclose(self):
        receipt_id = await self.make_session([
            {"product_id": self.product_id, "ean": "4006381333931", "ordered_qty": D(1), "unit_price": D(5)},
            {"description": "Shipping or another unclassified item", "supplier_sku": "UNKNOWN",
             "ordered_qty": D(1), "unit_price": D(4)},
        ])
        batch = await self.batch()
        scanned = await self.scan(batch["id"])
        invoice = scanned["summary"]["invoices"][0]
        self.assertFalse(invoice["complete_after"])
        self.assertEqual(D(invoice["service_qty"]), D(0))
        await self.confirm(batch["id"])
        self.assertEqual(await self.receipt(receipt_id), (ReceivingStatus.in_progress, [D(1), D(0)]))
        self.assertEqual(await self.balance(), (D(1), D(5)))

    async def test_archive_and_manually_processed_invoice_cannot_be_silently_received(self):
        receipt_id, number = await self.invoice(ordered="1")
        async with self.sessions() as db:
            await db.execute(text('''INSERT INTO uploaded_invoices
                (supplier_id,original_filename,stored_filename,file_path,invoice_number,
                 receiving_session_id,processing_status)
                VALUES(:supplier,'synthetic.csv','synthetic.csv','synthetic.csv',:number,:receipt,'processed_without_stock')'''),
                {"supplier": self.supplier_id, "number": number, "receipt": receipt_id})
            await db.commit()
        batch = await self.batch()
        await self.scan(batch["id"], supplier="receipt-test", invoice_no=number, expected=409)
        async with self.sessions() as db:
            await db.execute(text("UPDATE uploaded_invoices SET processing_status='unprocessed',archived_at=now() WHERE receiving_session_id=:id"),
                {"id": receipt_id})
            await db.commit()
        await self.scan(batch["id"], supplier="receipt-test", invoice_no=number, expected=409)
        self.assertEqual(await self.movements(), [])
        self.assertEqual((await self.receipt(receipt_id))[1], [D(0)])

    async def test_latest_source_archive_event_blocks_scan_until_an_explicit_restore(self):
        receipt_id, number = await self.invoice(ordered="1")
        batch = await self.batch()
        for action in ("archive", "restore"):
            async with self.sessions() as db:
                await db.execute(text('''INSERT INTO invoice_source_archive_events
                    (id,supplier_id,source_key,action,note,actor,request_hash)
                    VALUES(:id,:supplier,:source,:action,'Synthetic source lifecycle','test',:hash)'''),
                    {"id": uuid4(), "supplier": self.supplier_id, "source": number, "action": action, "hash": "a" * 64})
                await db.commit()
            if action == "archive":
                blocked = await self.scan(batch["id"], supplier="receipt-test", invoice_no=number, expected=409)
                self.assertEqual(blocked["detail"]["code"], "invoice_source_archived")
                self.assertEqual((await self.receipt(receipt_id))[1], [D(0)])
                self.assertEqual(await self.movements(), [])
            else:
                await self.scan(batch["id"], supplier="receipt-test", invoice_no=number)
        await self.confirm(batch["id"])
        self.assertEqual(await self.balance(), (D(1), D(5)))

    async def test_invoice_processing_stays_receiving_after_partial_post_and_received_after_complete(self):
        receipt_id, number = await self.invoice(ordered="2")
        async with self.sessions() as db:
            invoice_id = await db.scalar(text('''INSERT INTO uploaded_invoices
                (supplier_id,original_filename,stored_filename,file_path,invoice_number,receiving_session_id)
                VALUES(:supplier,'state.csv','state.csv','state.csv',:number,:receipt) RETURNING id'''),
                {"supplier": self.supplier_id, "number": number, "receipt": receipt_id})
            await db.commit()
        for expected_status in ("receiving", "received"):
            batch = await self.batch()
            await self.scan(batch["id"])
            await self.confirm(batch["id"])
            async with self.sessions() as db:
                row = (await db.execute(text('SELECT * FROM uploaded_invoices WHERE id=:id'), {"id": invoice_id})).mappings().one()
                state = await invoice_processing.detail(db, row)
            self.assertEqual(state["status"], expected_status)

    async def test_only_completed_batch_has_trusted_atomer_export_scope(self):
        await self.invoice(ordered="1")
        batch = await self.batch()
        await self.scan(batch["id"])
        async with self.sessions() as db:
            with self.assertRaises(HTTPException) as error:
                await mixed_receiving.get_export_scope(db, batch["id"])
            self.assertEqual(error.exception.status_code, 409)
        await self.confirm(batch["id"])
        async with self.sessions() as db:
            scope = await mixed_receiving.get_export_scope(db, batch["id"])
        self.assertEqual(scope, {"product_ids": [self.product_id], "warehouse_code": "receipt-test"})

    async def test_repeated_product_lines_keep_different_invoice_costs_and_need_line_choice(self):
        receipt_id = await self.make_session([
            {"product_id": self.product_id, "ean": "4006381333931", "ordered_qty": D(1), "unit_price": D(5)},
            {"product_id": self.product_id, "ean": "4006381333931", "ordered_qty": D(1), "unit_price": D(0)},
        ])
        async with self.sessions() as db:
            receipt = await db.get(ReceivingSession, receipt_id)
            line_ids = list(await db.scalars(select(ReceivingLine.id).where(ReceivingLine.session_id == receipt_id)
                .order_by(ReceivingLine.line_number)))
            invoice_number = receipt.invoice_number
        batch = await self.batch()
        failed = await self.scan(batch["id"], expected=409)
        self.assertEqual(failed["detail"]["code"], "mixed_receiving_line_choice")
        await self.scan(batch["id"], supplier="receipt-test", invoice_no=invoice_number, line_id=line_ids[1])
        await self.confirm(batch["id"])
        self.assertEqual(await self.balance(), (D(1), D(0)))

    async def test_new_scan_invalidates_preview_without_posting_any_stock(self):
        await self.invoice()
        batch = await self.batch()
        await self.scan(batch["id"])
        preview = await self.request("POST", f'/receiving/batches/{batch["id"]}/preview', {})
        await self.scan(batch["id"])
        await self.confirm(batch["id"], preview_hash=preview["preview_hash"], expected=409)
        self.assertEqual(await self.movements(), [])
        self.assertIsNone(await self.balance())

    async def test_validation_failure_rolls_back_every_invoice_in_batch(self):
        first_id, first_no = await self.invoice()
        second_id, second_no = await self.invoice(invoice_date="2026-11-11")
        batch = await self.batch()
        await self.scan(batch["id"], supplier="receipt-test", invoice_no=first_no)
        await self.scan(batch["id"], supplier="receipt-test", invoice_no=second_no)
        preview = await self.request("POST", f'/receiving/batches/{batch["id"]}/preview', {})
        async with self.sessions() as db:
            line = await db.scalar(select(ReceivingLine).where(ReceivingLine.session_id == second_id))
            line.unit_price = None
            await db.commit()
        await self.confirm(batch["id"], preview_hash=preview["preview_hash"], expected=409)
        self.assertEqual(await self.movements(), [])
        self.assertEqual((await self.receipt(first_id))[1], [D(0)])
        self.assertEqual((await self.receipt(second_id))[1], [D(0)])

    async def test_mid_posting_failure_rolls_back_first_invoice_and_retries_same_request(self):
        first_id, first_no = await self.invoice(ordered="1")
        second_id, second_no = await self.invoice(ordered="1", invoice_date="2026-11-11")
        batch = await self.batch()
        await self.scan(batch["id"], supplier="receipt-test", invoice_no=first_no)
        await self.scan(batch["id"], supplier="receipt-test", invoice_no=second_no)
        preview = await self.request("POST", f'/receiving/batches/{batch["id"]}/preview', {})
        payload = {"request_id": str(uuid4()), "preview_hash": preview["preview_hash"], "confirmed": True}
        original_writer = mixed_receiving.write_stock_for_line
        calls = 0

        async def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("Synthetic failure after the first invoice was written")
            return await original_writer(*args, **kwargs)

        with patch.object(mixed_receiving, "write_stock_for_line", fail_second):
            with self.assertRaisesRegex(RuntimeError, "Synthetic failure"):
                await self.client.post(f'/receiving/batches/{batch["id"]}/finalize', json=payload)
        self.assertEqual(calls, 2)
        self.assertEqual(await self.movements(), [])
        self.assertIsNone(await self.balance())
        self.assertEqual((await self.receipt(first_id))[1], [D(0)])
        self.assertEqual((await self.receipt(second_id))[1], [D(0)])
        await self.request("POST", f'/receiving/batches/{batch["id"]}/finalize', payload)
        self.assertEqual(await self.balance(), (D(2), D(10)))

    async def test_simultaneous_finalization_with_same_uuid_posts_one_batch(self):
        await self.invoice(ordered="1")
        batch = await self.batch()
        await self.scan(batch["id"])
        preview = await self.request("POST", f'/receiving/batches/{batch["id"]}/preview', {})
        payload = {"request_id": str(uuid4()), "preview_hash": preview["preview_hash"], "confirmed": True}
        path = f'/receiving/batches/{batch["id"]}/finalize'
        first, second = await asyncio.gather(self.request("POST", path, payload), self.request("POST", path, payload))
        self.assertEqual(first["result"], second["result"])
        self.assertEqual(len(await self.movements()), 1)
        self.assertEqual(await self.balance(), (D(1), D(5)))

    async def test_pending_batch_prevents_classic_quantity_reset_and_duplicate_finalization(self):
        receipt_id, _ = await self.invoice()
        batch = await self.batch()
        await self.scan(batch["id"])
        async with self.sessions() as db:
            with self.assertRaises(HTTPException) as error:
                await receiving.reset_all_items("receipt-test", receipt_id, db)
            self.assertEqual(error.exception.detail["code"], "mixed_receiving_required")
            await db.rollback()
        await self.confirm(batch["id"])
        async with self.sessions() as db:
            with self.assertRaises(HTTPException) as error:
                await receiving.finalize_session("receipt-test", receipt_id, receiving.FinalizeRequest(force=True), db)
            self.assertEqual(error.exception.detail["code"], "mixed_receiving_required")
            await db.rollback()
        self.assertEqual(await self.balance(), (D(1), D(5)))

    async def test_recovery_reverses_all_postings_and_consumed_layers_block_it(self):
        receipt_id, _ = await self.invoice()
        for qty in ("1", "2"):
            batch = await self.batch()
            await self.scan(batch["id"], qty=qty)
            await self.confirm(batch["id"])
        async with self.sessions() as db:
            preview = await receiving_operations.preview(db, "receipt-test", receipt_id, ReceivingOperationPreview(
                request_id=uuid4(), action="reverse", reason="Verify the mixed receipt reversal"), "test")
        self.assertTrue(preview["ready"], preview)
        self.assertEqual(D(preview["impacts"][0]["delta"]), D(-3))
        async with self.sessions() as db:
            balance = await db.scalar(select(StockBalance).where(StockBalance.product_id == self.product_id,
                StockBalance.warehouse_id == self.warehouse_id))
            plan = await fifo.plan_issue(db, balance, [{"line_key": "mixed-sale", "quantity": D(1)}], lock=True)
            movement = StockMovement(idempotency_key="mixed-test:issue", product_id=self.product_id,
                warehouse_id=self.warehouse_id, movement_type=MovementType.SALE_OUT, quantity=D(-1))
            await fifo.apply_issue(db, balance, movement, plan["allocations"])
            await db.commit()
        async with self.sessions() as db:
            blocked = await receiving_operations.preview(db, "receipt-test", receipt_id, ReceivingOperationPreview(
                request_id=uuid4(), action="reverse", reason="Used pieces must never be silently reversed"), "test")
        self.assertFalse(blocked["ready"])
        self.assertIn("receiving_reversal_used_stock", str(blocked["errors"]))

    async def test_reopen_compensates_both_mixed_postings_once_and_preserves_history(self):
        receipt_id, _ = await self.invoice()
        for qty in ("1", "2"):
            batch = await self.batch()
            await self.scan(batch["id"], qty=qty)
            await self.confirm(batch["id"])
        original = await self.movements()
        async with self.sessions() as db:
            preview = await receiving_operations.preview(db, "receipt-test", receipt_id, ReceivingOperationPreview(
                request_id=uuid4(), action="reopen", reason="Review the received units"), "test")
        self.assertTrue(preview["ready"], preview)
        payload = ReceivingOperationApply(preview_hash=preview["preview_hash"], confirmed=True)
        async with self.sessions() as db:
            result = await receiving_operations.apply(db, "receipt-test", receipt_id, preview["id"], payload, "test")
        async with self.sessions() as db:
            replay = await receiving_operations.apply(db, "receipt-test", receipt_id, preview["id"], payload, "test")
        self.assertEqual(result, replay)
        self.assertEqual((result["status"], result["posting_revision"]), ("paused", 1))
        movements = await self.movements()
        self.assertEqual(movements[:len(original)], original)
        self.assertEqual([row["quantity"] for row in movements], [D(1), D(2), D(-1), D(-2)])
        self.assertEqual(await self.balance(), (D(0), D(0)))

    async def test_new_mixed_posting_after_reopen_and_reset_clears_reversed_invoice_state(self):
        receipt_id, number = await self.invoice(ordered="2")
        async with self.sessions() as db:
            invoice_id = await db.scalar(text('''INSERT INTO uploaded_invoices
                (supplier_id,original_filename,stored_filename,file_path,invoice_number,receiving_session_id)
                VALUES(:supplier,'reposted.csv','reposted.csv','reposted.csv',:number,:receipt) RETURNING id'''),
                {"supplier": self.supplier_id, "number": number, "receipt": receipt_id})
            await db.commit()
        first = await self.batch()
        await self.scan(first["id"], qty="2")
        await self.confirm(first["id"])
        async with self.sessions() as db:
            preview = await receiving_operations.preview(db, "receipt-test", receipt_id, ReceivingOperationPreview(
                request_id=uuid4(), action="reopen", reason="Correct a completed mixed receipt"), "test")
        async with self.sessions() as db:
            await receiving_operations.apply(db, "receipt-test", receipt_id, preview["id"],
                ReceivingOperationApply(preview_hash=preview["preview_hash"], confirmed=True), "test")
        original_history = await self.movements()
        self.assertEqual([row["quantity"] for row in original_history], [D(2), D(-2)])
        async with self.sessions() as db:
            await receiving.resume_session("receipt-test", receipt_id, db)
            await db.commit()
        async with self.sessions() as db:
            await receiving.reset_all_items("receipt-test", receipt_id, db)
            await db.commit()
        second = await self.batch()
        await self.scan(second["id"], qty="2")
        await self.confirm(second["id"])
        self.assertEqual(await self.balance(), (D(2), D(10)))
        history = await self.movements()
        self.assertEqual(history[:len(original_history)], original_history)
        self.assertEqual([row["quantity"] for row in history], [D(2), D(-2), D(2)])
        async with self.sessions() as db:
            receipt = await db.get(ReceivingSession, receipt_id)
            self.assertNotIn("receiving_reversed", receipt.session_data)
            self.assertEqual(receiving_operations.posting_revision(receipt), 1)
            row = (await db.execute(text('SELECT * FROM uploaded_invoices WHERE id=:id'), {"id": invoice_id})).mappings().one()
            state = await invoice_processing.detail(db, row)
        self.assertEqual(state["status"], "received")
        self.assertFalse(state["receipts"][0]["reversed"])

    async def manual_scan(self, batch_id, **changes):
        payload = {'request_id': str(uuid4()), 'code': '4006381333931', 'qty': '1',
                   'confirmed_without_invoice': True, 'reason': 'Invoice not available', **changes}
        return await self.request('POST', f'/receiving/batches/{batch_id}/manual-scan', payload), payload

    async def test_manual_receipt_uses_existing_identity_and_nullable_fifo_cost_once(self):
        batch = await self.batch()
        product = await self.request('GET', f'/receiving/batches/{batch["id"]}/manual-product?code=4006381333931')
        self.assertEqual(product['product_id'], self.product_id)
        staged, payload = await self.manual_scan(batch['id'])
        self.assertEqual(staged['summary']['items'][0]['source_kind'], 'manual')
        self.assertIsNone(await self.balance())
        replay = await self.request('POST', f'/receiving/batches/{batch["id"]}/manual-scan', payload)
        self.assertTrue(replay['replayed'])
        self.assertEqual(D(replay['summary']['total_qty']), D(1))
        result, confirmation = await self.confirm(batch['id'])
        replay = await self.request('POST', f'/receiving/batches/{batch["id"]}/finalize', confirmation)
        self.assertEqual(result['result'], replay['result'])
        self.assertEqual(await self.balance(), (D(1), None))
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(text('SELECT count(*) FROM receiving_sessions')), 0)
            layer = await db.scalar(select(FifoLayer))
            self.assertEqual(layer.cost_status, 'unknown')
            self.assertIsNone(layer.unit_cost)
            self.assertEqual(layer.provenance['source_reference'], 'Invoice not available')
        self.assertEqual(len(await self.movements()), 1)

    async def test_manual_and_invoice_receipt_post_atomically_in_same_batch(self):
        receipt_id, _ = await self.invoice(ordered='1')
        batch = await self.batch()
        await self.scan(batch['id'])
        staged, _ = await self.manual_scan(batch['id'], qty='2', unit_cost='4.5000', cost_status='known')
        self.assertEqual({D(item['stock_qty_after']) for item in staged['summary']['items']}, {D(3)})
        result, _ = await self.confirm(batch['id'])
        self.assertEqual(result['result']['product_ids'], [self.product_id])
        self.assertEqual(await self.balance(), (D(3), D(14)))
        self.assertEqual(await self.receipt(receipt_id), (ReceivingStatus.completed, [D(1)]))
        self.assertEqual([row['quantity'] for row in await self.movements()], [D(1), D(2)])

    async def test_manual_cancel_preserves_operation_replay_and_allows_archive_restore(self):
        batch = await self.batch()
        staged, payload = await self.manual_scan(batch['id'])
        archive = {'request_id': str(uuid4()), 'reason': 'Remove empty test draft', 'empty_only': True}
        failure = await self.request('POST', f'/receiving/batches/{batch["id"]}/archive', archive, expected=409)
        self.assertEqual(failure['detail']['code'], 'mixed_receiving_not_empty')
        await self.request('POST', f'/receiving/batches/{batch["id"]}/scans/{staged["scan_id"]}/cancel',
                           {'request_id': str(uuid4())})
        result = await self.request('POST', f'/receiving/batches/{batch["id"]}/archive', archive)
        self.assertTrue(result['archived'])
        self.assertEqual((await self.request('GET', '/receiving/batches'))['batches'], [])
        self.assertTrue((await self.request('POST', f'/receiving/batches/{batch["id"]}/archive', archive))['replayed'])
        replay = await self.request('POST', f'/receiving/batches/{batch["id"]}/manual-scan', payload)
        self.assertTrue(replay['replayed'])
        self.assertEqual(replay['summary']['total_qty'], '0')
        self.assertTrue(replay['summary']['archived'])
        await self.request('POST', f'/receiving/batches/{batch["id"]}/manual-scan',
                           {**payload, 'request_id': str(uuid4())}, expected=409)
        restored = await self.request('POST', f'/receiving/batches/{batch["id"]}/restore',
                           {**archive, 'request_id': str(uuid4())})
        self.assertFalse(restored['archived'])
        self.assertEqual(len((await self.request('GET', '/receiving/batches'))['batches']), 1)
        self.assertEqual(await self.movements(), [])

    async def test_manual_identity_change_blocks_whole_mixed_batch(self):
        receipt_id, _ = await self.invoice(ordered='1')
        batch = await self.batch()
        await self.scan(batch['id'])
        await self.manual_scan(batch['id'])
        async with self.sessions() as db:
            product = await db.get(Product, self.product_id)
            product.is_active = False
            await db.commit()
        blocked, _ = await self.confirm(batch['id'], expected=409)
        self.assertEqual(blocked['detail']['code'], 'mixed_receiving_blocked')
        self.assertEqual(await self.movements(), [])
        self.assertEqual((await self.receipt(receipt_id))[1], [D(0)])

    async def test_completed_batch_archival_does_not_change_stock_or_invoices(self):
        receipt_id, _ = await self.invoice(ordered='1')
        batch = await self.batch()
        await self.scan(batch['id'])
        await self.confirm(batch['id'])
        original = await self.movements()
        await self.request('POST', f'/receiving/batches/{batch["id"]}/archive',
                           {'request_id': str(uuid4()), 'reason': 'Hide historical receipt', 'empty_only': False})
        self.assertEqual((await self.request('GET', '/receiving/batches?status=completed'))['batches'], [])
        self.assertEqual(await self.movements(), original)
        self.assertEqual(await self.balance(), (D(1), D(5)))
        self.assertEqual(await self.receipt(receipt_id), (ReceivingStatus.completed, [D(1)]))

    async def test_manual_post_failure_rolls_back_invoice_and_retries_original_confirmation(self):
        receipt_id, _ = await self.invoice(ordered='1')
        batch = await self.batch()
        await self.scan(batch['id'])
        await self.manual_scan(batch['id'])
        preview = await self.request('POST', f'/receiving/batches/{batch["id"]}/preview', {})
        payload = {'request_id': str(uuid4()), 'preview_hash': preview['preview_hash'], 'confirmed': True}
        with patch.object(fifo, 'write_documented_receipt', side_effect=fifo.FifoError('fifo_cutover_required')):
            await self.request('POST', f'/receiving/batches/{batch["id"]}/finalize', payload, expected=409)
        self.assertEqual(await self.movements(), [])
        self.assertIsNone(await self.balance())
        self.assertEqual((await self.receipt(receipt_id))[1], [D(0)])
        result = await self.request('POST', f'/receiving/batches/{batch["id"]}/finalize', payload)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(len(await self.movements()), 2)
        self.assertEqual(await self.balance(), (D(2), None))

    async def test_concurrent_manual_scans_with_one_uuid_create_one_physical_receipt(self):
        batch = await self.batch()
        payload = {'request_id': str(uuid4()), 'code': '4006381333931', 'qty': '1',
                   'confirmed_without_invoice': True, 'reason': 'Invoice not available'}
        responses = await asyncio.gather(*[self.request('POST',
            f'/receiving/batches/{batch["id"]}/manual-scan', payload) for _ in range(2)])
        self.assertEqual(sorted(row['replayed'] for row in responses), [False, True])
        self.assertEqual([D(row['summary']['total_qty']) for row in responses], [D(1), D(1)])
        await self.confirm(batch['id'])
        self.assertEqual(len(await self.movements()), 1)

    async def test_inactive_warehouse_blocks_new_writes_but_preserves_completed_replay(self):
        first = await self.batch()
        await self.manual_scan(first['id'])
        completed, confirmation = await self.confirm(first['id'])
        pending = await self.batch()
        await self.manual_scan(pending['id'])
        async with self.sessions() as db:
            await db.execute(text('UPDATE warehouses SET is_active=false WHERE id=:id'), {'id': self.warehouse_id})
            await db.commit()
        error = await self.request('POST', '/receiving/batches',
            {'request_id': str(uuid4()), 'warehouse_code': 'receipt-test'}, expected=409)
        self.assertEqual(error['detail']['code'], 'mixed_receiving_warehouse_missing')
        error, _ = await self.confirm(pending['id'], expected=409)
        self.assertEqual(error['detail']['code'], 'mixed_receiving_warehouse_missing')
        replay = await self.request('POST', f'/receiving/batches/{first["id"]}/finalize', confirmation)
        self.assertEqual(replay['result'], completed['result'])
        self.assertEqual(len(await self.movements()), 1)
