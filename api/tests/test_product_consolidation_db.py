"""Real PostgreSQL retirement, current owner projection and stock-race gates."""
import asyncio
import json
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit
from uuid import uuid4
import asyncpg
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import aliased
from sqlalchemy.pool import NullPool
from inventory_hub import config_io
from inventory_hub.db_models import IdentifierType, Product, ProductIdentifier, Shop, Supplier, SupplierProduct, Warehouse
from inventory_hub.db_models_ext import ShopProduct, StockBalance, StockMovement
from inventory_hub.product_consolidation_models import ProductCanonicalAlias, ProductConsolidation
from inventory_hub.product_consolidation_types import ProductConsolidationApplyRequest, ProductConsolidationPreviewRequest
from inventory_hub.product_link_models import ProductLinkPreview
from inventory_hub.services import product_consolidation as service, product_links as links
from inventory_hub.services import product_editor, sku_policy
from inventory_hub.services import product_candidates, supplier_card_links
from inventory_hub.services.canonical_products import ProductRetiredError
from inventory_hub.services.identifiers import IdentifierConflict, ProductIdentifierService
from inventory_hub.services.product_identity import IDENTITY_WRITE_LOCK, RemoteIdentity, load_identity_index
from inventory_hub.services.stock_balances import lock_stock_balances
from inventory_hub.settings import settings


TEST_URL = os.environ.get('CATALOG_TEST_DATABASE_URL', '')
EAN = '0727670929720'
UPC = '727670929720'
OTHER_EAN = '4006381333931'


@unittest.skipUnless(TEST_URL, 'Dedicated localhost *_catalog_test database required')
class ProductConsolidationDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        parsed = urlsplit(TEST_URL)
        if parsed.hostname not in ('localhost', '127.0.0.1') or not parsed.path.endswith('_catalog_test'):
            raise RuntimeError('Consolidation tests require a dedicated localhost *_catalog_test database')
        self.schema = 'consolidation_' + uuid4().hex
        self.sql_root = Path(__file__).resolve().parents[2] / 'infra' / 'db-init'
        connection = await asyncpg.connect(TEST_URL)
        try:
            await connection.execute(f'CREATE SCHEMA "{self.schema}"')
            await connection.execute(f'SET search_path TO "{self.schema}"')
            # Exercise the complete installed schema, including JSON-only
            # operation tables and the47 reference guards, not mock counts.
            for path in sorted(self.sql_root.glob('*.sql')):
                if path.name.startswith('002_'):
                    #002's historical enum existence check is not schema-scoped.
                    await connection.execute("CREATE TYPE payment_status AS ENUM ('unpaid','partial','paid')")
                await connection.execute(path.read_text())
        finally:
            await connection.close()
        self.engine = create_async_engine(TEST_URL.replace('postgresql://', 'postgresql+asyncpg://', 1),
            poolclass=NullPool, connect_args={'server_settings': {'search_path': self.schema}})
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False, autoflush=False)
        self.directory = TemporaryDirectory()
        self.patches = [patch.object(settings, 'INVENTORY_DATA_ROOT', Path(self.directory.name)),
            patch.object(config_io, 'DATA_ROOT', Path(self.directory.name)),
            patch.object(links.source, 'discover', AsyncMock(side_effect=self.discover)),
            patch.object(links.source, 'verify', AsyncMock(side_effect=self.verify)),
            patch.object(links.source, 'current_fingerprint', AsyncMock(return_value='a'*64)),
            patch.object(links.source, 'verify_unoccupied_codes', AsyncMock(side_effect=self.code_absence)),
            patch.object(links, '_verify_replacement_absence', AsyncMock(side_effect=self.absence))]
        for patcher in self.patches:
            patcher.start()
        self.absent_ids = set()
        async with self.transaction() as db:
            self.shop_id = await db.scalar(select(Shop.id).where(Shop.code == 'biketrek'))
            self.warehouse_id = await db.scalar(select(Warehouse.id).where(Warehouse.code == 'hlavny-sklad'))
            cards = [Product(sku=code, name='Synthetic identical retail unit', created_from_source='upgates:biketrek')
                     for code in ('SYN-KEEP', 'SYN-OLD')]
            db.add_all(cards)
            await db.flush()
            self.keeper, self.retired = [row.id for row in cards]
            db.add_all([ProductIdentifier(product_id=self.keeper, identifier_type=IdentifierType.ean, value=EAN),
                        ProductIdentifier(product_id=self.retired, identifier_type=IdentifierType.upc, value=UPC)])
            mappings = [ShopProduct(shop_id=self.shop_id, product_id=card.id, external_code=card.sku,
                        is_variant=False, shop_price=Decimal('12.34'), shop_stock=Decimal('3')) for card in cards]
            db.add_all(mappings)
            await db.flush()
            self.mapping_ids = [row.id for row in mappings]
            db.add(StockBalance(product_id=self.keeper, warehouse_id=self.warehouse_id, qty_on_hand=Decimal(3),
                qty_reserved=0, qty_quarantined=0, avg_cost=Decimal('1.2345'), total_value=Decimal('3.7035')))
            db.add(StockMovement(idempotency_key='synthetic-legacy-initial', product_id=self.keeper,
                warehouse_id=self.warehouse_id, movement_type='INITIAL', quantity=Decimal(3),
                unit_cost=Decimal('1.2345'), balance_after=Decimal(3), avg_cost_after=Decimal('1.2345'),
                reference_type='upgates_import', reference_source='upgates:biketrek',
                reference_id='SYN-KEEP', created_by='upgates_import'))
        self.remote = [self.leaf('SYN-KEEP', 9001), self.leaf('SYN-OLD', 9002)]

    async def asyncTearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()
        self.directory.cleanup()
        await self.engine.dispose()
        connection = await asyncpg.connect(TEST_URL)
        try:
            await connection.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        finally:
            await connection.close()

    @asynccontextmanager
    async def transaction(self):
        async with self.sessions() as db:
            try:
                yield db
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    def leaf(self, code, native_id):
        return {'code': code, 'parent_code': code, 'is_variant': False, 'external_id': str(native_id),
                'parent_id': str(native_id), 'name': 'Synthetic retail unit', 'eans': [EAN],
                'raw_barcodes': [EAN], 'invalid_barcodes': [], 'attributes': []}

    async def discover(self, shop_code, codes, **kwargs):
        return {'fingerprint': 'a'*64, 'requests': 2,
                'leaves': deepcopy([row for row in self.remote if row['code'] in codes])}

    async def verify(self, shop_code, leaves, fingerprint, **kwargs):
        return await self.discover(shop_code, [row['code'] for row in leaves])

    async def absence(self, shop_code, rows, fingerprint, **kwargs):
        return {'fingerprint': fingerprint, 'requests': 2,
                'existing_mapping_ids': [row['id'] for row in rows if row['id'] not in self.absent_ids]}

    async def code_absence(self, shop_code, codes, fingerprint, **kwargs):
        if any(row['code'].casefold() in {code.casefold() for code in codes} for row in self.remote):
            raise links.source.SourceError('source_code_not_proven_absent', 409)
        return {'fingerprint': fingerprint, 'codes': sorted(set(codes)), 'requests': 4,
            'endpoint': '/products/simple', 'checked_at': links.now().isoformat(), 'checks': [
                {'namespace': namespace, 'filter': field, 'archived_yn': archived, 'matched_rows': 0}
                for namespace, field in (('product', 'codes'), ('variant', 'variant_codes'))
                for archived in (False, True)]}

    def request(self):
        return ProductConsolidationPreviewRequest(request_id=uuid4(), keeper_product_id=self.keeper,
            retired_product_ids=[self.retired], mode='identity_only_preserve_unconfirmed_history',
            same_physical_retail_unit_confirmed=True, legacy_initial_history_preserved=True,
            identity_evidence={'review_note': 'Synthetic manufacturer proof: both cards are the identical finished retail unit.',
                'physical_unit': 'One complete synthetic retail unit', 'source_urls': ['https://manufacturer.invalid/synthetic']},
            mapping_actions=[{'mapping_id': mapping_id, 'action': 'bind', 'target': {
                'shop_code': 'biketrek', 'code': leaf['code'], 'parent_code': leaf['parent_code'], 'is_variant': False,
                'native_product_id': int(leaf['parent_id']), 'sale_to_stock_factor': 1}}
                for mapping_id, leaf in zip(self.mapping_ids, self.remote)])

    async def preview(self, request=None):
        async with self.transaction() as db:
            return await service.preview(db, request or self.request())

    async def apply(self, preview):
        async with self.transaction() as db:
            return await service.apply(db, preview['id'], ProductConsolidationApplyRequest(
                preview_hash=preview['preview_hash'], confirmed=True))

    async def stock(self):
        result = {}
        async with self.transaction() as db:
            for table in ('stock_balances', 'stock_movements', 'stock_tracking', 'fifo_layers', 'fifo_states',
                          'stock_sync_dirty', 'shop_sync_outbox'):
                result[table] = await db.scalar(text(f"SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text),'[]'::jsonb) FROM {table} t"))
        return result

    def unverified_request(self):
        request = self.request().model_dump(mode='json')
        request['mapping_actions'] = [{'mapping_id': mapping_id,
            'action': 'retire_unverified_code_absent', 'target': None} for mapping_id in self.mapping_ids]
        return ProductConsolidationPreviewRequest(**request)

    async def test_unverified_code_claim_retirement_preserves_null_ids_and_original_history(self):
        self.remote = []
        request = self.unverified_request()
        before = await self.stock()
        async with self.transaction() as db:
            original = (await db.execute(text('''SELECT id, to_jsonb(sp)-'retired_at'
                -'retired_by_consolidation_id'-'updated_at' AS value FROM shop_products sp ORDER BY id'''))).all()
        preview = await self.preview(request)
        self.assertTrue(preview['eligible'], preview['dependencies']['blockers'])
        self.assertEqual(preview['plan']['absence'], {})
        self.assertEqual(preview['native']['biketrek']['absent_mapping_ids'], [])
        self.assertEqual(preview['native']['biketrek']['requests'], 4)
        for claim in preview['existing_mappings']:
            self.assertIs(claim['legacy_external_id_column_present'], True)
            self.assertIs(claim['legacy_external_id_sql_null'], True)
            self.assertIsNone(claim['external_product_id'])
            self.assertIsNone(claim['external_variant_id'])
            self.assertIsNone(claim['legacy_external_id'])
        done = await self.apply(preview)
        self.assertEqual(done['status'], 'completed')
        self.assertEqual(done['result']['current_mappings'], [])
        self.assertFalse(done['result']['physical_stock_changed'])
        self.assertFalse(done['result']['remote_write_performed'])
        fresh = done['result']['unverified_code_absence']['biketrek']
        self.assertEqual(fresh['mapping_ids'], self.mapping_ids)
        self.assertEqual(fresh['codes'], ['SYN-KEEP', 'SYN-OLD'])
        self.assertEqual(fresh['old_native_identity_status'], 'unverified')
        self.assertIs(fresh['native_id_absence_proven'], False)
        self.assertIs(fresh['exact_code_absence_proven'], True)
        self.assertNotEqual(fresh['checked_at'], preview['native']['biketrek']['unverified_code_absence']['checked_at'])
        self.assertEqual(await self.stock(), before)
        async with self.transaction() as db:
            self.assertEqual((await db.execute(text('''SELECT id, to_jsonb(sp)-'retired_at'
                -'retired_by_consolidation_id'-'updated_at' AS value FROM shop_products sp ORDER BY id'''))).all(), original)
            self.assertEqual(list(await db.scalars(select(ShopProduct))), [])
            self.assertEqual(await db.scalar(select(func.count()).select_from(ProductCanonicalAlias)), 1)
        links._verify_replacement_absence.assert_not_awaited()
        links.source.discover.assert_not_awaited()
        links.source.verify.assert_not_awaited()
        self.assertEqual(links.source.verify_unoccupied_codes.await_count, 2)
        self.assertEqual(await self.preview(request), done)
        self.assertEqual(await self.apply(preview), done)
        self.assertEqual(links.source.verify_unoccupied_codes.await_count, 2)

    async def test_unverified_retirement_rejects_nonnull_raw_legacy_even_when_normalized_ids_are_null(self):
        self.remote = []
        for legacy in ('', '0', '901', 'not-an-id'):
            async with self.transaction() as db:
                await db.execute(text('UPDATE shop_products SET external_id=:legacy WHERE id=:id'),
                    {'legacy': legacy, 'id': self.mapping_ids[1]})
            with self.subTest(legacy=legacy), self.assertRaisesRegex(
                    links.LinkError, 'consolidation_unverified_standalone_claim_required'):
                await self.preview(self.unverified_request())
        links.source.verify_unoccupied_codes.assert_not_awaited()
        links.source.discover.assert_not_awaited()

    async def test_missing_physical_legacy_column_is_not_treated_as_sql_null(self):
        self.remote = []
        async with self.transaction() as db:
            # Rename only in this disposable schema; JSON now lacks the key,
            # exactly the distinction the production catalog check must make.
            await db.execute(text('ALTER TABLE shop_products RENAME COLUMN external_id TO synthetic_legacy_id'))
        with self.assertRaisesRegex(links.LinkError, 'consolidation_legacy_native_column_missing'):
            await self.preview(self.unverified_request())
        links.source.verify_unoccupied_codes.assert_not_awaited()
        links.source.current_fingerprint.assert_not_awaited()

    async def test_unverified_claim_change_after_preview_expires_without_ownership_mutation(self):
        self.remote = []
        for column, value, previous in (('external_id', '901', None),
                                       ('external_product_id', '901', None),
                                       ('external_code', 'SYN-CHANGED', 'SYN-OLD')):
            preview = await self.preview(self.unverified_request())
            async with self.transaction() as db:
                await db.execute(text(f'UPDATE shop_products SET {column}=:value WHERE id=:id'),
                    {'value': value, 'id': self.mapping_ids[1]})
            with self.subTest(column=column), self.assertRaisesRegex(
                    links.LinkError, 'consolidation_context_changed') as caught:
                await self.apply(preview)
            self.assertTrue(caught.exception.known_unapplied)
            async with self.transaction() as db:
                self.assertEqual((await db.get(ProductConsolidation, preview['id'])).status, 'expired')
                self.assertEqual(await db.scalar(select(func.count()).select_from(ProductCanonicalAlias)), 0)
                self.assertEqual(await db.scalar(select(func.count()).select_from(ShopProduct)), 2)
                await db.execute(text(f'UPDATE shop_products SET {column}=:value WHERE id=:id'),
                    {'value': previous, 'id': self.mapping_ids[1]})

    async def test_reappearing_code_during_apply_is_known_unapplied_and_does_not_retire_claim(self):
        self.remote = []
        before = await self.stock()
        preview = await self.preview(self.unverified_request())
        self.remote = [self.leaf('syn-old', 901)]
        with self.assertRaisesRegex(links.LinkError, 'source_code_not_proven_absent') as caught:
            await self.apply(preview)
        self.assertTrue(caught.exception.known_unapplied)
        self.assertEqual(await self.stock(), before)
        async with self.transaction() as db:
            self.assertEqual((await db.get(ProductConsolidation, preview['id'])).status, 'expired')
            self.assertEqual(await db.scalar(select(func.count()).select_from(ProductCanonicalAlias)), 0)
            self.assertEqual(await db.scalar(select(func.count()).select_from(ShopProduct)), 2)

    async def test_foreign_current_code_claim_blocks_retirement_even_with_empty_native_code_scopes(self):
        self.remote = []
        preview = await self.preview(self.unverified_request())
        async with self.transaction() as db:
            foreign = Product(sku='SYN-FOREIGN', name='Foreign current claim', is_active=False)
            db.add(foreign); await db.flush()
            db.add(ShopProduct(shop_id=self.shop_id, product_id=foreign.id, external_code='syn-old', is_variant=False))
        with self.assertRaisesRegex(links.LinkError, 'consolidation_foreign_native_owner') as caught:
            await self.apply(preview)
        self.assertTrue(caught.exception.known_unapplied)
        async with self.transaction() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(ProductCanonicalAlias)), 0)
            self.assertTrue((await db.get(Product, self.retired)).is_active)

    async def test_mixed_bind_and_unverified_retirement_share_request_budget_and_distinct_proofs(self):
        request = self.request().model_dump(mode='json')
        request['mapping_actions'][1] = {'mapping_id': self.mapping_ids[1],
            'action': 'retire_unverified_code_absent', 'target': None}
        self.remote = self.remote[:1]
        preview = await self.preview(ProductConsolidationPreviewRequest(**request))
        self.assertTrue(preview['eligible'])
        self.assertEqual(preview['native']['biketrek']['requests'], 6)
        self.assertEqual(links.source.verify_unoccupied_codes.await_args.kwargs['max_requests'], 6)
        self.assertEqual(preview['native']['biketrek']['unverified_code_absence']['mapping_ids'], [self.mapping_ids[1]])
        done = await self.apply(preview)
        self.assertEqual(len(done['result']['current_mappings']), 1)
        self.assertFalse(done['result']['current_mappings'][0]['shared_stock'])
        links._verify_replacement_absence.assert_not_awaited()

    async def test_unverified_claim_action_keeps_confirmed_tracking_as_a_hard_block(self):
        self.remote = []
        async with self.transaction() as db:
            await db.execute(text('''INSERT INTO stock_tracking
                (product_id,warehouse_id,source_type,source_id,operator_name,archived_balance)
                VALUES(:pid,:wid,'opening_stock','synthetic','Synthetic operator','{}'::jsonb)'''),
                {'pid': self.keeper, 'wid': self.warehouse_id})
        preview = await self.preview(self.unverified_request())
        self.assertFalse(preview['eligible'])
        self.assertIn('stock_tracking', {row.get('table') for row in preview['dependencies']['blockers']})
        with self.assertRaisesRegex(links.LinkError, 'consolidation_dependencies_blocked'):
            await self.apply(preview)

    async def test_two_sales_one_card_preserves_original_ids_skus_identifiers_and_legacy_stock(self):
        before = await self.stock()
        request = self.request()
        preview = await self.preview(request)
        self.assertTrue(preview['eligible'], preview['dependencies']['blockers'])
        self.assertEqual(await self.stock(), before)
        result = await self.apply(preview)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(await self.stock(), before)
        async with self.transaction() as db:
            current = list(await db.scalars(select(ShopProduct)))
            history = list(await db.scalars(select(ShopProduct).execution_options(include_retired_shop_mappings=True)))
            self.assertEqual(len(current), 2)
            self.assertEqual(len(history), 4)
            self.assertEqual({row.product_id for row in current}, {self.keeper})
            self.assertTrue(all(row.shared_stock and row.sale_to_stock_factor == 1 for row in current))
            retired_rows = [row for row in history if row.retired_at]
            self.assertEqual({row.id for row in retired_rows}, set(self.mapping_ids))
            self.assertEqual({row.shop_price for row in retired_rows}, {Decimal('12.34')})
            self.assertEqual({row.shop_stock for row in retired_rows}, {Decimal(3)})
            self.assertEqual((await db.get(Product, self.retired)).sku, 'SYN-OLD')
            self.assertFalse((await db.get(Product, self.retired)).is_active)
            self.assertEqual(await db.scalar(select(func.count()).select_from(ProductIdentifier)), 2)
            for identity in (RemoteIdentity(0, 'SYN-OLD'), RemoteIdentity(0, barcodes=(EAN,))):
                index = await load_identity_index(db, 0, [identity], local=True)
                self.assertEqual(index.resolve(identity).product_id, self.keeper)
            scanner = ProductIdentifierService(db)
            self.assertEqual((await scanner.find_product_by_barcode(EAN)).id, self.keeper)
            self.assertEqual((await sku_policy.preview_sku(db, None, sku='SYN-OLD'))['product_id'], self.keeper)
            retired = RemoteIdentity(0, expected_product_id=self.retired)
            self.assertEqual((await load_identity_index(db, 0, [retired], local=True)).resolve(retired).reasons, ['product_retired'])
        reads = links.source.discover.await_count, links.source.verify.await_count
        self.assertEqual(await self.preview(request), result)
        self.assertEqual(await self.apply(preview), result)
        self.assertEqual((links.source.discover.await_count, links.source.verify.await_count), reads)

    async def test_current_mapping_filter_covers_column_queries_aliases_and_joins(self):
        await self.apply(await self.preview())
        async with self.transaction() as db:
            self.assertEqual(len(list(await db.scalars(select(ShopProduct.id)))), 2)
            mapping = aliased(ShopProduct)
            self.assertEqual(len(list(await db.scalars(select(mapping.id)))), 2)
            joined = await db.execute(select(Product.id, ShopProduct.id).join(ShopProduct, ShopProduct.product_id == Product.id))
            self.assertEqual(len(joined.all()), 2)
            self.assertEqual(await db.scalar(text('SELECT count(*) FROM v_shop_sync_status')), 2)

    async def test_inactive_foreign_barcode_owner_is_not_absorbed(self):
        async with self.transaction() as db:
            other = Product(sku='SYN-FOREIGN', name='Different item', is_active=False)
            db.add(other); await db.flush()
            # Historical systems sometimes put a valid UPC into the EAN field.
            # Raw partial indexes permit it; the shared GTIN resolver must not.
            db.add(ProductIdentifier(product_id=other.id, identifier_type=IdentifierType.ean, value=UPC))
        with self.assertRaisesRegex(links.LinkError, 'consolidation_foreign_identity_owner'):
            await self.preview()
        self.assertEqual((await self.stock())['stock_movements'][0]['product_id'], self.keeper)

    async def test_new_foreign_owner_after_preview_expires_entire_apply(self):
        preview = await self.preview()
        async with self.transaction() as db:
            other = Product(sku='SYN-FOREIGN', name='New conflicting owner')
            db.add(other); await db.flush()
            db.add(ProductIdentifier(product_id=other.id, identifier_type=IdentifierType.ean, value=UPC))
        with self.assertRaisesRegex(links.LinkError, 'consolidation_foreign_identity_owner') as caught:
            await self.apply(preview)
        self.assertTrue(caught.exception.known_unapplied)
        async with self.transaction() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(ProductCanonicalAlias)), 0)
            self.assertTrue((await db.get(Product, self.retired)).is_active)
            self.assertEqual((await db.get(ProductConsolidation, preview['id'])).status, 'expired')

    async def test_json_only_pending_preview_and_unknown_fk_block_apply(self):
        async with self.transaction() as db:
            document = {'request': {'product_ids': [self.retired]}}
            db.add(ProductLinkPreview(id=str(uuid4()), shop_id=self.shop_id, request_hash='b'*64, preview_hash='c'*64,
                document=document, status='prepared', expires_at=links.now()+timedelta(minutes=30)))
            await db.execute(text('CREATE TABLE unreviewed_product_dependency(id bigint PRIMARY KEY, product_id bigint REFERENCES products(id))'))
            await db.execute(text('INSERT INTO unreviewed_product_dependency VALUES(1,:id)'), {'id': self.retired})
        preview = await self.preview()
        self.assertFalse(preview['eligible'])
        tables = {row.get('table') for row in preview['dependencies']['blockers']}
        self.assertIn('product_link_previews', tables)
        self.assertIn('unreviewed_product_dependency', tables)
        with self.assertRaisesRegex(links.LinkError, 'consolidation_dependencies_blocked'):
            await self.apply(preview)

    async def test_nonlegacy_history_and_reserved_quantity_are_hard_blocks(self):
        async with self.transaction() as db:
            db.add(StockMovement(idempotency_key='nonlegacy', product_id=self.retired,
                warehouse_id=self.warehouse_id, movement_type='ADJUSTMENT_IN', quantity=1, balance_after=1,
                reference_type='manual', created_by='operator'))
            balance = await db.scalar(select(StockBalance).where(StockBalance.product_id == self.keeper))
            balance.qty_reserved = 1
        preview = await self.preview()
        codes = {row['code'] for row in preview['dependencies']['blockers']}
        self.assertIn('consolidation_nonlegacy_stock_history', codes)
        self.assertIn('consolidation_reserved_or_quarantined_stock', codes)
        self.assertFalse(preview['eligible'])

    async def test_operator_pause_keeps_matching_prepared_job_as_a_consolidation_blocker(self):
        from inventory_hub.services import atomer_catalog_control as control
        campaign = 'c' * 64
        async with self.transaction() as db:
            await db.execute(text("INSERT INTO atomer_progress_sources(sha256,source_rows,identities) VALUES(:sha,1,'[]')"),
                             {'sha': 'a' * 64})
            run = await db.scalar(text("""INSERT INTO atomer_progress_runs(source_sha256,checked_at,totals)
                VALUES(:sha,now(),'{}') RETURNING id"""), {'sha': 'a' * 64})
            await db.execute(text("""INSERT INTO atomer_catalog_campaigns
                (id,run_id,source_sha,admin_sha,shop_fingerprint,actor,selection,state,checked_run_id)
                VALUES(:id,:run,:source,:admin,:fp,'operator','{}','running',:run)"""),
                {'id': campaign, 'run': run, 'source': 'a' * 64, 'admin': 'b' * 64, 'fp': 'd' * 64})
            await db.execute(text("""INSERT INTO atomer_catalog_jobs(campaign,family_key,priority,leaf_count,stage,document)
                VALUES(:campaign,'s:synthetic',1,1,'prepared',CAST(:doc AS jsonb))"""),
                {'campaign': campaign, 'doc': json.dumps({'product_id': self.retired})})
        async with self.transaction() as db:
            state = await control.get(db, campaign)
            await control.change(db, campaign, 'pause', control.ControlRequest(request_id=uuid4(),
                expected_revision=state['revision'], expected_updated_at=state['updated_at'],
                reason='Reviewed campaign scheduler maintenance.', confirmed=True), 'operator-token')
        before = await self.stock()
        preview = await self.preview()
        self.assertFalse(preview['eligible'])
        blockers = preview['dependencies']['blockers']
        self.assertIn('atomer_catalog_jobs', {row.get('table') for row in blockers})
        self.assertNotIn('atomer_catalog_controls', {row.get('table') for row in blockers})
        self.assertNotIn('consolidation_catalog_worker_active', {row['code'] for row in blockers})
        with self.assertRaisesRegex(links.LinkError, 'consolidation_dependencies_blocked'):
            await self.apply(preview)
        self.assertEqual(await self.stock(), before)
        async with self.transaction() as db:
            self.assertEqual(await db.scalar(text('SELECT stage FROM atomer_catalog_jobs')), 'prepared')
            self.assertEqual(await db.scalar(select(func.count()).select_from(ProductCanonicalAlias)), 0)

    async def test_changed_native_axes_barcode_and_exact_id_fail_without_mutations(self):
        for field, value, expected in [('external_id', '99999', 'consolidation_native_identity_ambiguous'),
                                      ('eans', [OTHER_EAN], 'consolidation_native_identity_changed'),
                                      ('attributes', [{'name': 'Color', 'value': 'red'}], 'consolidation_native_identity_changed')]:
            preview = await self.preview()
            before = await self.stock()
            original = deepcopy(self.remote[1])
            self.remote[1][field] = value
            with self.assertRaisesRegex(links.LinkError, expected):
                await self.apply(preview)
            self.assertEqual(await self.stock(), before)
            self.remote[1] = original

    async def test_direct_old_id_stock_and_metadata_writers_cannot_resurrect_card(self):
        await self.apply(await self.preview())
        before = await self.stock()
        with self.assertRaises(ProductRetiredError) as caught:
            async with self.transaction() as db:
                await lock_stock_balances(db, {self.retired}, self.warehouse_id)
        self.assertEqual(caught.exception.canonical_product_id, self.keeper)
        for statement in (
            update(Product).where(Product.id == self.retired).values(is_active=True),
            text('INSERT INTO stock_balances(product_id,warehouse_id) VALUES(:pid,:wid)'),
            text("INSERT INTO product_identifiers(product_id,identifier_type,value) VALUES(:pid,'custom','MUST-NOT-EXIST')")):
            with self.assertRaises(IntegrityError):
                async with self.transaction() as db:
                    await db.execute(statement, {'pid': self.retired, 'wid': self.warehouse_id})
        self.assertEqual(await self.stock(), before)

    async def test_retired_mapping_is_readonly_and_cannot_own_current_leaf(self):
        await self.apply(await self.preview())
        with self.assertRaises(IntegrityError):
            async with self.transaction() as db:
                await db.execute(update(ShopProduct).where(ShopProduct.id == self.mapping_ids[1]).values(retired_at=None))
        with self.assertRaises(IntegrityError):
            async with self.transaction() as db:
                db.add(ShopProduct(shop_id=self.shop_id, product_id=self.retired, external_code='RESURRECT', external_product_id='999'))

    async def test_concurrent_apply_replays_one_atomic_result(self):
        request = self.request()
        one, two = await asyncio.gather(self.preview(request), self.preview(request))
        self.assertEqual(one, two)
        before = await self.stock()
        done, replayed = await asyncio.gather(self.apply(one), self.apply(two))
        self.assertEqual(done, replayed)
        self.assertEqual(await self.stock(), before)

    async def test_stock_writer_waiting_on_retirement_rechecks_after_product_lock(self):
        preview = await self.preview()
        product_locked, allow_retirement = asyncio.Event(), asyncio.Event()
        original_local = service._local
        async def pause_after_lock(db, raw, *, lock=False):
            result = await original_local(db, raw, lock=lock)
            if lock:
                product_locked.set()
                await allow_retirement.wait()
            return result
        with patch.object(service, '_local', side_effect=pause_after_lock):
            retirement = asyncio.create_task(self.apply(preview))
            await asyncio.wait_for(product_locked.wait(), timeout=5)
            async def writer():
                async with self.transaction() as db:
                    # This is a stale explicit ID captured before retirement.
                    await lock_stock_balances(db, {self.retired}, self.warehouse_id)
            writing = asyncio.create_task(writer())
            await asyncio.sleep(.05)
            self.assertFalse(writing.done())
            allow_retirement.set()
            await asyncio.wait_for(retirement, timeout=10)
            with self.assertRaises(ProductRetiredError):
                await asyncio.wait_for(writing, timeout=10)
        async with self.transaction() as db:
            self.assertIsNone(await db.scalar(select(StockBalance.id).where(StockBalance.product_id == self.retired)))

    async def test_absence_requires_exact_native_claim_and_preserves_live_hidden_listing(self):
        request = self.request()
        request.mapping_actions[1].action = 'retire_absent'
        request.mapping_actions[1].target = None
        with self.assertRaisesRegex(links.LinkError, 'consolidation_old_native_identity_unverified'):
            await self.preview(request)
        async with self.transaction() as db:
            row = await db.get(ShopProduct, self.mapping_ids[1]); row.external_product_id = '9002'
        with self.assertRaisesRegex(links.LinkError, 'consolidation_original_native_still_exists'):
            await self.preview(request)
        self.absent_ids.add(self.mapping_ids[1])
        preview = await self.preview(request)
        done = await self.apply(preview)
        self.assertEqual(len(done['result']['current_mappings']), 1)
        self.assertFalse(done['result']['current_mappings'][0]['shared_stock'])

    async def test_migration_replay_keeps_aliases_history_and_retirement_indexes(self):
        await self.apply(await self.preview())
        before = await self.stock()
        connection = await asyncpg.connect(TEST_URL)
        try:
            await connection.execute(f'SET search_path TO "{self.schema}"')
            # The deployment runner replays these exact SQL payloads, each in
            # its own transaction. Feature installers039–045 already exist in
            # this fixture, as they do in an upgraded deployment.
            for number in [*range(5, 39), 46, 47, 48]:
                scripts = list(self.sql_root.glob(f'{number:03}_*.sql'))
                self.assertEqual(len(scripts), 1)
                await connection.execute(scripts[0].read_text())
            self.assertEqual(await connection.fetchval('SELECT count(*) FROM product_canonical_aliases'), 1)
            self.assertEqual(await connection.fetchval('SELECT count(*) FROM shop_products WHERE retired_at IS NULL'), 2)
            self.assertEqual(await connection.fetchval('SELECT count(*) FROM shop_products WHERE retired_at IS NOT NULL'), 2)
        finally:
            await connection.close()
        self.assertEqual(await self.stock(), before)

    async def test_new_unrelated_inactive_owner_after_consolidation_still_blocks_scanner(self):
        await self.apply(await self.preview())
        async with self.transaction() as db:
            other = Product(sku='SYN-UNRELATED', name='Unrelated barcode owner', is_active=False)
            db.add(other); await db.flush()
            db.add(ProductIdentifier(product_id=other.id, identifier_type=IdentifierType.ean, value=UPC))
        async with self.transaction() as db:
            with self.assertRaises(IdentifierConflict):
                await ProductIdentifierService(db).find_product_by_barcode(EAN)
            identity = RemoteIdentity(0, barcodes=(EAN,))
            index = await load_identity_index(db, 0, [identity], local=True)
            self.assertEqual(index.resolve(identity).status, 'conflict')

    async def test_retired_legacy_quantity_is_preserved_without_summing_into_keeper(self):
        async with self.transaction() as db:
            db.add(StockBalance(product_id=self.retired, warehouse_id=self.warehouse_id, qty_on_hand=2,
                qty_reserved=0, qty_quarantined=0, avg_cost=Decimal('1.1000'), total_value=Decimal('2.2000')))
            db.add(StockMovement(idempotency_key='synthetic-second-legacy', product_id=self.retired,
                warehouse_id=self.warehouse_id, movement_type='INITIAL', quantity=2,
                unit_cost=Decimal('1.1000'), balance_after=2, avg_cost_after=Decimal('1.1000'),
                reference_type='upgates_import', reference_source='upgates:biketrek',
                reference_id='SYN-OLD', created_by='upgates_import'))
        before = await self.stock()
        await self.apply(await self.preview())
        self.assertEqual(await self.stock(), before)
        async with self.transaction() as db:
            quantities = dict((await db.execute(select(StockBalance.product_id, StockBalance.qty_on_hand))).all())
            self.assertEqual(quantities, {self.keeper: Decimal(3), self.retired: Decimal(2)})
            self.assertFalse((await product_editor.detail(db, self.keeper))['stock']['known'])

    async def test_current_catalog_old_sku_search_and_keeper_supplier_link_survive_redeployment(self):
        async with self.transaction() as db:
            supplier = Supplier(code='synthetic-maker', name='Synthetic manufacturer')
            db.add(supplier); await db.flush()
            source = SupplierProduct(supplier_id=supplier.id, supplier_sku='SYN-MAKER-CODE',
                name='Synthetic identical retail unit', ean=EAN, attributes={'catalog': {
                    'supplier': supplier.code, 'code': 'SYN-MAKER-CODE', 'shop_code': 'SYN-MAKER-CODE',
                    'name': 'Synthetic identical retail unit', 'prices': {'currency': 'EUR'}, 'eans': [EAN]}})
            db.add(source); await db.flush()
            source_id = source.id
            db.add(ProductIdentifier(product_id=self.keeper, supplier_id=supplier.id,
                identifier_type=IdentifierType.supplier_sku, value='SYN-MAKER-CODE'))
        await self.apply(await self.preview())
        async with self.transaction() as db:
            for query in ('', 'SYN-OLD', UPC, 'SYN-MAKER-CODE'):
                page = await product_editor.list_products(db, q=query)
                self.assertEqual([row['id'] for row in page['items']], [self.keeper])
            historical = await product_editor.detail(db, self.retired)
            self.assertTrue(historical['retired'])
            self.assertEqual(historical['canonical_product_id'], self.keeper)
            candidates = await product_candidates.find_candidates(db, [
                product_candidates.CandidateQuery(key='former-sku', sku='SYN-OLD'),
                product_candidates.CandidateQuery(key='ean', eans=(EAN,))])
            for result in candidates.values():
                self.assertEqual([row['product_id'] for row in result['candidates']], [self.keeper])
            inspected = await supplier_card_links.inspect_link(db, self.keeper, source_id)
            self.assertEqual(inspected['errors'], [])
            self.assertEqual(inspected['planned_changes'], ['supplier_source'])
            await supplier_card_links.apply_link(db, self.keeper, source_id, expected_hash=inspected['preview_hash'])
            original_alias = await db.scalar(select(ProductIdentifier).where(
                ProductIdentifier.value == 'SYN-MAKER-CODE'))
            self.assertEqual(original_alias.product_id, self.keeper)
        connection = await asyncpg.connect(TEST_URL)
        try:
            await connection.execute(f'SET search_path TO "{self.schema}"')
            await connection.execute((self.sql_root/'032_supplier_authority.sql').read_text())
        finally:
            await connection.close()

    async def test_retired_scoped_supplier_alias_blocks_before_native_reads(self):
        async with self.transaction() as db:
            supplier = Supplier(code='synthetic-scoped', name='Synthetic supplier')
            db.add(supplier); await db.flush()
            db.add(ProductIdentifier(product_id=self.retired, supplier_id=supplier.id,
                identifier_type=IdentifierType.supplier_sku, value='SYN-PRESERVED-SUPPLIER-CODE'))
        before = await self.stock()
        with self.assertRaisesRegex(links.LinkError, 'consolidation_retired_supplier_alias_unsupported'):
            await self.preview()
        links.source.discover.assert_not_awaited()
        self.assertEqual(await self.stock(), before)

    async def test_confirmed_tracking_and_fifo_state_block_even_zero_quantity(self):
        async with self.transaction() as db:
            await db.execute(text('''INSERT INTO stock_tracking
                (product_id,warehouse_id,source_type,source_id,operator_name,archived_balance)
                VALUES(:pid,:wid,'opening_stock','synthetic','Synthetic operator','{}'::jsonb)'''),
                {'pid': self.keeper, 'wid': self.warehouse_id})
            await db.execute(text('''INSERT INTO fifo_states(product_id,warehouse_id,activation_kind)
                VALUES(:pid,:wid,'pristine')'''), {'pid': self.retired, 'wid': self.warehouse_id})
        preview = await self.preview()
        self.assertFalse(preview['eligible'])
        tables = {row.get('table') for row in preview['dependencies']['blockers']}
        self.assertTrue({'stock_tracking', 'fifo_states'} <= tables)
        before = await self.stock()
        with self.assertRaisesRegex(links.LinkError, 'consolidation_dependencies_blocked'):
            await self.apply(preview)
        self.assertEqual(await self.stock(), before)

    async def test_prior_stock_writer_commits_and_consolidation_revalidates_after_wait(self):
        preview = await self.preview()
        writer_ready, finish_writer, consolidation_waiting = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original_local = service._local
        async def local(db, raw, *, lock=False):
            if lock:
                consolidation_waiting.set()
            return await original_local(db, raw, lock=lock)
        async def writer():
            async with self.transaction() as db:
                balances, _ = await lock_stock_balances(db, {self.keeper}, self.warehouse_id)
                balances[self.keeper].qty_on_hand = 4
                db.add(StockMovement(idempotency_key='synthetic-real-adjustment', product_id=self.keeper,
                    warehouse_id=self.warehouse_id, movement_type='ADJUSTMENT_IN', quantity=1,
                    balance_after=4, reference_type='manual', created_by='Synthetic operator'))
                await db.flush()
                writer_ready.set()
                await finish_writer.wait()
        writing = asyncio.create_task(writer())
        await asyncio.wait_for(writer_ready.wait(), 5)
        with patch.object(service, '_local', side_effect=local):
            retiring = asyncio.create_task(self.apply(preview))
            await asyncio.wait_for(consolidation_waiting.wait(), 5)
            self.assertFalse(retiring.done())
            finish_writer.set()
            await asyncio.wait_for(writing, 10)
            with self.assertRaisesRegex(links.LinkError, 'consolidation_dependencies_changed'):
                await asyncio.wait_for(retiring, 10)
        async with self.transaction() as db:
            self.assertEqual(await db.scalar(select(StockBalance.qty_on_hand).where(
                StockBalance.product_id == self.keeper)), Decimal(4))
            self.assertEqual(await db.scalar(select(func.count()).select_from(ProductCanonicalAlias)), 0)

    async def test_foreign_identity_writer_already_inflight_causes_immediate_unapplied_conflict(self):
        async with self.transaction() as db:
            foreign = Product(sku='SYN-NEW-OWNER', name='Foreign product')
            db.add(foreign); await db.flush()
            foreign_id = foreign.id
        preview = await self.preview()
        async with self.transaction() as writer:
            # Deliberately exercise a direct writer without the advisory lock.
            writer.add(ProductIdentifier(product_id=foreign_id, identifier_type=IdentifierType.ean, value=UPC))
            await writer.flush()
            with self.assertRaisesRegex(links.LinkError, 'consolidation_identity_writer_busy') as caught:
                await asyncio.wait_for(self.apply(preview), 5)
            self.assertTrue(caught.exception.known_unapplied)
        async with self.transaction() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(ProductCanonicalAlias)), 0)
            self.assertEqual((await db.get(ProductConsolidation, preview['id'])).status, 'expired')

    async def test_foreign_identity_writer_cannot_enter_final_scan_commit_interval(self):
        async with self.transaction() as db:
            foreign = Product(sku='SYN-LATER-OWNER', name='Foreign product')
            db.add(foreign); await db.flush()
            foreign_id = foreign.id
        preview = await self.preview()
        locked, finish = asyncio.Event(), asyncio.Event()
        original_lock = service._lock_identity_owners
        async def pause(db):
            await original_lock(db)
            locked.set()
            await finish.wait()
        async def writer():
            async with self.transaction() as db:
                db.add(ProductIdentifier(product_id=foreign_id, identifier_type=IdentifierType.ean, value=UPC))
                await db.flush()
        with patch.object(service, '_lock_identity_owners', side_effect=pause):
            retiring = asyncio.create_task(self.apply(preview))
            await asyncio.wait_for(locked.wait(), 5)
            writing = asyncio.create_task(writer())
            await asyncio.sleep(.05)
            self.assertFalse(writing.done())
            finish.set()
            result = await asyncio.wait_for(retiring, 10)
            self.assertEqual(result['status'], 'completed')
            await asyncio.wait_for(writing, 10)
        # A subsequent unrelated owner is still a conflict, never folded in.
        async with self.transaction() as db:
            with self.assertRaises(IdentifierConflict):
                await ProductIdentifierService(db).find_product_by_barcode(EAN)

    async def test_catalog_event_intent_overrides_completed_file_snapshot(self):
        from inventory_hub.services import catalog_import
        path = config_io.shop_path('biketrek').parent / 'catalog-imports' / (uuid4().hex + '.json')
        item = {'code': 'SYN-OLD', 'payload': {}, 'status': 'created', 'errors': [], 'warnings': []}
        result = {'items': [item], 'updated_at': links.now().isoformat()}
        document = {'preview': {'items': [deepcopy(item)]}, 'result': deepcopy(result)}
        catalog_import._save_job(path, document)
        item.update(status='uncertain', product_write_attempted=True)
        catalog_import._item_checkpoint(path, result, item)
        preview = await self.preview()
        self.assertFalse(preview['eligible'])
        self.assertIn('consolidation_catalog_journal_pending',
                      {row['code'] for row in preview['dependencies']['blockers']})
        self.assertEqual(json.loads(path.read_text())['result']['items'][0]['status'], 'created')

    async def test_aliases_cannot_chain_reverse_or_mutate_and_keeper_sku_stays_immutable(self):
        done = await self.apply(await self.preview())
        async with self.transaction() as db:
            other = Product(sku='SYN-THIRD', name='Independent item', is_active=False)
            db.add(other); await db.flush()
            third = other.id
        for statement, params, message in [
            ("UPDATE product_consolidations SET result='{}'::jsonb WHERE id=:review",
             {'review': done['id']}, 'consolidation_audit_immutable'),
            ('DELETE FROM product_consolidations WHERE id=:review',
             {'review': done['id']}, 'consolidation_audit_immutable'),
            ('UPDATE product_canonical_aliases SET canonical_product_id=:third WHERE retired_product_id=:old',
             {'third': third, 'old': self.retired}, 'canonical_alias_immutable'),
            ('DELETE FROM product_canonical_aliases WHERE retired_product_id=:old',
             {'old': self.retired}, 'canonical_alias_immutable'),
            ('INSERT INTO product_canonical_aliases(retired_product_id,canonical_product_id,consolidation_id) VALUES(:third,:old,:review)',
             {'third': third, 'old': self.retired, 'review': done['id']}, 'canonical_alias_one_hop_required'),
            ("UPDATE products SET sku='SYN-CHANGED' WHERE id=:keeper", {'keeper': self.keeper}, 'canonical_identity_requires_review')]:
            with self.subTest(message=message), self.assertRaisesRegex(IntegrityError, message):
                async with self.transaction() as db:
                    await db.execute(text(statement), params)
