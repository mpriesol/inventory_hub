"""Daily refresh of the configured Paul Lange product catalog."""
import asyncio
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from inventory_hub import config_io, operation_log
from inventory_hub.database import get_session_context
from inventory_hub.db_models import FeedRunStatus, Supplier, SupplierFeedRun
from inventory_hub.services import catalog

SUPPLIER = "paul-lange"
REFRESH_INTERVAL = timedelta(days=1)
RETRY_INTERVAL = timedelta(hours=1)
CHECK_SECONDS = 300
DOWNLOAD_SECONDS = 300


async def refresh_if_due(db: AsyncSession) -> dict | None:
    if not config_io.supplier_path(SUPPLIER).is_file():
        return None
    cfg = catalog.supplier_config(SUPPLIER)
    source = (cfg["feeds"].get("sources") or {}).get("products") or {}
    if (not cfg.get("is_active", True) or source.get("scheduled_refresh", True) is not True
            or source.get("mode", "remote") != "remote" or not catalog._source_configured(source)):
        return None

    # Hold the same lock used by a manual refresh while checking freshness.
    if not await db.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": catalog._lock_key(SUPPLIER)}):
        return None
    if await db.scalar(select(Supplier.is_active).where(Supplier.code == SUPPLIER)) is False:
        return None
    feed = await catalog._feed(db, SUPPLIER, cfg, "products")
    now = datetime.now(timezone.utc)
    if feed is not None:
        if not feed.is_active or (feed.last_run_at and feed.last_run_at > now - REFRESH_INTERVAL):
            return None
        recent_failure = await db.scalar(select(SupplierFeedRun.id).where(
            SupplierFeedRun.feed_id == feed.id,
            SupplierFeedRun.status == FeedRunStatus.failed,
            SupplierFeedRun.started_at > now - RETRY_INTERVAL,
            SupplierFeedRun.error_details["workflow"].astext.is_distinct_from("supplier_availability"),
        ).limit(1))
        if recent_failure is not None:
            return None
    return await catalog.refresh_catalog(db, SUPPLIER, max_download_seconds=DOWNLOAD_SECONDS)


async def run() -> None:
    with operation_log.context(component="supplier_catalog", supplier=SUPPLIER, source="products"):
        while True:
            started = time.monotonic()
            try:
                async with get_session_context() as db:
                    result = await refresh_if_due(db)
            except Exception as error:
                code = error.code if isinstance(error, catalog.CatalogError) else type(error).__name__
                operation_log.emit("catalog_refresh_failed", level="error", error_code=code,
                                   duration_ms=round((time.monotonic() - started) * 1000))
            else:
                if result is not None:
                    operation_log.emit("catalog_refresh_completed", run_id=result["run_id"], rows=result["items"],
                                       duration_ms=round((time.monotonic() - started) * 1000))
            await asyncio.sleep(CHECK_SECONDS)
