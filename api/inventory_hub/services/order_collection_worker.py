"""Bounded read-only collection in the API process, serialized across replicas."""
import asyncio
from datetime import datetime
import logging

from sqlalchemy import select, text

from inventory_hub import database
from inventory_hub.database import get_session_context
from inventory_hub.order_collection_models import OrderCollectionSettings, OrderCollectionRun
from inventory_hub.services import order_collection as service
from inventory_hub.services.order_collection_source import CollectionSourceError, load_changed_page


logger = logging.getLogger(__name__)
WORKER_LOCK = 691432112
MAX_PAGES = 100
RUN_TIMEOUT = 180


async def collect(plan):
    seen_uuids, seen_numbers = set(), set()
    for deleted in (False, True):
        expected, count, previous_time = None, 0, None
        for page in range(1, MAX_PAGES + 1):
            async with get_session_context() as db:
                await service.check_run(db, plan["id"])
            data = await load_changed_page(plan["shop_code"], created_from=plan["created_from"],
                changed_from=plan["changed_from"], created_to=plan["created_to"], page=page, deleted=deleted,
                expected_target_fingerprint=plan["target_fingerprint"])
            metadata = (data["number_of_pages"], data["number_of_items"])
            if data["number_of_pages"] > MAX_PAGES:
                raise service.CollectionError("order_collection_backlog_limit")
            if expected is not None and metadata != expected:
                raise service.CollectionError("order_collection_unstable_scan")
            expected = metadata
            for entry in data["entries"]:
                updated = datetime.fromisoformat(entry["updated_at"])
                if entry["uuid"] in seen_uuids or entry["order_number"] in seen_numbers or (previous_time and updated < previous_time):
                    raise service.CollectionError("order_collection_unstable_scan")
                seen_uuids.add(entry["uuid"])
                seen_numbers.add(entry["order_number"])
                previous_time = updated
            count += len(data["entries"])
            async with get_session_context() as db:
                await service.save_page(db, plan["id"], data["entries"])
            if not data["has_more"]:
                if count != data["number_of_items"]:
                    raise service.CollectionError("order_collection_unstable_scan")
                break
        else:
            raise service.CollectionError("order_collection_backlog_limit")
    async with get_session_context() as db:
        await service.complete_run(db, plan["id"])


async def cycle():
    if database._engine is None:
        await database.init_db()
    # A dedicated connection keeps this lock across the small page transactions.
    async with database._engine.connect() as connection:
        locked = await connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": WORKER_LOCK})
        if not locked:
            return False
        try:
            async with get_session_context() as db:
                interrupted = (await db.scalars(select(OrderCollectionRun.id).where(OrderCollectionRun.status == "running"))).all()
                for identifier in interrupted:
                    await service.fail_run(db, identifier, "order_collection_interrupted")
                shop_id = await db.scalar(select(OrderCollectionSettings.shop_id).where(
                    OrderCollectionSettings.enabled.is_(True), OrderCollectionSettings.next_poll_at <= service.now()
                ).order_by(OrderCollectionSettings.next_poll_at, OrderCollectionSettings.shop_id).limit(1))
                plan = await service.start_run(db, shop_id) if shop_id is not None else None
            if plan is None:
                return False
            try:
                async with asyncio.timeout(RUN_TIMEOUT):
                    await collect(plan)
            except (CollectionSourceError, service.CollectionError) as error:
                async with get_session_context() as db:
                    await service.fail_run(db, plan["id"], error.code, getattr(error, "retry_after", None))
            except asyncio.CancelledError:
                # Durable running state is recovered by the next lock owner.
                raise
            except Exception as error:
                logger.warning("Order collection paused (%s)", type(error).__name__)
                async with get_session_context() as db:
                    await service.fail_run(db, plan["id"], "order_collection_source_unavailable")
            return True
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
            logger.warning("Order collector unavailable (%s)", type(error).__name__)
            worked = False
        await asyncio.sleep(1 if worked else 5)
