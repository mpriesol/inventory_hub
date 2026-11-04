"""One-off batch transport must fail closed without repeating uncertain writes."""
from copy import deepcopy
import importlib.util
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from test_atomer_content_transport import parent, remote

spec = importlib.util.spec_from_file_location('atomer_direct_batch', Path(__file__).resolve().parents[2] / 'scripts/atomer_direct_batch.py')
batch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(batch)


def fixture():
    row = parent()
    parameters = [{'descriptions': [{'language': 'sk', 'name': 'Rozmer'}],
                   'values': [{'descriptions': [{'language': 'sk', 'value': '0,8 cm'}]}]}]
    row.update(code='ONE', variants_exists_yn=False, variants=[], parameters=parameters)
    row['descriptions'][0]['seo_title'] = 'Old title'
    identity = {'code': 'ONE', 'parent_code': 'ONE', 'product_id': 101,
                'variant_code': None, 'variant_id': None}
    before = remote(row, identity)
    before['identity']['parameters_yn'] = True
    values = {'long_description': '<p>New</p>', 'seo_title': 'New title',
              'seo_description': 'New SEO', 'parameters': parameters}
    result = batch.content.build_patch(values, before, batch.FIELDS)
    record = {'code': 'ONE', 'state': 'ready', 'source_sha256': 'hash', 'remote_before': before,
              'protected_before': batch.content.protected_observation(before, batch.FIELDS), **result}
    after = deepcopy(before)
    after['parent']['descriptions'][0].update({k: v for k, v in values.items() if k != 'parameters'})
    after['parent']['last_update_time'] = '2026-11-04 12:00:00'
    return record, before, after


class DirectBatchTests(TestCase):
    def test_price_uses_exact_vat_decimal(self):
        observed = {'parent': {'prices': [{'language': 'sk', 'currency': 'EUR', 'pricelists': [
            {'name': 'Default', 'price_sale': None, 'price_with_vat': '23.50'}]}]}}
        batch.verify_price(observed, {'price_vat': '23.500'}, {'pricelist': 'Default'})
        with self.assertRaisesRegex(batch.Blocked, 'price_differs'):
            batch.verify_price(observed, {'price_vat': '23.51'}, {'pricelist': 'Default'})

    def test_selected_content_and_stock_drift_prevent_sending(self):
        record, before, after = fixture()
        batch.revalidate(record, before)
        for observed, reason in ((after, 'content_changed'), (deepcopy(before), 'protected_fields')):
            if reason == 'protected_fields':
                observed['parent']['stock'] = 99
            with self.subTest(reason=reason), self.assertRaisesRegex(batch.Blocked, reason):
                batch.revalidate(record, observed)

    def test_native_identity_and_ack_are_required(self):
        record, before, after = fixture()
        self.assertEqual(batch.validate_outcome(record, after, True), ('completed', None))
        self.assertEqual(batch.validate_outcome(record, after, False)[0], 'uncertain')
        after['identity']['product_id'] = 999
        self.assertEqual(batch.validate_outcome(record, after, True)[0], 'uncertain')

    def test_protected_drift_after_write_requires_review(self):
        record, _, after = fixture()
        after['parent']['prices'] = [{'price_original': '1'}]
        self.assertEqual(batch.validate_outcome(record, after, True)[0], 'review_after_write')

    def test_successful_batch_has_durable_intent_and_second_pass_noop(self):
        record, before, after = fixture()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            batch.save(batch.record_path(root, 'ONE'), record)
            def send(method, endpoint, *, payload):
                self.assertEqual(method, 'PUT')
                self.assertEqual(batch.load(batch.record_path(root, 'ONE'))['state'], 'sending')
                self.assertEqual(len(list((root / 'batches').glob('*.json'))), 1)
                self.assertEqual(set(payload['products'][0]), {'code', 'descriptions', 'parameters'})
                return {'products': [{'code': 'ONE', 'product_id': 101, 'updated_yn': True}]}
            api = Mock(remaining=100)
            api.read.side_effect = [{'ONE': before}, {'ONE': after}, {'ONE': after}]
            api.request.side_effect = send
            sources = {'source_sha256': 'hash', 'records': [record]}
            batch.apply(api, sources, root, 0)
            batch.verify(api, sources, root)
            batch.apply(api, sources, root, 0)
            api.request.assert_called_once()
            self.assertTrue(batch.load(batch.record_path(root, 'ONE'))['second_pass']['no_op'])

    def test_timeout_and_partial_ack_are_never_retried(self):
        for result in (batch.Halt('network'), {'products': []}):
            with self.subTest(result=result), TemporaryDirectory() as directory:
                root = Path(directory)
                record, before, _ = fixture()
                batch.save(batch.record_path(root, 'ONE'), record)
                api = Mock(remaining=100)
                api.read.return_value = {'ONE': before}
                if isinstance(result, Exception):
                    api.request.side_effect = result
                else:
                    api.request.return_value = result
                sources = {'source_sha256': 'hash', 'records': [record]}
                with self.assertRaises(batch.Halt):
                    batch.apply(api, sources, root, 0)
                self.assertEqual(batch.load(batch.record_path(root, 'ONE'))['state'], 'uncertain')
                with self.assertRaisesRegex(batch.Halt, 'no_retry'):
                    batch.apply(api, sources, root, 0)
                api.request.assert_called_once()

    def test_malformed_or_unrequested_batch_response_refuses(self):
        good = {'current_page': 1, 'number_of_pages': 1, 'number_of_items': 1,
                'current_page_items': 1, 'products': [{'code': 'ONE'}]}
        api = batch.Api('test', 'test', Path('/unused'))
        for changed in ({'number_of_pages': 2}, {'products': [{'code': 'OTHER'}]},
                        {'current_page_items': 0}, {'products': [{'code': 'ONE'}, {'code': 'ONE'}]}):
            with self.subTest(changed=changed), patch.object(api, 'request', return_value={**good, **changed}):
                with self.assertRaises(batch.Halt):
                    api.products(['ONE'])
        api.session.close()

    def test_stale_source_selection_isolated_but_unsafe_xml_not_ignored(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'source.xml'
            path.write_text('<PRODUCTS><PRODUCT><PRODUCT_ID>ONE</PRODUCT_ID></PRODUCT></PRODUCTS>')
            parsed, errors = batch.parse_candidates(path, ['ONE', 'MISSING'])
            self.assertEqual(set(parsed['groups']), {'ONE'})
            self.assertEqual(errors, {'MISSING': 'atomer_source_selection_missing'})
            path.write_text('<!DOCTYPE PRODUCTS><PRODUCTS/>')
            with self.assertRaisesRegex(batch.AtomerSourceError, 'dtd_forbidden'):
                batch.parse_candidates(path, ['ONE', 'MISSING'])

    def test_other_host_cannot_receive_image_request(self):
        with patch.object(batch.requests, 'get') as get:
            with self.assertRaises(batch.Blocked):
                batch.image_bytes('https://untrusted.example/image.jpg', Path('/unused'))
            get.assert_not_called()
