"""Single worker, fair bounded passes and one durable cost publication per tick."""
import asyncio
import logging
from datetime import timedelta
from sqlalchemy import func, or_, select, text
from inventory_hub import database
from inventory_hub.database import get_session_context
from inventory_hub.db_models import Shop
from inventory_hub.fifo_cost_models import FifoCostSettings as Policy, FifoCostPublication as Publication
from inventory_hub.fifo_cost_types import CostRunInput
from inventory_hub.services import fifo_cost_sync as service

logger = logging.getLogger(__name__)
WORKER_LOCK = 691432119


async def cycle():
    if database._engine is None:
        await database.init_db()
    async with database._engine.connect() as connection:
        if not await connection.scalar(text('SELECT pg_try_advisory_lock(:key)'), {'key': WORKER_LOCK}):
            return False
        await connection.commit()
        try:
            async with get_session_context() as db:
                await service.recover(db)
                due = (await db.execute(select(Policy.shop_id, Shop.code).join(Shop, Shop.id == Policy.shop_id)
                    .where(Policy.enabled.is_(True), Policy.scan_active.is_(False),
                           or_(Policy.retry_after_at.is_(None), Policy.retry_after_at <= service.now()),
                           or_(Policy.next_run_at.is_(None), Policy.next_run_at <= service.now()))
                    .order_by(Policy.next_run_at.nullsfirst(), Policy.shop_id).limit(1))).first()
                if due:
                    due_shop_id, due_code = due.shop_id, due.code
                    try:
                        await service.enqueue(db, CostRunInput(shop_code=due_code, confirmed=True), automatic=True)
                    except service.CostSyncError as error:
                        await db.rollback()
                        policy = await service.one(db, Policy, Policy.shop_id == due_shop_id, True)
                        policy.last_error = error.code
                        policy.next_run_at = service.now() + timedelta(seconds=60)
                        await db.commit()
                shop_id = await db.scalar(select(Policy.shop_id).where(Policy.scan_active.is_(True),
                    or_(Policy.retry_after_at.is_(None), Policy.retry_after_at <= service.now()))
                    .order_by(Policy.last_batch_at.nullsfirst(), Policy.shop_id).limit(1))
                # Do not fill an unbounded queue while the remote service is slow.
                queued = await db.scalar(select(func.count()).select_from(Publication)
                    .join(Policy, Policy.shop_id == Publication.shop_id).where(Publication.status == 'queued',
                        or_(Policy.retry_after_at.is_(None), Policy.retry_after_at <= service.now())))
                if shop_id is not None and queued < 100:
                    await service.scan_batch(db, shop_id)
                publication_id = await db.scalar(select(Publication.id).join(Policy, Policy.shop_id == Publication.shop_id)
                    .where(Publication.status == 'queued',
                           or_(Policy.retry_after_at.is_(None), Policy.retry_after_at <= service.now()))
                    .order_by(Publication.created_at, Publication.id).limit(1))
            if publication_id:
                async with get_session_context() as db:
                    await service.process_publication(db, publication_id)
            return shop_id is not None or publication_id is not None
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
            logger.warning('FIFO cost worker unavailable (%s)', type(error).__name__)
            worked = False
        await asyncio.sleep(1 if worked else 5)
