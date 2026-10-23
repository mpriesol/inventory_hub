"""Idempotent archive ingestion of external invoices, without stock mutations."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tempfile
from datetime import date, datetime, timezone
from decimal import Decimal

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select, text

from inventory_hub import config_io
from inventory_hub.db_models import Product, Shop, Supplier, SupplierProduct
from inventory_hub.file_access import code_component, confined_path, stored_document_path
from inventory_hub.invoice_archive_types import ArchiveLine, ArchiveMetadata, InvoiceReconcileInput
from inventory_hub.settings import settings


class InvoiceArchiveError(Exception):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


MAX_FILE = 50 * 1024 * 1024
MAX_LINES = 10000


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str)


def _digest(value):
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


def source_hash(metadata, lines):
    """Source payment observations and resolved local links are not invoice facts."""
    header = metadata.model_dump(mode='json', exclude={'source_payment_status', 'source_paid_amount', 'source_paid_at', 'source_paid_date'})
    rows = [row.model_dump(mode='json', exclude={'matched_product_id', 'matched_supplier_product_id', 'match_evidence'}) for row in lines]
    return _digest([header, rows])


def _bytes(path):
    if path.stat().st_size > MAX_FILE:
        raise InvoiceArchiveError('invoice_file_too_large', 422)
    with path.open('rb') as file:
        content = file.read(MAX_FILE + 1)
    if len(content) > MAX_FILE:
        raise InvoiceArchiveError('invoice_file_too_large', 422)
    return content


def _file(root, owner_area, owner_code, value):
    """Keep a content-addressed original even if the external cache later changes."""
    role = value.get('role', 'original')
    if role not in ('original', 'parsed'):
        raise InvoiceArchiveError('invoice_file_invalid', 422)
    original_path = None
    if 'path' in value:
        original_path = stored_document_path(root, value['path'])
        content = _bytes(original_path)
        name = value.get('filename') or original_path.name
    else:
        content, name = value.get('content'), value.get('filename', '')
    if not isinstance(content, bytes) or not content or len(content) > MAX_FILE:
        raise InvoiceArchiveError('invoice_file_invalid', 422)
    if not isinstance(name, str) or not name or len(name) > 500:
        raise InvoiceArchiveError('invoice_file_invalid', 422)
    name = name.replace('\\', '/').rsplit('/', 1)[-1]
    suffix = Path(name).suffix.lower()
    if suffix not in {'.pdf', '.csv', '.tsv', '.xml', '.json', '.xlsx', '.xls', '.doc', '.docx', '.txt', '.jpg', '.jpeg', '.png'}:
        raise InvoiceArchiveError('invoice_file_type_unsupported', 422)
    if suffix == '.pdf' and not content.startswith(b'%PDF-'):
        raise InvoiceArchiveError('invoice_file_invalid', 422)
    digest = hashlib.sha256(content).hexdigest()
    area = 'invoices/raw/archive' if owner_area == 'suppliers' else 'exports/invoices'
    target = confined_path(root, f'{owner_area}/{code_component(owner_code)}/{area}/{digest}/document{suffix}')
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if hashlib.sha256(_bytes(target)).hexdigest() != digest:
            raise InvoiceArchiveError('invoice_archive_file_conflict')
    else:
        # Atomic file-first persistence: an interrupted DB transaction leaves a
        # reusable blob; it never leaves a committed row pointing at half a file.
        fd, temporary = tempfile.mkstemp(prefix='invoice-', dir=target.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return {'role': role, 'filename': name, 'file_path': str(target), 'file_type': suffix.lstrip('.'),
            'sha256': digest, 'size_bytes': len(content), 'original_path': str(original_path) if original_path else None}


def barcode_values(line):
    from inventory_hub.services.identifiers import ProductIdentifierService
    from inventory_hub.services.product_identity import verified_barcodes
    values = verified_barcodes((line.ean or '', *line.barcodes))
    return sorted(set((*values, *(ProductIdentifierService.gtin_key(value) for value in values))))


async def _resolved_lines(db, lines, supplier_id):
    """Only existing explicit identities can link a document line to a card."""
    from inventory_hub.services.product_identity import RemoteIdentity, load_identity_index, verified_barcodes
    identities = [RemoteIdentity(0, '', supplier_id=supplier_id, supplier_sku=line.supplier_sku or '',
        barcodes=tuple(verified_barcodes((line.ean or '', *line.barcodes))), expected_product_id=line.matched_product_id)
        for line in lines]
    index = await load_identity_index(db, 0, identities, local=True) if identities else None
    result = []
    for line, identity in zip(lines, identities):
        values = line.model_dump()
        if line.matched_product_id is not None and await db.get(Product, line.matched_product_id) is None:
            raise InvoiceArchiveError('invoice_product_missing', 422)
        if line.matched_supplier_product_id is not None:
            source = await db.get(SupplierProduct, line.matched_supplier_product_id)
            if source is None or supplier_id is None or source.supplier_id != supplier_id:
                raise InvoiceArchiveError('invoice_supplier_product_invalid', 422)
        resolved = index.resolve(identity)
        if resolved.status in ('identified', 'mapped'):
            values['matched_product_id'] = resolved.product_id
        elif resolved.status == 'conflict':
            # Source evidence remains searchable even when identity needs review.
            values['matched_product_id'] = None
        values['barcode_values'] = barcode_values(line)
        result.append(values)
    return result


async def _legacy_candidate(db, supplier_id, header, files, source_kind):
    if supplier_id is None:
        return None
    paths = [path for item in files for path in (item['original_path'], item['file_path']) if path]
    hashes = [item['sha256'] for item in files]
    rows = (await db.execute(text('''SELECT * FROM uploaded_invoices
        WHERE supplier_id=:supplier AND (source_key IS NULL OR source_kind='upload' OR :kind='upload')
          AND (invoice_number=:number OR file_path=ANY(CAST(:paths AS text[]))
            OR id IN (SELECT invoice_id FROM invoice_files WHERE sha256=ANY(CAST(:hashes AS text[])))) FOR UPDATE'''),
        {'supplier': supplier_id, 'number': header.invoice_number, 'paths': paths,
         'hashes': hashes, 'kind': source_kind})).mappings().all()
    if not rows:
        return None
    incoming = {item['sha256'] for item in files}
    proven = []
    for row in rows:
        try:
            path = stored_document_path(settings.INVENTORY_DATA_ROOT, row['file_path'])
            if hashlib.sha256(_bytes(path)).hexdigest() in incoming:
                proven.append(row)
        except (HTTPException, OSError, InvoiceArchiveError):
            continue
    if len(rows) != 1 or len(proven) != 1:
        raise InvoiceArchiveError('invoice_archive_existing_ambiguous')
    row = proven[0]
    for key in ('currency', 'total_amount', 'total_without_vat', 'vat_amount', 'total_with_vat'):
        incoming_value = getattr(header, key)
        if row[key] is not None and incoming_value is not None and row[key] != incoming_value:
            raise InvoiceArchiveError('invoice_source_changed')
    return row


async def ingest_invoice(db, *, direction, source_kind, source_key, metadata, lines=(), files=(),
                         supplier_id=None, shop_id=None, source_data=None, source_fingerprint=None, actor='operator'):
    """Caller commits. Replays never replace commercial facts or manual payments."""
    try:
        header = metadata if isinstance(metadata, ArchiveMetadata) else ArchiveMetadata.model_validate(metadata)
        parsed = [line if isinstance(line, ArchiveLine) else ArchiveLine.model_validate(line) for line in lines]
    except (ValidationError, ValueError):
        raise InvoiceArchiveError('invoice_source_invalid', 422) from None
    if (len(parsed) > MAX_LINES or len({line.line_number for line in parsed}) != len(parsed)
            or direction not in ('purchase', 'sale') or source_kind not in ('upload', 'supplier', 'shop')
            or not isinstance(source_key, str) or not source_key.strip() or len(source_key) > 500
            or any(ord(char) < 32 for char in source_key)):
        raise InvoiceArchiveError('invoice_source_invalid', 422)
    if direction == 'purchase' and supplier_id and shop_id is None:
        owner = await db.get(Supplier, supplier_id)
        area, column, owner_id = 'suppliers', 'supplier_id', supplier_id
    elif direction == 'sale' and shop_id and supplier_id is None:
        owner = await db.get(Shop, shop_id)
        area, column, owner_id = 'shops', 'shop_id', shop_id
    else:
        raise InvoiceArchiveError('invoice_owner_invalid', 422)
    if owner is None:
        raise InvoiceArchiveError('invoice_owner_missing', 404)
    fingerprint = source_fingerprint or source_hash(header, parsed)
    if not isinstance(fingerprint, str) or not re.fullmatch(r'[0-9a-f]{64}', fingerprint):
        raise InvoiceArchiveError('invoice_source_invalid', 422)
    await db.execute(text('SELECT pg_advisory_xact_lock(:key)'),
        {'key': int(_digest(['invoice-archive', direction, owner_id])[:15], 16)})
    existing = (await db.execute(text(f'''SELECT * FROM uploaded_invoices WHERE direction=:direction
        AND source_kind=:kind AND {column}=:owner AND source_key=:key FOR UPDATE'''),
        {'direction': direction, 'kind': source_kind, 'owner': owner_id, 'key': source_key})).mappings().first()
    if existing and existing['source_hash'] != fingerprint:
        raise InvoiceArchiveError('invoice_source_changed')
    stored = [_file(Path(settings.INVENTORY_DATA_ROOT), area, owner.code, value) for value in files]
    stored.sort(key=lambda row: (row['role'] != 'original', row['file_type'] != 'pdf'))
    # A shop may regenerate the PDF after a payment stamp. The commercial hash
    # must still match; keep both observed originals and retain the first primary.
    adopted = await _legacy_candidate(db, supplier_id, header, stored, source_kind) if existing is None else None
    preserve_source = bool(adopted and source_kind == 'upload' and adopted['source_kind'] != 'upload'
                           and adopted['source_key'] is not None)
    identity = existing or adopted
    if identity is None:
        primary = stored[0] if stored else {}
        values = {**header.model_dump(exclude={'parse_error'}), 'supplier': supplier_id, 'shop': shop_id,
            'direction': direction, 'kind': source_kind, 'key': source_key, 'hash': fingerprint,
            'data': _json(source_data or {}), 'filename': primary.get('filename', ''),
            'stored_name': _digest([source_kind, source_key]) + '-' + (Path(primary.get('file_path', '')).name or 'metadata'),
            'path': primary.get('file_path', ''),
            'file_type': primary.get('file_type'), 'size': primary.get('size_bytes'), 'actor': actor,
            'parsed': bool(parsed), 'parse_error': header.parse_error, 'count': len(parsed)}
        identifier = await db.scalar(text('''INSERT INTO uploaded_invoices
          (supplier_id,shop_id,direction,source_kind,source_key,source_hash,source_data,source_seen_at,
           document_type,invoice_number,invoice_date,due_date,currency,total_amount,total_without_vat,vat_amount,total_with_vat,
           source_payment_status,source_paid_amount,source_paid_at,source_paid_date,original_filename,stored_filename,file_path,
           file_type,file_size_bytes,uploaded_by,is_parsed,parse_error,items_count,vat_rate,vat_included)
          VALUES (:supplier,:shop,:direction,:kind,:key,:hash,CAST(:data AS jsonb),NOW(),
           :document_type,:invoice_number,:invoice_date,:due_date,:currency,:total_amount,:total_without_vat,:vat_amount,:total_with_vat,
           :source_payment_status,:source_paid_amount,:source_paid_at,:source_paid_date,:filename,:stored_name,:path,
           :file_type,:size,:actor,:parsed,:parse_error,:count,NULL,NULL) RETURNING id'''), values)
    else:
        identifier = identity['id']
        if adopted and not preserve_source:
            if adopted['payment_revision'] and any(adopted[key] != getattr(header, key) for key in
                    ('currency', 'total_amount', 'total_without_vat', 'vat_amount', 'total_with_vat')):
                raise InvoiceArchiveError('invoice_source_changed')
            await db.execute(text('''UPDATE uploaded_invoices SET source_kind=:kind,source_key=:key,
                source_hash=:hash,source_data=CAST(:data AS jsonb),document_type=:document_type,
                invoice_number=:invoice_number,invoice_date=COALESCE(:invoice_date,invoice_date),
                due_date=COALESCE(:due_date,due_date),currency=COALESCE(:currency,currency),
                total_amount=COALESCE(:total_amount,total_amount),total_without_vat=COALESCE(:total_without_vat,total_without_vat),
                vat_amount=COALESCE(:vat_amount,vat_amount),total_with_vat=COALESCE(:total_with_vat,total_with_vat),
                is_parsed=:parsed,parse_error=:parse_error,items_count=:count WHERE id=:id'''),
                {**header.model_dump(), 'id': identifier, 'kind': source_kind, 'key': source_key,
                 'hash': fingerprint, 'data': _json(source_data or {}), 'parsed': bool(parsed), 'count': len(parsed)})
    # Refresh observations independently of document facts and the manual ledger.
    if not preserve_source:
        await db.execute(text('''UPDATE uploaded_invoices SET source_seen_at=NOW(),
        payment_revision=payment_revision + CASE WHEN
          (source_payment_status IS DISTINCT FROM :status OR source_paid_amount IS DISTINCT FROM :amount
             OR source_paid_at IS DISTINCT FROM :paid_at OR source_paid_date IS DISTINCT FROM :paid_date)
          AND NOT EXISTS(SELECT 1 FROM invoice_payments WHERE invoice_id=uploaded_invoices.id)
          THEN 1 ELSE 0 END,
        source_payment_status=:status,source_paid_amount=:amount,source_paid_at=:paid_at,source_paid_date=:paid_date WHERE id=:id'''),
        {'id': identifier, 'status': header.source_payment_status, 'amount': header.source_paid_amount,
         'paid_at': header.source_paid_at, 'paid_date': header.source_paid_date})
    for file in stored:
        await db.execute(text('''INSERT INTO invoice_files(invoice_id,role,filename,file_path,file_type,sha256,size_bytes)
            VALUES(:invoice,:role,:filename,:file_path,:file_type,:sha256,:size_bytes)
            ON CONFLICT(invoice_id,sha256) DO NOTHING'''), {**file, 'invoice': identifier})
    if existing is None and not preserve_source:
        if adopted and await db.scalar(text('SELECT count(*) FROM uploaded_invoice_lines WHERE invoice_id=:id'), {'id': identifier}):
            raise InvoiceArchiveError('invoice_archive_existing_lines')
        for line in await _resolved_lines(db, parsed, supplier_id):
            await db.execute(text('''INSERT INTO uploaded_invoice_lines
                (invoice_id,line_number,source_line_key,ean,barcode_values,supplier_sku,product_name,quantity,unit,
                 unit_price,total_price,vat_rate,unit_price_with_vat,total_price_with_vat,matched_product_id,matched_supplier_product_id,raw_data)
                VALUES(:invoice,:line_number,:source_line_key,:ean,CAST(:barcode_values AS text[]),:supplier_sku,:product_name,:quantity,:unit,
                 :unit_price,:total_price,:vat_rate,:unit_price_with_vat,:total_price_with_vat,:matched_product_id,:matched_supplier_product_id,CAST(:raw_data AS jsonb))'''),
                {**line, 'invoice': identifier, 'raw_data': _json({key: value for key, value in line.items() if key not in
                    ('matched_product_id', 'matched_supplier_product_id', 'barcode_values')})})
    await db.flush()
    return {'id': identifier, 'replayed': existing is not None or adopted is not None, 'source_key': source_key,
        'files_count': await db.scalar(text('SELECT count(*) FROM invoice_files WHERE invoice_id=:id'), {'id': identifier}),
        'lines_count': await db.scalar(text('SELECT count(*) FROM uploaded_invoice_lines WHERE invoice_id=:id'), {'id': identifier})}


def _source_path(root, supplier, value):
    if not isinstance(value, str) or not value:
        return None
    if value.startswith('data/'):
        value = value[5:]
    if not value.startswith('suppliers/') and not Path(value).is_absolute():
        value = f'suppliers/{supplier}/{value}'
    path = stored_document_path(root, value)
    expected = (Path(root).resolve() / 'suppliers' / supplier / 'invoices')
    if not path.is_relative_to(expected):
        raise InvoiceArchiveError('invoice_file_scope_invalid', 422)
    return path


def _number_text(value):
    if value is None or value == '':
        return None
    if isinstance(value, bool):
        raise InvoiceArchiveError('invoice_source_invalid', 422)
    value = str(value).strip().replace('\u00a0', '').replace('\u202f', '').replace(' ', '')
    value = re.sub(r'(?:EUR|CZK|€|Kč)$', '', value, flags=re.I)
    if ',' in value:
        value = value.replace('.', '').replace(',', '.')
    if not re.fullmatch(r'-?[0-9]+(?:\.[0-9]+)?', value):
        raise InvoiceArchiveError('invoice_source_invalid', 422)
    return value


def _source_date(value):
    if value in (None, ''):
        return None
    if isinstance(value, date):
        return value.date() if isinstance(value, datetime) else value
    value = str(value).strip()
    for format_string in ('%Y-%m-%d', '%d.%m.%Y', '%d. %m. %Y'):
        try:
            return datetime.strptime(value, format_string).date()
        except ValueError:
            pass
    raise InvoiceArchiveError('invoice_source_date_invalid', 422)


def _csv_lines(path, mapping=None):
    """Strict normalized receiving CSV reader; missing money/tax stays unknown."""
    return _csv_content(_bytes(path), mapping)


def _csv_content(raw, mapping=None):
    from inventory_hub.routers import receiving_db as receiving
    try:
        content = raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        content = raw.decode('cp1250')
    reader = csv.reader(io.StringIO(content), delimiter=receiving._detect_delimiter(content.splitlines()[0] if content else ''))
    headers = next(reader, [])
    index = receiving._build_index(headers)
    result = []
    for row in reader:
        if not any(value.strip() for value in row):
            continue
        get = lambda names: receiving._get_val(row, index, names)
        source = lambda canonical, names: get([mapping[canonical]]) if mapping and mapping.get(canonical) else get(names)
        quantity = _number_text(source('QTY', receiving._HDR_QTY + ['Quantity']))
        price = _number_text(source('UNIT_PRICE_EX', receiving._HDR_PRICE))
        if not quantity:
            raise InvoiceArchiveError('invoice_lines_invalid', 422)
        result.append(ArchiveLine(line_number=len(result) + 1, supplier_sku=source('SCM', receiving._HDR_SCM) or None,
            ean=source('EAN', receiving._HDR_EAN) or None, product_name=source('TITLE', receiving._HDR_TITLE) or None,
            quantity=quantity, unit_price=price or None,
            total_price=_number_text(get(['TOTAL_EUR', 'TOTAL_NET'])),
            total_price_with_vat=_number_text(get(['TOTAL_EUR_VAT', 'TOTAL_GROSS']))))
        if len(result) > MAX_LINES:
            raise InvoiceArchiveError('invoice_lines_invalid', 422)
    if not result:
        raise InvoiceArchiveError('invoice_lines_invalid', 422)
    return result


def _parsed_pdf(supplier, content):
    from inventory_hub.adapters import sk_profi_bike_invoice, zookee_invoice
    parser = zookee_invoice.parse_invoice_pdf if supplier == 'zookee' else sk_profi_bike_invoice.parse_invoice_pdf
    parsed = parser(content)
    metadata = {'invoice_number': parsed.number, 'invoice_date': parsed.issue_date, 'due_date': parsed.due_date,
        'currency': 'EUR', 'total_amount': parsed.gross, 'total_without_vat': parsed.net,
        'vat_amount': parsed.vat, 'total_with_vat': parsed.gross}
    lines = [{'line_number': row.number, 'supplier_sku': row.sku, 'product_name': row.title,
        'quantity': row.qty, 'unit_price': row.price, 'ean': row.ean or None, 'vat_rate': row.vat,
        'total_price': row.net, 'total_price_with_vat': row.gross} for row in parsed.lines]
    return metadata, lines


def parse_uploaded_document(supplier, filename, content):
    """Reuse validated supplier/CSV parsers; unsupported originals stay unparsed."""
    suffix = Path(filename).suffix.lower()
    try:
        if suffix == '.pdf' and supplier in ('sk-profi-bike', 'zookee'):
            metadata, lines = _parsed_pdf(supplier, content)
        elif suffix in ('.csv', '.tsv'):
            config_path = confined_path(settings.INVENTORY_DATA_ROOT,
                f'suppliers/{code_component(supplier)}/config.json')
            cfg = json.loads(config_path.read_text()) if config_path.is_file() else {}
            mapping = ((cfg.get('adapter_settings') or {}).get('mapping') or
                (cfg.get('invoices') or {}).get('mapping') or {}).get('invoice_to_canon') or {}
            metadata, lines = {}, _csv_content(content, mapping)
        else:
            return {}, []
        # Failed parsing must still allow retention of the original document.
        ArchiveMetadata.model_validate(metadata)
        lines = [line if isinstance(line, ArchiveLine) else ArchiveLine.model_validate(line) for line in lines]
        return metadata, lines
    except (InvoiceArchiveError, HTTPException, ValidationError, ValueError, OSError):
        return {'parse_error': 'invoice_parse_failed'}, []


def _index_entries(root, supplier):
    path = confined_path(root, f'suppliers/{code_component(supplier)}/invoices/index.latest.json')
    if not path.is_file():
        return []
    try:
        if path.stat().st_size > 20 * 1024 * 1024:
            raise ValueError()
        data = json.loads(path.read_text(encoding='utf-8'))
        if isinstance(data, dict) and isinstance(data.get('invoices'), list):
            data = data['invoices']
        rows = list(data.values()) if isinstance(data, dict) else data
        if not isinstance(rows, list):
            raise ValueError()
    except (ValueError, OSError):
        raise InvoiceArchiveError('invoice_index_invalid', 422) from None
    entries = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = str(row.get('original_number') or row.get('number') or row.get('invoice_id') or '')
        if not key:
            continue
        # Mutable processing/download fields do not define a document. Validate
        # each candidate only after filtering and pagination by this source key.
        entries.setdefault(key, []).append(row)
    return sorted(entries.items())


def _indexed_document(root, supplier, key, entry):
    files, paths = [], {}
    for field, role in (('pdf_path', 'original'), ('raw_path', 'original'), ('csv_path', 'parsed')):
        if entry.get(field):
            path = _source_path(root, supplier, entry[field])
            paths[field] = path
            if str(path) not in {str(file['path']) for file in files}:
                files.append({'path': path, 'role': role})
    if not files:
        raise InvoiceArchiveError('invoice_document_missing', 404)
    # Validated PDF parsers carry source tax amounts; normalized CSV intentionally
    # carries acquisition values only and must not be used to invent invoice VAT.
    original_pdf = next((file['path'] for file in files if file['path'].suffix.lower() == '.pdf'), None)
    if supplier in ('sk-profi-bike', 'zookee') and original_pdf:
        metadata, lines = _parsed_pdf(supplier, _bytes(original_pdf))
        return metadata, lines, files
    # Only source-proven dates are accepted. Generic uploads historically put
    # today's date in their index, which is an upload date, not an invoice date.
    dated_source = supplier == 'northfinder' or entry.get('source_fingerprint') or entry.get('issue_date_source') == 'document'
    metadata = {'invoice_number': key, 'invoice_date': _source_date(entry.get('issue_date')) if dated_source else None,
        'due_date': _source_date(entry.get('due_date')), 'currency': entry.get('currency') or None,
        'total_amount': _number_text(entry.get('total_with_vat', entry.get('total'))),
        'total_without_vat': _number_text(entry.get('total_without_vat')), 'vat_amount': _number_text(entry.get('vat_amount')),
        'total_with_vat': _number_text(entry.get('total_with_vat', entry.get('total')))}
    config_path = confined_path(root, f'suppliers/{supplier}/config.json')
    cfg = json.loads(config_path.read_text()) if config_path.is_file() else {}
    mapping = ((cfg.get('adapter_settings') or {}).get('mapping') or (cfg.get('invoices') or {}).get('mapping') or {}).get('invoice_to_canon') or {}
    if metadata['currency'] is None:
        metadata['currency'] = (cfg.get('invoices') or {}).get('currency') or None
    lines = _csv_lines(paths['csv_path'], mapping) if 'csv_path' in paths else []
    # If CSV is the supplied original, retain it as such for accountant export.
    if len(files) == 1:
        files[0]['role'] = 'original'
    return metadata, lines, files


def _indexed_candidates(root, supplier, key, entries):
    """Merge index aliases only when facts and original bytes prove agreement."""
    if len(entries) == 1:
        return (*_indexed_document(root, supplier, key, entries[0]), entries[0])
    expected = None
    merged, evidence = {}, []
    for entry in entries:
        metadata, lines, files = _indexed_document(root, supplier, key, entry)
        header = ArchiveMetadata.model_validate(metadata)
        rows = [ArchiveLine.model_validate(line) for line in lines]
        originals, observed = set(), []
        for file in files:
            # Freeze the proven bytes for ingestion, so a subsequent cache
            # replacement cannot substitute an unverified original.
            content = _bytes(file['path'])
            digest = hashlib.sha256(content).hexdigest()
            observed.append({'path': str(file['path']), 'role': file['role'], 'sha256': digest})
            if file['role'] == 'original':
                originals.add(digest)
            if digest not in merged or file['role'] == 'original':
                merged[digest] = {'filename': file['path'].name, 'content': content, 'role': file['role']}
        proof = (source_hash(header, rows), frozenset(originals))
        if not originals or expected is not None and proof != expected:
            raise InvoiceArchiveError('invoice_index_ambiguous')
        if expected is None:
            expected, agreed_header, agreed_rows = proof, header, rows
        evidence.append({'entry': entry, 'files': observed})
    return agreed_header, agreed_rows, list(merged.values()), {'index_candidates': evidence}


async def reconcile_supplier(db, payload, *, source_keys=None):
    """Bounded, explicit index import; individual bad documents remain visible."""
    if not isinstance(payload, InvoiceReconcileInput):
        payload = InvoiceReconcileInput.model_validate(payload)
    supplier = await _supplier(db, payload.supplier_code)
    entries = _index_entries(Path(settings.INVENTORY_DATA_ROOT), supplier.code)
    if source_keys is not None:
        entries = [(key, entry) for key, entry in entries if key in source_keys]
    selected = [(key, entry) for key, entry in entries if payload.after_key is None or key > payload.after_key]
    batch = selected[:payload.limit]
    report = {'supplier_code': supplier.code, 'scanned': len(batch), 'created': 0, 'existing': 0,
              'conflicts': [], 'next_after_key': batch[-1][0] if len(selected) > len(batch) else None}
    for key, candidates in batch:
        try:
            metadata, lines, files, evidence = _indexed_candidates(
                Path(settings.INVENTORY_DATA_ROOT), supplier.code, key, candidates)
            async with db.begin_nested():
                # All aliases must agree on a single existing receipt as well.
                session_keys = sorted({str(entry.get('number') or key) for entry in candidates})
                session_ids = (await db.execute(text('''SELECT id FROM receiving_sessions
                    WHERE supplier_id=:supplier AND invoice_number=ANY(CAST(:numbers AS text[]))'''),
                    {'supplier': supplier.id, 'numbers': session_keys})).scalars().all()
                if len(session_ids) > 1:
                    raise InvoiceArchiveError('invoice_receiving_ambiguous')
                result = await ingest_invoice(db, direction='purchase', source_kind='supplier', source_key=key,
                    supplier_id=supplier.id, metadata=metadata, lines=lines, files=files, source_data=evidence,
                    actor='supplier-reconcile')
                # A receiving session is a separate workflow; link only an exact
                # existing supplier+invoice key, never create or finalize one.
                if session_ids:
                    await db.execute(text('UPDATE uploaded_invoices SET receiving_session_id=:session WHERE id=:id'),
                        {'id': result['id'], 'session': session_ids[0]})
                report['existing' if result['replayed'] else 'created'] += 1
        except (InvoiceArchiveError, HTTPException, ValidationError, ValueError, OSError) as error:
            report['conflicts'].append({'source_key': key, 'code': getattr(error, 'code', 'invoice_source_invalid')})
    return report


async def _supplier(db, supplier_code):
    supplier = await db.scalar(select(Supplier).where(Supplier.code == supplier_code))
    if supplier is None:
        # Configuration-only suppliers can archive invoices before their first
        # catalog import or receipt. This creates the supplier row, never cards.
        config_path = confined_path(settings.INVENTORY_DATA_ROOT, f'suppliers/{code_component(supplier_code)}/config.json')
        if not config_path.is_file():
            raise InvoiceArchiveError('invoice_owner_missing', 404)
        cfg = config_io.load_supplier(supplier_code, write_back_on_load=False)
        name = str(cfg.get('name') or supplier_code)[:255]
        await db.execute(text('''INSERT INTO suppliers(code,name,config_path)
            VALUES(:code,:name,:path) ON CONFLICT(code) DO NOTHING'''),
            {'code': supplier_code, 'name': name, 'path': str(config_path)})
        supplier = await db.scalar(select(Supplier).where(Supplier.code == supplier_code))
    return supplier


async def archive_after_upload(supplier_code, result, invoice_number=None):
    """Finish file-first uploads; a DB outage leaves an explicit recoverable state."""
    if not settings.USE_POSTGRES:
        return {'status': 'disabled'}
    from inventory_hub.database import get_session_context
    from sqlalchemy.exc import SQLAlchemyError
    try:
        async with get_session_context() as db:
            if result.get('csv_path'):
                key = result.get('invoice_number') or invoice_number or Path(result['csv_path']).stem
                report = await reconcile_supplier(db, InvoiceReconcileInput(supplier_code=supplier_code), source_keys={key})
                if not report['scanned']:
                    raise InvoiceArchiveError('invoice_index_entry_missing', 404)
                return {'status': 'completed' if not report['conflicts'] else 'attention', **report}
            supplier = await _supplier(db, supplier_code)
            number = invoice_number or Path(result['filename']).stem
            path = _source_path(Path(settings.INVENTORY_DATA_ROOT), supplier_code, result['path'])
            saved = await ingest_invoice(db, direction='purchase', source_kind='supplier', source_key=number,
                supplier_id=supplier.id, metadata={'invoice_number': number}, files=[{'path': path, 'role': 'original'}],
                actor='supplier-upload')
            return {'status': 'completed', **saved}
    except (InvoiceArchiveError, HTTPException, SQLAlchemyError, OSError, ValueError) as error:
        return {'status': 'pending', 'code': getattr(error, 'code', 'invoice_archive_unavailable')}


async def archive_after_refresh(supplier_code):
    if not settings.USE_POSTGRES:
        return {'status': 'disabled'}
    from inventory_hub.database import get_session_context
    from sqlalchemy.exc import SQLAlchemyError
    try:
        async with get_session_context() as db:
            report = await reconcile_supplier(db, InvoiceReconcileInput(supplier_code=supplier_code, limit=200))
            return {'status': 'completed' if not report['conflicts'] else 'attention', **report}
    except (InvoiceArchiveError, HTTPException, SQLAlchemyError, OSError, ValueError) as error:
        return {'status': 'pending', 'code': getattr(error, 'code', 'invoice_archive_unavailable')}
