"""Read original issued Upgates invoices through the shared API budget.

Only fixed authenticated GET endpoints are used. The provider's arbitrary
invoice_pdf_url is retained as source metadata, never followed. Customer and
invoice bodies are not logged. No API request is retried or redirected.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, time
from decimal import Decimal
import hashlib
import json
from time import monotonic
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests

from inventory_hub.services.upgates import UpgatesClient, UpgatesError, connection_fingerprint, _retry_after_seconds
from inventory_hub.services.upgates_budget import AdmissionDenied

MAX_JSON_BYTES = 8_000_000
MAX_PDF_BYTES = 25_000_000
MAX_READ_SECONDS = 30
CREATION_TIME_ZONE = ZoneInfo('Europe/Bratislava')


class SourceError(Exception):
    def __init__(self, code, status=502, retry_after=None):
        super().__init__(code)
        self.code, self.status, self.retry_after = code, status, retry_after


def _need(condition, code, status=502):
    if not condition:
        raise SourceError(code, status)


def invoice_number(value):
    _need(isinstance(value, str) and value == value.strip() and 0 < len(value) <= 100
          and not any(ord(char) < 32 or ord(char) == 127 for char in value)
          and '\\' not in value and not any(part in ('.', '..') for part in value.split('/')),
          'invoice_source_identity_invalid')
    return value


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        _need(key not in value, 'invoice_source_json_invalid')
        value[key] = item
    return value


def _nonfinite(_):
    raise SourceError('invoice_source_amount_invalid')


def _json_values(value):
    if isinstance(value, Decimal):
        _need(value.is_finite(), 'invoice_source_amount_invalid')
        return format(value, 'f')
    if isinstance(value, dict):
        return {key: _json_values(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_values(item) for item in value]
    return value


def _client(shop_code, expected_fingerprint=None):
    try:
        client = UpgatesClient.from_shop(shop_code)
    except (UpgatesError, ValueError, OSError):
        raise SourceError('invoice_source_configuration_invalid', 409) from None
    client.log_dir = None
    fingerprint = connection_fingerprint(client.base_url, client.session.auth[0])
    if not fingerprint or expected_fingerprint is not None and fingerprint != expected_fingerprint:
        client.session.close()
        raise SourceError('invoice_source_connection_changed', 409)
    return client, fingerprint


def _get(client, path, *, params=None, pdf=False):
    response = None
    limit = MAX_PDF_BYTES if pdf else MAX_JSON_BYTES
    try:
        response = client.session.get(f'{client.base_url}/{path}', params=params or {},
            headers={'Accept': 'application/pdf' if pdf else 'application/json'},
            timeout=(5, 20), verify=True, allow_redirects=False, stream=True)
        status = response.status_code
        if status != 200:
            code = ('invoice_source_rate_limited' if status == 429 else
                    'invoice_source_authentication_failed' if status in (401, 403) else
                    'invoice_source_redirect_refused' if 300 <= status < 400 else
                    'invoice_source_document_missing' if status == 404 else 'invoice_source_http_failed')
            raise SourceError(code, 429 if status == 429 else 502,
                              _retry_after_seconds(response.headers.get('Retry-After')) if status == 429 else None)
        content_type = response.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
        _need(content_type == ('application/pdf' if pdf else 'application/json'), 'invoice_source_content_type_invalid')
        length = response.headers.get('Content-Length')
        if length is not None:
            _need(isinstance(length, str) and length.isascii() and length.isdigit()
                  and int(length) <= limit, 'invoice_source_response_limit')
        data, started = bytearray(), monotonic()
        for chunk in response.iter_content(65536):
            data.extend(chunk)
            _need(len(data) <= limit and monotonic() - started <= MAX_READ_SECONDS, 'invoice_source_response_limit')
        if pdf:
            _need(data.startswith(b'%PDF-') and b'%%EOF' in data[-1024:], 'invoice_source_pdf_invalid')
            return bytes(data)
        try:
            document = json.loads(data, object_pairs_hook=_unique_object, parse_float=Decimal, parse_constant=_nonfinite)
        except (ValueError, UnicodeError):
            raise SourceError('invoice_source_json_invalid') from None
        _need(isinstance(document, dict) and not document.get('messages'), 'invoice_source_response_invalid')
        return _json_values(document)
    except AdmissionDenied as error:
        raise SourceError('invoice_source_rate_limited', 429, error.retry_after) from None
    except requests.RequestException:
        raise SourceError('invoice_source_unavailable') from None
    finally:
        if response is not None:
            response.close()


def _page(shop_code, date_from, date_to, page):
    client, fingerprint = _client(shop_code)
    try:
        payload = _get(client, 'invoices', params={
            'creation_time_from': datetime.combine(date_from, time.min, CREATION_TIME_ZONE).isoformat(),
            'creation_time_to': datetime.combine(date_to, time.max, CREATION_TIME_ZONE).isoformat(),
            'page': page})
        rows = payload.get('invoices')
        _need(isinstance(rows, list) and len(rows) <= 100 and all(isinstance(row, dict) for row in rows),
              'invoice_source_rows_invalid')
        current, count, total, pages = (payload.get(key) for key in
            ('current_page', 'current_page_items', 'number_of_items', 'number_of_pages'))
        _need(all(type(value) is int for value in (current, count, total, pages))
              and current == page and count == len(rows) and 0 <= total <= 10_000_000 and 0 <= pages <= 100_000
              and ((not rows and total == 0 and page == 1 and pages in (0, 1))
                   or (bool(rows) and 1 <= page <= pages and total >= len(rows))), 'invoice_source_pagination_invalid')
        numbers = [invoice_number(row.get('invoice_number')) for row in rows]
        _need(len(numbers) == len(set(numbers)), 'invoice_source_duplicate_identity')
        page_token = hashlib.sha256(json.dumps([fingerprint, str(date_from), str(date_to), page, numbers],
            separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
        return {'invoices': rows, 'fingerprint': fingerprint, 'page_token': page_token,
                'page': page, 'pages': pages, 'total': total}
    finally:
        client.session.close()


def _pdf(shop_code, number, fingerprint):
    number = invoice_number(number)
    client, _ = _client(shop_code, fingerprint)
    try:
        return _get(client, f'invoices/{quote(number, safe="")}/pdf', pdf=True)
    finally:
        client.session.close()


async def read_page(shop_code, date_from, date_to, page):
    return await asyncio.to_thread(_page, shop_code, date_from, date_to, page)


async def read_pdf(shop_code, number, fingerprint):
    return await asyncio.to_thread(_pdf, shop_code, number, fingerprint)
