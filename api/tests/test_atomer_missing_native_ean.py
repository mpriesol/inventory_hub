import unittest
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock, patch

from inventory_hub.services import atomer_missing_native_ean as fill
from inventory_hub.services.atomer_card_batch import ObservedCatalog


class BarcodeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.row = fill.Item(code='TEST-SKU', source_id='11', ean='4006381333931', card=10, xtrek_id=22,
                             xtrek_link=33, bike_id=44, bike_variant=45, bike_link=55)
        self.s = fill.Selection(run_id=6, source_sha='a'*64, admin_sha='b'*64, items=[self.row])
        self.remote = {'TEST-SKU': dict(xtrek=dict(code='TEST-SKU', product_id=22, ean=None, stock=9, variants=[]),
                                        biketrek=dict(code='TEST-SKU', variant_id=45, stock=10))}
        self.local = {'TEST-SKU': dict(card=10, sku='TEST-SKU', movement_digest='history', links=[33, 55])}
        self.jobs = {}
        self.db = Mock()
        self.db.scalar = AsyncMock(return_value=None)
        self.db.commit = AsyncMock()
        self.db.add.side_effect = lambda obj: self.jobs.update({obj.id: obj})
        self.db.get = AsyncMock(side_effect=lambda cls,key,**kw: self.jobs.get(key))

    def test_source_requires_both_the_original_gtin_and_sole_owner_in_hidden_source(self):
        src = {'code': self.row.code, 'parent':'', 'ean':self.row.ean}
        admin = {self.row.code: dict(src, has_catalog_code=True, internal_id='11')}
        fill._source([src], admin, [self.row])
        admin['HIDDEN'] = dict(ean=self.row.ean)
        with self.assertRaisesRegex(fill.ReviewError, 'source_conflict'):
            fill._source([src], admin, [self.row])

    def test_complete_census_includes_non_sellable_parent_gtin_and_cross_namespace_sku(self):
        root = self.remote['TEST-SKU']['xtrek']
        fill._ownership([root], [self.row])
        for other in [dict(product_id=8, code='OLD', ean=self.row.ean, variants=[dict(code='CHILD',variant_id=101)]),
                      dict(product_id=8, code='OTHER', variants=[dict(code='test-sku',variant_id=101)])]:
            with self.assertRaisesRegex(fill.ReviewError, 'occupied'):
                fill._ownership([root, other], [self.row])

    def test_operator_closure_and_other_shop_scope_stop_new_writes(self):
        for changed in [dict(status='done'), dict(scope='biketrek'), dict(status='working')]:
            with self.assertRaises(fill.ReviewError):
                fill._guard_review(dict(dict(revision=1, status='open', scope='both'), **changed))

    def mocks(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        def setmock(owner, method, replacement):
            stack.enter_context(patch.object(owner, method, replacement))
        sk = self.row.code
        def census(shop):
            return ObservedCatalog([deepcopy(self.remote[sk]['xtrek'])], datetime.now(timezone.utc), 'x')
        setmock(fill, 'target_fingerprint', lambda s: 'x' if s=='xtrek' else 'b')
        setmock(fill, '_scope', AsyncMock(side_effect=lambda *args: deepcopy(self.local)))
        setmock(fill.evidence, 'read_sources', AsyncMock(return_value=([], {})))
        setmock(fill, '_source', Mock())
        setmock(fill.atomer_source_state, 'catalog_with_admin', Mock(return_value=[]))
        setmock(fill.cards, 'load_shop_catalog', AsyncMock(side_effect=census))
        setmock(fill, '_targets', lambda *args: deepcopy(self.remote))
        setmock(fill, '_jobs', AsyncMock(side_effect=lambda db,keys: {k:self.jobs.get(i) for k,i in keys.items()}))
        setmock(fill.native, '_before_write', Mock(return_value=ExitStack()))
        setmock(fill.observation, 'verify_read_window', Mock())
        self.send = Mock()
        setmock(fill.gen, 'send', self.send)

    async def prepare(self):
        preview = await fill.process(self.db, self.s, 'preview', 'operator')
        self.assertEqual(preview['status'],'ready')
        return self.s.model_copy(update={'preview_sha': preview['preview_sha']})

    async def test_only_ean_dispatched_after_committed_intent_and_both_native_and_local_readbacks(self):
        self.mocks()
        request = await self.prepare()
        def update(payload, shop):
            self.assertEqual(len(self.jobs), 1)
            self.assertEqual(next(iter(self.jobs.values())).state, 'sending')
            self.db.commit.assert_awaited()
            self.assertEqual(payload, [{'code':self.row.code,'ean':self.row.ean}])
            self.remote[self.row.code]['xtrek']['ean'] = self.row.ean
        self.send.side_effect = update
        result = await fill.process(self.db, request, 'apply', 'operator')
        self.assertEqual(result['status'], 'verified')
        self.assertEqual(result['rows'][0]['product_id'], 22)
        self.remote[self.row.code]['xtrek']['stock'] = 8  # a later legitimate sale
        self.assertEqual((await fill.process(self.db, request, 'apply', 'operator'))['rows'][0]['outcome'], 'already_present')
        self.assertEqual(self.send.call_count, 1)

    async def test_ambiguous_request_is_never_resent_even_if_previous_read_showed_empty(self):
        self.mocks()
        request = await self.prepare()
        self.send.side_effect = fill.transport.SourceError('lost', uncertain=True, request_sent=True)
        for _ in range(2):
            self.assertEqual((await fill.process(self.db, request, 'apply', 'operator'))['status'], 'uncertain')
        self.remote[self.row.code]['xtrek']['ean'] = self.row.ean
        self.assertEqual((await fill.process(self.db, request, 'apply', 'operator'))['status'], 'verified')
        self.assertEqual(self.send.call_count,1)

    async def test_local_or_native_change_since_operator_preview_requires_new_preview(self):
        self.mocks()
        request = await self.prepare()
        self.local[self.row.code]['movement_digest'] = 'changed'
        with self.assertRaisesRegex(fill.ReviewError, 'preview_changed'):
            await fill.process(self.db, request, 'apply', 'operator')
        self.send.assert_not_called()
        self.assertFalse(self.jobs)
