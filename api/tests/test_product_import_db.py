"""Durable import staging against an isolated PostgreSQL schema and fake shops."""
import asyncio
import json
import unittest
from contextlib import asynccontextmanager
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from pydantic import SecretStr
from sqlalchemy import select, func, text

import test_catalog_db as catalog_db
from catalog_fixtures import FakeUpgates, xml_item
from test_ai_content import content
from test_ai_content_db import FakeContentShop
from inventory_hub import config_io, database
from inventory_hub.ai_content_models import AiJob
from inventory_hub.db_models import Product, ProductIdentifier, Supplier, IdentifierType
from inventory_hub.db_models_ext import ShopProduct, ProductSupplySource
from inventory_hub.product_import_models import ProductImportDraft
from inventory_hub.product_import_types import DraftCreate, DraftPatch, DraftRevision, DraftAiRequest, DraftPublish, DraftIdentityDecisions
from inventory_hub.services import product_import as service, catalog, catalog_import, ai_content_worker as worker
from inventory_hub.services import product_import_identity
from inventory_hub.settings import settings


@unittest.skipUnless(catalog_db.TEST_URL, 'Dedicated localhost *_catalog_test database required')
class ProductImportDatabaseTests(unittest.IsolatedAsyncioTestCase):
    write_feed = catalog_db.CatalogDatabaseTests.write_feed
    refresh = catalog_db.CatalogDatabaseTests.refresh

    async def asyncSetUp(self):
        await catalog_db.CatalogDatabaseTests.asyncSetUp(self)
        async with self.engine.begin() as connection:
            raw = await connection.get_raw_connection()
            for name in ('005_ai_content.sql', '012_product_editor.sql', '021_product_feed_mapping.sql', '022_product_import_drafts.sql', '023_ai_content_settings.sql'):
                sql = (Path(__file__).resolve().parents[2] / 'infra/db-init' / name).read_text()
                await raw.driver_connection.execute(sql)
                if name == '022_product_import_drafts.sql':
                    await raw.driver_connection.execute(sql)
        await self.refresh()
        self.client = FakeContentShop()
        path = config_io.shop_path('biketrek')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'upgates_api_base_url':'https://biketrek.example.test/api/v2',
                                   'upgates_login':'fixture', 'upgates_api_key':'fixture'}))
        @asynccontextmanager
        async def sessions():
            async with self.sessions() as db:
                yield db
                await db.commit()
        self.patches = [patch.object(catalog_import.UpgatesClient, 'from_shop', return_value=self.client),
            patch.object(catalog_import, 'get_session_context', sessions), patch.object(worker, 'get_session_context', sessions),
            patch.object(database, '_engine', self.engine), patch.object(settings, 'AI_CONTENT_ENABLED', True),
            patch.object(settings, 'OPENAI_API_KEY', SecretStr('fixture-no-network')), patch.object(settings, 'AI_CONTENT_MONTHLY_USD', 20)]
        for p in self.patches:
            p.start()
        async with self.sessions() as db:
            page = await catalog.catalog_page(db, 'paul-lange')
            self.ids = [item.product.id for item in page.items]

    async def asyncTearDown(self):
        for p in reversed(self.patches):
            p.stop()
        await catalog_db.CatalogDatabaseTests.asyncTearDown(self)

    def request(self, **changes):
        return DraftCreate(request_id=uuid4(), supplier='paul-lange', shop='biketrek',
            product_ids=[self.ids[0]], options={'category_code':'K-TEST'}, **changes)

    async def create(self, request=None):
        async with self.sessions() as db:
            result = await service.create(db, request or self.request())
            await db.commit()
            return result

    async def mutate(self, result, fn, request_type=DraftRevision, **kwargs):
        async with self.sessions() as db:
            draft = await service.get_draft(db, result['id'], lock=True)
            response = await fn(db, draft, request_type(expected_revision=result['revision'], **kwargs))
            await db.commit()
            return response

    async def choose_existing(self, result, product_id):
        review = await self.mutate(result, product_import_identity.review)
        return await self.mutate(result, product_import_identity.decisions, DraftIdentityDecisions,
            review_hash=review['review_hash'], decisions=[{'row_id': result['rows'][0]['id'],
                'action': 'link', 'product_id': product_id}])

    async def test_category_refresh_preserves_manual_cells_and_reads_inactive_categories(self):
        original = await self.create()
        edited = await self.mutate(original, service.patch_rows, DraftPatch,
            rows=[{'id': original['rows'][0]['id'], 'values': {'name': 'Manual name'}}])
        old_get = self.client.get
        def get(path, params=None):
            if path == 'categories':
                return {'categories': [
                    {'code': 'K-TEST', 'descriptions': [{'language': 'sk', 'name': 'Test'}]},
                    {'code': 'SADDLES', 'parent_code': 'K-TEST', 'active_yn': False,
                     'descriptions': [{'language': 'sk', 'name': 'Návleky na sedlá'}]}]}
            return old_get(path, params)
        with patch.object(self.client, 'get', side_effect=get):
            refreshed = await self.mutate(edited, service.refresh_categories)
        self.assertEqual(refreshed['rows'][0]['values'], edited['rows'][0]['values'])
        self.assertIn('name', refreshed['rows'][0]['manual_fields'])
        self.assertFalse(next(c for c in refreshed['categories'] if c['code'] == 'SADDLES')['active'])
        self.assertEqual(self.client.sent, [], 'Refreshing categories cannot publish products')
        with self.assertRaises(catalog.CatalogError) as error:
            await self.mutate(edited, service.refresh_categories)
        self.assertEqual(error.exception.code, 'import_draft_changed')

    async def test_draft_snapshot_holds_supplier_mapping_lock_until_commit(self):
        async with self.sessions() as writer:
            result = await service.create(writer, self.request())
            async with self.sessions() as competing:
                available = await competing.scalar(text('SELECT pg_try_advisory_xact_lock(:key)'), {'key':catalog._lock_key('paul-lange')})
                self.assertFalse(available, 'Mapping save or feed refresh must not interleave draft rows')
                await competing.rollback()
            await writer.commit()
        async with self.sessions() as competing:
            available = await competing.scalar(text('SELECT pg_try_advisory_xact_lock(:key)'), {'key':catalog._lock_key('paul-lange')})
            self.assertTrue(available)
            await competing.rollback()

    async def test_sloger_data_preparation_is_durable_unpaid_and_reuses_b2b_ean_identity(self):
        from test_catalog_sloger import product as sloger_xml
        from test_product_import_source import observation
        from inventory_hub.services import product_import_source as sources
        from inventory_hub.services.identifiers import ProductIdentifierService
        from inventory_hub.sloger_probe_types import SlogerBatchResult, SlogerLookupResult
        from inventory_hub.supplier_availability_models import SupplierAvailabilityObservation, SupplierAvailabilitySettings

        path = config_io.supplier_path('sloger')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'name': 'Sloger', 'feeds': {'sources': {'products': {
            'mode': 'local', 'local_path': str(self.feed)}}}, 'adapter_settings': {
            'vat_rate': 23, 'product_code_prefix': 'SLG-',
            'availability': {'orderable': 'do 2-4 dní', 'unknown': 'overíme'}}}))
        self.feed.write_text('<products>' + sloger_xml(code='7700860', ean='', price='8.50') + '</products>')
        async with self.sessions() as db:
            await catalog.refresh_catalog(db, 'sloger')
            selected = await catalog.catalog_selection(db, 'sloger')
            other = Supplier(code='existing-supplier', name='Existing supplier')
            db.add(other)
            await db.flush()
            existing = Product(sku='EXISTING-BRUSH', name='Existing brush', supplier_id=other.id)
            db.add(existing)
            await db.flush()
            await ProductIdentifierService(db).add_identifier(existing.id, '0763477003706', is_primary=True)
            existing_id = existing.id
            await db.commit()
        request = DraftCreate(request_id=uuid4(), supplier='sloger', shop='biketrek', product_ids=selected.ids,
            options={'category_code': 'K-TEST'})
        initial = await self.create(request)
        self.assertEqual(initial['source_lookup']['state'], 'queued')
        replay = await self.create(request)
        self.assertEqual(replay['id'], initial['id'])
        with self.assertRaises(catalog.CatalogError) as pending:
            await self.mutate(initial, service.save)
        self.assertEqual(pending.exception.code, 'import_source_pending')

        @asynccontextmanager
        async def sessions():
            async with self.sessions() as db:
                yield db
                await db.commit()

        calls = []
        async def lookup(codes):
            calls.append(codes)
            # Supplier network work must not retain the creation/global source
            # locks or the draft row lock while another request loads progress.
            async with self.sessions() as competing:
                for key in (691432122, catalog._lock_key('sloger')):
                    self.assertTrue(await competing.scalar(text('SELECT pg_try_advisory_xact_lock(:key)'), {'key': key}))
                await competing.execute(select(ProductImportDraft).where(ProductImportDraft.id == initial['id']).with_for_update(nowait=True))
            await asyncio.sleep(.01)
            return SlogerBatchResult(items=[SlogerLookupResult(sku='7700860', observation=observation('7700860'))])

        with patch.object(sources, 'get_session_context', sessions), \
             patch.object(sources.sloger_data, 'lookup_products', side_effect=lookup), \
             patch.object(service.ai_content, 'create_batch', AsyncMock(side_effect=AssertionError('B2B ingestion must not call AI'))):
            await asyncio.gather(sources.process(initial['id']), sources.process(initial['id']))
            await sources.process(initial['id'])
        self.assertEqual(calls, [['7700860']])
        async with self.sessions() as db:
            ready = await service.public(db, await service.get_draft(db, initial['id']))
            self.assertEqual(ready['source_lookup']['state'], 'completed')
            self.assertEqual(ready['rows'][0]['values']['code'], 'EXISTING-BRUSH')
            self.assertEqual(ready['rows'][0]['values']['purchase_net'], '5.10')
            self.assertEqual(ready['rows'][0]['values']['sale_gross'], '8.50')
            self.assertEqual(ready['rows'][0]['values']['availability'], 'do 2-4 dní')
            self.assertEqual(await db.scalar(select(func.count()).select_from(AiJob)), 0)
            self.assertEqual(await db.scalar(select(func.count()).select_from(SupplierAvailabilityObservation)), 1)
            settings_row = await db.scalar(select(SupplierAvailabilitySettings))
            self.assertFalse(settings_row.enabled)
            self.assertIsNone(settings_row.manual_requested_at)
            self.assertEqual(await db.scalar(text('SELECT count(*) FROM stock_movements')), 0)
        ready = await self.choose_existing(ready, existing_id)
        await self.mutate(ready, service.save)
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(Product)), 1)
            self.assertEqual((await db.get(Product, existing_id)).sku, 'EXISTING-BRUSH')
            self.assertEqual(await db.scalar(select(func.count()).select_from(ShopProduct)), 0)
            self.assertEqual(await db.scalar(text('SELECT count(*) FROM stock_movements')), 0)

    async def test_snapshot_replay_cas_and_hub_save_never_create_shop_or_stock(self):
        request = self.request()
        result = await self.create(request)
        replay = await self.create(request)
        self.assertEqual(result['id'], replay['id'])
        result = await self.mutate(result, service.patch_rows, DraftPatch,
            rows=[{'id': self.ids[0], 'values': {'name':'Ručne zmenený názov', 'sale_gross':'89.99'}}])
        with self.assertRaises(catalog.CatalogError) as error:
            await self.mutate(replay, service.save)
        self.assertEqual(error.exception.code, 'import_draft_changed')
        saved = await self.mutate(result, service.save)
        self.assertEqual(saved['status'], 'saved')
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(Product)), 1)
            self.assertEqual(await db.scalar(select(func.count()).select_from(ShopProduct)), 0)
            self.assertEqual(await db.scalar(select(func.count()).select_from(ProductSupplySource)), 1)
            self.assertEqual(await db.scalar(text('SELECT count(*) FROM stock_balances')), 0)
            self.assertEqual(await db.scalar(text('SELECT count(*) FROM stock_movements')), 0)
        self.assertEqual(self.client.sent, [])
        # Save replay through a current revision reuses the same canonical product.
        saved2 = await self.mutate(saved, service.save)
        self.assertEqual(saved['rows'][0]['hub_product_id'], saved2['rows'][0]['hub_product_id'])

    async def test_failed_sloger_read_can_explicitly_continue_to_feed_save_and_publication_preview(self):
        from test_catalog_sloger import product as sloger_xml
        from inventory_hub.services import product_import_source as sources
        from inventory_hub.supplier_availability_models import SupplierAvailabilityObservation

        path = config_io.supplier_path('sloger')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'name': 'Sloger', 'feeds': {'sources': {'products': {
            'mode': 'local', 'local_path': str(self.feed)}}}, 'adapter_settings': {
            'vat_rate': 23, 'product_code_prefix': 'SLG-',
            'availability': {'orderable': 'do 2-4 dní', 'unknown': 'overíme'}}}))
        self.feed.write_text('<products>' + sloger_xml(code='FEED-ONLY', price='24.60', ean='0763477003706') + '</products>')
        async with self.sessions() as db:
            await catalog.refresh_catalog(db, 'sloger')
            selected = await catalog.catalog_selection(db, 'sloger')
            await db.commit()
        initial = await self.create(DraftCreate(request_id=uuid4(), supplier='sloger', shop='biketrek',
            product_ids=selected.ids, options={'category_code': 'K-TEST'}))

        @asynccontextmanager
        async def sessions():
            async with self.sessions() as db:
                yield db
                await db.commit()

        with patch.object(sources, 'get_session_context', sessions), \
             patch.object(sources.sloger_data, 'lookup_products', AsyncMock(side_effect=
                 catalog.CatalogError('sloger_b2b_unavailable', 'Unavailable', 503))):
            await sources.process(initial['id'])
        async with self.sessions() as db:
            failed = await service.public(db, await service.get_draft(db, initial['id']))
        self.assertTrue(failed['source_lookup']['skippable'])
        with self.assertRaises(catalog.CatalogError) as blocked:
            await self.mutate(failed, service.save)
        self.assertEqual(blocked.exception.code, 'import_source_failed')
        with self.assertRaises(catalog.CatalogError) as stale:
            await self.mutate(initial, sources.skip)
        self.assertEqual(stale.exception.code, 'import_draft_changed')
        skipped = await self.mutate(failed, sources.skip)
        self.assertEqual(skipped['source_lookup']['state'], 'skipped')
        self.assertEqual(skipped['source_lookup']['skipped'], 1)
        self.assertEqual(skipped['rows'][0]['source_error'], 'sloger_b2b_unavailable')
        self.assertIn('sloger_b2b_feed_fallback', skipped['rows'][0]['warnings'])
        edited = await self.mutate(skipped, service.patch_rows, DraftPatch,
            rows=[{'id': selected.ids[0], 'values': {'name': 'Feed product after unavailable B2B'}}])
        saved = await self.mutate(edited, service.save)
        previewed = await self.mutate(saved, service.preview)
        payload = previewed['publication']['items'][0]['payload']
        self.assertEqual(payload['descriptions'][0]['title'], 'Feed product after unavailable B2B')
        self.assertEqual(payload['code'], 'SLG-FEED-ONLY')
        self.assertEqual(payload['availability'], 'overíme')
        self.assertNotIn('price_purchase', payload['prices'][0])
        self.assertEqual(previewed['rows'][0]['values']['sale_gross'], '24.60')
        self.assertFalse(previewed['rows'][0]['values']['ai_enabled'])
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(Product)), 1)
            self.assertEqual(await db.scalar(select(func.count()).select_from(ShopProduct)), 0)
            self.assertEqual(await db.scalar(select(func.count()).select_from(AiJob)), 0)
            self.assertEqual(await db.scalar(select(func.count()).select_from(SupplierAvailabilityObservation)), 0)
            self.assertEqual(await db.scalar(text('SELECT count(*) FROM stock_movements')), 0)
        self.assertEqual(self.client.sent, [])

    async def test_explicit_publication_uses_edited_snapshot_after_feed_refresh(self):
        result = await self.create()
        result = await self.mutate(result, service.patch_rows, DraftPatch, rows=[{'id':self.ids[0], 'values': {
            'name':'Môj názov', 'description_html':'<p>Ručný popis</p>', 'seo_title':'Moje SEO',
            'seo_description':'Presný popis z tabuľky', 'brand':'Nová značka', 'sale_gross':'89.99',
            'images':['https://images.example.com/edited.jpg'], 'parameters':[{'name':'Farba','value':'modrá'}]}}])
        result = await self.mutate(result, service.save)
        self.write_feed(xml_item(name='Updated supplier name'))
        await self.refresh()
        result = await self.mutate(result, service.preview)
        preview = result['publication']
        payload = preview['items'][0]['payload']
        self.assertEqual(payload['descriptions'][0]['title'], 'Môj názov')
        self.assertEqual(payload['manufacturer'], 'Nová značka')
        self.assertEqual(payload['images'][0]['url'], 'https://images.example.com/edited.jpg')
        self.assertEqual(payload['parameters'][0]['values'][0]['descriptions'][0]['value'], 'modrá')
        with self.assertRaises(catalog.CatalogError) as error:
            catalog_import.queue_import('biketrek', preview['preview_id'])
        self.assertEqual(error.exception.code, 'import_staging_confirmation_required')
        published, run = await self.mutate(result, service.publish, DraftPublish, preview_id=preview['preview_id'])
        self.assertTrue(run)
        await catalog_import.execute_import('biketrek', preview['preview_id'])
        outcome = catalog_import.import_result('biketrek', preview['preview_id'])
        self.assertEqual(outcome['items'][0]['status'], 'created', outcome)
        self.assertEqual(len([sent for sent in self.client.sent if sent[0]=='products']), 1)
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(ShopProduct)), 1)
            self.assertEqual(await db.scalar(text('SELECT count(*) FROM stock_movements')), 0)

    async def test_saved_existing_product_is_not_overwritten(self):
        first = await self.mutate(await self.create(), service.save)
        second = await self.create()
        second = await self.mutate(second, service.patch_rows, DraftPatch, rows=[{'id':self.ids[0], 'values':{'name':'Should not replace existing'}}])
        saved = await self.mutate(second, service.save)
        self.assertIn('already_in_hub_unchanged', saved['rows'][0]['warnings'])
        self.assertEqual(saved['rows'][0]['hub_product_id'], first['rows'][0]['hub_product_id'])
        from inventory_hub.product_editor_models import ProductEditorOverride
        async with self.sessions() as db:
            override = await db.get(ProductEditorOverride, first['rows'][0]['hub_product_id'])
            self.assertNotEqual(override.data['common']['name'], 'Should not replace existing')
        # Reopening the same preparation saves merchandising, not duplicate EANs.
        repeated = await self.mutate(first, service.save)
        async with self.sessions() as db:
            barcodes = (await db.scalars(select(ProductIdentifier).where(
                ProductIdentifier.product_id == repeated['rows'][0]['hub_product_id'],
                ProductIdentifier.identifier_type.in_([IdentifierType.ean, IdentifierType.upc, IdentifierType.unverified_barcode])))).all()
            self.assertEqual(len(barcodes), len(first['rows'][0]['values']['eans']))
            self.assertEqual(sum(identifier.is_primary for identifier in barcodes), 1)

    async def receiving_draft(self, sources, lineage, *, request=None):
        request = request or DraftCreate(request_id=uuid4(), supplier='paul-lange', shop='biketrek',
            feed_key='receiving', product_ids=[p.id for p in sources], options={'category_code': 'K-TEST'})
        async with self.sessions() as db:
            result = await service.create(db, request, sources=sources,
                receiving_origin={'kind': 'receiving', 'supplier': 'paul-lange', 'session_id': 123, 'invoice_no': 'TEST-INVOICE'},
                receiving_rows=lineage)
            await db.commit()
        return result

    async def test_invoice_only_draft_uses_frozen_facts_without_catalog_fk_and_can_prepare_ai(self):
        from inventory_hub.catalog_types import CatalogProduct, CatalogPrices
        from inventory_hub.db_models import SupplierProduct
        from inventory_hub.services import receiving_products
        # This ID deliberately collides with an unrelated real supplier product.
        source = CatalogProduct(id=self.ids[0], supplier='paul-lange', feed_key='invoice',
            code='INVOICE-ONLY', shop_code='PL-INVOICE-ONLY', name='Invoice-only card',
            prices=CatalogPrices(currency='EUR', purchase_net='7.13'))
        lineage = {source.id: {'line_ids': [source.id, 999], 'catalog_product_id': None, 'existing_product_id': None}}
        with patch.object(service, 'selected_products', AsyncMock(side_effect=AssertionError('Do not look up receipt IDs as feed IDs'))):
            draft = await self.receiving_draft([source], lineage)
            self.assertEqual(draft['receiving_origin']['session_id'], 123)
            self.assertEqual(draft['rows'][0]['source_kind'], 'invoice')
            self.assertEqual(draft['rows'][0]['values']['name'], 'Invoice-only card')
            with patch.object(receiving_products, 'bind_imported_products', AsyncMock()):
                saved = await self.mutate(draft, service.save)
            with self.assertRaises(catalog.CatalogError) as invalid:
                await self.mutate(saved, service.preview)
            self.assertEqual(invalid.exception.code, 'import_invalid_rows')
            async with self.sessions() as db:
                card = await db.get(Product, saved['rows'][0]['hub_product_id'])
                self.assertIsNone(card.source_supplier_product_id)
                self.assertEqual(card.sku, 'PL-INVOICE-ONLY')
                self.assertEqual(await db.scalar(select(func.count()).select_from(SupplierProduct)), 3)
                self.assertEqual(await db.scalar(select(func.count()).select_from(ProductSupplySource)), 0)
                self.assertEqual(await db.scalar(text('SELECT count(*) FROM stock_movements')), 0)
            edited = await self.mutate(saved, service.patch_rows, DraftPatch, rows=[{'id': source.id,
                'values': {'sale_gross': '20.00', 'vat_percent': '23', 'ai_enabled': True, 'ai_category_profile': 'general'}}])
            prepared = await self.mutate(edited, service.prepare_ai, DraftAiRequest)
            async with self.sessions() as db:
                job = await db.get(AiJob, prepared['rows'][0]['ai_job']['id'])
                self.assertEqual(job.context['staging_id'], draft['id'])
                self.assertEqual(job.context['facts'][0]['name'], 'Invoice-only card')
                self.assertEqual(job.context['research'], 'official')

    async def test_receiving_enrichment_preserves_manual_card_and_blocks_concurrent_editor_change(self):
        from inventory_hub.product_editor_models import ProductEditorOverride
        from inventory_hub.services import receiving_products
        async with self.sessions() as db:
            source = (await catalog.selected_products(db, 'paul-lange', 'products', [self.ids[0]]))[0]
            card = Product(sku='ORIGINAL-CARD', supplier_id=await db.scalar(select(Supplier.id).where(Supplier.code == 'paul-lange')),
                name='Invoice item', created_from_source='invoice:TEST-INVOICE', validation_required=True)
            db.add(card)
            await db.flush()
            db.add(ProductIdentifier(product_id=card.id, supplier_id=card.supplier_id,
                identifier_type=IdentifierType.supplier_sku, value=source.code, is_primary=False))
            from inventory_hub.services.identifiers import ProductIdentifierService
            identifiers = ProductIdentifierService(db)
            await identifiers.add_identifier(card.id, '0763477003706', is_primary=True)
            for ean in source.eans:
                await identifiers.add_identifier(card.id, ean, is_primary=False)
            db.add(ProductEditorOverride(product_id=card.id, revision=1,
                data={'common': {'name': 'Manual card name'}, 'variant': {'sale_price_gross': '77.00'}}))
            card_id = card.id
            await db.commit()
        lineage = {source.id: {'line_ids': [source.id], 'catalog_product_id': source.id, 'existing_product_id': card_id}}
        draft = await self.receiving_draft([source], lineage)
        self.assertEqual(draft['rows'][0]['values']['code'], 'ORIGINAL-CARD')
        self.assertEqual(draft['rows'][0]['values']['name'], 'Manual card name')
        self.assertEqual(draft['rows'][0]['values']['sale_gross'], '77.00')
        self.assertIn('name', draft['rows'][0]['manual_fields'])
        with patch.object(receiving_products, 'bind_imported_products', AsyncMock()):
            saved = await self.mutate(draft, service.save)
            saved = await self.mutate(saved, service.save)
            self.assertEqual(saved['rows'][0]['hub_product_id'], card_id)
            async with self.sessions() as db:
                self.assertEqual(await db.scalar(select(func.count()).select_from(Product)), 1)
                self.assertEqual((await db.get(Product, card_id)).sku, 'ORIGINAL-CARD')
                primary = await db.scalar(select(ProductIdentifier.value).where(
                    ProductIdentifier.product_id == card_id, ProductIdentifier.is_primary.is_(True)))
                self.assertEqual(primary, '0763477003706', 'Enrichment must retain the invoice card primary barcode')
                override = await db.get(ProductEditorOverride, card_id)
                override.revision += 1
                override.data = {**override.data, 'common': {'name': 'New manual edit'}}
                await db.commit()
            with self.assertRaises(catalog.CatalogError) as changed:
                await self.mutate(saved, service.save)
            self.assertEqual(changed.exception.code, 'import_hub_product_changed')
        async with self.sessions() as db:
            self.assertEqual((await db.get(ProductEditorOverride, card_id)).data['common']['name'], 'New manual edit')
            self.assertEqual(await db.scalar(text('SELECT count(*) FROM stock_movements')), 0)

    async def test_pristine_receiving_card_joins_only_selected_explicit_feed_family(self):
        from inventory_hub.catalog_types import CatalogParameter
        from inventory_hub.db_models import ProductGroup
        from inventory_hub.db_models_ext import ProductVariantAttribute
        from inventory_hub.services import receiving_products
        async with self.sessions() as db:
            sources = await catalog.selected_products(db, 'paul-lange', 'products', self.ids[:2])
            sources = [p.model_copy(update={'id': 100 + index, 'group_code': 'RECEIPT-FAMILY', 'group_name': 'Receipt family',
                'variant_relationship': 'explicit', 'variant_attributes': [CatalogParameter(name='Size', value=str(index))]})
                for index, p in enumerate(sources)]
            supplier_id = await db.scalar(select(Supplier.id).where(Supplier.code == 'paul-lange'))
            existing = Product(sku='OTHER-SUPPLIER-CARD', name='Invoice item', supplier_id=supplier_id,
                created_from_source='invoice:TEST-INVOICE', validation_required=True)
            db.add(existing)
            await db.flush()
            db.add(ProductIdentifier(product_id=existing.id, supplier_id=supplier_id,
                identifier_type=IdentifierType.supplier_sku, value=sources[0].code, is_primary=False))
            existing_id = existing.id
            await db.commit()
        lineage = {p.id: {'line_ids': [p.id], 'catalog_product_id': self.ids[index],
            'existing_product_id': existing_id if index == 0 else None} for index, p in enumerate(sources)}
        draft = await self.receiving_draft(sources, lineage)
        self.assertEqual({row['group_key'] for row in draft['rows']}, {'PL-G-RECEIPT-FAMILY'})
        with patch.object(receiving_products, 'bind_imported_products', AsyncMock()):
            saved = await self.mutate(draft, service.save)
        async with self.sessions() as db:
            cards = (await db.scalars(select(Product))).all()
            self.assertEqual(len(cards), 2)
            self.assertEqual(len({card.group_id for card in cards}), 1)
            self.assertEqual((await db.get(Product, existing_id)).sku, 'OTHER-SUPPLIER-CARD')
            self.assertEqual((await db.get(ProductGroup, cards[0].group_id)).code, 'PL-G-RECEIPT-FAMILY')
            self.assertEqual(await db.scalar(select(func.count()).select_from(ProductVariantAttribute)), 2)
            self.assertEqual(await db.scalar(text('SELECT count(*) FROM stock_movements')), 0)

    async def test_per_row_two_shop_publication_and_hub_only_save_are_replay_safe(self):
        clients = {'biketrek': self.client, 'xtrek': FakeContentShop()}
        path = config_io.shop_path('xtrek')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'upgates_api_base_url': 'https://xtrek.example.test/api/v2',
                                   'upgates_login': 'fixture', 'upgates_api_key': 'fixture'}))
        with patch.object(catalog_import.UpgatesClient, 'from_shop', side_effect=lambda shop: clients[shop]):
            request = DraftCreate(request_id=uuid4(), supplier='paul-lange', shop='biketrek', product_ids=self.ids[:2],
                                  options={'category_code': 'K-TEST'})
            result = await self.create(request)
            self.assertTrue(all(row['values']['target_shops'] == ['biketrek'] for row in result['rows']))
            selected, local_only = result['rows']
            result = await self.mutate(result, service.patch_rows, DraftPatch, rows=[
                {'id': selected['id'], 'values': {'target_shops': ['biketrek', 'xtrek'], 'target_category_codes': {'xtrek': 'K-TEST'}}},
                {'id': local_only['id'], 'values': {'target_shops': []}}])
            result = await self.mutate(result, service.save)
            result = await self.mutate(result, service.preview)
            self.assertEqual(set(result['publications']), {'biketrek', 'xtrek'})
            original_previews = {shop: value['preview']['preview_id'] for shop, value in result['publications'].items()}
            for value in result['publications'].values():
                self.assertEqual(value['preview']['items'][0]['product_ids'], [selected['id']])
            result, tasks = await self.mutate(result, service.publish, DraftPublish, preview_id=result['publication']['preview_id'])
            self.assertEqual(set(tasks), set(original_previews.items()))
            await service.execute_publications(tasks)
            async with self.sessions() as db:
                result = await service.public(db, await service.get_draft(db, result['id']))
                self.assertEqual(result['publication_state'], 'completed', result)
                self.assertEqual(await db.scalar(select(func.count()).select_from(Product)), 2)
                self.assertEqual(await db.scalar(select(func.count()).select_from(ShopProduct)), 2)
                self.assertEqual(await db.scalar(text('SELECT count(*) FROM stock_movements')), 0)
            result, repeated = await self.mutate(result, service.publish, DraftPublish, preview_id=result['publication']['preview_id'])
            self.assertFalse(repeated)
            for client in clients.values():
                self.assertEqual(len([sent for sent in client.sent if sent[0] == 'products']), 1)
            self.assertEqual({shop: value['preview']['preview_id'] for shop, value in result['publications'].items()}, original_previews)

    async def test_hub_only_existing_canonical_product_gains_proved_supplier_alias(self):
        from inventory_hub.services.identifiers import ProductIdentifierService
        # The default catalog fixture deliberately has an unverified barcode.
        # Cross-supplier reuse requires a checksum-validated EAN, exactly as
        # receiving and the shared identity resolver do in production.
        self.write_feed(xml_item(ean='0000000000017'))
        await self.refresh()
        async with self.sessions() as db:
            source = (await catalog.selected_products(db, 'paul-lange', 'products', [self.ids[0]]))[0]
            self.assertEqual(ProductIdentifierService.classify_barcode(source.eans[0]), IdentifierType.ean)
            other = Supplier(code='other-fixture', name='Other supplier')
            db.add(other)
            await db.flush()
            original = Product(sku='EXISTING-001', name='Existing name', supplier_id=other.id)
            db.add(original)
            await db.flush()
            product_id = original.id
            await ProductIdentifierService(db).add_identifier(product_id, source.eans[0], is_primary=True)
            await db.commit()
        result = await self.create()
        self.assertEqual(result['rows'][0]['values']['code'], 'EXISTING-001')
        result = await self.mutate(result, service.patch_rows, DraftPatch,
            rows=[{'id': self.ids[0], 'values': {'target_shops': []}}])
        result = await self.choose_existing(result, product_id)
        result = await self.mutate(result, service.save)
        async with self.sessions() as db:
            supplier_id = await db.scalar(select(Supplier.id).where(Supplier.code == 'paul-lange'))
            alias = await db.scalar(select(ProductIdentifier).where(ProductIdentifier.supplier_id == supplier_id,
                ProductIdentifier.identifier_type == IdentifierType.supplier_sku, ProductIdentifier.value == source.code))
            self.assertEqual(alias.product_id, product_id)
            self.assertEqual((await db.get(Product, product_id)).sku, 'EXISTING-001')
            self.assertEqual((await db.get(Product, product_id)).name, 'Existing name')
            self.assertEqual(await db.scalar(select(func.count()).select_from(ProductSupplySource)), 1)
            self.assertEqual(await db.scalar(select(func.count()).select_from(ShopProduct)), 0)
        result = await self.mutate(result, service.patch_rows, DraftPatch,
            rows=[{'id': self.ids[0], 'values': {'target_shops': ['biketrek']}}])
        result = await self.mutate(result, service.save)
        result = await self.mutate(result, service.preview)
        self.assertEqual(result['publication']['items'][0]['payload']['code'], 'EXISTING-001')
        self.assertFalse([sent for sent in self.client.sent if sent[0] == 'products'])

    async def test_ai_reads_mapped_edited_facts_and_preserves_later_manual_work(self):
        result = await self.create()
        result = await self.mutate(result, service.patch_rows, DraftPatch, rows=[{'id':self.ids[0], 'values':{
            'description_html':'<p>Mapped technical fact</p>', 'ai_enabled':True, 'ai_category_profile':'general'}}])
        result = await self.mutate(result, service.prepare_ai, DraftAiRequest, research='feed_only')
        job_id = result['rows'][0]['ai_job']['id']
        async with self.sessions() as db:
            job = await db.get(AiJob, job_id)
            self.assertIn('Mapped technical fact', job.context['facts'][0]['description'])
            self.assertEqual(job.status, 'estimate')
            self.assertEqual(job.context['options']['category_code'], 'K-TEST')
        result = await self.mutate(result, service.start_ai)
        result = await self.mutate(result, service.patch_rows, DraftPatch, rows=[{'id':self.ids[0], 'values':{'name':'Edited during AI'}}])
        output = content(evidence=[{'claim':'Mapped technical fact','source':f'feed:{self.ids[0]}','quote':'Mapped technical fact'}])
        response = {'id':'testresponse','status':'completed','usage':{'input_tokens':100,'output_tokens':100},
            'output':[{'type':'message','content':[{'type':'output_text','text':output.model_dump_json()}]}]}
        with patch.object(worker.provider, 'generate', AsyncMock(return_value=response)) as generate:
            await worker.generation(job_id)
            self.assertEqual(generate.call_count, 1)
        result = await self.mutate(result, service.save)
        with self.assertRaises(catalog.CatalogError) as pending:
            await self.mutate(result, service.preview)
        self.assertEqual(pending.exception.code, 'import_ai_not_applied')
        applied = await self.mutate(result, service.apply_ai)
        value = applied['rows'][0]['values']
        self.assertEqual(value['name'], 'Edited during AI')
        self.assertEqual(value['description_html'], '<p>Mapped technical fact</p>')
        self.assertEqual(value['seo_title'], output.seo_title)
        self.assertEqual(value['category_code'], 'K-TEST')
        self.assertEqual(self.client.sent, [])
        async with self.sessions() as db:
            job = await db.get(AiJob, job_id)
            self.assertEqual(job.status, 'completed')
            self.assertIsNone(job.preview_id)
        saved = await self.mutate(applied, service.save)
        prepared = await self.mutate(saved, service.preview)
        published, run = await self.mutate(prepared, service.publish, DraftPublish, preview_id=prepared['publication']['preview_id'])
        self.assertTrue(run)
        await catalog_import.execute_import('biketrek', prepared['publication']['preview_id'])
        async with self.sessions() as db:
            from inventory_hub.services import ai_content
            job = await db.get(AiJob, job_id)
            summary = (await ai_content.summaries(db, [job], detail=True))[0]
            self.assertEqual(summary['staging']['publication_status'], 'completed')
            self.assertTrue(summary['staging']['ai_applied'])
            draft = await service.get_draft(db, saved['id'])
            final = await service.public(db, draft)
            self.assertEqual(final['publication_state'], 'completed')
            self.assertEqual(final['rows'][0]['publication_status'], 'created')
            self.assertEqual((await service.history(db))['items'][0]['publication_state'], 'completed')

    async def test_inherited_automatic_policy_creates_one_active_product_without_manual_steps(self):
        from inventory_hub.ai_content_models import AiRuleVersion
        from inventory_hub.ai_content_types import Policy, RuleBook
        from inventory_hub.services import ai_content_rules
        async with self.sessions() as db:
            current = await ai_content_rules.published(db)
            rules = RuleBook.model_validate(current.book)
            rules.rules[0].policy = Policy(review_required=False, active_after_import=True,
                show_cost_estimate=False, confirm_import=False)
            version = AiRuleVersion(book=rules.model_dump(), origin='test', note='Automatic policy inheritance')
            db.add(version)
            await db.flush()
            await ai_content_rules.publish(db, version.id, current.id)
            await db.commit()
        result = await self.create()
        result = await self.mutate(result, service.patch_rows, DraftPatch, rows=[{'id': self.ids[0], 'values': {
            'ai_enabled': True, 'ai_category_profile': 'general'}}])
        result = await self.mutate(result, service.prepare_ai, DraftAiRequest, research='feed_only')
        job_id = result['rows'][0]['ai_job']['id']
        self.assertEqual(result['rows'][0]['ai_job']['status'], 'queued')
        self.assertEqual(result['rows'][0]['ai_job']['policy'], dict(review_required=False, active_after_import=True,
            show_cost_estimate=False, confirm_import=False))
        self.assertNotIn('run', result['rows'][0]['ai_job']['origins'].values())
        output = content(evidence=[{'claim': 'Popis', 'source': f'feed:{self.ids[0]}', 'quote': 'Popis'}])
        response = {'id': 'automatic-fixture', 'status': 'completed', 'usage': {'input_tokens': 100, 'output_tokens': 100},
            'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': output.model_dump_json()}]}]}
        with patch.object(worker.provider, 'generate', AsyncMock(return_value=response)) as generate:
            await worker.cycle()  # One paid generation, fake provider.
            await worker.cycle()  # Apply, save, preview and authorize once.
            async with self.sessions() as db:
                pending = await db.get(AiJob, job_id)
                self.assertEqual(pending.status, 'import_queued', pending.error)
                self.assertTrue(service.applied_ai(pending))
            await worker.cycle()  # Existing create-only importer.
            await worker.cycle()  # No duplicate generation or publication.
            self.assertEqual(generate.await_count, 1)
        sent = [payload for path, payload in self.client.sent if path == 'products']
        self.assertEqual(len(sent), 1)
        payload = sent[0]['products'][0]
        self.assertTrue(payload['active_yn'])
        self.assertTrue(payload['descriptions'][0]['active_yn'])
        self.assertEqual(next(m['value'] for m in payload['metas'] if m['key'] == 'validation_required'), '0')
        async with self.sessions() as db:
            job = await db.get(AiJob, job_id)
            self.assertEqual(job.status, 'completed', job.error)
            final = await service.public(db, await service.get_draft(db, result['id']))
            self.assertEqual(final['publication_state'], 'completed')
            self.assertTrue(final['rows'][0]['ai_job']['staging']['ai_applied'])
            self.assertEqual(await db.scalar(text('SELECT count(*) FROM stock_movements')), 0)
