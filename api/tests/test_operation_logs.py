import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from inventory_hub import operation_log as log
from inventory_hub.adapters import husky_b2b as b2b
from inventory_hub.operation_middleware import OperationMiddleware
from inventory_hub.routers.operation_logs import router
from husky_fixtures import page


class OperationLogTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        root = patch.object(log.settings, "INVENTORY_DATA_ROOT", self.root)
        root.start(); self.addCleanup(root.stop)

    def emit(self, **fields):
        return log.emit("test_operation", component="husky", **fields)

    def test_rotation_is_bounded_and_cursor_survives_new_writes(self):
        with patch.object(log, "MAX_BYTES", 900):
            for number in range(20):
                self.assertTrue(self.emit(run_id=number + 1))
            files = list((self.root / "logs").glob("operations.jsonl*"))
            self.assertEqual(len(files), 4)
            self.assertLessEqual(sum(p.stat().st_size for p in files), 3600)
            first = log.read(limit=2)
            self.assertEqual([e["run_id"] for e in first["items"]], [20, 19])
            self.emit(run_id=21)
            second = log.read(limit=2, before=first["next_cursor"])
            self.assertEqual([e["run_id"] for e in second["items"]], [18, 17])
            for number in range(50):
                self.emit(run_id=100 + number)
            with self.assertRaises(log.CursorExpired):
                log.read(before=first["next_cursor"])

    def test_parallel_writers_do_not_interleave_and_context_crosses_threads(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertTrue(all(pool.map(lambda i: self.emit(run_id=i + 1), range(100))))
        result = log.read(limit=200)
        self.assertEqual({x["run_id"] for x in result["items"]}, set(range(1, 101)))
        async def work():
            with log.context(supplier="husky", run_id=987):
                await asyncio.to_thread(self.emit)
        asyncio.run(work())
        self.assertEqual(log.read(run_id=987)["items"][0]["supplier"], "husky")
        self.emit()
        self.assertNotIn("run_id", log.read(limit=1)["items"][0])

    def test_filters_and_redaction_on_both_write_and_read(self):
        self.emit(supplier="husky", run_id=7, level="error", request_id="a" * 32,
                  password="secret-value", headers={"Authorization": "secret-value"}, body="secret-value",
                  target="https://private.test/path?token=secret-value", reason="secret value\n")
        self.emit(supplier="other", run_id=8)
        result = log.read(component="husky", supplier="husky", level="error", run_id=7, request_id="a" * 32)
        self.assertEqual(len(result["items"]), 1)
        entry = result["items"][0]
        self.assertNotIn("secret-value", json.dumps(entry))
        self.assertNotIn("target", entry)
        self.assertFalse(log.read(since="2999-01-01T00:00:00.000000Z")["items"])
        self.assertFalse(log.read(until="2000-01-01T00:00:00.000000Z")["items"])
        path = self.root / "logs/operations.jsonl"
        with path.open("a") as stream:
            stream.write('{broken}\n' + json.dumps({**entry, "password": "read-secret", "url": "read-secret"}) + '\n')
        self.assertNotIn("read-secret", json.dumps(log.read()))

    def test_logging_failure_and_symlink_do_not_break_operations(self):
        with patch.object(log.os, "open", side_effect=OSError("sensitive path")):
            self.assertFalse(self.emit())
            with self.assertRaises(log.LogUnavailable):
                log.read()
        (self.root / "logs").mkdir(exist_ok=True)
        target = self.root / "private.txt"; target.write_text("do not change")
        (self.root / "logs/operations.jsonl").symlink_to(target)
        self.assertFalse(self.emit())
        self.assertEqual(target.read_text(), "do not change")
        with self.assertRaises(log.LogUnavailable):
            log.read()

    def test_protected_api_validation_and_request_route_redaction(self):
        app = FastAPI(); app.include_router(router); app.add_middleware(OperationMiddleware)
        @app.get("/orders/{order_id}")
        def order(order_id: str):
            return {"ok": True}
        @app.get("/explode")
        def explode():
            raise ValueError("private exception message")
        client = TestClient(app, raise_server_exceptions=False)
        token = "synthetic-operation-test-token-123"
        with patch.object(log.settings, "AI_CONTENT_ACCESS_TOKEN", SecretStr(token)):
            response = client.get("/logs/events")
            self.assertEqual(response.status_code, 401)
            headers = {"Authorization": "Bearer " + token}
            for query in ("limit=201", "before=../private", "run_id=0", "since=2026-01-01", "since=2026-02-01T00:00:00Z&until=2026-01-01T00:00:00Z"):
                self.assertEqual(client.get("/logs/events?" + query, headers=headers).status_code, 422)
            self.assertEqual(client.get("/logs/events?before=" + "0" * 32, headers=headers).status_code, 409)
            response = client.get("/orders/private-customer?token=private-secret", headers={"X-Request-ID": "attacker-secret"})
            request_id = response.headers["X-Request-ID"]
            self.assertEqual(len(request_id), 32)
            entry = log.read()["items"][0]
            self.assertEqual((entry["target"], entry["request_id"], entry["status_code"]), ("/orders/{order_id}", request_id, 200))
            self.assertNotIn("private", json.dumps(entry))
            self.assertEqual(client.get("/explode").status_code, 500)
            self.assertEqual(log.read()["items"][0]["level"], "error")
            response = client.get("/logs/events", headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertNotIn("private exception", response.text)
            self.assertFalse(any(e.get("target") == "/logs/events" for e in response.json()["items"]))

    def test_husky_pager_diagnostics_preserve_the_failure_and_run(self):
        with log.context(run_id=1240):
            with self.assertRaises(b2b.HuskyError):
                b2b.parse_page(b2b.document(page(current=1, next_page=2)), 2)
        entry = log.read()["items"][0]
        self.assertEqual((entry["reason"], entry["page"], entry["observed_page"], entry["run_id"]), ("pager_marker_mismatch", 2, 1, 1240))

    def test_husky_http_metadata_never_includes_login_body_or_response(self):
        class Response:
            status_code = 403
            headers = {"Set-Cookie": "secret-cookie"}
            def __enter__(self): return self
            def __exit__(self, *args): return False
        client = SimpleNamespace(request=lambda *args, **kwargs: Response())
        with self.assertRaises(b2b.HuskyError):
            b2b.Session(client, time.monotonic() + 10).read(b2b.LOGIN, {"password": "secret-password"})
        entry = log.read()["items"][0]
        self.assertEqual((entry["status_code"], entry["target"], entry["level"]), (403, "husky_login", "error"))
        self.assertNotIn("secret", json.dumps(entry))


if __name__ == "__main__":
    unittest.main()
