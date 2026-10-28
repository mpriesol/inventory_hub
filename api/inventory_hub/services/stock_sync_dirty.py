"""Dispatch changed leaves through the existing delta publisher.

Triggers only record intent. The regular publisher's authority, completeness,
baseline and ambiguous-write gates still decide whether any network write runs.
An acknowledgement covers only the generation attached to that particular run.
"""
from datetime import timedelta
from sqlalchemy import select, update
from inventory_hub.db_models import Product, Shop
from inventory_hub.stock_sync_dirty_models import StockSyncDirty as Dirty
from inventory_hub.stock_sync_models import StockSyncRun as Run, StockSyncItem as Item, StockSyncSettings as Policy
from inventory_hub.stock_sync_types import SyncRunInput


async def settle(db):
    from inventory_hub.services import stock_sync as sync
    at = sync.now()
    rows = (await db.execute(select(Dirty, Run.status, Run.error)
        .join(Run, Run.id == Dirty.run_id)
        .where(Run.status.not_in(sync.ACTIVE), Dirty.next_attempt_at <= at)
        .order_by(Dirty.shop_id, Dirty.product_id, Dirty.warehouse_id).limit(100)
        .with_for_update(of=Dirty))).all()
    for row, status, error in rows:
        item = await db.scalar(select(Item).where(Item.run_id == row.run_id, Item.sku == row.dispatched_sku))
        if item is not None and item.status == 'verified':
            row.acknowledged_generation = max(row.acknowledged_generation, row.dispatched_generation)
            row.last_error = None
            row.next_attempt_at = at
        elif item is not None and item.status in sync.FENCED:
            # Keep the same run reference until explicit uncertain resolution.
            row.last_error = item.error or 'stock_sync_target_uncertain'
            row.next_attempt_at = at + timedelta(seconds=60)
            continue
        else:
            row.last_error = (item.error if item is not None else error) or 'stock_sync_items_need_review'
            # Failures remain dirty, but a persistent remote identity error
            # must not cause one request every worker tick.
            row.next_attempt_at = at + timedelta(seconds=3600)
        row.run_id = row.dispatched_generation = row.dispatched_sku = None
    await db.commit()


async def bind(db, run, wakeups):
    """Called before enqueue commits; no network and no premature clean flag."""
    for product_id, warehouse_id, generation, sku in wakeups:
        result = await db.execute(update(Dirty).where(
            Dirty.shop_id == run.shop_id, Dirty.product_id == product_id,
            Dirty.warehouse_id == warehouse_id, Dirty.run_id.is_(None),
            Dirty.generation >= generation).values(
                run_id=run.id, dispatched_generation=generation, dispatched_sku=sku,
                last_error=None))
        if result.rowcount != 1:
            from inventory_hub.services.stock_sync import SyncError
            raise SyncError('stock_sync_dirty_changed')


async def dispatch(db):
    """At most one small selected-SKU run per tick, sharing the worker lock."""
    from inventory_hub.services import stock_sync as sync
    if not sync.enabled():
        return False
    busy = select(Run.id).where(Run.shop_id == Dirty.shop_id, Run.status.in_(sync.ACTIVE)).exists()
    row = (await db.execute(select(Dirty.shop_id, Shop.code)
        .join(Shop, Shop.id == Dirty.shop_id)
        .join(Policy, (Policy.shop_id == Dirty.shop_id) & (Policy.warehouse_id == Dirty.warehouse_id))
        .where(Dirty.generation > Dirty.acknowledged_generation, Dirty.run_id.is_(None),
               Dirty.next_attempt_at <= sync.now(), ~busy,
               Policy.enabled.is_(True), Policy.authorized.is_(True), Policy.protocol == 'delta_v1',
               Shop.is_active.is_(True), Shop.platform == 'upgates')
        .order_by(Dirty.next_attempt_at, Dirty.shop_id).limit(1))).first()
    if row is None:
        return False
    shop_id, shop_code = row
    wakeups = (await db.execute(select(Dirty.product_id, Dirty.warehouse_id, Dirty.generation, Product.sku)
        .join(Product, Product.id == Dirty.product_id)
        .join(Policy, (Policy.shop_id == Dirty.shop_id) & (Policy.warehouse_id == Dirty.warehouse_id))
        .where(Dirty.shop_id == shop_id, Dirty.generation > Dirty.acknowledged_generation,
               Dirty.run_id.is_(None), Dirty.next_attempt_at <= sync.now())
        .order_by(Dirty.product_id).limit(100))).all()
    try:
        await sync.enqueue(db, SyncRunInput(shop_code=shop_code, skus=[r.sku for r in wakeups], confirmed=True),
                           automatic=True, wakeups=wakeups)
    except sync.SyncError as error:
        await db.rollback()
        # A readiness problem must retain every generation, including one
        # arriving while enqueue inspected the channel completeness gates.
        delay = 5 if error.code in ('stock_sync_orders_pending', 'stock_sync_order_notifications_pending',
                                    'stock_sync_run_busy') else 60
        await db.execute(update(Dirty).where(Dirty.shop_id == shop_id, Dirty.run_id.is_(None),
            Dirty.product_id.in_([r.product_id for r in wakeups])).values(
                next_attempt_at=sync.now() + timedelta(seconds=delay), last_error=error.code))
        await db.commit()
        return False
    return True
