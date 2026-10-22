"""The retired one-off rename is read-only, including successful UUID recovery."""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from sqlalchemy import select, func

from inventory_hub.db_models import Product, IdentifierType, ProductIdentifier
from inventory_hub.db_models_ext import ShopProduct, StockMovement
from inventory_hub.product_editor_models import ProductEditorAudit, ProductEditorPublication, ProductEditorSave
from inventory_hub.services import zookee_code_reconciliation as service
from inventory_hub.services import product_publication_source, product_link_source
import test_product_publication_db as fixture


@unittest.skipUnless(os.environ.get('CATALOG_TEST_DATABASE_URL'), 'Requires isolated localhost *_catalog_test database')
class CodeReconciliationDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixture.PublicationDatabaseTests.asyncSetUp(self)
        self.pid = self.products['BIKE-2']
        self.request_id = uuid4()
        self.result = {'request_id': str(self.request_id), 'product_id': self.pid,
            'old_code': 'TOPEAK-1', 'new_code': 'ZOO-1', 'status': 'completed',
            'external_write_enabled': False, 'verified_shops': ['biketrek', 'xtrek']}
        async with self.sessions() as db:
            db.add(ProductEditorSave(request_id=str(self.request_id), input_hash=service._hash(self.pid), result=self.result))
            await db.flush()
            db.add(ProductEditorAudit(request_id=str(self.request_id), product_id=self.pid, revision=1,
                before_data={'sku': 'TOPEAK-1'}, after_data={'sku': 'ZOO-1', 'operation': 'verified_code_reconciliation'}))
            db.add(ProductIdentifier(product_id=self.pid, identifier_type=IdentifierType.internal_sku,
                value='TOPEAK-1', notes='Existing historical order alias'))
            db.add(Product(sku='TOPEAK-UNMIGRATED', name='Existing SKU must stay'))
            await db.commit()
        self.reader = self.enterContext(patch.object(product_publication_source, 'read_remote', AsyncMock()))
        self.discover = self.enterContext(patch.object(product_link_source, 'discover', AsyncMock()))

    transaction = fixture.PublicationDatabaseTests.transaction
    detail = fixture.PublicationDatabaseTests.detail
    change = fixture.PublicationDatabaseTests.change
    save = fixture.PublicationDatabaseTests.save

    async def asyncTearDown(self):
        self.reader.assert_not_awaited()
        self.discover.assert_not_awaited()
        await fixture.PublicationDatabaseTests.asyncTearDown(self)

    async def warehouse_snapshot(self):
        async with self.sessions() as db:
            return {
                'products': list((await db.execute(select(Product.id, Product.sku).order_by(Product.id))).all()),
                'mappings': list((await db.execute(select(ShopProduct.id, ShopProduct.external_code,
                    ShopProduct.external_product_id, ShopProduct.external_variant_id, ShopProduct.parent_code)
                    .order_by(ShopProduct.id))).all()),
                'aliases': list((await db.execute(select(ProductIdentifier.product_id, ProductIdentifier.value)
                    .order_by(ProductIdentifier.id))).all()),
                'movements': await db.scalar(select(func.count()).select_from(StockMovement)),
                'audits': await db.scalar(select(func.count()).select_from(ProductEditorAudit)),
            }

    async def test_fresh_request_is_gone_and_preserves_sku_aliases_mappings_and_ledger(self):
        before = await self.warehouse_snapshot()
        async with self.sessions() as db:
            with self.assertRaises(service.editor.EditorError) as failure:
                await service.reconcile_one(db, 'zookee', self.pid, uuid4())
            self.assertEqual((failure.exception.code, failure.exception.status), ('code_rename_retired', 410))
        self.assertEqual(await self.warehouse_snapshot(), before)

    async def test_history_lists_committed_results_without_new_rename_candidates(self):
        before = await self.warehouse_snapshot()
        async with self.sessions() as db:
            value = await service.preview(db, 'zookee')
            self.assertTrue(value['retired'])
            self.assertEqual(value['items'], [])
            self.assertEqual(len(value['results']), 1)
            self.assertEqual(value['results'][0]['request_id'], str(self.request_id))
            self.assertIsNotNone(value['results'][0]['completed_at'])
        self.assertEqual(await self.warehouse_snapshot(), before)

    async def test_successful_uuid_get_and_post_recover_without_prefix_configuration(self):
        before = await self.warehouse_snapshot()
        async with self.sessions() as db:
            self.assertEqual(await service.read_result(db, 'zookee', self.request_id), self.result)
            self.assertEqual(await service.reconcile_one(db, 'zookee', self.pid, self.request_id), self.result)
        self.assertEqual(await self.warehouse_snapshot(), before)

    async def test_unknown_result_is_missing_and_uuid_cannot_be_reused_for_another_card(self):
        async with self.sessions() as db:
            with self.assertRaises(service.editor.EditorError) as failure:
                await service.read_result(db, 'zookee', uuid4())
            self.assertEqual((failure.exception.code, failure.exception.status), ('code_rename_result_missing', 404))
            with self.assertRaisesRegex(service.editor.EditorError, 'product_editor_request_reused'):
                await service.reconcile_one(db, 'zookee', self.products['BIKE-10'], self.request_id)

    async def test_old_batch_returns_saved_rows_and_retires_every_fresh_row(self):
        before = await self.warehouse_snapshot()
        rows = [SimpleNamespace(product_id=self.pid, request_id=self.request_id),
                SimpleNamespace(product_id=self.products['BIKE-10'], request_id=uuid4())]
        async with self.sessions() as db:
            value = await service.reconcile_batch(db, 'zookee', rows)
            self.assertTrue(value['retired'])
            self.assertEqual(value['results'][0], self.result)
            self.assertEqual(value['results'][1]['error'], 'code_rename_retired')
        self.assertEqual(await self.warehouse_snapshot(), before)

    async def test_saved_recovery_leaves_uncertain_shop_publication_in_original_workflow(self):
        publication_id = str(uuid4())
        async with self.sessions() as db:
            db.add(ProductEditorPublication(id=publication_id, product_id=self.pid,
                shop_id=self.shops['biketrek'], state='uncertain', document={'identity': {'parent_code': 'xTrek'}}))
            await db.commit()
        async with self.sessions() as db:
            self.assertEqual(await service.read_result(db, 'zookee', self.request_id), self.result)
            self.assertEqual((await db.get(ProductEditorPublication, publication_id)).state, 'uncertain')

    async def test_ordinary_editor_save_is_not_exposed_as_rename_history(self):
        identifier = str(uuid4())
        async with self.sessions() as db:
            db.add(ProductEditorSave(request_id=identifier, input_hash='a' * 64,
                result={'product_id': self.pid, 'status': 'saved'}))
            await db.commit()
        async with self.sessions() as db:
            with self.assertRaisesRegex(service.editor.EditorError, 'code_rename_result_missing'):
                await service.read_result(db, 'zookee', identifier)
            self.assertEqual(len((await service.preview(db, 'zookee'))['results']), 1)
