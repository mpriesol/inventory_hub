"""Issued-document transport never leaves the configured API or writes remotely."""
from datetime import date
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import requests

from inventory_hub.services import shop_invoice_source as source
from inventory_hub.services.upgates import connection_fingerprint

BASE = 'https://fixture.invalid/api/v2'
FINGERPRINT = connection_fingerprint(BASE, 'fixture-login')
PDF = b'%PDF-1.7\nsynthetic fixture, no customer data\n%%EOF\n'


class Response:
    def __init__(self, content, content_type='application/json', status=200, headers=None):
        self.content = json.dumps(content).encode() if isinstance(content, dict) else content
        self.headers = {'Content-Type': content_type, **(headers or {})}
        self.status_code, self.closed = status, False

    def iter_content(self, size):
        for offset in range(0, len(self.content), size):
            yield self.content[offset:offset + size]

    def close(self):
        self.closed = True


class Session:
    auth = ('fixture-login', 'fixture-key')

    def __init__(self, responses):
        self.responses, self.calls, self.closed = responses, [], False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        value = self.responses[len(self.calls) - 1]
        if isinstance(value, Exception):
            raise value
        return value

    def close(self):
        self.closed = True


def invoice_page(numbers=('2026/001',), **changes):
    return {'current_page': 1, 'current_page_items': len(numbers), 'number_of_items': len(numbers),
            'number_of_pages': 1, 'invoices': [{'invoice_number': number} for number in numbers], **changes}


class ShopInvoiceSourceTests(unittest.IsolatedAsyncioTestCase):
    def client(self, *responses, base=BASE):
        session = Session(list(responses))
        client = SimpleNamespace(base_url=base, session=session, log_dir='must-not-write')
        patcher = patch.object(source.UpgatesClient, 'from_shop', return_value=client)
        patcher.start()
        self.addCleanup(patcher.stop)
        return client, session

    async def test_list_is_one_bounded_read_of_selected_source_creation_period(self):
        response = Response(invoice_page())
        client, session = self.client(response)
        result = await source.read_page('biketrek', date(2026, 10, 1), date(2026, 10, 31), 1)
        self.assertEqual((result['total'], result['pages'], result['invoices'][0]['invoice_number']), (1, 1, '2026/001'))
        url, options = session.calls[0]
        self.assertEqual(url, BASE + '/invoices')
        self.assertEqual(options['params'], {'page': 1, 'creation_time_from': '2026-10-01T00:00:00+02:00',
            'creation_time_to': '2026-10-31T23:59:59+01:00'})
        self.assertEqual(len(session.calls), 1)
        self.assertFalse(options['allow_redirects'])
        self.assertTrue(options['verify'])
        self.assertTrue(response.closed and session.closed)
        self.assertIsNone(client.log_dir)

    async def test_strict_provider_accepts_whole_second_cutoff_on_first_get_without_fallback(self):
        _, session = self.client()
        responses = []

        def provider(url, **options):
            self.assertEqual(url, BASE + '/invoices')
            params = options['params']
            self.assertEqual(params['creation_time_from'], '2026-10-01T00:00:00+02:00')
            self.assertEqual(params['page'], 2)
            response = (Response({'messages': [{'property': 'creation_time_to', 'level': 'error'}]}, status=400)
                if '.' in params['creation_time_to'] else Response(invoice_page(
                    current_page=2, number_of_pages=3, number_of_items=3)))
            responses.append(response)
            return response

        with patch.object(session, 'get', side_effect=provider) as get:
            result = await source.read_page('biketrek', date(2026, 10, 1), date(2026, 10, 23), 2)
        get.assert_called_once()
        self.assertEqual(get.call_args.kwargs['params']['creation_time_to'], '2026-10-23T23:59:59+02:00')
        self.assertEqual((result['page'], result['pages'], result['total']), (2, 3, 3))
        self.assertEqual(len(result['page_token']), 64)
        self.assertTrue(session.closed and all(response.closed for response in responses))

    async def test_selected_calendar_day_bounds_keep_bratislava_dst_offsets(self):
        periods = (
            (date(2026, 3, 1), date(2026, 3, 31), '2026-03-01T00:00:00+01:00', '2026-03-31T23:59:59+02:00'),
            (date(2026, 3, 29), date(2026, 3, 29), '2026-03-29T00:00:00+01:00', '2026-03-29T23:59:59+02:00'),
            (date(2026, 10, 25), date(2026, 10, 25), '2026-10-25T00:00:00+02:00', '2026-10-25T23:59:59+01:00'),
        )
        for date_from, date_to, start, end in periods:
            with self.subTest(date_from=date_from, date_to=date_to):
                response = Response(invoice_page())
                _, session = self.client(response)
                await source.read_page('biketrek', date_from, date_to, 1)
                self.assertEqual(len(session.calls), 1)
                self.assertEqual(session.calls[0][1]['params'], {
                    'creation_time_from': start, 'creation_time_to': end, 'page': 1})
                self.assertTrue(response.closed and session.closed)

    async def test_original_pdf_uses_encoded_native_number_and_fixed_authenticated_path(self):
        response = Response(PDF, 'application/pdf')
        _, session = self.client(response)
        result = await source.read_pdf('biketrek', '2026/001 ?x=1', FINGERPRINT)
        self.assertEqual(result, PDF)
        self.assertEqual(session.calls[0][0], BASE + '/invoices/2026%2F001%20%3Fx%3D1/pdf')
        self.assertEqual(session.calls[0][1]['headers'], {'Accept': 'application/pdf'})
        self.assertFalse(session.calls[0][1]['allow_redirects'])
        self.assertTrue(response.closed and session.closed)

    async def test_redirect_and_html_masquerading_as_pdf_are_not_followed_or_saved(self):
        for response, error in ((Response(b'', 'application/pdf', 302, {'Location': 'http://169.254.169.254/'}),
                                'invoice_source_redirect_refused'),
                                (Response(b'<html>private error</html>', 'text/html'), 'invoice_source_content_type_invalid'),
                                (Response(b'<html>private error</html>', 'application/pdf'), 'invoice_source_pdf_invalid')):
            with self.subTest(error=error):
                _, session = self.client(response)
                with self.assertRaisesRegex(source.SourceError, error):
                    await source.read_pdf('biketrek', 'INV-1', FINGERPRINT)
                self.assertEqual(len(session.calls), 1)
                self.assertTrue(response.closed)

    async def test_oversized_response_is_bounded_and_closed(self):
        response = Response(PDF, 'application/pdf', headers={'Content-Length': '25000001'})
        _, session = self.client(response)
        with self.assertRaisesRegex(source.SourceError, 'invoice_source_response_limit'):
            await source.read_pdf('biketrek', 'INV-1', FINGERPRINT)
        self.assertTrue(response.closed and session.closed)

    async def test_budget_and_auth_failures_are_sanitized_without_retry(self):
        for status, code in ((429, 'invoice_source_rate_limited'), (401, 'invoice_source_authentication_failed'),
                             (403, 'invoice_source_authentication_failed')):
            with self.subTest(status=status):
                response = Response(b'customer details must not leak', 'text/plain', status, {'Retry-After': '37'})
                _, session = self.client(response)
                with self.assertRaises(source.SourceError) as caught:
                    await source.read_pdf('biketrek', 'INV-1', FINGERPRINT)
                self.assertEqual(str(caught.exception), code)
                self.assertEqual(caught.exception.retry_after, 37 if status == 429 else None)
                self.assertEqual(caught.exception.diagnostics(), {'upstream_status': status, 'operation': 'pdf'})
                self.assertEqual(len(session.calls), 1)

    async def test_http_failure_keeps_numeric_status_and_fixed_operation_without_private_response(self):
        for status in (400, 422, 500):
            for operation in ('list', 'pdf'):
                with self.subTest(status=status, operation=operation):
                    response = Response(b'private customer body and credentials', 'text/plain', status)
                    _, session = self.client(response)
                    with self.assertRaises(source.SourceError) as caught:
                        if operation == 'list':
                            await source.read_page('biketrek', date(2026, 10, 1), date(2026, 10, 23), 1)
                        else:
                            await source.read_pdf('biketrek', 'PRIVATE-INVOICE-001', FINGERPRINT)
                    self.assertEqual(str(caught.exception), 'invoice_source_http_failed')
                    self.assertEqual(caught.exception.status, 502)
                    self.assertEqual(caught.exception.diagnostics(), {'upstream_status': status, 'operation': operation})
                    self.assertEqual(len(session.calls), 1)
                    self.assertTrue(response.closed and session.closed)

    def test_diagnostics_reject_unexpected_status_or_operation_values(self):
        for status, operation in (('400 private', 'list'), (True, 'pdf'), (700, 'pdf'), (400, 'https://private.invalid')):
            with self.subTest(status=status, operation=operation):
                error = source.SourceError('invoice_source_http_failed', upstream_status=status, operation=operation)
                values = error.diagnostics()
                self.assertNotIn('private', json.dumps(values))
                self.assertTrue('upstream_status' not in values or type(values['upstream_status']) is int)
                self.assertTrue('operation' not in values or values['operation'] in ('list', 'pdf'))

    async def test_rejected_parameters_use_only_exact_error_properties_from_sent_list_query(self):
        messages = [
            {'object': 'private object', 'property': name, 'message': 'private body and credentials', 'level': level}
            for name, level in (('page', 'error'), ('creation_time_to', 'fatal_error'),
                                ('creation_time_from', 'error'), ('page', 'error'),
                                ('creation_time_from=private', 'error'), ('private', 'error'))
        ]
        for status in (400, 422):
            with self.subTest(status=status):
                response = Response({'messages': messages}, status=status)
                _, session = self.client(response)
                with self.assertRaises(source.SourceError) as caught:
                    await source.read_page('biketrek', date(2026, 10, 1), date(2026, 10, 23), 1)
                self.assertEqual(caught.exception.diagnostics(), {'upstream_status': status, 'operation': 'list',
                    'rejected_parameters': ['creation_time_from', 'creation_time_to', 'page']})
                self.assertEqual(str(caught.exception), 'invoice_source_http_failed')
                self.assertNotIn('private', json.dumps(caught.exception.diagnostics()))
                self.assertEqual(len(session.calls), 1)
                self.assertTrue(response.closed and session.closed)

    async def test_ambiguous_property_shapes_or_message_text_do_not_name_rejected_parameters(self):
        response = Response({'messages': [
            {'property': 'private', 'message': 'creation_time_from is invalid', 'level': 'error'},
            {'property': 'query.page', 'level': 'error'}, {'property': ' page ', 'level': 'error'},
            {'property': ['creation_time_to'], 'level': 'error'}, {'property': 'page', 'level': 'warning'},
            {'property': 'creation_time_from', 'level': 'info'}, {'property': 'creation_time_to'},
        ]}, status=400)
        self.client(response)
        with self.assertRaises(source.SourceError) as caught:
            await source.read_page('biketrek', date(2026, 10, 1), date(2026, 10, 23), 1)
        self.assertEqual(caught.exception.diagnostics(), {'upstream_status': 400, 'operation': 'list'})
        # An exact name for a parameter not sent by this call is not evidence.
        response = Response({'messages': [{'property': 'page', 'level': 'error'}]}, status=400)
        self.assertEqual(source._rejected_parameters(response, {'creation_time_from': 'unused'}), [])

    async def test_invalid_error_json_keeps_original_http_failure_without_diagnostics(self):
        bodies = (b'not JSON private text', b'{"messages":{"property":"page","level":"error"}}',
            b'{"messages":[{"property":"private","property":"page","level":"error"}]}',
            b'{"messages":[{"property":"page","level":"error"}],"private":NaN}',
            b'{"messages":[{"property":"page","level":"error"}]} trailing',
            b'{"nested":' + b'[' * 2000 + b'0' + b']' * 2000 + b'}')
        for body in bodies:
            with self.subTest(body_size=len(body)):
                response = Response(body, status=400)
                _, session = self.client(response)
                with self.assertRaises(source.SourceError) as caught:
                    await source.read_page('biketrek', date(2026, 10, 1), date(2026, 10, 23), 1)
                self.assertEqual(caught.exception.diagnostics(), {'upstream_status': 400, 'operation': 'list'})
                self.assertEqual(str(caught.exception), 'invoice_source_http_failed')
                self.assertTrue(response.closed and session.closed)

    async def test_error_body_byte_time_and_read_failures_preserve_original_http_error(self):
        valid = b'{"messages":[{"property":"page","level":"error"}]}'
        response = Response(valid, status=400, headers={'Content-Length': str(source.MAX_ERROR_BYTES + 1)})
        with patch.object(response, 'iter_content', side_effect=AssertionError('Oversized body must not be read')):
            self.assertEqual(source._rejected_parameters(response, {'page': 1}), [])
        emitted = []
        response = Response(valid + b' ' * source.MAX_ERROR_BYTES, status=400)
        original_stream = response.iter_content

        def bounded_stream(size):
            for chunk in original_stream(size):
                emitted.append(len(chunk))
                yield chunk

        with patch.object(response, 'iter_content', side_effect=bounded_stream):
            self.assertEqual(source._rejected_parameters(response, {'page': 1}), [])
        self.assertLessEqual(sum(emitted), source.MAX_ERROR_BYTES)
        response = Response(valid, status=400)
        with patch.object(source, 'monotonic', side_effect=[0, source.MAX_ERROR_READ_SECONDS + 1]):
            self.assertEqual(source._rejected_parameters(response, {'page': 1}), [])
        response = Response(valid, status=400)
        self.client(response)
        with patch.object(response, 'iter_content', side_effect=requests.ConnectionError('private stream error')):
            with self.assertRaises(source.SourceError) as caught:
                await source.read_page('biketrek', date(2026, 10, 1), date(2026, 10, 23), 1)
        self.assertEqual(str(caught.exception), 'invoice_source_http_failed')
        self.assertEqual(caught.exception.diagnostics(), {'upstream_status': 400, 'operation': 'list'})
        self.assertTrue(response.closed)

    async def test_other_status_pdf_and_non_json_failures_do_not_read_error_bodies(self):
        body = {'messages': [{'property': 'page', 'level': 'error'}]}
        for status, pdf, content_type in ((403, False, 'application/json'), (500, False, 'application/json'),
                                         (400, True, 'application/json'), (400, False, 'text/html')):
            with self.subTest(status=status, pdf=pdf, content_type=content_type):
                response = Response(body, content_type=content_type, status=status)
                self.client(response)
                with patch.object(response, 'iter_content', side_effect=AssertionError('Body must not be inspected')):
                    with self.assertRaises(source.SourceError) as caught:
                        if pdf:
                            await source.read_pdf('biketrek', 'PRIVATE-INVOICE', FINGERPRINT)
                        else:
                            await source.read_page('biketrek', date(2026, 10, 1), date(2026, 10, 23), 1)
                self.assertNotIn('rejected_parameters', caught.exception.diagnostics())

    def test_rejected_parameter_constructor_is_an_allowlist_with_http_and_operation_context(self):
        values = ['private', 'page', {'property': 'creation_time_to'}, 'creation_time_from', 'page']
        error = source.SourceError('invoice_source_http_failed', upstream_status=400, operation='list',
                                   rejected_parameters=values)
        self.assertEqual(error.rejected_parameters, ['creation_time_from', 'page'])
        for status, operation in ((400, 'pdf'), (403, 'list'), (None, 'list')):
            with self.subTest(status=status, operation=operation):
                error = source.SourceError('invoice_source_http_failed', upstream_status=status,
                                           operation=operation, rejected_parameters=['page'])
                self.assertEqual(error.rejected_parameters, [])

    async def test_local_budget_denial_has_operation_without_inventing_upstream_http_status(self):
        _, session = self.client(source.AdmissionDenied(19))
        with self.assertRaises(source.SourceError) as caught:
            await source.read_page('biketrek', date(2026, 10, 1), date(2026, 10, 23), 1)
        self.assertEqual(caught.exception.diagnostics(), {'operation': 'list'})
        self.assertEqual(caught.exception.retry_after, 19)
        self.assertEqual(len(session.calls), 1)
        self.assertTrue(session.closed)

    async def test_connection_change_or_traversal_is_rejected_before_get(self):
        _, session = self.client(base='https://other.invalid/api/v2')
        with self.assertRaisesRegex(source.SourceError, 'invoice_source_connection_changed'):
            await source.read_pdf('biketrek', 'INV-1', FINGERPRINT)
        self.assertEqual(session.calls, [])
        for number in ('..', '../orders', 'INV/../orders', 'INV\\1', 'INV\n1'):
            with self.assertRaisesRegex(source.SourceError, 'invoice_source_identity_invalid'):
                await source.read_pdf('biketrek', number, FINGERPRINT)
        self.assertEqual(session.calls, [])

    async def test_duplicate_identity_or_invalid_pagination_never_becomes_complete(self):
        for payload, code in ((invoice_page(('A', 'A')), 'invoice_source_duplicate_identity'),
                              (invoice_page(current_page=2), 'invoice_source_pagination_invalid'),
                              (invoice_page(current_page_items=0), 'invoice_source_pagination_invalid')):
            with self.subTest(code=code):
                self.client(Response(payload))
                with self.assertRaisesRegex(source.SourceError, code):
                    await source.read_page('biketrek', date(2026, 1, 1), date(2026, 1, 31), 1)

    async def test_json_money_is_not_converted_through_binary_float(self):
        payload = b'{"current_page":1,"current_page_items":1,"number_of_items":1,"number_of_pages":1,' \
            b'"invoices":[{"invoice_number":"A","total_with_vat":123456.7891}]}'
        self.client(Response(payload))
        result = await source.read_page('biketrek', date(2026, 1, 1), date(2026, 1, 31), 1)
        self.assertEqual(result['invoices'][0]['total_with_vat'], '123456.7891')

    async def test_network_error_contains_no_credentials_or_customer_body(self):
        _, session = self.client(requests.ConnectionError('https://fixture.invalid secret customer'))
        with self.assertRaisesRegex(source.SourceError, '^invoice_source_unavailable$') as caught:
            await source.read_pdf('biketrek', 'INV-1', FINGERPRINT)
        self.assertEqual(caught.exception.diagnostics(), {'operation': 'pdf'})
        self.assertEqual(len(session.calls), 1)
        self.assertTrue(session.closed)
