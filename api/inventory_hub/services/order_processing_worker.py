"""Bounded automatic local processing, serialized across API replicas."""
import asyncio
import logging
from types import SimpleNamespace

from sqlalchemy import select, text

from inventory_hub import database
from inventory_hub.database import get_session_context
from inventory_hub.order_processing_models import OrderProcessingJob
from inventory_hub.stock_settings_models import StockShopSettings
from inventory_hub.services import order_processing as service
from inventory_hub.services.order_stock_source import SourceError, load_source
from inventory_hub.services.order_stock import OrderStockError
from inventory_hub.services.order_stock_ledger import OrderStockError as LedgerError
from inventory_hub.services.stock_settings import SettingsError


logger = logging.getLogger(__name__)
WORKER_LOCK = 691432113
EXPECTED_ERRORS = (service.ProcessingError, SourceError, OrderStockError, LedgerError, SettingsError)


async def process(plan):
    """The source read owns no DB locks; the final context commits everything."""
    try:
        async with asyncio.timeout(plan["values"]["run_timeout_seconds"]):
            async with get_session_context() as db:
                fetched = await load_source(db, SimpleNamespace(id=plan["shop_id"], code=plan["shop_code"]),
                    plan["order_number"], expected_target_fingerprint=plan["target_fingerprint"])
            async with get_session_context() as db:
                return await service.finish_job(db, plan, fetched)
    except asyncio.CancelledError:
        # Durable running state is recovered by the next global lock owner.
        raise
    except EXPECTED_ERRORS as error:
        async with get_session_context() as db:
            await service.fail_job(db, plan, error.code, getattr(error, "retry_after", None))
    except Exception as error:
        logger.warning("Local order processing failed (%s)", type(error).__name__)
        async with get_session_context() as db:
            await service.fail_job(db, plan, "order_processing_source_unavailable")
    return None


async def cycle():
    if database._engine is None:
        await database.init_db()
    async with database._engine.connect() as connection:
        locked = await connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": WORKER_LOCK})
        if not locked:
            return False
        try:
            async with get_session_context() as db:
                await service.recover(db)
                shops = (await db.scalars(select(StockShopSettings.shop_id).where(
                    StockShopSettings.mode.in_(["reserve", "fulfill"])).order_by(StockShopSettings.shop_id))).all()
            worked = False
            for shop_id in shops:
                try:
                    async with get_session_context() as db:
                        await service.enqueue(db, shop_id)
                        _, _, config = await service._context(db, shop_id)
                        identifiers = (await db.scalars(select(OrderProcessingJob.id).where(
                            OrderProcessingJob.shop_id == shop_id, OrderProcessingJob.status != "running",
                            OrderProcessingJob.next_attempt_at <= service.now())
                            .order_by(OrderProcessingJob.next_attempt_at, OrderProcessingJob.id)
                            .limit(config["values"]["processing_batch_size"]))).all()
                    for identifier in identifiers:
                        async with get_session_context() as db:
                            plan = await service.start_job(db, identifier)
                        if plan is not None:
                            await process(plan)
                            worked = True
                except EXPECTED_ERRORS:
                    # Disabled, paused or no longer authorized shops do not
                    # repeatedly fetch orders or change existing reservations.
                    continue
            return worked
        finally:
            await connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": WORKER_LOCK})
            await connection.commit()


async def run():
    while True:
        try:
            worked = await cycle()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning("Local order processor unavailable (%s)", type(error).__name__)
            worked = False
        await asyncio.sleep(1 if worked else 5)
