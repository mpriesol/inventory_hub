"""Supplier alias writes and recovery against disposable localhost PostgreSQL."""
from contextlib import asynccontextmanager
from copy import deepcopy
import asyncio
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from inventory_hub.db_models import IdentifierType, Product, ProductIdentifier, Supplier, SupplierProduct
from inventory_hub.db_models_ext import ProductSupplySource, ProductVariantAttribute, ShopProduct
from inventory_hub.product_editor_models import ProductEditorAudit, ProductEditorOverride, ProductEditorSave
from inventory_hub.services import supplier_card_links as service
from inventory_hub.supplier_card_link_types import (
    SupplierCardLinkApplyRequest, SupplierCardLinkCandidatesRequest, SupplierCardLinkPreviewRequest,
)


TEST_URL = os.environ.get('CATALOG_TEST_DATABASE_URL', '')
EAN = '8585047234448'
OTHER_EAN = '5901234123457'


@unittest.skipUnless(TEST_URL, 'Dedicated localhost *_catalog_test database required')
class SupplierCardLinksDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        parsed = urlsplit(TEST_URL)
        if parsed.hostname not in ('localhost', '127.0.0.1') or not parsed.path.endswith('_catalog_test'):
            raise RuntimeError('Supplier card link tests require an isolated localhost *_catalog_test database')
        self.schema = 'supplier_card_links_test_' + uuid4().hex
        root = Path(__file__).resolve().parents[2] / 'infra' / 'db-init'
        connection = await asyncpg.connect(TEST_URL)
        try:
            await connection.execute(f'CREATE SCHEMA "{self.schema}"')
            await connection.execute(f'SET search_path TO "{self.schema}"')
            for name in ('001_schema.sql', '004_shop_product_content.sql', '012_product_editor.sql',
                         '028_supplier_card_links.sql', '028_supplier_card_links.sql', '029_sku_policy.sql'):
                await connection.execute((root / name).read_text())
        finally:
            await connection.close()
        self.engine = create_async_engine(TEST_URL.replace('postgresql://', 'postgresql+asyncpg://', 1),
            poolclass=NullPool, connect_args={'server_settings': {'search_path': self.schema}})
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False, autoflush=False)
        self.patches = [patch.object(service, '_prefix', return_value='NF-'),
                        patch.object(service.config_io, 'claim_supplier_prefix', return_value='NF-')]
        for patcher in self.patches:
            patcher.start()
        async with self.transaction() as db:
            supplier = Supplier(code='northfinder-test', name='Synthetic supplier')
            db.add(supplier)
            await db.flush()
            self.supplier_id = supplier.id
            local = Product(sku='EXISTING-BLUE-M', name='Original card name', brand='Northfinder',
                            supplier_price='17.2500')
            source = SupplierProduct(supplier_id=supplier.id, supplier_sku='103404-281-103',
                name='Northkit blue M', brand='Northfinder', ean=EAN, purchase_price='11.7500',
                supplier_group_code='103404', attributes={'catalog': {
                    'supplier': supplier.code, 'code': '103404-281-103', 'shop_code': 'NF-103404-281-103',
                    'name': 'Northkit blue M', 'brand': 'Northfinder', 'prices': {'currency': 'EUR'},
                    'eans': [EAN], 'variant_attributes': [{'name': 'Farba', 'value': 'Blue'},
                        {'name': 'Veľkosť', 'value': 'M'}], 'group_code': '103404'}})
            db.add_all([local, source])
            await db.flush()
            self.product_id, self.source_id = local.id, source.id
            db.add_all([ProductVariantAttribute(product_id=local.id, attribute_name='Farba', attribute_value='Blue'),
                        ProductVariantAttribute(product_id=local.id, attribute_name='Veľkosť', attribute_value='M')])
            shop_id = await db.scalar(text("SELECT id FROM shops WHERE code='biketrek'"))
            db.add(ShopProduct(shop_id=shop_id, product_id=local.id, external_code='SHOP-PARENT',
                parent_code='SHOP-PARENT', variant_code=local.sku, external_id='123', is_variant=True, is_listed=True))
            await db.execute(text("""INSERT INTO stock_balances
                (product_id,warehouse_id,qty_on_hand,qty_reserved,avg_cost,total_value)
                SELECT :id,id,7,2,17.25,120.75 FROM warehouses WHERE code='hlavny-sklad'"""), {'id': local.id})
            await db.execute(text("""INSERT INTO stock_movements
                (idempotency_key,product_id,warehouse_id,movement_type,quantity,unit_cost,balance_after,avg_cost_after)
                SELECT 'existing-fixture-receipt',:id,id,'RECEIVING_IN',7,17.25,7,17.25
                FROM warehouses WHERE code='hlavny-sklad'"""), {'id': local.id})

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
            async with db.begin():
                yield db

    async def inspect(self, product_id=None, source_id=None):
        async with self.transaction() as db:
            return await service.inspect_link(db, product_id or self.product_id, source_id or self.source_id)

    async def apply(self, preview):
        async with self.transaction() as db:
            return await service.apply_link(db, self.product_id, self.source_id, expected_hash=preview['preview_hash'])

    async def snapshot(self):
        async with self.sessions() as db:
            return {table: await db.scalar(text(f"SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY t.id),'[]'::jsonb) FROM {table} t"))
                    for table in ('products', 'supplier_products', 'product_variant_attributes', 'shop_products',
                                  'stock_balances', 'stock_movements', 'shop_sync_outbox')}

    async def relations(self):
        async with self.sessions() as db:
            aliases = (await db.execute(select(ProductIdentifier.product_id, ProductIdentifier.supplier_id,
                ProductIdentifier.value).where(ProductIdentifier.identifier_type == IdentifierType.supplier_sku))).all()
            links = (await db.execute(select(ProductSupplySource.product_id, ProductSupplySource.supplier_product_id))).all()
            return aliases, links

    async def test_different_supplier_code_links_existing_shop_card_without_other_writes(self):
        before = await self.snapshot()
        preview = await self.inspect()
        self.assertEqual(preview['errors'], [])
        self.assertEqual(await self.relations(), ([], []))
        await self.apply(preview)
        aliases, links = await self.relations()
        self.assertEqual(aliases, [(self.product_id, self.supplier_id, '103404-281-103')])
        self.assertEqual(links, [(self.product_id, self.source_id)])
        self.assertEqual(await self.snapshot(), before)
        # An absent canonical EAN stays absent; a supplier offer is not a barcode edit.
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(text("SELECT count(*) FROM product_identifiers WHERE identifier_type='ean'")), 0)
        await self.apply(preview)
        self.assertEqual(await self.relations(), (aliases, links))

    async def test_brand_change_after_preview_blocks_without_writing_links(self):
        preview = await self.inspect()
        async with self.transaction() as db:
            source = await db.get(SupplierProduct, self.source_id)
            source.brand = 'Topeak'
            source.attributes = {**source.attributes, 'catalog': {**source.attributes['catalog'], 'brand': 'Topeak'}}
        current = await self.inspect()
        self.assertIn('sku_brand_conflict', current['errors'])
        with self.assertRaises(service.LinkError):
            await self.apply(preview)
        self.assertEqual(await self.relations(), ([], []))

    async def test_upc_and_ean_equivalence_keeps_raw_values_and_stock_unchanged(self):
        async with self.transaction() as db:
            source = await db.get(SupplierProduct, self.source_id)
            source.ean = '727670929720'
            source.attributes = {**source.attributes, 'catalog': {**source.attributes['catalog'], 'eans': ['0727670929720']}}
            db.add(ProductIdentifier(product_id=self.product_id, identifier_type=IdentifierType.ean, value='0727670929720'))
        before = await self.snapshot()
        preview = await self.inspect()
        self.assertEqual(preview['errors'], [])
        await self.apply(preview)
        self.assertEqual(await self.snapshot(), before)
        async with self.sessions() as db:
            values = (await db.scalars(select(ProductIdentifier.value).where(
                ProductIdentifier.identifier_type.in_((IdentifierType.ean, IdentifierType.upc))))).all()
            self.assertEqual(values, ['0727670929720'])

    async def test_nonempty_conflicting_barcode_cannot_be_overridden_by_explicit_choice(self):
        async with self.transaction() as db:
            db.add(ProductIdentifier(product_id=self.product_id, identifier_type=IdentifierType.ean, value=OTHER_EAN))
        preview = await self.inspect()
        self.assertTrue(preview['errors'])
        with self.assertRaises(service.LinkError):
            await self.apply(preview)
        self.assertEqual(await self.relations(), ([], []))

    async def test_known_different_variant_size_blocks_link_even_with_same_name(self):
        async with self.transaction() as db:
            attr = await db.scalar(select(ProductVariantAttribute).where(
                ProductVariantAttribute.product_id == self.product_id, ProductVariantAttribute.attribute_name == 'Veľkosť'))
            attr.attribute_value = 'L'
        preview = await self.inspect()
        self.assertTrue(preview['errors'])
        with self.assertRaises(service.LinkError):
            await self.apply(preview)
        self.assertEqual(await self.relations(), ([], []))

    async def assert_existing_owner_not_reassigned(self, kind):
        async with self.transaction() as db:
            owner = Product(sku='OTHER-' + kind, name='Existing owner',
                source_supplier_product_id=self.source_id if kind == 'legacy' else None)
            db.add(owner)
            await db.flush()
            if kind == 'alias':
                db.add(ProductIdentifier(product_id=owner.id, identifier_type=IdentifierType.supplier_sku,
                    supplier_id=self.supplier_id, value='103404-281-103'))
        before = await self.relations()
        preview = await self.inspect()
        self.assertTrue(preview['errors'])
        with self.assertRaises(service.LinkError):
            await self.apply(preview)
        self.assertEqual(await self.relations(), before)

    async def test_source_alias_owner_is_not_reassigned(self):
        await self.assert_existing_owner_not_reassigned('alias')

    async def test_source_legacy_pointer_owner_is_not_reassigned(self):
        await self.assert_existing_owner_not_reassigned('legacy')

    async def test_source_identity_changes_invalidate_preview_and_rollback_partial_write(self):
        preview = await self.inspect()
        async with self.transaction() as db:
            source = await db.get(SupplierProduct, self.source_id)
            attrs = deepcopy(source.attributes)
            attrs['catalog']['variant_attributes'][1]['value'] = 'XL'
            source.attributes = attrs
        with self.assertRaises(service.LinkError):
            await self.apply(preview)
        self.assertEqual(await self.relations(), ([], []))

    async def test_outer_transaction_rollback_removes_both_new_relations(self):
        preview = await self.inspect()
        with self.assertRaisesRegex(RuntimeError, 'injected outer failure'):
            async with self.transaction() as db:
                await service.apply_link(db, self.product_id, self.source_id, expected_hash=preview['preview_hash'])
                raise RuntimeError('injected outer failure')
        self.assertEqual(await self.relations(), ([], []))
        await self.apply(preview)
        self.assertEqual(len((await self.relations())[1]), 1)

    async def test_concurrent_identical_links_do_not_duplicate_relations(self):
        preview = await self.inspect()
        await asyncio.gather(self.apply(preview), self.apply(preview))
        aliases, links = await self.relations()
        self.assertEqual(len(aliases), 1)
        self.assertEqual(len(links), 1)

    async def preview_request(self, request=None):
        request = request or SupplierCardLinkPreviewRequest(request_id=uuid4(),
            product_id=self.product_id, supplier_product_id=self.source_id)
        async with self.transaction() as db:
            return await service.preview(db, request)

    async def apply_request(self, preview):
        async with self.transaction() as db:
            return await service.apply(db, preview['id'], SupplierCardLinkApplyRequest(
                preview_hash=preview['preview_hash'], confirmed=True))

    async def test_durable_preview_replays_only_its_original_selection(self):
        request = SupplierCardLinkPreviewRequest(request_id=uuid4(),
            product_id=self.product_id, supplier_product_id=self.source_id)
        before = await self.snapshot()
        preview = await self.preview_request(request)
        self.assertEqual(await self.preview_request(request), preview)
        with self.assertRaises(service.LinkError):
            await self.preview_request(request.model_copy(update={'product_id': self.product_id + 10000}))
        self.assertEqual(await self.relations(), ([], []))
        self.assertEqual(await self.snapshot(), before)

    async def test_committed_result_is_recoverable_after_lost_response_and_later_catalog_change(self):
        preview = await self.preview_request()
        completed = await self.apply_request(preview)
        self.assertEqual(completed['status'], 'completed')
        async with self.transaction() as db:
            source = await db.get(SupplierProduct, self.source_id)
            source.is_active = False
        async with self.sessions() as db:
            recovered = await service.get(db, preview['id'])
        self.assertEqual(recovered['result'], completed['result'])
        replay = await self.apply_request(preview)
        self.assertEqual(replay['result'], completed['result'])
        self.assertEqual(len((await self.relations())[1]), 1)

    async def test_changed_identity_fences_delayed_retry_even_if_identity_is_restored(self):
        preview = await self.preview_request()
        async with self.transaction() as db:
            product = await db.get(Product, self.product_id)
            product.is_active = False
        with self.assertRaises(service.LinkError) as error:
            await self.apply_request(preview)
        self.assertTrue(error.exception.known_unapplied)
        async with self.transaction() as db:
            product = await db.get(Product, self.product_id)
            product.is_active = True
        with self.assertRaises(service.LinkError) as delayed:
            await self.apply_request(preview)
        self.assertEqual(delayed.exception.code, 'supplier_card_links_preview_expired')
        self.assertEqual(await self.relations(), ([], []))

    async def test_existing_source_policy_is_preserved(self):
        async with self.transaction() as db:
            db.add(ProductSupplySource(product_id=self.product_id, supplier_product_id=self.source_id,
                priority=7, orderable=False, is_active=False, pack_size=4, negotiated_price='9.25'))
        preview = await self.inspect()
        self.assertEqual(preview['errors'], [])
        self.assertIn('supplier_link_disabled', preview['warnings'])
        await self.apply(preview)
        async with self.sessions() as db:
            row = await db.scalar(select(ProductSupplySource).where(ProductSupplySource.product_id == self.product_id))
            self.assertEqual((row.priority, row.orderable, row.is_active, row.pack_size, str(row.negotiated_price)),
                             (7, False, False, 4, '9.2500'))

    async def ean_preview(self):
        return await self.preview_request(SupplierCardLinkPreviewRequest(request_id=uuid4(),
            product_id=self.product_id, supplier_product_id=self.source_id, copy_supplier_eans=True))

    async def test_explicit_ean_copy_is_atomic_audited_and_preserves_manual_fields(self):
        async with self.transaction() as db:
            db.add(ProductEditorOverride(product_id=self.product_id, revision=4, data={
                'common': {'name': 'Manual title'}, 'variant': {'eans': [], 'note': 'Keep this', 'sale_price_gross': '99.00'}}))
        before = await self.snapshot()
        preview = await self.ean_preview()
        self.assertEqual(preview['ean_adoption']['eans'], [EAN])
        self.assertIn('supplier_eans', preview['planned_changes'])
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(text("SELECT count(*) FROM product_identifiers WHERE identifier_type='ean'")), 0)
        completed = await self.apply_request(preview)
        self.assertEqual(completed['result']['copied_eans'], [EAN])
        self.assertEqual(completed['product']['eans'], [EAN])
        self.assertEqual(await self.apply_request(preview), completed)
        self.assertEqual(await self.snapshot(), before)
        async with self.sessions() as db:
            codes = list(await db.scalars(select(ProductIdentifier).where(
                ProductIdentifier.product_id == self.product_id, ProductIdentifier.identifier_type == IdentifierType.ean)))
            self.assertEqual([(row.value, row.is_primary) for row in codes], [(EAN, True)])
            override = await db.get(ProductEditorOverride, self.product_id)
            self.assertEqual(override.revision, 5)
            self.assertEqual(override.data, {'common': {'name': 'Manual title'},
                'variant': {'eans': [EAN], 'note': 'Keep this', 'sale_price_gross': '99.00'}})
            audits = list(await db.scalars(select(ProductEditorAudit)))
            self.assertEqual(len(audits), 1)
            self.assertEqual(audits[0].before_data['eans'], [])
            self.assertEqual(audits[0].after_data['eans'], [EAN])
            self.assertIsNotNone(await db.get(ProductEditorSave, audits[0].request_id))

    async def test_already_linked_card_can_fill_ean_without_recreating_relations(self):
        await self.apply(await self.inspect())
        relations = await self.relations()
        preview = await self.ean_preview()
        self.assertEqual(preview['planned_changes'], ['supplier_eans'])
        completed = await self.apply_request(preview)
        self.assertEqual(completed['result']['copied_eans'], [EAN])
        self.assertEqual(await self.relations(), relations)
        async with self.sessions() as db:
            self.assertEqual((await service.get(db, preview['id']))['product']['eans'], [EAN])

    async def test_ean_copy_never_overwrites_even_unverified_existing_barcode(self):
        async with self.transaction() as db:
            db.add(ProductIdentifier(product_id=self.product_id, identifier_type=IdentifierType.unverified_barcode, value='1234'))
        preview = await self.ean_preview()
        self.assertFalse(preview['ean_adoption']['available'])
        self.assertEqual(preview['ean_adoption']['reason'], 'ean_present')
        self.assertIn('supplier_card_links_ean_unavailable', preview['errors'])
        with self.assertRaises(service.LinkError):
            await self.apply_request(preview)
        self.assertEqual(await self.relations(), ([], []))
        async with self.sessions() as db:
            self.assertEqual(list(await db.scalars(select(ProductIdentifier.value))), ['1234'])

    async def test_ean_copy_rejects_any_other_identifier_owner_and_nonempty_manual_ean(self):
        async with self.transaction() as db:
            other = Product(sku='OTHER-EAN-OWNER', name='Other card')
            db.add(other); await db.flush()
            db.add(ProductIdentifier(product_id=other.id, identifier_type=IdentifierType.custom, value=EAN))
        preview = await self.ean_preview()
        self.assertEqual(preview['ean_adoption']['reason'], 'ean_conflict')
        self.assertEqual(await self.relations(), ([], []))
        async with self.transaction() as db:
            db.add(ProductEditorOverride(product_id=self.product_id, revision=1, data={'variant': {'eans': [OTHER_EAN]}}))
        preview = await self.ean_preview()
        self.assertEqual(preview['ean_adoption']['reason'], 'ean_present')

    async def test_changed_source_before_ean_apply_fences_without_partial_alias_or_ean(self):
        preview = await self.ean_preview()
        async with self.transaction() as db:
            source = await db.get(SupplierProduct, self.source_id)
            source.ean = OTHER_EAN
            attributes = deepcopy(source.attributes); attributes['catalog']['eans'] = [OTHER_EAN]
            source.attributes = attributes
        with self.assertRaises(service.LinkError) as failure:
            await self.apply_request(preview)
        self.assertTrue(failure.exception.known_unapplied)
        self.assertEqual(await self.relations(), ([], []))
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(text('SELECT count(*) FROM product_editor_audit')), 0)
            self.assertEqual(await db.scalar(text('SELECT count(*) FROM product_identifiers')), 0)

    async def test_ean_copy_rollback_removes_audit_ean_and_new_relations_together(self):
        preview = await self.ean_preview()
        actual = service._record_ean_copy
        async def fail_after_copy(*args, **kwargs):
            await actual(*args, **kwargs)
            await args[0].flush()
            raise RuntimeError('synthetic failure after EAN audit')
        with patch.object(service, '_record_ean_copy', side_effect=fail_after_copy):
            with self.assertRaisesRegex(RuntimeError, 'synthetic failure'):
                await self.apply_request(preview)
        self.assertEqual(await self.relations(), ([], []))
        async with self.sessions() as db:
            for table in ('product_identifiers', 'product_editor_overrides', 'product_editor_audit', 'product_editor_saves'):
                self.assertEqual(await db.scalar(text('SELECT count(*) FROM ' + table)), 0)
        self.assertEqual((await self.apply_request(preview))['result']['copied_eans'], [EAN])

    async def test_concurrent_ean_confirmation_records_one_editor_revision(self):
        preview = await self.ean_preview()
        results = await asyncio.gather(self.apply_request(preview), self.apply_request(preview))
        self.assertEqual(results[0], results[1])
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(text('SELECT count(*) FROM product_editor_audit')), 1)
            self.assertEqual(await db.scalar(text("SELECT count(*) FROM product_identifiers WHERE identifier_type='ean'")), 1)

    async def test_exact_ean_offer_is_kept_ahead_of_large_fuzzy_name_pool(self):
        async with self.transaction() as db:
            db.add(ProductIdentifier(product_id=self.product_id, identifier_type=IdentifierType.ean, value=EAN))
            source = await db.get(SupplierProduct, self.source_id)
            source.supplier_sku = 'ZZZ-EXACT-EAN'
            catalog = deepcopy(source.attributes['catalog'])
            catalog.update(code=source.supplier_sku, shop_code='NF-' + source.supplier_sku)
            source.attributes = {'catalog': catalog}
            for number in range(110):
                code = f'AAA-NAME-{number:03}'
                data = {**catalog, 'code': code, 'shop_code': 'NF-' + code,
                        'name': 'Original card name', 'eans': [], 'group_code': None}
                db.add(SupplierProduct(supplier_id=self.supplier_id, supplier_sku=code,
                    name=data['name'], attributes={'catalog': data}))
        async with self.sessions() as db:
            result = await service.candidates(db, SupplierCardLinkCandidatesRequest(
                product_id=self.product_id, supplier_code='northfinder-test'))
        self.assertEqual(result['rows'][0]['id'], self.source_id)
        self.assertIn('exact_ean', result['rows'][0]['match_reasons'])
        self.assertTrue(result['truncated'])
