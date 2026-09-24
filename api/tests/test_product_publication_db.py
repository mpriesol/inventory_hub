"""Durable publication fences and real editor identity corrections in isolated PostgreSQL."""
import copy
import os
import unittest
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch
from uuid import uuid4
from sqlalchemy import select
from inventory_hub.db_models import ProductIdentifier
from inventory_hub.product_editor_models import ProductEditorPublication
from inventory_hub.product_editor_types import PublicationPreviewRequest, PublicationResolveRequest
from inventory_hub.services import product_publication as service
from inventory_hub.services import product_publication_source as source
import test_product_editor_db as fixtures


@unittest.skipUnless(os.environ.get('CATALOG_TEST_DATABASE_URL'), 'Requires isolated localhost *_catalog_test database')
class PublicationDatabaseTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ProductEditorDatabaseTests.asyncSetUp
    asyncTearDown = fixtures.ProductEditorDatabaseTests.asyncTearDown
    transaction = fixtures.ProductEditorDatabaseTests.transaction
    detail = fixtures.ProductEditorDatabaseTests.detail
    change = fixtures.ProductEditorDatabaseTests.change
    save = fixtures.ProductEditorDatabaseTests.save

    def remote(self):
        return {'identity': {'code': 'BIKE-2', 'variant_code': 'BIKE-2', 'parent_code': 'xTrek', 'product_id': 11, 'variant_id': 22},
                'options': None, 'leaf': {'code': 'BIKE-2', 'active_yn': True}}

    async def prepared(self):
        row = await self.detail()
        await self.save([self.change(row, shops={'biketrek': {'visible': False}})])
        row = await self.detail()
        async with self.sessions() as db:
            return await service.preview(db, row['id'], PublicationPreviewRequest(shop_code='biketrek',
                expected_revision=row['revision'], fields=['visible']))

    def mocks(self, stack, remote, *, uncertain=False):
        stack.enter_context(patch.object(service, 'fingerprint', return_value='a' * 64))
        reader = stack.enter_context(patch.object(source, 'read_remote', AsyncMock(side_effect=lambda *a, **k: copy.deepcopy(remote))))
        async def write(*args):
            if uncertain:
                raise source.source.SourceError('product_publication_write_unconfirmed', uncertain=True)
            remote['leaf']['active_yn'] = args[3]['products'][0]['variants'][0]['active_yn']
        writer = stack.enter_context(patch.object(source, 'write_once', AsyncMock(side_effect=write)))
        return reader, writer

    async def test_success_is_recovered_without_repeated_put_and_receipts_follow_individual_values(self):
        remote = self.remote()
        with ExitStack() as stack:
            reader, writer = self.mocks(stack, remote)
            prepared = await self.prepared()
            async with self.sessions() as db:
                result = await service.send(db, prepared['id'])
            self.assertEqual(result['state'], 'completed')
            async with self.sessions() as db:
                self.assertEqual((await service.send(db, prepared['id']))['state'], 'completed')
            self.assertEqual(writer.await_count, 1)
            row = await self.detail()
            shop = next(s for s in row['shops'] if s['shop_code'] == 'biketrek')
            self.assertEqual(shop['state'], 'published')
            self.assertEqual(shop['published_fields'], ['visible'])
            await self.save([self.change(row, shops={'biketrek': {'visible': True}})])
            row = await self.detail()
            self.assertEqual(next(s for s in row['shops'] if s['shop_code'] == 'biketrek')['state'], 'saved_unpublished')

    async def test_ambiguous_write_fences_new_preview_until_explicit_settled_resolution(self):
        remote = self.remote()
        with ExitStack() as stack:
            reader, writer = self.mocks(stack, remote, uncertain=True)
            prepared = await self.prepared()
            async with self.sessions() as db:
                result = await service.send(db, prepared['id'])
            self.assertEqual(result['state'], 'uncertain')
            async with self.sessions() as db:
                await service.send(db, prepared['id'])
            self.assertEqual(writer.await_count, 1)
            row = await self.detail()
            async with self.sessions() as db:
                with self.assertRaisesRegex(service.ERROR, 'inflight'):
                    await service.preview(db, row['id'], PublicationPreviewRequest(shop_code='biketrek',
                        expected_revision=row['revision'], fields=['visible']))
            remote['leaf']['active_yn'] = False
            async with self.sessions() as db:
                result = await service.resolve(db, prepared['id'], PublicationResolveRequest(confirmed=True,
                    original_request_settled=True, note='Operator confirmed original request settled.'))
            self.assertEqual(result['state'], 'completed')
            self.assertEqual(writer.await_count, 1)

    async def test_local_edit_after_preview_rejects_without_put(self):
        with ExitStack() as stack:
            reader, writer = self.mocks(stack, self.remote())
            prepared = await self.prepared()
            row = await self.detail()
            await self.save([self.change(row, common={'name': 'Concurrent change'})])
            async with self.sessions() as db:
                with self.assertRaisesRegex(service.ERROR, 'product_editor_changed'):
                    await service.send(db, prepared['id'])
            writer.assert_not_awaited()

    async def test_barcode_edits_update_scanner_identity_and_audit_atomically_with_conflict_guard(self):
        row = await self.detail()
        result = await self.save([self.change(row, variant={'eans': ['4006381333931']})])
        self.assertEqual(result['results'][0]['status'], 'saved')
        row = await self.detail()
        self.assertEqual(row['eans'], ['4006381333931'])
        self.assertEqual(row['overrides']['variant']['eans'], ['4006381333931'])
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(ProductIdentifier.product_id).where(ProductIdentifier.value == '4006381333931')), row['id'])
        other = await self.detail('BIKE-10')
        conflict = await self.save([self.change(other, variant={'eans': ['4006381333931']})])
        self.assertEqual(conflict['results'][0]['errors'][0]['code'], 'product_editor_identity_conflict')
        renamed = await self.save([self.change(row, variant={'sku': 'NEW-CODE'})])
        self.assertEqual(renamed['results'][0]['errors'][0]['code'], 'product_editor_mapped_sku_rename')
