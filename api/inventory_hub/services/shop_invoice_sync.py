"""Manually mirror bounded pages of existing shop invoices into the archive.

The source PDF and invoice facts remain the shop's originals. This service
does not issue invoices, register payments, send email, or mutate stock/orders.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re

from sqlalchemy import select, func, or_, text

from inventory_hub.db_models import Shop
from inventory_hub.db_models_ext import ShopProduct
from inventory_hub.services.product_identity import IDENTITY_WRITE_LOCK
from inventory_hub.services import shop_invoice_source as source

MAX_DOCUMENTS = 10
DOCUMENT_TYPES = {'invoice': 'invoice', 'creditNote': 'credit_note', 'receipt': 'receipt'}
PAYMENT_FIELDS = {'paid_yn', 'paid_date', 'total_rest', 'last_update_time', 'invoice_pdf_url'}


def _amount(row, key):
    raw = row.get(key)
    if raw is None or raw == '':
        return None
    if type(raw) not in (str, int, Decimal):
        raise source.SourceError('invoice_source_amount_invalid')
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError):
        raise source.SourceError('invoice_source_amount_invalid') from None
    if not value.is_finite() or abs(value) > Decimal('999999999999999') or abs(value.as_tuple().exponent) > 12:
        raise source.SourceError('invoice_source_amount_invalid')
    return format(value, 'f')


def _date(row, key):
    value = row.get(key)
    if value is None or value == '':
        return None
    try:
        if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            raise ValueError
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise source.SourceError('invoice_source_date_invalid') from None


def normalize(document):
    """Map documented provider fields, never manufacture product relationships."""
    number = source.invoice_number(document.get('invoice_number'))
    kind = DOCUMENT_TYPES.get(document.get('type'))
    currency = document.get('currency_id')
    if kind is None:
        raise source.SourceError('invoice_source_type_unsupported')
    if not isinstance(currency, str) or re.fullmatch('[A-Z]{3}', currency) is None:
        raise source.SourceError('invoice_source_currency_invalid')
    rows = document.get('items')
    if not isinstance(rows, list) or len(rows) > 10_000 or any(not isinstance(row, dict) for row in rows):
        raise source.SourceError('invoice_source_lines_invalid')
    gross, net = _amount(document, 'total_with_vat'), _amount(document, 'total_without_vat')
    paid = document.get('paid_yn')
    if paid is not None and type(paid) is not bool:
        raise source.SourceError('invoice_source_payment_invalid')
    remaining = _amount(document, 'total_rest')
    # The source's remaining balance is evidence; it is not a local cash event.
    # A false paid flag may mean partially paid, so it cannot establish zero.
    settled = (format(Decimal(gross) - Decimal(remaining), 'f')
               if gross is not None and remaining is not None else gross if paid is True else None)
    paid_date = _date(document, 'paid_date')
    metadata = {'invoice_number': number, 'invoice_date': _date(document, 'date_of_issuance'),
        'due_date': _date(document, 'date_of_expiration'), 'currency': currency, 'total_amount': gross,
        'total_with_vat': gross, 'total_without_vat': net,
        'vat_amount': format(Decimal(gross) - Decimal(net), 'f') if gross is not None and net is not None else None,
        'document_type': kind, 'source_payment_status': 'paid' if paid else 'not_paid' if paid is False else None,
        'source_paid_amount': settled, 'source_paid_at': None, 'source_paid_date': paid_date}
    lines = []
    for position, row in enumerate(rows, 1):
        code, name, unit = row.get('code'), row.get('name'), row.get('unit')
        if any(value is not None and not isinstance(value, str) for value in (code, name, unit)):
            raise source.SourceError('invoice_source_lines_invalid')
        lines.append({'line_number': position, 'source_line_key': str(position),
            'supplier_sku': code or None, 'product_name': name or None, 'unit': unit or None,
            'quantity': _amount(row, 'quantity'), 'unit_price': _amount(row, 'price_per_unit_without_vat'),
            'total_price': _amount(row, 'price_without_vat'), 'vat_rate': _amount(row, 'vat'),
            'unit_price_with_vat': _amount(row, 'price_per_unit_with_vat'),
            'total_price_with_vat': _amount(row, 'price_with_vat'),
            'ean': None, 'barcodes': [], 'matched_product_id': None})
    financial = {key: value for key, value in document.items() if key not in PAYMENT_FIELDS}
    fingerprint = hashlib.sha256(json.dumps(financial, sort_keys=True, separators=(',', ':'),
        ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    return metadata, lines, fingerprint


async def bind_lines(db, shop_id, lines, source_rows):
    """Record a current exact, native-verified mapping; never infer from Hub SKU.

    The evidence is an import-time observation, not a claim that a reused code
    had the same owner at every historical date. Archive replay retains it.
    """
    product_positions = {position for position, row in enumerate(source_rows, 1) if row.get('type') == 'product'}
    codes = {line['supplier_sku'] for line in lines
             if line['line_number'] in product_positions and line.get('supplier_sku')}
    if not codes:
        return lines
    await db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': IDENTITY_WRITE_LOCK})
    forms = {form for code in codes for form in (code.lower(), code.casefold())}
    mappings = await db.scalars(select(ShopProduct).where(ShopProduct.shop_id == shop_id, or_(
        func.lower(ShopProduct.external_code).in_(forms), ShopProduct.external_code.op('~')(r'[^ -~]'))))
    owners = defaultdict(list)
    for mapping in mappings:
        if mapping.external_code:
            owners[mapping.external_code.casefold()].append(mapping)
    observed = datetime.now(timezone.utc).isoformat()
    result = []
    for original in lines:
        line = dict(original)
        candidates = (owners.get((line.get('supplier_sku') or '').casefold(), [])
                      if line['line_number'] in product_positions else [])
        if len(candidates) == 1:
            mapping = candidates[0]
            native_ids = [mapping.external_product_id]
            if mapping.is_variant:
                native_ids.append(mapping.external_variant_id)
            if (mapping.is_listed and mapping.external_code == line['supplier_sku']
                    and all(isinstance(value, str) and re.fullmatch(r'[1-9][0-9]{0,19}', value) for value in native_ids)):
                line['matched_product_id'] = mapping.product_id
                line['match_evidence'] = {'source': 'shop_mapping', 'shop_product_id': mapping.id,
                    'shop_id': shop_id, 'product_id': mapping.product_id, 'external_code': mapping.external_code,
                    'external_product_id': mapping.external_product_id, 'external_variant_id': mapping.external_variant_id,
                    'is_variant': mapping.is_variant, 'matched_at': observed}
        result.append(line)
    return result


async def refresh(db, shop_code, request, *, actor='operator'):
    from inventory_hub.services.invoice_archive import ingest_invoice, InvoiceArchiveError

    shop = await db.scalar(select(Shop).where(Shop.code == shop_code, Shop.is_active.is_(True)))
    if shop is None:
        raise source.SourceError('invoice_sync_shop_not_found', 404)
    if shop.platform != 'upgates':
        raise source.SourceError('invoice_sync_platform_unsupported', 422)
    shop_id = shop.id
    await db.commit()  # No archive transaction or row lock spans external I/O.
    page = await source.read_page(shop_code, request.date_from, request.date_to, request.page)
    if request.page_token is not None and request.page_token != page['page_token']:
        raise source.SourceError('invoice_source_page_changed', 409)
    rows = page['invoices']
    if request.offset > len(rows) or request.offset and request.offset == len(rows):
        raise source.SourceError('invoice_sync_offset_invalid', 422)
    result = {'imported': 0, 'unchanged': 0, 'failed': 0, 'items': [], 'next': None,
              'total_in_source': page['total'], 'source_pages': page['pages'], 'remote_requests': 1,
              'stopped_error': None, 'retry_after': None}
    end = min(len(rows), request.offset + MAX_DOCUMENTS)
    offset = request.offset
    for offset in range(request.offset, end):
        document = rows[offset]
        number = document['invoice_number']
        try:
            metadata, lines, financial_hash = normalize(document)
        except source.SourceError as error:
            result['failed'] += 1
            result['items'].append({'invoice_number': number, 'status': 'failed', 'error': error.code})
            continue
        try:
            result['remote_requests'] += 1
            pdf = await source.read_pdf(shop_code, number, page['fingerprint'])
        except source.SourceError as error:
            result['stopped_error'], result['retry_after'] = error.code, error.retry_after
            result['next'] = {'page': request.page, 'offset': offset, 'page_token': page['page_token']}
            return result
        try:
            lines = await bind_lines(db, shop_id, lines, document['items'])
            saved = await ingest_invoice(db, direction='sale', source_kind='shop', source_key=number,
                shop_id=shop_id, metadata=metadata, lines=lines,
                files=[{'filename': f'invoice-{hashlib.sha256(number.encode()).hexdigest()[:20]}.pdf',
                        'content': pdf, 'role': 'original', 'file_type': 'pdf'}],
                source_data=document, source_fingerprint=financial_hash, actor=actor)
            await db.commit()
        except InvoiceArchiveError as error:
            await db.rollback()
            result['failed'] += 1
            result['items'].append({'invoice_number': number, 'status': 'failed', 'error': error.code})
            continue
        status = 'unchanged' if saved['replayed'] else 'imported'
        result[status] += 1
        result['items'].append({'invoice_number': number, 'status': status, 'id': saved['id']})
    if end < len(rows):
        result['next'] = {'page': request.page, 'offset': end, 'page_token': page['page_token']}
    elif request.page < page['pages']:
        result['next'] = {'page': request.page + 1, 'offset': 0, 'page_token': None}
    return result
