"""Keep malformed historical sources out of ingestion without deleting originals."""
from pathlib import Path
from sqlalchemy import select, text
from inventory_hub.db_models import Supplier
from inventory_hub.settings import settings
from inventory_hub.services import invoice_archive as archive
from inventory_hub.services.receiving_source import lock_supplier


def _input(payload):
    return payload.model_dump(mode='json', exclude={'preview_hash', 'confirmed'})


def _result(row):
    return {'request_id': str(row['id']), 'supplier_id': row['supplier_id'],
            'source_key': row['source_key'], 'action': row['action'], 'note': row['note'],
            'actor': row['actor'], 'created_at': row['created_at'].isoformat(),
            'stock_effect': False, 'payment_effect': False, 'original_files_changed': False}


async def get(db, identifier):
    row = (await db.execute(text('SELECT * FROM invoice_source_archive_events WHERE id=CAST(:id AS uuid)'),
                            {'id': str(identifier)})).mappings().first()
    if row is None:
        raise archive.InvoiceArchiveError('invoice_source_operation_missing', 404)
    return _result(row)


async def current(db, supplier_code):
    rows = (await db.execute(text('''SELECT latest.*, s.code AS supplier_code FROM (
        SELECT DISTINCT ON (supplier_id,source_key) * FROM invoice_source_archive_events
        ORDER BY supplier_id,source_key,created_at DESC,id DESC
        ) latest JOIN suppliers s ON s.id=latest.supplier_id
        WHERE s.code=:code ORDER BY latest.source_key LIMIT 201'''),
        {'code': supplier_code})).mappings().all()
    return {'items': [{**_result(row), 'archived': row['action'] == 'archive'} for row in rows[:200]],
            'truncated': len(rows) > 200}


async def _context(db, payload, *, lock=False):
    supplier = await db.scalar(select(Supplier).where(Supplier.code == payload.supplier_code))
    if supplier is None:
        raise archive.InvoiceArchiveError('invoice_owner_missing', 404)
    if lock:
        await lock_supplier(db, supplier.id)
    latest = (await db.execute(text('''SELECT id,action FROM invoice_source_archive_events
        WHERE supplier_id=:supplier AND source_key=:key ORDER BY created_at DESC,id DESC LIMIT 1'''),
        {'supplier': supplier.id, 'key': payload.source_key})).mappings().first()
    entries = dict(archive._index_entries(Path(settings.INVENTORY_DATA_ROOT), supplier.code))
    if payload.action == 'archive' and payload.source_key not in entries:
        raise archive.InvoiceArchiveError('invoice_source_missing', 404)
    invoices = (await db.execute(text('''SELECT id,archived_at FROM uploaded_invoices
        WHERE supplier_id=:supplier AND source_kind='supplier' AND source_key=:key ORDER BY id'''),
        {'supplier': supplier.id, 'key': payload.source_key})).mappings().all()
    archived = bool(latest and latest['action'] == 'archive')
    blockers = []
    if payload.action == 'archive':
        if archived:
            blockers.append('invoice_source_already_archived')
        if any(row['archived_at'] is None for row in invoices):
            blockers.append('invoice_source_archive_invoice_first')
    elif not archived:
        blockers.append('invoice_source_not_archived')
    state = {'supplier_id': supplier.id, 'source_key': payload.source_key,
             'latest_event': str(latest['id']) if latest else None, 'archived': archived,
             'source_hash': archive._digest(entries.get(payload.source_key)),
             'invoices': [{'id': row['id'], 'archived': row['archived_at'] is not None} for row in invoices]}
    result = {'ready': not blockers, 'blockers': blockers, 'before_archived': archived,
              'after_archived': payload.action == 'archive', 'source_key': payload.source_key,
              'supplier_code': supplier.code, 'note': payload.note,
              'stock_effect': False, 'payment_effect': False, 'original_files_changed': False,
              'preview_hash': archive._digest({'input': _input(payload), 'state': state})}
    return supplier.id, result


async def preview(db, payload):
    _, result = await _context(db, payload)
    return result


async def apply(db, payload, actor):
    request_hash = archive._digest(_input(payload))
    # UUID lock first, then the same supplier lock as receiving/reconciliation.
    await db.execute(text('SELECT pg_advisory_xact_lock(:key)'),
        {'key': int(archive._digest(['invoice-source-archive', str(payload.request_id)])[:15], 16)})
    old = (await db.execute(text('SELECT * FROM invoice_source_archive_events WHERE id=CAST(:id AS uuid)'),
                            {'id': str(payload.request_id)})).mappings().first()
    if old:
        if old['request_hash'] != request_hash:
            raise archive.InvoiceArchiveError('invoice_source_request_conflict')
        result = _result(old)
        await db.commit()
        return result
    supplier_id, prepared = await _context(db, payload, lock=True)
    if prepared['preview_hash'] != payload.preview_hash:
        raise archive.InvoiceArchiveError('invoice_source_preview_stale')
    if not prepared['ready']:
        raise archive.InvoiceArchiveError(prepared['blockers'][0])
    row = (await db.execute(text('''INSERT INTO invoice_source_archive_events
        (id,supplier_id,source_key,action,note,actor,request_hash)
        VALUES(CAST(:id AS uuid),:supplier,:key,:action,:note,:actor,:hash) RETURNING *'''),
        {'id': str(payload.request_id), 'supplier': supplier_id, 'key': payload.source_key,
         'action': payload.action, 'note': payload.note, 'actor': actor, 'hash': request_hash})).mappings().one()
    result = _result(row)
    await db.commit()
    return result
