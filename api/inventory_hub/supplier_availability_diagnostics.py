"""Read-only production evidence: python -m inventory_hub.supplier_availability_diagnostics northfinder.

No feed download, supplier credentials, product selection or shop write. Run this
in the deployed API container after a refresh to inspect persisted evidence.
"""
import argparse
import asyncio
import json
import re

from sqlalchemy import select, text
from inventory_hub import database
from inventory_hub.db_models import Supplier, SupplierFeed, SupplierFeedRun
from inventory_hub.services import supplier_availability as availability, stock_sync
from inventory_hub.services.supplier_availability_source import configuration, AvailabilityError
from inventory_hub.settings import settings
from inventory_hub.supplier_availability_models import SupplierAvailabilitySettings


async def snapshot(db, supplier, shops):
    supplier_row = await db.scalar(select(Supplier).where(Supplier.code == supplier))
    row = await db.get(SupplierAvailabilitySettings, supplier_row.id) if supplier_row else None
    result = {"captured_at": availability.now(), "read_only": True, "supplier": supplier,
              "postgres_enabled": bool(settings.USE_POSTGRES), "configured": row is not None,
              "live_shop_write_tested": False}
    if row:
        try:
            configuration(supplier, row.feed_key)
            config_error = None
        except AvailabilityError as error:
            config_error = error.code
        result["schedule"] = {key: getattr(row, key) for key in (
            "enabled", "feed_key", "interval_seconds", "freshness_seconds", "next_run_at",
            "last_started_at", "last_success_at", "manual_requested_at", "running_run_id")}
        result["configuration_error"] = config_error
        result["last_error"] = availability._safe_error(row.last_error)
        result["latest_run"] = await availability.latest_run(db, row.supplier_id, row.feed_key)
        runs = (await db.scalars(select(SupplierFeedRun).join(SupplierFeed, SupplierFeed.id == SupplierFeedRun.feed_id)
            .where(SupplierFeed.supplier_id == row.supplier_id, SupplierFeed.code == row.feed_key,
                   SupplierFeedRun.error_details["workflow"].astext == "supplier_availability")
            .order_by(SupplierFeedRun.id.desc()).limit(3))).all()
        result["recent_runs"] = [{"id": run.id, "trigger": (run.error_details or {}).get("trigger", "unknown"),
            "status": run.status.value, "started_at": run.started_at, "finished_at": run.finished_at,
            "accepted": run.items_updated, "skipped": run.items_error,
            "error": availability._safe_error(run.error_message)} for run in runs]
    result["shops"] = {}
    for code in shops:
        try:
            view = await stock_sync.options(db, code)
            result["shops"][code] = {"server_write_enabled": view["server_write_enabled"],
                "enabled": view["settings"]["enabled"], "authorized": view["settings"]["authorized"],
                "protocol": view["settings"].get("protocol"), "blockers": view["blockers"],
                "baseline_count": view["baseline_count"], "last_completed_at": view["settings"]["last_completed_at"],
                "scope": "shop_configuration_only; individual products require their own valid mapping, stock evidence and baseline"}
        except stock_sync.SyncError as error:
            result["shops"][code] = {"error": error.code}
    return result


async def main(supplier, shops):
    try:
        async with database.get_session_context() as db:
            await db.execute(text("SET TRANSACTION READ ONLY"))
            await db.execute(text("SET LOCAL statement_timeout = '30s'"))
            result = await snapshot(db, supplier, shops)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    except Exception as error:
        # Never expose database URLs or configured supplier credentials on failure.
        print(json.dumps({"read_only": True, "error": "diagnostic_failed", "exception": type(error).__name__}))
        raise SystemExit(1) from None
    finally:
        await database.close_db()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("supplier")
    parser.add_argument("--shops", nargs="*", default=["biketrek", "xtrek"])
    args = parser.parse_args()
    if not all(re.fullmatch(r"[a-zA-Z0-9_-]{1,50}", code) for code in [args.supplier, *args.shops]):
        parser.error("Supplier and shop codes must be simple identifiers")
    asyncio.run(main(args.supplier, args.shops))
