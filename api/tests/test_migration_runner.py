import contextlib
import io
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

from psycopg2 import OperationalError
from psycopg2.errors import DeadlockDetected, LockNotAvailable, QueryCanceled, SerializationFailure

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
            "supplier_availability_delivery_once", "order_webhooks", "stock_sync_dirty", "atomer_progress", "atomer_review",
            "shared_stock_sales", "product_consolidation", "atomer_catalog_control", "atomer_catalog_job_control", "atomer_catalog_handoff",
            "invoice_processing", "receiving_operation",
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
        self.assertIn("[migrations] done completed=41/41 elapsed_s=", output.getvalue())
        self.assertEqual(output.getvalue().count("done module="), 41)

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
        self.assertIn("failed completed=1/41 elapsed_s=", stderr.getvalue())
        self.assertNotIn("done completed=", stdout.getvalue())
        self.assertNotIn(str(failure), stdout.getvalue() + stderr.getvalue())

    def test_import_failure_returns_nonzero_without_trying_later_upgrades(self):
        for failure in (ImportError("private detail"), DeadlockDetected("private detail")):
            with self.subTest(error_type=type(failure).__name__):
                stdout, stderr = io.StringIO(), io.StringIO()
                with patch.object(migrate, "import_module", side_effect=failure) as load, \
                        patch.object(migrate, "sleep") as wait, \
                        contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    result = migrate.main()

                self.assertEqual(result, 1)
                load.assert_called_once_with("inventory_hub.ai_content_migrate")
                wait.assert_not_called()
                self.assertIn(f"error_type={type(failure).__name__}", stderr.getvalue())
                self.assertIn("failed completed=0/41", stderr.getvalue())
                self.assertNotIn("private detail", stdout.getvalue() + stderr.getvalue())

    def test_deadlock_retry_waits_for_rollback_and_preserves_migration_order(self):
        events, committed = [], []
        transaction_count = 0
        private_detail = "postgresql://operator:private-password@host/db private SQL row"

        @contextlib.contextmanager
        def transaction():
            nonlocal transaction_count
            transaction_count += 1
            number, pending = transaction_count, []
            events.append(("begin", number))
            try:
                yield number, pending
            except Exception:
                events.append(("rollback", number))
                raise
            else:
                committed.extend(pending)
                events.append(("commit", number))

        def apply():
            with transaction() as (number, pending):
                pending.append("migration applied")
                if number == 1:
                    raise DeadlockDetected(private_detail)

        before = Mock(side_effect=lambda: events.append("before"))
        after = Mock(side_effect=lambda: events.append("after"))
        modules = [SimpleNamespace(main=before), SimpleNamespace(main=apply), SimpleNamespace(main=after)]
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(migrate, "MIGRATIONS", ("before_migrate", "retry_migrate", "after_migrate")), \
                patch.object(migrate, "import_module", side_effect=modules) as load, \
                patch.object(migrate, "sleep", side_effect=lambda seconds: events.append(("sleep", seconds))), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = migrate.main()

        self.assertEqual(result, 0)
        self.assertEqual(events, [
            "before", ("begin", 1), ("rollback", 1), ("sleep", 1),
            ("begin", 2), ("commit", 2), "after",
        ])
        self.assertEqual(committed, ["migration applied"])
        self.assertEqual(load.call_args_list, [
            call("inventory_hub.before_migrate"), call("inventory_hub.retry_migrate"),
            call("inventory_hub.after_migrate"),
        ])
        before.assert_called_once_with()
        after.assert_called_once_with()
        self.assertEqual(stdout.getvalue().count("done module="), 3)
        self.assertIn("done completed=3/3", stdout.getvalue())
        self.assertIn("retry module=retry_migrate failed_attempt=1/3 next_attempt=2/3 delay_s=1", stderr.getvalue())
        self.assertIn("error_type=DeadlockDetected sqlstate=40P01", stderr.getvalue())
        self.assertNotIn(private_detail, stdout.getvalue() + stderr.getvalue())
        self.assertNotIn("Traceback", stdout.getvalue() + stderr.getvalue())

    def test_last_allowed_deadlock_retry_can_succeed(self):
        apply = Mock(side_effect=[DeadlockDetected("private SQL"), DeadlockDetected("private SQL"), None])
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(migrate, "MIGRATIONS", ("example_migrate",)), \
                patch.object(migrate, "import_module", return_value=SimpleNamespace(main=apply)) as load, \
                patch.object(migrate, "sleep") as wait, \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = migrate.main()

        self.assertEqual(result, 0)
        self.assertEqual(apply.call_count, 3)
        load.assert_called_once_with("inventory_hub.example_migrate")
        self.assertEqual(wait.call_args_list, [call(1), call(2)])
        self.assertIn("failed_attempt=2/3 next_attempt=3/3 delay_s=2", stderr.getvalue())
        self.assertIn("done completed=1/1", stdout.getvalue())
        self.assertNotIn("private SQL", stdout.getvalue() + stderr.getvalue())

    def test_repeated_deadlocks_stop_after_three_attempts(self):
        failures = [DeadlockDetected(f"private SQL {number}") for number in range(3)]
        before, broken, after = Mock(), Mock(side_effect=failures), Mock()
        modules = [SimpleNamespace(main=before), SimpleNamespace(main=broken), SimpleNamespace(main=after)]
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(migrate, "MIGRATIONS", ("before_migrate", "broken_migrate", "after_migrate")), \
                patch.object(migrate, "import_module", side_effect=modules) as load, \
                patch.object(migrate, "sleep") as wait, \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            with self.assertRaises(DeadlockDetected) as raised:
                migrate.run_migrations()

        self.assertIs(raised.exception, failures[-1])
        before.assert_called_once_with()
        self.assertEqual(broken.call_count, 3)
        after.assert_not_called()
        self.assertEqual(load.call_count, 2)
        self.assertEqual(wait.call_args_list, [call(1), call(2)])
        self.assertEqual(stderr.getvalue().count("retry module="), 2)
        self.assertIn("failed module=broken_migrate", stderr.getvalue())
        self.assertIn("failed completed=1/3", stderr.getvalue())
        self.assertNotIn("done module=broken_migrate", stdout.getvalue())
        self.assertNotIn("done completed=", stdout.getvalue())
        self.assertNotIn("private SQL", stdout.getvalue() + stderr.getvalue())
        self.assertNotIn("Traceback", stdout.getvalue() + stderr.getvalue())

    def test_other_database_errors_do_not_retry(self):
        class OtherErrorWithCode(RuntimeError):
            pgcode = "40P01"

        for error_type in (OperationalError, SerializationFailure, LockNotAvailable, QueryCanceled, OtherErrorWithCode):
            with self.subTest(error_type=error_type.__name__):
                failure = error_type("deadlock detected SQLSTATE 40P01 private detail")
                broken, after = Mock(side_effect=failure), Mock()
                stdout, stderr = io.StringIO(), io.StringIO()
                with patch.object(migrate, "MIGRATIONS", ("broken_migrate", "after_migrate")), \
                        patch.object(migrate, "import_module", side_effect=[
                            SimpleNamespace(main=broken), SimpleNamespace(main=after),
                        ]) as load, \
                        patch.object(migrate, "sleep") as wait, \
                        contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    result = migrate.main()

                self.assertEqual(result, 1)
                broken.assert_called_once_with()
                after.assert_not_called()
                load.assert_called_once_with("inventory_hub.broken_migrate")
                wait.assert_not_called()
                self.assertIn(f"error_type={error_type.__name__}", stderr.getvalue())
                self.assertIn("failed completed=0/2", stderr.getvalue())
                self.assertNotIn("retry module=", stderr.getvalue())
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
