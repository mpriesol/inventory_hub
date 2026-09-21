"""Durable Postgres queue, serialized across API processes using a session lock.

The worker runs in the existing API container. Job state lives in PostgreSQL,
so closing the browser or restarting the container does not lose the queue.
"""
import asyncio
import logging

from sqlalchemy import select, text

from inventory_hub.ai_content_models import AiContentRevision, AiJob
from inventory_hub.ai_content_types import Content
from inventory_hub import database
from inventory_hub.database import get_session_context
from inventory_hub.services import ai_content as service, ai_content_provider as provider, catalog_import
from inventory_hub.services.ai_content_validation import validate_content
from inventory_hub.services.catalog import CatalogError
from inventory_hub.settings import settings

logger = logging.getLogger(__name__)


async def generation(id):
    async with get_session_context() as db:
        job = await service.get_job(db, id, lock=True)
        if job.status != "queued" or not settings.AI_CONTENT_ENABLED:
            return
        context, kind = job.context, job.kind
        service.event(job, "generating", "Provider request started; automatic paid retry disabled")
    try:
        response = await provider.generate(context, kind)
        # Record billed usage before parsing: invalid output can still cost money.
        usage, cost = provider.usage_cost(response, context["model"])
        async with get_session_context() as db:
            job = await service.get_job(db, id, lock=True)
            job.usage, job.actual_usd = usage, cost
        output, opened = provider.parse_response(response, kind)
        async with get_session_context() as db:
            job = await service.get_job(db, id, lock=True)
            job.output, job.usage = output, {**usage, "opened_sources": opened}
            if kind == "rules":
                service.event(job, "review", "AI rule proposal; never auto-published")
            else:
                checks = validate_content(Content.model_validate(output), context, opened)
                job.checks = checks
                needs_review = context["resolved"]["policy"]["review_required"] or not checks["automatic_ready"]
                state = "blocked" if checks["errors"] else "review" if needs_review else "preparing_import"
                job.context = {**context, "approval": "policy" if state == "preparing_import" else None}
                service.event(job, state, "Server validation complete; " + ("policy approval" if state == "preparing_import" else "human review needed"))
                db.add(AiContentRevision(job_id=id, revision=job.revision, content=output,
                                        decision="policy_approved" if state == "preparing_import" else "generated"))
    except CatalogError as error:
        async with get_session_context() as db:
            job = await service.get_job(db, id, lock=True)
            job.error = error.code
            if error.code in ("ai_provider_rejected", "ai_not_configured") and job.actual_usd is None:
                job.reserved_usd = 0
            service.event(job, "uncertain" if error.code == "ai_outcome_unknown" else "failed", error.code)


async def importing(id):
    async with get_session_context() as db:
        job = await service.get_job(db, id, lock=True)
        if job.status != "import_queued":
            return
        shop, preview_id = job.context["shop"], job.preview_id
        retry = job.context.get("retry_import", False)
        if not preview_id:
            service.event(job, "preparing_import", "Preparing missing import preview")
            return
        # Persist intent before invoking the existing idempotent import mechanism.
        service.event(job, "importing", "Upgates import started")
    try:
        _, run = catalog_import.queue_import(shop, preview_id, retry)
        if run:
            await catalog_import.execute_import(shop, preview_id)
        result = catalog_import.import_result(shop, preview_id)
        async with get_session_context() as db:
            job = await service.get_job(db, id, lock=True)
            if result["status"] in ("queued", "running"):
                service.event(job, "import_queued", "Existing import still running; reconciliation pending")
            else:
                ok = result["status"] == "completed" and all(i["status"] in ("created", "exists") for i in result["items"])
                service.event(job, "completed" if ok else "import_failed", "Import result checked in Upgates")
    except CatalogError as error:
        async with get_session_context() as db:
            job = await service.get_job(db, id, lock=True)
            job.error = error.code
            service.event(job, "import_failed", error.code)


async def cycle():
    # A dedicated connection keeps the advisory lock across transaction commits.
    async with database._engine.connect() as connection:
        locked = await connection.scalar(text("SELECT pg_try_advisory_lock(691432108)"))
        if not locked:
            return False
        try:
            async with get_session_context() as db:
                # Acquiring the sole worker lock proves there is no active worker.
                # Never repeat a request whose provider response was lost on restart.
                interrupted = (await db.scalars(select(AiJob).where(AiJob.status.in_(["generating", "importing"]))
                                               .with_for_update())).all()
                for job in interrupted:
                    if job.status == "generating":
                        job.error = "ai_outcome_unknown"
                        service.event(job, "uncertain", "Worker restarted after provider intent; paid retry requires a new explicit job")
                    else:
                        service.event(job, "import_queued", "Resume existing preview; reconcile before any repeated write")
                states = ["preparing_import", "import_queued"] + (["queued"] if settings.AI_CONTENT_ENABLED else [])
                job = await db.scalar(select(AiJob).where(AiJob.status.in_(states)).order_by(AiJob.created_at, AiJob.id)
                                      .with_for_update(skip_locked=True).limit(1))
                if job is None:
                    return False
                id, state = job.id, job.status
            if state == "queued":
                await generation(id)
            elif state == "import_queued":
                await importing(id)
            else:
                async with get_session_context() as db:
                    job = await service.get_job(db, id, lock=True)
                    if job.status != "preparing_import":
                        return True
                    try:
                        await service.prepare_import(db, job)
                    except CatalogError as error:
                        job.error = error.code
                        service.event(job, "import_blocked", error.code)
            return True
        finally:
            await connection.execute(text("SELECT pg_advisory_unlock(691432108)"))
            await connection.commit()


async def run():
    while True:
        try:
            worked = await cycle()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # Do not log source material, credentials or database connection strings.
            logger.warning("AI queue paused (%s)", type(error).__name__)
            worked = False
        await asyncio.sleep(1 if worked else 5)
