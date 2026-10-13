"""Persistent secret isolation, atomic replacement and safe operator endpoints."""
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pydantic import SecretStr

from inventory_hub.routers.sloger_probe import router
from inventory_hub.services import sloger_credentials as service, sloger_probe
from inventory_hub.services.catalog import CatalogError
from inventory_hub.settings import settings
from inventory_hub.sloger_probe_types import ProbeRequest, SlogerCredentials


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

    def test_missing_login_and_partial_transient_pair_are_rejected(self):
        with self.assertRaises(CatalogError) as caught:
            service.load()
        self.assertEqual(caught.exception.code, "sloger_credentials_not_configured")
        request = {"request_id": uuid4(), "sku": "TEST-1"}
        self.assertIsNone(ProbeRequest(**request).password)
        for extra in ({"username": "test"}, {"password": "secret"}):
            with self.assertRaises(ValueError):
                ProbeRequest(**request, **extra)


class CredentialsRouteTests(unittest.IsolatedAsyncioTestCase):
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
                    patch.object(sloger_probe, "probe", AsyncMock()) as probe:
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


class CredentialsReservationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.credentials = SlogerCredentials(username="saved-login@example.test", password="saved-private-password")
        self.db = SimpleNamespace(execute=AsyncMock(), get=AsyncMock(return_value=None),
                                  add=Mock(), flush=AsyncMock())

        @asynccontextmanager
        async def sessions():
            yield self.db

        item = patch.object(sloger_probe, "get_session_context", sessions)
        item.start()
        self.addCleanup(item.stop)

    async def test_saved_secret_sku_is_rejected_before_any_audit_or_paid_call(self):
        with patch.object(service, "load", return_value=self.credentials) as load, \
                patch.object(sloger_probe, "call_provider", AsyncMock()) as provider:
            for identity in (self.credentials.username, self.credentials.password.get_secret_value()):
                with self.subTest(identity="redacted"):
                    request = ProbeRequest(request_id=uuid4(), sku="prefix-" + identity)
                    with self.assertRaises(CatalogError) as caught:
                        await sloger_probe.probe(request)
                    self.assertEqual(caught.exception.code, "sloger_probe_request_invalid")
                    self.assertNotIn(identity, str(caught.exception))
            self.assertEqual(load.call_count, 2)
            self.db.add.assert_not_called()
            self.db.flush.assert_not_awaited()
            provider.assert_not_awaited()

    async def test_missing_saved_credentials_create_no_audit_or_paid_call(self):
        with patch.object(service, "load", side_effect=CatalogError("sloger_credentials_not_configured", "missing", 409)), \
                patch.object(sloger_probe, "call_provider", AsyncMock()) as provider:
            with self.assertRaises(CatalogError) as caught:
                await sloger_probe.probe(ProbeRequest(request_id=uuid4(), sku="TEST-1"))
            self.assertEqual(caught.exception.code, "sloger_credentials_not_configured")
            self.db.add.assert_not_called()
            provider.assert_not_awaited()

    async def test_saved_credentials_read_once_before_safe_audit_and_replay_ignores_removed_store(self):
        request = ProbeRequest(request_id=uuid4(), sku="TEST-1")
        with patch.object(service, "load", return_value=self.credentials) as load, \
                patch.object(settings, "AI_CONTENT_ENABLED", True), \
                patch.object(settings, "OPENAI_API_KEY", SecretStr("fixture-key")), \
                patch.object(sloger_probe.ai_content, "budget", AsyncMock(return_value=0)):
            outcome, credentials = await sloger_probe.reserve(request)
            self.assertEqual(credentials, self.credentials)
            load.assert_called_once_with()
            batch, job = [call.args[0] for call in self.db.add.call_args_list]
            for secret in (credentials.username, credentials.password.get_secret_value()):
                self.assertNotIn(secret, json.dumps(job.context))
                self.assertNotIn(secret, json.dumps(outcome))
            self.db.get.side_effect = [batch, job]
            load.reset_mock()
            load.side_effect = AssertionError("Replay must not read credentials")
            replay, credentials = await sloger_probe.reserve(request)
            self.assertEqual(replay, outcome)
            self.assertIsNone(credentials)
            load.assert_not_called()
