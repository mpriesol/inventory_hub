"""Shared supplier upload: retain the original, index facts, prepare receiving.

Preparation creates no receiving session, physical movement or shop write.
"""
from __future__ import annotations
import asyncio
import fcntl
import hashlib
import json
import logging
import re
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from pydantic import ValidationError
from inventory_hub.adapters import invoice_pdf
from inventory_hub.adapters.sk_profi_bike_invoice import (
    InvoiceConflictError, InvoiceParseError, _write_atomic, save_receiving_invoice,
)
from inventory_hub.services import invoice_archive as archive
from inventory_hub.services.receiving_source import lock_supplier
from inventory_hub.settings import settings

logger = logging.getLogger(__name__)
ALLOWED_EXTENSIONS = {'.pdf', '.csv', '.tsv', '.xlsx', '.xls', '.doc', '.docx', '.xml', '.txt', '.jpg', '.jpeg', '.png'}


def _number_from_filename(filename):
    for pattern in (r'F?(\d{10})', r'(\d{7,})', r'FA[-_]?(\d+)', r'INV[-_]?(\d+)', r'[Ff]akt[uú]ra[-_]?(\d+)'):
        match = re.search(pattern, Path(filename).stem)
        if match:
            return match.group(1)
    return None


def _spreadsheet(supplier, filename, content):
    from inventory_hub.adapters.northfinder_xlsx_parser import parse_xlsx_to_csv
    with tempfile.TemporaryDirectory(prefix='hub-invoice-') as directory:
        source = Path(directory) / ('original' + Path(filename).suffix.lower())
        target = Path(directory) / 'parsed.csv'
        source.write_bytes(content)
        report = parse_xlsx_to_csv(source, target)
        if not report['success']:
            return {'parse_error': report.get('code', 'invoice_parse_failed')}, [], None
        parsed = target.read_bytes()
        try:
            return {'currency': 'EUR'}, archive._csv_content(parsed), parsed
        except (archive.InvoiceArchiveError, ValidationError, ValueError):
            return {'parse_error': 'invoice_parse_failed'}, [], None


def _prepare_spreadsheet(root, supplier, filename, content, parsed, metadata):
    """Atomically publish the validated intermediate without inventing dates."""
    number = metadata['invoice_number']
    key = re.sub(r'[^a-zA-Z0-9_-]', '_', number)
    if not key or len(key) > 100:
        raise InvoiceConflictError('invoice_source_invalid')
    area = root / 'suppliers' / supplier / 'invoices'
    area.mkdir(parents=True, exist_ok=True)
    raw = area / 'raw' / (key + Path(filename).suffix.lower())
    csv = area / 'csv' / (key + '.csv')
    index_path = area / 'index.latest.json'
    identifier = f'{supplier}:{key}'
    digest = hashlib.sha256(content).hexdigest()
    with (area / '.upload.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            index = json.loads(index_path.read_text()) if index_path.exists() else {}
        except (ValueError, OSError):
            raise InvoiceConflictError('invoice_index_invalid') from None
        if isinstance(index, dict) and isinstance(index.get('invoices'), list):
            index = index['invoices']
        if isinstance(index, list):
            if any(not isinstance(row, dict) or not (row.get('invoice_id') or row.get('number')) for row in index):
                raise InvoiceConflictError('invoice_index_invalid')
            index = {row.get('invoice_id') or f"{supplier}:{row['number']}": row for row in index}
        if not isinstance(index, dict):
            raise InvoiceConflictError('invoice_index_invalid')
        aliases = [item for item, value in index.items() if isinstance(value, dict) and
                   (item == identifier or value.get('number') == key or value.get('original_number') == number)]
        if len(aliases) > 1:
            raise InvoiceConflictError('invoice_index_ambiguous')
        index_key = aliases[0] if aliases else identifier
        existing = index.get(index_key)
        for path, expected in ((raw, content), (csv, parsed)):
            if path.exists() and path.read_bytes() != expected:
                raise InvoiceConflictError('invoice_source_changed')
        if existing and existing.get('source_fingerprint') not in (None, digest):
            raise InvoiceConflictError('invoice_source_changed')
        if existing and (existing.get('raw_path') != raw.relative_to(root).as_posix() or
                         existing.get('csv_path') != csv.relative_to(root).as_posix()):
            raise InvoiceConflictError('invoice_source_changed')
        _write_atomic(raw, content)
        _write_atomic(csv, parsed)
        if not existing:
            index[index_key] = {'supplier': supplier, 'invoice_id': identifier, 'number': key,
                'original_number': number, 'issue_date': str(metadata.get('invoice_date') or '') or None,
                'due_date': str(metadata.get('due_date') or '') or None,
                'issue_date_source': 'operator' if metadata.get('invoice_date') else None,
                'currency': 'EUR', 'vat_included': False, 'source_fingerprint': digest,
                'raw_path': raw.relative_to(root).as_posix(), 'csv_path': csv.relative_to(root).as_posix(),
                'status': 'new', 'downloaded_at': datetime.now(timezone.utc).isoformat()}
            _write_atomic(index_path, json.dumps(index, ensure_ascii=False, indent=2).encode())
    return {'filename': raw.name, 'path': raw.relative_to(root).as_posix(),
            'csv_path': csv.relative_to(root).as_posix(), 'invoice_id': identifier}


async def upload(db, *, supplier_code, filename, content, invoice_number=None,
                 invoice_date=None, due_date=None, vat_included=True, expected_id=None, actor='operator'):
    if not filename or Path(filename).suffix.lower() not in ALLOWED_EXTENSIONS:
        raise archive.InvoiceArchiveError('invoice_file_type_unsupported', 422)
    if not content or len(content) > archive.MAX_FILE:
        raise archive.InvoiceArchiveError('invoice_file_invalid', 422)
    supplier = await archive.resolve_supplier(db, supplier_code)
    supplier_id, code = supplier.id, supplier.code
    try:
        supplied = {'invoice_number': invoice_number or None,
                    'invoice_date': date.fromisoformat(invoice_date) if invoice_date else None,
                    'due_date': date.fromisoformat(due_date) if due_date else None}
    except ValueError:
        raise archive.InvoiceArchiveError('invoice_date_invalid', 422) from None
    parsed_csv = None
    suffix = Path(filename).suffix.lower()
    if code == 'northfinder' and suffix in ('.xlsx', '.xls'):
        metadata, lines, parsed_csv = await asyncio.to_thread(_spreadsheet, code, filename, content)
    else:
        metadata, lines = await asyncio.to_thread(archive.parse_uploaded_document, code, filename, content)
    verified_pdf = bool(lines and suffix == '.pdf' and code in invoice_pdf.PARSERS)
    if expected_id is not None and not verified_pdf:
        raise archive.InvoiceArchiveError('invoice_parse_failed', 422)
    for key, value in supplied.items():
        if value is not None and metadata.get(key) is not None and value != metadata[key]:
            raise archive.InvoiceArchiveError('invoice_source_metadata_conflict', 422)
        if value is not None:
            metadata[key] = value
    metadata.setdefault('invoice_number', _number_from_filename(filename))
    if parsed_csv is not None and not metadata.get('invoice_number'):
        raise archive.InvoiceArchiveError('invoice_number_required_for_receiving', 422)
    await lock_supplier(db, supplier_id)
    if await archive.source_is_archived(db, supplier_id, metadata.get('invoice_number')):
        raise archive.InvoiceArchiveError('invoice_source_archived')
    result = await archive.ingest_invoice(db, direction='purchase', source_kind='upload',
        source_key='sha256:' + hashlib.sha256(content).hexdigest(), supplier_id=supplier_id,
        metadata=metadata, lines=lines, files=[{'filename': filename, 'content': content}],
        source_data={'kind': 'manual-upload'}, actor=actor, expected_id=expected_id,
        complete_unparsed_pdf=verified_pdf and code == 'northfinder')
    if not result['replayed']:
        proven_net = verified_pdf or parsed_csv is not None
        await db.execute(text('UPDATE uploaded_invoices SET vat_included=:included WHERE id=:id'),
                         {'id': result['id'], 'included': False if proven_net else vat_included})
    # Preserve original/source facts regardless of preparation. Exact original
    # identity makes a retry safe; preparation failure is a partial result.
    await db.commit()
    issues, prepared = [], None
    if metadata.get('parse_error'):
        issues.append(metadata['parse_error'])
    elif not lines:
        issues.append('invoice_parser_unavailable')
    else:
        try:
            await lock_supplier(db, supplier_id)
            state = (await db.execute(text('SELECT archived_at,processing_status FROM uploaded_invoices WHERE id=:id'),
                                      {'id': result['id']})).mappings().first()
            # Archiving a source does not necessarily archive its document.
            # The first commit released the supplier fence, so recheck before
            # publishing any receiving file, while retaining the saved original.
            if await archive.source_is_archived(db, supplier_id, metadata.get('invoice_number')):
                issues.append('invoice_source_archived')
            elif state and (state['archived_at'] is not None or state['processing_status'] != 'unprocessed'):
                issues.append('invoice_not_available_for_receiving')
            elif code in invoice_pdf.PARSERS and suffix == '.pdf':
                prepared = await asyncio.to_thread(save_receiving_invoice, content,
                    Path(settings.INVENTORY_DATA_ROOT), code, metadata.get('invoice_number'),
                    parser=lambda data: invoice_pdf.parse_invoice_pdf(code, data))
            elif parsed_csv is not None:
                prepared = await asyncio.to_thread(_prepare_spreadsheet, Path(settings.INVENTORY_DATA_ROOT),
                    code, filename, content, parsed_csv, metadata)
            else:
                issues.append('receiving_source_unverified')
            await db.commit()
        except (InvoiceConflictError, InvoiceParseError, OSError, ValueError, TypeError, KeyError, SQLAlchemyError) as error:
            await db.rollback()
            issues.append('invoice_receiving_preparation_conflict' if isinstance(error, InvoiceConflictError)
                          else 'invoice_receiving_preparation_failed')
            logger.warning('Invoice receiving preparation incomplete: invoice_id=%s code=%s', result['id'], issues[-1])
    missing = [key for key in ('invoice_number', 'invoice_date', 'due_date', 'currency', 'total_with_vat')
               if metadata.get(key) is None]
    return {**result, **(prepared or {}), 'success': True, 'supplier_code': code,
            'original_filename': filename, 'filename': (prepared or {}).get('filename', filename),
            'csv_path': (prepared or {}).get('csv_path'), 'invoice_number': metadata.get('invoice_number'),
            'size_bytes': len(content), 'items_count': len(lines), 'message': 'Invoice archived',
            'archive': {'status': 'completed', 'id': result['id']},
            'processing': {'status': 'partial' if issues or missing else 'complete',
                'parsed_lines': len(lines), 'stored_lines': result.get('lines_count', len(lines)),
                'receiving_ready': prepared is not None, 'issues': issues, 'missing_fields': missing}}


async def reparse_northfinder_pdf(db, identifier, *, actor):
    row = (await db.execute(text('''SELECT ui.original_filename,ui.file_path,ui.file_type,ui.source_kind
        FROM uploaded_invoices ui JOIN suppliers s ON s.id=ui.supplier_id
        WHERE ui.id=:id AND ui.direction='purchase' AND s.code='northfinder' '''),
        {'id': identifier})).mappings().first()
    if row is None:
        raise archive.InvoiceArchiveError('invoice_not_found', 404)
    if row['file_type'] != 'pdf' or row['source_kind'] not in ('upload', 'supplier'):
        raise archive.InvoiceArchiveError('invoice_reparse_conflict')
    try:
        path = archive._source_path(Path(settings.INVENTORY_DATA_ROOT), 'northfinder', row['file_path'])
        content = await asyncio.to_thread(archive._bytes, path)
    except (OSError, TypeError):
        raise archive.InvoiceArchiveError('invoice_document_missing', 404) from None
    return await upload(db, supplier_code='northfinder', filename=row['original_filename'],
                        content=content, expected_id=identifier, actor=actor)
