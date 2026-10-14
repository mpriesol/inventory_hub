"""Verify the deployed application wiring, without workers or external I/O."""
import importlib.util
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from pydantic import SecretStr

from inventory_hub import database, logging_setup
from inventory_hub.routers import auth
from inventory_hub.settings import settings


TOKEN = "synthetic-application-security-token-123456"


class ApplicationSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.enterContext(patch.object(settings, "INVENTORY_DATA_ROOT", self.root))
        self.enterContext(patch.object(settings, "AI_CONTENT_ACCESS_TOKEN", SecretStr(TOKEN)))
        self.enterContext(patch.object(settings, "USE_POSTGRES", True))
        self.enterContext(patch.object(logging_setup, "setup_logging"))
        self.database_init = self.enterContext(patch.object(database, "init_db", AsyncMock(
            side_effect=AssertionError("Security requests must not initialize a database"))))

        # Execute the real application module with production routers enabled.
        # Do not replace inventory_hub.main in sys.modules or run its lifespan;
        # other tests retain their own fixtures and no worker can be started.
        module_path = Path(__file__).resolve().parents[1] / "inventory_hub" / "main.py"
        spec = importlib.util.spec_from_file_location("inventory_hub._security_application_fixture", module_path)
        self.main = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.main)
        self.app = self.main.app
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="https://hub.test")
        self.addAsyncCleanup(self.client.aclose)

    async def asyncTearDown(self):
        self.database_init.assert_not_awaited()

    async def test_every_registered_private_endpoint_rejects_anonymous_requests_before_its_handler(self):
        attempted = set()

        async def forbidden_handler(scope, receive, send):
            raise AssertionError("An anonymous request reached a business handler")

        for route in self.app.routes:
            if not getattr(route, "methods", None):
                continue
            path = re.sub(r"\{[^}]+\}", "security-fixture", route.path)
            private_methods = set(route.methods) - {
                method for method, public_path in auth.PUBLIC_ENDPOINTS if public_path == route.path
            }
            if not private_methods:
                continue
            route.app = forbidden_handler
            for method in sorted(private_methods):
                with self.subTest(method=method, path=route.path):
                    response = await self.client.request(method, path, content=b"confidential-invalid-body")
                    self.assertEqual(response.status_code, 401)
                    self.assertEqual(response.headers["cache-control"], "no-store")
                    self.assertNotIn("confidential-invalid-body", response.text)
                    attempted.add((method, route.path))

        # These concrete production areas must be registered in this fixture;
        # testing a reduced legacy app would miss the warehouse API entirely.
        for fragment in ("/stock", "/receiving/sessions", "/shops", "/suppliers", "/product-imports", "/upgates/orders"):
            self.assertTrue(any(fragment in path for _, path in attempted), fragment)
        self.assertEqual(list(self.root.iterdir()), [])

    async def test_private_data_mount_and_document_routes_require_authentication(self):
        for path in ("/data/console/config.json", "/data/suppliers/sloger/state/config.json",
                     "/files/download?relpath=console/config.json", "/files/preview?relpath=console/config.json",
                     "/files/stat?relpath=console/config.json"):
            with self.subTest(path=path):
                response = await self.client.get(path)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.headers["cache-control"], "no-store")

    async def test_cors_preflight_never_unlocks_the_requested_write(self):
        path = "/shops/synthetic/config"
        allowed_origin = "http://localhost:5173"
        response = await self.client.options(path, headers={
            "Origin": allowed_origin,
            "Access-Control-Request-Method": "PUT",
            "Access-Control-Request-Headers": "Content-Type,X-Hub-Request",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["access-control-allow-origin"], allowed_origin)
        response = await self.client.put(path, json={"password": "private-input"}, headers={
            "Origin": allowed_origin, "X-Hub-Request": "1"})
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("private-input", response.text)
        response = await self.client.options(path, headers={
            "Origin": "https://attacker.test", "Access-Control-Request-Method": "PUT"})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("access-control-allow-origin", response.headers)
        self.assertEqual(list(self.root.iterdir()), [])

    async def test_public_health_reports_failure_without_connection_details(self):
        secret = "postgresql://synthetic-user:private-password@private-host/internal-database"
        with patch.object(self.main, "check_db_health", AsyncMock(return_value={
            "status": "unhealthy", "error": secret, "host": "private-host"})):
            response = await self.client.get("/health")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["status"], "degraded")
        self.assertEqual(response.json()["database"], {"status": "unhealthy"})
        self.assertNotIn("private", response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        with patch.object(self.main, "check_db_health", AsyncMock(return_value={
            "status": "healthy", "version": "private-database-version"})):
            response = await self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["database"], {"status": "healthy"})
        self.assertNotIn("private-database-version", response.text)

    async def test_authenticated_validation_does_not_echo_credentials_on_legacy_routes(self):
        response = await self.client.post("/runs/prepare_legacy", headers={"Authorization": "Bearer " + TOKEN},
            json={"invoice_relpath": {"password": "synthetic-private-password"}})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("synthetic-private-password", response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(list(self.root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
