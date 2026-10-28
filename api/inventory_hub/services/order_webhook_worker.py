"""Serialize notification reads and registration reconciliation across replicas."""
import asyncio
import logging
from sqlalchemy import text
from inventory_hub import database
from inventory_hub.database import get_session_context
from inventory_hub.services import order_webhooks as service
from inventory_hub.services.order_collection_source import CollectionSourceError

logger = logging.getLogger(__name__)
WORKER_LOCK = 691432235


async def process_batch(plan):
    try:
        async with asyncio.timeout(90):
            entries = await service.fetch_current(plan)
        async with get_session_context() as db:
            await service.finish_batch(db, plan, entries=entries)
    except asyncio.CancelledError:
        raise
    except (service.WebhookError, CollectionSourceError) as error:
        async with get_session_context() as db:
            await service.finish_batch(db, plan, error=error.code, retry_after=getattr(error, 'retry_after', None))
    except Exception as error:
        logger.warning('Order webhook read failed (%s)', type(error).__name__)
        async with get_session_context() as db:
            await service.finish_batch(db, plan, error='order_webhook_source_unavailable')


async def register(plan):
    loop = asyncio.get_running_loop()
    async def persist_intent(event):
        async with asyncio.timeout(5):
            async with get_session_context() as db:
                await service.record_create_intent(db, plan, event)
    def before_create(event):
        asyncio.run_coroutine_threadsafe(persist_intent(event), loop).result(timeout=10)
    # Cancellation must not release the cross-process lock while a synchronous
    # network write keeps running in its thread. Each transport call is bounded;
    # preserve the durable create intent if the process is forcibly terminated.
    task = asyncio.create_task(asyncio.to_thread(service.reconcile_registration, plan, before_create))
    try:
        try:
            events = await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await task
            except Exception:
                pass
            raise
        async with get_session_context() as db:
            await service.finish_registration(db, plan, events=events)
    except asyncio.CancelledError:
        raise
    except service.WebhookError as error:
        async with get_session_context() as db:
            await service.finish_registration(db, plan, error=error.code)
    except Exception as error:
        logger.warning('Order webhook registration failed (%s)', type(error).__name__)
        async with get_session_context() as db:
            await service.finish_registration(db, plan, error='order_webhook_registration_unverified')


async def cycle():
    if database._engine is None:
        await database.init_db()
    async with database._engine.connect() as connection:
        locked = await connection.scalar(text('SELECT pg_try_advisory_lock(:key)'), {'key': WORKER_LOCK})
        if not locked:
            return False
        try:
            async with get_session_context() as db:
                await service.recover(db)
                plan = await service.start_batch(db)
            # Process known changes before routine registration verification.
            if plan:
                await process_batch(plan)
                return True
            async with get_session_context() as db:
                registration = await service.start_registration(db)
            if registration:
                await register(registration)
                return True
            return False
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
            logger.warning('Order webhook worker unavailable (%s)', type(error).__name__)
            worked = False
        await asyncio.sleep(1 if worked else 2)
