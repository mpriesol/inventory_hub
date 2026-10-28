"""Authenticated hints -> durable queue -> current order metadata.

A notification never supplies quantities, UUID identity, or collection coverage.
Only current upstream reads may update the shared inbox; its existing processor
continues to own every reservation and physical stock operation.
"""
import asyncio
import base64
import binascii
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import secrets
from uuid import uuid4
from urllib.parse import urlsplit, urlunsplit

import requests
from sqlalchemy import and_, case, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert

from inventory_hub.db_models import Shop
from inventory_hub.order_collection_models import OrderCollectionSettings
from inventory_hub.order_webhook_models import OrderWebhookEvent, OrderWebhookSettings, OrderWebhookResolution
from inventory_hub.order_processing_models import OrderProcessingJob
from inventory_hub.services import order_collection as collection
from inventory_hub.services import order_webhook_credentials as credentials
from inventory_hub.services.order_collection_source import (
    CollectionSourceError, OLDEST_ORDER_CREATION, _entry, _fetch_page, _metadata,
)
from inventory_hub.services.order_stock_source import SourceError, _timestamp
from inventory_hub.services.upgates import UpgatesClient, UpgatesError, connection_fingerprint


EVENTS = ('create', 'update', 'delete')
MAX_NOTIFICATIONS = 5000
MAX_BODY = 2_000_000
BATCH_SIZE = 100
REGISTRATION_CHECK_SECONDS = 3600


class WebhookError(Exception):
    def __init__(self, code, status=409, retry_after=None):
        self.code, self.status, self.retry_after = code, status, retry_after
        super().__init__(code)


def now():
    return datetime.now(timezone.utc)


def basic_credentials(authorization):
    try:
        if not isinstance(authorization, str) or len(authorization) > 300 or not authorization.startswith('Basic '):
            raise ValueError()
        value = base64.b64decode(authorization[6:], validate=True).decode('ascii')
        shop_code, token = value.split(':', 1)
        if (not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,49}', shop_code)
                or not re.fullmatch(r'[A-Za-z0-9_-]{64}', token)):
            raise ValueError()
        return shop_code, token
    except (ValueError, UnicodeError, binascii.Error):
        raise WebhookError('order_webhook_access_required', 401) from None


def notifications(payload, event):
    """Bound and project the documented payload, ignoring all stock-like fields."""
    if event not in EVENTS or not isinstance(payload, dict) or not isinstance(payload.get('orders'), list):
        raise WebhookError('order_webhook_invalid_request', 422)
    rows = payload['orders']
    if not 1 <= len(rows) <= MAX_NOTIFICATIONS:
        raise WebhookError('order_webhook_invalid_request', 422)
    field = {'create': 'creation_time', 'update': 'last_update_time', 'delete': 'deletion_time'}[event]
    result = {}
    for row in rows:
        if not isinstance(row, dict):
            raise WebhookError('order_webhook_invalid_request', 422)
        number = row.get('order_number')
        if (not isinstance(number, str) or not 1 <= len(number) <= 100 or number != number.strip()
                or ';' in number or any(ord(c) < 32 or ord(c) == 127 for c in number)):
            raise WebhookError('order_webhook_invalid_request', 422)
        try:
            number.encode('utf-8')
            at = datetime.fromisoformat(_timestamp(row.get(field)))
        except (UnicodeError, SourceError, TypeError, ValueError):
            raise WebhookError('order_webhook_invalid_request', 422) from None
        # Reject clocks far in the future so one bad hint cannot fence out real
        # later changes. Historical notifications remain harmless current reads.
        if at > now() + timedelta(minutes=5):
            raise WebhookError('order_webhook_invalid_request', 422)
        digest = hashlib.sha256(json.dumps([event, number, at.isoformat(), row.get('status_id')],
            separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()
        candidate = {'order_number': number, 'event': event, 'notification_at': at, 'notification_hash': digest}
        if number not in result or at >= result[number]['notification_at']:
            result[number] = candidate
    return [result[key] for key in sorted(result)]


async def authenticate(db, authorization):
    shop_code, token = basic_credentials(authorization)
    # Short SQL timeouts are local to this request; slow DBs answer retryable
    # failure before Upgates' three-second delivery timeout whenever possible.
    await db.execute(text("SET LOCAL lock_timeout = '400ms'"))
    await db.execute(text("SET LOCAL statement_timeout = '1000ms'"))
    # Same Shop -> settings order as the collector and worker. Inserting a new
    # event also acquires a Shop FK key-share lock; taking it now avoids the
    # settings -> FK inversion with a worker claiming this shop concurrently.
    shop = await db.scalar(select(Shop).where(Shop.code == shop_code, Shop.is_active.is_(True),
        Shop.platform == 'upgates').with_for_update(read=True, key_share=True))
    row = await db.scalar(select(OrderWebhookSettings).where(OrderWebhookSettings.shop_id == shop.id)
        .with_for_update().execution_options(populate_existing=True)) if shop else None
    if row is None or not row.enabled or not secrets.compare_digest(credentials.digest(token), row.secret_hash):
        raise WebhookError('order_webhook_access_required', 401)
    if collection.target_fingerprint(shop_code) != row.target_fingerprint:
        raise WebhookError('order_webhook_target_changed', 503)
    return shop, row



async def accept(db, shop, settings, rows):
    at = now()
    # Deterministic key order avoids inversions between simultaneous batches.
    # Identical pending hints coalesce. Upgates has no delivery ID and second-
    # precision timestamps: an identical hint during/after a read can represent
    # another same-second edit, so it must wake a new current-state read.
    for offset in range(0, len(rows), 500):
        values = [{**row, 'shop_id': shop.id, 'generation': 1, 'status': 'pending', 'attempts': 0,
                   'next_attempt_at': at, 'last_received_at': at} for row in rows[offset:offset + 500]]
        statement = insert(OrderWebhookEvent).values(values)
        newer = or_(statement.excluded.notification_at > OrderWebhookEvent.notification_at,
            and_(statement.excluded.notification_at == OrderWebhookEvent.notification_at,
                 or_(statement.excluded.notification_hash != OrderWebhookEvent.notification_hash,
                     OrderWebhookEvent.status.in_(['running', 'completed']))))
        # At the same instant deletion dominates a delayed create/update hint.
        newer = and_(newer, or_(statement.excluded.notification_at > OrderWebhookEvent.notification_at,
                               OrderWebhookEvent.event != 'delete', statement.excluded.event == 'delete'))
        await db.execute(statement.on_conflict_do_update(
            index_elements=['shop_id', 'order_number'], set_={
                'event': statement.excluded.event, 'notification_at': statement.excluded.notification_at,
                'notification_hash': statement.excluded.notification_hash,
                'generation': OrderWebhookEvent.generation + 1, 'status': 'pending', 'attempts': 0,
                'next_attempt_at': at, 'last_received_at': at, 'last_error': None,
            }, where=newer))
    # The settings row serializes delivery with configuration and event claims.
    await db.execute(update(OrderWebhookSettings).where(OrderWebhookSettings.shop_id == shop.id)
                     .values(last_received_at=at))
    await db.flush()


async def delivery_health(db, shop_id, at=None):
    row = await db.scalar(select(OrderWebhookSettings).where(OrderWebhookSettings.shop_id == shop_id)
                          .execution_options(populate_existing=True))
    if row is None:
        return None
    counts = dict((await db.execute(select(OrderWebhookEvent.status, func.count()).where(
        OrderWebhookEvent.shop_id == shop_id).group_by(OrderWebhookEvent.status))).all())
    fields = ('enabled', 'target_fingerprint', 'registration_status', 'registered_at', 'registration_checked_at',
              'catchup_interval_seconds', 'last_error')
    return {**{field: getattr(row, field) for field in fields},
            'pending_count': sum(counts.get(s, 0) for s in ('pending', 'running', 'retry')),
            'review_count': counts.get('review', 0)}


async def effective_poll_interval(db, shop_id, configured):
    row = await db.scalar(select(OrderWebhookSettings).where(OrderWebhookSettings.shop_id == shop_id)
                          .execution_options(populate_existing=True))
    shop = await db.get(Shop, shop_id) if row else None
    if (row and shop and row.enabled and row.registration_status == 'registered' and not row.last_error
            and collection.target_fingerprint(shop.code) == row.target_fingerprint and row.registration_checked_at
            and row.registration_checked_at >= now() - timedelta(hours=6)):
        return row.catchup_interval_seconds
    return configured


async def status(db, shop_code):
    shop = await collection._shop(db, shop_code)
    row = await db.get(OrderWebhookSettings, shop.id)
    collector = await db.get(OrderCollectionSettings, shop.id)
    health = await delivery_health(db, shop.id)
    events = (await db.scalars(select(OrderWebhookEvent).where(OrderWebhookEvent.shop_id == shop.id)
        .order_by(OrderWebhookEvent.last_received_at.desc(), OrderWebhookEvent.order_number).limit(50))).all()
    fields = ('enabled', 'revision', 'public_base_url', 'catchup_interval_seconds', 'registration_status',
              'registered_at', 'registration_checked_at', 'last_received_at', 'last_completed_at', 'last_error',
              'registered_events', 'next_registration_at', 'registration_attempts')
    event_fields = ('order_number', 'event', 'notification_at', 'last_received_at', 'last_completed_at',
                    'status', 'attempts', 'last_error')
    return {'shop': {'code': shop.code, 'name': shop.name},
        'webhook': {**{field: getattr(row, field) for field in fields}, 'secret_configured': bool(row.secret_hash),
            'pending_mutations': [{'event': event, **{key: value.get(key) for key in ('method', 'intent_id', 'started_at')}}
                for event, value in (row.registration_intents or {}).items() if value.get('method') in ('POST', 'PUT')]} if row else None,
        'pending_count': health['pending_count'] if health else 0, 'review_count': health['review_count'] if health else 0,
        'collector_enabled': bool(collector and collector.enabled),
        'connection_matches': bool(row is None or row.target_fingerprint == collection.target_fingerprint(shop.code)),
        'events': [{field: getattr(event, field) for field in event_fields} for event in events],
        'external_write_enabled': False}


async def configure(db, payload):
    shop = await collection._shop(db, payload.shop_code, lock=True)
    collector = await collection._settings(db, shop.id, lock=True)
    row = await db.get(OrderWebhookSettings, shop.id, with_for_update=True)
    if payload.expected_revision != (row.revision if row else None):
        raise WebhookError('order_webhook_settings_changed')
    target = collection.target_fingerprint(shop.code)
    if not target:
        raise WebhookError('order_webhook_connection_missing')
    if payload.enabled and (not collector or not collector.enabled):
        raise WebhookError('order_webhook_collection_required')
    if (collector and collector.target_fingerprint != target) or (row and row.target_fingerprint != target):
        raise WebhookError('order_webhook_target_changed')
    at = now()
    if row is None:
        try:
            secret_hash = credentials.create(shop.id)
        except Exception:
            raise WebhookError('order_webhook_secret_unavailable', 503) from None
        row = OrderWebhookSettings(shop_id=shop.id, enabled=payload.enabled, revision=1,
            target_fingerprint=target, secret_hash=secret_hash, public_base_url=payload.public_base_url,
            catchup_interval_seconds=payload.catchup_interval_seconds, registration_status='pending',
            next_registration_at=at, registration_attempts=0, registered_events={})
        db.add(row)
    else:
        row.revision += 1
        row.enabled = payload.enabled
        row.public_base_url = payload.public_base_url
        row.catchup_interval_seconds = payload.catchup_interval_seconds
        row.registration_status, row.next_registration_at, row.last_error = 'pending', at, None
        row.registered_at = row.registration_checked_at = None
        row.registration_attempts = 0
    if collector and payload.enabled:
        collector.next_poll_at = min(collector.next_poll_at, at)
        collector.last_mode = 'reconcile'  # The next complete scan must be a delta.
    await db.flush()
    result = await status(db, shop.code)
    await db.commit()
    return result


async def request_registration(db, payload):
    shop = await collection._shop(db, payload.shop_code, lock=True)
    row = await db.get(OrderWebhookSettings, shop.id, with_for_update=True)
    if row is None or row.revision != payload.expected_revision:
        raise WebhookError('order_webhook_settings_changed')
    if row.registration_status != 'running':
        row.registration_status, row.next_registration_at, row.last_error = 'pending', now(), None
    await db.flush()
    result = await status(db, shop.code)
    await db.commit()
    return result


async def retry_notifications(db, payload):
    shop = await collection._shop(db, payload.shop_code, lock=True)
    row = await db.get(OrderWebhookSettings, shop.id, with_for_update=True)
    if row is None or row.revision != payload.expected_revision:
        raise WebhookError('order_webhook_settings_changed')
    await db.execute(update(OrderWebhookEvent).where(OrderWebhookEvent.shop_id == shop.id,
        OrderWebhookEvent.status.in_(['review', 'retry'])).values(status='pending', attempts=0,
                                                               next_attempt_at=now(), last_error=None))
    await db.flush()
    result = await status(db, shop.code)
    await db.commit()
    return result


def _client(shop_code, target):
    client = UpgatesClient.from_shop(shop_code)
    auth = client.session.auth
    login = auth[0] if isinstance(auth, (tuple, list)) and len(auth) == 2 else getattr(auth, 'username', None)
    if connection_fingerprint(client.base_url, login) != target:
        client.session.close()
        raise WebhookError('order_webhook_target_changed')
    return client


def _request(client, method, payload=None):
    """No generic diagnostic transport: webhook URLs contain a credential."""
    try:
        response = client.session.request(method, f'{client.base_url}/webhooks', json=payload,
            timeout=(5, 25), verify=client.verify_ssl, allow_redirects=False)
    except requests.RequestException:
        raise WebhookError('order_webhook_source_unavailable', 502) from None
    try:
        if response.status_code in (401, 403):
            raise WebhookError('order_webhook_access_denied', 502)
        if response.status_code != 200 or len(response.content) > 2_000_000:
            raise WebhookError('order_webhook_registration_unverified', 502)
        try:
            data = response.json()
        except ValueError:
            raise WebhookError('order_webhook_registration_unverified', 502) from None
        if not isinstance(data, dict) or data.get('messages'):
            raise WebhookError('order_webhook_registration_unverified', 502)
        return data
    finally:
        response.close()


def reconcile_registration(plan, before_create, before_update=None, after_update=None):
    if any(value.get('method') == 'PUT' for value in (plan.get('registration_intents') or {}).values()):
        # A matching GET cannot prove an older timed-out PUT has finished.
        # Keep this fence across changed desired configuration until settled.
        raise WebhookError('order_webhook_registration_uncertain')
    token = credentials.load(plan['shop_id'], plan['secret_hash'])
    client = _client(plan['shop_code'], plan['target_fingerprint'])
    try:
        # Always inspect before writing, including after a timed-out create.
        # Names are stable per target and Hub installation, so uncertain creates
        # are discovered instead of blindly creating duplicate subscriptions.
        listed = _request(client, 'GET').get('webhooks')
        if not isinstance(listed, list) or len(listed) > 1000:
            raise WebhookError('order_webhook_registration_unverified', 502)
        parsed = urlsplit(plan['public_base_url'])
        credential_base = urlunsplit((parsed.scheme, f"{plan['shop_code']}:{token}@{parsed.netloc}", parsed.path, '', ''))
        installation = hashlib.sha256(plan['secret_hash'].encode()).hexdigest()[:12]
        result = {}
        for event in EVENTS:
            name = f'inventory-hub-{installation}-{plan["shop_code"]}-{event}'
            matches = [item for item in listed if isinstance(item, dict) and item.get('name') == name]
            if len(matches) > 1:
                raise WebhookError('order_webhook_registration_ambiguous')
            desired = {'name': name, 'url': f'{credential_base}/webhooks/upgates/orders/{event}',
                       'event': f'Orders.{event}', 'active_yn': plan['enabled']}
            if not matches and not plan['enabled']:
                if event in (plan.get('registration_intents') or {}):
                    raise WebhookError('order_webhook_registration_uncertain')
                continue
            old = matches[0] if matches else None
            if old is not None and (type(old.get('id')) is not int or old['id'] < 1):
                raise WebhookError('order_webhook_registration_unverified', 502)
            if old and all(old.get(key) == value for key, value in desired.items()):
                confirmed = old
            else:
                if old is None:
                    # A GET that does not yet show a timed-out POST is not proof
                    # it failed. Never submit another create after that boundary.
                    if event in (plan.get('registration_intents') or {}):
                        raise WebhookError('order_webhook_registration_uncertain')
                    before_create(event)  # Durable commit before remote POST.
                body = {**desired, **({'id': old['id']} if old else {})}
                intent_id, payload_hash = None, None
                if old is not None:
                    if before_update is None or after_update is None:
                        raise WebhookError('order_webhook_registration_uncertain')
                    payload_hash = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                    intent_id = before_update(event, payload_hash)
                confirmed = _request(client, 'PUT' if old else 'POST', body).get('webhook')
            if (not isinstance(confirmed, dict) or type(confirmed.get('id')) is not int or confirmed['id'] < 1
                    or (old is not None and confirmed['id'] != old['id'])
                    or any(confirmed.get(key) != value for key, value in desired.items())):
                raise WebhookError('order_webhook_registration_unverified', 502)
            if old is not None and not all(old.get(key) == value for key, value in desired.items()):
                after_update(event, intent_id, payload_hash)
            result[event] = confirmed['id']
        return result
    except UpgatesError:
        raise WebhookError('order_webhook_source_unavailable', 502) from None
    finally:
        client.session.close()


async def start_registration(db):
    row = await db.scalar(select(OrderWebhookSettings).where(OrderWebhookSettings.next_registration_at <= now(),
        or_(OrderWebhookSettings.enabled.is_(True), OrderWebhookSettings.registration_status.in_(['pending', 'failed'])))
        .order_by(OrderWebhookSettings.next_registration_at, OrderWebhookSettings.shop_id).limit(1).with_for_update())
    if row is None:
        return None
    shop = await db.get(Shop, row.shop_id)
    if not shop or not shop.is_active or collection.target_fingerprint(shop.code) != row.target_fingerprint:
        row.last_error, row.registration_status = 'order_webhook_target_changed', 'failed'
        row.next_registration_at = now() + timedelta(hours=1)
        return None
    row.registration_status, row.registration_attempts = 'running', row.registration_attempts + 1
    await db.flush()
    return {field: getattr(row, field) for field in ('shop_id', 'enabled', 'revision', 'target_fingerprint',
        'secret_hash', 'public_base_url', 'registration_attempts', 'registration_intents')} | {'shop_code': shop.code}


async def record_create_intent(db, plan, event):
    row = await db.get(OrderWebhookSettings, plan['shop_id'], with_for_update=True)
    if (row is None or row.revision != plan['revision'] or row.registration_status != 'running'
            or row.registration_attempts != plan['registration_attempts'] or event not in EVENTS):
        raise WebhookError('order_webhook_settings_changed')
    if event in (row.registration_intents or {}):
        raise WebhookError('order_webhook_registration_uncertain')
    descriptor = {key: plan[key] for key in ('shop_id', 'target_fingerprint', 'public_base_url', 'secret_hash', 'enabled')}
    descriptor['event'] = event
    payload_hash = hashlib.sha256(json.dumps(descriptor, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    row.registration_intents = {**(row.registration_intents or {}), event: {'method': 'POST', 'intent_id': str(uuid4()),
        'payload_hash': payload_hash, 'started_at': now().isoformat()}}
    await db.flush()


async def record_update_intent(db, plan, event, payload_hash):
    row = await db.get(OrderWebhookSettings, plan['shop_id'], with_for_update=True)
    if (row is None or row.revision != plan['revision'] or row.registration_status != 'running'
            or row.registration_attempts != plan['registration_attempts'] or event not in EVENTS):
        raise WebhookError('order_webhook_settings_changed')
    if any(value.get('method') == 'PUT' for value in (row.registration_intents or {}).values()):
        raise WebhookError('order_webhook_registration_uncertain')
    intent_id = str(uuid4())
    row.registration_intents = {**(row.registration_intents or {}), event: {'method': 'PUT', 'intent_id': intent_id,
        'payload_hash': payload_hash, 'started_at': now().isoformat()}}
    await db.flush()
    return intent_id


async def confirm_update_intent(db, plan, event, intent_id, payload_hash):
    row = await db.get(OrderWebhookSettings, plan['shop_id'], with_for_update=True)
    intent = (row.registration_intents or {}).get(event) if row else None
    if (not intent or intent.get('method') != 'PUT' or intent.get('intent_id') != intent_id
            or intent.get('payload_hash') != payload_hash):
        raise WebhookError('order_webhook_settings_changed')
    # The exact old request received a validated ACK. Its completion remains
    # valid if an operator edited desired configuration while it was in flight.
    row.registration_intents = {key: value for key, value in row.registration_intents.items() if key != event}
    await db.flush()


async def resolve_registration(db, payload, actor):
    # Do not clear evidence while the live worker can still receive an ACK.
    # Acquire this before Shop/settings locks, matching the worker lock order.
    if not await db.scalar(text('SELECT pg_try_advisory_xact_lock(691432235)')):
        raise WebhookError('order_webhook_registration_busy')
    shop = await collection._shop(db, payload.shop_code, lock=True)
    row = await db.get(OrderWebhookSettings, shop.id, with_for_update=True)
    if row is None or row.revision != payload.expected_revision:
        raise WebhookError('order_webhook_settings_changed')
    intent = (row.registration_intents or {}).get(payload.event)
    if not intent or intent.get('method') not in ('POST', 'PUT') or intent.get('intent_id') != payload.intent_id:
        raise WebhookError('order_webhook_settings_changed')
    db.add(OrderWebhookResolution(shop_id=shop.id, event=payload.event, intent_id=payload.intent_id,
        method=intent['method'], payload_hash=intent['payload_hash'], settings_revision=row.revision,
        evidence_note=payload.note, resolved_by=actor, resolved_at=now()))
    row.registration_intents = {key: value for key, value in row.registration_intents.items() if key != payload.event}
    row.registration_status, row.next_registration_at, row.last_error = 'pending', now(), None
    await db.flush()
    result = await status(db, shop.code)
    await db.commit()
    return result


async def finish_registration(db, plan, events=None, error=None):
    await db.scalar(select(Shop).where(Shop.id == plan['shop_id']).with_for_update())
    collector = await collection._settings(db, plan['shop_id'], lock=True)
    row = await db.get(OrderWebhookSettings, plan['shop_id'], with_for_update=True)
    if (row is None or row.revision != plan['revision'] or row.registration_status != 'running'
            or row.registration_attempts != plan['registration_attempts']):
        return
    at = now()
    if any(value.get('method') == 'PUT' for value in (row.registration_intents or {}).values()):
        error = 'order_webhook_registration_uncertain'
    if error:
        row.registration_status, row.last_error = 'failed', error
        row.next_registration_at = at + timedelta(seconds=min(3600, 60 * 2 ** min(row.registration_attempts - 1, 6)))
    else:
        row.registration_status = 'registered' if row.enabled else 'disabled'
        row.registered_events, row.last_error = events, None
        row.registration_intents = {key: value for key, value in (row.registration_intents or {}).items() if key not in events}
        row.registered_at = row.registered_at or at
        row.registration_checked_at = at
        row.next_registration_at = at + timedelta(seconds=REGISTRATION_CHECK_SECONDS)
        if collector and row.enabled and (collector.cursor_at < row.registered_at):
            collector.next_poll_at, collector.last_mode = at, 'reconcile'


async def recover(db):
    # Only the dedicated advisory-lock owner calls this. In-flight upstream
    # reads are safe to retry; registrations are always read before rewriting.
    await db.execute(update(OrderWebhookSettings).where(OrderWebhookSettings.registration_status == 'running')
        .values(registration_status='pending', next_registration_at=now(), last_error='order_webhook_interrupted'))
    await db.execute(update(OrderWebhookEvent).where(OrderWebhookEvent.status == 'running')
        .values(status='pending', next_attempt_at=now(), last_error='order_webhook_interrupted'))


async def start_batch(db):
    # A paused/misconfigured oldest shop must not starve another shop. SQL
    # excludes inactive collectors, and changed filesystem targets are fenced
    # visibly before continuing to the next eligible shop.
    shop_ids = (await db.scalars(select(OrderWebhookEvent.shop_id)
        .join(OrderWebhookSettings, OrderWebhookSettings.shop_id == OrderWebhookEvent.shop_id)
        .join(OrderCollectionSettings, OrderCollectionSettings.shop_id == OrderWebhookEvent.shop_id)
        .join(Shop, Shop.id == OrderWebhookEvent.shop_id)
        .where(OrderWebhookSettings.enabled.is_(True), OrderCollectionSettings.enabled.is_(True),
            Shop.is_active.is_(True), Shop.platform == 'upgates',
            OrderWebhookEvent.status.in_(['pending', 'retry']), OrderWebhookEvent.next_attempt_at <= now())
        .group_by(OrderWebhookEvent.shop_id)
        .order_by(func.min(OrderWebhookEvent.next_attempt_at), OrderWebhookEvent.shop_id).limit(20))).all()
    for shop_id in shop_ids:
        shop = await db.scalar(select(Shop).where(Shop.id == shop_id).with_for_update()
                               .execution_options(populate_existing=True))
        collector = await collection._settings(db, shop_id, lock=True)
        settings = await db.scalar(select(OrderWebhookSettings).where(OrderWebhookSettings.shop_id == shop_id)
                                   .with_for_update().execution_options(populate_existing=True))
        if not settings.enabled or not collector or not collector.enabled or not shop.is_active:
            continue
        if (collector.target_fingerprint != settings.target_fingerprint
                or collection.target_fingerprint(shop.code) != settings.target_fingerprint):
            await db.execute(update(OrderWebhookEvent).where(OrderWebhookEvent.shop_id == shop_id,
                OrderWebhookEvent.status.in_(['pending', 'retry'])).values(
                    status='review', last_error='order_webhook_target_changed'))
            # Commit this fence before locking another Shop; the next short
            # worker cycle can claim the next shop without multi-shop inversion.
            return None
        rows = (await db.scalars(select(OrderWebhookEvent).where(OrderWebhookEvent.shop_id == shop_id,
            OrderWebhookEvent.status.in_(['pending', 'retry']), OrderWebhookEvent.next_attempt_at <= now())
            .order_by(OrderWebhookEvent.next_attempt_at, OrderWebhookEvent.order_number).limit(BATCH_SIZE).with_for_update())).all()
        for row in rows:
            row.status, row.attempts = 'running', row.attempts + 1
        await db.flush()
        return {'shop_id': shop_id, 'shop_code': shop.code, 'revision': settings.revision,
                'target_fingerprint': settings.target_fingerprint,
                'events': [{'order_number': row.order_number, 'generation': row.generation,
                            'attempts': row.attempts, 'notification_at': row.notification_at} for row in rows]}
    return None


def _fetch_current(plan):
    numbers = [entry['order_number'] for entry in plan['events']]
    def fetch(requested, deleted=False):
        params = {'order_numbers': ';'.join(requested), 'page': 1}
        if deleted:
            params['deleted_yn'] = 'true'
        payload = _fetch_page(plan['shop_code'], params, plan['target_fingerprint'])
        raw, pages, count = _metadata(payload, 1)
        if pages > 1 or count != len(raw):
            raise CollectionSourceError('order_collection_invalid_response')
        normalized = [_entry(item, created_from=OLDEST_ORDER_CREATION, changed_from=OLDEST_ORDER_CREATION,
            created_to=now() + timedelta(minutes=5), deleted=deleted) for item in raw]
        if (len({item['order_number'] for item in normalized}) != len(normalized)
                or any(item['order_number'] not in requested for item in normalized)
                or len({item['uuid'] for item in normalized}) != len(normalized)):
            raise CollectionSourceError('order_collection_invalid_response')
        return {item['order_number']: item for item in normalized}
    result = fetch(numbers)
    missing = [number for number in numbers if number not in result]
    if missing:
        result.update(fetch(missing, deleted=True))
    return result


async def fetch_current(plan):
    return await asyncio.to_thread(_fetch_current, plan)


async def finish_batch(db, plan, entries=None, error=None, retry_after=None):
    shop = await collection._shop(db, plan['shop_code'], lock=True)
    policy = await collection._policy(db, shop.id, lock=True)
    collector = await collection._settings(db, shop.id, lock=True)
    settings = await db.get(OrderWebhookSettings, shop.id, with_for_update=True)
    if (settings is None or not settings.enabled or settings.revision != plan['revision'] or not policy
            or not collector or not collector.enabled or settings.target_fingerprint != plan['target_fingerprint']
            or collection.target_fingerprint(shop.code) != plan['target_fingerprint']):
        error = 'order_webhook_settings_changed'
    at, accepted = now(), []
    for claimed in sorted(plan['events'], key=lambda item: item['order_number']):
        row = await db.get(OrderWebhookEvent, (shop.id, claimed['order_number']), with_for_update=True)
        if row is None or row.generation != claimed['generation'] or row.status != 'running':
            continue
        data = (entries or {}).get(row.order_number)
        reason = error or ('order_webhook_order_not_found' if data is None else None)
        if not reason and not data['deleted'] and datetime.fromisoformat(data['updated_at']) < claimed['notification_at']:
            reason = 'order_webhook_source_behind'
        if reason:
            row.status = 'review' if row.attempts >= 8 or reason == 'order_webhook_settings_changed' else 'retry'
            row.last_error = reason
            delay = min(3600, 5 * 2 ** min(row.attempts - 1, 10))
            if type(retry_after) is int:
                delay = max(delay, min(604800, retry_after))
            row.next_attempt_at = at + timedelta(seconds=delay)
        else:
            accepted.append(data)
            row.status, row.last_error, row.last_completed_at = 'completed', None, at
    if accepted:
        await collection.save_entries(db, shop.id, policy, accepted)
        # Same-second quantity edits may retain identical header metadata.
        # Wake already-known jobs explicitly; the processor still fetches the
        # complete current order and applies its policy/idempotency guards.
        # Shop FOR UPDATE serializes this with job claims/completion. Advancing
        # generation invalidates an older in-flight full-order read.
        await db.execute(update(OrderProcessingJob).where(OrderProcessingJob.shop_id == shop.id,
            OrderProcessingJob.order_number.in_([item['order_number'] for item in accepted])).values(
                generation=OrderProcessingJob.generation + 1, status='pending', next_attempt_at=at))
        settings.last_completed_at = at
    # No collection cursor/last_completed update: one observed order cannot
    # establish that no other notification was lost.
    await db.flush()
