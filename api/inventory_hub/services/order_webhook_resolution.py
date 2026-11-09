"""Explicit recovery of an absent, never-managed DELETE notification.

The generic sync journal stores a prepared snapshot and a separate completed
decision. Neither audit row is updated or deleted. This is not an order, a
native-stock fact, a collection checkpoint, or a stock movement.
"""
from copy import deepcopy
from datetime import datetime, timedelta
import asyncio
import hashlib
import json

from sqlalchemy import String, cast, func, or_, select, text

from inventory_hub.db_models import Shop
from inventory_hub.db_models_ext import ShopOrder, StockMovement, SyncLog
from inventory_hub.order_collection_models import OrderCollectionSettings, OrderInboxEntry
from inventory_hub.order_processing_models import OrderProcessingJob
from inventory_hub.order_stock_models import OrderStockPolicy, OrderStockPreview
from inventory_hub.order_webhook_models import OrderWebhookEvent, OrderWebhookSettings
from inventory_hub.stock_native_models import StockNativeEvent, StockNativeOrder
from inventory_hub.services import order_collection as collection, order_webhooks as webhooks
from inventory_hub.services.order_collection_source import CollectionSourceError


NAMESPACE = 'order_webhook_resolution'
EXPIRY_MINUTES = 15
HISTORY_LIMIT = 50
ACKNOWLEDGE = 'acknowledge_absent_delete'


def _error(code, status=409):
    return webhooks.WebhookError('order_webhook_resolution_' + code, status)


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':'), default=str).encode()).hexdigest()


def _iso(value):
    return value.isoformat() if value is not None else None


def _event(row):
    return {'event': row.event, 'generation': row.generation, 'notification_hash': row.notification_hash,
        'notification_at': _iso(row.notification_at), 'status': row.status, 'last_error': row.last_error,
        'last_received_at': _iso(row.last_received_at), 'attempts': row.attempts,
        'last_completed_at': _iso(row.last_completed_at)}


async def _request_lock(db, request_id):
    # A global UUID lock also rejects reuse for another shop/payload. The same
    # lock is reacquired after network reads; no DB lock spans those reads.
    await db.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))'),
                     {'key': NAMESPACE + ':' + str(request_id)})


def _journal():
    return select(SyncLog).where(SyncLog.sync_type == NAMESPACE, SyncLog.direction == 'inbound',
                                 SyncLog.target_type == 'shop')


async def _stored(db, request_id):
    return await db.scalar(_journal().where(SyncLog.metadata_['request_id'].astext == str(request_id))
        .order_by(SyncLog.id.desc()).limit(1).execution_options(populate_existing=True))


async def _latest(db, shop_code, order_number):
    return await db.scalar(_journal().where(SyncLog.target_code == shop_code, SyncLog.status == 'completed',
        SyncLog.metadata_['operation']['order_number'].astext == order_number)
        .order_by(SyncLog.id.desc()).limit(1).execution_options(populate_existing=True))


def _operation(row):
    return deepcopy(row.metadata_['operation'])


async def _no_local_history(db, shop, number):
    # Reject even an unmanaged matching order: there is no reason to infer its
    # stock meaning here. Child/native history keeps its real order FK.
    matches = (ShopOrder.shop_id == shop.id, or_(ShopOrder.external_id == number,
        ShopOrder.external_code == number, ShopOrder.stock_snapshot['order_number'].astext == number))
    checks = (
        select(ShopOrder.id).where(*matches),
        select(OrderInboxEntry.id).where(OrderInboxEntry.shop_id == shop.id, OrderInboxEntry.order_number == number),
        select(OrderProcessingJob.id).where(OrderProcessingJob.shop_id == shop.id, OrderProcessingJob.order_number == number),
        select(OrderStockPreview.id).where(OrderStockPreview.shop_id == shop.id,
            OrderStockPreview.preview_data['source']['order_number'].astext == number),
        select(StockNativeOrder.order_id).join(ShopOrder, ShopOrder.id == StockNativeOrder.order_id).where(*matches),
        select(StockNativeEvent.id).join(ShopOrder, ShopOrder.id == StockNativeEvent.order_id).where(*matches),
    )
    for query in checks:
        if await db.scalar(query.limit(1)) is not None:
            raise _error('local_history')
    # Supported issues reference the immutable local order ID and shop ID. A
    # legacy/orphan reference for this shop cannot prove that it is unrelated
    # to the missing order, so keep it on review too.
    order_ids = select(cast(ShopOrder.id, String)).where(ShopOrder.shop_id == shop.id)
    movement = await db.scalar(select(StockMovement.id).where(
        StockMovement.reference_type == 'shop_order',
        StockMovement.reference_source.in_((str(shop.id), shop.code)),
        or_(StockMovement.reference_id == number, StockMovement.reference_id.is_(None),
            StockMovement.reference_id.not_in(order_ids))).limit(1))
    if movement is not None:
        raise _error('local_history')


async def _state(db, shop_code, number, action):
    # Shop FOR UPDATE serializes notifications, collection and every supported
    # local order writer (which holds at least Shop FOR SHARE). Preserve their
    # Shop -> policy -> collector -> webhook settings -> event lock order.
    shop = await db.scalar(select(Shop).where(Shop.code == shop_code, Shop.is_active.is_(True),
        Shop.platform == 'upgates').with_for_update().execution_options(populate_existing=True))
    if shop is None:
        raise _error('shop_not_found', 404)
    policy = await db.scalar(select(OrderStockPolicy).where(OrderStockPolicy.shop_id == shop.id)
        .with_for_update(read=True).execution_options(populate_existing=True))
    collector = await db.scalar(select(OrderCollectionSettings).where(OrderCollectionSettings.shop_id == shop.id)
        .with_for_update(read=True).execution_options(populate_existing=True))
    settings = await db.scalar(select(OrderWebhookSettings).where(OrderWebhookSettings.shop_id == shop.id)
        .with_for_update().execution_options(populate_existing=True))
    row = await db.scalar(select(OrderWebhookEvent).where(OrderWebhookEvent.shop_id == shop.id,
        OrderWebhookEvent.order_number == number).with_for_update().execution_options(populate_existing=True))
    if row is None:
        raise _error('event_not_found', 404)
    target = collection.target_fingerprint(shop_code)
    if (policy is None or collector is None or settings is None or not target
            or settings.target_fingerprint != target or collector.target_fingerprint != target):
        raise _error('target_changed')
    previous = await _latest(db, shop_code, number)
    if row.event != 'delete':
        raise _error('not_eligible')
    if action == ACKNOWLEDGE:
        if row.status != 'review' or row.last_error != 'order_webhook_order_not_found':
            raise _error('not_eligible')
        await _no_local_history(db, shop, number)
    else:
        old = _operation(previous) if previous else None
        if (row.status != 'completed' or old is None or old['action'] != ACKNOWLEDGE
                or old['event']['generation'] != row.generation
                or old['event']['notification_hash'] != row.notification_hash
                or old['result']['event_status'] != 'completed'):
            raise _error('not_eligible')
    frozen = {'shop_id': shop.id, 'target_fingerprint': target, 'settings_revision': settings.revision,
        'webhooks_enabled': settings.enabled, 'collector_revision': collector.revision,
        'collector_enabled': collector.enabled, 'policy_revision': policy.revision,
        'policy_starts_at': _iso(policy.starts_at), 'warehouse_id': policy.warehouse_id,
        'event': _event(row), 'last_decision_id': previous.metadata_['request_id'] if previous else None}
    return row, frozen


async def _absence(shop_code, number, frozen):
    # Reuse the bounded, authenticated, strict membership reader. For an empty
    # result it has successfully read BOTH active and deleted exact-number
    # queries. HTTP/parse/permission failures never become an empty result.
    try:
        async with asyncio.timeout(60):
            result = await webhooks.fetch_current({'shop_code': shop_code,
                'target_fingerprint': frozen['target_fingerprint'], 'events': [{'order_number': number}]})
    except (CollectionSourceError, webhooks.WebhookError):
        raise
    except Exception:
        raise _error('source_unavailable', 502) from None
    if not isinstance(result, dict):
        raise _error('source_unavailable', 502)
    if result:
        raise _error('source_exists')
    return {'kind': 'absent_delete', 'checked_at': _iso(webhooks.now()), 'active_absent': True,
            'deleted_absent': True, 'no_local_history': True}


def _reopen_evidence():
    return {'kind': 'reopen_manual_ack', 'checked_at': _iso(webhooks.now()), 'active_absent': None,
            'deleted_absent': None, 'no_local_history': None}


def _append(db, operation, input_hash, frozen, *, verification=None):
    # Separate append-only journal entries, including for reopening a previous
    # decision. No generic sync-log writer/retention endpoint exists.
    completed = operation['phase'] == 'completed'
    db.add(SyncLog(sync_type=NAMESPACE, direction='inbound', target_type='shop',
        target_code=operation['shop_code'], status=operation['phase'], items_total=1,
        items_success=1 if completed else 0, items_failed=0, items_skipped=0,
        started_at=datetime.fromisoformat(operation['created_at']),
        finished_at=webhooks.now() if completed else None,
        metadata_={'version': 1, 'request_id': operation['request_id'], 'input_hash': input_hash,
                   'state': deepcopy(frozen), 'operation': deepcopy(operation),
                   'apply_verification': deepcopy(verification)}))


async def preview(db, payload, actor):
    request_id = str(payload.request_id)
    identity = {**payload.model_dump(mode='json'), 'actor': actor}
    input_hash = _hash(identity)
    await _request_lock(db, request_id)
    existing = await _stored(db, request_id)
    if existing:
        if existing.metadata_['input_hash'] != input_hash:
            raise _error('request_conflict')
        result = _operation(existing)
        await db.commit()
        return result
    _, frozen = await _state(db, payload.shop_code, payload.order_number, payload.action)
    await db.commit()
    evidence = (await _absence(payload.shop_code, payload.order_number, frozen)
                if payload.action == ACKNOWLEDGE else _reopen_evidence())
    await _request_lock(db, request_id)
    existing = await _stored(db, request_id)
    if existing:
        if existing.metadata_['input_hash'] != input_hash:
            raise _error('request_conflict')
        result = _operation(existing)
        await db.commit()
        return result
    _, fresh = await _state(db, payload.shop_code, payload.order_number, payload.action)
    if fresh != frozen:
        raise _error('stale')
    at = webhooks.now()
    operation = {**identity, 'phase': 'prepared', 'created_at': _iso(at),
        'expires_at': _iso(at + timedelta(minutes=EXPIRY_MINUTES)), 'event': frozen['event'],
        'after_status': 'completed' if payload.action == ACKNOWLEDGE else 'review',
        'stock_effect': 'none', 'evidence': evidence, 'result': None}
    operation['preview_hash'] = _hash({'operation': operation, 'state': frozen})
    _append(db, operation, input_hash, frozen)
    await db.commit()
    return operation


def _check_apply(row, payload, actor):
    operation = _operation(row)
    if (operation['shop_code'] != payload.shop_code or operation['preview_hash'] != payload.preview_hash
            or operation['actor'] != actor):
        raise _error('request_conflict')
    if operation['phase'] != 'completed' and datetime.fromisoformat(operation['expires_at']) <= webhooks.now():
        raise _error('expired')
    return operation


async def apply(db, request_id, payload, actor):
    request_id = str(request_id)
    await _request_lock(db, request_id)
    saved = await _stored(db, request_id)
    if saved is None:
        raise _error('not_found', 404)
    operation = _check_apply(saved, payload, actor)
    if operation['phase'] == 'completed':
        await db.commit()
        return operation
    frozen, input_hash = deepcopy(saved.metadata_['state']), saved.metadata_['input_hash']
    _, current = await _state(db, operation['shop_code'], operation['order_number'], operation['action'])
    if current != frozen:
        raise _error('stale')
    await db.commit()
    evidence = (await _absence(operation['shop_code'], operation['order_number'], frozen)
                if operation['action'] == ACKNOWLEDGE else _reopen_evidence())
    await _request_lock(db, request_id)
    saved = await _stored(db, request_id)
    if saved is None:
        raise _error('not_found', 404)
    operation = _check_apply(saved, payload, actor)
    if operation['phase'] == 'completed':
        await db.commit()
        return operation
    row, current = await _state(db, operation['shop_code'], operation['order_number'], operation['action'])
    if current != frozen:
        raise _error('stale')
    at = webhooks.now()
    row.status = operation['after_status']
    if operation['action'] == ACKNOWLEDGE:
        row.last_error, row.last_completed_at = None, at
    else:
        previous = await _latest(db, operation['shop_code'], operation['order_number'])
        original = _operation(previous)['event']
        row.last_error = original['last_error']
        row.last_completed_at = datetime.fromisoformat(original['last_completed_at']) if original['last_completed_at'] else None
    operation['phase'] = 'completed'
    operation['result'] = {'applied_at': _iso(at), 'event_status': row.status, 'event_generation': row.generation}
    _append(db, operation, input_hash, frozen, verification=evidence)
    # Do not change settings.last_completed_at or the collector's cursor: a
    # human acknowledgement is not evidence that an order was collected.
    await db.commit()
    return operation


async def get_operation(db, request_id, shop_code):
    row = await _stored(db, str(request_id))
    if row is None or row.target_code != shop_code:
        raise _error('not_found', 404)
    return _operation(row)


async def history(db, shop_code, order_number):
    await collection._shop(db, shop_code)
    rows = (await db.scalars(_journal().where(SyncLog.target_code == shop_code,
        SyncLog.status == 'completed', SyncLog.metadata_['operation']['order_number'].astext == order_number)
        .order_by(SyncLog.id.desc()).limit(HISTORY_LIMIT))).all()
    return {'shop_code': shop_code, 'order_number': order_number, 'limit': HISTORY_LIMIT,
            'operations': [_operation(row) for row in rows]}


async def status_resolutions(db, shop_code, events):
    if not events:
        return {}
    number = SyncLog.metadata_['operation']['order_number'].astext
    latest = select(func.max(SyncLog.id)).where(SyncLog.sync_type == NAMESPACE,
        SyncLog.direction == 'inbound', SyncLog.target_type == 'shop', SyncLog.target_code == shop_code,
        SyncLog.status == 'completed', number.in_([row.order_number for row in events])).group_by(number)
    rows = (await db.scalars(select(SyncLog).where(SyncLog.id.in_(latest)))).all()
    current = {row.order_number: row for row in events}
    result = {}
    for saved in rows:
        operation = _operation(saved)
        event, outcome = operation['event'], operation['result']
        row = current[operation['order_number']]
        if (event['generation'] == row.generation and event['notification_hash'] == row.notification_hash
                and outcome['event_status'] == row.status):
            result[row.order_number] = {'request_id': operation['request_id'], 'action': operation['action'],
                'note': operation['note'], 'actor': operation['actor'], 'applied_at': outcome['applied_at'],
                'event_generation': outcome['event_generation']}
    return result
