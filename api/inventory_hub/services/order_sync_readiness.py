"""Read-only proof that waiting orders already account for current own stock.

A successful partial reservation remains a processing retry so a later receipt
can fill its shortage. That commercial wait is not itself an unknown stock
effect. Publication may proceed only while re-planning the saved reservation
would leave every allocation unchanged. Callers preparing a stock write hold
the warehouse lock through this proof and their final stock projection.
"""
from copy import deepcopy
from datetime import datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import and_, or_, select

from inventory_hub.db_models import Shop
from inventory_hub.db_models_ext import ShopOrder
from inventory_hub.order_collection_models import OrderInboxEntry
from inventory_hub.order_processing_models import OrderProcessingJob
from inventory_hub.services import order_stock_ledger as ledger
from inventory_hub.services.order_stock_source import resolve_lines


async def backorder_accounted(db, entry, job, warehouse_id, config):
    """Fail closed unless the latest applied backorder still covers free units."""
    if (job is None or job.status != 'retry' or job.error != 'order_processing_backorder'
            or entry.deleted or entry.review_reason is not None
            or job.shop_id != entry.shop_id or job.inbox_id != entry.id
            or job.source_uuid != entry.source_uuid or job.order_number != entry.order_number
            or job.observation_hash != entry.observation_hash
            or job.configuration_hash != config['configuration_hash']
            or not job.source_hash or job.last_completed_at is None):
        return False
    result = job.result
    if (not isinstance(result, dict) or result.get('action') != 'reserve'
            or result.get('stock_state') != 'reserved'
            or type(result.get('order_id')) is not int or type(result.get('revision')) is not int):
        return False
    order = await db.scalar(select(ShopOrder).where(ShopOrder.id == result['order_id'])
                            .execution_options(populate_existing=True))
    if (order is None or order.shop_id != entry.shop_id
            or order.external_id != entry.order_number or order.stock_source_uuid != entry.source_uuid
            or order.stock_warehouse_id != warehouse_id or order.stock_state != 'reserved'
            or order.stock_revision != result['revision'] or order.stock_source_hash != job.source_hash
            or not isinstance(order.stock_snapshot, dict)):
        return False
    snapshot = order.stock_snapshot
    try:
        # A detail read can be newer than the collector header, never older.
        updated = datetime.fromisoformat(snapshot['updated_at'])
        if (snapshot['uuid'] != entry.source_uuid or snapshot['order_number'] != entry.order_number
                or datetime.fromisoformat(snapshot['created_at']) != entry.created_at
                or updated < entry.updated_at
                or (updated == entry.updated_at and
                    (snapshot['status_id'] != entry.status_id or snapshot['origin'] != entry.origin))):
            return False
        shop = await db.get(Shop, entry.shop_id)
        if shop is None or not shop.is_active or shop.platform != 'upgates':
            return False
        current = deepcopy(snapshot)
        current['lines'] = await resolve_lines(db, shop, current)
        identity = lambda source: sorted((line['line_key'], line.get('product_id'), line.get('sku'))
                                         for line in source['lines'])
        if identity(current) != identity(snapshot):
            return False
        projected = await ledger.plan_order(db, order, current, 'reserve', warehouse_id, lock=False)
        return (projected['ready']
                and all(Decimal(effect['old_allocation']) == Decimal(effect['allocation'])
                        for effect in projected['effects'])
                and any(Decimal(line['shortage']) > 0 for line in projected['lines']))
    except (ledger.LedgerError, KeyError, TypeError, ValueError, InvalidOperation):
        return False


async def has_unaccounted_orders(db, shop_id, warehouse_id, config):
    """Check every post-activation order; never let one safe row hide another."""
    joined = select(OrderInboxEntry, OrderProcessingJob).outerjoin(OrderProcessingJob,
        (OrderProcessingJob.shop_id == OrderInboxEntry.shop_id)
        & (OrderProcessingJob.source_uuid == OrderInboxEntry.source_uuid)).where(
            OrderInboxEntry.shop_id == shop_id,
            OrderInboxEntry.created_at >= config['automation_starts_at']).execution_options(populate_existing=True)
    backorder = and_(OrderProcessingJob.status == 'retry',
                     OrderProcessingJob.error == 'order_processing_backorder')
    accounted_status = or_(OrderProcessingJob.status == 'completed', backorder)
    unknown = joined.with_only_columns(OrderInboxEntry.id).where(or_(
        OrderInboxEntry.review_reason.is_not(None), OrderInboxEntry.deleted.is_(True),
        OrderProcessingJob.id.is_(None), accounted_status.is_not(True),
        OrderProcessingJob.observation_hash != OrderInboxEntry.observation_hash)).limit(1)
    if await db.scalar(unknown) is not None:
        return True
    exception = and_(OrderProcessingJob.status == 'completed',
                     OrderProcessingJob.result['action'].astext == 'resolve_exception')
    waiting = await db.stream(joined.where(or_(backorder, exception)).order_by(OrderInboxEntry.id)
                              .execution_options(yield_per=100))
    try:
        async for entry, job in waiting:
            if job.status == 'completed':
                from inventory_hub.services.order_exceptions import current_resolution
                if not await current_resolution(db, job, entry, config, warehouse_id):
                    return True
            elif not await backorder_accounted(db, entry, job, warehouse_id, config):
                return True
    finally:
        await waiting.close()
    return False
