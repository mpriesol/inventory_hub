"""Regular eventual publication with an explicitly selected central stock authority.

No inventory ledger writes or maintenance holds. The authority contract requires
shop-side stock writers disabled; freshness gates reduce, but cannot eliminate,
order polling delay. Ambiguous absolute PUTs retain durable per-target fences.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4
from sqlalchemy import case, func, or_, select
from inventory_hub.db_models import Product, Shop, Warehouse
from inventory_hub.db_models_ext import ShopProduct, StockBalance
from inventory_hub.order_stock_models import OrderStockPolicy
from inventory_hub.order_collection_models import OrderCollectionSettings, OrderInboxEntry
from inventory_hub.order_processing_models import OrderProcessingJob
from inventory_hub.stock_publication_models import StockPublicationHold
from inventory_hub.stock_settings_models import StockShopSettings
from inventory_hub.stock_sync_models import StockSyncSettings as Policy, StockSyncWarehouseSettings as Defaults, StockSyncRun as Run, StockSyncItem as Item
from inventory_hub.settings import settings
from inventory_hub.services import stock_projection, stock_settings, stock_sync_source as source
from inventory_hub.services.order_collection import target_fingerprint

DEFAULTS = {'interval_seconds': 300, 'batch_size': 20, 'max_order_age_seconds': 900}
ACTIVE = ('queued', 'running')
FENCED = ('sending', 'uncertain')


class SyncError(Exception):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


def now():
    return datetime.now(timezone.utc)


def enabled():
    return bool(getattr(settings, 'STOCK_SYNC_WRITE_ENABLED', False))


async def one(db, model, condition, lock=False):
    query = select(model).where(condition).execution_options(populate_existing=True)
    return await db.scalar(query.with_for_update() if lock else query)


async def scope(db, shop_code, lock=False):
    shop = await one(db, Shop, Shop.code == shop_code, lock)
    if shop is None or not shop.is_active or shop.platform != 'upgates':
        raise SyncError('stock_sync_shop_not_found', 404)
    order_policy = await one(db, OrderStockPolicy, OrderStockPolicy.shop_id == shop.id)
    warehouse = await one(db, Warehouse, Warehouse.id == order_policy.warehouse_id, lock) if order_policy else None
    warehouse = warehouse if warehouse and warehouse.is_active else None
    policy = await one(db, Policy, Policy.shop_id == shop.id, lock)
    defaults = await one(db, Defaults, Defaults.warehouse_id == warehouse.id, lock) if warehouse else None
    values = {key: getattr(policy, key) if policy and getattr(policy, key) is not None
              else getattr(defaults, key) if defaults else value for key, value in DEFAULTS.items()}
    return shop, warehouse, policy, defaults, values


def warehouse_dto(warehouse, row):
    return {'warehouse_code': warehouse.code, 'revision': row.revision if row else 0,
            **{key: getattr(row, key) if row else value for key, value in DEFAULTS.items()}}


def policy_dto(policy):
    fields = ('revision', 'enabled', 'authorized', *DEFAULTS, 'authority_confirmed_at', 'next_run_at',
              'last_started_at', 'last_completed_at', 'last_error', 'retry_after_at')
    if policy:
        return {key: getattr(policy, key) for key in fields}
    return {key: 0 if key == 'revision' else False if key in ('enabled', 'authorized') else None for key in fields}


async def channels(db, warehouse_id):
    # Include any channel exposing known stock in this warehouse, even if that
    # channel was accidentally omitted from order-policy configuration.
    products = select(StockBalance.product_id).where(StockBalance.warehouse_id == warehouse_id)
    mapped = select(ShopProduct.shop_id).where(ShopProduct.product_id.in_(products), ShopProduct.is_listed.is_(True))
    rows = (await db.execute(select(Shop, OrderStockPolicy, StockShopSettings)
        .outerjoin(OrderStockPolicy, OrderStockPolicy.shop_id == Shop.id)
        .outerjoin(StockShopSettings, StockShopSettings.shop_id == Shop.id)
        .where(Shop.is_active.is_(True), Shop.platform == 'upgates',
               or_(OrderStockPolicy.warehouse_id == warehouse_id, Shop.id.in_(mapped))).order_by(Shop.id))).all()
    return [{'shop_id': shop.id, 'code': shop.code, 'target': target_fingerprint(shop.code),
             'policy_revision': policy.revision if policy else None,
             'warehouse_id': policy.warehouse_id if policy else None,
             'starts_at': policy.starts_at.isoformat() if policy else None,
             'processing_mode': processing.mode if processing else 'manual',
             'automation_starts_at': processing.automation_starts_at.isoformat() if processing and processing.automation_starts_at else None,
             'issue_starts_at': processing.issue_starts_at.isoformat() if processing and processing.issue_starts_at else None,
             'processing_policy_revision': processing.authorized_policy_revision if processing else None,
             'processing_target': processing.target_fingerprint if processing else None} for shop, policy, processing in rows]


async def blockers(db, shop, warehouse, policy, values, *, check_authority=True):
    result = []
    if not warehouse:
        return ['stock_sync_order_policy_required']
    if check_authority:
        if not policy or not policy.authorized:
            result.append('stock_sync_authority_required')
        elif policy.warehouse_id != warehouse.id or policy.target_fingerprint != target_fingerprint(shop.code):
            result.append('stock_sync_target_changed')
    if not enabled():
        result.append('stock_sync_server_disabled')
    if policy and policy.retry_after_at and policy.retry_after_at > now():
        result.append('stock_sync_retry_later')
    if await db.scalar(select(StockPublicationHold.id).where(StockPublicationHold.warehouse_id == warehouse.id,
                                                           StockPublicationHold.active.is_(True)).limit(1)):
        result.append('stock_sync_maintenance_active')
    peers = await channels(db, warehouse.id)
    if check_authority and policy and policy.authorized and policy.authority_snapshot != peers:
        result.append('stock_sync_authority_changed')
    cutoff = now() - timedelta(seconds=values['max_order_age_seconds'])
    for peer in peers:
        if peer['warehouse_id'] != warehouse.id or not peer['target']:
            result.append('stock_sync_channel_not_configured')
            continue
        collector = await one(db, OrderCollectionSettings, OrderCollectionSettings.shop_id == peer['shop_id'])
        if (collector is None or not collector.enabled or collector.target_fingerprint != peer['target']
                or collector.last_error or collector.last_completed_at is None or collector.last_completed_at < cutoff
                or collector.cursor_at < cutoff):
            result.append('stock_sync_orders_stale')
            continue
        config = await stock_settings.effective(db, peer['shop_id'])
        if not config['processing_ready'] or config['automation_starts_at'] is None:
            result.append('stock_sync_order_processing_required')
            continue
        pending = await db.scalar(select(OrderInboxEntry.id).outerjoin(OrderProcessingJob,
            (OrderProcessingJob.shop_id == OrderInboxEntry.shop_id) &
            (OrderProcessingJob.source_uuid == OrderInboxEntry.source_uuid)).where(
                OrderInboxEntry.shop_id == peer['shop_id'], OrderInboxEntry.created_at >= config['automation_starts_at'],
                or_(OrderInboxEntry.review_reason.is_not(None), OrderProcessingJob.id.is_(None),
                    OrderProcessingJob.status != 'completed',
                    OrderProcessingJob.observation_hash != OrderInboxEntry.observation_hash)).limit(1))
        if pending:
            result.append('stock_sync_orders_pending')
    return list(dict.fromkeys(result))


async def counts(db, run_id):
    rows = (await db.execute(select(Item.status, func.count()).where(Item.run_id == run_id).group_by(Item.status))).all()
    values = {status: amount for status, amount in rows}
    return {key: values.get(key, 0) for key in ('verified', 'skipped', 'failed', 'uncertain')} | {
        'pending': values.get('prepared', 0) + values.get('sending', 0)}


async def run_dto(db, row, detail=False, offset=0, limit=1000):
    data = {key: getattr(row, key) for key in ('id', 'trigger', 'status', 'started_at', 'completed_at', 'error')}
    data.update(shop_code=await db.scalar(select(Shop.code).where(Shop.id == row.shop_id)), counts=await counts(db, row.id),
                more_pending=row.cursor_product_id < row.max_product_id)
    if detail:
        items = (await db.scalars(select(Item).where(Item.run_id == row.id)
            .order_by(case((Item.status == 'uncertain', 0), else_=1), Item.id).offset(offset).limit(limit))).all()
        data['items'] = [{key: getattr(item, key) for key in ('id', 'sku', 'status', 'error', 'desired', 'before',
                        'after', 'attempt_started_at', 'verified_at')} for item in items]
        data['items_total'] = await db.scalar(select(func.count()).select_from(Item).where(Item.run_id == row.id))
        data['items_offset'], data['items_limit'] = offset, limit
        data['items_truncated'] = data['items_total'] > offset + len(items)
    return data


async def options(db, shop_code):
    shop, warehouse, policy, defaults, values = await scope(db, shop_code)
    runs = (await db.scalars(select(Run).where(Run.shop_id == shop.id).order_by(Run.started_at.desc()).limit(20))).all()
    return {'shop': {'code': shop.code, 'name': shop.name},
            'warehouse': {'code': warehouse.code, 'name': warehouse.name} if warehouse else None,
            'warehouse_settings': warehouse_dto(warehouse, defaults) if warehouse else None,
            'settings': policy_dto(policy), 'effective': values if warehouse else None,
            'blockers': await blockers(db, shop, warehouse, policy, values),
            'runs': [await run_dto(db, run) for run in runs], 'server_write_enabled': enabled()}


async def configure_warehouse(db, payload):
    warehouse = await one(db, Warehouse, Warehouse.code == payload.warehouse_code, True)
    if warehouse is None or not warehouse.is_active:
        raise SyncError('stock_sync_warehouse_not_found', 404)
    row = await one(db, Defaults, Defaults.warehouse_id == warehouse.id, True)
    if payload.expected_revision != (row.revision if row else 0):
        raise SyncError('stock_sync_settings_changed')
    if row is None:
        row = Defaults(warehouse_id=warehouse.id, revision=1)
        db.add(row)
    else:
        row.revision += 1
    for key in DEFAULTS:
        setattr(row, key, getattr(payload, key))
    row.updated_at = now()
    await db.flush()
    result = warehouse_dto(warehouse, row)
    await db.commit()
    return result


async def configure(db, payload):
    if payload.authorized:
        # Serialize authority activation with AI availability intents before
        # taking shop/warehouse locks (same identity-first lock order).
        from inventory_hub.services.merchandising_write_guard import require_availability_authority_available
        from inventory_hub.services.catalog import CatalogError
        try:
            await require_availability_authority_available(db, payload.shop_code)
        except CatalogError as error:
            raise SyncError(error.code, error.status) from None
    shop, warehouse, policy, _, values = await scope(db, payload.shop_code, True)
    if warehouse is None:
        raise SyncError('stock_sync_order_policy_required')
    if payload.expected_revision != (policy.revision if policy else 0):
        raise SyncError('stock_sync_settings_changed')
    if payload.enabled and not payload.authorized:
        raise SyncError('stock_sync_authority_required')
    peers = await channels(db, warehouse.id)
    reauthorize = payload.authorized and (policy is None or not policy.authorized or policy.authority_snapshot != peers
                     or policy.target_fingerprint != target_fingerprint(shop.code) or policy.warehouse_id != warehouse.id)
    if reauthorize and not (payload.hub_is_stock_authority and payload.external_stock_writers_disabled and payload.orders_reconciled):
        raise SyncError('stock_sync_authority_confirmation_required')
    if payload.authorized and (not peers or any(peer['warehouse_id'] != warehouse.id or not peer['target'] for peer in peers)):
        raise SyncError('stock_sync_channel_not_configured')
    if policy is None:
        policy = Policy(shop_id=shop.id, warehouse_id=warehouse.id, revision=1)
        db.add(policy)
    else:
        policy.revision += 1
    policy.enabled, policy.authorized = payload.enabled, payload.authorized
    for key in DEFAULTS:
        setattr(policy, key, getattr(payload, key))
    if reauthorize:
        policy.authority_snapshot, policy.authority_confirmed_at = peers, now()
        policy.warehouse_id, policy.target_fingerprint = warehouse.id, target_fingerprint(shop.code)
    policy.next_run_at = max(now(), policy.retry_after_at or now())
    await db.flush()
    result = await options(db, shop.code)
    await db.commit()
    return result


async def enqueue(db, payload, *, automatic=False):
    shop, warehouse, policy, _, values = await scope(db, payload.shop_code, True)
    reasons = await blockers(db, shop, warehouse, policy, values)
    if reasons:
        raise SyncError(reasons[0])
    if automatic and not policy.enabled:
        raise SyncError('stock_sync_disabled')
    active = await db.scalar(select(Run).where(Run.shop_id == shop.id, Run.status.in_(ACTIVE)).limit(1))
    selected = sorted(payload.skus) if payload.skus else None
    if active:
        if active.selected_skus != selected:
            raise SyncError('stock_sync_run_busy')
        result = await run_dto(db, active, True)
        await db.commit()
        return result
    # Manual request cannot circumvent shared API cooldown, but may immediately
    # follow a successful previous pass to publish an operator's local edit.
    if policy.retry_after_at and policy.retry_after_at > now():
        raise SyncError('stock_sync_retry_later', 429)
    maximum = await db.scalar(select(func.max(Product.id)).join(ShopProduct, ShopProduct.product_id == Product.id)
                             .where(ShopProduct.shop_id == shop.id, ShopProduct.is_listed.is_(True))) or 0
    row = Run(id=str(uuid4()), shop_id=shop.id, warehouse_id=warehouse.id, target_fingerprint=policy.target_fingerprint,
              settings_revision=policy.revision, trigger='automatic' if automatic else 'manual', status='queued',
              selected_skus=selected, cursor_product_id=0, max_product_id=maximum, started_at=now())
    db.add(row)
    policy.last_started_at, policy.last_error = now(), None
    await db.flush()
    result = await run_dto(db, row, True)
    await db.commit()
    return result


async def get_run(db, identifier, offset=0, limit=1000):
    row = await one(db, Run, Run.id == identifier)
    if row is None:
        raise SyncError('stock_sync_run_not_found', 404)
    return await run_dto(db, row, True, offset=offset, limit=limit)


async def assert_no_inflight(db, warehouse_id):
    if await db.scalar(select(Item.id).where(Item.warehouse_id == warehouse_id, Item.status.in_(FENCED)).limit(1)):
        raise SyncError('stock_sync_inflight')


def matches(observed, desired):
    return all(observed.get(key) == value for key, value in desired.items())


async def desired(db, shop_code, sku):
    projection = await stock_projection.preview(db, shop_code, [sku])
    row = projection['rows'][0]
    if row['errors']:
        raise SyncError(row['errors'][0])
    from inventory_hub.services.supplier_availability import project
    offers = await project(db, [row['product_id']])
    offer = offers.get(row['product_id'], {})
    label = 'SKLADOM' if Decimal(row['qty_available']) > 0 else (
        offer.get('label', 'do 5 dní') if offer.get('fresh') and offer.get('available') is True else 'overíme')
    value = {'stock': row['qty_available'], 'availability': label, 'can_add_to_basket_yn': True}
    source._fields(value)
    return row['target'], value


async def recover(db):
    # Called only while the process-wide advisory worker lock is owned. A read
    # matching desired data never proves a detached PUT cannot arrive later.
    sending = (await db.scalars(select(Item).where(Item.status == 'sending').with_for_update())).all()
    for item in sending:
        item.status, item.error = 'uncertain', 'stock_sync_interrupted_write'
    await db.commit()


async def finish(db, run, policy, values, error=None):
    summary = await counts(db, run.id)
    run.status = 'uncertain' if summary['uncertain'] else 'failed' if error else 'partial' if summary['failed'] or summary['skipped'] else 'completed'
    run.completed_at, run.error = now(), error
    policy.last_completed_at, policy.last_error = now(), error or ('stock_sync_items_need_review' if run.status != 'completed' else None)
    policy.next_run_at = max(now() + timedelta(seconds=values['interval_seconds']), policy.retry_after_at or now())
    await db.commit()


async def process_item(db, item_id):
    item = await one(db, Item, Item.id == item_id)
    if item is None or item.status != 'prepared':
        await db.commit()
        return
    run = await one(db, Run, Run.id == item.run_id)
    shop_code = await db.scalar(select(Shop.code).where(Shop.id == run.shop_id))
    # Rollback expires every ORM instance, including the caller's run. Keep
    # scalar intent data for error recovery before any path can roll back.
    fingerprint = run.target_fingerprint
    started = False
    try:
        shop, warehouse, policy, _, values = await scope(db, shop_code, True)
        reasons = await blockers(db, shop, warehouse, policy, values)
        if reasons:
            raise SyncError(reasons[0])
        if policy.revision != run.settings_revision or run.target_fingerprint != policy.target_fingerprint or run.warehouse_id != warehouse.id:
            raise SyncError('stock_sync_settings_changed')
        if await db.scalar(select(Item.id).where(Item.shop_id == shop.id, Item.sku == item.sku, Item.status.in_(FENCED)).limit(1)):
            raise SyncError('stock_sync_target_uncertain')
        target, value = await desired(db, shop_code, item.sku)
        await db.commit()
        observation = await source.read(shop_code, target, fingerprint)
        # Recompute after the remote GET; local receipts/reservations are not
        # frozen during network work and must not use an older desired value.
        shop, warehouse, policy, _, values = await scope(db, shop_code, True)
        reasons = await blockers(db, shop, warehouse, policy, values)
        if reasons:
            raise SyncError(reasons[0])
        if policy.revision != run.settings_revision:
            raise SyncError('stock_sync_settings_changed')
        current_target, value = await desired(db, shop_code, item.sku)
        if target != current_target:
            raise SyncError('stock_sync_identity_changed')
        item = await one(db, Item, Item.id == item_id, True)
        item.target, item.desired, item.before = observation['identity'], value, observation
        if matches(observation, value):
            item.status, item.after, item.verified_at = 'verified', observation, now()
            await db.commit()
            return
        item.status, item.attempt_started_at = 'sending', now()
        await db.commit()  # durable fence before any possible PUT dispatch
        started = True
        await source.write_once(shop_code, item.target, value, fingerprint)
        after = await source.read(shop_code, item.target, fingerprint)
        item = await one(db, Item, Item.id == item_id, True)
        item.after = after
        if matches(after, value):
            item.status, item.verified_at, item.error = 'verified', now(), None
        else:
            item.status, item.error = 'uncertain', 'stock_sync_readback_mismatch'
        await db.commit()
    except (SyncError, source.SourceError, stock_settings.SettingsError, stock_projection.StockProjectionError) as error:
        await db.rollback()
        item = await one(db, Item, Item.id == item_id, True)
        item.status = 'uncertain' if started else 'skipped'
        item.error = error.code
        if getattr(error, 'status', None) == 429:
            policy = await one(db, Policy, Policy.shop_id == item.shop_id, True)
            delay = getattr(error, 'retry_after', None)
            policy.retry_after_at = now() + timedelta(seconds=min(604800, max(60, delay if type(delay) is int else 300)))
            # Share rate-limit pressure with collector/maintenance readers.
            from inventory_hub.services.stock_publication import _remember_rate_limit
            await db.commit()
            await _remember_rate_limit(db, shop_code, fingerprint, error)
        await db.commit()
    except Exception:
        await db.rollback()
        item = await one(db, Item, Item.id == item_id, True)
        item.status, item.error = ('uncertain' if started else 'failed'), 'stock_sync_worker_error'
        await db.commit()


async def process_run(db, identifier):
    run_id = identifier
    run = await one(db, Run, Run.id == run_id)
    if run is None or run.status not in ACTIVE:
        await db.commit()
        return
    shop_code = await db.scalar(select(Shop.code).where(Shop.id == run.shop_id))
    shop, warehouse, policy, _, values = await scope(db, shop_code, True)
    reasons = await blockers(db, shop, warehouse, policy, values)
    if reasons or policy.revision != run.settings_revision:
        await finish(db, run, policy, values, (reasons or ['stock_sync_settings_changed'])[0])
        return
    run.status = 'running'
    pending = (await db.scalars(select(Item.id).where(Item.run_id == run.id, Item.status == 'prepared')
                               .order_by(Item.id).limit(values['batch_size']))).all()
    if not pending:
        if run.selected_skus is not None:
            existing = set((await db.scalars(select(Item.sku).where(Item.run_id == run.id))).all())
            selected = [sku for sku in run.selected_skus if sku not in existing][:values['batch_size']]
            exhausted = len(existing) + len(selected) == len(run.selected_skus)
            run.cursor_product_id = run.max_product_id if exhausted else 0
        else:
            rows = (await db.execute(select(Product.id, Product.sku).join(ShopProduct, ShopProduct.product_id == Product.id)
                .where(ShopProduct.shop_id == shop.id, ShopProduct.is_listed.is_(True), Product.id > run.cursor_product_id,
                       Product.id <= run.max_product_id).distinct().order_by(Product.id).limit(values['batch_size']))).all()
            selected = [row.sku for row in rows]
            run.cursor_product_id = rows[-1].id if rows else run.max_product_id
        for sku in selected:
            item = Item(run_id=run.id, shop_id=shop.id, warehouse_id=warehouse.id, sku=sku, status='prepared')
            db.add(item)
            await db.flush()
            pending.append(item.id)
    await db.commit()
    for item_id in pending:
        await process_item(db, item_id)
    # A skipped/failed item rolls back its transaction and expires this run.
    # Reload using the frozen scalar key, never run.id from the expired object.
    run = await one(db, Run, Run.id == run_id, True)
    run.last_batch_at = now()
    policy = await one(db, Policy, Policy.shop_id == run.shop_id)
    if run.cursor_product_id >= run.max_product_id:
        await finish(db, run, policy, values)
    else:
        run.status = 'queued'
        await db.commit()


async def resolve(db, identifier, payload):
    item = await one(db, Item, Item.id == identifier, True)
    if item is None:
        raise SyncError('stock_sync_item_not_found', 404)
    if item.status != 'uncertain':
        raise SyncError('stock_sync_item_not_uncertain')
    run = await one(db, Run, Run.id == item.run_id)
    shop_code = await db.scalar(select(Shop.code).where(Shop.id == item.shop_id))
    if run.target_fingerprint != target_fingerprint(shop_code):
        raise SyncError('stock_sync_target_changed')
    target, value, run_id, fingerprint = item.target, item.desired, run.id, run.target_fingerprint
    await db.commit()
    try:
        observed = await source.read(shop_code, target, fingerprint)
    except source.SourceError as error:
        raise SyncError(error.code, error.status) from None
    item = await one(db, Item, Item.id == identifier, True)
    if item.status != 'uncertain':
        raise SyncError('stock_sync_item_not_uncertain')
    item.after = observed
    item.status = 'verified' if matches(observed, value) else 'failed'
    item.error = None if item.status == 'verified' else 'stock_sync_resolved_mismatch'
    item.verified_at = now() if item.status == 'verified' else None
    item.resolution = {'external_requests_finished': True, 'confirmed_at': now().isoformat()}
    await db.flush()
    run = await one(db, Run, Run.id == run_id, True)
    if run.status == 'uncertain' and (await counts(db, run.id))['uncertain'] == 0:
        summary = await counts(db, run.id)
        run.status = 'partial' if summary['failed'] or summary['skipped'] else 'completed'
    result = await run_dto(db, run, True)
    await db.commit()
    return result
