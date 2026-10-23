"""Invoice filters and header sorting remain correct across SQL pagination."""
from decimal import Decimal
import json
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import text

from invoice_db_fixture import DATABASE_REQUIRED, TEST_URL, InvoiceDatabaseCase
from inventory_hub.database import get_session
from inventory_hub.routers import invoices_unified as routes
from inventory_hub.settings import settings


@unittest.skipUnless(TEST_URL, DATABASE_REQUIRED)
class InvoiceListDatabaseTests(InvoiceDatabaseCase):
    async def client(self):
        app = FastAPI()
        app.include_router(routes.router)

        async def session():
            async with self.sessions() as db:
                yield db

        app.dependency_overrides[get_session] = session
        token = "invoice-list-test-" + "x" * 32
        self.enterContext(patch.object(settings, "AI_CONTENT_ACCESS_TOKEN", SecretStr(token)))
        client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                             headers={"Authorization": "Bearer " + token})
        self.addAsyncCleanup(client.aclose)
        return client

    async def document(self, *, supplier_id=None, number="DUPLICATE", total="100", gross="100", paid="0", shop=False):
        async with self.sessions() as db:
            invoice_id = await db.scalar(text("""INSERT INTO uploaded_invoices
                (supplier_id,shop_id,direction,original_filename,stored_filename,file_path,file_type,
                 invoice_number,currency,total_amount,total_with_vat,source_paid_amount)
                VALUES (:supplier,:shop,:direction,'synthetic.pdf',:filename,:path,'pdf',
                    :number,'EUR',:total,:gross,:paid) RETURNING id"""),
                {"supplier": None if shop else supplier_id or self.supplier_id,
                 "shop": self.shop_id if shop else None, "direction": "sale" if shop else "purchase",
                 "path": str(self.data_root / "synthetic.pdf"), "number": number, "filename": uuid4().hex + ".pdf",
                 "total": Decimal(total) if total is not None else None,
                 "gross": Decimal(gross) if gross is not None else None,
                 "paid": Decimal(paid) if paid is not None else None})
            await db.commit()
            return invoice_id

    async def test_first_scan_upload_resolves_configured_supplier_and_preserves_original_only(self):
        folder = self.data_root / "suppliers/configured-only"
        folder.mkdir(parents=True)
        (folder / "config.json").write_text(json.dumps({"name": "Configured-only supplier"}), encoding="utf-8")
        self.assertEqual(await self.scalar("SELECT count(*) FROM suppliers WHERE code='configured-only'"), 0)
        client = await self.client()
        original = b"\x89PNG\r\n\x1a\nSynthetic scan original"
        result = await client.post('/invoices/upload', data={"supplier_code": "configured-only", "invoice_number": "SCAN-1"},
            files={"file": ("invoice-scan.png", original, "image/png")})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(await self.scalar("SELECT count(*) FROM suppliers WHERE code='configured-only'"), 1)
        row = (await self.rows("SELECT supplier_id,total_amount,total_with_vat,currency,is_parsed FROM uploaded_invoices"))[0]
        self.assertIsNone(row['total_amount'])
        self.assertIsNone(row['total_with_vat'])
        self.assertIsNone(row['currency'])
        self.assertFalse(row['is_parsed'])
        path = await self.scalar("SELECT file_path FROM invoice_files")
        from pathlib import Path
        self.assertEqual(Path(path).read_bytes(), original)
        self.assertEqual(await self.scalar("SELECT count(*) FROM uploaded_invoice_lines"), 0)
        self.assertEqual(await self.scalar("SELECT count(*) FROM products"), 1, "The existing fixture card is the only card")
        await self.assert_no_inventory_effects()

    async def test_exact_supplier_filter_stable_pages_and_inactive_archive_options(self):
        async with self.sessions() as db:
            similar = await db.scalar(text("INSERT INTO suppliers(code,name) VALUES ('archive-fixture-sibling','Similar supplier') RETURNING id"))
            await db.execute(text("UPDATE suppliers SET is_active=false WHERE id=:id"), {"id": self.other_supplier_id})
            await db.execute(text("INSERT INTO suppliers(code,name,is_active) VALUES ('inactive-empty','Inactive empty',false)"))
            await db.commit()
        first = await self.document(number="SAME")
        second = await self.document(number="SAME")
        await self.document(supplier_id=similar, number="SAME")
        await self.document(supplier_id=self.other_supplier_id, number="SAME")
        client = await self.client()
        options = await client.get('/invoices/supplier-options')
        self.assertEqual(options.status_code, 200, options.text)
        codes = {row['code'] for row in options.json()}
        self.assertIn('archive-other', codes)
        self.assertIn('archive-fixture', codes)
        self.assertNotIn('inactive-empty', codes)
        for page, expected in ((1, first), (2, second)):
            result = await client.get('/invoices', params={"supplier_code": "archive-fixture", "sort_by": "invoice_number", "sort_order": "asc", "page_size": 1, "page": page})
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()['total'], 2)
            self.assertEqual([row['id'] for row in result.json()['items']], [expected])
        inactive = await client.get('/invoices', params={"supplier_code": "archive-other"})
        self.assertEqual(inactive.status_code, 200, inactive.text)
        self.assertEqual(inactive.json()['total'], 1)
        self.assertEqual(inactive.json()['items'][0]['supplier_code'], 'archive-other')
        await self.assert_no_inventory_effects()

    async def test_money_and_payment_header_sort_matches_finance_snapshot_before_pagination(self):
        high_gross_low_rest = await self.document(number="HIGH", total="1", gross="100", paid="90")
        low_gross_high_rest = await self.document(number="LOW", total="999", gross="20", paid="0")
        unknown = await self.document(number="UNKNOWN", total=None, gross=None, paid=None)
        client = await self.client()
        cases = [
            ('total_amount', 'asc', [low_gross_high_rest, high_gross_low_rest, unknown]),
            ('total_amount', 'desc', [high_gross_low_rest, low_gross_high_rest, unknown]),
            ('outstanding_amount', 'asc', [high_gross_low_rest, low_gross_high_rest, unknown]),
            ('outstanding_amount', 'desc', [low_gross_high_rest, high_gross_low_rest, unknown]),
            ('payment_status', 'asc', [low_gross_high_rest, high_gross_low_rest, unknown]),
        ]
        for sort, order, expected in cases:
            found = []
            for page in (1, 2, 3):
                result = await client.get('/invoices', params={"sort_by": sort, "sort_order": order, "page_size": 1, "page": page})
                self.assertEqual(result.status_code, 200, result.text)
                found.extend(row['id'] for row in result.json()['items'])
            self.assertEqual(found, expected, (sort, order))
        await self.assert_no_inventory_effects()

    async def test_sales_source_text_filter_uses_shop_owner(self):
        purchase = await self.document(number="PURCHASE")
        sale = await self.document(number="SALE", shop=True)
        client = await self.client()
        result = await client.get('/invoices', params={"direction": "sale", "f_supplier": "archive-shop"})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual([row['id'] for row in result.json()['items']], [sale])
        self.assertNotIn(purchase, [row['id'] for row in result.json()['items']])
        await self.assert_no_inventory_effects()
