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
            'creation_time_to': '2026-10-31T23:59:59.999999+01:00'})
        self.assertFalse(options['allow_redirects'])
        self.assertTrue(options['verify'])
        self.assertTrue(response.closed and session.closed)
        self.assertIsNone(client.log_dir)

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
                self.assertEqual(len(session.calls), 1)

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
        with self.assertRaisesRegex(source.SourceError, '^invoice_source_unavailable$'):
            await source.read_pdf('biketrek', 'INV-1', FINGERPRINT)
        self.assertEqual(len(session.calls), 1)
        self.assertTrue(session.closed)
