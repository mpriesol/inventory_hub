#!/usr/bin/env python3
"""Run complete unittest discovery in balanced, module-preserving CI shards.

Reports are verified by the final CI job: every discovered test must be selected
and executed exactly once across all shards. Timing does not change test logic.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import glob
import json
import os
from pathlib import Path
import sys
import time
import unittest
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
WEIGHTS_PATH = Path(__file__).with_name("ci_test_weights.json")
PHASES = {
    "_callSetUp": "setup_seconds",
    "_callTestMethod": "test_seconds",
    "_callTearDown": "teardown_seconds",
    "doCleanups": "cleanup_seconds",
}


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


def discover_tests():
    sys.path.insert(0, str(ROOT / "api"))
    loader = unittest.TestLoader()
    tests = list(flatten(loader.discover(str(ROOT / "api/tests"))))
    if loader.errors:
        raise ValueError("Test discovery failed:\n" + "\n".join(loader.errors))
    if not tests:
        raise ValueError("Test discovery returned no tests")
    if len({test.id() for test in tests}) != len(tests):
        raise ValueError("Test discovery returned duplicate test IDs")
    return tests


def plan_shards(tests, shard_count, weights):
    """Longest module first; keep class/module fixtures and test order intact."""
    if shard_count < 1:
        raise ValueError("shard_count must be positive")
    modules = defaultdict(list)
    for test in tests:
        modules[test.__class__.__module__].append(test)

    def weight(module):
        fallback = len(modules[module]) * (1.2 if module.endswith("_db") else 0.008)
        return float(weights.get(module, fallback))

    assignments = {}
    totals = [0.0] * shard_count
    for module in sorted(modules, key=lambda name: (-weight(name), name)):
        index = min(range(shard_count), key=lambda value: (totals[value], value))
        assignments[module] = index
        totals[index] += weight(module)
    shards = [[] for _ in range(shard_count)]
    for test in tests:
        shards[assignments[test.__class__.__module__]].append(test)
    return shards, totals


class TimingResult(unittest.TextTestResult):
    """Time existing unittest hooks, including IsolatedAsyncioTestCase hooks."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records = []
        self._active = {}

    def startTest(self, test):
        super().startTest(test)
        record = {"id": test.id(), "status": "started", **dict.fromkeys(PHASES.values(), 0.0)}
        originals = {}
        for method_name, phase in PHASES.items():
            method = getattr(test, method_name)
            originals[method_name] = (method_name in test.__dict__, test.__dict__.get(method_name))

            def timed(*args, _method=method, _phase=phase, **kwargs):
                started = time.perf_counter()
                try:
                    return _method(*args, **kwargs)
                finally:
                    record[_phase] += time.perf_counter() - started

            setattr(test, method_name, timed)
        self._active[id(test)] = (record, originals, time.perf_counter())
        self.records.append(record)

    def stopTest(self, test):
        record, originals, started = self._active.pop(id(test))
        record["seconds"] = time.perf_counter() - started
        for name, (had_value, value) in originals.items():
            if had_value:
                setattr(test, name, value)
            else:
                delattr(test, name)
        super().stopTest(test)

    def _status(self, test, status):
        active = self._active.get(id(test))
        if active:
            active[0]["status"] = status

    def addSuccess(self, test):
        self._status(test, "passed")
        super().addSuccess(test)

    def addError(self, test, err):
        self._status(test, "error")
        super().addError(test, err)

    def addFailure(self, test, err):
        self._status(test, "failed")
        super().addFailure(test, err)

    def addSkip(self, test, reason):
        self._status(test, "skipped")
        super().addSkip(test, reason)

    def addExpectedFailure(self, test, err):
        self._status(test, "expected_failure")
        super().addExpectedFailure(test, err)

    def addUnexpectedSuccess(self, test):
        self._status(test, "unexpected_success")
        super().addUnexpectedSuccess(test)

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self._status(test, "failed" if issubclass(err[0], test.failureException) else "error")
        super().addSubTest(test, subtest, err)


def validate_reports(reports, shard_count, *, require_database=True):
    """Fail closed on missing shards, dropped tests, duplicate runs or failures."""
    if len(reports) != shard_count:
        raise ValueError(f"Expected {shard_count} shard reports, found {len(reports)}")
    if {report["shard_index"] for report in reports} != set(range(shard_count)):
        raise ValueError("Missing or duplicate shard indexes")
    discovered = reports[0]["discovered_ids"]
    if not discovered or len(set(discovered)) != len(discovered):
        raise ValueError("Empty or duplicate discovered test IDs")
    selected = []
    executed = []
    for report in reports:
        if report["schema_version"] != 1 or report["shard_count"] != shard_count:
            raise ValueError("Incompatible shard report")
        if report["discovered_ids"] != discovered:
            raise ValueError("Shards disagree about the discovered test inventory")
        if not report["success"] or report["failures"] or report["errors"] or report["unexpected_successes"]:
            raise ValueError(f"Shard {report['shard_index']} failed")
        if require_database and (not report["require_database"] or report["skipped"]):
            raise ValueError("Database tests must be enabled and no tests may be skipped in CI")
        if Counter(report["selected_ids"]) != Counter(report["executed_ids"]):
            raise ValueError("A shard did not execute exactly its selected tests")
        if Counter(record["id"] for record in report["tests"]) != Counter(report["executed_ids"]):
            raise ValueError("Test timing records do not match executed tests")
        allowed_statuses = {"passed", "expected_failure"}
        if not require_database:
            allowed_statuses.add("skipped")
        if any(record["status"] not in allowed_statuses for record in report["tests"]):
            raise ValueError("A test did not complete successfully")
        selected.extend(report["selected_ids"])
        executed.extend(report["executed_ids"])
    if Counter(selected) != Counter(discovered) or Counter(executed) != Counter(discovered):
        raise ValueError("Missing or duplicate tests across shards")
    return len(discovered)


def write_summary(reports, path, *, verified=False):
    lines = ["## Backend test timings", "", "| Shard | Tests | Suite | Setup | Test bodies | Teardown + cleanup |", "| --- | ---: | ---: | ---: | ---: | ---: |"]
    records = []
    for report in sorted(reports, key=lambda value: value["shard_index"]):
        tests = report["tests"]
        records.extend(tests)
        setup = sum(test["setup_seconds"] for test in tests)
        body = sum(test["test_seconds"] for test in tests)
        teardown = sum(test["teardown_seconds"] + test["cleanup_seconds"] for test in tests)
        lines.append(f"| {report['shard_index'] + 1} | {len(tests)} | {report['seconds']:.2f}s | {setup:.2f}s | {body:.2f}s | {teardown:.2f}s |")
    if verified:
        lines.extend(["", f"Verified: **{len(records)} tests**, each discovered test executed exactly once, no skipped tests."])
    lines.extend(["", "Suite time includes shared class/module fixtures and runner overhead; phase times measure per-test hooks.", "", "### Slowest tests", "", "| Test | Total | Setup |", "| --- | ---: | ---: |"])
    for test in sorted(records, key=lambda record: record["seconds"], reverse=True)[:15]:
        lines.append(f"| `{test['id']}` | {test['seconds']:.3f}s | {test['setup_seconds']:.3f}s |")
    modules = defaultdict(float)
    for test in records:
        modules[test["id"].rsplit(".", 2)[0]] += test["seconds"]
    lines.extend(["", "### Slowest modules", "", "| Module | Per-test total |", "| --- | ---: |"])
    for module, seconds in sorted(modules.items(), key=lambda item: item[1], reverse=True)[:15]:
        lines.append(f"| `{module}` | {seconds:.3f}s |")
    with Path(path).open("a", encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")


def require_test_database():
    url = urlparse(os.environ.get("CATALOG_TEST_DATABASE_URL", ""))
    if url.hostname not in {"localhost", "127.0.0.1", "::1"} or not url.path.endswith("_catalog_test"):
        raise ValueError("CI requires CATALOG_TEST_DATABASE_URL for an isolated localhost *_catalog_test database")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--require-database", action="store_true")
    parser.add_argument("--verify-reports", help="Glob matching all downloaded shard JSON reports")
    parser.add_argument("--plan", action="store_true", help="Print the complete shard inventory without executing tests")
    args = parser.parse_args(argv)
    if args.shard_count < 1 or not 0 <= args.shard_index < args.shard_count:
        parser.error("Require shard-count > 0 and 0 <= shard-index < shard-count")
    try:
        if args.verify_reports:
            reports = [json.loads(Path(path).read_text()) for path in sorted(glob.glob(args.verify_reports, recursive=True))]
            count = validate_reports(reports, args.shard_count)
            if args.summary:
                write_summary(reports, args.summary, verified=True)
            print(f"Verified {count} tests across {args.shard_count} shards: no missing, duplicate or skipped tests")
            return 0
        if args.require_database and not args.plan:
            require_test_database()
        tests = discover_tests()
        weights = json.loads(WEIGHTS_PATH.read_text())
        shards, estimates = plan_shards(tests, args.shard_count, weights)
        if args.plan:
            print(json.dumps([{"shard_index": index, "estimated_seconds": estimates[index], "test_ids": [test.id() for test in shard]} for index, shard in enumerate(shards)], indent=2))
            return 0
        selected = shards[args.shard_index]
        selected_ids = [test.id() for test in selected]
        # Tests may legitimately assign their own ``self.id`` during setUp.
        # Capture discovery identities before executing any fixtures.
        discovered_ids = sorted(test.id() for test in tests)
        original_ids = {id(test): test.id() for test in tests}

        def report_id(test):
            return original_ids.get(id(test), str(test))

        started = time.perf_counter()
        result = unittest.TextTestRunner(verbosity=2, resultclass=TimingResult).run(unittest.TestSuite(selected))
        report = {
            "schema_version": 1,
            "shard_index": args.shard_index,
            "shard_count": args.shard_count,
            "require_database": args.require_database,
            "discovered_ids": discovered_ids,
            "selected_ids": selected_ids,
            "executed_ids": [record["id"] for record in result.records],
            "tests": result.records,
            "seconds": time.perf_counter() - started,
            "failures": [report_id(test) for test, _ in result.failures],
            "errors": [report_id(test) for test, _ in result.errors],
            "skipped": [report_id(test) for test, _ in result.skipped],
            "unexpected_successes": [report_id(test) for test in result.unexpectedSuccesses],
            "success": result.wasSuccessful() and (not args.require_database or not result.skipped),
        }
        report["success"] = report["success"] and Counter(report["executed_ids"]) == Counter(selected_ids)
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        if args.summary:
            write_summary([report], args.summary)
        return 0 if report["success"] else 1
    except (ValueError, KeyError, TypeError, OSError) as error:
        print(f"CI test verification failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
