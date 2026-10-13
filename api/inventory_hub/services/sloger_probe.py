"""Read-only access to historical Sloger AI tests.

New supplier data is fetched directly by sloger_data during imports/refreshes.
This module cannot create jobs, contact a provider or start supplier requests.
"""
from inventory_hub.ai_content_models import AiJob
from inventory_hub.database import get_session_context
from inventory_hub.services.catalog import CatalogError


KIND = "supplier_lookup"


def public(job: AiJob) -> dict:
    return {"id": job.id, "status": job.status, "model": (job.context or {}).get("model", "unknown"), "result": job.output,
            "usage": job.usage or {}, "actual_usd": str(job.actual_usd) if job.actual_usd is not None else None,
            "error": job.error, "diagnostic": (job.context or {}).get("supplier_diagnostic")}


async def read(id: str) -> dict:
    async with get_session_context() as db:
        job = await db.get(AiJob, id)
        if job is None or job.kind != KIND:
            raise CatalogError("sloger_probe_not_found", "Sloger lookup was not found", 404)
        return public(job)
