"""One read-only export worker across API processes, with durable daily work."""

import asyncio
from datetime import timedelta
import logging
import re
from uuid import uuid4
from sqlalchemy import select, text, or_, and_
from inventory_hub import database
from inventory_hub.database import get_session_context
from inventory_hub.atomer_availability_models import (
    AtomerAvailabilitySettings as Settings,
    AtomerAvailabilityRun as Run,
)
from inventory_hub.atomer_availability_types import RunInput, ExportInput
from inventory_hub.services import atomer_availability as service

logger = logging.getLogger(__name__)
WORKER_LOCK = 691432355


async def process(identifier):
    async with get_session_context() as db:
        run = await db.get(Run, identifier, with_for_update=True)
        if run.status == "ready" and run.trigger == "daily":
            needs_source = False
        elif run.status in ("queued", "fetching"):
            run.status = "fetching"
            needs_source = True
        else:
            return
        setting = await db.get(Settings, 1, with_for_update=True)
        if setting:
            setting.last_started_at = service.now()
    try:
        if needs_source:
            cfg = service.source_config()
            started = service.now()
            source_hash = service.digest(cfg)
            raw = await asyncio.to_thread(service.fetch_source, cfg)
            async with get_session_context() as db:
                run = await db.get(Run, identifier, with_for_update=True)
                await service.prepare(db, run, raw, started, source_hash)
        async with get_session_context() as db:
            run = await db.get(Run, identifier)
            if run.trigger == "daily":
                await service.export(
                    db, identifier, ExportInput(expected_revision=run.revision)
                )
            setting = await db.get(Settings, 1, with_for_update=True)
            if setting:
                setting.last_completed_at = service.now()
                setting.last_error = None
    except asyncio.CancelledError:
        # Fetching is restartable: it has no remote or physical mutations.
        raise
    except Exception as error:
        code = getattr(error, "code", "atomer_availability_source_unavailable")
        if not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,99}", code):
            code = "atomer_availability_source_unavailable"
        async with get_session_context() as db:
            run = await db.get(Run, identifier, with_for_update=True)
            if run.status != "exported":
                run.status, run.error, run.completed_at = "failed", code, service.now()
            setting = await db.get(Settings, 1, with_for_update=True)
            if setting:
                setting.last_error = code
        logger.warning("Atomer export unavailable (%s)", code)


async def cycle():
    if database._engine is None:
        await database.init_db()
    async with database._engine.connect() as connection:
        locked = await connection.scalar(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": WORKER_LOCK}
        )
        if not locked:
            return False
        await connection.commit()
        try:
            async with get_session_context() as db:
                await db.execute(
                    text("SELECT pg_advisory_xact_lock(:key)"),
                    {"key": service.SETTINGS_LOCK},
                )
                setting = await db.get(Settings, 1, with_for_update=True)
                active = or_(
                    Run.status.in_(("queued", "fetching")),
                    and_(Run.status == "ready", Run.trigger == "daily"),
                )
                busy = await db.scalar(
                    select(Run.id).where(active, Run.trigger == "daily").limit(1)
                )
                if (
                    setting
                    and setting.enabled
                    and not busy
                    and (
                        setting.next_run_at is None
                        or setting.next_run_at <= service.now()
                    )
                ):
                    await service.enqueue(
                        db, RunInput(request_id=uuid4()), trigger="daily"
                    )
                    setting.next_run_at = service.now() + timedelta(days=1)
                identifier = await db.scalar(
                    select(Run.id)
                    .where(active)
                    .order_by(Run.created_at, Run.id)
                    .limit(1)
                )
                await service.prune(db)
            if identifier:
                await process(identifier)
            return identifier is not None
        finally:
            await connection.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": WORKER_LOCK}
            )
            await connection.commit()


async def run():
    while True:
        try:
            worked = await cycle()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning(
                "Atomer export worker unavailable (%s)", type(error).__name__
            )
            worked = False
        await asyncio.sleep(1 if worked else 10)
