"""Discard settles prepared reviews under the same fences as apply."""
import asyncio
from copy import deepcopy
from datetime import timedelta
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from sqlalchemy import text

import test_product_consolidation_db as fixture
from inventory_hub.product_consolidation_models import ProductConsolidation
from inventory_hub.product_consolidation_types import ProductConsolidationApplyRequest
from inventory_hub.product_editor_models import ProductEditorOverride
from inventory_hub.services import product_consolidation as service
from inventory_hub.services import product_links as links


@unittest.skipUnless(fixture.TEST_URL, 'Dedicated localhost *_catalog_test database required')
class DiscardDatabaseTests(unittest.IsolatedAsyncioTestCase):
    transaction = fixture.ProductConsolidationDatabaseTests.transaction
    leaf = fixture.ProductConsolidationDatabaseTests.leaf
    discover = fixture.ProductConsolidationDatabaseTests.discover
    verify = fixture.ProductConsolidationDatabaseTests.verify
    absence = fixture.ProductConsolidationDatabaseTests.absence
    code_absence = fixture.ProductConsolidationDatabaseTests.code_absence
    request = fixture.ProductConsolidationDatabaseTests.request
    preview = fixture.ProductConsolidationDatabaseTests.preview
    apply = fixture.ProductConsolidationDatabaseTests.apply
    stock = fixture.ProductConsolidationDatabaseTests.stock

    async def asyncSetUp(self):
        await fixture.ProductConsolidationDatabaseTests.asyncSetUp(self)

    async def asyncTearDown(self):
        await fixture.ProductConsolidationDatabaseTests.asyncTearDown(self)

    async def discard(self, preview):
        async with self.transaction() as db:
            return await service.discard(db, preview['id'], ProductConsolidationApplyRequest(
                preview_hash=preview['preview_hash'], confirmed=True))

    async def identity_hashes(self):
        async with self.transaction() as db:
            return {table: await db.scalar(text(f'''SELECT md5(coalesce(
                string_agg(to_jsonb(t)::text,E'\\n' ORDER BY to_jsonb(t)::text),'')) FROM {table} t'''))
                for table in ('products', 'product_identifiers', 'product_variant_attributes', 'shop_products',
                    'product_canonical_aliases', 'stock_balances', 'stock_movements', 'stock_tracking',
                    'fifo_layers', 'fifo_states')}

    async def frozen(self, identifier):
        async with self.transaction() as db:
            row = await db.get(ProductConsolidation, identifier)
            return {'request_hash': row.request_hash, 'preview_hash': row.preview_hash,
                'document': deepcopy(row.document), 'result': deepcopy(row.result),
                'created_at': row.created_at, 'completed_at': row.completed_at}

    async def test_discard_replays_without_native_reads_and_new_preview_can_proceed(self):
        request = self.request()
        preview = await self.preview(request)
        before, frozen = await self.identity_hashes(), await self.frozen(preview['id'])
        with patch.object(links.source, 'discover', AsyncMock(side_effect=AssertionError('native read forbidden'))), \
             patch.object(links.source, 'verify', AsyncMock(side_effect=AssertionError('native read forbidden'))):
            discarded = await self.discard(preview)
            self.assertEqual(discarded['status'], 'expired')
            self.assertEqual(await self.discard(preview), discarded)
            self.assertEqual(await self.preview(request), discarded)
        self.assertEqual(await self.identity_hashes(), before)
        self.assertEqual(await self.frozen(preview['id']), frozen)
        replacement = await self.preview()
        self.assertNotEqual(replacement['id'], preview['id'])
        self.assertTrue(replacement['eligible'], replacement['dependencies']['blockers'])

    async def test_blocked_review_can_be_discarded_before_corrected_replacement(self):
        async with self.transaction() as db:
            db.add(ProductEditorOverride(product_id=self.retired, revision=1,
                data={'common': {'name': 'Duplicate manual content'}}))
        preview = await self.preview()
        self.assertFalse(preview['eligible'])
        before, frozen = await self.identity_hashes(), await self.frozen(preview['id'])
        discarded = await self.discard(preview)
        self.assertEqual(discarded['status'], 'expired')
        self.assertEqual(await self.identity_hashes(), before)
        self.assertEqual(await self.frozen(preview['id']), frozen)
        async with self.transaction() as db:
            override = await db.get(ProductEditorOverride, self.retired)
            override.data = {}
            override.revision += 1
        replacement = await self.preview()
        self.assertTrue(replacement['eligible'], replacement['dependencies']['blockers'])

    async def test_obsolete_parser_and_elapsed_time_do_not_prevent_discard(self):
        original = service._dependencies
        async def old_version(*args, **kwargs):
            result = await original(*args, **kwargs)
            result['facts']['typed_catalog_scope']['parser_version'] = 'typed-catalog-scope-v4'
            return result
        with patch.object(service, '_dependencies', side_effect=old_version):
            preview = await self.preview()
        with self.assertRaisesRegex(links.LinkError, 'consolidation_dependency_inventory_version_changed'):
            await self.apply(preview)
        frozen = await self.frozen(preview['id'])
        future = links.now() + timedelta(hours=1)
        with patch.object(links, 'now', return_value=future):
            discarded = await self.discard(preview)
        self.assertEqual(discarded['status'], 'expired')
        self.assertEqual(discarded['expires_at'], preview['expires_at'])
        self.assertEqual(await self.frozen(preview['id']), frozen)

    async def test_wrong_hash_missing_review_and_broken_proof_never_change_review(self):
        preview = await self.preview()
        frozen = await self.frozen(preview['id'])
        with self.assertRaisesRegex(links.LinkError, 'consolidation_preview_changed'):
            await self.discard({**preview, 'preview_hash': 'f' * 64})
        with self.assertRaisesRegex(links.LinkError, 'consolidation_not_found'):
            await self.discard({**preview, 'id': str(uuid4())})
        self.assertEqual(await self.frozen(preview['id']), frozen)
        async with self.transaction() as db:
            self.assertEqual((await db.get(ProductConsolidation, preview['id'])).status, 'prepared')
            corrupt_id = str(uuid4())
            document = deepcopy(frozen['document'])
            document['request']['request_id'] = corrupt_id
            db.add(ProductConsolidation(id=corrupt_id, request_hash='e' * 64, preview_hash='d' * 64,
                document=document, status='prepared', expires_at=links.now() + timedelta(minutes=30)))
        with self.assertRaisesRegex(links.LinkError, 'consolidation_preview_changed'):
            await self.discard({'id': corrupt_id, 'preview_hash': 'd' * 64})
        async with self.transaction() as db:
            self.assertEqual((await db.get(ProductConsolidation, corrupt_id)).status, 'prepared')

    async def test_completed_operation_is_replayed_instead_of_discarded(self):
        preview = await self.preview()
        completed = await self.apply(preview)
        before, frozen = await self.identity_hashes(), await self.frozen(preview['id'])
        with patch.object(links.source, 'verify', AsyncMock(side_effect=AssertionError('native read forbidden'))), \
             patch.object(links.source, 'discover', AsyncMock(side_effect=AssertionError('native read forbidden'))):
            self.assertEqual(await self.discard(preview), completed)
        self.assertEqual(await self.identity_hashes(), before)
        self.assertEqual(await self.frozen(preview['id']), frozen)

    async def test_discard_winning_during_native_apply_fences_later_ownership_writes(self):
        preview = await self.preview()
        before = await self.identity_hashes()
        entered, release = asyncio.Event(), asyncio.Event()
        original = links.source.verify
        async def delayed_verify(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)
        with patch.object(links.source, 'verify', side_effect=delayed_verify):
            applying = asyncio.create_task(self.apply(preview))
            try:
                await asyncio.wait_for(entered.wait(), 3)
                discarded = await self.discard(preview)
                self.assertEqual(discarded['status'], 'expired')
            finally:
                release.set()
            with self.assertRaisesRegex(links.LinkError, 'consolidation_preview_expired') as failure:
                await asyncio.wait_for(applying, 3)
            self.assertTrue(failure.exception.known_unapplied)
        self.assertEqual(await self.identity_hashes(), before)

    async def test_apply_winning_under_identity_fence_makes_discard_return_completion(self):
        preview = await self.preview()
        before = await self.stock()
        entered, release, discarding = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original = service._dependencies
        async def delayed_dependencies(*args, **kwargs):
            result = await original(*args, **kwargs)
            entered.set()
            await release.wait()
            return result
        async def discard_after_signal():
            async with self.transaction() as db:
                execute = db.execute
                async def signalled_execute(statement, *args, **kwargs):
                    if 'pg_advisory_xact_lock' in str(statement):
                        discarding.set()
                    return await execute(statement, *args, **kwargs)
                with patch.object(db, 'execute', side_effect=signalled_execute):
                    return await service.discard(db, preview['id'], ProductConsolidationApplyRequest(
                        preview_hash=preview['preview_hash'], confirmed=True))
        with patch.object(service, '_dependencies', side_effect=delayed_dependencies):
            applying = asyncio.create_task(self.apply(preview))
            discarded = None
            try:
                await asyncio.wait_for(entered.wait(), 3)
                discarded = asyncio.create_task(discard_after_signal())
                await asyncio.wait_for(discarding.wait(), 3)
                self.assertFalse(discarded.done())
            finally:
                release.set()
            done = await asyncio.wait_for(applying, 3)
            self.assertEqual(done['status'], 'completed')
            self.assertIsNotNone(discarded)
            self.assertEqual(await asyncio.wait_for(discarded, 3), done)
        self.assertEqual(await self.stock(), before)
