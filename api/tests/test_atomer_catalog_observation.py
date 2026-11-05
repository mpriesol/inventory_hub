from copy import deepcopy
from datetime import datetime, timezone
from unittest import TestCase
from unittest.mock import patch

from inventory_hub.services import atomer_catalog_observation as observe
from inventory_hub.services.migration_upgates_export import ExportError


def parent(pid,vid):
    code = f'parent-{pid}'
    leaf = dict(product_id=pid,product_code=code,variant_id=vid,code=f'leaf-{vid}',ean='123')
    return dict(product_id=pid,code=code,variants_exists_yn=True,variants=[leaf])


class NativeCatalogObservationTests(TestCase):
    def test_complete_current_axes_and_stable_identity_for_each_leaf(self):
        roots=[parent(12,35),parent(13,36)]
        current = deepcopy(roots)
        for p in current:
            for v in p['variants']:
                del v['ean']
                v['parameters']=[{'name':{'sk':'size'},'values':[{'sk':'L'}]}]
        with patch.object(observe,'total',return_value=2),patch.object(observe,'page_rows',return_value=current) as read:
            values=list(observe.parameter_rows(None,roots))
            self.assertEqual([v['variant_id'] for v in values],[35,36])
            self.assertEqual(values[1]['ean'],'123')
            self.assertEqual(read.call_args.args[2],{'product_ids':'12;13'})
        current[1]['variants'][0]['code']='something-else'
        with patch.object(observe,'total',return_value=2),patch.object(observe,'page_rows',return_value=current):
            with self.assertRaisesRegex(ExportError,'catalog_changed'):
                list(observe.parameter_rows(None,roots))

    def test_changed_parent_replaces_cache_and_tombstone_forces_full_read(self):
        now=datetime.now(timezone.utc)
        cached={'pulled_at':now.isoformat(),'products':[parent(12,35)]}
        updated=parent(12,36); added=parent(13,78)
        with patch.object(observe,'total',return_value=2),patch.object(observe,'page_rows',return_value=[updated,added]):
            self.assertEqual(observe.recent_products(None,cached,now),[updated,added])
        with patch.object(observe,'total',return_value=1),patch.object(observe,'page_rows',return_value=[added]):
            self.assertIsNone(observe.recent_products(None,cached,now))

    def test_exact_import_reads_the_matching_native_parent_again(self):
        a,b=parent(1,10),parent(2,20)
        updated=deepcopy(a); updated['variants'][0]['ean']='new'
        with patch.object(observe,'recent_products',return_value=[a,b]),patch.object(observe,'page_rows',return_value=[updated]) as get:
            result=observe.selection_products(None,{},['leaf-10'])
        self.assertEqual(get.call_args.args[2],{'product_ids':'1'})
        self.assertEqual(result,[updated,b])
