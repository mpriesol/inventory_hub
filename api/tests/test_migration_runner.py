import contextlib
import io
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from inventory_hub import migrate


class MigrationRunnerTests(unittest.TestCase):
    def test_all_existing_upgrades_run_in_deployment_order(self):
        # Independent contract for the upgrade order that production used
        # before switching from nineteen containers to this one entrypoint.
        expected = (
            "ai_content", "opening_stock", "order_stock", "order_collection",
            "stock_automation", "stock_publication", "fifo", "product_editor",
            "receiving_scan", "supplier_availability", "stock_sync",
            "product_publication", "stock_adjustment", "fifo_cost",
            "stock_tracking", "user_sessions", "product_feed_mapping",
            "product_import", "ai_content_settings", "upgates_api_budget", "stock_native", "stock_delta", "product_links",
            "supplier_card_links", "sku_policy", "shop_identity", "item_units", "supplier_authority", "invoice_archive",
            "supplier_availability_delivery", "order_webhooks", "stock_sync_dirty",
        )
        calls = []

        def load(name):
            calls.append(("import", name))
            return SimpleNamespace(main=lambda: calls.append(("run", name)))

        output = io.StringIO()
        with patch.object(migrate, "import_module", side_effect=load), \
                contextlib.redirect_stdout(output):
            result = migrate.main()

        self.assertEqual(result, 0)
        self.assertEqual(calls, [
            (action, f"inventory_hub.{name}_migrate")
            for name in expected for action in ("import", "run")
        ])
        self.assertIn("[migrations] done completed=32/32 elapsed_s=", output.getvalue())
        self.assertEqual(output.getvalue().count("done module="), 32)

    def test_migration_failure_stops_before_importing_the_next_upgrade(self):
        completed = Mock()
        failure = RuntimeError("private database connection detail")
        broken = Mock(side_effect=failure)
        modules = [SimpleNamespace(main=completed), SimpleNamespace(main=broken)]
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(migrate, "import_module", side_effect=modules) as load, \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            with self.assertRaises(RuntimeError) as raised:
                migrate.run_migrations()

        self.assertIs(raised.exception, failure)
        completed.assert_called_once_with()
        broken.assert_called_once_with()
        self.assertEqual(load.call_count, 2)
        self.assertIn("failed module=opening_stock_migrate", stderr.getvalue())
        self.assertIn("error_type=RuntimeError", stderr.getvalue())
        self.assertIn("failed completed=1/32 elapsed_s=", stderr.getvalue())
        self.assertNotIn("done completed=", stdout.getvalue())
        self.assertNotIn(str(failure), stdout.getvalue() + stderr.getvalue())

    def test_import_failure_returns_nonzero_without_trying_later_upgrades(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(migrate, "import_module", side_effect=ImportError("private detail")) as load, \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = migrate.main()

        self.assertEqual(result, 1)
        load.assert_called_once_with("inventory_hub.ai_content_migrate")
        self.assertIn("error_type=ImportError", stderr.getvalue())
        self.assertIn("failed completed=0/32", stderr.getvalue())
        self.assertNotIn("private detail", stdout.getvalue() + stderr.getvalue())

    def test_timings_include_module_execution_and_total(self):
        output = io.StringIO()
        with patch.object(migrate, "MIGRATIONS", ("example_migrate",)), \
                patch.object(migrate, "import_module", return_value=SimpleNamespace(main=Mock())), \
                patch.object(migrate, "perf_counter", side_effect=[10.0, 10.25, 12.25, 12.5]), \
                contextlib.redirect_stdout(output):
            migrate.run_migrations()

        self.assertIn("done module=example_migrate elapsed_s=2.000", output.getvalue())
        self.assertIn("done completed=1/1 elapsed_s=2.500", output.getvalue())


if __name__ == "__main__":
    unittest.main()
