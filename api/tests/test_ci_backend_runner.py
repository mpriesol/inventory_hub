"""The CI splitter must never turn missing, skipped or failed tests into green CI."""

import asyncio
from collections import Counter
from copy import deepcopy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import ci_backend_tests as runner


class BackendCIRunnerTests(unittest.TestCase):
    @staticmethod
    def reports():
        ids = ["one.Case.test_one", "two.Case.test_two"]
        return [
            {
                "schema_version": 1, "shard_index": index, "shard_count": 2,
                "require_database": True, "discovered_ids": ids,
                "selected_ids": [test_id], "executed_ids": [test_id],
                "tests": [{"id": test_id, "status": "passed"}],
                "failures": [], "errors": [], "skipped": [], "unexpected_successes": [],
                "success": True,
            }
            for index, test_id in enumerate(ids)
        ]

    def test_shards_include_new_modules_and_keep_module_fixtures_together(self):
        tests = []
        for module, count in [("database_without_db_suffix", 3), ("fast_tests", 8), ("new_db", 2), ("future_tests", 1)]:
            case = type("Example", (unittest.TestCase,), {"__module__": module, "test_example": lambda self: None})
            tests.extend(case("test_example") for _ in range(count))
        shards, estimates = runner.plan_shards(tests, 4, {"database_without_db_suffix": 10})
        self.assertEqual(Counter(map(id, tests)), Counter(id(test) for shard in shards for test in shard))
        for module in {test.__class__.__module__ for test in tests}:
            owners = [index for index, shard in enumerate(shards) if any(test.__class__.__module__ == module for test in shard)]
            self.assertEqual(len(owners), 1)
            self.assertEqual([test for test in tests if test.__class__.__module__ == module], [test for test in shards[owners[0]] if test.__class__.__module__ == module])
        again, _ = runner.plan_shards(tests, 4, {"database_without_db_suffix": 10})
        self.assertEqual(shards, again)
        self.assertEqual(estimates[0], 10)

    def test_complete_reports_pass_but_missing_or_duplicate_shards_fail(self):
        reports = self.reports()
        self.assertEqual(runner.validate_reports(reports, 2), 2)
        for bad in [reports[:1], [reports[0], reports[0]]]:
            with self.subTest(reports=bad), self.assertRaises(ValueError):
                runner.validate_reports(bad, 2)

    def test_missing_duplicate_or_unexecuted_tests_cannot_pass(self):
        for mutation in ["missing", "duplicate", "unexecuted", "inventory", "timing"]:
            reports = deepcopy(self.reports())
            if mutation == "missing":
                reports[1]["selected_ids"] = []
                reports[1]["executed_ids"] = []
                reports[1]["tests"] = []
            elif mutation == "duplicate":
                reports[1]["selected_ids"] = reports[0]["selected_ids"]
                reports[1]["executed_ids"] = reports[0]["executed_ids"]
                reports[1]["tests"] = reports[0]["tests"]
            elif mutation == "unexecuted":
                reports[1]["executed_ids"] = []
                reports[1]["tests"] = []
            elif mutation == "inventory":
                reports[1]["discovered_ids"] = ["different.Case.test_one"]
            else:
                reports[1]["tests"] = []
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                runner.validate_reports(reports, 2)

    def test_failures_skips_and_disabled_database_are_rejected(self):
        for key, value in [("failures", ["one.Case.test_one"]), ("errors", ["setUpClass"]), ("unexpected_successes", ["one.Case.test_one"]), ("skipped", ["one.Case.test_one"]), ("require_database", False), ("success", False)]:
            reports = deepcopy(self.reports())
            reports[0][key] = value
            with self.subTest(field=key), self.assertRaises(ValueError):
                runner.validate_reports(reports, 2)

    def test_timing_preserves_async_fixture_order_and_restores_hooks(self):
        calls = []

        class AsyncExample(unittest.IsolatedAsyncioTestCase):
            @classmethod
            def setUpClass(cls):
                calls.append("class_setup")

            @classmethod
            def tearDownClass(cls):
                calls.append("class_teardown")

            async def asyncSetUp(self):
                calls.append("setup")
                self.addAsyncCleanup(self.finish)
                await asyncio.sleep(0)

            async def test_example(self):
                calls.append("body")
                await asyncio.sleep(0)

            async def asyncTearDown(self):
                calls.append("teardown")

            async def finish(self):
                calls.append("cleanup")

        test = AsyncExample("test_example")
        result = unittest.TextTestRunner(stream=io.StringIO(), resultclass=runner.TimingResult).run(unittest.TestSuite([test]))
        self.assertTrue(result.wasSuccessful())
        self.assertEqual(calls, ["class_setup", "setup", "body", "teardown", "cleanup", "class_teardown"])
        self.assertEqual(result.records[0]["status"], "passed")
        for phase in runner.PHASES.values():
            self.assertGreater(result.records[0][phase], 0)
        for method in runner.PHASES:
            self.assertNotIn(method, test.__dict__)

    def test_timing_preserves_failure_cleanup_and_skips(self):
        calls = []

        class Examples(unittest.TestCase):
            def setUp(self):
                self.addCleanup(lambda: calls.append("cleanup"))

            def test_failure(self):
                with self.subTest(value=1):
                    self.fail("deliberate failure inside the runner self-test")

            @unittest.skip("deliberate skip inside the runner self-test")
            def test_skip(self):
                self.fail("must not run")

        suite = unittest.defaultTestLoader.loadTestsFromTestCase(Examples)
        result = unittest.TextTestRunner(stream=io.StringIO(), resultclass=runner.TimingResult).run(suite)
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(len(result.skipped), 1)
        self.assertEqual([record["status"] for record in result.records], ["failed", "skipped"])
        self.assertEqual(calls, ["cleanup"])

    def test_database_requirement_rejects_missing_or_non_test_database(self):
        for url in ["", "postgresql://localhost/production", "postgresql://remote.example/inventory_catalog_test"]:
            with self.subTest(url=url), patch.dict("os.environ", {"CATALOG_TEST_DATABASE_URL": url}), self.assertRaises(ValueError):
                runner.require_test_database()
        with patch.dict("os.environ", {"CATALOG_TEST_DATABASE_URL": "postgresql://localhost/inventory_catalog_test"}):
            runner.require_test_database()

    def test_report_keeps_original_identity_when_fixture_assigns_self_id(self):
        class Example(unittest.TestCase):
            def setUp(self):
                self.id = "business-object-id"

            def test_example(self):
                self.assertEqual(self.id, "business-object-id")

        test = Example("test_example")
        original_id = test.id()
        with tempfile.TemporaryDirectory() as directory, patch.object(runner, "discover_tests", return_value=[test]), patch("sys.stderr", new_callable=io.StringIO):
            path = Path(directory) / "report.json"
            self.assertEqual(runner.main(["--report", str(path)]), 0)
            report = json.loads(path.read_text())
        self.assertEqual(report["discovered_ids"], [original_id])
        self.assertEqual(report["executed_ids"], [original_id])
