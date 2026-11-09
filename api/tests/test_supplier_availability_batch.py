"""Availability batches isolate exact leaf errors without retrying remote calls."""
import unittest
from unittest.mock import patch

from inventory_hub.services import product_publication_source as transport
from inventory_hub.services.supplier_availability_publication import desired_label
import test_stock_sync_batch as fixture


class SupplierAvailabilityBatchTests(unittest.IsolatedAsyncioTestCase):
    async def read(self, targets, pages):
        client, responses = fixture.StockSyncBatchTests().client(pages)
        with patch.object(transport.source.UpgatesClient, 'from_shop', return_value=client):
            result = await transport.read_availability_many('biketrek', targets, fixture.FINGERPRINT)
        client.session.close.assert_called_once()
        client.session.put.assert_not_called()
        client.session.post.assert_not_called()
        for response in responses:
            response.close.assert_called_once()
        return result, client

    async def test_malformed_quantity_or_identity_only_rejects_its_exact_leaf(self):
        targets = [fixture.target('A', True), fixture.target('B', True)]
        for change, code in (({'stock': None}, 'stock_publication_stock_unknown'),
                             ({'variant_id': 123}, 'stock_publication_identity_changed'),
                             ({'availability': None}, 'supplier_availability_publication_remote_unknown')):
            with self.subTest(change=change):
                payload = fixture.page(targets)
                payload['variants'][0].update(change)
                result, client = await self.read(targets, [payload])
                self.assertEqual(result['A'].code, code)
                self.assertEqual(result['B']['identity'], targets[1])
                self.assertEqual(result['B']['leaf']['stock'], '4')
                client.session.get.assert_called_once()

    async def test_missing_code_is_not_zero_and_does_not_poison_returned_sibling(self):
        targets = [fixture.target('A', True), fixture.target('B', True)]
        result, client = await self.read(targets, [fixture.page(targets[:1])])
        self.assertEqual(result['A']['leaf']['stock'], '4')
        self.assertEqual(result['B'].code, 'stock_publication_source_missing')
        client.session.get.assert_called_once()
        payload = {'variants': [], 'current_page': 1, 'current_page_items': 0,
                   'number_of_items': 0, 'number_of_pages': 0}
        result, client = await self.read(targets, [payload])
        self.assertEqual({value.code for value in result.values()}, {'stock_publication_source_missing'})
        client.session.get.assert_called_once()

    async def test_unrelated_orderability_is_not_required_and_positive_stock_keeps_priority(self):
        targets = [fixture.target('A'), fixture.target('B')]
        payload = fixture.page(targets)
        del payload['products'][0]['can_add_to_basket_yn']
        payload['products'][1]['can_add_to_basket_yn'] = 'unrelated upstream value'
        result, client = await self.read(targets, [payload])
        for remote in result.values():
            self.assertEqual(desired_label(remote['leaf']['stock'], False, {'unavailable': 'vypredané'}), 'SKLADOM')
        client.session.get.assert_called_once()

    async def test_truncated_page_duplicate_or_extra_codes_cannot_certify_any_leaf(self):
        targets = [fixture.target('A', True), fixture.target('B', True)]
        good = fixture.page(targets)
        bodies = [dict(good, number_of_pages=2), dict(good, number_of_items=3),
                  dict(good, variants=good['variants'][:1]),
                  dict(good, variants=[good['variants'][0], good['variants'][0]]),
                  dict(good, variants=[good['variants'][0], {**good['variants'][1], 'code': 'EXTRA'}])]
        for body in bodies:
            with self.subTest(body=body):
                result, client = await self.read(targets, [body])
                self.assertTrue(all(isinstance(value, transport.source.SourceError) for value in result.values()))
                client.session.get.assert_called_once()

    async def test_bad_complete_group_does_not_poison_the_other_kind_or_fan_out(self):
        simple, variant = fixture.target('A'), fixture.target('B', True)
        malformed = fixture.page([simple])
        malformed['current_page'] = 2
        result, client = await self.read([simple, variant], [malformed, fixture.page([variant])])
        self.assertEqual(result['A'].code, 'stock_publication_invalid_response')
        self.assertEqual(result['B']['identity'], variant)
        self.assertEqual(client.session.get.call_count, 2)

    async def test_transport_failure_stops_without_request_retry(self):
        targets = [fixture.target('A'), fixture.target('B', True)]
        client, responses = fixture.StockSyncBatchTests().client([{}], status=503)
        with patch.object(transport.source.UpgatesClient, 'from_shop', return_value=client):
            with self.assertRaises(transport.source.SourceError):
                await transport.read_availability_many('biketrek', targets, fixture.FINGERPRINT)
        client.session.get.assert_called_once()
        client.session.put.assert_not_called()
        client.session.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
