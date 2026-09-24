"""One supplier download at a time across replicas, outside DB transactions."""
import asyncio
import logging

from sqlalchemy import or_, select, text

from inventory_hub import database
from inventory_hub.database import get_session_context
from inventory_hub.services import supplier_availability as service
from inventory_hub.services.supplier_availability_source import AvailabilityError, fetch
from inventory_hub.supplier_availability_models import SupplierAvailabilitySettings

logger = logging.getLogger(__name__)
WORKER_LOCK = 691432214


async def cycle():
    if database._engine is None:
        await database.init_db()
    async with database._engine.connect() as connection:
        locked = await connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": WORKER_LOCK})
        if not locked:
            return False
        try:
            # End the transaction while retaining the session lock for the bounded network read.
            await connection.commit()
            async with get_session_context() as db:
                interrupted = (await db.execute(select(SupplierAvailabilitySettings.supplier_id,
                    SupplierAvailabilitySettings.running_run_id).where(SupplierAvailabilitySettings.running_run_id.is_not(None)))).all()
                for supplier_id, run_id in interrupted:
                    await service.fail_run(db, supplier_id, run_id, "supplier_availability_interrupted")
                supplier_ids = (await db.scalars(select(SupplierAvailabilitySettings.supplier_id).where(
                    or_(SupplierAvailabilitySettings.enabled.is_(True), SupplierAvailabilitySettings.manual_requested_at.is_not(None)),
                    SupplierAvailabilitySettings.next_run_at <= service.now())
                    .order_by(SupplierAvailabilitySettings.next_run_at, SupplierAvailabilitySettings.supplier_id).limit(20))).all()
                plan = None
                for supplier_id in supplier_ids:
                    plan = await service.start_run(db, supplier_id)
                    if plan is not None:
                        break
            if plan is None:
                return False
            try:
                async with asyncio.timeout(360):
                    records, size = await asyncio.to_thread(fetch, plan)
                async with get_session_context() as db:
                    await service.accept_run(db, plan, records, size)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                code = error.code if isinstance(error, AvailabilityError) else "supplier_availability_source_unavailable"
                logger.warning("Supplier availability pass failed (%s)", code)
                async with get_session_context() as db:
                    await service.fail_run(db, plan["supplier_id"], plan["run_id"], code)
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
            logger.warning("Supplier availability worker unavailable (%s)", type(error).__name__)
            worked = False
        await asyncio.sleep(1 if worked else 5)
