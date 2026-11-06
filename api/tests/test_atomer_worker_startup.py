"""Exercise real startup wiring while replacing worker bodies and database I/O."""
import asyncio
import importlib.util
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

from inventory_hub import database, logging_setup
from inventory_hub.settings import Settings, settings


WORKERS = {
    "atomer_catalog_engine": "atomer-catalog",
    "atomer_catalog_source_cards": "atomer-source-cards",
    "ai_content_worker": "ai-content-queue",
    "order_collection_worker": "order-collection",
    "order_webhook_worker": "order-webhooks",
    "order_processing_worker": "order-processing",
    "stock_publication_worker": "stock-publication",
    "supplier_availability_worker": "supplier-availability",
    "stock_sync_worker": "stock-sync",
    "fifo_cost_worker": "fifo-cost-sync",
    "invoice_archive_worker": "invoice-archive",
}
ATOMER_WORKERS = {"atomer_catalog_engine", "atomer_catalog_source_cards"}


class AtomerWorkerStartupTests(unittest.IsolatedAsyncioTestCase):
    async def check_startup(self, environment, enabled):
        with patch.dict(os.environ, environment, clear=True):
            configured = Settings(_env_file=None)
        self.assertIs(configured.ATOMER_CATALOG_WORKER_ENABLED, enabled)
        directory = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.object(settings, "INVENTORY_DATA_ROOT", Path(directory)))
        self.enterContext(patch.object(settings, "USE_POSTGRES", True))
        self.enterContext(patch.object(settings, "ATOMER_CATALOG_WORKER_ENABLED", configured.ATOMER_CATALOG_WORKER_ENABLED))
        self.enterContext(patch.object(logging_setup, "setup_logging"))
        initialize = self.enterContext(patch.object(database, "init_db", AsyncMock()))
        close = self.enterContext(patch.object(database, "close_db", AsyncMock()))

        async def wait_until_shutdown():
            await asyncio.Event().wait()

        workers = {module: self.enterContext(patch(
            f"inventory_hub.services.{module}.run", AsyncMock(side_effect=wait_until_shutdown)))
            for module in WORKERS}
        module_path = Path(__file__).resolve().parents[1] / "inventory_hub" / "main.py"
        spec = importlib.util.spec_from_file_location("inventory_hub._worker_startup_fixture", module_path)
        main = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(main)
        async with main.lifespan(main.app):
            await asyncio.sleep(0)
            active = {task.get_name(): task for task in asyncio.all_tasks()
                      if task.get_name() in WORKERS.values()}
            expected = {name for module, name in WORKERS.items()
                        if enabled or module not in ATOMER_WORKERS}
            self.assertEqual(set(active), expected)
            for module, worker in workers.items():
                if enabled or module not in ATOMER_WORKERS:
                    worker.assert_awaited_once()
                else:
                    worker.assert_not_called()
        initialize.assert_awaited_once()
        close.assert_awaited_once()
        self.assertTrue(all(task.cancelled() for task in active.values()))

    async def test_default_disables_only_atomer_workers(self):
        await self.check_startup({}, False)

    async def test_explicit_environment_opt_in_starts_both_atomer_workers(self):
        await self.check_startup({"ATOMER_CATALOG_WORKER_ENABLED": "true"}, True)
