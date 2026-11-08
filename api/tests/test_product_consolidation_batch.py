"""Atomic local migration, exact replay and unchanged original physical facts."""
from copy import deepcopy
import asyncio
from decimal import Decimal
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import select, text

from inventory_hub.ai_content_models import AiBatch, AiJob
from inventory_hub.database import get_session
from inventory_hub.db_models import IdentifierType, Product, ProductIdentifier, Supplier
from inventory_hub.db_models_ext import ShopProduct, StockBalance, StockMovement
from inventory_hub.product_consolidation_models import ProductConsolidation
from inventory_hub.product_consolidation_batch_types import BatchApplyRequest, BatchPreviewRequest
from inventory_hub.product_editor_models import ProductEditorPublication
from inventory_hub.routers import product_consolidation as routes
from inventory_hub.services import product_consolidation_batch as batch
from inventory_hub.services import product_consolidation as core
from inventory_hub.services import product_dependency_scan as scan
from inventory_hub.services.product_dependency_scope import Scope
from inventory_hub.settings import settings
import test_product_consolidation_db as fixture
import test_atomer_catalog_handoff as handoff_fixture
import test_product_consolidation_supplier_gtin_db as supplier_fixture


def body(request, leaves):
    return dict(request_id=str(uuid4()), approved_scope_sha256='d' * 64,
        groups=[request.model_dump(mode='json', exclude={'request_id'})], native_catalogs=[dict(
            shop_code='biketrek', fingerprint='a' * 64, captured_at=core.links.now().isoformat(),
            export_sha256='e' * 64, complete=True, unfiltered_products_export=True,
            leaves=deepcopy(leaves), absent_native=[], absent_codes=[])],
        identity_scope_confirmed=True, preserve_all_raw_history=True, keep_catalog_campaigns_paused=True)


class BatchContractTests(unittest.TestCase):
    def synthetic(self):
        base = fixture.ProductConsolidationDatabaseTests()
        base.keeper, base.retired, base.mapping_ids = 1, 2, [11, 12]
        base.remote = [base.leaf('SYN-KEEP', 9001), base.leaf('SYN-OLD', 9002)]
        return body(base.request(), base.remote)

    def test_disjoint_scope_and_explicit_attestation(self):
        value = self.synthetic()
        parsed = BatchPreviewRequest(**value)
        raw = batch._request(parsed)
        self.assertNotEqual(raw['groups'][0]['request_id'], value['request_id'])
        self.assertEqual(batch._request(BatchPreviewRequest(**value)), raw)
        for change in ({'groups': value['groups'] * 2}, {'identity_scope_confirmed': 1},
                       {'preserve_all_raw_history': 'true'}, {'keep_catalog_campaigns_paused': False}):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                BatchPreviewRequest(**{**value, **change})
        changed = deepcopy(value)
        changed['groups'][0]['request_id'] = str(uuid4())
        with self.assertRaises(ValidationError):
            BatchPreviewRequest(**changed)

    def test_retained_axes_are_separate_bound_evidence(self):
        value = self.synthetic()
        leaf = value['native_catalogs'][0]['leaves'][0]
        attrs = [dict(name='size', value='XL')]
        leaf.update(reviewed_attributes=attrs, reviewed_attributes_sha256=core.links.digest(attrs),
                    reviewed_attributes_source_sha256='f' * 64)
        BatchPreviewRequest(**value)
        for changes in ({'reviewed_attributes_sha256': '0' * 64}, {'attributes': attrs},
                        {'reviewed_attributes_source_sha256': None}):
            changed = deepcopy(value)
            changed['native_catalogs'][0]['leaves'][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                BatchPreviewRequest(**changed)
        changed = self.synthetic()
        changed['native_catalogs'][0]['leaves'][0].update(raw_barcodes=['unregistered legacy text'], eans=[], invalid_barcodes=[])
        with self.assertRaises(ValidationError):
            BatchPreviewRequest(**changed)

    def test_operator_no_store_and_validation_before_db(self):
        calls = []
        async def session():
            calls.append(True)
            yield SimpleNamespace()
        app = FastAPI()
        app.include_router(routes.router)
        app.dependency_overrides[get_session] = session
        token = 'synthetic-one-off-' + 'x' * 32
        request = self.synthetic()
        base = '/product-consolidations/one-off/'
        with patch.object(settings, 'AI_CONTENT_ACCESS_TOKEN', SecretStr(token)), TestClient(app) as client:
            for method, path, payload, name in (
                    ('POST', base + 'preview', request, 'preview'),
                    ('POST', base + 'apply', dict(request=request, preview_hash='b' * 64, confirmed=True), 'apply'),
                    ('GET', base + request['request_id'], None, 'get')):
                with patch.object(batch, name, AsyncMock(return_value={'status': 'synthetic'})) as operation:
                    count = len(calls)
                    denied = client.request(method, path, json=payload)
                    self.assertEqual((denied.status_code, denied.headers['cache-control']), (401, 'no-store'))
                    self.assertEqual(len(calls), count)
                    operation.assert_not_awaited()
                    answer = client.request(method, path, json=payload, headers={'Authorization': 'Bearer ' + token})
                    self.assertEqual((answer.status_code, answer.headers['cache-control']), (200, 'no-store'))
            answer = client.post(base + 'apply', json=dict(request=request, preview_hash='b' * 64, confirmed=1),
                headers={'Authorization': 'Bearer ' + token})
            self.assertEqual((answer.status_code, answer.headers['cache-control']), (422, 'no-store'))


@unittest.skipUnless(fixture.TEST_URL, 'Dedicated localhost *_catalog_test database required')
class BatchDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.base = fixture.ProductConsolidationDatabaseTests()
        await self.base.asyncSetUp()
        self.sessions = self.base.sessions
        self.forbidden = [patch('requests.sessions.Session.request', side_effect=AssertionError('HTTP forbidden')),
            patch.object(core, '_native', AsyncMock(side_effect=AssertionError('native read forbidden'))),
            patch.object(core, '_dependencies', AsyncMock(side_effect=AssertionError('global typed scan forbidden')))]
        for p in self.forbidden:
            p.start()
        self.value = body(self.base.request(), self.base.remote)

    async def asyncTearDown(self):
        for p in reversed(self.forbidden):
            p.stop()
        await self.base.asyncTearDown()

    async def preview(self, value=None):
        async with self.sessions() as db:
            return await batch.preview(db, BatchPreviewRequest(**(value or self.value)))

    async def apply(self, preview=None, value=None):
        preview = preview or await self.preview(value)
        payload = BatchApplyRequest(request=BatchPreviewRequest(**(value or self.value)),
            preview_hash=preview['preview_hash'], confirmed=True)
        async with self.sessions() as db:
            return await batch.apply(db, payload, actor={'source': 'synthetic-test'})

    async def second_group(self):
        async with self.base.transaction() as db:
            cards = [Product(sku=code, name='Synthetic other retail unit', created_from_source='upgates:biketrek')
                for code in ('SECOND-KEEP', 'SECOND-OLD')]
            db.add_all(cards)
            await db.flush()
            db.add(ProductIdentifier(product_id=cards[0].id, identifier_type=IdentifierType.ean, value=fixture.OTHER_EAN))
            mappings = [ShopProduct(shop_id=self.base.shop_id, product_id=card.id, external_code=card.sku)
                for card in cards]
            db.add_all(mappings)
            await db.flush()
            group = deepcopy(self.value['groups'][0])
            group.update(keeper_product_id=cards[0].id, retired_product_ids=[cards[1].id])
            group['mapping_actions'] = []
            for i, mapping in enumerate(mappings):
                leaf = self.base.leaf(cards[i].sku, 9101 + i)
                leaf.update(eans=[fixture.OTHER_EAN], raw_barcodes=[fixture.OTHER_EAN])
                self.value['native_catalogs'][0]['leaves'].append(leaf)
                group['mapping_actions'].append(dict(mapping_id=mapping.id, action='bind', target=dict(
                    shop_code='biketrek', code=leaf['code'], parent_code=leaf['parent_code'], is_variant=False,
                    native_product_id=int(leaf['parent_id']), sale_to_stock_factor=1)))
            self.value['groups'].append(group)

    async def original_rows(self):
        tables = ('products', 'product_identifiers', 'shop_products', 'stock_balances', 'stock_movements',
            'product_supply_sources', 'product_sku_identities', 'atomer_catalog_jobs', 'atomer_catalog_proofs',
            'atomer_catalog_source_cards', 'product_consolidations', 'product_canonical_aliases')
        async with self.sessions() as db:
            return {table: await batch._summary(db, table, 'true', {}) for table in tables}

    async def renamed_import_history(self):
        async with self.base.transaction() as db:
            for pid, old_sku in ((self.base.keeper, 'SYN-KEEP'), (self.base.retired, 'SYN-OLD')):
                product = await db.get(Product, pid)
                product.sku = 'RENAMED-' + old_sku
                db.add(ProductIdentifier(product_id=pid, identifier_type=IdentifierType.internal_sku,
                    value=old_sku, supplier_id=None))
            db.add(StockBalance(product_id=self.base.retired, warehouse_id=self.base.warehouse_id,
                qty_on_hand=Decimal(1), qty_reserved=0, qty_quarantined=0))
            db.add(StockMovement(idempotency_key='synthetic-renamed-import', product_id=self.base.retired,
                warehouse_id=self.base.warehouse_id, movement_type='INITIAL', quantity=Decimal(1),
                balance_after=Decimal(1), reference_type='upgates_import', reference_source='upgates:biketrek',
                reference_id='SYN-OLD', created_by='upgates_import'))

    async def test_exact_old_skus_on_both_raw_owners_preserve_all_import_history(self):
        await self.renamed_import_history()
        before = await self.original_rows()
        preview = await self.preview()
        self.assertTrue(preview['eligible'])
        self.assertEqual(await self.original_rows(), before)
        result = await self.apply(preview)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['history_hash'], preview['history_hash'])
        self.assertFalse(result['physical_stock_changed'])
        after = await self.original_rows()
        for table in ('product_identifiers', 'stock_balances', 'stock_movements'):
            self.assertEqual(after[table], before[table])
        self.assertEqual(after['stock_movements']['count'], 2)

    async def test_old_reference_requires_exact_unscoped_internal_alias_on_same_raw_owner(self):
        await self.renamed_import_history()
        async with self.base.transaction() as db:
            supplier = Supplier(code='synthetic-alias-supplier', name='Synthetic alias supplier')
            db.add(supplier)
            await db.flush()
            supplier_id = supplier.id
            alias_id = await db.scalar(select(ProductIdentifier.id).where(
                ProductIdentifier.product_id == self.base.keeper,
                ProductIdentifier.identifier_type == IdentifierType.internal_sku))
        for name, pid, kind, supplier_id_value, value in (
            ('missing', self.base.keeper, 'internal_sku', None, 'UNRELATED'),
            ('other-card-in-group', self.base.retired, 'internal_sku', None, 'SYN-KEEP'),
            ('supplier-code', self.base.keeper, 'supplier_sku', supplier_id, 'SYN-KEEP'),
            ('scoped-internal-code', self.base.keeper, 'internal_sku', supplier_id, 'SYN-KEEP'),
            ('case-only', self.base.keeper, 'internal_sku', None, 'syn-keep'),
        ):
            with self.subTest(name=name):
                async with self.base.transaction() as db:
                    await db.execute(text('''UPDATE product_identifiers SET product_id=:pid,
                        identifier_type=CAST(:kind AS identifier_type), supplier_id=:supplier, value=:value
                        WHERE id=:id'''), dict(pid=pid, kind=kind, supplier=supplier_id_value,
                                              value=value, id=alias_id))
                before = await self.original_rows()
                with self.assertRaisesRegex(core.links.LinkError, 'consolidation_nonlegacy_stock_history'):
                    await self.preview()
                self.assertEqual(await self.original_rows(), before)

    async def test_old_reference_collision_with_same_group_sku_or_alias_still_blocks(self):
        await self.renamed_import_history()
        async with self.base.transaction() as db:
            await db.execute(text('UPDATE products SET sku=:sku WHERE id=:id'),
                dict(sku='syn-old', id=self.base.keeper))
        before = await self.original_rows()
        with self.assertRaisesRegex(core.links.LinkError, 'consolidation_nonlegacy_stock_history'):
            await self.preview()
        self.assertEqual(await self.original_rows(), before)
        async with self.base.transaction() as db:
            await db.execute(text('UPDATE products SET sku=:sku WHERE id=:id'),
                dict(sku='RENAMED-SYN-KEEP', id=self.base.keeper))
            db.add(ProductIdentifier(product_id=self.base.keeper, identifier_type=IdentifierType.internal_sku,
                value='syn-old'))
        before = await self.original_rows()
        with self.assertRaisesRegex(core.links.LinkError, 'consolidation_nonlegacy_stock_history'):
            await self.preview()
        self.assertEqual(await self.original_rows(), before)

    async def test_old_alias_does_not_waive_any_other_movement_provenance(self):
        await self.renamed_import_history()
        before = await self.original_rows()
        raw = batch._request(BatchPreviewRequest(**self.value))
        async with self.sessions() as db:
            proof = await batch._capture(db, raw)
            for change in ({'reference_source': 'upgates:xtrek'}, {'reference_source': 'manual'},
                           {'created_by': 'operator'}, {'reference_type': 'receiving_session'},
                           {'movement_type': 'RECEIVING_IN'}):
                with self.subTest(change=change):
                    savepoint = await db.begin_nested()
                    try:
                        values = dict(idempotency_key='synthetic-invalid-' + uuid4().hex,
                            product_id=self.base.retired, warehouse_id=self.base.warehouse_id,
                            movement_type='INITIAL', quantity=Decimal(1), balance_after=Decimal(2),
                            reference_type='upgates_import', reference_source='upgates:biketrek',
                            reference_id='SYN-OLD', created_by='upgates_import')
                        db.add(StockMovement(**{**values, **change}))
                        await db.flush()
                        history = await batch._history(db, raw, proof['reference_schema'])
                        with self.assertRaisesRegex(core.links.LinkError, 'consolidation_nonlegacy_stock_history'):
                            await batch._admission(db, raw, proof['groups'], history)
                    finally:
                        await savepoint.rollback()
        self.assertEqual(await self.original_rows(), before)

    async def test_atomic_two_group_commit_replay_and_complete_fk_conservation(self):
        await self.second_group()
        before = await self.original_rows()
        preview = await self.preview()
        self.assertEqual(await self.original_rows(), before)
        self.assertTrue(preview['eligible'])
        self.assertEqual(preview['group_count'], 2)
        result = await self.apply(preview)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['history_hash'], preview['history_hash'])
        self.assertFalse(result['physical_stock_changed'])
        self.assertEqual(len(result['groups']), 2)
        self.assertEqual(await self.apply(preview), result)
        async with self.sessions() as db:
            self.assertEqual(await batch.get(db, self.value['request_id']), result)
            self.assertEqual(await db.scalar(text('SELECT count(*) FROM product_canonical_aliases')), 2)
            self.assertEqual(await db.scalar(text('SELECT count(*) FROM product_consolidations')), 2)
            self.assertEqual(await db.scalar(text('SELECT count(*) FROM shop_products WHERE retired_at IS NULL')), 4)
            self.assertEqual(await db.scalar(text('SELECT count(*) FROM shop_products WHERE retired_at IS NOT NULL')), 4)
            self.assertEqual(await db.scalar(text('SELECT count(*) FROM shop_products WHERE retired_at IS NULL AND (shop_stock IS NOT NULL OR shop_price IS NOT NULL)')), 0)
            for table in ('product_identifiers', 'stock_balances', 'stock_movements', 'product_supply_sources', 'product_sku_identities'):
                self.assertEqual(await batch._summary(db, table, 'true', {}), before[table])
            # Completed children remain understood by the existing strict parser.
            for row in await db.scalars(select(ProductConsolidation)):
                proof = scan.s.Proof(Scope.build([], [], [], [], []))
                scan.s.consolidation(proof, {'document': row.document})
                self.assertFalse(proof.errors)
        changed = deepcopy(self.value)
        changed['approved_scope_sha256'] = '0' * 64
        with self.assertRaisesRegex(core.links.LinkError, 'consolidation_batch_request_changed'):
            await self.apply(preview, changed)

    async def test_late_failure_rolls_back_first_completed_child_and_every_change(self):
        await self.second_group()
        preview = await self.preview()
        before = await self.original_rows()
        original = core._mutate
        calls = []
        async def fail_second(*args, **kwargs):
            calls.append(True)
            if len(calls) == 2:
                raise core.links.LinkError('synthetic_last_group_conflict')
            return await original(*args, **kwargs)
        with patch.object(core, '_mutate', side_effect=fail_second):
            with self.assertRaisesRegex(core.links.LinkError, 'synthetic_last_group_conflict'):
                await self.apply(preview)
        self.assertEqual(len(calls), 2)
        self.assertEqual(await self.original_rows(), before)
        async with self.sessions() as db:
            with self.assertRaisesRegex(core.links.LinkError, 'consolidation_batch_not_found'):
                await batch.get(db, self.value['request_id'])

    async def test_stale_raw_history_and_missing_reference_guard_fail_before_mutation(self):
        preview = await self.preview()
        async with self.base.transaction() as db:
            await db.execute(text('UPDATE stock_balances SET qty_on_hand=qty_on_hand+1'))
        before = await self.original_rows()
        with self.assertRaisesRegex(core.links.LinkError, 'consolidation_batch_preview_changed'):
            await self.apply(preview)
        self.assertEqual(await self.original_rows(), before)
        async with self.base.transaction() as db:
            await db.execute(text('ALTER TABLE stock_balances DISABLE TRIGGER trg_retired_reference_product_id'))
        with self.assertRaisesRegex(core.links.LinkError, 'consolidation_batch_reference_guard_missing'):
            await self.preview()
        async with self.base.transaction() as db:
            await db.execute(text('ALTER TABLE stock_balances ENABLE TRIGGER trg_retired_reference_product_id'))
            await db.execute(text('ALTER TABLE shop_products DISABLE TRIGGER trg_retired_mapping_guard'))
        with self.assertRaisesRegex(core.links.LinkError, 'consolidation_batch_schema_guard_missing'):
            await self.preview()

    async def test_absence_scope_and_native_payload_cannot_be_silently_omitted(self):
        value = deepcopy(self.value)
        value['native_catalogs'][0]['leaves'].pop()
        with self.assertRaisesRegex(core.links.LinkError, 'consolidation_batch_native_identity_changed'):
            await self.preview(value)
        value = deepcopy(self.value)
        value['native_catalogs'][0]['absent_native'] = [dict(is_variant=False, parent_id='9999', external_id='9999')]
        with self.assertRaisesRegex(core.links.LinkError, 'consolidation_batch_catalog_scope_changed'):
            await self.preview(value)

    async def test_physical_reservations_and_foreign_owner_are_not_waived(self):
        async with self.base.transaction() as db:
            await db.execute(text('UPDATE stock_balances SET qty_reserved=1'))
        with self.assertRaisesRegex(core.links.LinkError, 'consolidation_reserved_or_quarantined_stock'):
            await self.preview()
        async with self.base.transaction() as db:
            await db.execute(text('UPDATE stock_balances SET qty_reserved=0'))
            third = Product(sku='FOREIGN-OWNER', name='Different card')
            db.add(third)
            await db.flush()
            db.add(ProductIdentifier(product_id=third.id, identifier_type=IdentifierType.internal_sku, value='SYN-KEEP'))
        with self.assertRaisesRegex(core.links.LinkError, 'consolidation_foreign_identity_owner'):
            await self.preview()

    async def test_pending_publication_names_only_its_fixed_gate(self):
        async with self.base.transaction() as db:
            db.add(ProductEditorPublication(id=str(uuid4()), product_id=self.base.keeper,
                shop_id=self.base.shop_id, state='ready', document={'private': 'synthetic-marker'}))
        before = await self.original_rows()
        with self.assertRaises(core.links.LinkError) as failure:
            await self.preview()
        self.assertEqual(failure.exception.code,
            'consolidation_batch_pending_publication_product_editor_publications')
        self.assertFalse(failure.exception.known_unapplied)
        self.assertEqual(await self.original_rows(), before)

    async def ai_jobs(self, contexts):
        async with self.base.transaction() as db:
            group = AiBatch(id=uuid4().hex, request_hash='f' * 64)
            db.add(group)
            await db.flush()
            jobs = [AiJob(id=uuid4().hex, batch_id=group.id, status='review', context=context)
                    for context in contexts]
            db.add_all(jobs)
            return [job.id for job in jobs]

    async def test_absent_and_json_null_ai_previews_allow_batch_without_changing_jobs(self):
        await self.ai_jobs([{'code': 'SYN-KEEP'}, {'code': 'SYN-OLD', 'update_preview': None}])
        async with self.sessions() as db:
            before = await batch._summary(db, 'ai_content_jobs', 'true', {})
        result = await self.apply()
        self.assertEqual(result['status'], 'completed')
        async with self.sessions() as db:
            self.assertEqual(await batch._summary(db, 'ai_content_jobs', 'true', {}), before)

    async def test_pending_and_malformed_nonnull_ai_previews_still_block(self):
        job_id, = await self.ai_jobs([{'code': 'SYN-KEEP'}])
        for value in ({'state': 'prepared'}, {'state': 'sending'}, {'state': 'uncertain'},
                      {'state': 'unknown'}, {'state': None}, {}, [], 'malformed', 1, False):
            with self.subTest(value=value):
                async with self.base.transaction() as db:
                    job = await db.get(AiJob, job_id)
                    job.context = {'code': 'SYN-KEEP', 'update_preview': value}
                async with self.sessions() as db:
                    before = await batch._summary(db, 'ai_content_jobs', 'true', {})
                with self.assertRaises(core.links.LinkError) as failure:
                    await self.preview()
                self.assertEqual(failure.exception.code, 'consolidation_batch_pending_publication_ai_content_jobs')
                async with self.sessions() as db:
                    self.assertEqual(await batch._summary(db, 'ai_content_jobs', 'true', {}), before)

    async def test_concurrent_same_uuid_busy_is_not_a_false_unapplied_receipt(self):
        preview = await self.preview()
        reached, release = asyncio.Event(), asyncio.Event()
        original = core._mutate
        async def held(*args, **kwargs):
            reached.set()
            await release.wait()
            return await original(*args, **kwargs)
        with patch.object(core, '_mutate', side_effect=held):
            first = asyncio.create_task(self.apply(preview))
            try:
                await asyncio.wait_for(reached.wait(), 10)
                with self.assertRaises(core.links.LinkError) as raised:
                    await self.apply(preview)
                self.assertFalse(raised.exception.known_unapplied)
            finally:
                release.set()
            result = await first
        async with self.sessions() as db:
            self.assertEqual(await batch.get(db, self.value['request_id']), result)
            self.assertEqual(await db.scalar(text('SELECT count(*) FROM product_consolidations')), 1)

    async def test_private_mapping_error_is_hashed_but_never_returned(self):
        marker = 'PRIVATE-SYNTHETIC-REMOTE-ERROR'
        async with self.base.transaction() as db:
            await db.execute(text('UPDATE shop_products SET last_push_error=:marker'), {'marker': marker})
        preview = await self.preview()
        self.assertNotIn(marker, json.dumps(preview))
        async with self.base.transaction() as db:
            await db.execute(text("UPDATE shop_products SET last_push_error='changed'"))
        with self.assertRaisesRegex(core.links.LinkError, 'consolidation_batch_preview_changed'):
            await self.apply(preview)


@unittest.skipUnless(fixture.TEST_URL, 'Dedicated localhost *_catalog_test database required')
class BatchPreservedEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_paused_pending_job_and_all_retained_source_proofs_remain_unchanged(self):
        context = handoff_fixture.HandoffDatabaseTests()
        await context.asyncSetUp()
        try:
            value = body(context.base.request(), context.base.remote)
            before = await context.protected()
            async with context.sessions() as db:
                jobs = await batch._summary(db, 'atomer_catalog_jobs', 'true', {})
                campaigns_before = await batch._summary(db, 'atomer_catalog_campaigns', 'true', {})
            async with context.sessions() as db:
                preview = await batch.preview(db, BatchPreviewRequest(**value))
            async with context.sessions() as db:
                result = await batch.apply(db, BatchApplyRequest(request=BatchPreviewRequest(**value),
                    preview_hash=preview['preview_hash'], confirmed=True))
            self.assertFalse(result['source_publication_completed'])
            self.assertEqual(result['historical_native_effects'], 'not_determined')
            async with context.sessions() as db:
                self.assertEqual(await batch._summary(db, 'atomer_catalog_jobs', 'true', {}), jobs)
                self.assertEqual(await batch._summary(db, 'atomer_catalog_campaigns', 'true', {}), campaigns_before)
                self.assertEqual(await db.scalar(text('SELECT stage FROM atomer_catalog_jobs')), 'pending')
                self.assertTrue(await db.scalar(text('SELECT operator_paused FROM atomer_catalog_campaigns')))
            after = await context.protected()
            for table in ('stock_balances', 'stock_movements', 'atomer_catalog_proofs', 'atomer_catalog_source_cards',
                          'atomer_review_work', 'atomer_review_events', 'product_identifiers'):
                self.assertEqual(after[table], before[table])
        finally:
            await context.asyncTearDown()

    async def test_actual_supplier_proof_accepts_explicit_retained_axes_and_keeps_all_sources(self):
        context = supplier_fixture.SupplierGtinDatabaseTests()
        await context.asyncSetUp()
        try:
            value = body(context.request(), context.remote)
            bt = value['native_catalogs'][0]
            xt = {**deepcopy(bt), 'shop_code': 'xtrek', 'leaves': [deepcopy(bt['leaves'][1])]}
            bt['leaves'] = bt['leaves'][:1]
            value['native_catalogs'].append(xt)
            for catalog in value['native_catalogs']:
                for leaf in catalog['leaves']:
                    leaf.update(reviewed_attributes=leaf['attributes'],
                        reviewed_attributes_sha256=core.links.digest(leaf['attributes']),
                        reviewed_attributes_source_sha256='f' * 64,
                        attributes=[], warnings=['variant_axes_unknown'])
            before = await context.preserved()
            with patch.object(core, '_native', AsyncMock(side_effect=AssertionError('native forbidden'))):
                async with context.sessions() as db:
                    preview = await batch.preview(db, BatchPreviewRequest(**value))
                async with context.sessions() as db:
                    result = await batch.apply(db, BatchApplyRequest(request=BatchPreviewRequest(**value),
                        preview_hash=preview['preview_hash'], confirmed=True))
            self.assertEqual(await context.preserved(), before)
            async with context.sessions() as db:
                row = await db.get(ProductConsolidation, result['groups'][0]['id'])
                proof = scan.s.Proof(Scope.build([], [], [], [], []))
                scan.s.consolidation(proof, {'document': row.document})
                for native in row.document['native'].values():
                    self.assertEqual(native['leaves'][0]['attributes_basis'], 'operator_reviewed_retained_evidence')
                    self.assertEqual(native['leaves'][0]['catalog_attributes'], [])
                self.assertFalse(proof.errors)
        finally:
            await context.asyncTearDown()
