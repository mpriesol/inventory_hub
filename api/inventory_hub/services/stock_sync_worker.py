"""One regular publisher across API processes; each pass advances bounded batches."""
import asyncio
import logging
from sqlalchemy import func, or_, select, text
from inventory_hub import database
from inventory_hub.database import get_session_context
from inventory_hub.db_models import Shop
from inventory_hub.stock_sync_models import StockSyncSettings, StockSyncRun
from inventory_hub.stock_sync_types import SyncRunInput
from inventory_hub.services import stock_sync as service

logger = logging.getLogger(__name__)
WORKER_LOCK = 691432116


async def cycle():
    if database._engine is None:
        await database.init_db()
    async with database._engine.connect() as connection:
        locked = await connection.scalar(text('SELECT pg_try_advisory_lock(:key)'), {'key': WORKER_LOCK})
        if not locked:
            return False
        await connection.commit()
        try:
            async with get_session_context() as db:
                await service.recover(db)
                # Create at most one due run each tick, including while another
                # shop is running. Round-robin batches prevent a large POS
                # umbrella catalogue from starving the other shop.
                busy = select(StockSyncRun.id).where(StockSyncRun.shop_id == StockSyncSettings.shop_id,
                                                    StockSyncRun.status.in_(service.ACTIVE)).exists()
                row = (await db.execute(select(StockSyncSettings.shop_id, Shop.code).join(Shop, Shop.id == StockSyncSettings.shop_id)
                    .where(StockSyncSettings.enabled.is_(True), StockSyncSettings.authorized.is_(True), ~busy,
                           or_(StockSyncSettings.next_run_at.is_(None), StockSyncSettings.next_run_at <= service.now()))
                    .order_by(StockSyncSettings.next_run_at.nullsfirst(), Shop.id).limit(1))).first()
                if row:
                    try:
                        await service.enqueue(db, SyncRunInput(shop_code=row.code, confirmed=True), automatic=True)
                    except service.SyncError as error:
                        await db.rollback()
                        policy = await service.one(db, StockSyncSettings, StockSyncSettings.shop_id == row.shop_id, True)
                        from datetime import timedelta
                        policy.last_error = error.code
                        policy.next_run_at = max(service.now() + timedelta(seconds=60), policy.retry_after_at or service.now())
                        await db.commit()
                identifier = await db.scalar(select(StockSyncRun.id).where(StockSyncRun.status.in_(service.ACTIVE))
                    .order_by(func.coalesce(StockSyncRun.last_batch_at, StockSyncRun.started_at), StockSyncRun.id).limit(1))
            if identifier:
                async with get_session_context() as db:
                    await service.process_run(db, identifier)
            return identifier is not None
        finally:
            await connection.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': WORKER_LOCK})
            await connection.commit()


async def run():
    while True:
        try:
            worked = await cycle()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning('Regular stock worker unavailable (%s)', type(error).__name__)
            worked = False
        await asyncio.sleep(1 if worked else 5)
