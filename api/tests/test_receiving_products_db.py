"""Invoice -> shared product preparation; never changes receipt costs or stock."""
import json
import unittest
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import func, select, text

import test_receiving_db as fixture
from inventory_hub import config_io
from inventory_hub.catalog_types import CatalogProduct, CatalogPrices
from inventory_hub.db_models import FeedRunStatus, IdentifierType, Product, ProductIdentifier, Shop, Supplier, SupplierFeed, SupplierProduct
from inventory_hub.db_models_ext import ReceivingLine, ReceivingSession, StockBalance, StockMovement
from inventory_hub.product_editor_models import ProductEditorOverride
from inventory_hub.product_import_models import ProductImportDraft
from inventory_hub.product_import_types import DraftRevision, DraftPatch
from inventory_hub.routers import receiving_db as receiving
from inventory_hub.services import catalog_import, product_import
from inventory_hub.services import receiving_products as service
from inventory_hub.services.identifiers import ProductIdentifierService
from inventory_hub.stock_tracking_models import StockTracking
from inventory_hub.settings import settings


class InvoiceSourceTests(unittest.TestCase):
    def test_product_routes_require_operator_access_before_loading_receipt(self):
        app = FastAPI()
        app.include_router(receiving.router)
        with patch.object(settings, 'AI_CONTENT_ACCESS_TOKEN', SecretStr('fixture-token-at-least-24-characters')):
            with TestClient(app) as client:
                base = '/suppliers/test/receiving/sessions/1/products'
                for method, path, body in [('GET', base, None), ('POST', base + '/reconcile', {}),
                                          ('POST', base + '/prepare', {'line_ids': [1]})]:
                    response = client.request(method, path, json=body)
                    self.assertEqual(response.status_code, 401)
                    self.assertEqual(response.headers['Cache-Control'], 'no-store')

    def test_unknown_invoice_vat_stays_unknown_and_cost_rounding_is_only_for_card(self):
        line = SimpleNamespace(id=4, supplier_sku='0001', description='Invoice item', ean='',
                               unit_price=D('7.1234'), vat_rate=None)
        session = SimpleNamespace(id=8, supplier=SimpleNamespace(code='test'), invoice_currency='EUR',
                                  invoice_number='F-1', vat_rate=D('23'))
        source = service._invoice_source(session, line, 'TEST-', None, None)
        self.assertIsNone(source.prices.vat_percent)
        self.assertEqual(source.prices.purchase_net, D('7.12'))
        self.assertEqual(line.unit_price, D('7.1234'))
        self.assertEqual(source.shop_code, 'TEST-0001')
        self.assertEqual(source.feed_key, 'invoice')
        self.assertIsNone(source.supplier_stock)
        self.assertIsNone(source.availability)

    def test_invoice_without_supplier_code_keeps_blank_proposal_and_distinct_line_identity(self):
        session = SimpleNamespace(id=8, supplier=SimpleNamespace(code='test'), invoice_currency='EUR', invoice_number='F-1')
        first = SimpleNamespace(id=4, supplier_sku=None, description='First item', ean='', unit_price=None, vat_rate=None)
        second = SimpleNamespace(id=5, supplier_sku=None, description='Second item', ean='', unit_price=None, vat_rate=None)
        source = service._invoice_source(session, first, 'TEST-', None, None)
        self.assertEqual(source.code, '')
        self.assertEqual(source.shop_code, '')
        self.assertNotEqual(service._preparation_key(first, None, None), service._preparation_key(second, None, None))


@unittest.skipUnless(fixture.TEST_URL, 'Set CATALOG_TEST_DATABASE_URL to an isolated localhost *_catalog_test DB')
class ReceivingProductsDatabaseTests(unittest.IsolatedAsyncioTestCase):
    make_session = fixture.ReceivingDatabaseTests.make_session
    finalize = fixture.ReceivingDatabaseTests.finalize
    counts = fixture.ReceivingDatabaseTests.counts

    async def asyncSetUp(self):
        await fixture.ReceivingDatabaseTests.asyncSetUp(self)
        async with self.engine.begin() as connection:
            raw = await connection.get_raw_connection()
            root = Path(__file__).resolve().parents[2] / 'infra/db-init'
            for name in ('004_shop_product_content.sql', '005_ai_content.sql', '012_product_editor.sql',
                         '021_product_feed_mapping.sql', '022_product_import_drafts.sql', '023_ai_content_settings.sql', '029_sku_policy.sql'):
                await raw.driver_connection.execute((root / name).read_text())
        path = config_io.shop_path('biketrek')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'upgates_api_base_url': 'https://biketrek.example.test/api/v2',
                                   'upgates_login': 'fixture', 'upgates_api_key': 'fixture'}))
        self.options_patch = patch.object(catalog_import, 'cached_import_options', return_value={'categories': []})
        self.options_patch.start()
        async with self.sessions() as db:
            shop = await db.scalar(select(Shop).where(Shop.code == 'biketrek'))
            if shop is None:
                db.add(Shop(code='biketrek', name='BIKETREK', is_active=True))
            feed = SupplierFeed(supplier_id=self.supplier_id, code='products', name='Products', feed_type='products',
                                last_run_status=FeedRunStatus.completed, last_run_id=1)
            db.add(feed)
            await db.commit()
            self.feed_id = feed.id

    async def asyncTearDown(self):
        self.options_patch.stop()
        await fixture.ReceivingDatabaseTests.asyncTearDown(self)

    async def feed_item(self, code, ean, **changes):
        async with self.sessions() as db:
            source = CatalogProduct(supplier='receipt-test', code=code, shop_code='TEST-' + code,
                name='Feed ' + code, eans=[ean] if ean else [], description='Supplier description',
                prices=CatalogPrices(currency='EUR', vat_percent=D(23), purchase_net=D(99), retail_gross=D(149)),
                **changes)
            row = SupplierProduct(supplier_id=self.supplier_id, supplier_sku=code, ean=ean or None,
                name=source.name, source_feed_id=self.feed_id, attributes={'catalog': source.model_dump(mode='json')})
            db.add(row)
            await db.commit()
            return row.id

    async def inspect(self, session_id, *, reconcile=False):
        async with self.sessions() as db:
            session = await receiving._get_session_for_supplier(db, 'receipt-test', session_id, lock=reconcile)
            if reconcile:
                result = await service.reconcile_products(db, session, 'TEST-')
                await db.commit()
                return result
            return (await service.inspect_products(db, session, 'TEST-'))[0]

    async def prepare(self, session_id, ids):
        async with self.sessions() as db:
            session = await receiving._get_session_for_supplier(db, 'receipt-test', session_id, lock=True)
            result = await service.prepare_products(db, session, 'TEST-', ids)
            await db.commit()
            return result

    async def save(self, draft_id):
        async with self.sessions() as db:
            draft = await product_import.get_draft(db, draft_id, lock=True)
            # This synthetic fixture explicitly establishes that its source code
            # is a manufacturer leaf code; real invoice fields never imply it.
            patches = [{"id": row["id"], "values": {"brand": "Topeak", "sku_identity": {
                "brand": "Topeak", "source_code": row["values"]["supplier_code"],
                "code_kind": "manufacturer", "source_ref": "Synthetic fixture manufacturer leaf specification",
                "leaf_confirmed": True}}}
                for row in draft.document["rows"] if not row.get("hub_product_id") and not row["values"].get("sku_identity")]
            if patches:
                await product_import.patch_rows(db, draft, DraftPatch(expected_revision=draft.revision, rows=patches))
            result = await product_import.save(db, draft, DraftRevision(expected_revision=draft.revision))
            await db.commit()
            return result

    async def test_exact_feed_ean_finds_existing_card_and_keeps_unconfirmed_stock_unknown(self):
        await self.feed_item('new-alias', '5901234123457')
        await self.feed_item('only-feed', '4006381333931')
        async with self.sessions() as db:
            await ProductIdentifierService(db).add_identifier(self.product_id, '5901234123457')
            db.add(StockBalance(product_id=self.product_id, warehouse_id=self.warehouse_id, qty_on_hand=D(100)))
            basic = Product(sku='TEST-basic', name='Invoice title', created_from_source='invoice:old', validation_required=True)
            db.add(basic)
            await db.commit()
        session_id = await self.make_session([{'supplier_sku': 'new-alias'}, {'supplier_sku': 'only-feed'},
                                             {'supplier_sku': 'basic'}, {'supplier_sku': 'missing'}])
        result = await self.inspect(session_id)
        self.assertEqual([item['status'] for item in result['items']], ['matched', 'feed_only', 'basic_card', 'not_found'])
        first = result['items'][0]
        self.assertEqual(first['product_code'], 'RECEIPT-A')
        self.assertFalse(first['stock_confirmed'])
        self.assertIsNone(first['own_stock_qty'])
        async with self.sessions() as db:
            rows = list((await db.scalars(select(ReceivingLine).where(ReceivingLine.session_id == session_id))).all())
            self.assertTrue(all(row.product_id is None for row in rows))
            db.add(StockTracking(product_id=self.product_id, warehouse_id=self.warehouse_id, source_type='fixture',
                source_id='1', operator_name='fixture', archived_balance={}))
            await db.commit()
        result = await self.inspect(session_id, reconcile=True)
        self.assertEqual(result['items'][0]['own_stock_qty'], 100)
        async with self.sessions() as db:
            row = await db.scalar(select(ReceivingLine).where(ReceivingLine.session_id == session_id,
                                                             ReceivingLine.supplier_sku == 'new-alias'))
            self.assertEqual(row.product_id, self.product_id)
            self.assertEqual(await db.scalar(select(func.count()).select_from(StockMovement)), 0)

    async def test_failed_or_absent_feed_never_claims_product_missing_and_invoice_rows_are_not_feed(self):
        async with self.sessions() as db:
            feed = await db.get(SupplierFeed, self.feed_id)
            feed.last_run_status = FeedRunStatus.failed
            db.add(SupplierProduct(supplier_id=self.supplier_id, supplier_sku='minimal-source', name='Invoice source'))
            await db.commit()
        session_id = await self.make_session([{'supplier_sku': 'minimal-source', 'description': 'Manual item'}])
        result = await self.inspect(session_id)
        self.assertEqual(result['items'][0]['status'], 'feed_unverified')
        self.assertTrue(result['items'][0]['can_prepare'])
        self.assertIsNone(result['items'][0]['catalog_product_id'])
        async with self.sessions() as db:
            feed = await db.get(SupplierFeed, self.feed_id)
            feed.is_active = False
            await db.commit()
        self.assertFalse((await self.inspect(session_id))['feed_verified'])

    async def test_missing_supplier_codes_prepare_separately_and_require_explicit_identity_before_save(self):
        session_id = await self.make_session([{'description': 'First unnamed identity'}, {'description': 'Second unnamed identity'}])
        report = await self.inspect(session_id)
        self.assertTrue(all(item['can_prepare'] for item in report['items']))
        prepared = await self.prepare(session_id, [item['line_id'] for item in report['items']])
        async with self.sessions() as db:
            draft = await product_import.get_draft(db, prepared['draft_id'], lock=True)
            self.assertEqual(len(draft.document['rows']), 2)
            self.assertTrue(all(row['values']['supplier_code'] == '' for row in draft.document['rows']))
            self.assertTrue(all(not row['values'].get('sku_identity') for row in draft.document['rows']))
            with self.assertRaises(Exception) as error:
                await product_import.save(db, draft, DraftRevision(expected_revision=draft.revision))
            self.assertIn(getattr(error.exception, 'code', ''), ('sku_identity_required', 'import_invalid_cells', 'import_validation_required'))
            await db.rollback()
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(Product)), 1)
            self.assertEqual(await db.scalar(select(func.count()).select_from(StockMovement)), 0)

    async def test_repeated_invoice_rows_share_card_and_resume_without_changing_prices_or_stock(self):
        catalog_id = await self.feed_item('repeat', '5901234123457')
        session_id = await self.make_session([
            {'supplier_sku': 'repeat', 'received_qty': D(1), 'unit_price': D('7.1234')},
            {'supplier_sku': 'repeat', 'received_qty': D(1), 'unit_price': D(0)},
        ])
        report = await self.inspect(session_id)
        first, second = [item['line_id'] for item in report['items']]
        prepared = await self.prepare(session_id, [first])
        resumed = await self.prepare(session_id, [second])
        self.assertEqual(prepared['draft_id'], resumed['draft_id'])
        self.assertFalse(resumed['created'])
        async with self.sessions() as db:
            draft = await db.get(ProductImportDraft, prepared['draft_id'])
            self.assertEqual(len(draft.document['rows']), 1)
            self.assertEqual(draft.document['rows'][0]['catalog_product_id'], catalog_id)
            self.assertEqual(draft.document['receiving_rows'][str(first)]['line_ids'], [first, second])
            self.assertEqual(await db.scalar(select(func.count()).select_from(StockMovement)), 0)
        result = await self.save(prepared['draft_id'])
        product_id = result['rows'][0]['hub_product_id']
        await self.inspect(session_id, reconcile=True)
        async with self.sessions() as db:
            rows = list((await db.scalars(select(ReceivingLine).where(ReceivingLine.session_id == session_id)
                                         .order_by(ReceivingLine.id))).all())
            self.assertEqual([row.product_id for row in rows], [product_id, product_id])
            self.assertEqual([row.unit_price for row in rows], [D('7.1234'), D(0)])
            self.assertEqual([row.received_qty for row in rows], [D(1), D(1)])
            self.assertTrue(all(not row.ean for row in rows))
            self.assertEqual(await db.scalar(select(func.count()).select_from(StockBalance)), 0)
            self.assertEqual(await db.scalar(select(func.count()).select_from(StockMovement)), 0)
        async with self.sessions() as db:
            with self.assertRaises(HTTPException) as ambiguous:
                await receiving.scan_code('receipt-test', session_id,
                    receiving.ScanRequest(code='5901234123457', request_id=uuid4()), db)
            self.assertEqual(ambiguous.exception.detail['code'], 'scan_line_ambiguous')
            await db.rollback()
        async with self.sessions() as db:
            scan = await receiving.scan_code('receipt-test', session_id,
                receiving.ScanRequest(code='5901234123457', line_id=first, request_id=uuid4()), db)
            self.assertEqual(scan['line']['received_qty'], 2)
            await db.commit()
        finalized = await self.finalize(session_id, force=True)
        self.assertEqual(finalized, await self.finalize(session_id, force=True))
        self.assertEqual(finalized['movements_created'], 2)
        async with self.sessions() as db:
            movements = list((await db.scalars(select(StockMovement).where(StockMovement.product_id == product_id)
                                               .order_by(StockMovement.id))).all())
            self.assertEqual([row.unit_cost for row in movements], [D('7.1234'), D(0)])
            balance = await db.scalar(select(StockBalance).where(StockBalance.product_id == product_id))
            self.assertEqual(balance.qty_on_hand, D(3))

    async def test_manual_source_saves_without_fake_feed_offer_and_preserves_unknown_vat(self):
        session_id = await self.make_session([{'supplier_sku': 'manual-only', 'description': 'Invoice name',
                                               'unit_price': D('2.1234'), 'ean': '5901234123457'}])
        line_id = (await self.inspect(session_id))['items'][0]['line_id']
        prepared = await self.prepare(session_id, [line_id])
        async with self.sessions() as db:
            draft = await db.get(ProductImportDraft, prepared['draft_id'])
            row = draft.document['rows'][0]
            self.assertEqual(row['source']['feed_key'], 'invoice')
            self.assertIsNone(row['values']['vat_percent'])
            self.assertIsNone(row['catalog_product_id'])
        saved = await self.save(prepared['draft_id'])
        async with self.sessions() as db:
            product = await db.get(Product, saved['rows'][0]['hub_product_id'])
            self.assertIsNone(product.source_supplier_product_id)
            self.assertIsNone(await db.scalar(select(SupplierProduct.id).where(SupplierProduct.supplier_sku == 'manual-only')))
            self.assertEqual(await db.scalar(select(func.count()).select_from(StockMovement)), 0)

    async def test_conflict_rows_do_not_prevent_other_lines_reconciling(self):
        await self.feed_item('conflict', '5901234123457')
        await self.feed_item('good', '4006381333931')
        async with self.sessions() as db:
            await ProductIdentifierService(db).add_identifier(self.product_id, '4006381333931')
            await db.commit()
        session_id = await self.make_session([{'supplier_sku': 'conflict', 'ean': '9780201379624'},
                                             {'supplier_sku': 'good'},
                                             {'supplier_sku': 'duplicate', 'ean': '5901234123457'},
                                             {'supplier_sku': 'duplicate', 'ean': '9780201379624'}])
        result = await self.inspect(session_id, reconcile=True)
        self.assertEqual([row['status'] for row in result['items']], ['ambiguous', 'matched', 'ambiguous', 'ambiguous'])
        self.assertFalse(result['items'][0]['can_prepare'])
        self.assertIn('invoice_catalog_ean_conflict', result['items'][0]['conflict_reasons'])
        self.assertEqual(result['items'][0]['candidate_sources'][0]['code'], 'conflict')
        async with self.sessions() as db:
            rows = list((await db.scalars(select(ReceivingLine).where(ReceivingLine.session_id == session_id)
                                         .order_by(ReceivingLine.id))).all())
            self.assertEqual([row.product_id for row in rows], [None, self.product_id, None, None])
            self.assertEqual(await db.scalar(select(func.count()).select_from(StockMovement)), 0)

    async def test_alternative_barcodes_of_same_known_product_are_not_false_conflicts(self):
        await self.feed_item('known', '5901234123457')
        async with self.sessions() as db:
            identifiers = ProductIdentifierService(db)
            for ean in ('5901234123457', '4006381333931'):
                await identifiers.add_identifier(self.product_id, ean)
            await db.commit()
        session_id = await self.make_session([
            {'supplier_sku': 'known', 'ean': '4006381333931'},
            {'supplier_sku': 'known', 'ean': '5901234123457'},
        ])
        report = await self.inspect(session_id)
        self.assertEqual([row['status'] for row in report['items']], ['matched', 'matched'])
        self.assertEqual([row['product_id'] for row in report['items']], [self.product_id, self.product_id])

    async def test_ean_match_preserves_invoice_alias_without_creating_second_supplier_offer(self):
        catalog_id = await self.feed_item('feed-code', '5901234123457')
        session_id = await self.make_session([{'supplier_sku': 'invoice-code', 'ean': '5901234123457',
                                               'received_qty': D(1), 'unit_price': D('3.4321')}])
        report = await self.inspect(session_id)
        line_id = report['items'][0]['line_id']
        self.assertEqual(report['items'][0]['status'], 'feed_only')
        prepared = await self.prepare(session_id, [line_id])
        saved = await self.save(prepared['draft_id'])
        product_id = saved['rows'][0]['hub_product_id']
        # Saving/reconciling again must remain safe after the source link exists.
        await self.save(prepared['draft_id'])
        await self.inspect(session_id, reconcile=True)
        finalized = await self.finalize(session_id, force=True)
        self.assertEqual(finalized['movements_created'], 1)
        async with self.sessions() as db:
            product = await db.get(Product, product_id)
            self.assertEqual(product.sku, 'TP-feed-code')
            aliases = set((await db.scalars(select(ProductIdentifier.value).where(
                ProductIdentifier.product_id == product_id, ProductIdentifier.identifier_type == IdentifierType.supplier_sku))).all())
            self.assertEqual(aliases, {'feed-code', 'invoice-code'})
            line = await db.get(ReceivingLine, line_id)
            self.assertEqual(line.matched_supplier_product_id, catalog_id)
            self.assertEqual(line.unit_price, D('3.4321'))
            self.assertIsNone(await db.scalar(select(SupplierProduct.id).where(SupplierProduct.supplier_sku == 'invoice-code')))
