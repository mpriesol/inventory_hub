"""Deterministic Sloger facts, shared by import and manual availability refresh.

No model, AI budget, supplier configuration, catalog or stock write occurs here.
Callers own durable work/progress; this service owns one protected source read.
"""
import asyncio
from contextlib import asynccontextmanager

from sqlalchemy import text

from inventory_hub import database
from inventory_hub.services import sloger_b2b, sloger_credentials
from inventory_hub.services.catalog import CatalogError
from inventory_hub.sloger_probe_types import SlogerBatchResult


SUPPLIER_ACCESS_LOCK = 691432262
MAX_BATCH_ITEMS = sloger_b2b.MAX_BATCH_ITEMS


@asynccontextmanager
async def _supplier_access():
    if database._engine is None:
        await database.init_db()
    async with database._engine.connect() as connection:
        locked = await connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": SUPPLIER_ACCESS_LOCK})
        if not locked:
            raise CatalogError("sloger_b2b_busy", "Another Sloger lookup is running; this batch has not started", 409)
        try:
            # Release the transaction, retain only the dedicated session lock.
            await connection.commit()
            yield
        finally:
            try:
                await connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": SUPPLIER_ACCESS_LOCK})
                await connection.commit()
            except BaseException:
                # Never return a connection still owning this lock to the pool.
                await connection.invalidate()
                raise


async def _wait_for_batch(username: str, password: str, skus: list[str]) -> SlogerBatchResult:
    # Cancelling asyncio.to_thread does not stop its HTTP thread. Keep the
    # supplier lock until that bounded thread actually finishes, even if the
    # caller is cancelled, so a second worker cannot overlap its source access.
    task = asyncio.create_task(sloger_b2b.lookup_products(username, password, skus))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()  # Retrieve a failure without logging its input.
        raise


async def lookup_products(skus: list[str]) -> SlogerBatchResult:
    """Read at most ten exact SKUs with the saved login and one B2B session.

`sloger_b2b_busy` means no supplier request began; a caller may defer that same
chunk. Other failures must remain visible, without automatic failed retries.
"""
    credentials = sloger_credentials.load()
    username, password = credentials.username, credentials.password.get_secret_value()
    sloger_b2b.validate_skus(username, password, skus)
    try:
        async with _supplier_access():
            return await _wait_for_batch(username, password, list(skus))
    except CatalogError:
        raise
    except Exception:
        # Database/transport exceptions must not expose DSNs, form inputs or HTML.
        raise CatalogError("sloger_b2b_unavailable", "Sloger B2B lookup could not be completed", 503) from None
