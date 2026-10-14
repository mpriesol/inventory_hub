"""Origin-aware checkpoints; no read/absolute-write cycle in normal operation."""
from datetime import timedelta
from decimal import Decimal
from sqlalchemy import func, select, text
from inventory_hub.db_models import Product, Shop
from inventory_hub.stock_delta_models import StockSyncCheckpoint as Checkpoint, StockSyncBaselineEvent
from inventory_hub.stock_sync_models import StockSyncItem as Item, StockSyncRun as Run
from inventory_hub.stock_publication_models import StockPublicationHold
from inventory_hub.services import stock_native
from inventory_hub.services.product_identity import IDENTITY_WRITE_LOCK

PROTOCOL = 'delta_v1'
AUDIT_SECONDS = 21600


def increment(local, native, old_local, old_native):
    """Native effects are signed: a normal order contributes a negative debt."""
    return str(int(Decimal(local) - Decimal(old_local) - Decimal(native) + Decimal(old_native)))


def metadata(value):
    return {key: value.get(key) for key in ('availability', 'can_add_to_basket_yn')}


async def checkpoint(db, shop_id, sku, lock=False):
    query = select(Checkpoint).where(Checkpoint.shop_id == shop_id, Checkpoint.sku == sku)
    if lock:
        query = query.with_for_update()
    return await db.scalar(query.execution_options(populate_existing=True))


async def current(db, shop, warehouse, sku):
    from inventory_hub.services import stock_sync as sync
    target, value = await sync.desired(db, shop.code, sku)
    product_id = await db.scalar(select(Product.id).where(Product.sku == sku))
    try:
        native = await stock_native.projection(db, shop.id, warehouse.id, product_id, checked=True)
    except stock_native.NativeError as error:
        raise sync.SyncError(error.code, error.status) from None
    return {'target': target, 'value': value, 'product_id': product_id, 'native': str(native)}


def check_binding(cp, state, warehouse_id, fingerprint):
    from inventory_hub.services import stock_sync as sync
    if cp is None:
        raise sync.SyncError('stock_sync_baseline_required')
    if (cp.warehouse_id != warehouse_id or cp.product_id != state['product_id']
            or cp.target_fingerprint != fingerprint
            or any(cp.target.get(key) != value for key, value in state['target'].items())):
        raise sync.SyncError('stock_sync_baseline_changed')


def payload(cp, state):
    return {'stock_increment': increment(state['value']['stock'], state['native'], cp.local_quantity, cp.native_quantity),
            **metadata(state['value'])}


def needs_read(cp, state, at):
    value = payload(cp, state)
    return (value['stock_increment'] != '0' or metadata(value) != cp.metadata_values
            or cp.last_audit_at is None or cp.last_audit_at <= at - timedelta(seconds=AUDIT_SECONDS)
            or bool((cp.last_observation or {}).get('audit_mismatch')))


def snapshot(cp, state):
    return {'checkpoint_revision': cp.revision, 'local_quantity': state['value']['stock'],
            'native_quantity': state['native'], 'metadata_values': metadata(state['value'])}


def advance(cp, frozen, at):
    cp.local_quantity = Decimal(frozen['local_quantity'])
    cp.native_quantity = Decimal(frozen['native_quantity'])
    cp.metadata_values = frozen['metadata_values']
    cp.revision += 1
    cp.updated_at = at


async def baseline(db, request):
    """Read-only remote verification under an explicitly asserted maintenance hold."""
    from inventory_hub.services import stock_sync as sync
    async def locked():
        await db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': IDENTITY_WRITE_LOCK})
        shop, warehouse, policy, _, _ = await sync.scope(db, request.shop_code, True)
        if warehouse is None:
            raise sync.SyncError('stock_sync_order_policy_required')
        hold = await db.scalar(select(StockPublicationHold).where(StockPublicationHold.id == request.hold_id,
            StockPublicationHold.shop_id == shop.id, StockPublicationHold.warehouse_id == warehouse.id,
            StockPublicationHold.active.is_(True)).with_for_update())
        if hold is None:
            raise sync.SyncError('stock_sync_baseline_hold_required')
        await sync.assert_no_inflight(db, warehouse.id)
        from inventory_hub.order_collection_models import OrderCollectionSettings
        for peer in await sync.channels(db, warehouse.id):
            collector = await db.get(OrderCollectionSettings, peer['shop_id'])
            if (peer['warehouse_id'] != warehouse.id or not peer['target'] or collector is None
                    or collector.target_fingerprint != peer['target'] or collector.last_error
                    or collector.cursor_at is None or collector.cursor_at < hold.created_at
                    or collector.last_completed_at is None or collector.last_completed_at < hold.created_at):
                raise sync.SyncError('stock_sync_baseline_orders_unsettled')
            config = await sync.stock_settings.effective(db, peer['shop_id'])
            if (config['automation_starts_at'] is None
                    or await sync.has_unaccounted_orders(db, peer['shop_id'], warehouse.id, config)):
                raise sync.SyncError('stock_sync_baseline_orders_unsettled')
        try:
            native_plan = await stock_native.maintenance_plan(db, shop.id, warehouse.id, request.skus)
            await stock_native.coverage(db, shop.id, warehouse.id, maintenance_plan=native_plan)
        except stock_native.NativeError as error:
            raise sync.SyncError(error.code, error.status) from None
        fingerprint = sync.target_fingerprint(shop.code)
        if not fingerprint:
            raise sync.SyncError('stock_sync_target_changed')
        states = {sku: await current(db, shop, warehouse, sku) for sku in request.skus}
        return shop, warehouse, fingerprint, states, native_plan

    shop, warehouse, fingerprint, states, native_plan = await locked()
    shop_id, warehouse_id = shop.id, warehouse.id
    await db.commit()
    try:
        observations = await sync.source.read_many(request.shop_code, [state['target'] for state in states.values()], fingerprint)
    except sync.source.SourceError as error:
        raise sync.SyncError(error.code, error.status) from None
    shop, warehouse, fresh_fingerprint, fresh, fresh_plan = await locked()
    if (shop.id, warehouse.id, fresh_fingerprint, fresh, fresh_plan) != (shop_id, warehouse_id, fingerprint, states, native_plan):
        raise sync.SyncError('stock_sync_baseline_changed')
    # Validate every row before storing any checkpoint; the caller owns rollback.
    for sku, state in fresh.items():
        observed = observations[state['target']['code']]['observation']
        if observed['stock'] != state['value']['stock']:
            raise sync.SyncError('stock_sync_baseline_mismatch')
    try:
        await stock_native.apply_maintenance(db, fresh_plan, request.hold_id)
    except stock_native.NativeError as error:
        raise sync.SyncError(error.code, error.status) from None
    fresh = {sku: await current(db, shop, warehouse, sku) for sku in request.skus}
    at = sync.now()
    audit_rows = []
    for sku, state in fresh.items():
        observed = observations[state['target']['code']]['observation']
        cp = await checkpoint(db, shop_id, sku, True)
        if cp is None:
            cp = Checkpoint(shop_id=shop_id, sku=sku, revision=1)
            db.add(cp)
        else:
            cp.revision += 1
        audit_rows.append({'sku': sku, 'revision': cp.revision, 'target': observed['identity'],
                           'local_quantity': state['value']['stock'], 'native_quantity': state['native']})
        cp.product_id, cp.warehouse_id = state['product_id'], warehouse_id
        cp.target_fingerprint, cp.target = fingerprint, observed['identity']
        cp.local_quantity, cp.native_quantity = Decimal(state['value']['stock']), Decimal(state['native'])
        cp.metadata_values, cp.hold_id = metadata(observed), request.hold_id
        cp.baseline_at = cp.updated_at = cp.last_audit_at = at
        cp.last_observation = observed
    db.add(StockSyncBaselineEvent(shop_id=shop_id, hold_id=request.hold_id, created_at=at,
        evidence={'target_fingerprint': fingerprint, 'rows': audit_rows,
                  'external_writers_paused': True, 'orders_reconciled': True, 'native_plan': fresh_plan}))
    await db.flush()
    count = await db.scalar(select(func.count()).select_from(Checkpoint).where(
        Checkpoint.shop_id == shop_id, Checkpoint.target_fingerprint == fingerprint))
    await db.commit()
    return {'shop_code': request.shop_code, 'baseline_count': count, 'skus': request.skus}


async def process_item(db, item_id, prefetched=None):
    from inventory_hub.services import stock_sync as sync
    item = await sync.one(db, Item, Item.id == item_id)
    if item is None or item.status != 'prepared':
        await db.commit()
        return
    run = await sync.one(db, Run, Run.id == item.run_id)
    shop_code = await db.scalar(select(Shop.code).where(Shop.id == run.shop_id))
    fingerprint, run_id, started = run.target_fingerprint, run.id, False

    async def locked():
        await db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': IDENTITY_WRITE_LOCK})
        shop, warehouse, policy, _, values = await sync.scope(db, shop_code, True)
        reasons = await sync.blockers(db, shop, warehouse, policy, values)
        if reasons:
            raise sync.SyncError(reasons[0])
        active = await sync.one(db, Run, Run.id == run_id)
        if (active.protocol != PROTOCOL or policy.revision != active.settings_revision
                or fingerprint != policy.target_fingerprint or active.warehouse_id != warehouse.id):
            raise sync.SyncError('stock_sync_settings_changed')
        row = await sync.one(db, Item, Item.id == item_id, True)
        if row.status != 'prepared':
            raise sync.SyncError('stock_sync_item_changed')
        if await db.scalar(select(Item.id).where(Item.shop_id == shop.id, Item.sku == row.sku,
                                                 Item.status.in_(sync.FENCED)).limit(1)):
            raise sync.SyncError('stock_sync_target_uncertain')
        state = await current(db, shop, warehouse, row.sku)
        cp = await checkpoint(db, shop.id, row.sku, True)
        check_binding(cp, state, warehouse.id, fingerprint)
        return row, cp, state

    try:
        item, cp, state = await locked()
        value, frozen = payload(cp, state), snapshot(cp, state)
        if not needs_read(cp, state, sync.now()):
            # Own-shop native order and matching Hub reservation may cancel
            # exactly. Advance both counters without any network request.
            item.target, item.desired, item.delta_snapshot = cp.target, value, frozen
            item.status, item.verified_at = 'verified', sync.now()
            item.after = {'no_remote_write': True, 'stock_increment': '0'}
            advance(cp, frozen, sync.now())
            await db.commit()
            return
        target = cp.target
        await db.commit()
        if prefetched is not None and 'error' in prefetched:
            raise prefetched['error']
        cached = prefetched.get('result') if prefetched else None
        if (cached is not None and 0 <= sync.monotonic() - cached['observed_monotonic'] <= sync.BATCH_OBSERVATION_SECONDS):
            observed = cached['observation']
            if observed['identity'] != target:
                raise sync.SyncError('stock_sync_identity_changed')
        else:
            observed = await sync.source.read(shop_code, target, fingerprint)
        item, cp, state = await locked()
        if cp.target != observed['identity']:
            raise sync.SyncError('stock_sync_identity_changed')
        value, frozen = payload(cp, state), snapshot(cp, state)
        item.target, item.desired, item.before, item.delta_snapshot = cp.target, value, observed, frozen
        if value['stock_increment'] == '0' and metadata(observed) == metadata(value):
            # A periodic read only reports drift; it never corrects it with an
            # absolute PUT or guesses which concurrent order caused it.
            cp.last_audit_at, cp.last_observation = sync.now(), observed
            if observed['stock'] != state['value']['stock']:
                cp.last_observation = {**observed, 'audit_mismatch': True}
                item.status, item.error = 'skipped', 'stock_sync_audit_mismatch'
            else:
                item.status, item.after, item.verified_at = 'verified', observed, sync.now()
                advance(cp, frozen, sync.now())
            await db.commit()
            return
        item.status, item.attempt_started_at = 'sending', sync.now()
        await db.commit()
        started = True
        # A single signed increment commutes with a native order decrement;
        # stock is deliberately absent. Never repeat after an ambiguous result.
        await sync.source.write_delta_once(shop_code, item.target, value, fingerprint)
        item = await sync.one(db, Item, Item.id == item_id, True)
        cp = await checkpoint(db, item.shop_id, item.sku, True)
        if cp is None or cp.revision != frozen['checkpoint_revision']:
            raise sync.SyncError('stock_sync_checkpoint_changed')
        advance(cp, frozen, sync.now())
        cp.last_audit_at, cp.last_observation = sync.now(), observed
        item.status, item.verified_at, item.error = 'verified', sync.now(), None
        item.after = {'acknowledged': True, 'stock_increment': value['stock_increment']}
        if value['stock_increment'] == '0' and observed['stock'] != state['value']['stock']:
            cp.last_observation = {**observed, 'audit_mismatch': True}
            item.status, item.error = 'skipped', 'stock_sync_audit_mismatch'
        await db.commit()
    except Exception as error:
        await db.rollback()
        item = await sync.one(db, Item, Item.id == item_id, True)
        if item is None:
            return
        was_sent = started and getattr(error, 'request_sent', None) is not False
        item.status = 'uncertain' if was_sent else 'skipped' if hasattr(error, 'code') else 'failed'
        item.error = getattr(error, 'code', 'stock_sync_worker_error')
        if getattr(error, 'status', None) == 429:
            await sync.remember_rate_limit(db, item.shop_id, shop_code, fingerprint, error)
        await db.commit()


async def resolve(db, item, run, request):
    from inventory_hub.services import stock_sync as sync
    if request.outcome is None or not request.evidence or len(request.evidence.strip()) < 8:
        raise sync.SyncError('stock_sync_delta_resolution_required', 422)
    cp = await checkpoint(db, item.shop_id, item.sku, True)
    frozen = item.delta_snapshot
    if cp is None or not frozen or cp.revision != frozen['checkpoint_revision']:
        raise sync.SyncError('stock_sync_checkpoint_changed')
    if request.outcome == 'applied':
        advance(cp, frozen, sync.now())
        item.status, item.verified_at, item.error = 'verified', sync.now(), None
    else:
        item.status, item.error = 'failed', 'stock_sync_delta_not_applied'
    item.resolution = {'external_requests_finished': True, 'outcome': request.outcome,
                       'evidence': request.evidence.strip(), 'confirmed_at': sync.now().isoformat()}
    await db.flush()
    if run.status == 'uncertain' and (await sync.counts(db, run.id))['uncertain'] == 0:
        summary = await sync.counts(db, run.id)
        run.status = 'partial' if summary['failed'] or summary['skipped'] else 'completed'
    result = await sync.run_dto(db, run, True)
    await db.commit()
    return result
