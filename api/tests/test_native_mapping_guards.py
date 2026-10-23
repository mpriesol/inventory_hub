"""A missing normalized native identity never authorizes a cost publication."""
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from inventory_hub.services import fifo_cost_sync


class NativeCostMappingTests(IsolatedAsyncioTestCase):
    async def test_unverified_mapping_is_blocked_before_product_or_remote_reads(self):
        for variant, parent, leaf in ((False, None, None), (True, None, '20'), (True, '10', None)):
            mapping = SimpleNamespace(is_listed=True, is_variant=variant,
                external_product_id=parent, external_variant_id=leaf)
            db = SimpleNamespace(get=AsyncMock(), execute=AsyncMock())
            with self.subTest(variant=variant, parent=parent, leaf=leaf), \
                    patch.object(fifo_cost_sync, 'one', AsyncMock(return_value=mapping)), \
                    patch.object(fifo_cost_sync.source, 'read_product', AsyncMock()) as remote:
                with self.assertRaisesRegex(fifo_cost_sync.CostSyncError, 'fifo_cost_mapping_unverified'):
                    await fifo_cost_sync.product_target(db, 1, 7, 'SKU-A')
                db.get.assert_not_called()
                db.execute.assert_not_called()
                remote.assert_not_called()
