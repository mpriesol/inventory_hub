"""Authentication, bounded source selection and read-only export fidelity."""
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit
from uuid import uuid4
import zipfile

import asyncpg
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import NullPool

from current_schema_fixture import apply_current_core_schema
from inventory_hub.database import get_session
from inventory_hub.routers import migration_export as routes
from inventory_hub.services import migration_export as service
from inventory_hub.settings import settings


class ExportRoutesTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(routes.router)
        self.sessions = []
        async def session():
            self.sessions.append(True)
            yield object()
        app.dependency_overrides[get_session] = session
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        token = "migration-test-only-" + "x" * 30
        self.headers = {"Authorization": "Bearer " + token}
        p = patch.object(settings, "AI_CONTENT_ACCESS_TOKEN", SecretStr(token))
        p.start()
        self.addCleanup(p.stop)

    def test_anonymous_export_never_reads_database_or_remote(self):
        with patch.object(routes, "_fetch_upgates_products") as upstream:
            response = self.client.get("/migration/catalog-export")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.sessions, [])
        upstream.assert_not_called()

    def test_unknown_shop_is_rejected_before_remote_read(self):
        with patch.object(routes, "_fetch_upgates_products") as upstream:
            response = self.client.get("/migration/catalog-export?shop_code=https://example.org", headers=self.headers)
        self.assertEqual(response.status_code, 422)
        upstream.assert_not_called()

    def test_authenticated_download_closes_temp_and_propagates_freshness(self):
        output = io.BytesIO(b"archive-content")
        with patch.object(routes, "_fetch_upgates_products", return_value=([{"code": "001"}], {"source": "api"})) as upstream, \
                patch.object(routes, "build_export", AsyncMock(return_value=output)) as build:
            response = self.client.get("/migration/catalog-export?shop_code=biketrek&refresh=true", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"archive-content")
        self.assertIn("attachment", response.headers["content-disposition"])
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertTrue(output.closed)
        upstream.assert_called_once_with("biketrek", ttl_s=routes.PREVIEW_CACHE_TTL_S, force_refresh=True)
        self.assertEqual(build.await_args.args[1:], ([{"code": "001"}], {"source": "api"}, "biketrek"))


TEST_URL = os.environ.get("CATALOG_TEST_DATABASE_URL", "")


@unittest.skipUnless(TEST_URL, "Set CATALOG_TEST_DATABASE_URL to an isolated localhost *_catalog_test DB")
class ExportDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        parsed = urlsplit(TEST_URL)
        if parsed.hostname not in ("localhost", "127.0.0.1") or not parsed.path.endswith("_catalog_test"):
            raise RuntimeError("Export tests require a dedicated localhost *_catalog_test database")
        self.schema = "migration_export_test_" + uuid4().hex
        root = Path(__file__).resolve().parents[2] / "infra" / "db-init"
        connection = await asyncpg.connect(TEST_URL)
        try:
            await connection.execute(f'CREATE SCHEMA "{self.schema}"')
            await connection.execute(f'SET search_path TO "{self.schema}"')
            for name in ("001_schema.sql", "004_shop_product_content.sql", "012_product_editor.sql", "029_sku_policy.sql"):
                await connection.execute((root / name).read_text())
            await apply_current_core_schema(connection)
            await connection.execute("INSERT INTO products(sku,name) VALUES ('00042-X','Test exact identity')")
        finally:
            await connection.close()
        self.engine = create_async_engine(TEST_URL.replace("postgresql://", "postgresql+asyncpg://", 1),
            poolclass=NullPool, connect_args={"server_settings": {"search_path": self.schema}})
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False, autoflush=False)

    async def asyncTearDown(self):
        await self.engine.dispose()
        connection = await asyncpg.connect(TEST_URL)
        try:
            await connection.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        finally:
            await connection.close()

    async def test_complete_snapshot_preserves_nulls_and_never_confirms_stock(self):
        remote = [{"code": "xTrek", "variants": [{"code": "00042-X", "ean": "0012345678905", "stock": None}]}]
        async with self.sessions() as db:
            output = await service.build_export(db, remote, {"source": "api", "pulled_at": "2026-10-27T12:00:00"}, "biketrek")
            self.assertEqual(await db.scalar(text("SHOW transaction_read_only")), "on")
            self.assertEqual(await db.scalar(text("SHOW transaction_isolation")), "repeatable read")
            with output, zipfile.ZipFile(output) as archive:
                manifest = json.loads(archive.read("manifest.json"))
                rows = [json.loads(line) for line in archive.read("products.jsonl").splitlines()]
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["sku"], "00042-X")
                self.assertIsNone(rows[0]["brand"])
                self.assertEqual(manifest["counts"]["products"], 1)
                self.assertEqual(manifest["counts"]["tracking"], 0)
                self.assertEqual(json.loads(archive.read("upgates_products.jsonl")), remote[0])
                for name, count in manifest["counts"].items():
                    self.assertEqual(len(archive.read(name + ".jsonl").splitlines()), count)
                self.assertNotIn(b"config_path", archive.read("suppliers.jsonl"))
            self.assertEqual(await db.scalar(text("SELECT count(*) FROM stock_movements")), 0)
