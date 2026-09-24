"""Operator permission boundary and immutable issued-line checks."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from pydantic import ValidationError
from inventory_hub.fifo_cost_types import CostShopInput, CostOrderPreviewInput, CostResolveInput
from inventory_hub.services import fifo_cost_sync as service


class FifoCostSyncTests(unittest.TestCase):
    def test_confirmation_flags_and_cutoff_cannot_be_forged(self):
        values = dict(shop_code='biketrek', warehouse_code='central', expected_revision=0, enabled=False,
                      product_cost_enabled=True, order_cost_enabled=True, confirmed=True)
        CostShopInput(**values)
        for change in ({'confirmed': 1}, {'enabled': 'true'}, {'expected_revision': '0'},
                       {'orders_since': '2000-01-01T00:00:00Z'}, {'batch_size': 101}):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                CostShopInput(**(values | change))

    def test_preview_exact_order_and_settlement_are_explicit(self):
        for value in (' TEST-1', 'TEST-1\n', ''):
            with self.assertRaises(ValidationError):
                CostOrderPreviewInput(shop_code='biketrek', order_number=value, confirmed=True)
        for value in (False, 1, 'true'):
            with self.assertRaises(ValidationError):
                CostResolveInput(confirmed=True, original_request_settled=value, note='Confirmed in administration')
        with self.assertRaises(ValidationError):
            CostResolveInput(confirmed=True, original_request_settled=True, note='     ')

    def test_no_stock_authority_requirement_and_manual_schedule_independence(self):
        shop = SimpleNamespace(code='biketrek')
        warehouse = SimpleNamespace(is_active=True)
        policy = SimpleNamespace(target_fingerprint='f', product_cost_enabled=True,
                                 order_cost_enabled=False, enabled=False)
        with patch.object(service, 'enabled', return_value=True), patch.object(service, 'target_fingerprint', return_value='f'):
            self.assertEqual(service.blockers(shop, warehouse, policy, kind='product'), [])
            self.assertIn('fifo_cost_scope_disabled', service.blockers(shop, warehouse, policy, kind='order'))
            self.assertIn('fifo_cost_schedule_disabled', service.blockers(shop, warehouse, policy, automatic=True))
        with patch.object(service, 'enabled', return_value=False), patch.object(service, 'target_fingerprint', return_value='f'):
            self.assertIn('fifo_cost_server_disabled', service.blockers(shop, warehouse, policy))
            self.assertEqual(service.blockers(shop, warehouse, policy, require_write=False), [])

    def test_added_manual_line_blocks_old_order_repricing(self):
        evidence = {'kind': 'order', 'source_uuid': 'original', 'source_lines': [{'line_key': 'a'}],
                    'lines': [{'line_key': 'a', 'code': 'SKU', 'quantity': '3', 'unit_cost': '20'}]}
        remote = {'identity': {'uuid': 'original'}, 'source_lines': [{'line_key': 'a'}, {'line_key': 'manual'}]}
        with patch.object(service.source, 'prepare_order') as prepare:
            with self.assertRaises(service.CostSyncError) as error:
                service.prepare_remote(remote, evidence)
            self.assertEqual(error.exception.code, 'fifo_cost_order_lines_changed')
            prepare.assert_not_called()
            remote['source_lines'] = deepcopy(evidence['source_lines'])
            service.prepare_remote(remote, evidence)
            self.assertEqual(prepare.call_args.args[1][0]['unit_cost_net'], '20')
