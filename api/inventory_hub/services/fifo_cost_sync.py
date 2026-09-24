"""Bounded acquisition-cost reconciliation with durable one-attempt intents.

Reads never hold a database transaction across a network call. Queued work is
revalidated against its FIFO source and settings immediately before its intent
is committed. A possible PUT always fences the target until explicit recovery.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import case, func, or_, select, text, update

from inventory_hub.db_models import Product, Shop, Warehouse
from inventory_hub.db_models_ext import ShopOrder, ShopProduct
from inventory_hub.fifo_cost_models import FifoCostPublication as Publication, FifoCostSettings as Policy, FifoCostWarehouseSettings as Defaults
from inventory_hub.order_stock_models import OrderStockPolicy
from inventory_hub.order_collection_models import OrderCollectionSettings
from inventory_hub.stock_settings_models import StockShopSettings
from inventory_hub.services import fifo_cost_projection as projection, fifo_cost_source as source
from inventory_hub.services.catalog import CatalogError
from inventory_hub.services.fifo import digest
from inventory_hub.services.merchandising_write_guard import require_target_available
from inventory_hub.services.order_collection import target_fingerprint
from inventory_hub.services.product_identity import IDENTITY_WRITE_LOCK
from inventory_hub.settings import settings

DEFAULTS = {'interval_seconds': 300, 'batch_size': 20}
FENCED = ('queued', 'sending', 'uncertain')


class CostSyncError(Exception):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


def now():
    return datetime.now(timezone.utc)


def enabled():
    return bool(getattr(settings, 'FIFO_COST_SYNC_WRITE_ENABLED', False))


async def one(db, model, condition, lock=False):
    query = select(model).where(condition).execution_options(populate_existing=True)
    return await db.scalar(query.with_for_update() if lock else query)


async def identity_lock(db):
    await db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': IDENTITY_WRITE_LOCK})


async def scope(db, shop_code, lock=False):
    shop = await one(db, Shop, Shop.code == shop_code, lock)
    if shop is None or not shop.is_active or shop.platform != 'upgates':
        raise CostSyncError('fifo_cost_shop_not_found', 404)
    policy = await one(db, Policy, Policy.shop_id == shop.id, lock)
    order_policy = await one(db, OrderStockPolicy, OrderStockPolicy.shop_id == shop.id)
    warehouse_id = policy.warehouse_id if policy else order_policy.warehouse_id if order_policy else None
    warehouse = await one(db, Warehouse, Warehouse.id == warehouse_id) if warehouse_id else None
    defaults = await one(db, Defaults, Defaults.warehouse_id == warehouse_id) if warehouse_id else None
    values = {key: getattr(policy, key) if policy and getattr(policy, key) is not None
              else getattr(defaults, key) if defaults else value for key, value in DEFAULTS.items()}
    return shop, warehouse, policy, defaults, values


def blockers(shop, warehouse, policy, *, kind=None, require_write=True, automatic=False):
    errors = []
    if not warehouse or not warehouse.is_active:
        errors.append('fifo_cost_warehouse_required')
    if not policy:
        errors.append('fifo_cost_configuration_required')
    elif policy.target_fingerprint != target_fingerprint(shop.code):
        errors.append('fifo_cost_target_changed')
    if policy and ((kind == 'product' and not policy.product_cost_enabled)
                   or (kind == 'order' and not policy.order_cost_enabled)
                   or (kind is None and not policy.product_cost_enabled and not policy.order_cost_enabled)):
        errors.append('fifo_cost_scope_disabled')
    if automatic and policy and not policy.enabled:
        errors.append('fifo_cost_schedule_disabled')
    if require_write and not enabled():
        errors.append('fifo_cost_server_disabled')
    if policy and getattr(policy, 'retry_after_at', None) and policy.retry_after_at > now():
        errors.append('fifo_cost_retry_later')
    return errors


def warehouse_dto(warehouse, row):
    return {'warehouse_code': warehouse.code, 'revision': row.revision if row else 0,
            **{key: getattr(row, key) if row else value for key, value in DEFAULTS.items()}}


def policy_dto(policy, warehouse):
    fields = ('revision', 'enabled', 'product_cost_enabled', 'order_cost_enabled', 'interval_seconds',
              'batch_size', 'orders_since', 'next_run_at', 'retry_after_at', 'last_completed_at', 'last_error', 'scan_active')
    values = {key: getattr(policy, key) for key in fields} if policy else {
        key: 0 if key == 'revision' else False if key.endswith('_enabled') or key in ('enabled', 'scan_active') else None
        for key in fields}
    return {**values, 'warehouse_code': warehouse.code if warehouse else None}


def publication_dto(row):
    result = {key: deepcopy(getattr(row, key)) for key in ('id', 'kind', 'subject', 'status', 'source',
        'before', 'after', 'error', 'created_at', 'expires_at', 'attempt_started_at', 'verified_at', 'resolution')}
    document = row.document
    if document:
        before, desired = document['before']['values'], document['after']['values']
        result['remote_costs'] = {'prices_with_vat_yn': document['prices_with_vat_yn'], 'lines': []}
        if row.kind == 'product':
            result['remote_costs']['product'] = {'before': before['price_purchase'], 'desired': desired['price_purchase']}
        else:
            result['remote_costs']['lines'] = [{'line_key': line['line_key'], 'code': line['code'],
                'before': before[line['line_key']], 'desired': desired[line['line_key']]}
                for line in document['selected']]
    else:
        result['remote_costs'] = None
    return result


async def history(db, shop_code, offset=0, limit=50):
    shop, _, _, _, _ = await scope(db, shop_code)
    rows = (await db.scalars(select(Publication).where(Publication.shop_id == shop.id)
        .order_by(Publication.created_at.desc(), Publication.id).offset(offset).limit(limit))).all()
    total = await db.scalar(select(func.count()).select_from(Publication).where(Publication.shop_id == shop.id))
    return {'items': [publication_dto(row) for row in rows], 'total': total, 'offset': offset, 'limit': limit}


async def options(db, shop_code):
    shop, warehouse, policy, defaults, values = await scope(db, shop_code)
    warehouses = (await db.scalars(select(Warehouse).where(Warehouse.is_active.is_(True)).order_by(Warehouse.code))).all()
    records = (await db.scalars(select(Publication).where(Publication.shop_id == shop.id)
        .order_by(case((Publication.status.in_(('sending', 'uncertain')), 0), else_=1),
                  Publication.created_at.desc()).limit(20))).all()
    return {'shop': {'code': shop.code, 'name': shop.name},
        'warehouse': {'code': warehouse.code, 'name': warehouse.name} if warehouse else None,
        'warehouses': [{'code': row.code, 'name': row.name} for row in warehouses],
        'warehouse_settings': warehouse_dto(warehouse, defaults) if warehouse else None,
        'settings': policy_dto(policy, warehouse), 'effective': values if warehouse else None,
        'server_write_enabled': enabled(), 'blockers': blockers(shop, warehouse, policy),
        'publications': [publication_dto(row) for row in records]}


async def configure_warehouse(db, payload):
    warehouse = await one(db, Warehouse, Warehouse.code == payload.warehouse_code, True)
    if warehouse is None or not warehouse.is_active:
        raise CostSyncError('fifo_cost_warehouse_not_found', 404)
    row = await one(db, Defaults, Defaults.warehouse_id == warehouse.id, True)
    if payload.expected_revision != (row.revision if row else 0):
        raise CostSyncError('fifo_cost_settings_changed')
    if row is None:
        row = Defaults(warehouse_id=warehouse.id, revision=1)
        db.add(row)
    else:
        row.revision += 1
    for key in DEFAULTS:
        setattr(row, key, getattr(payload, key))
    await db.flush()
    result = warehouse_dto(warehouse, row)
    await db.commit()
    return result


async def configure(db, payload):
    await identity_lock(db)
    shop, _, policy, _, _ = await scope(db, payload.shop_code, True)
    warehouse = await one(db, Warehouse, Warehouse.code == payload.warehouse_code)
    if warehouse is None or not warehouse.is_active:
        raise CostSyncError('fifo_cost_warehouse_not_found', 404)
    fingerprint = target_fingerprint(shop.code)
    if not fingerprint:
        raise CostSyncError('fifo_cost_target_not_configured')
    if payload.expected_revision != (policy.revision if policy else 0):
        raise CostSyncError('fifo_cost_settings_changed')
    if payload.enabled and not (payload.product_cost_enabled or payload.order_cost_enabled):
        raise CostSyncError('fifo_cost_scope_disabled')
    new_scope = not policy or policy.warehouse_id != warehouse.id or policy.target_fingerprint != fingerprint
    activate_orders = payload.order_cost_enabled and (new_scope or not policy.order_cost_enabled)
    if policy is None:
        policy = Policy(shop_id=shop.id, warehouse_id=warehouse.id, revision=1,
                        target_fingerprint=fingerprint)
        db.add(policy)
    else:
        policy.revision += 1
    policy.warehouse_id, policy.target_fingerprint = warehouse.id, fingerprint
    for key in ('enabled', 'product_cost_enabled', 'order_cost_enabled', *DEFAULTS):
        setattr(policy, key, getattr(payload, key))
    if activate_orders:
        policy.orders_since = now()
    elif new_scope:
        policy.orders_since = None
    policy.scan_active = False
    policy.next_run_at = now() if policy.enabled else None
    # Previously queued work has not crossed the network and is safely cancelled.
    await db.execute(update(Publication).where(Publication.shop_id == shop.id, Publication.status == 'queued')
        .values(status='skipped', error='fifo_cost_settings_changed'))
    await db.flush()
    result = await options(db, shop.code)
    await db.commit()
    return result


async def product_target(db, shop_id, product_id, sku):
    mapping = await one(db, ShopProduct, (ShopProduct.shop_id == shop_id) & (ShopProduct.product_id == product_id))
    if mapping is None or not mapping.is_listed:
        raise CostSyncError('fifo_cost_mapping_missing')
    code = mapping.variant_code if mapping.is_variant else mapping.external_code
    parent = mapping.parent_code if mapping.is_variant else mapping.external_code
    if code != sku or not parent:
        raise CostSyncError('fifo_cost_mapping_alias')
    # Exact SKU is authoritative. Case-fold collisions must not select another
    # leaf, including Unicode case-fold expansions which SQL lower() misses.
    leaf = case((ShopProduct.is_variant.is_(True), ShopProduct.variant_code), else_=ShopProduct.external_code)
    candidates = (await db.execute(select(ShopProduct.product_id, ShopProduct.is_variant,
        ShopProduct.external_id, leaf.label('code')).where(
        ShopProduct.shop_id == shop_id,
        or_(func.lower(leaf) == code.lower(), leaf.op('~')(r'[^ -~]'),
            (ShopProduct.external_id == mapping.external_id) & (ShopProduct.is_variant == mapping.is_variant)
            if mapping.external_id else False)))).all()
    if len([row for row in candidates if row.code and row.code.casefold() == code.casefold()]) != 1:
        raise CostSyncError('fifo_cost_mapping_ambiguous')
    if mapping.external_id and len([row for row in candidates if row.external_id == mapping.external_id
                                   and row.is_variant == mapping.is_variant]) != 1:
        raise CostSyncError('fifo_cost_mapping_ambiguous')
    identity = {'code': code, 'parent_code': parent, 'variant_code': code if mapping.is_variant else None}
    if mapping.external_id:
        if not mapping.external_id.isdigit() or int(mapping.external_id) <= 0:
            raise CostSyncError('fifo_cost_mapping_invalid')
        identity['variant_id' if mapping.is_variant else 'product_id'] = int(mapping.external_id)
    return {'identity': identity, 'mapping_id': mapping.id, 'external_id': mapping.external_id}


async def local_source(db, kind, identifier, warehouse_id, shop_id):
    try:
        if kind == 'product':
            evidence = await projection.product_cost(db, identifier, warehouse_id)
            target = await product_target(db, shop_id, identifier, evidence['sku'])
        else:
            evidence = await projection.order_cost(db, identifier)
            if evidence['warehouse_id'] != warehouse_id or evidence['shop_id'] != shop_id:
                raise CostSyncError('fifo_cost_order_scope_changed')
            target = None
    except projection.FifoCostProjectionError as error:
        raise CostSyncError(error.code, error.status) from None
    return evidence, target


def identifier(row):
    return int(row.target_key.split(':', 1)[1])


async def read_remote(shop_code, kind, subject, target, fingerprint):
    if kind == 'product':
        remote = await source.read_product(shop_code, target['identity'], fingerprint)
        for key, value in target['identity'].items():
            if remote['identity'].get(key) != value:
                raise CostSyncError('fifo_cost_mapping_changed')
        return remote
    return await source.read_order(shop_code, subject, fingerprint)


async def remember_rate_limit(db, shop_code, fingerprint, error):
    if getattr(error, 'status', None) != 429:
        return
    await db.rollback()
    shop = await one(db, Shop, Shop.code == shop_code, True)
    if not shop or target_fingerprint(shop_code) != fingerprint:
        await db.commit()
        return
    policy = await one(db, Policy, Policy.shop_id == shop.id, True)
    delay = getattr(error, 'retry_after', None)
    until = now() + timedelta(seconds=min(604800, max(60, delay if type(delay) is int else 300)))
    if policy:
        policy.retry_after_at = max(policy.retry_after_at or until, until)
        policy.next_run_at = max(policy.next_run_at or until, until)
        policy.last_error = 'fifo_cost_retry_later'
    await db.commit()
    # Share pressure with order collection and all existing publication readers.
    from inventory_hub.services.stock_publication import _remember_rate_limit
    await _remember_rate_limit(db, shop_code, fingerprint, error)


async def network_ready(db, shop_code, fingerprint):
    if target_fingerprint(shop_code) != fingerprint:
        raise CostSyncError('fifo_cost_target_changed')
    shop_id = await db.scalar(select(Shop.id).where(Shop.code == shop_code))
    policy = await one(db, Policy, Policy.shop_id == shop_id)
    processing = await one(db, StockShopSettings, StockShopSettings.shop_id == shop_id)
    collector = await one(db, OrderCollectionSettings, OrderCollectionSettings.shop_id == shop_id)
    deadlines = [row for row in (
        policy.retry_after_at if policy else None,
        processing.processing_retry_after_at if processing else None,
        collector.retry_after_at if collector and collector.target_fingerprint == fingerprint
        and collector.last_error == 'order_collection_rate_limited' else None) if row is not None]
    deadline = max(deadlines) if deadlines else None
    if deadline and deadline > now():
        if policy:
            policy.retry_after_at = deadline
            policy.last_error = 'fifo_cost_retry_later'
        await db.commit()
        raise CostSyncError('fifo_cost_retry_later', 429)
    await db.commit()


async def network_read(db, shop_code, kind, subject, target, fingerprint):
    await network_ready(db, shop_code, fingerprint)
    try:
        return await read_remote(shop_code, kind, subject, target, fingerprint)
    except source.SourceError as error:
        await remember_rate_limit(db, shop_code, fingerprint, error)
        raise


def prepare_remote(remote, evidence):
    if evidence['kind'] == 'product':
        return source.prepare_product(remote, evidence['unit_cost'])
    if remote['identity'].get('uuid') != evidence['source_uuid']:
        raise CostSyncError('fifo_cost_order_identity_changed')
    if remote.get('source_lines') != evidence.get('source_lines'):
        raise CostSyncError('fifo_cost_order_lines_changed')
    return source.prepare_order(remote, [
        {'line_key': row['line_key'], 'code': row['code'], 'quantity': row['quantity'], 'unit_cost_net': row['unit_cost']}
        for row in evidence['lines']])


async def get_publication(db, publication_id):
    row = await one(db, Publication, Publication.id == publication_id)
    if row is None:
        raise CostSyncError('fifo_cost_publication_not_found', 404)
    return publication_dto(row)


async def order_preview(db, payload):
    request_id = str(payload.request_id or uuid4())
    existing = await one(db, Publication, Publication.id == request_id)
    if existing:
        shop_code = await db.scalar(select(Shop.code).where(Shop.id == existing.shop_id))
        if shop_code != payload.shop_code or existing.kind != 'order' or existing.subject != payload.order_number:
            raise CostSyncError('fifo_cost_request_conflict')
        return publication_dto(existing)
    shop, warehouse, policy, _, _ = await scope(db, payload.shop_code)
    errors = blockers(shop, warehouse, policy, kind='order', require_write=False)
    if errors:
        raise CostSyncError(errors[0])
    order = await one(db, ShopOrder, (ShopOrder.shop_id == shop.id) & (ShopOrder.external_id == payload.order_number))
    if not order:
        raise CostSyncError('fifo_cost_order_not_found', 404)
    evidence, target = await local_source(db, 'order', order.id, warehouse.id, shop.id)
    frozen = {'shop_id': shop.id, 'warehouse_id': warehouse.id, 'revision': policy.revision,
              'fingerprint': policy.target_fingerprint, 'order_id': order.id}
    await db.commit()
    remote = await network_read(db, payload.shop_code, 'order', payload.order_number, target, frozen['fingerprint'])
    document = prepare_remote(remote, evidence)
    await identity_lock(db)
    shop, warehouse, policy, _, _ = await scope(db, payload.shop_code, True)
    if (policy is None or policy.revision != frozen['revision']
            or blockers(shop, warehouse, policy, kind='order', require_write=False)):
        raise CostSyncError('fifo_cost_settings_changed')
    current, _ = await local_source(db, 'order', frozen['order_id'], warehouse.id, shop.id)
    if current['source_hash'] != evidence['source_hash']:
        raise CostSyncError('fifo_cost_source_changed')
    # Client UUID also makes lost preview responses recoverable without a new GET.
    existing = await one(db, Publication, Publication.id == request_id)
    if existing:
        if existing.shop_id != shop.id or existing.subject != payload.order_number or existing.kind != 'order':
            raise CostSyncError('fifo_cost_request_conflict')
        return publication_dto(existing)
    row = Publication(id=request_id, shop_id=shop.id, warehouse_id=warehouse.id, kind='order',
        target_key=f"order:{frozen['order_id']}", subject=payload.order_number, status='prepared', automatic=False,
        settings_revision=policy.revision, target_fingerprint=policy.target_fingerprint,
        source_hash=evidence['source_hash'], source=evidence, target=None, document=document,
        before=document.get('before'), after=None, created_at=now(), expires_at=now() + timedelta(minutes=30))
    db.add(row)
    await db.flush()
    result = publication_dto(row)
    await db.commit()
    return result


async def send(db, publication_id):
    await identity_lock(db)
    row = await one(db, Publication, Publication.id == publication_id, True)
    if row is None:
        raise CostSyncError('fifo_cost_publication_not_found', 404)
    if row.status != 'prepared':
        return publication_dto(row)  # Retry of same id never creates another attempt.
    shop_code = await db.scalar(select(Shop.code).where(Shop.id == row.shop_id))
    shop, warehouse, policy, _, _ = await scope(db, shop_code, True)
    errors = blockers(shop, warehouse, policy, kind=row.kind)
    if errors:
        raise CostSyncError(errors[0])
    if policy.revision != row.settings_revision or row.expires_at <= now():
        raise CostSyncError('fifo_cost_preview_expired')
    if await db.scalar(select(Publication.id).where(Publication.shop_id == row.shop_id,
            Publication.target_key == row.target_key, Publication.status.in_(FENCED)).limit(1)):
        raise CostSyncError('fifo_cost_target_inflight')
    row.status = 'queued'
    await db.flush()
    result = publication_dto(row)
    await db.commit()
    return result


async def enqueue(db, payload, *, automatic=False):
    await identity_lock(db)
    shop, warehouse, policy, _, _ = await scope(db, payload.shop_code, True)
    errors = blockers(shop, warehouse, policy, automatic=automatic)
    if errors:
        raise CostSyncError(errors[0])
    if not policy.scan_active:
        policy.scan_active, policy.scan_manual = True, not automatic
        policy.product_cursor = policy.order_cursor = 0
        policy.product_max = (await db.scalar(select(func.max(ShopProduct.product_id)).where(
            ShopProduct.shop_id == shop.id, ShopProduct.is_listed.is_(True)))) or 0
        policy.order_max = (await db.scalar(select(func.max(ShopOrder.id)).where(ShopOrder.shop_id == shop.id))) or 0
        policy.last_error = None
        policy.last_batch_at = now()
    await db.flush()
    result = await options(db, payload.shop_code)
    await db.commit()
    return result


async def cache_current(db, policy, key, evidence, target):
    # Any unresolved request wins over new desired costs, warehouses or config.
    if await db.scalar(select(Publication.id).where(Publication.shop_id == policy.shop_id,
        Publication.target_key == key, Publication.status.in_(FENCED)).limit(1)):
        return True
    if policy.scan_manual:
        return False
    latest = await db.scalar(select(Publication).where(Publication.shop_id == policy.shop_id,
        Publication.target_key == key).order_by(Publication.created_at.desc(), Publication.id.desc()).limit(1))
    return bool(latest and latest.status == 'verified' and latest.source_hash == evidence['source_hash']
        and latest.warehouse_id == policy.warehouse_id and latest.target_fingerprint == policy.target_fingerprint
        and latest.target == target)


async def scan_batch(db, shop_id):
    """Advance both catalogues on every pass; batch limits bound local evidence reads."""
    await identity_lock(db)
    shop_code = await db.scalar(select(Shop.code).where(Shop.id == shop_id))
    shop, warehouse, policy, _, values = await scope(db, shop_code, True)
    if not policy or not policy.scan_active:
        return
    errors = blockers(shop, warehouse, policy, automatic=not policy.scan_manual)
    if errors:
        policy.scan_active, policy.last_error = False, errors[0]
        policy.next_run_at = now() + timedelta(seconds=60)
        await db.commit()
        return
    # Each kind gets half the configured batch, minimum one. A batch of one
    # alternates kinds by completing products first then advancing orders.
    total_budget = values['batch_size']
    product_budget = max(1, total_budget // 2) if policy.product_cost_enabled else 0
    order_budget = total_budget - product_budget if policy.order_cost_enabled else 0
    if not policy.product_cost_enabled or policy.product_cursor >= policy.product_max:
        product_budget, order_budget = 0, total_budget if policy.order_cost_enabled else 0
    if not policy.order_cost_enabled or policy.order_cursor >= policy.order_max:
        order_budget, product_budget = 0, total_budget if policy.product_cost_enabled else 0
    if not policy.product_cost_enabled:
        policy.product_cursor = policy.product_max
    if not policy.order_cost_enabled:
        policy.order_cursor = policy.order_max
    products = (await db.execute(select(Product.id, Product.sku).join(ShopProduct, ShopProduct.product_id == Product.id)
        .where(ShopProduct.shop_id == shop.id, ShopProduct.is_listed.is_(True), Product.id > policy.product_cursor,
               Product.id <= policy.product_max).order_by(Product.id).limit(product_budget))).all() if product_budget else []
    orders = (await db.execute(select(ShopOrder.id, ShopOrder.external_id).where(ShopOrder.shop_id == shop.id,
        ShopOrder.id > policy.order_cursor, ShopOrder.id <= policy.order_max, ShopOrder.stock_state == 'issued',
        ShopOrder.stock_warehouse_id == warehouse.id, ShopOrder.stock_issued_at >= policy.orders_since)
        .order_by(ShopOrder.id).limit(order_budget))).all() if order_budget and policy.orders_since else []
    for kind, candidates in (('product', products), ('order', orders)):
        for candidate in candidates:
            key, subject = f'{kind}:{candidate[0]}', candidate[1]
            try:
                evidence, target = await local_source(db, kind, candidate[0], warehouse.id, shop.id)
            except CostSyncError as error:
                # Record one diagnostic until its reason changes; still retry
                # evidence locally on the next full pass, with no remote GET.
                last = await db.scalar(select(Publication).where(Publication.shop_id == shop.id,
                    Publication.target_key == key).order_by(Publication.created_at.desc()).limit(1))
                if not last or last.status != 'skipped' or last.error != error.code:
                    db.add(Publication(id=str(uuid4()), shop_id=shop.id, warehouse_id=warehouse.id,
                        kind=kind, target_key=key, subject=subject, status='skipped', automatic=not policy.scan_manual,
                        settings_revision=policy.revision, target_fingerprint=policy.target_fingerprint,
                        source_hash=digest({'key': key, 'error': error.code}), source={'kind': kind},
                        created_at=now(), error=error.code))
                continue
            if not await cache_current(db, policy, key, evidence, target):
                db.add(Publication(id=str(uuid4()), shop_id=shop.id, warehouse_id=warehouse.id, kind=kind,
                    target_key=key, subject=subject, status='queued', automatic=not policy.scan_manual,
                    settings_revision=policy.revision, target_fingerprint=policy.target_fingerprint,
                    source_hash=evidence['source_hash'], source=evidence, target=target, created_at=now()))
            await db.flush()
    if product_budget:
        policy.product_cursor = products[-1][0] if len(products) == product_budget else policy.product_max
    if order_budget:
        policy.order_cursor = orders[-1][0] if len(orders) == order_budget else policy.order_max
    policy.last_batch_at = now()
    if policy.product_cursor >= policy.product_max and policy.order_cursor >= policy.order_max:
        policy.scan_active = False
        policy.last_completed_at = now()
        policy.next_run_at = now() + timedelta(seconds=values['interval_seconds'])
    await db.commit()


async def check_current(db, row, shop_code):
    shop, warehouse, policy, _, _ = await scope(db, shop_code, True)
    errors = blockers(shop, warehouse, policy, kind=row.kind, automatic=row.automatic)
    if errors:
        raise CostSyncError(errors[0])
    if policy.revision != row.settings_revision or policy.warehouse_id != row.warehouse_id:
        raise CostSyncError('fifo_cost_settings_changed')
    if policy.target_fingerprint != row.target_fingerprint:
        raise CostSyncError('fifo_cost_target_changed')
    if row.expires_at and row.expires_at <= now():
        raise CostSyncError('fifo_cost_preview_expired')
    evidence, target = await local_source(db, row.kind, identifier(row), warehouse.id, shop.id)
    if evidence['source_hash'] != row.source_hash or target != row.target:
        raise CostSyncError('fifo_cost_source_changed')
    return evidence


async def mark_failure(db, publication_id, code, *, uncertain=False, no_put=False):
    await db.rollback()
    row = await one(db, Publication, Publication.id == publication_id, True)
    if row and row.status in ('queued', 'sending'):
        row.status = 'uncertain' if uncertain or (row.status == 'sending' and not no_put) else 'failed'
        row.error = code
        await db.commit()


async def process_publication(db, publication_id):
    """Worker-only execution. Ambiguous sending is never returned to queued."""
    sent = False
    try:
        row = await one(db, Publication, Publication.id == publication_id)
        if row is None or row.status != 'queued':
            return
        shop_code = await db.scalar(select(Shop.code).where(Shop.id == row.shop_id))
        await check_current(db, row, shop_code)
        frozen = {'kind': row.kind, 'subject': row.subject, 'target': deepcopy(row.target),
                  'fingerprint': row.target_fingerprint, 'source': deepcopy(row.source),
                  'document': deepcopy(row.document)}
        await db.commit()
        remote = await network_read(db, shop_code, frozen['kind'], frozen['subject'], frozen['target'], frozen['fingerprint'])
        document = frozen['document'] or prepare_remote(remote, frozen['source'])
        if frozen['document'] and not source.matches_before(document, remote):
            raise CostSyncError('fifo_cost_remote_changed')
        await identity_lock(db)
        row = await one(db, Publication, Publication.id == publication_id, True)
        if row is None or row.status != 'queued':
            await db.rollback()
            return
        await check_current(db, row, shop_code)
        if row.kind == 'product':
            await require_target_available(db, shop_code, document['identity']['parent_code'], fifo_publication_id=publication_id)
        row.document, row.before = document, document.get('before')
        if source.matches_after(document, remote):
            row.status, row.after, row.verified_at = 'verified', remote, now()
            await db.commit()
            return
        row.status, row.attempt_started_at = 'sending', now()
        await db.commit()  # The durable target claim precedes the only PUT.
        # The first GET happened before claiming the shared parent. Another
        # publisher may have finished in that gap; freeze again under our fence.
        fresh_remote = await network_read(db, shop_code, frozen['kind'], frozen['subject'], frozen['target'], frozen['fingerprint'])
        if not source.matches_before(document, fresh_remote):
            raise CostSyncError('fifo_cost_remote_changed')
        await identity_lock(db)
        row = await one(db, Publication, Publication.id == publication_id, True)
        if row is None or row.status != 'sending':
            await db.rollback()
            return
        await check_current(db, row, shop_code)
        await db.commit()
        sent = True
        await source.write_once(shop_code, document, frozen['fingerprint'])
        remote_after = await network_read(db, shop_code, frozen['kind'], frozen['subject'], frozen['target'], frozen['fingerprint'])
        row = await one(db, Publication, Publication.id == publication_id, True)
        row.after = remote_after
        if source.matches_after(document, remote_after):
            row.status, row.verified_at, row.error = 'verified', now(), None
        else:
            row.status, row.error = 'uncertain', 'fifo_cost_readback_mismatch'
        await db.commit()
    except (CostSyncError, source.SourceError, CatalogError) as error:
        await mark_failure(db, publication_id, error.code, uncertain=sent, no_put=not sent)
        if isinstance(error, source.SourceError) and error.status == 429:
            await remember_rate_limit(db, shop_code, frozen['fingerprint'], error)
    except Exception:
        await mark_failure(db, publication_id, 'fifo_cost_processing_failed', uncertain=sent, no_put=not sent)
        raise


async def recover(db):
    # Called only under the exclusive worker lock. Another API process cannot
    # still execute this worker's original request once that lock is acquired.
    await db.execute(update(Publication).where(Publication.status == 'sending')
                     .values(status='uncertain', error='fifo_cost_interrupted'))
    await db.commit()


async def resolve(db, publication_id, payload):
    row = await one(db, Publication, Publication.id == publication_id)
    if row is None:
        raise CostSyncError('fifo_cost_publication_not_found', 404)
    if row.status not in ('sending', 'uncertain'):
        return publication_dto(row)
    if row.attempt_started_at is None or row.attempt_started_at > now() - timedelta(minutes=5):
        raise CostSyncError('fifo_cost_resolution_too_early')
    shop_code = await db.scalar(select(Shop.code).where(Shop.id == row.shop_id))
    if target_fingerprint(shop_code) != row.target_fingerprint:
        raise CostSyncError('fifo_cost_target_changed')
    frozen = {'kind': row.kind, 'subject': row.subject, 'target': deepcopy(row.target),
              'fingerprint': row.target_fingerprint, 'document': deepcopy(row.document)}
    await db.commit()
    remote = await network_read(db, shop_code, frozen['kind'], frozen['subject'], frozen['target'], frozen['fingerprint'])
    await identity_lock(db)
    row = await one(db, Publication, Publication.id == publication_id, True)
    if row.status not in ('sending', 'uncertain'):
        return publication_dto(row)
    if row.target_fingerprint != frozen['fingerprint'] or target_fingerprint(shop_code) != frozen['fingerprint']:
        raise CostSyncError('fifo_cost_target_changed')
    matched = source.matches_after(frozen['document'], remote)
    row.after = remote
    row.status = 'verified' if matched else 'resolved'
    row.verified_at = now() if matched else None
    row.error = None if matched else 'fifo_cost_resolved_without_match'
    row.resolution = {'original_request_settled': payload.original_request_settled, 'note': payload.note,
                      'at': now().isoformat(), 'matched': matched}
    await db.flush()
    result = publication_dto(row)
    await db.commit()
    return result
