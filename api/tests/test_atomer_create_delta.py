from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from inventory_hub.services import atomer_create_delta as delta


FP = 'a' * 64


def parent(i, **kwargs):
    return dict(dict(product_id=i, code='SKU-'+str(i), variants_exists_yn=False, variants=[]), **kwargs)


def page(rows, total=None, index=1):
    total = len(rows) if total is None else total
    return dict(products=rows, current_page=index, current_page_items=len(rows),
                number_of_items=total, number_of_pages=(total+99)//100)


class ContinuousDeltaTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dir = Path(folder.name)
        self.before = datetime.now(timezone.utc) - timedelta(days=5)
        self.keys = [('ean', '04006381333931'), ('code', 'sku-101'), ('code', 'sku-777')]

    def lookup(self):
        return delta.candidates(Mock(), FP, self.before, self.keys, self.dir)

    def test_resume_exactly_the_unfinished_page_then_current_tail_and_keep_committed_candidates(self):
        roots = [parent(i) for i in range(1, 101)]
        roots[0]['ean'] = '4006381333931'
        with patch.object(delta.export, '_read', side_effect=[page(roots,101),delta.export.ExportError('quota')]) as get:
            with self.assertRaises(delta.export.ExportError):
                self.lookup()
            self.assertEqual([c.args[2]['page'] for c in get.call_args_list], [1, 2])
        with patch.object(delta.export, '_read', side_effect=[page([parent(101)], 101, 2), page([parent(777)])]) as get:
            self.assertEqual(self.lookup(), {1,101,777})
            self.assertEqual([c.args[2]['page'] for c in get.call_args_list], [2, 1])
            first, tail = [c.args[2]['last_update_time_from'] for c in get.call_args_list]
            self.assertGreater(tail, first)
        with patch.object(delta.export, '_read', return_value=page([])) as get:
            self.assertEqual(self.lookup(), {1, 101, 777})
            self.assertEqual(get.call_count, 1)
            self.assertGreater(datetime.fromisoformat(get.call_args.args[2]['last_update_time_from']), self.before)

    def _checkpoint(self, phase):
        root = self.dir / 'atomer-card-catalog-v1'
        root.mkdir()
        stamp = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
        with sqlite3.connect(root / (FP + '.sqlite3')) as db:
            db.execute('CREATE TABLE cursor(id INTEGER,document TEXT)')
            db.execute('CREATE TABLE parents(id INTEGER,payload TEXT)')
            state = dict(phase=phase, page=1, delta_count=None, phase_before_delta='validate',
                         base_count=1, final_started=stamp, base_started=stamp)
            db.execute('INSERT INTO cursor VALUES(1,?)', (json.dumps(state),))
            db.execute('INSERT INTO parents VALUES(777,?)', (json.dumps(parent(777)),))
        return stamp

    def test_full_certified_target_is_a_seed_but_still_requires_a_current_native_delta(self):
        self._checkpoint('ready')
        with patch.object(delta.export, '_read', return_value=page([])) as get:
            self.assertEqual(self.lookup(), {777})
            self.assertEqual(get.call_count,1)
            since = datetime.fromisoformat(get.call_args.args[2]['last_update_time_from'])
            self.assertGreater(since, self.before + timedelta(days=1))

    def test_partial_account_checkpoint_cannot_prove_source_absence_or_supply_a_seed(self):
        self._checkpoint('pages')
        with patch.object(delta.export, '_read', return_value=page([])) as get:
            self.assertEqual(self.lookup(), set())
            self.assertLess(datetime.fromisoformat(get.call_args.args[2]['last_update_time_from']), self.before)

    def test_cross_account_state_is_never_shared_and_a_newly_seen_foreign_gtin_stops_the_write(self):
        with patch.object(delta.export, '_read', return_value=page([parent(5, code='4006381333931')])):
            self.assertEqual(self.lookup(), {5})
        with patch.object(delta.export, '_read', return_value=page([])):
            self.assertEqual(delta.candidates(Mock(), 'b'*64, self.before, self.keys, self.dir), set())
