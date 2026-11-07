"""Atomic sale mapping reviews and real piece/FIFO outcomes in isolated PostgreSQL."""
from copy import deepcopy
import asyncio
from pathlib import Path
from decimal import Decimal as D
import os
import unittest
from uuid import uuid4
from unittest.mock import AsyncMock, patch
import asyncpg
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError

from inventory_hub.db_models import Product, MovementType, Shop
from inventory_hub.db_models_ext import ShopOrder, ShopOrderItem, ShopProduct, ShopProductContent, StockBalance, StockMovement
from inventory_hub.fifo_models import FifoAllocation, FifoLayer
from inventory_hub.fifo_return_types import FifoReturnInput
from inventory_hub.services import shared_stock_sales as sales, product_links as links, fifo, fifo_returns
from inventory_hub.services import product_editor, product_publication, catalog_import
from inventory_hub.services.stock_balances import lock_stock_balances
from inventory_hub.shared_stock_sale_types import SharedStockSalePreviewRequest, SharedStockSaleApplyRequest
from inventory_hub.shared_stock_sale_models import SharedStockSalePreview
from inventory_hub.product_link_types import ProductLinksPreviewRequest
from inventory_hub.routers import upgates_sync
from inventory_hub.services.product_identity import load_identity_index, RemoteIdentity
from stock_tracking_fixture import confirmed_inventory
import test_product_links_db as link_fixture
import test_order_stock_db as order_fixture
from test_order_stock import raw_order, raw_line, NOW


TEST_URL = os.environ.get('CATALOG_TEST_DATABASE_URL', '')


@unittest.skipUnless(TEST_URL, 'Set CATALOG_TEST_DATABASE_URL to an isolated localhost *_catalog_test DB')
class SharedSaleMappingDatabaseTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = link_fixture.ProductLinksDatabaseTests.asyncSetUp
    asyncTearDown = link_fixture.ProductLinksDatabaseTests.asyncTearDown
    transaction = link_fixture.ProductLinksDatabaseTests.transaction
    discover_remote = link_fixture.ProductLinksDatabaseTests.discover_remote
    verify_remote = link_fixture.ProductLinksDatabaseTests.verify_remote
    snapshot = link_fixture.ProductLinksDatabaseTests.snapshot
    mappings = link_fixture.ProductLinksDatabaseTests.mappings

    def request(self):
        self.remote = [link_fixture.leaf(f'PACK-{factor}', 100+factor, parent_code='PATCHES', eans=[])
                       for factor in (1, 8, 100)]
        return SharedStockSalePreviewRequest(request_id=uuid4(), shop_code='xtrek', product_id=self.ids[0],
            sales=[{'code': row['code'], 'parent_code': row['parent_code'], 'is_variant': True,
                    'native_product_id': 800, 'native_variant_id': int(row['external_id']),
                    'sale_to_stock_factor': factor} for factor, row in zip((1, 8, 100), self.remote)],
            identity_evidence='Verified loose pieces. The sale variants choose1,8or100 of the same physical patch.',
            same_physical_item_confirmed=True, base_piece_unit_confirmed=True)

    async def preview(self, request=None):
        async with self.transaction() as db:
            return await sales.preview(db, request or self.request())

    async def apply(self, preview):
        async with self.transaction() as db:
            return await sales.apply(db, preview['id'], SharedStockSaleApplyRequest(
                preview_hash=preview['preview_hash'], confirmed=True))

    async def test_review_and_exact_replay_link_three_sales_without_touching_stock(self):
        request = self.request()
        before = await self.snapshot()
        preview = await self.preview(request)
        self.assertEqual(await self.snapshot(), before)
        self.assertEqual(await self.mappings(), [])
        completed = await self.apply(preview)
        self.assertEqual(completed['status'], 'completed')
        self.assertEqual(await self.snapshot(), before)
        mapped = await self.mappings()
        self.assertEqual(sorted(row.sale_to_stock_factor for row in mapped), [D(1), D(8), D(100)])
        self.assertTrue(all(row.shared_stock and row.product_id == self.ids[0] for row in mapped))
        reads = self.discover.await_count, self.verify.await_count
        self.assertEqual(await self.apply(preview), completed)
        self.assertEqual(await self.preview(request), completed)
        self.assertEqual((self.discover.await_count, self.verify.await_count), reads)

    async def test_foreign_native_owner_appearing_after_review_blocks_every_link(self):
        preview = await self.preview()
        async with self.transaction() as db:
            db.add(ShopProduct(shop_id=self.shop_id, product_id=self.ids[1], external_code='PACK-8',
                is_variant=True, parent_code='PATCHES', external_product_id='800', external_variant_id='108'))
        with self.assertRaises(links.LinkError) as error:
            await self.apply(preview)
        self.assertEqual(error.exception.code, 'shared_stock_sale_ownership_conflict')
        self.assertTrue(error.exception.known_unapplied)
        self.assertEqual(len(await self.mappings()), 1)

    async def test_changed_native_identity_or_factor_request_requires_fresh_review(self):
        request = self.request()
        preview = await self.preview(request)
        request.sales[0].sale_to_stock_factor = 2
        with self.assertRaises(links.LinkError) as changed:
            await self.preview(request)
        self.assertEqual(changed.exception.code, 'shared_stock_sale_request_changed')
        self.remote[0]['external_id'] = '999'
        with self.assertRaises(links.LinkError) as changed:
            await self.apply(preview)
        self.assertEqual(changed.exception.code, 'shared_stock_sale_native_identity_changed')
        self.assertEqual(await self.mappings(), [])

    async def test_existing_mappings_must_be_preserved_and_plain_writer_cannot_add_a_fourth(self):
        preview = await self.preview()
        await self.apply(preview)
        request = self.request(); request.sales.pop()
        with self.assertRaises(links.LinkError) as omitted:
            await self.preview(request)
        self.assertEqual(omitted.exception.code, 'shared_stock_sale_existing_mapping_omitted')
        with self.assertRaises(IntegrityError):
            async with self.transaction() as db:
                db.add(ShopProduct(shop_id=self.shop_id, product_id=self.ids[0], external_code='UNCONFIRMED'))
        self.assertEqual(len(await self.mappings()), 3)

    async def test_shared_unit_cannot_later_become_pack_or_service(self):
        await self.apply(await self.preview())
        for changes in ({'stock_unit': 'pack'}, {'item_type': 'service'}):
            with self.subTest(changes=changes), self.assertRaises(IntegrityError):
                async with self.transaction() as db:
                    await db.execute(update(Product).where(Product.id == self.ids[0]).values(**changes))

    async def test_concurrent_exact_preview_and_apply_create_one_group(self):
        request = self.request()
        first, second = await asyncio.gather(self.preview(request), self.preview(request))
        self.assertEqual(first, second)
        completed, replayed = await asyncio.gather(self.apply(first), self.apply(second))
        self.assertEqual(completed, replayed)
        self.assertEqual(len(await self.mappings()), 3)

    async def test_prior_managed_order_blocks_reinterpreting_legacy_quantity(self):
        request = self.request()
        async with self.transaction() as db:
            order = ShopOrder(shop_id=self.shop_id, external_id='legacy-sale', order_date=NOW,
                              stock_state='reserved')
            db.add(order)
            await db.flush()
            db.add(ShopOrderItem(order_id=order.id, product_id=self.ids[0], external_item_id='legacy',
                                external_product_code='PACK-8', quantity=1, stock_managed=True))
        before = await self.snapshot()
        with self.assertRaisesRegex(links.LinkError, 'shared_stock_sale_history_requires_review'):
            await self.preview(request)
        self.assertEqual(await self.snapshot(), before)
        self.assertEqual(await self.mappings(), [])

    async def test_migration_replay_preserves_existing_physical_and_shared_rows(self):
        await self.apply(await self.preview())
        before = await self.snapshot()
        mappings = [sales._mapping(row) for row in await self.mappings()]
        connection = await asyncpg.connect(TEST_URL)
        try:
            await connection.execute(f'SET search_path TO "{self.schema}"')
            sql = (Path(__file__).resolve().parents[2] / 'infra/db-init/046_shared_stock_sales.sql').read_text()
            await connection.execute(sql)
            await connection.execute(sql)
        finally:
            await connection.close()
        self.assertEqual(await self.snapshot(), before)
        self.assertEqual([sales._mapping(row) for row in await self.mappings()], mappings)

    async def test_editor_preserves_all_sale_leaves_and_blocks_scalar_publication(self):
        await self.apply(await self.preview())
        async with self.sessions() as db:
            row = await product_editor.detail(db, self.ids[0])
        shop = next(value for value in row['shops'] if value['shop_code'] == 'xtrek')
        self.assertTrue(shop['mapped'])
        self.assertTrue(shop['shared_stock'])
        self.assertIsNone(shop['mapping'])
        self.assertEqual({value['code'] for value in shop['sale_mappings']}, {'PACK-1', 'PACK-8', 'PACK-100'})
        self.assertTrue(row['item_definition_locked'])
        with self.assertRaisesRegex(product_editor.EditorError, 'product_publication_shared_sales'):
            product_publication.target(row, 'xtrek')
        async with self.sessions() as db:
            self.assertEqual(await catalog_import.shared_product_ids(db, {self.ids[0]}), {self.ids[0]})

    async def test_other_shop_cannot_adopt_shared_piece_card_by_legacy_sku_or_gtin(self):
        await self.apply(await self.preview())
        async with self.transaction() as db:
            bike = await db.scalar(select(Shop).where(Shop.code == 'biketrek'))
            identity = RemoteIdentity(bike.id, 'SHARED-A', False, '9001', barcodes=(link_fixture.EAN_A,))
            index = await load_identity_index(db, bike.id, [identity])
            resolved = index.resolve(identity)
            self.assertEqual(resolved.reasons, ['shared_stock_sale_requires_group_workflow'])
            report = await upgates_sync.register_observed_products(db, bike, [
                {'code': 'SHARED-A', 'product_id': 9001, 'ean': link_fixture.EAN_A,
                 'descriptions': [{'language': 'sk', 'title': 'Legacy package identity'}]}],
                selected_leaves=None, import_all=True)
            self.assertEqual(report['created_products'], 0)
            self.assertIn('shared_stock_sale_requires_group_workflow', report['conflicts'][0]['reasons'])
            facts = await links._facts(db, [self.ids[0]])
            remote = link_fixture.leaf('SHARED-A', 9001, eans=[link_fixture.EAN_A])
            checked = await links._rows(db, bike, facts, {self.ids[0]: (remote, 'manual', 1)})
            self.assertEqual(checked[0]['status'], 'conflict')
            self.assertIn('shared_stock_sale_requires_group_workflow', checked[0]['errors'])
        self.assertEqual(len(await self.mappings()), 3)


@unittest.skipUnless(TEST_URL, 'Set CATALOG_TEST_DATABASE_URL to an isolated localhost *_catalog_test DB')
class SharedSaleReplacementDatabaseTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = link_fixture.ProductLinksDatabaseTests.asyncTearDown
    transaction = link_fixture.ProductLinksDatabaseTests.transaction
    discover_remote = link_fixture.ProductLinksDatabaseTests.discover_remote
    verify_remote = link_fixture.ProductLinksDatabaseTests.verify_remote
    snapshot = link_fixture.ProductLinksDatabaseTests.snapshot
    full_snapshot = link_fixture.ProductLinksDatabaseTests.standalone_snapshot
    mappings = link_fixture.ProductLinksDatabaseTests.mappings
    preview = SharedSaleMappingDatabaseTests.preview
    apply = SharedSaleMappingDatabaseTests.apply

    async def asyncSetUp(self):
        await link_fixture.ProductLinksDatabaseTests.asyncSetUp(self)
        self.absent_product = AsyncMock(return_value={'fingerprint': self.fingerprint,
            'requests': 2, 'existing_mapping_ids': []})
        self.absent_variant = AsyncMock(return_value={'fingerprint': self.fingerprint,
            'requests': 2, 'existing_mapping_ids': []})
        for name, operation in (('verify_deleted_products', self.absent_product),
                                ('verify_deleted_variants', self.absent_variant)):
            patcher = patch.object(links.source, name, operation)
            patcher.start(); self.patches.append(patcher)

    async def replacement_fixture(self, *, variant=False, same_parent=False):
        parent = {'product_id': 97557, 'code': 'TEST-PIECE', 'ean': link_fixture.EAN_A, 'active_yn': True,
                  'variants_exists_yn': True, 'set_yn': False,
                  'descriptions': [{'language': 'sk', 'title': 'Loose patches'}]}
        leaves = [{'code': f'TEST-PIECE-{index}', 'variant_id': native, 'product_id': 97557, 'ean': None}
                  for index, native in enumerate((97802, 97803, 97804), 1)]
        self.remote = [links.source._leaf(parent, leaf, variant=True) for leaf in leaves]
        self.assertTrue(all(row['eans'] == [] for row in self.remote))
        self.old_code = 'TEST-PIECE' if same_parent else 'TEST-OLD-PACK'
        async with self.transaction() as db:
            old = ShopProduct(shop_id=self.shop_id, product_id=self.ids[0], external_code=self.old_code,
                is_variant=variant, parent_code='TEST-OLD-PARENT' if variant else None,
                external_product_id='97148', external_variant_id='97001' if variant else None,
                shop_stock=D(2), shop_price=D('9.99'), shop_availability='old snapshot',
                last_push_at=NOW, last_push_status='old', last_push_error='private-old-diagnostic', last_pull_at=NOW)
            db.add(old)
            db.add(ShopProductContent(shop_id=self.shop_id, external_code=self.old_code,
                data={'product_id': 97148, 'code': self.old_code, 'stock': 2, 'prices': [{'price': '9.99'}]}))
            await db.flush()
            self.old_id = old.id
        async with self.sessions() as db:
            self.original = sales._replacement_snapshot(await db.get(ShopProduct, self.old_id))
        request = SharedStockSalePreviewRequest(request_id=uuid4(), shop_code='xtrek', product_id=self.ids[0],
            sales=[{'code': row['code'], 'parent_code': 'TEST-PIECE', 'is_variant': True,
                    'native_product_id': 97557, 'native_variant_id': int(row['external_id']),
                    'sale_to_stock_factor': factor,
                    **({'replace_deleted_mapping': {'mapping_id': self.old_id, 'native_product_id': 97148,
                        'native_variant_id': 97001 if variant else None}} if factor == 100 else {})}
                   for factor, row in zip((1, 8, 100), self.remote)],
            identity_evidence='Verified same loose patches sold as 1, 8 and 100 pieces; no current count is asserted.',
            same_physical_item_confirmed=True, base_piece_unit_confirmed=True)
        return request

    async def test_deleted_standalone_with_parent_only_ean_is_replaced_atomically_and_replays(self):
        request = await self.replacement_fixture()
        before = await self.full_snapshot()
        preview = await self.preview(request)
        self.assertEqual(preview['replaced_mappings'][0]['external_product_id'], '97148')
        self.assertEqual(preview['replaced_mappings'][0]['shop_stock'], '2.000')
        self.assertNotIn('_last_push_error', preview['replaced_mappings'][0])
        self.assertEqual(await self.full_snapshot(), before)
        completed = await self.apply(preview)
        self.assertEqual(await self.full_snapshot(), before)
        mapped = await self.mappings()
        self.assertEqual(len(mapped), 3)
        hundred = next(row for row in mapped if row.external_variant_id == '97804')
        self.assertEqual((hundred.id, hundred.product_id, hundred.sale_to_stock_factor),
                         (self.old_id, self.ids[0], D(100)))
        self.assertIsNone(hundred.shop_stock)
        self.assertIsNone(hundred.shop_price)
        self.assertEqual(completed['result']['replaced_mappings'][0]['previous_mapping'],
                         sales._public_mapping(self.original))
        async with self.sessions() as db:
            stored = await db.get(SharedStockSalePreview, preview['id'])
            self.assertEqual(stored.result['replaced_mappings'][0]['previous_mapping'], self.original)
            old_content = await db.scalar(select(ShopProductContent).where(
                ShopProductContent.shop_id == self.shop_id, ShopProductContent.external_code == self.old_code))
            self.assertEqual(old_content.data, {'product_id': 97148, 'code': self.old_code,
                                               'stock': 2, 'prices': [{'price': '9.99'}]})
        self.assertEqual(self.absent_product.await_count, 2)
        self.absent_variant.assert_not_awaited()
        self.assertEqual(self.absent_product.await_args.kwargs['max_requests'], links.MAX_REQUESTS-1)
        reads = self.verify.await_count, self.absent_product.await_count
        self.assertEqual(await self.apply(preview), completed)
        self.assertEqual(await self.preview(request), completed)
        self.assertEqual((self.verify.await_count, self.absent_product.await_count), reads)

    async def test_deleted_variant_uses_its_own_typed_absence_check(self):
        request = await self.replacement_fixture(variant=True)
        completed = await self.apply(await self.preview(request))
        self.assertEqual(completed['status'], 'completed')
        self.assertEqual(self.absent_variant.await_count, 2)
        self.absent_product.assert_not_awaited()
        self.assertEqual(self.absent_variant.await_args.args[1][0]['external_variant_id'], '97001')

    async def test_existing_or_reappeared_original_blocks_every_sale_and_expires_apply(self):
        request = await self.replacement_fixture()
        self.absent_product.return_value['existing_mapping_ids'] = [self.old_id]
        with self.assertRaisesRegex(links.LinkError, 'shared_stock_sale_original_still_exists'):
            await self.preview(request)
        self.absent_product.return_value['existing_mapping_ids'] = []
        preview = await self.preview(request)
        self.absent_product.side_effect = links.source.SourceError('product_links_product_still_exists', 409)
        with self.assertRaises(links.LinkError) as error:
            await self.apply(preview)
        self.assertTrue(error.exception.known_unapplied)
        self.assertEqual(len(await self.mappings()), 1)
        self.assertEqual(sales._replacement_snapshot((await self.mappings())[0]), self.original)
        async with self.sessions() as db:
            self.assertEqual((await db.get(SharedStockSalePreview, preview['id'])).status, 'expired')

    async def test_wrong_original_native_pin_and_changed_local_mapping_fail_before_mutation(self):
        request = await self.replacement_fixture()
        request.sales[-1].replace_deleted_mapping.native_product_id = 999
        with self.assertRaisesRegex(links.LinkError, 'shared_stock_sale_replacement_changed'):
            await self.preview(request)
        self.absent_product.assert_not_awaited()
        request.sales[-1].replace_deleted_mapping.native_product_id = 97148
        preview = await self.preview(request)
        async with self.transaction() as db:
            await db.execute(update(ShopProduct).where(ShopProduct.id == self.old_id).values(external_code='CHANGED'))
        with self.assertRaisesRegex(links.LinkError, 'shared_stock_sale_context_changed'):
            await self.apply(preview)
        self.assertEqual(len(await self.mappings()), 1)

    async def test_conflicting_parent_vault_rolls_back_all_replacement_and_siblings(self):
        request = await self.replacement_fixture(same_parent=True)
        preview = await self.preview(request)
        before = await self.full_snapshot()
        with self.assertRaises(links.LinkError) as error:
            await self.apply(preview)
        self.assertEqual(error.exception.code, 'product_links_identity_changed')
        self.assertTrue(error.exception.known_unapplied)
        self.assertEqual(await self.full_snapshot(), before)
        self.assertEqual(len(await self.mappings()), 1)
        self.assertEqual(sales._replacement_snapshot((await self.mappings())[0]), self.original)

    async def test_managed_history_and_pending_cost_publication_block_replacement(self):
        from inventory_hub.fifo_cost_models import FifoCostPublication
        request = await self.replacement_fixture()
        async with self.transaction() as db:
            warehouse = await db.scalar(text("SELECT id FROM warehouses WHERE code='hlavny-sklad'"))
            db.add(FifoCostPublication(id=str(uuid4()), shop_id=self.shop_id, warehouse_id=warehouse,
                kind='product', target_key=f'product:{self.ids[0]}:old-sale', subject='SHARED-A', status='uncertain',
                automatic=False, settings_revision=1, target_fingerprint=self.fingerprint,
                source_hash='a'*64, source={}, document={'products': []}, attempt_started_at=NOW))
        with self.assertRaisesRegex(links.LinkError, 'shared_stock_sale_operation_in_use'):
            await self.preview(request)
        self.absent_product.assert_not_awaited()
        async with self.transaction() as db:
            await db.execute(text('DELETE FROM fifo_cost_publications'))
            order = ShopOrder(shop_id=self.shop_id, external_id='prior-managed', order_date=NOW, stock_state='issued')
            db.add(order); await db.flush()
            db.add(ShopOrderItem(order_id=order.id, product_id=self.ids[0], external_item_id='old-line',
                                quantity=1, stock_managed=True))
        with self.assertRaisesRegex(links.LinkError, 'shared_stock_sale_history_requires_review'):
            await self.preview(request)
        self.absent_product.assert_not_awaited()
        self.assertEqual(sales._replacement_snapshot((await self.mappings())[0]), self.original)


@unittest.skipUnless(TEST_URL, 'Set CATALOG_TEST_DATABASE_URL to an isolated localhost *_catalog_test DB')
class SharedSaleOrderDatabaseTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = order_fixture.OrderStockDatabaseTests.asyncTearDown
    client = order_fixture.OrderStockDatabaseTests.client
    put = order_fixture.OrderStockDatabaseTests.put
    configure = order_fixture.OrderStockDatabaseTests.configure
    preview = order_fixture.OrderStockDatabaseTests.preview
    apply = order_fixture.OrderStockDatabaseTests.apply
    process = order_fixture.OrderStockDatabaseTests.process
    seed = order_fixture.OrderStockDatabaseTests.seed
    balance = order_fixture.OrderStockDatabaseTests.balance
    movements = order_fixture.OrderStockDatabaseTests.movements
    holds = order_fixture.OrderStockDatabaseTests.holds
    transaction = link_fixture.ProductLinksDatabaseTests.transaction

    async def asyncSetUp(self):
        await order_fixture.OrderStockDatabaseTests.asyncSetUp(self)
        async with self.transaction() as db:
            mappings = [ShopProduct(shop_id=self.shops['biketrek'], product_id=self.products['SKU-A'],
                external_code=f'PACK-{factor}', parent_code='PATCHES', is_variant=True,
                external_product_id='800', external_variant_id=str(100+factor),
                shared_stock=True, sale_to_stock_factor=D(factor), sale_barcodes=[]) for factor in (1, 8, 100)]
            db.add_all(mappings)
            await db.flush()
            self.mapping_ids = {int(row.sale_to_stock_factor): row.id for row in mappings}

    async def receive(self, quantity='200', cost='0.10'):
        async with self.transaction() as db:
            pid = self.products['SKU-A']
            db.add(confirmed_inventory(pid, self.warehouse_id))
            rows, _ = await lock_stock_balances(db, {pid}, self.warehouse_id)
            balance = rows[pid]
            balance.qty_on_hand += D(quantity)
            movement = StockMovement(idempotency_key='shared-receipt-'+uuid4().hex, product_id=pid,
                warehouse_id=self.warehouse_id, movement_type=MovementType.RECEIVING_IN,
                quantity=D(quantity), unit_cost=D(cost), total_cost=D(quantity)*D(cost),
                balance_after=balance.qty_on_hand, avg_cost_after=D(cost))
            db.add(movement)
            await db.flush()
            await fifo.add_receipt(db, balance, movement, NOW, D(cost), 'known',
                {'kind': 'documented_receipt', 'source_reference': 'Synthetic200piece receipt'})

    def sale(self, factor, quantity='1'):
        return raw_line(code=f'PACK-{factor}', quantity=quantity, product_id=800, option_set_id=1000+factor)

    async def test_buy200_sell8_leaves192_with_frozen_quantity_and_fifo_cost(self):
        await self.receive()
        raw = raw_order(self.sale(8))
        reserved = await self.process(raw)
        self.assertEqual((await self.balance('SKU-A'))[:2], (D(200), D(8)))
        raw['status_id'] = 8
        issued = await self.process(raw)
        self.assertEqual((await self.balance('SKU-A'))[:2], (D(192), D(0)))
        self.assertEqual(issued['lines'][0]['sale_quantity'], '1')
        self.assertEqual(issued['lines'][0]['quantity'], '8')
        async with self.sessions() as db:
            item = await db.scalar(select(ShopOrderItem).where(ShopOrderItem.order_id == issued['order_id']))
            order = await db.get(ShopOrder, issued['order_id'])
            self.assertEqual((item.sale_quantity, item.quantity, item.sale_to_stock_factor,
                              item.sale_mapping_id), (D(1), D(8), D(8), self.mapping_ids[8]))
            self.assertEqual(order.stock_snapshot['lines'][0]['quantity'], '1')
            self.assertEqual(order.stock_snapshot['lines'][0]['sale_to_stock_factor'], '8')
            movement = await db.get(StockMovement, issued['movement_ids'][0])
            self.assertEqual((movement.quantity, movement.total_cost), (D(-8), D('0.80')))
        self.assertEqual(await self.process(raw), issued)

    async def test_mixed_sales_share_one_pool_and_cancellation_releases_base_pieces(self):
        await self.seed('SKU-A', '200', '0.10')
        other = raw_order(self.sale(8), order_number='OTHER-8')
        await self.process(other)
        raw = raw_order(self.sale(1, '2'), self.sale(100))
        await self.process(raw)
        self.assertEqual((await self.balance('SKU-A'))[:2], (D(200), D(110)))
        raw.update(status_id=2, products=[])
        await self.process(raw)
        self.assertEqual((await self.balance('SKU-A'))[:2], (D(200), D(8)))
        self.assertEqual(await self.movements(), [])

    async def test_partial_physical_return_restores_pieces_and_does_not_reinterpret_sale(self):
        await self.receive()
        issued = await self.process(raw_order(self.sale(8), status_id=8))
        payload = FifoReturnInput(request_id=uuid4(), confirmed=True, reason='Customer returned three physical patches',
            issue_movement_id=issued['movement_ids'][0], quantity='3', case_reference='RETURN-3',
            condition='good', physical_received=True)
        async with self.transaction() as db:
            result = await fifo_returns.receive_return(db, payload)
        self.assertEqual(result['quantity'], '3')
        async with self.sessions() as db:
            balance = await db.scalar(select(StockBalance).where(StockBalance.product_id == self.products['SKU-A']))
            allocation = await db.scalar(select(FifoAllocation).where(FifoAllocation.issue_movement_id == issued['movement_ids'][0]))
            item = await db.scalar(select(ShopOrderItem).where(ShopOrderItem.order_id == issued['order_id']))
            movement = await db.get(StockMovement, issued['movement_ids'][0])
            self.assertEqual((balance.qty_on_hand, balance.qty_quarantined), (D(195), D(3)))
            self.assertEqual((allocation.quantity, allocation.returned_quantity), (D(8), D(3)))
            self.assertEqual((item.sale_quantity, item.sale_to_stock_factor, movement.quantity), (D(1), D(8), D(-8)))
        async with self.transaction() as db:
            self.assertEqual(await fifo_returns.receive_return(db, payload), result)

    async def test_factor_cannot_change_after_reservation_and_old_issue_is_never_recomputed(self):
        await self.seed('SKU-A', '200', '0.10')
        await self.process(raw_order(self.sale(8)))
        with self.assertRaises(IntegrityError):
            async with self.transaction() as db:
                await db.execute(update(ShopProduct).where(ShopProduct.id == self.mapping_ids[8])
                                 .values(sale_to_stock_factor=100))
        self.assertEqual((await self.balance('SKU-A'))[:2], (D(200), D(8)))
