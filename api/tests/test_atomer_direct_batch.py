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
    def price_fixture(self):
        record, before, after = fixture()
        before['parent']['action_currently_yn'] = False
        before['parent']['prices'] = [{'language': 'sk', 'currency': 'EUR', 'vat': 23,
            'price_purchase': '5.00', 'price_common': '15.00', 'pricelists': [
                {'name': 'Default', 'price_original': '10', 'price_with_vat': '10',
                 'price_without_vat': '8.13', 'price_sale': None, 'product_discount': None,
                 'product_discount_real': 0}]}]
        options = {'pricelist': 'Default', 'currency': 'EUR', 'prices_with_vat': True}
        record['price_patch'] = batch.content.build_source_price_patch(before, '12.30', '23', options)
        record['source_candidate'] = {'code': 'ONE', 'price_vat': '12.30', 'group': {'rows': [{'raw_xml': '<PRODUCT><VAT>23</VAT></PRODUCT>'}]}}
        record['payload']['products'][0]['prices'] = deepcopy(record['price_patch']['payload']['products'][0]['prices'])
        record['protected_before'] = batch.content.protected_observation(before, batch.FIELDS)
        after['parent']['action_currently_yn'] = False
        after['parent']['prices'] = deepcopy(before['parent']['prices'])
        after['parent']['prices'][0]['pricelists'][0].update(record['price_patch']['after'])
        return record, before, after

    def test_authorized_price_and_content_readback_keeps_other_price_fields(self):
        record, before, after = self.price_fixture()
        batch.revalidate(record, before)
        self.assertEqual(batch.validate_outcome(record, after, True), ('completed', None))
        after['parent']['prices'][0]['price_purchase'] = '0'
        self.assertEqual(batch.validate_outcome(record, after, True)[0], 'review_after_write')

    def test_source_price_mismatch_or_discount_is_not_success(self):
        record, before, after = self.price_fixture()
        price = after['parent']['prices'][0]['pricelists'][0]
        price['price_with_vat'] = '11.07'
        self.assertEqual(batch.validate_outcome(record, after, True), ('uncertain', 'readback_price_differs'))
        for key, value in [('price_sale', '9'), ('product_discount_real', 10), ('product_discount_real', None)]:
            observed = deepcopy(before)
            observed['parent']['prices'][0]['pricelists'][0][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(batch.content.source.SourceError):
                batch.content.build_source_price_patch(observed, '12.3', '23', record['price_patch']['options'])

    def test_price_payload_cannot_change_other_fields_or_source_value(self):
        for change in ('stock', 'price_purchase', 'gross'):
            record, before, _ = self.price_fixture()
            if change == 'stock':
                record['payload']['products'][0]['stock'] = 100
            elif change == 'price_purchase':
                record['payload']['products'][0]['prices'][0]['price_purchase'] = 1
            else:
                record['price_patch']['gross'] = '1'
            with self.subTest(change=change), self.assertRaises((batch.Blocked, batch.content.source.SourceError)):
                batch.revalidate(record, before)

    def test_content_noop_does_not_skip_a_price_change(self):
        record, _, _ = self.price_fixture()
        record['after'] = deepcopy(record['before'])
        self.assertFalse(batch.record_is_noop(record))
        record['price_patch']['after'] = deepcopy(record['price_patch']['before'])
        self.assertTrue(batch.record_is_noop(record))

    def test_price_configuration_change_stops_before_write(self):
        record, _, _ = self.price_fixture()
        api = Mock()
        api.request.return_value = {'config': {'prices_with_vat_yn': False}, 'languages': [], 'pricelists': []}
        with self.assertRaisesRegex(batch.Halt, 'price_configuration_changed'):
            batch.revalidate_price_config(api, [record])
        api.request.assert_called_once_with('GET', 'config')

    def test_price_vat_and_precision_are_exact(self):
        record, before, _ = self.price_fixture()
        for gross, vat in [('12.345', '23'), ('12.30', '20'), (None, '23')]:
            with self.subTest(gross=gross, vat=vat), self.assertRaises(batch.content.source.SourceError):
                batch.content.build_source_price_patch(before, gross, vat, record['price_patch']['options'])

    def test_price_batch_verifies_and_does_not_repeat(self):
        record, before, after = self.price_fixture()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            batch.save(batch.record_path(root, 'ONE'), record)
            api = Mock(remaining=100)
            api.read.side_effect = [{'ONE': before}, {'ONE': after}, {'ONE': after}]
            def request(method, endpoint, **kwargs):
                if method == 'GET':
                    self.assertEqual(endpoint, 'config')
                    return {'config': {'prices_with_vat_yn': True},
                            'languages': [{'language_id': 'sk', 'currency_id': 'EUR', 'active_yn': True}],
                            'pricelists': [{'name': 'Default', 'default_yn': True}]}
                product = kwargs['payload']['products'][0]
                self.assertEqual(set(product), {'code', 'descriptions', 'parameters', 'prices'})
                self.assertEqual(product['prices'], [{'language': 'sk', 'pricelists': [{'name': 'Default', 'price_original': 12.3}]}])
                return {'products': [{'code': 'ONE', 'product_id': 101, 'updated_yn': True}]}
            api.request.side_effect = request
            sources = {'source_sha256': 'hash', 'price_policy': 'atomer', 'records': [record['source_candidate']]}
            batch.apply(api, sources, root, 0)
            batch.verify(api, sources, root)
            batch.apply(api, sources, root, 0)
            self.assertEqual(sum(c.args[0] == 'PUT' for c in api.request.call_args_list), 1)
            self.assertTrue(batch.load(batch.record_path(root, 'ONE'))['second_pass']['no_op'])

    def test_price_selection_is_exact_and_requires_separate_journal(self):
        import hashlib
        with TemporaryDirectory() as directory:
            root = Path(directory); old = root / 'old'; new = root / 'new'; new.mkdir()
            xml = root / 'source.xml'; xml.write_text('<PRODUCTS/>')
            sha = hashlib.sha256(xml.read_bytes()).hexdigest()
            candidate = {'code': 'ONE', 'state': 'source_ready'}
            batch.save(old / 'sources.json', {'source_sha256': sha, 'records': [candidate]})
            batch.save(batch.record_path(old, 'ONE'), {'code': 'ONE', 'state': 'blocked_preflight',
                'reason': 'source_target_price_differs', 'source_sha256': sha, 'source_candidate': candidate})
            selected = batch.prepare_price_conflicts(xml, root / 'unused.zip', new, old, sha)
            self.assertEqual(selected['records'], [candidate])
            self.assertEqual(selected['price_policy'], 'atomer')
            with self.assertRaisesRegex(batch.Halt, 'separate_journal'):
                batch.prepare_price_conflicts(xml, root / 'unused.zip', old, old, sha)
            with self.assertRaisesRegex(batch.Halt, 'source_changed'):
                batch.prepare_price_conflicts(xml, root / 'unused.zip', new, old, 'wrong')

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
