"""Reasoned invoice handling and archival, independent of stock and payments.

Supplier lock -> related receipt rows -> invoice row is shared with receiving.
Every mutation appends its audit and exact UUID result in the same transaction.
"""
from datetime import datetime, timezone
from hashlib import sha256
import json

from sqlalchemy import text

from inventory_hub.invoice_processing_types import InvoiceProcessingPreview
from inventory_hub.services.receiving_source import lock_supplier


class InvoiceProcessingError(Exception):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str)


def _digest(value):
    return sha256(_json(value).encode('utf-8')).hexdigest()


def _keys(row):
    keys = {row.get('invoice_number'), row.get('source_key')}
    source = row.get('source_data') or {}
    for item in source.get('index_candidates', []):
        entry = item.get('entry', {}) if isinstance(item, dict) else {}
        keys.update((entry.get('number'), entry.get('original_number')))
    return sorted(value for value in keys if isinstance(value, str) and value)


async def _invoice(db, identifier, lock=False):
    row = (await db.execute(text('SELECT * FROM uploaded_invoices WHERE id=:id' + (' FOR UPDATE' if lock else '')),
                            {'id': identifier})).mappings().first()
    if row is None:
        raise InvoiceProcessingError('invoice_not_found', 404)
    return dict(row)


async def _receipts(db, row, lock=False):
    if row['direction'] != 'purchase':
        return []
    values = {'supplier': row['supplier_id'], 'session': row.get('receiving_session_id'), 'numbers': _keys(row)}
    records = (await db.execute(text('''SELECT rs.id,rs.invoice_number,rs.status::text AS status,
        rs.session_data,rs.updated_at,
        EXISTS(SELECT 1 FROM stock_movements sm WHERE sm.reference_type='receiving_session'
               AND sm.reference_id=rs.id::text) AS has_stock_movements
        FROM receiving_sessions rs WHERE rs.supplier_id=:supplier
          AND (rs.id=CAST(:session AS bigint) OR rs.invoice_number=ANY(CAST(:numbers AS text[])))
        ORDER BY rs.id''' + (' FOR UPDATE OF rs' if lock else '')), values)).mappings().all()
    return [dict(record) for record in records]


def _receipt(record):
    data = record['session_data'] or {}
    archived = data.get('receiving_archive') or None
    reversed_posting = bool(data.get('receiving_reversed')) and not data.get('finalize_result')
    status = archived.get('previous_status', record['status']) if archived else record['status']
    received = not reversed_posting and (status == 'completed' or record['has_stock_movements'])
    return {'id': record['id'], 'invoice_number': record['invoice_number'], 'status': record['status'],
            'archived': bool(archived), 'received': received, 'reversed': reversed_posting,
            'posting_revision': data.get('posting_revision', 0)}


def _snapshot(row, receipts):
    linked = [_receipt(item) for item in receipts]
    if any(item['received'] for item in linked):
        status = 'received'
    elif any(not item['archived'] and item['status'] in ('new', 'in_progress', 'paused') for item in linked):
        status = 'receiving'
    else:
        status = row['processing_status']
    return {'revision': row['processing_revision'], 'status': status,
            'manual_status': row['processing_status'], 'archived': row['archived_at'] is not None,
            'archived_at': row['archived_at'].isoformat() if row['archived_at'] else None,
            'receipts': linked, 'stock_effect': False, 'payment_effect': False}


def _blockers(row, snapshot, action):
    if action in ('process_without_stock', 'reopen'):
        if row['direction'] != 'purchase':
            return ['invoice_processing_purchase_required']
        if snapshot['archived']:
            return ['invoice_archived']
    if action == 'process_without_stock':
        if snapshot['status'] == 'received':
            return ['invoice_processing_receipt_owned']
        if snapshot['status'] == 'receiving':
            return ['invoice_processing_receiving_active']
        if row['processing_status'] != 'unprocessed':
            return ['invoice_processing_already_processed']
    elif action == 'reopen' and row['processing_status'] != 'processed_without_stock':
        return ['invoice_processing_receipt_owned' if snapshot['status'] == 'received'
                else 'invoice_processing_not_manually_processed']
    elif action == 'archive':
        if snapshot['archived']:
            return ['invoice_already_archived']
        if any(not item['archived'] and (item['status'] != 'cancelled' or item['received'])
               for item in snapshot['receipts']):
            return ['invoice_processing_archive_receipt_first']
    elif action == 'restore' and not snapshot['archived']:
        return ['invoice_not_archived']
    return []


async def _context(db, identifier, lock=False):
    row = await _invoice(db, identifier)
    if lock and row['supplier_id'] is not None:
        await lock_supplier(db, row['supplier_id'])
    receipts = await _receipts(db, row, lock=lock)
    if lock:
        locked = await _invoice(db, identifier, lock=True)
        if (_keys(locked), locked['supplier_id'], locked.get('receiving_session_id')) != (
                _keys(row), row['supplier_id'], row.get('receiving_session_id')):
            raise InvoiceProcessingError('invoice_processing_changed')
        row = locked
    return row, receipts


def _preview(row, receipts, request):
    before = _snapshot(row, receipts)
    if before['revision'] != request.expected_revision:
        raise InvoiceProcessingError('invoice_processing_changed')
    blockers = _blockers(row, before, request.action)
    if blockers:
        raise InvoiceProcessingError(blockers[0])
    after = {**before, 'revision': before['revision'] + 1}
    if request.action == 'process_without_stock':
        after.update(status='processed_without_stock', manual_status='processed_without_stock')
    elif request.action == 'reopen':
        after.update(status='unprocessed', manual_status='unprocessed')
    elif request.action == 'archive':
        after['archived'] = True
    else:
        after.update(archived=False, archived_at=None)
    fingerprint = _digest({'invoice_id': row['id'], 'source_hash': row.get('source_hash'),
        'invoice_number': row.get('invoice_number'), 'original_filename': row.get('original_filename'),
        'before': before, 'receipts': receipts,
        'request': InvoiceProcessingPreview.model_validate(request.model_dump(include={'action', 'expected_revision', 'note'})).model_dump()})
    return {'invoice_id': row['id'], 'invoice_number': row.get('invoice_number'),
            'action': request.action, 'note': request.note, 'preview_hash': fingerprint,
            'before': before, 'after': after, 'stock_effect': False, 'payment_effect': False}


async def preview(db, identifier, request):
    row, receipts = await _context(db, identifier)
    return _preview(row, receipts, request)


async def detail(db, row):
    row = dict(row)
    current = _snapshot(row, await _receipts(db, row))
    actions = ('process_without_stock', 'reopen', 'archive', 'restore')
    current['allowed_actions'] = [action for action in actions if not _blockers(row, current, action)]
    current['action_blockers'] = {action: errors for action in actions if (errors := _blockers(row, current, action))}
    history = (await db.execute(text('''SELECT request_id,revision,action,note,actor,before_data,after_data,created_at
        FROM invoice_processing_operations WHERE invoice_id=:id ORDER BY revision DESC LIMIT 50'''),
        {'id': row['id']})).mappings().all()
    current['history'] = [{**dict(item), 'request_id': str(item['request_id']),
                           'created_at': item['created_at'].isoformat()} for item in history]
    return current


async def operation_result(db, identifier, request_id):
    saved = (await db.execute(text('SELECT invoice_id,result FROM invoice_processing_operations WHERE request_id=:request'),
                              {'request': request_id})).mappings().first()
    if saved is None or saved['invoice_id'] != identifier:
        raise InvoiceProcessingError('invoice_processing_operation_not_found', 404)
    return saved['result']


async def apply(db, identifier, request, *, actor):
    payload_hash = _digest({'invoice_id': identifier, 'request': request.model_dump(mode='json')})
    await db.execute(text('SELECT pg_advisory_xact_lock(:key)'),
                     {'key': int.from_bytes(request.request_id.bytes[:8], 'big', signed=True)})
    saved = (await db.execute(text('SELECT invoice_id,payload_hash,result FROM invoice_processing_operations WHERE request_id=:request'),
                              {'request': request.request_id})).mappings().first()
    if saved:
        if saved['invoice_id'] != identifier or saved['payload_hash'] != payload_hash:
            raise InvoiceProcessingError('invoice_processing_request_conflict')
        return saved['result']
    row, receipts = await _context(db, identifier, lock=True)
    reviewed = _preview(row, receipts, request)
    if reviewed['preview_hash'] != request.preview_hash:
        raise InvoiceProcessingError('invoice_processing_changed')
    before = reviewed['before']
    row['processing_revision'] += 1
    if request.action == 'process_without_stock':
        row['processing_status'] = 'processed_without_stock'
    elif request.action == 'reopen':
        row['processing_status'] = 'unprocessed'
    elif request.action == 'archive':
        row['archived_at'] = datetime.now(timezone.utc)
    else:
        row['archived_at'] = None
    after = _snapshot(row, receipts)
    response = {'invoice_id': identifier, 'request_id': str(request.request_id), 'action': request.action,
                'processing_revision': row['processing_revision'], 'processing': after,
                'stock_effect': False, 'payment_effect': False}
    await db.execute(text('''UPDATE uploaded_invoices SET processing_revision=:revision,
        processing_status=:status,archived_at=:archived,updated_at=NOW() WHERE id=:id'''),
        {'id': identifier, 'revision': row['processing_revision'], 'status': row['processing_status'], 'archived': row['archived_at']})
    await db.execute(text('''INSERT INTO invoice_processing_operations
        (request_id,invoice_id,revision,action,note,actor,payload_hash,before_data,after_data,result)
        VALUES(:request,:id,:revision,:action,:note,:actor,:hash,CAST(:before AS jsonb),CAST(:after AS jsonb),CAST(:result AS jsonb))'''),
        {'request': request.request_id, 'id': identifier, 'revision': row['processing_revision'],
         'action': request.action, 'note': request.note, 'actor': actor, 'hash': payload_hash,
         'before': _json(before), 'after': _json(after), 'result': _json(response)})
    return response


async def assert_receiving_allowed(db, supplier_id, invoice_number, session_id=None):
    """Caller holds the supplier lock before any receipt row; retain it to commit."""
    await lock_supplier(db, supplier_id)
    rows = (await db.execute(text('''SELECT ui.id,ui.archived_at,ui.processing_status
        FROM uploaded_invoices ui WHERE ui.direction='purchase' AND ui.supplier_id=:supplier
          AND (ui.receiving_session_id=CAST(:session AS bigint) OR ui.invoice_number=:number OR ui.source_key=:number
            OR EXISTS(SELECT 1 FROM jsonb_array_elements(CASE WHEN jsonb_typeof(ui.source_data->'index_candidates')='array'
              THEN ui.source_data->'index_candidates' ELSE '[]'::jsonb END) item
              WHERE item->'entry'->>'number'=:number OR item->'entry'->>'original_number'=:number))
        ORDER BY ui.id FOR UPDATE'''), {'supplier': supplier_id, 'session': session_id, 'number': invoice_number})).mappings().all()
    if any(row['archived_at'] is not None for row in rows):
        raise InvoiceProcessingError('invoice_archived')
    if any(row['processing_status'] == 'processed_without_stock' for row in rows):
        raise InvoiceProcessingError('invoice_processed_without_stock')


async def project_index(db, supplier_code, entries, *, include_archived=False):
    """Read-only DB overlay for the legacy supplier index; never trusts file flags."""
    supplier_id = await db.scalar(text('SELECT id FROM suppliers WHERE code=:code'), {'code': supplier_code})
    if supplier_id is None:
        return entries
    invoices = (await db.execute(text("SELECT * FROM uploaded_invoices WHERE direction='purchase' AND supplier_id=:supplier"),
                                  {'supplier': supplier_id})).mappings().all()
    receipts = (await db.execute(text('SELECT id,invoice_number,status::text AS status,session_data FROM receiving_sessions WHERE supplier_id=:supplier'),
                                  {'supplier': supplier_id})).mappings().all()
    source_events = (await db.execute(text('''SELECT DISTINCT ON (source_key) source_key,id,action
        FROM invoice_source_archive_events WHERE supplier_id=:supplier ORDER BY source_key,created_at DESC,id DESC'''),
        {'supplier': supplier_id})).mappings().all()
    excluded = {item['source_key']: str(item['id']) for item in source_events if item['action'] == 'archive'}
    result = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        keys = {item.get('number'), item.get('original_number')}
        identifier = item.get('invoice_id')
        if isinstance(identifier, str) and identifier.startswith(supplier_code + ':'):
            keys.add(identifier[len(supplier_code) + 1:])
        keys = {key for key in keys if isinstance(key, str) and key}
        matches = [row for row in invoices if keys.intersection(_keys(row))]
        linked_ids = {row['receiving_session_id'] for row in matches if row['receiving_session_id']}
        sessions = [row for row in receipts if row['invoice_number'] in keys or row['id'] in linked_ids]
        value = {**item, 'invoice_archived': any(row['archived_at'] is not None for row in matches),
                 'receiving_archived': bool(sessions) and all((row['session_data'] or {}).get('receiving_archive') for row in sessions),
                 'source_archived': bool(keys.intersection(excluded))}
        if len(matches) == 1:
            value.update(archive_invoice_id=matches[0]['id'], processing_status=matches[0]['processing_status'])
        if len(sessions) == 1:
            session = sessions[0]
            value['receiving_session_id'] = str(session['id'])
            value['current_session_id'] = str(session['id'])
            status = session['status']
            value['status'] = 'processed' if status == 'completed' else 'in_progress' if status in ('in_progress', 'paused') else 'new'
        if matches and all(row['processing_status'] == 'processed_without_stock' for row in matches):
            value.update(status='processed', processing_status='processed_without_stock')
        if not include_archived and any(value[key] for key in ('invoice_archived', 'receiving_archived', 'source_archived')):
            continue
        result.append(value)
    return result
