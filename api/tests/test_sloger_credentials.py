"""Persistent secret isolation, atomic replacement and safe operator endpoints."""
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pydantic import SecretStr

from inventory_hub.routers.sloger_probe import router
from inventory_hub.services import sloger_credentials as service, ai_content_provider
from inventory_hub.services.catalog import CatalogError
from inventory_hub.settings import settings
from inventory_hub.sloger_probe_types import SlogerCredentials


class CredentialsStorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "private"
        self.public = Path(self.temporary.name) / "public"
        self.public.mkdir()
        self.credentials = SlogerCredentials(username="test-login@example.test", password="fixture-private-password")
        for name, value in (("SUPPLIER_CREDENTIALS_ROOT", self.root), ("INVENTORY_DATA_ROOT", self.public)):
            item = patch.object(settings, name, value)
            item.start()
            self.addCleanup(item.stop)

    def test_restart_style_read_and_atomic_replacement_never_return_password(self):
        self.assertEqual(service.status(), {"configured": False, "username": None})
        self.assertEqual(service.save(self.credentials), {"configured": True, "username": self.credentials.username})
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.root / service.FILENAME).stat().st_mode), 0o600)
        self.assertEqual(service.load(), self.credentials)
        changed = SlogerCredentials(username="other-login@example.test", password="replacement-password")
        self.assertEqual(service.save(changed), {"configured": True, "username": changed.username})
        self.assertEqual(service.load(), changed)
        self.assertEqual(list(self.root.iterdir()), [self.root / service.FILENAME])
        self.assertNotIn(changed.password.get_secret_value(), str(service.status()))
        self.assertFalse(list(self.public.iterdir()))

    def test_failed_replace_keeps_previous_login_and_removes_temporary_secret(self):
        service.save(self.credentials)
        with patch.object(service.os, "replace", side_effect=OSError("synthetic failure")):
            with self.assertRaises(CatalogError) as caught:
                service.save(SlogerCredentials(username="different", password="second-password"))
        self.assertEqual(caught.exception.code, "sloger_credentials_unavailable")
        self.assertEqual(service.load(), self.credentials)
        self.assertEqual(list(self.root.iterdir()), [self.root / service.FILENAME])

    def test_public_paths_symlinks_and_loose_permissions_are_refused(self):
        for path in (self.public, self.public / "secrets"):
            with self.subTest(path=path), patch.object(settings, "SUPPLIER_CREDENTIALS_ROOT", path):
                with self.assertRaises(CatalogError):
                    service.save(self.credentials)
        self.root.symlink_to(self.public, target_is_directory=True)
        with self.assertRaises(CatalogError):
            service.save(self.credentials)
        self.root.unlink()
        service.save(self.credentials)
        target = self.root / service.FILENAME
        target.chmod(0o644)
        with self.assertRaises(CatalogError):
            service.load()
        target.chmod(0o600)
        self.root.chmod(0o755)
        self.assertTrue(service.status()["configured"])
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        target.unlink()
        target.symlink_to(self.public / "not-a-secret")
        with self.assertRaises(CatalogError):
            service.save(self.credentials)
        with self.assertRaises(CatalogError):
            service.load()

    def test_malformed_oversized_and_nonregular_files_are_safe_failures(self):
        service.save(self.credentials)
        target = self.root / service.FILENAME
        for raw in (b"malformed-private-content", b"a" * (service.MAX_BYTES + 1),
                    json.dumps({"username": "test", "password": "fixture-private-password", "unexpected": 1}).encode()):
            target.write_bytes(raw)
            with self.assertRaises(CatalogError) as caught:
                service.load()
            self.assertNotIn("fixture-private-password", str(caught.exception))
            self.assertNotIn("malformed-private-content", str(caught.exception))
            self.assertEqual(caught.exception.code, "sloger_credentials_unavailable")
        target.unlink()
        os.mkfifo(target, 0o600)
        with self.assertRaises(CatalogError):
            service.load()

    def test_missing_login_is_rejected(self):
        with self.assertRaises(CatalogError) as caught:
            service.load()
        self.assertEqual(caught.exception.code, "sloger_credentials_not_configured")


class CredentialsRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_account_operator_can_check_status_but_only_admin_can_replace_credentials(self):
        app = FastAPI()
        app.include_router(router)

        @app.middleware("http")
        async def account_fixture(request, call_next):
            request.state.hub_user = {"role": request.headers.get("x-test-role", "operator")}
            request.state.hub_bearer = True
            return await call_next(request)

        payload = {"username": "test-login@example.test", "password": "synthetic-password"}
        status = {"configured": True, "username": payload["username"]}
        with patch.object(service, "save", return_value=status) as save, patch.object(service, "status", return_value=status):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://hub.example.test") as client:
                path = "/suppliers/sloger/lookup/credentials"
                read = await client.get(path)
                self.assertEqual(read.status_code, 200)
                denied = await client.put(path, json=payload)
                self.assertEqual(denied.status_code, 403)
                self.assertEqual(denied.json()["detail"]["code"], "hub_admin_required")
                save.assert_not_called()
                accepted = await client.put(path, json=payload, headers={"x-test-role": "admin"})
                self.assertEqual(accepted.status_code, 200)
                save.assert_called_once()
                self.assertNotIn(payload["password"], accepted.text)
                self.assertEqual(accepted.headers["cache-control"], "no-store")

    async def test_operator_only_uncached_save_status_and_public_data_isolation(self):
        with tempfile.TemporaryDirectory() as temporary:
            public = Path(temporary) / "public"
            public.mkdir()
            private = Path(temporary) / "private"
            app = FastAPI()
            app.include_router(router)
            app.mount("/data", StaticFiles(directory=public), name="data")
            token = "fixture-operator-token-only-123456789"
            headers = {"Authorization": "Bearer " + token}
            payload = {"username": "test-login@example.test", "password": "fixture-private-password"}
            with patch.object(settings, "SUPPLIER_CREDENTIALS_ROOT", private), \
                    patch.object(settings, "INVENTORY_DATA_ROOT", public), \
                    patch.object(settings, "AI_CONTENT_ACCESS_TOKEN", SecretStr(token)), \
                    patch.object(ai_content_provider, "generate", AsyncMock()) as probe:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://hub.example.test") as client:
                    path = "/suppliers/sloger/lookup/credentials"
                    for method in ("GET", "PUT"):
                        denied = await client.request(method, path, json=payload if method == "PUT" else None)
                        self.assertEqual(denied.status_code, 401)
                        self.assertEqual(denied.headers["cache-control"], "no-store")
                    self.assertFalse(private.exists())
                    before = await client.get(path, headers=headers)
                    self.assertEqual(before.json(), {"configured": False, "username": None})
                    saved = await client.put(path, headers=headers, json=payload)
                    self.assertEqual(saved.status_code, 200)
                    self.assertEqual(saved.json(), {"configured": True, "username": payload["username"]})
                    after = await client.get(path, headers=headers)
                    self.assertEqual(after.json(), saved.json())
                    invalid = await client.put(path, headers=headers, json={**payload, "username": " "})
                    self.assertEqual(invalid.status_code, 422)
                    with patch.object(service.os, "replace", side_effect=OSError(payload["password"])):
                        failed = await client.put(path, headers=headers, json=payload)
                    self.assertEqual(failed.status_code, 503)
                    for response in (before, saved, after, invalid, failed):
                        self.assertNotIn(payload["password"], response.text)
                        self.assertEqual(response.headers["cache-control"], "no-store")
                    for url in ("/data/sloger.json", "/data/supplier-secrets/sloger.json", "/data/%2e%2e/private/sloger.json"):
                        self.assertEqual((await client.get(url)).status_code, 404)
                    probe.assert_not_awaited()
