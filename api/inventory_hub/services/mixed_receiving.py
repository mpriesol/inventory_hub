"""Persistent mixed-invoice scans; one reviewed transaction posts their deltas.

Draft allocations reserve invoice capacity, never physical stock. Invoice lines,
identity preparation, FIFO writes and channel dispatch remain shared with the
existing receiving workflow.
"""
from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select, text

from inventory_hub.db_models import Product, ReceivingStatus, Warehouse
from inventory_hub.db_models_ext import ReceivingSession, StockMovement, ScanEvent, ScanSessionType, ScanStatus
from inventory_hub.mixed_receiving_types import BatchScan
from inventory_hub.services import receiving_lookup, receiving_operations, receiving_source
from inventory_hub.services.receiving_posting import write_stock_for_line
from inventory_hub.services.product_identity import IDENTITY_WRITE_LOCK
from inventory_hub.services.item_units import receipt_values, validate_stock_quantity, ItemUnitError


def fail(code, **detail):
    raise HTTPException(409, detail={'code': code, **detail})


def number(value):
    return str(Decimal(value or 0))


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def _physical_lines(session):
    # Only an explicitly identified service is outside physical completion.
    # An unknown invoice row must never disappear as an assumed service.
    return [line for line in session.lines
            if not (line.__dict__.get('product') and line.product.item_type == 'service')]


async def _batch(db, identifier, *, lock=False):
    row = (await db.execute(text('SELECT * FROM mixed_receiving_batches WHERE id=:id' +
                                (' FOR UPDATE' if lock else '')), {'id': identifier})).mappings().first()
    if row is None:
        raise HTTPException(404, detail={'code': 'mixed_receiving_batch_not_found'})
    return dict(row)


async def _request(db, batch_id, request, kind, extra=None):
    payload_hash = digest({'batch_id': str(batch_id), 'kind': kind,
                           'request': request.model_dump(mode='json'), 'extra': extra})
    key = int(digest(['mixed-receiving-request', str(request.request_id)])[:15], 16)
    await db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': key})
    previous = (await db.execute(text('SELECT * FROM mixed_receiving_requests WHERE request_id=:id'),
                                 {'id': request.request_id})).mappings().first()
    if previous:
        if str(previous['batch_id']) != str(batch_id) or previous['payload_hash'] != payload_hash:
            fail('mixed_receiving_request_conflict')
        result = {**previous['result'], 'replayed': True}
        if result.pop('_current_summary', False):
            result['summary'] = await summary(db, batch_id)
        return payload_hash, result
    return payload_hash, None


async def _save(db, batch_id, request_id, payload_hash, result):
    # Keep one small immutable acknowledgement per operation. Persisting every
    # growing batch summary per scan would make storage quadratic in scans.
    stored = {key: value for key, value in result.items() if key != 'summary'}
    if 'summary' in result:
        stored['_current_summary'] = True
    await db.execute(text('''INSERT INTO mixed_receiving_requests(request_id,batch_id,payload_hash,result)
        VALUES(:id,:batch,:hash,CAST(:result AS jsonb))'''),
        {'id': request_id, 'batch': batch_id, 'hash': payload_hash, 'result': encoded(stored)})
    return result


async def _scans(db, batch_id):
    return [dict(row) for row in (await db.execute(text('''SELECT * FROM mixed_receiving_scans
        WHERE batch_id=:id ORDER BY created_at,id'''), {'id': batch_id})).mappings()]


async def _manual_scans(db, batch_id):
    return [dict(row) for row in (await db.execute(text('''SELECT * FROM mixed_receiving_manual_scans
        WHERE batch_id=:id ORDER BY created_at,id'''), {'id': batch_id})).mappings()]


async def _manual_product(db, code, expected_product_id=None):
    from inventory_hub.services.product_identity import RemoteIdentity, load_identity_index
    identity = RemoteIdentity(0, code.strip(), barcodes=(code.strip(),), expected_product_id=expected_product_id)
    index = await load_identity_index(db, 0, [identity], local=True)
    resolution = index.resolve(identity)
    if resolution.status == 'conflict':
        fail('receiving_identity_conflict', reasons=resolution.reasons,
             candidate_product_ids=resolution.candidate_product_ids)
    product = index.products.get(resolution.product_id)
    if product is None or not product.is_active:
        fail('receiving_product_preparation_required')
    if product.item_type != 'stock':
        fail('stock_item_required')
    return product


async def manual_product(db, batch_id, code):
    await _batch(db, batch_id)
    product = await _manual_product(db, code)
    return {'product_id': product.id, 'sku': product.sku, 'title': product.name,
            'stock_unit': product.stock_unit}


async def manual_scan(db, batch_id, request, *, actor='receiving'):
    payload_hash, previous = await _request(db, batch_id, request, 'manual_scan')
    if previous:
        return previous
    batch = await _batch(db, batch_id, lock=True)
    if batch['status'] != 'open' or batch['archived_at']:
        fail('mixed_receiving_batch_closed')
    await db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': IDENTITY_WRITE_LOCK})
    product = await _manual_product(db, request.code)
    try:
        validate_stock_quantity(request.qty, product.stock_unit)
    except ItemUnitError as error:
        fail(error.code)
    await db.execute(text('''INSERT INTO mixed_receiving_manual_scans
        (id,batch_id,product_id,code,quantity,stock_unit,unit_cost,cost_status,reason,assignment_history)
        VALUES(:id,:batch,:product,:code,:qty,:unit,:cost,:cost_status,:reason,CAST(:history AS jsonb))'''),
        {'id': request.request_id, 'batch': batch_id, 'product': product.id, 'code': request.code.strip(),
         'qty': request.qty, 'unit': product.stock_unit,
         'cost': Decimal(request.unit_cost) if request.unit_cost is not None else None,
         'cost_status': request.cost_status, 'reason': request.reason,
         'history': encoded([{'action': 'manual_scan', 'actor': actor, 'confirmed_without_invoice': True,
                             'at': datetime.now(timezone.utc).isoformat()}])})
    result = {'request_id': str(request.request_id), 'scan_id': str(request.request_id), 'replayed': False,
              'candidates': [], 'summary': await summary(db, batch_id)}
    return await _save(db, batch_id, request.request_id, payload_hash, result)


async def archive(db, batch_id, request, *, actor='receiving', restore=False):
    kind = 'restore_batch' if restore else 'archive_batch'
    payload_hash, previous = await _request(db, batch_id, request, kind)
    if previous:
        return previous
    if restore:
        scope = await _batch(db, batch_id)
        if scope['status'] == 'open':
            warehouse = await db.scalar(select(Warehouse).where(Warehouse.id == scope['warehouse_id'])
                .with_for_update(read=True).execution_options(populate_existing=True))
            if warehouse is None or not warehouse.is_active:
                fail('mixed_receiving_warehouse_missing')
    batch = await _batch(db, batch_id, lock=True)
    if restore:
        if not batch['archived_at']:
            fail('mixed_receiving_not_archived')
        await db.execute(text('''UPDATE mixed_receiving_batches
            SET archived_at=NULL,archive_reason=NULL,archived_by=NULL WHERE id=:id'''), {'id': batch_id})
    else:
        if batch['archived_at']:
            fail('mixed_receiving_archived')
        scans = [row for row in [*await _scans(db, batch_id), *await _manual_scans(db, batch_id)]
                 if row['status'] == 'active']
        if request.empty_only and (batch['status'] != 'open' or scans):
            fail('mixed_receiving_not_empty')
        # A nonempty unfinished draft must first cancel its exact allocations.
        # Archiving cannot release capacity silently or hide a pending receipt.
        if batch['status'] == 'open' and scans:
            fail('mixed_receiving_pending')
        await db.execute(text('''UPDATE mixed_receiving_batches
            SET archived_at=now(),archive_reason=:reason,archived_by=:actor WHERE id=:id'''),
            {'id': batch_id, 'reason': request.reason, 'actor': actor})
    result = {'request_id': str(request.request_id), 'batch_id': str(batch_id),
              'archived': not restore, 'reason': request.reason, 'actor': actor, 'replayed': False}
    return await _save(db, batch_id, request.request_id, payload_hash, result)


async def _reserved(db, *, exclude_scan=None):
    return {row.line_id: row.quantity for row in (await db.execute(text('''
        SELECT s.line_id,SUM(s.quantity) AS quantity FROM mixed_receiving_scans s
        JOIN mixed_receiving_batches b ON b.id=s.batch_id
        WHERE b.status='open' AND s.status='active' AND (CAST(:exclude AS uuid) IS NULL OR s.id<>CAST(:exclude AS uuid))
        GROUP BY s.line_id'''), {'exclude': str(exclude_scan) if exclude_scan else None})).all()}


async def _sessions(db, session_ids, *, lock=False):
    from inventory_hub.routers import receiving_db as receiving
    rows = (await db.execute(text('''SELECT rs.id,s.code,s.id AS supplier_id FROM receiving_sessions rs
        JOIN suppliers s ON s.id=rs.supplier_id WHERE rs.id=ANY(CAST(:ids AS bigint[])) ORDER BY s.id,rs.id'''),
        {'ids': sorted(session_ids)})).all() if session_ids else []
    if lock:
        for supplier_id in sorted({row.supplier_id for row in rows}):
            await receiving_source.lock_supplier(db, supplier_id)
    return {row.id: await receiving._get_session_for_supplier(db, row.code, row.id, lock=lock) for row in rows}


async def _set_pending(db, sessions):
    for session in sessions.values():
        pending = bool(await db.scalar(text('''SELECT EXISTS(SELECT 1 FROM mixed_receiving_scans s
            JOIN mixed_receiving_batches b ON b.id=s.batch_id WHERE s.session_id=:id
            AND s.status='active' AND b.status='open')'''), {'id': session.id}))
        data = dict(session.session_data or {})
        if pending:
            data.setdefault('mixed_receiving_previous_status', session.status.value)
            data['mixed_receiving_pending'] = True
            session.status = ReceivingStatus.in_progress
            session.started_at = session.started_at or datetime.now(timezone.utc)
        else:
            data.pop('mixed_receiving_pending', None)
            previous = data.pop('mixed_receiving_previous_status', None)
            if previous and not data.get('mixed_receiving'):
                session.status = ReceivingStatus(previous)
        session.session_data = data
    await db.flush()


async def create(db, request, *, actor='receiving'):
    identifier = request.request_id
    payload_hash, previous = await _request(db, identifier, request, 'create')
    if previous:
        return await summary(db, identifier)
    warehouse = await db.scalar(select(Warehouse).where(
        Warehouse.code == request.warehouse_code if request.warehouse_code else Warehouse.is_default.is_(True))
        .with_for_update(read=True))
    if warehouse is None or not warehouse.is_active:
        fail('mixed_receiving_warehouse_missing')
    await db.execute(text('''INSERT INTO mixed_receiving_batches(id,warehouse_id,actor)
        VALUES(:id,:warehouse,:actor)'''), {'id': identifier, 'warehouse': warehouse.id, 'actor': actor})
    result = await summary(db, identifier)
    await _save(db, identifier, request.request_id, payload_hash, result)
    return result


async def list_batches(db, status='open'):
    rows = (await db.execute(text('''SELECT b.id,b.status,b.created_at,w.code AS warehouse_code,
        COUNT(s.id) FILTER(WHERE s.status='active') AS scan_count,
        COALESCE(SUM(s.quantity) FILTER(WHERE s.status='active'),0) AS total_qty
        FROM mixed_receiving_batches b JOIN warehouses w ON w.id=b.warehouse_id
        LEFT JOIN (SELECT batch_id,id,status,quantity FROM mixed_receiving_scans
            UNION ALL SELECT batch_id,id,status,quantity FROM mixed_receiving_manual_scans) s ON s.batch_id=b.id
        WHERE b.status=:status AND b.archived_at IS NULL
        GROUP BY b.id,w.code ORDER BY b.created_at DESC LIMIT 100'''), {'status': status})).mappings()
    return {'batches': [{**dict(row), 'id': str(row['id']), 'created_at': row['created_at'].isoformat(),
                        'total_qty': number(row['total_qty'])} for row in rows]}


async def _candidates(db, code, *, exclude_scan=None):
    result = await receiving_lookup.lookup(db, code)
    if result['unreadable_invoices'] and not result['candidates']:
        fail('mixed_receiving_lookup_incomplete', unreadable_invoices=result['unreadable_invoices'])
    reserved = await _reserved(db, exclude_scan=exclude_scan)
    candidates = result['candidates']
    sessions = await _sessions(db, {int(c['session_id']) for c in candidates if c['session_id']})
    posted = await posted_quantities(db, sessions)
    from inventory_hub.services.invoice_processing import _keys
    invoices = [dict(row) for row in (await db.execute(text("SELECT * FROM uploaded_invoices WHERE direction='purchase'"))).mappings()]
    retired = {(row.supplier_id, row.source_key) for row in (await db.execute(text('''
        SELECT DISTINCT ON (supplier_id,source_key) supplier_id,source_key,action
        FROM invoice_source_archive_events ORDER BY supplier_id,source_key,created_at DESC,id DESC'''))).all()
        if row.action == 'archive'}
    for candidate in candidates:
        candidate['unreadable_invoices'] = result['unreadable_invoices']
        session = sessions.get(int(candidate['session_id'])) if candidate['session_id'] else None
        supplier_id = session.supplier_id if session else await db.scalar(text('SELECT id FROM suppliers WHERE code=:code'), {'code': candidate['supplier']})
        linked = [row for row in invoices if row['supplier_id'] == supplier_id and
                  (candidate['invoice_no'] in _keys(row) or session and row.get('receiving_session_id') == session.id)]
        dates = {row['invoice_date'] for row in linked if row['invoice_date']}
        if session and session.invoice_date:
            dates.add(session.invoice_date)
        candidate['invoice_date'] = next(iter(dates)).isoformat() if len(dates) == 1 else None
        candidate['blocked'] = bool(session and (receiving_operations.archived(session) or session.status == ReceivingStatus.cancelled)
                                    or any(row['archived_at'] or row['processing_status'] == 'processed_without_stock' for row in linked))
        candidate['blocked_reason'] = None
        # Archival belongs to one exact reconciliation source, never all of its
        # document aliases (a distinct active source may share such an alias).
        source_key = candidate['invoice_no']
        if (supplier_id, source_key) in retired:
            candidate['blocked'], candidate['blocked_reason'] = True, 'invoice_source_archived'
        if session and any(line.received_qty > posted[line.id] for line in _physical_lines(session)):
            candidate['blocked'] = True
            candidate['blocked_reason'] = 'mixed_receiving_legacy_pending'
        candidate['warehouse_id'] = session.warehouse_id if session else None
        by_id = {line.id: line for line in session.lines} if session else {}
        for line in candidate['lines']:
            persisted = by_id.get(line['id'])
            line['unit_price'] = number(persisted.unit_price) if persisted and persisted.unit_price is not None else None
            line['remaining_qty'] = number(max(Decimal('0'), Decimal(line['ordered_qty']) - Decimal(line['received_qty']) - reserved.get(line['id'], Decimal('0'))))
    candidates.sort(key=lambda c: (c['invoice_date'] or '', c['supplier'], c['invoice_no']), reverse=True)
    return candidates


async def _allocate(db, batch, request, *, exclude_scan=None):
    from inventory_hub.routers import receiving_db as receiving
    candidates = await _candidates(db, request.code.strip(), exclude_scan=exclude_scan)
    eligible = [c for c in candidates if not c['blocked'] and any(Decimal(line['remaining_qty']) > 0 for line in c['lines'])]
    if request.supplier or request.invoice_no:
        eligible = [c for c in eligible if c['supplier'] == request.supplier and c['invoice_no'] == request.invoice_no]
        if not eligible:
            blocked = next((c for c in candidates if c['supplier'] == request.supplier and c['invoice_no'] == request.invoice_no), None)
            reason = blocked.get('blocked_reason') if blocked else None
            fail(reason or 'mixed_receiving_invoice_unavailable', candidates=candidates)
    elif not eligible:
        fail('mixed_receiving_legacy_pending' if any(c.get('blocked_reason') == 'mixed_receiving_legacy_pending' for c in candidates)
             else 'mixed_receiving_invoice_missing', candidates=candidates)
    elif any(c.get('unreadable_invoices') for c in candidates):
        fail('mixed_receiving_invoice_choice', candidates=eligible,
             unreadable_invoices=max(c.get('unreadable_invoices', 0) for c in candidates))
    elif len(eligible) > 1 and (any(not c['invoice_date'] for c in eligible)
                               or eligible[0]['invoice_date'] == eligible[1]['invoice_date']):
        fail('mixed_receiving_invoice_choice', candidates=eligible)
    chosen = eligible[0]
    if chosen['warehouse_id'] is not None and chosen['warehouse_id'] != batch['warehouse_id']:
        fail('mixed_receiving_warehouse_mismatch', candidates=eligible)
    if chosen['session_id'] is None:
        warehouse_code = await db.scalar(text('SELECT code FROM warehouses WHERE id=:id'), {'id': batch['warehouse_id']})
        created = await receiving.create_session(chosen['supplier'], receiving.CreateSessionRequest(
            invoice_id=chosen['invoice_no'], warehouse_code=warehouse_code), db)
        chosen['session_id'] = str(created['session_id'])
    session = (await _sessions(db, {int(chosen['session_id'])}, lock=True))[int(chosen['session_id'])]
    if session.invoice_date is None and chosen['invoice_date']:
        session.invoice_date = date.fromisoformat(chosen['invoice_date'])
    await receiving._require_invoice_receiving(db, session.supplier_id, session.invoice_number, session.id)
    if receiving_operations.archived(session) or session.status == ReceivingStatus.cancelled:
        fail('mixed_receiving_invoice_unavailable')
    posted = await posted_quantities(db, {session.id: session})
    if any(line.received_qty > posted[line.id] for line in _physical_lines(session)):
        fail('mixed_receiving_legacy_pending', session_id=session.id, supplier=session.supplier.code,
             invoice_no=session.invoice_number)
    # Re-read capacity under the same supplier/receipt lock as ordinary scans.
    reserved = await _reserved(db, exclude_scan=exclude_scan)
    prefix = receiving._product_code_prefix(chosen['supplier'])
    matching_numbers = {line['line_number'] for line in chosen['lines']}
    lines = [line for line in session.lines if line.line_number in matching_numbers and
             line.ordered_qty - line.received_qty - reserved.get(line.id, Decimal('0')) > 0]
    if request.line_id is not None:
        lines = [line for line in lines if line.id == request.line_id]
    if not lines:
        fail('mixed_receiving_quantity_exceeded', candidates=candidates)
    if len(lines) > 1:
        # Never choose between discounted/bonus rows by their position.
        fail('mixed_receiving_line_choice', candidates=await _candidates(db, request.code.strip(), exclude_scan=exclude_scan))
    line = lines[0]
    if request.qty > line.ordered_qty - line.received_qty - reserved.get(line.id, Decimal('0')):
        fail('mixed_receiving_quantity_exceeded', candidates=candidates)
    from inventory_hub.services.receiving_scan_identity import match_scan
    matched, product = await match_scan(db, session, request.code.strip(), prefix,
                                      line_id=line.id, resolve_line=receiving._resolve_line)
    if matched is None:
        fail('scan_line_mismatch')
    if product is not None and product.item_type != 'stock':
        fail('stock_item_required')
    return session, line, candidates


async def scan(db, batch_id, request, *, actor='receiving'):
    payload_hash, previous = await _request(db, batch_id, request, 'scan')
    if previous:
        return previous
    batch = await _batch(db, batch_id, lock=True)
    if batch['status'] != 'open' or batch['archived_at']:
        fail('mixed_receiving_batch_closed')
    session, line, candidates = await _allocate(db, batch, request)
    await db.execute(text('''INSERT INTO mixed_receiving_scans(id,batch_id,code,quantity,session_id,line_id,posting_revision,assignment_history,candidates)
        VALUES(:id,:batch,:code,:qty,:session,:line,:revision,CAST(:history AS jsonb),CAST(:candidates AS jsonb))'''),
        {'id': request.request_id, 'batch': batch_id, 'code': request.code.strip(), 'qty': request.qty,
         'session': session.id, 'line': line.id, 'revision': receiving_operations.posting_revision(session),
         'candidates': encoded(candidates), 'history': encoded([{'action': 'scan', 'actor': actor, 'session_id': session.id, 'line_id': line.id,
                              'at': datetime.now(timezone.utc).isoformat()}])})
    await _set_pending(db, {session.id: session})
    result = {'request_id': str(request.request_id), 'scan_id': str(request.request_id), 'replayed': False,
              'candidates': candidates, 'summary': await summary(db, batch_id)}
    return await _save(db, batch_id, request.request_id, payload_hash, result)


async def reassign(db, batch_id, scan_id, request, *, actor='receiving'):
    return await _change_scan(db, batch_id, scan_id, request, actor=actor, cancel_scan=False)


async def cancel(db, batch_id, scan_id, request, *, actor='receiving'):
    return await _change_scan(db, batch_id, scan_id, request, actor=actor, cancel_scan=True)


async def _change_scan(db, batch_id, scan_id, request, *, actor, cancel_scan):
    kind = 'cancel' if cancel_scan else 'reassign'
    payload_hash, previous = await _request(db, batch_id, request, kind, str(scan_id))
    if previous:
        return previous
    batch = await _batch(db, batch_id, lock=True)
    if batch['status'] != 'open' or batch['archived_at']:
        fail('mixed_receiving_batch_closed')
    existing = next((s for s in await _scans(db, batch_id) if str(s['id']) == str(scan_id) and s['status'] == 'active'), None)
    if existing is None:
        manual = next((s for s in await _manual_scans(db, batch_id) if str(s['id']) == str(scan_id) and s['status'] == 'active'), None)
        if manual is None or not cancel_scan:
            fail('mixed_receiving_scan_not_found')
        history = [*manual['assignment_history'], {'action': 'cancel', 'actor': actor,
            'request_id': str(request.request_id), 'at': datetime.now(timezone.utc).isoformat()}]
        await db.execute(text('''UPDATE mixed_receiving_manual_scans SET status='cancelled',
            assignment_history=CAST(:history AS jsonb) WHERE id=:id'''), {'history': encoded(history), 'id': scan_id})
        result = {'request_id': str(request.request_id), 'scan_id': str(scan_id), 'replayed': False,
                  'summary': await summary(db, batch_id)}
        return await _save(db, batch_id, request.request_id, payload_hash, result)
    # Lock all suppliers in one order before selecting the replacement.
    candidate_suppliers = (await db.execute(text('SELECT id FROM suppliers ORDER BY id'))).scalars().all()
    for supplier_id in candidate_suppliers:
        await receiving_source.lock_supplier(db, supplier_id)
    sessions = await _sessions(db, {existing['session_id']}, lock=True)
    target = existing
    if not cancel_scan:
        session, line, candidates = await _allocate(db, batch, BatchScan(request_id=request.request_id,
            code=existing['code'], qty=existing['quantity'], supplier=request.supplier,
            invoice_no=request.invoice_no, line_id=request.line_id), exclude_scan=scan_id)
        sessions[session.id] = session
        target = {**existing, 'session_id': session.id, 'line_id': line.id,
                  'posting_revision': receiving_operations.posting_revision(session), 'candidates': candidates}
    history = [*existing['assignment_history'], {'action': kind, 'actor': actor, 'request_id': str(request.request_id),
               'session_id': target['session_id'], 'line_id': target['line_id'], 'at': datetime.now(timezone.utc).isoformat()}]
    await db.execute(text('''UPDATE mixed_receiving_scans SET session_id=:session,line_id=:line,
        posting_revision=:revision,status=:status,assignment_history=CAST(:history AS jsonb),candidates=CAST(:candidates AS jsonb) WHERE id=:id'''),
        {'session': target['session_id'], 'line': target['line_id'], 'revision': target['posting_revision'],
         'status': 'cancelled' if cancel_scan else 'active', 'history': encoded(history), 'candidates': encoded(target['candidates']), 'id': scan_id})
    await _set_pending(db, sessions)
    result = {'request_id': str(request.request_id), 'scan_id': str(scan_id), 'replayed': False,
              'summary': await summary(db, batch_id)}
    return await _save(db, batch_id, request.request_id, payload_hash, result)


async def posted_quantities(db, sessions):
    """Only ledger evidence is posted; old unfinished scan quantities are not."""
    keys = {receiving_operations.movement_key(session, line.id): (line.id, line.purchase_to_stock_factor)
            for session in sessions.values() for line in session.lines}
    quantities = defaultdict(lambda: Decimal('0'))
    if keys:
        movements = await db.scalars(select(StockMovement).where(StockMovement.idempotency_key.in_(keys)))
        for movement in movements:
            line_id, factor = keys[movement.idempotency_key]
            quantities[line_id] += movement.quantity / Decimal(factor or 1)
    rows = (await db.execute(text('''SELECT line_id,session_id,posting_revision,purchase_quantity
        FROM mixed_receiving_postings WHERE session_id=ANY(CAST(:ids AS bigint[]))'''),
        {'ids': sorted(sessions)})).mappings() if sessions else []
    for row in rows:
        if row['posting_revision'] == receiving_operations.posting_revision(sessions[row['session_id']]):
            quantities[row['line_id']] += row['purchase_quantity']
    return quantities


async def _shop_status(db, product_id, warehouse_id):
    from inventory_hub.services import stock_sync
    rows = (await db.execute(text('''SELECT s.id,s.code,s.name,
        EXISTS(SELECT 1 FROM shop_products sp WHERE sp.shop_id=s.id AND sp.product_id=:product
            AND sp.retired_at IS NULL AND sp.is_listed) AS linked,
        COALESCE(p.enabled AND p.authorized AND p.protocol='delta_v1',false) AS sync_enabled,
        d.generation,d.acknowledged_generation,d.run_id,d.last_error
        FROM shops s LEFT JOIN stock_sync_settings p ON p.shop_id=s.id AND p.warehouse_id=:warehouse
        LEFT JOIN stock_sync_dirty d ON d.shop_id=s.id AND d.product_id=:product AND d.warehouse_id=:warehouse
        WHERE s.is_active AND s.platform='upgates' ORDER BY s.code'''),
        {'product': product_id, 'warehouse': warehouse_id})).mappings()
    result = []
    for row in rows:
        enabled = row['sync_enabled'] and stock_sync.enabled()
        status = ('missing' if not row['linked'] else 'disabled' if not enabled else
                  'needs_attention' if row['last_error'] else 'pending' if row['run_id'] or
                  row['generation'] is not None and row['generation'] > row['acknowledged_generation'] else
                  'verified' if row['generation'] is not None and row['acknowledged_generation'] == row['generation'] else 'ready')
        result.append({'code': row['code'], 'name': row['name'], 'linked': row['linked'],
                       'sync_enabled': enabled, 'status': status, 'run_id': row['run_id'], 'error': row['last_error']})
    return result


async def _manual_summary(db, batch, scans):
    items, blockers, rendered = [], [], []
    for row in scans:
        product = await db.get(Product, row['product_id'])
        errors = []
        if row['status'] == 'active':
            try:
                product = await _manual_product(db, row['code'], row['product_id'])
                if row['stock_unit'] != product.stock_unit:
                    fail('receiving_unit_changed')
                validate_stock_quantity(row['quantity'], product.stock_unit)
            except HTTPException as error:
                errors.append(error.detail['code'])
            except ItemUnitError as error:
                errors.append(error.code)
        scan = {'id': str(row['id']), 'source_kind': 'manual', 'code': row['code'],
            'created_at': row['created_at'].isoformat(),
            'qty': number(row['quantity']), 'supplier': None, 'supplier_name': None,
            'invoice_no': None, 'invoice_date': None, 'session_id': None, 'line_id': None,
            'line_number': None, 'title': product.name if product else '',
            'product_id': row['product_id'], 'sku': product.sku if product else None,
            'unit_price': number(row['unit_cost']) if row['unit_cost'] is not None else None,
            'cost_status': row['cost_status'], 'manual_reason': row['reason'],
            'status': row['status'], 'alternatives': []}
        rendered.append(scan)
        if row['status'] != 'active':
            continue
        stock = (await db.execute(text('''SELECT b.qty_on_hand,b.qty_available,
            t.product_id IS NOT NULL AS confirmed FROM products p
            LEFT JOIN stock_balances b ON b.product_id=p.id AND b.warehouse_id=:warehouse
            LEFT JOIN stock_tracking t ON t.product_id=p.id AND t.warehouse_id=:warehouse
            WHERE p.id=:product'''), {'warehouse': batch['warehouse_id'], 'product': row['product_id']})).mappings().first()
        confirmed = bool(stock and stock['confirmed'])
        before = stock['qty_on_hand'] if confirmed else None
        available = stock['qty_available'] if confirmed else None
        items.append({**scan, 'scan_id': str(row['id']), 'card_status': 'matched', 'can_prepare': False,
            'draft_id': None, 'warnings': ['receiving_without_invoice'],
            'value': number(row['quantity'] * row['unit_cost']) if row['unit_cost'] is not None else None,
            'purchase_unit': row['stock_unit'], 'purchase_to_stock_factor': '1',
            'stock_quantity': number(row['quantity']), 'stock_unit': row['stock_unit'],
            'stock_confirmed': confirmed, 'stock_qty_before': number(before) if before is not None else None,
            'available_before': number(available) if available is not None else None,
            'stock_qty_after': number((before or 0) + row['quantity']),
            'available_after': number((available or 0) + row['quantity']), 'posted_qty': '0',
            'shops': await _shop_status(db, row['product_id'], batch['warehouse_id'])})
        blockers.extend({'code': error, 'scan_id': str(row['id']), 'session_id': None, 'line_id': None}
                        for error in errors)
    return items, blockers, rendered


async def summary(db, batch_id):
    from inventory_hub.routers import receiving_db as receiving
    from inventory_hub.services.receiving_products import inspect_products
    from inventory_hub.services.catalog import CatalogError
    batch = await _batch(db, batch_id)
    scans = await _scans(db, batch_id)
    manual_scans = await _manual_scans(db, batch_id)
    sessions = await _sessions(db, {row['session_id'] for row in scans})
    warehouse = await db.get(Warehouse, batch['warehouse_id'])
    posted = await posted_quantities(db, sessions)
    by_line = {(session.id, line.id): line for session in sessions.values() for line in session.lines}
    quantities = defaultdict(lambda: Decimal('0'))
    for row in scans:
        if row['status'] == 'active':
            quantities[(row['session_id'], row['line_id'])] += row['quantity']
    items, blockers, invoices, rendered_scans = [], [], [], []
    reports = {}
    for session in sessions.values():
        try:
            report, _ = await inspect_products(db, session, receiving._product_code_prefix(session.supplier.code))
            reports[session.id] = {row['line_id']: row for row in report['items']}
        except (CatalogError, ValueError, OSError):
            reports[session.id] = {}
    for (session_id, line_id), quantity in quantities.items():
        session, line = sessions[session_id], by_line[(session_id, line_id)]
        card = reports[session_id].get(line_id, {})
        product = await db.get(Product, card.get('product_id') or line.product_id) if card.get('product_id') or line.product_id else None
        item_errors = []
        if card.get('status') == 'ambiguous':
            item_errors.append('receiving_identity_conflict')
        elif product is None:
            item_errors.append('receiving_product_preparation_required')
        elif product.item_type != 'stock':
            item_errors.append('stock_item_required')
        if line.unit_price is None or not line.unit_price.is_finite() or line.unit_price < 0:
            item_errors.append('receiving_purchase_price_missing')
        if receiving_operations.archived(session):
            item_errors.append('receiving_archived')
        if any(row['posting_revision'] != receiving_operations.posting_revision(session)
               for row in scans if row['status'] == 'active' and row['line_id'] == line.id):
            item_errors.append('receiving_posting_revision_changed')
        stock_quantity = None
        if product and product.item_type == 'stock':
            try:
                stock_quantity, _ = receipt_values(quantity, line.unit_price, line.purchase_to_stock_factor)
                validate_stock_quantity(stock_quantity, product.stock_unit)
                if line.conversion_stock_unit is not None and line.conversion_stock_unit != product.stock_unit:
                    raise ItemUnitError('receiving_unit_changed')
            except ItemUnitError as error:
                item_errors.append(error.code)
        stock = (await db.execute(text('''SELECT b.qty_on_hand,b.qty_available,
            t.product_id IS NOT NULL AS confirmed FROM products p
            LEFT JOIN stock_balances b ON b.product_id=p.id AND b.warehouse_id=:warehouse
            LEFT JOIN stock_tracking t ON t.product_id=p.id AND t.warehouse_id=:warehouse
            WHERE p.id=:product'''), {'warehouse': batch['warehouse_id'], 'product': product.id})).mappings().first() if product else None
        confirmed = bool(stock and stock['confirmed'])
        before = stock['qty_on_hand'] if confirmed else None
        available_before = stock['qty_available'] if confirmed else None
        item = {'source_kind': 'invoice', 'session_id': session_id, 'supplier': session.supplier.code, 'invoice_no': session.invoice_number,
            'line_id': line_id, 'title': line.description or '', 'qty': number(quantity),
            'product_id': product.id if product else None, 'sku': product.sku if product else None,
            'card_status': card.get('status', 'matched' if product else 'not_found'),
            'can_prepare': card.get('can_prepare', product is None), 'draft_id': card.get('draft_id'),
            'warnings': card.get('warnings', []), 'unit_price': number(line.unit_price) if line.unit_price is not None else None,
            'value': number(quantity * line.unit_price) if line.unit_price is not None else None,
            'purchase_unit': line.purchase_unit, 'purchase_to_stock_factor': number(line.purchase_to_stock_factor),
            'stock_quantity': number(stock_quantity) if stock_quantity is not None else None,
            'stock_unit': product.stock_unit if product else None, 'stock_confirmed': confirmed,
            'stock_qty_before': number(before) if before is not None else None,
            'stock_qty_after': number((before or 0) + stock_quantity) if stock_quantity is not None else None,
            'available_before': number(available_before) if available_before is not None else None,
            'available_after': number((available_before or 0) + stock_quantity) if stock_quantity is not None else None,
            'posted_qty': number(posted[line_id]),
            'shops': await _shop_status(db, product.id if product else None, batch['warehouse_id'])}
        items.append(item)
        blockers.extend({'code': code, 'session_id': session_id, 'line_id': line_id} for code in item_errors)
    manual_items, manual_blockers, manual_rendered = await _manual_summary(db, batch, manual_scans)
    items.extend(manual_items)
    blockers.extend(manual_blockers)
    product_increments = defaultdict(lambda: Decimal('0'))
    for item in items:
        if item['product_id'] is not None and item['stock_quantity'] is not None:
            product_increments[item['product_id']] += Decimal(item['stock_quantity'])
    for item in items:
        if item['product_id'] in product_increments:
            increment = product_increments[item['product_id']]
            item['stock_qty_after'] = number(Decimal(item['stock_qty_before'] or 0) + increment)
            item['available_after'] = number(Decimal(item['available_before'] or 0) + increment)
    if batch['status'] == 'completed' and batch['result']:
        saved = {(item['session_id'], item['line_id'], item.get('scan_id')): item for item in batch['result']['items']}
        items = [{**saved.get((item['session_id'], item['line_id'], item.get('scan_id')), item), 'shops': item['shops']} for item in items]
        blockers = []
    for session in sessions.values():
        invoice_scanned = sum((quantity for (sid, _), quantity in quantities.items() if sid == session.id), Decimal('0'))
        complete = all(posted[line.id] + (quantities.get((session.id, line.id), Decimal('0')) if batch['status'] == 'open' else 0)
                       >= line.ordered_qty for line in _physical_lines(session))
        invoices.append({'session_id': session.id, 'supplier': session.supplier.code,
            'supplier_name': session.supplier.name, 'invoice_no': session.invoice_number,
            'invoice_date': session.invoice_date.isoformat() if session.invoice_date else None,
            'ordered_qty': number(sum((line.ordered_qty for line in session.lines), Decimal('0'))),
            'service_qty': number(sum((line.ordered_qty for line in session.lines if line not in _physical_lines(session)), Decimal('0'))),
            'received_qty': number(sum((posted[line.id] for line in session.lines), Decimal('0'))),
            'unposted_legacy_qty': number(sum((max(Decimal('0'), line.received_qty - posted[line.id]) for line in session.lines), Decimal('0'))),
            'scanned_qty': number(invoice_scanned), 'complete_after': complete})
    for row in scans:
        session, line = sessions[row['session_id']], by_line[(row['session_id'], row['line_id'])]
        rendered_scans.append({'id': str(row['id']), 'source_kind': 'invoice', 'code': row['code'], 'qty': number(row['quantity']),
            'created_at': row['created_at'].isoformat(),
            'supplier': session.supplier.code, 'supplier_name': session.supplier.name,
            'invoice_no': session.invoice_number, 'invoice_date': session.invoice_date.isoformat() if session.invoice_date else None,
            'session_id': session.id, 'line_id': line.id, 'line_number': line.line_number,
            'title': line.description or '', 'product_id': line.product_id,
            'sku': line.product.sku if line.__dict__.get('product') else None,
            'unit_price': number(line.unit_price) if line.unit_price is not None else None,
            'status': row['status'], 'alternatives': row['candidates']})
    rendered_scans.extend(manual_rendered)
    rendered_scans.sort(key=lambda row: (row['created_at'], row['id']))
    result = {'id': str(batch_id), 'status': batch['status'], 'archived': bool(batch['archived_at']), 'warehouse_code': warehouse.code,
        'warehouse_name': warehouse.name, 'created_at': batch['created_at'].isoformat(),
        'completed_at': batch['completed_at'].isoformat() if batch['completed_at'] else None,
        'scans': rendered_scans, 'items': items, 'invoices': invoices,
        'total_qty': number(sum(quantities.values(), Decimal('0')) + sum(
            (row['quantity'] for row in manual_scans if row['status'] == 'active'), Decimal('0'))),
        'blockers': blockers, 'result': batch['result']}
    # Transient worker outcomes are informative; local inventory/identity and
    # configured destinations form the reviewed physical posting contract.
    result['preview_hash'] = digest({'batch': str(batch_id), 'status': batch['status'], 'archived': bool(batch['archived_at']),
        'scans': [{key: row[key] for key in ('id', 'code', 'quantity', 'session_id', 'line_id', 'posting_revision', 'status')} for row in scans],
        'manual_scans': [{key: row[key] for key in ('id', 'code', 'product_id', 'quantity', 'unit_cost',
            'cost_status', 'stock_unit', 'reason', 'status')} for row in manual_scans],
        'sessions': [{'id': session.id, 'revision': receiving_operations.posting_revision(session),
            'lines': [{'id': line.id, 'product_id': reports[session.id].get(line.id, {}).get('product_id') or line.product_id, 'received': line.received_qty,
                'ordered': line.ordered_qty, 'price': line.unit_price, 'factor': line.purchase_to_stock_factor}
                      for line in session.lines]} for session in sessions.values()],
        'items': [{key: value for key, value in item.items() if key not in ('shops', 'warnings')} for item in items],
        'blockers': blockers})
    return result


async def preview(db, batch_id):
    return await summary(db, batch_id)


async def finalize(db, batch_id, request, *, actor='receiving'):
    from inventory_hub.routers import receiving_db as receiving
    from inventory_hub.services.receiving_products import reconcile_products
    payload_hash, previous = await _request(db, batch_id, request, 'finalize')
    if previous:
        return previous
    batch = await _batch(db, batch_id, lock=True)
    if batch['status'] != 'open' or batch['archived_at']:
        fail('mixed_receiving_batch_closed')
    scans = [row for row in await _scans(db, batch_id) if row['status'] == 'active']
    manual_scans = [row for row in await _manual_scans(db, batch_id) if row['status'] == 'active']
    if not scans and not manual_scans:
        fail('mixed_receiving_empty')
    sessions = await _sessions(db, {row['session_id'] for row in scans}, lock=True)
    current = await summary(db, batch_id)
    if current['preview_hash'] != request.preview_hash:
        fail('mixed_receiving_preview_changed')
    if current['blockers']:
        fail('mixed_receiving_blocked', blockers=current['blockers'])
    quantities = defaultdict(lambda: Decimal('0'))
    by_line = {(session.id, line.id): line for session in sessions.values() for line in session.lines}
    for row in scans:
        session = sessions[row['session_id']]
        if row['posting_revision'] != receiving_operations.posting_revision(session):
            fail('receiving_posting_revision_changed')
        quantities[(row['session_id'], row['line_id'])] += row['quantity']
    for session in sessions.values():
        await receiving._require_invoice_receiving(db, session.supplier_id, session.invoice_number, session.id)
        if receiving_operations.archived(session) or session.status == ReceivingStatus.cancelled:
            fail('mixed_receiving_invoice_unavailable')
        try:
            await receiving_source.validate_session(db, session, session.supplier.code,
                receiving.settings.INVENTORY_DATA_ROOT, receiving._parse_invoice_csv)
        except receiving_source.ReceivingSourceError as error:
            raise HTTPException(409, detail=error.detail) from None
    await db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': IDENTITY_WRITE_LOCK})
    # Shared editor writers hold identity before warehouse admission. Preserve
    # that order while the dictionary may be waiting for warehouse UPDATE.
    warehouse = await db.scalar(select(Warehouse).where(Warehouse.id == batch['warehouse_id'])
                                .with_for_update(read=True).execution_options(populate_existing=True))
    if warehouse is None or not warehouse.is_active:
        fail('mixed_receiving_warehouse_missing')
    resolved = []
    for session in sessions.values():
        await reconcile_products(db, session, receiving._product_code_prefix(session.supplier.code))
    for (session_id, line_id), quantity in quantities.items():
        session, line = sessions[session_id], by_line[(session_id, line_id)]
        if quantity > line.ordered_qty - line.received_qty:
            fail('mixed_receiving_quantity_exceeded')
        product, _, _ = await receiving._ensure_product_for_line(db, session.supplier, line,
            receiving._product_code_prefix(session.supplier.code), session.invoice_number,
            receiving.ProductIdentifierService(db))
        if product is None or product.item_type != 'stock':
            fail('receiving_product_preparation_required', line_id=line_id)
        if line.unit_price is None or not line.unit_price.is_finite() or line.unit_price < 0:
            fail('receiving_purchase_price_missing', line_id=line_id)
        resolved.append((session, line, quantity, product))
    manual_products = {}
    for row in manual_scans:
        product = await _manual_product(db, row['code'], row['product_id'])
        if product.stock_unit != row['stock_unit']:
            fail('receiving_unit_changed')
        try:
            validate_stock_quantity(row['quantity'], product.stock_unit)
        except ItemUnitError as error:
            fail(error.code)
        manual_products[product.id] = product
    product_ids = {product.id for _, _, _, product in resolved} | set(manual_products)
    balances = await receiving._lock_stock_balances(db, batch['warehouse_id'], product_ids)
    # Recheck reviewed physical balances under locks; an intervening sale needs
    # a new summary, never an outdated projection presented as current stock.
    locked_summary = await summary(db, batch_id)
    if locked_summary['preview_hash'] != request.preview_hash:
        fail('mixed_receiving_preview_changed')
    movement_ids = []
    for session, line, quantity, product in resolved:
        revision = receiving_operations.posting_revision(session)
        movement = await write_stock_for_line(db, session, line, product, session.supplier.code,
            balances[product.id], operator_name=actor, purchase_quantity=quantity,
            idempotency_key=f'mixed-receiving:{batch_id}:{line.id}:r{revision}')
        movement_ids.append(movement.id)
        await db.execute(text('''INSERT INTO mixed_receiving_postings
            (batch_id,session_id,line_id,posting_revision,purchase_quantity,movement_id)
            VALUES(:batch,:session,:line,:revision,:qty,:movement)'''),
            {'batch': batch_id, 'session': session.id, 'line': line.id, 'revision': revision,
             'qty': quantity, 'movement': movement.id})
        line.received_qty += quantity
        line.status = receiving._status_for(line.received_qty, line.ordered_qty)
        line.product_id = product.id
    from inventory_hub.services import fifo
    for row in manual_scans:
        try:
            posted_manual = await fifo.write_documented_receipt(db, balances[row['product_id']],
                {'quantity': number(row['quantity']), 'unit_cost': number(row['unit_cost']) if row['unit_cost'] is not None else None,
                 'cost_status': row['cost_status'], 'physical_received_at': row['created_at'].isoformat(),
                 'source_reference': row['reason'], 'operator_name': actor},
                reference_type='mixed_receiving_manual', reference_id=str(row['id']),
                idempotency_key=f'mixed-receiving-manual:{row["id"]}')
        except fifo.FifoError as error:
            raise HTTPException(error.status, detail={'code': error.code}) from None
        movement_ids.append(posted_manual['movement_id'])
        await db.execute(text('''INSERT INTO mixed_receiving_manual_postings(scan_id,movement_id)
            VALUES(:scan,:movement)'''), {'scan': row['id'], 'movement': posted_manual['movement_id']})
    for row in scans:
        line = by_line[(row['session_id'], row['line_id'])]
        db.add(ScanEvent(session_type=ScanSessionType.receiving, receiving_session_id=row['session_id'],
            receiving_line_id=row['line_id'], scanned_code=row['code'],
            scanned_code_type=receiving.ProductIdentifierService(db).classify_barcode(row['code']),
            product_id=line.product_id, match_method='mixed_receiving', quantity=row['quantity'],
            status=ScanStatus.active, scanned_by=actor))
    await db.flush()
    stamp = datetime.now(timezone.utc)
    result = {'batch_id': str(batch_id), 'warehouse_code': current['warehouse_code'],
        'product_ids': sorted(product_ids),
        'movement_ids': movement_ids, 'completed_at': stamp.isoformat(),
        'items': current['items'], 'invoices': [], 'total_qty': current['total_qty']}
    await db.execute(text("UPDATE mixed_receiving_batches SET status='completed',completed_at=:at WHERE id=:id"),
                     {'at': stamp, 'id': batch_id})
    posted = await posted_quantities(db, sessions)
    for session in sessions.values():
        data = dict(session.session_data or {})
        data.pop('receiving_reversed', None)
        data['mixed_receiving'] = True
        data.pop('mixed_receiving_previous_status', None)
        session.session_data = data
        complete = all(posted[line.id] >= line.ordered_qty for line in _physical_lines(session))
        session.status = ReceivingStatus.completed if complete else ReceivingStatus.in_progress
        session.finished_at = stamp if complete else None
        if complete:
            skipped = [{'line_number': line.line_number, 'product_id': line.product_id, 'reason': 'service_outside_stock'}
                       for line in session.lines if line not in _physical_lines(session)]
            count = sum(1 for posted_session, _, _, _ in resolved if posted_session.id == session.id)
            finalized = await receiving._finalize_result(db, session, count, 0, skipped)
            session.session_data = {**session.session_data, 'finalize_result': finalized}
        else:
            session.session_data = {key: value for key, value in session.session_data.items() if key != 'finalize_result'}
        result['invoices'].append({'supplier': session.supplier.code, 'invoice_no': session.invoice_number,
                                   'session_id': session.id, 'completed': complete})
    await _set_pending(db, sessions)
    await db.execute(text('UPDATE mixed_receiving_batches SET result=CAST(:result AS jsonb) WHERE id=:id'),
                     {'result': encoded(result), 'id': batch_id})
    response = {'request_id': str(request.request_id), 'batch_id': str(batch_id), 'status': 'completed',
                'replayed': False, 'result': result, 'summary': await summary(db, batch_id)}
    await _save(db, batch_id, request.request_id, payload_hash, response)
    await db.commit()
    for session in sessions.values():
        if session.status == ReceivingStatus.completed:
            receiving._sync_finalized_invoice(session.supplier.code, session.session_data['finalize_result'])
    return response


async def get_export_scope(db, batch_id):
    batch = await _batch(db, batch_id)
    if batch['status'] != 'completed' or not batch['result']:
        fail('mixed_receiving_not_completed')
    return {key: batch['result'][key] for key in ('product_ids', 'warehouse_code')}
