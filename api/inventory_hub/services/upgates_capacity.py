"""One read-only API status probe and an explicitly theoretical local call model.

No schedule, credential, stock, order, or remote product is changed. Upstream
bodies and URLs never enter the report. A permission declaration is not a test
of a product write, and remaining-now headers are not a daily allowance.
"""
import asyncio
from collections import Counter
from datetime import datetime, timezone
import json
from time import monotonic

import requests
from sqlalchemy import func, select

from inventory_hub.db_models import Product
from inventory_hub.db_models_ext import ShopProduct
from inventory_hub.order_collection_models import OrderCollectionSettings, OrderInboxEntry
from inventory_hub.order_processing_models import OrderProcessingJob
from inventory_hub.stock_settings_types import OperationalValues
from inventory_hub.stock_tracking_models import StockTracking
from inventory_hub.services import stock_projection, stock_settings, stock_sync
from inventory_hub.services.upgates import UpgatesClient, UpgatesError, connection_fingerprint, _retry_after_seconds

MAX_BYTES = 262_144
STREAM_CHECK_SECONDS = 15
RATE_HEADERS = {
    'hour_limit': 'X-Rate-Limit-Hour',
    'day_limit': 'X-Rate-Limit-Day',
    'hour_remaining': 'X-Rate-Limit-Hour-Remaining',
    'day_remaining': 'X-Rate-Limit-Day-Remaining',
    'total_remaining': 'X-Rate-Limit-Total-Remaining',
}
JOB_STATUSES = ('pending', 'running', 'completed', 'retry', 'review')
NOT_INCLUDED = ['other_integrations', 'manual_operations', 'extra_order_pages',
                'new_orders', 'errors_and_retries', 'other_hub_sync']


class CapacityError(Exception):
    def __init__(self, code, status=503):
        self.code, self.status = code, status
        super().__init__(code)


def now():
    return datetime.now(timezone.utc)


def _numeric_header(value):
    if (not isinstance(value, str) or not value or len(value) > 10
            or not value.isascii() or not value.isdigit()):
        return None
    parsed = int(value)
    return parsed if parsed <= 1_000_000_000 else None


def _quota(headers, status=None):
    quota = {key: _numeric_header(headers.get(header)) for key, header in RATE_HEADERS.items()}
    for window in ('hour', 'day'):
        limit, remaining = quota[f'{window}_limit'], quota[f'{window}_remaining']
        if limit is not None and remaining is not None and remaining > limit:
            quota[f'{window}_remaining'] = None
    quota['headers_complete'] = all(value is not None for value in quota.values())
    quota['retry_after_seconds'] = _retry_after_seconds(headers.get('Retry-After')) if status == 429 else None
    quota['theoretical_daily_max'] = (
        quota['hour_limit'] * 24 + quota['day_limit']
        if quota['hour_limit'] is not None and quota['day_limit'] is not None else None)
    return quota


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def _constant(_value):
    raise ValueError('Invalid JSON constant')


def _permissions(payload):
    if (not isinstance(payload, dict) or not isinstance(payload.get('services'), dict)
            or len(payload['services']) > 200
            or not isinstance(payload.get('documentation_link'), str)
            or len(payload['documentation_link']) > 2048):
        raise ValueError('Invalid status schema')
    result = dict.fromkeys(('products', 'orders', 'order_statuses'), 'unknown')
    aliases = {'products': 'products', 'orders': 'orders', 'orderstatuses': 'order_statuses',
               'order-statuses': 'order_statuses', 'order_statuses': 'order_statuses'}
    seen = set()
    for key, row in payload['services'].items():
        if (not isinstance(key, str) or not 1 <= len(key) <= 100 or not isinstance(row, dict)
                or not isinstance(row.get('service'), str) or not 1 <= len(row['service']) <= 100
                or not isinstance(row.get('url'), str) or len(row['url']) > 2048
                or not isinstance(row.get('privilege'), str) or len(row['privilege']) > 100):
            raise ValueError('Invalid service schema')
        keyed, named = aliases.get(key.casefold()), aliases.get(row['service'].casefold())
        if keyed is not None and named is not None and keyed != named:
            raise ValueError('Conflicting service identity')
        selected = keyed or named
        if selected is None:
            continue
        if selected in seen:
            raise ValueError('Ambiguous service permission')
        seen.add(selected)
        # Unknown upstream vocabulary is never interpreted as write permission.
        result[selected] = row['privilege'] if row['privilege'] in ('deny', 'read', 'write', 'all') else 'unknown'
    return result


def probe(shop_code):
    """Exactly one configured GET /status, or zero if no safe target exists."""
    result = {'ok': False, 'http_status': None, 'error': None, 'requests_attempted': 0,
              'latency_ms': None, 'target_fingerprint': None,
              'permissions': dict.fromkeys(('products', 'orders', 'order_statuses'), 'unknown'),
              'write_tested': False}
    quota = _quota({})
    client, response, started = None, None, None
    try:
        try:
            client = UpgatesClient.from_shop(shop_code)
        except (UpgatesError, OSError, ValueError, TypeError):
            result['error'] = 'upgates_capacity_connection_missing'
            return {'probe': result, 'quota': quota, 'observed_at': now().isoformat()}
        auth = client.session.auth
        login = auth[0] if isinstance(auth, (tuple, list)) and len(auth) == 2 else getattr(auth, 'username', None)
        result['target_fingerprint'] = connection_fingerprint(client.base_url, login)
        if result['target_fingerprint'] is None:
            result['error'] = 'upgates_capacity_target_invalid'
            return {'probe': result, 'quota': quota, 'observed_at': now().isoformat()}
        started = monotonic()
        result['requests_attempted'] = 1
        response = client.session.get(f'{client.base_url}/status', timeout=(5, 10),
                                      verify=True, allow_redirects=False, stream=True)
        if getattr(response, 'hub_request_sent', None) is False:
            result['requests_attempted'] = 0
        status = response.status_code
        result['http_status'] = status
        quota = _quota(response.headers, status)
        if status != 200:
            result['error'] = ('upgates_capacity_access_denied' if status in (401, 403) else
                               'upgates_capacity_rate_limited' if status == 429 else
                               'upgates_capacity_redirect_rejected' if 300 <= status < 400 else
                               'upgates_capacity_http_error')
        else:
            # Socket connect/read timeouts and decoded byte limit bound the probe.
            # This extra between-chunk check is not a strict wall-clock deadline.
            chunks, size = [], 0
            for chunk in response.iter_content(chunk_size=16_384):
                size += len(chunk)
                if size > MAX_BYTES or monotonic() - started > STREAM_CHECK_SECONDS:
                    raise ValueError('Status response exceeds bounds')
                chunks.append(chunk)
            if monotonic() - started > STREAM_CHECK_SECONDS:
                raise ValueError('Status response exceeds bounds')
            body = json.loads(b''.join(chunks), object_pairs_hook=_object, parse_constant=_constant)
            result['permissions'] = _permissions(body)
            result['ok'] = True
    except requests.RequestException:
        result['error'] = 'upgates_capacity_source_unavailable'
    except (ValueError, TypeError, UnicodeError, RecursionError):
        result['error'] = 'upgates_capacity_invalid_response'
    finally:
        if started is not None:
            result['latency_ms'] = max(0, round((monotonic() - started) * 1000))
        if response is not None:
            response.close()
        if client is not None:
            client.session.close()
    return {'probe': result, 'quota': quota, 'observed_at': now().isoformat()}


def _daily(calls, seconds):
    return round(calls * 86400 / seconds, 2) if calls is not None else None


async def projection(db, shop_code):
    """Count actual local eligible leaves, not total supplier catalog records.

Quantities, identity and evidence use the publisher's read-only projection.
The estimates assume configured intervals without run time or cooldown; they
are scenarios if enabled, never a claim about measured or current API usage.
"""
    with db.no_autoflush:
        try:
            shop, warehouse, policy, _, values = await stock_sync.scope(db, shop_code)
        except stock_sync.SyncError as error:
            raise CapacityError('upgates_capacity_shop_not_found', 404) from None
        operational = OperationalValues().model_dump()
        config = None
        if warehouse is not None:
            config = await stock_settings.effective(db, shop.id)
            operational = config['values']
        collector = await db.get(OrderCollectionSettings, shop.id)
        listed_query = select(Product.id, Product.sku).join(ShopProduct,
            ShopProduct.product_id == Product.id).where(ShopProduct.shop_id == shop.id,
                ShopProduct.is_listed.is_(True)).distinct()
        listed = (await db.execute(listed_query.order_by(Product.id))).all()
        confirmed = None
        eligible = None
        initial_batches = None
        eligible_targets = {}
        errors = Counter()
        if warehouse is not None:
            confirmed = await db.scalar(select(func.count()).select_from(StockTracking).where(
                StockTracking.warehouse_id == warehouse.id,
                StockTracking.product_id.in_(listed_query.with_only_columns(Product.id)))) or 0
            eligible = 0
            valid_codes = []
            for row in listed:
                try:
                    stock_projection._validate_skus([row.sku])
                    valid_codes.append(row.sku)
                except stock_projection.StockProjectionError as error:
                    errors[error.code] += 1
            for start in range(0, len(valid_codes), 100):
                observed = await stock_projection.preview(db, shop_code, valid_codes[start:start + 100])
                for row in observed['rows']:
                    if row['errors']:
                        errors.update(set(row['errors']))
                        continue
                    try:
                        stock_sync.source.transport._target(row['target'])
                    except stock_sync.source.SourceError as error:
                        errors[error.code] += 1
                        continue
                    eligible += 1
                    eligible_targets[row['sku']] = row['target']
            # The worker chunks listed Product.id rows before filtering local
            # eligibility; invalid rows still occupy space in that chunk.
            initial_batches = 0
            for start in range(0, len(listed), values['batch_size']):
                targets = [eligible_targets[row.sku] for row in listed[start:start + values['batch_size']]
                           if row.sku in eligible_targets]
                if targets:
                    initial_batches += len(stock_sync.source.batch_groups(targets))
        jobs = dict.fromkeys(JOB_STATUSES, 0)
        jobs.update(dict((await db.execute(select(OrderProcessingJob.status, func.count())
            .where(OrderProcessingJob.shop_id == shop.id).group_by(OrderProcessingJob.status))).all()))
        inbox_count = await db.scalar(select(func.count()).select_from(OrderInboxEntry)
                                      .where(OrderInboxEntry.shop_id == shop.id)) or 0
        missing_query = select(func.count()).select_from(OrderInboxEntry).outerjoin(OrderProcessingJob,
            (OrderProcessingJob.shop_id == OrderInboxEntry.shop_id)
            & (OrderProcessingJob.source_uuid == OrderInboxEntry.source_uuid)).where(
                OrderInboxEntry.shop_id == shop.id, OrderProcessingJob.id.is_(None))
        if config and config['automation_starts_at'] is not None:
            missing_query = missing_query.where(OrderInboxEntry.created_at >= config['automation_starts_at'])
        unprocessed = await db.scalar(missing_query) or 0
    return {
        'captured_at': now().isoformat(), 'warehouse_code': warehouse.code if warehouse else None,
        'listed_skus': len(listed), 'confirmed_skus': confirmed, 'eligible_skus': eligible,
        'unpublishable_skus': len(listed) - eligible if eligible is not None else None,
        'eligibility_errors': [{'code': code, 'count': count} for code, count in sorted(errors.items())],
        'stock': {'configured': policy is not None, 'enabled': bool(policy and policy.enabled),
                  'authorized': bool(policy and policy.authorized), 'server_enabled': stock_sync.enabled(),
                  'interval_seconds': values['interval_seconds'], 'batch_size': values['batch_size'],
                  'call_model': 'origin_aware_delta', 'audit_interval_seconds': 21600, 'audit_pass_calls': initial_batches,
                  'initial_batch_calls': initial_batches, 'singleton_pass_calls': eligible,
                  'unchanged_pass_calls': 0 if eligible is not None else None,
                  'all_changed_pass_calls': initial_batches + eligible if eligible is not None else None,
                  'daily_unchanged_if_enabled': _daily(initial_batches, 21600),
                  'daily_all_changed_if_enabled': _daily(initial_batches + eligible if eligible is not None else None,
                                                          values['interval_seconds'])},
        'orders': {'collector_enabled': bool(collector and collector.enabled),
                   'processing_mode': config['mode'] if config else 'manual',
                   'processing_ready': bool(config and config['processing_ready']),
                   'processing_error': config['processing_error'] if config else 'stock_settings_policy_required',
                   'poll_interval_seconds': operational['poll_interval_seconds'],
                   'retry_minutes': operational['processing_retry_minutes'],
                   'full_check_hours': operational['full_order_check_hours'],
                   'inbox_count': inbox_count, 'jobs_by_status': jobs, 'unprocessed_count': unprocessed,
                   'collector_empty_poll_calls': 2,
                   'collector_daily_if_enabled': _daily(2, operational['poll_interval_seconds']),
                   'retry_daily_if_processing': _daily(2 * jobs['retry'], operational['processing_retry_minutes'] * 60),
                   'history_daily_if_processing': _daily(2 * (jobs['completed'] + jobs['review']),
                                                        operational['full_order_check_hours'] * 3600)},
        'estimate_kind': 'theoretical_no_runtime_delay', 'not_included': list(NOT_INCLUDED),
    }


async def _sample_targets(db, shop_code):
    """At most four exact leaves per kind; bounded to two product requests.

    Four 100-character UTF-8 codes fit the transport's 6 KB encoded filter
    even in the worst case. Never infer or require a local stock quantity.
    """
    from inventory_hub.db_models import Shop
    with db.no_autoflush:
        rows = (await db.execute(select(Product.id, Product.sku, Product.is_active, ShopProduct)
            .join(ShopProduct, ShopProduct.product_id == Product.id).join(Shop, Shop.id == ShopProduct.shop_id)
            .where(Shop.code == shop_code).order_by(Product.id, ShopProduct.id))).all()
    codes = [row.ShopProduct.external_code for row in rows]
    owners = Counter(code.casefold() for code in codes if isinstance(code, str))
    mappings = Counter(row.id for row in rows)
    counts = Counter()
    targets = []
    for row, code in zip(rows, codes):
        mapping = row.ShopProduct
        if (not row.is_active or not mapping.is_listed or code != row.sku or not isinstance(code, str)
                or owners[code.casefold()] != 1 or mappings[row.id] != 1):
            continue
        kind = 'variants' if mapping.is_variant else 'products'
        if counts[kind] >= 4:
            continue
        try:
            target = stock_sync.source.transport._target({'code': code,
                'parent_code': mapping.parent_code if mapping.is_variant else code,
                'variant_code': code if mapping.is_variant else None})
        except stock_sync.source.SourceError:
            continue
        targets.append(target)
        counts[kind] += 1
    return targets


async def _sample_probe(shop_code, targets, status_probe):
    report = {'requested': True, 'ok': False,
              'selected_products': sum(row['variant_code'] is None for row in targets),
              'selected_variants': sum(row['variant_code'] is not None for row in targets),
              'planned_requests': len(stock_sync.source.batch_groups(targets)) if targets else 0,
              'requests_attempted': 0, 'error': None}
    if not targets:
        report['error'] = 'upgates_capacity_sample_empty'
    elif not status_probe['ok']:
        report['error'] = 'upgates_capacity_sample_status_required'
    else:
        # On a partial/failed group sequence the exact sent count is unknown.
        # Never report the maximum planned count as actual measured traffic.
        report['requests_attempted'] = None
        try:
            await stock_sync.source.read_many(shop_code, targets, status_probe['target_fingerprint'])
            report.update(ok=True, requests_attempted=report['planned_requests'])
        except stock_sync.source.SourceError as error:
            report['error'] = error.code
        except Exception:
            report['error'] = 'upgates_capacity_sample_unavailable'
    return report


async def diagnose(db, shop_code, *, include_product_sample=False):
    local = await projection(db, shop_code)
    targets = await _sample_targets(db, shop_code) if include_product_sample else None
    # End the read-only transaction before the single external request.
    await db.rollback()
    remote = await asyncio.to_thread(probe, shop_code)
    report = {'shop_code': shop_code, **remote, 'projection': local}
    if targets is not None:
        report['catalog_probe'] = await _sample_probe(shop_code, targets, remote['probe'])
    return report
