"""Run the existing ordered upgrades in one process before restarting the API.

Each migration retains its own transaction and advisory lock. Completed upgrades
are not rolled back if a later one fails; the existing SQL remains replay-safe.
Only PostgreSQL deadlocks retry the failed module after its transaction unwinds.
"""
from importlib import import_module
import sys
from time import perf_counter, sleep

from psycopg2.errors import DeadlockDetected


DEADLOCK_MAX_ATTEMPTS = 3


# Explicit deployment order: adding a SQL file alone must not start running it.
MIGRATIONS = (
    "ai_content_migrate",             # 005
    "opening_stock_migrate",          # 006
    "order_stock_migrate",            # 007
    "order_collection_migrate",       # 008
    "stock_automation_migrate",       # 009
    "stock_publication_migrate",      # 010
    "fifo_migrate",                   # 011
    "product_editor_migrate",         # 012
    "receiving_scan_migrate",         # 013
    "supplier_availability_migrate",  # 014
    "stock_sync_migrate",             # 015
    "product_publication_migrate",    # 016
    "stock_adjustment_migrate",       # 017
    "fifo_cost_migrate",              # 018
    "stock_tracking_migrate",         # 019
    "user_sessions_migrate",          # 020
    "product_feed_mapping_migrate",   # 021
    "product_import_migrate",         # 022
    "ai_content_settings_migrate",    # 023
    "upgates_api_budget_migrate",     # 024
    "stock_native_migrate",           # 025
    "stock_delta_migrate",            # 026
    "product_links_migrate",          # 027
    "supplier_card_links_migrate",    # 028
    "sku_policy_migrate",             # 029
    "shop_identity_migrate",          # 030
    "item_units_migrate",             # 031
    "supplier_authority_migrate",     # 032
    "invoice_archive_migrate",        # 033
    "supplier_availability_delivery_once_migrate",  # 034; preserve later user schedules
    "order_webhooks_migrate",         # 035
    "stock_sync_dirty_migrate",       # 036
    "atomer_progress_migrate",        # 037
    "atomer_review_migrate",          # 038
    "shared_stock_sales_migrate",     # 046;039–045 retain their existing feature installers
    "product_consolidation_migrate",  # 047; no live consolidation or stock activation
    "atomer_catalog_control_migrate", # 048; no campaign lifecycle changes
    "atomer_catalog_job_control_migrate", # 049; no job lifecycle changes
    "atomer_catalog_handoff_migrate", # 050; no job lifecycle changes
    "invoice_processing_migrate",    # 051; invoice handling/archival, no stock writes
    "receiving_operation_migrate",   # 052; receipt correction audits, no automatic reset
)


def run_migrations() -> None:
    started = perf_counter()
    print(f"[migrations] start count={len(MIGRATIONS)}", flush=True)
    for completed, module_name in enumerate(MIGRATIONS):
        step_started = perf_counter()
        print(f"[migrations] start module={module_name}", flush=True)
        try:
            module = import_module(f"inventory_hub.{module_name}")
            for attempt in range(1, DEADLOCK_MAX_ATTEMPTS + 1):
                try:
                    module.main()
                    break
                except DeadlockDetected:
                    if attempt == DEADLOCK_MAX_ATTEMPTS:
                        raise
                    # All current modules open their own connection/transaction.
                    # Its context has rolled back before this handler; the next
                    # call reruns the whole module in a fresh transaction.
                    print(
                        f"[migrations] retry module={module_name} "
                        f"failed_attempt={attempt}/{DEADLOCK_MAX_ATTEMPTS} "
                        f"next_attempt={attempt + 1}/{DEADLOCK_MAX_ATTEMPTS} "
                        f"delay_s={attempt} error_type=DeadlockDetected sqlstate=40P01",
                        file=sys.stderr,
                        flush=True,
                    )
                    sleep(attempt)
        except Exception as exc:
            # Connection errors can contain credentials or SQL data: log only
            # the type and failing module, never the exception text or traceback.
            print(
                f"[migrations] failed module={module_name} "
                f"elapsed_s={perf_counter() - step_started:.3f} "
                f"error_type={type(exc).__name__}",
                file=sys.stderr,
                flush=True,
            )
            print(
                f"[migrations] failed completed={completed}/{len(MIGRATIONS)} "
                f"elapsed_s={perf_counter() - started:.3f}",
                file=sys.stderr,
                flush=True,
            )
            raise
        print(
            f"[migrations] done module={module_name} "
            f"elapsed_s={perf_counter() - step_started:.3f}",
            flush=True,
        )
    print(
        f"[migrations] done completed={len(MIGRATIONS)}/{len(MIGRATIONS)} "
        f"elapsed_s={perf_counter() - started:.3f}",
        flush=True,
    )


def main() -> int:
    try:
        run_migrations()
    except Exception:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
