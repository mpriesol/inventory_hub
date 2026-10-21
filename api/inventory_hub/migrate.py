"""Run the existing ordered upgrades in one process before restarting the API.

Each migration retains its own transaction and advisory lock. Completed upgrades
are not rolled back if a later one fails; the existing SQL remains replay-safe.
"""
from importlib import import_module
import sys
from time import perf_counter


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
)


def run_migrations() -> None:
    started = perf_counter()
    print(f"[migrations] start count={len(MIGRATIONS)}", flush=True)
    for completed, module_name in enumerate(MIGRATIONS):
        step_started = perf_counter()
        print(f"[migrations] start module={module_name}", flush=True)
        try:
            import_module(f"inventory_hub.{module_name}").main()
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
