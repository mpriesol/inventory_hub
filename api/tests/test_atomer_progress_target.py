from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, Mock, patch
import zlib

from inventory_hub.services import atomer_progress_target as evidence
from inventory_hub.services.atomer_card_batch import ObservedCatalog


SOURCE, ADMIN, FP = '1'*64, '2'*64, '3'*64
T = datetime.now(timezone.utc) - timedelta(days=2)


def flat():
    return [dict(code='SKU', pid='1', vid='2', ean='', brand='M', axes={'x': ['a']}, availability='A', stock=0)]


def snapshot():
    seal = evidence.pack(flat(), SOURCE, ADMIN, FP, T)
    return dict(digest=seal['digest'], source_sha=SOURCE, admin_sha=ADMIN, shop_fingerprint=FP,
                observed_at=T, item_count=1, compressed=seal['blob'])


class TargetTests(unittest.TestCase):
    def test_seal_rejects_corruption_partial_payload_or_wrong_account_and_keeps_original_time(self):
        row = snapshot()
        self.assertEqual(evidence.unpack(row, SOURCE, ADMIN, FP), (flat(), T))
        for changed in [dict(item_count=2), dict(shop_fingerprint='another'), dict(admin_sha='changed'),
                        dict(observed_at=T + timedelta(seconds=1)),
                        dict(compressed=row['compressed'] + b'garbage')]:
            with self.subTest(keys=list(changed)), self.assertRaises(evidence.export.ExportError):
                evidence.unpack({**row, **changed}, SOURCE, ADMIN, FP)
        raw = json.loads(zlib.decompress(row['compressed']))
        raw['rows'][0]['code'] = 'WRONG'
        with self.assertRaises(evidence.export.ExportError):
            evidence.unpack({**row, 'compressed': zlib.compress(json.dumps(raw).encode())}, SOURCE, ADMIN, FP)
        with self.assertRaises(evidence.export.ExportError):
            evidence.pack(flat() + flat(), SOURCE, ADMIN, FP, T)

    def test_bounded_native_axes_resume_on_unchanged_revision_and_invalidate_edited_family(self):
        parents = [dict(product_id=1, code='ROOT', variants_exists_yn=True, last_update_time=T.isoformat(),
                        variants=[dict(product_id=1, product_code='ROOT', variant_id=2, code='LEAF', ean='')])]
        params = [dict(product_id=1, code='ROOT', variants=[dict(variant_id=2, code='LEAF', parameters=[])])]
        with TemporaryDirectory() as folder, patch.object(evidence.observation, 'total', return_value=1), \
             patch.object(evidence.observation, 'page_rows', return_value=params) as get:
            args = (Mock(), parents, FP, datetime.now(timezone.utc), Path(folder))
            original = evidence._parameter_rows(*args)
            self.assertEqual(evidence._parameter_rows(*args), original)
            self.assertEqual(get.call_count, 1)
            parents[0]['last_update_time'] = (T + timedelta(seconds=3)).isoformat()
            evidence._parameter_rows(*args)
            self.assertEqual(get.call_count, 2)
            parents[0]['variants'][0]['code'] = 'WRONG'
            with self.assertRaises(evidence.export.ExportError):
                evidence._parameter_rows(*args)


class ReindexTests(unittest.IsolatedAsyncioTestCase):
    async def test_pinned_reindex_reads_current_bike_and_zero_xtrek_pages(self):
        stored = snapshot()
        head = dict(source_sha256=SOURCE, admin_export_sha256=ADMIN, totals={'xtrek_target_snapshot': stored['digest']})
        db = AsyncMock()
        result = Mock()
        result.mappings.return_value.first.return_value = stored
        db.execute.return_value = result
        bike = ObservedCatalog([dict(product_id=3, code='LOCAL', variants=[])], datetime.now(timezone.utc), 'bike-fp')
        with patch.object(evidence, 'target_fingerprint', side_effect=lambda shop: FP if shop == 'xtrek' else 'bike-fp'), \
             patch('inventory_hub.services.atomer_card_batch.load_shop_catalog', new=AsyncMock(return_value=bike)) as load:
            rows, bikes, observed, meta = await evidence.read_target(db, head, SOURCE, ADMIN, 'pinned_source')
            self.assertEqual(rows, flat())
            self.assertIn(('3', None), bikes)
            self.assertEqual(observed, T)
            self.assertEqual(meta['xtrek_observed_at'], T.isoformat())
            load.assert_awaited_once_with('biketrek')
            with self.assertRaises(evidence.export.ExportError):
                await evidence.read_target(db, dict(head, totals={}), SOURCE, ADMIN, 'pinned_source')
            self.assertEqual(load.await_count, 1)
