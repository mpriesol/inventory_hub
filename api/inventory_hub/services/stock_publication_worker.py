"""Only explicitly queued maintenance batches are sent; no scheduled publication."""
import asyncio
import logging
from sqlalchemy import select, text
from inventory_hub import database
from inventory_hub.database import get_session_context
from inventory_hub.stock_publication_models import StockPublicationBatch
from inventory_hub.services import stock_publication as service

logger = logging.getLogger(__name__)
WORKER_LOCK = 691432114


async def cycle():
    if database._engine is None:
        await database.init_db()
    async with database._engine.connect() as connection:
        locked = await connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": WORKER_LOCK})
        if not locked:
            return False
        await connection.commit()  # Session advisory lock survives this commit.
        try:
            async with get_session_context() as db:
                await service.recover(db)
                identifier = await db.scalar(select(StockPublicationBatch.id).where(
                    StockPublicationBatch.status == "queued").order_by(StockPublicationBatch.queued_at,
                    StockPublicationBatch.id).limit(1))
            if identifier:
                async with get_session_context() as db:
                    await service.process_batch(db, identifier)
            return identifier is not None
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
            logger.warning("Stock publication worker unavailable (%s)", type(error).__name__)
            worked = False
        await asyncio.sleep(1 if worked else 5)
