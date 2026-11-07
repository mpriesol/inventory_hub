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

    def test_local_only_download_never_reads_upgates_even_when_refresh_requested(self):
        for refresh in ("false", "true"):
            with self.subTest(refresh=refresh):
                output = io.BytesIO(b"local-archive")
                with patch.object(routes, "_fetch_upgates_products", side_effect=AssertionError("Unexpected Upgates read")) as upstream, \
                        patch.object(routes, "build_export", AsyncMock(return_value=output)) as build:
                    response = self.client.get(
                        f"/migration/catalog-export?shop_code=xtrek&local_only=true&refresh={refresh}",
                        headers=self.headers)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, b"local-archive")
                self.assertTrue(output.closed)
                upstream.assert_not_called()
                self.assertEqual(build.await_args.args[1:],
                    ([], {"source": "local_only", "catalog_included": False}, "xtrek"))


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


    async def test_atomer_tracker_commits_complete_groups_and_keeps_previous_run_after_failure(self):
        from inventory_hub.services import atomer_progress_store as store
        from inventory_hub.services.migration_upgates_export import ExportError
        sql = Path(__file__).resolve().parents[2] / "infra/db-init/037_atomer_progress.sql"
        connection = await asyncpg.connect(TEST_URL)
        try:
            await connection.execute(f'SET search_path TO "{self.schema}"')
            for path in (sql,sql.with_name("038_atomer_review.sql")):
                await connection.execute(path.read_text())
                await connection.execute(path.read_text())
            await connection.execute("INSERT INTO shops(code,name,platform) VALUES ('xtrek','xTrek','upgates'),('biketrek','BIKETREK','upgates') ON CONFLICT (code) DO NOTHING")
        finally:
            await connection.close()
        rows = [dict(code="one", parent="F", name="Family One", ean="", brand="", axes={}, duplicates=1, conflict=False),
                dict(code="two", parent="F", name="Family Two", ean="", brand="", axes={}, duplicates=1, conflict=False)]
        with patch.object(store.progress, "remote_catalog", return_value=([],{})):
            async with self.sessions() as db:
                saved = await store.refresh(db,("a"*64,2,rows),0)
            async with self.sessions() as db:
                page = await store.page(db,"variant","needs_both","no_card","missing","family",0,1)
            self.assertEqual((page["total"],page["families"],len(page["groups"][0]["items"])), (2,1,2))
            async with self.sessions() as db:
                with self.assertRaisesRegex(store.ProgressError,"run_changed"):
                    await store.refresh(db,None,0)
        with patch.object(store.progress,"remote_catalog",side_effect=ExportError("interrupted")):
            async with self.sessions() as db:
                with self.assertRaises(ExportError):
                    await store.refresh(db,None,saved["id"])
        async with self.sessions() as db:
            self.assertEqual((await store.latest(db))["id"],saved["id"])

    async def test_atomer_review_cas_queue_scope_refresh_and_approval(self):
        from inventory_hub.services import atomer_progress_store as store, atomer_review as review
        from inventory_hub.services.atomer_review_targets import ReviewError
        root = Path(__file__).resolve().parents[2] / "infra/db-init"
        async with self.sessions() as db:
            for filename in ("037_atomer_progress.sql","038_atomer_review.sql"):
                connection = await db.connection()
                raw = await connection.get_raw_connection()
                await raw.driver_connection.execute((root/filename).read_text())
            await db.execute(text("INSERT INTO shops(code,name,platform) VALUES ('xtrek','X','upgates'),('biketrek','B','upgates') ON CONFLICT DO NOTHING"))
            await db.commit()
        src = [dict(code="kept",parent="F",name="Family",ean="",brand="",axes={},duplicates=1,conflict=False)]
        remote = [dict(pid="2",vid="3",parent="F",code="kept",ean="",brand="",axes={}),
                  dict(pid="2",vid="4",parent="F",code="other",ean="",brand="",axes={"size":["l"]},stock=0)]
        pins={"2/4":dict(product_id="2",variant_id="4",parent="F",source_parent="F",code="other",ean="",axes={"size":["l"]})}
        async def action(kind,key,revision,command,**fields):
            async with self.sessions() as db:
                return await review.edit(db,review.Edit(kind=kind,key=key,revision=revision,command=command,run_id=run,**fields),"tester")
        with patch.object(store.progress,"remote_catalog",return_value=(remote,{})):
            async with self.sessions() as db:
                run=(await store.refresh(db,("a"*64,1,src),0,review_pins=pins))["id"]
            await action("item","kept",0,"save",scope="xtrek",note="Internal")
            async with self.sessions() as db:
                self.assertEqual((await review.queue(db,run,"queued",0,20))["total"],0)
            await action("item","kept",1,"save",instruction="Check that exact leaf")
            with self.assertRaisesRegex(ReviewError,"review_changed"):
                await action("item","kept",1,"done")
            with self.assertRaisesRegex(ReviewError,"active_child_work"):
                await action("family","v:F",0,"done")
            await action("item","kept",2,"claim")
            with self.assertRaisesRegex(ReviewError,"result_required"):
                await action("item","kept",3,"complete")
            await action("item","kept",3,"complete",result="Verified test leaf 2/3")
            await action("family","v:F",0,"done",scope="xtrek")
            async with self.sessions() as db:
                self.assertEqual((await store.page(db,None,None,None,None,"",0,20,"open"))["total"],0)
                self.assertEqual((await store.page(db,None,None,None,None,"",0,20,"done"))["total"],1)
                extra=(await review.extras(db,run,"","undecided","all",0,20))["items"][0]
                self.assertEqual((extra["key"],extra["check"]),("2/4","eligible"))
            await action("extra","2/4",0,"save",decision="yes")
            with self.assertRaisesRegex(ReviewError,"exact_variant_required"):
                await action("family","v:F",1,"save",decision="yes")
            remote[1]["stock"]=1
            async with self.sessions() as db:
                run=(await store.refresh(db,None,run))["id"]
            async with self.sessions() as db:
                extra=(await review.extras(db,run,"other","","all",0,20))["items"][0]
                self.assertEqual((extra["work"]["decision"],extra["work"]["status"]),("undecided","blocked"))
                group=(await store.page(db,None,None,None,None,"",0,20,"done"))["groups"][0]
                self.assertEqual(group["work"]["scope"],"xtrek")
                self.assertEqual(group["items"][0]["work"]["result"],"Verified test leaf 2/3")
            await action("family","v:F",1,"reopen")
            await action("item","kept",4,"reopen")
            async with self.sessions() as db:
                self.assertEqual((await store.page(db,None,None,None,None,"",0,20,"open"))["total"],1)
                self.assertEqual((await review.queue(db,run,"blocked",0,20))["total"],1)
                self.assertEqual(await db.scalar(text("SELECT count(*) FROM atomer_review_events WHERE kind='extra'")),2)

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

    async def test_shared_sale_factors_and_empty_sale_barcodes_are_preserved_in_export(self):
        async with self.sessions() as db:
            await db.execute(text("""INSERT INTO shop_products
                (shop_id,product_id,external_product_id,external_variant_id,external_code,parent_code,
                 is_variant,shared_stock,sale_to_stock_factor,sale_barcodes)
                SELECT s.id,p.id,'800',CAST(100+f.factor AS text),'PACK-'||f.factor,'PATCHES',
                       true,true,f.factor,'[]'::jsonb
                FROM shops s CROSS JOIN products p CROSS JOIN (VALUES (1),(8),(100)) AS f(factor)
                WHERE s.code='biketrek' AND p.sku='00042-X'"""))
            await db.commit()
        async with self.sessions() as db:
            output = await service.build_export(db, [], {'source': 'local_only'}, 'biketrek')
            with output, zipfile.ZipFile(output) as archive:
                links = [json.loads(line) for line in archive.read('shop_links.jsonl').splitlines()]
                self.assertEqual({row['sale_to_stock_factor'] for row in links}, {'1.000', '8.000', '100.000'})
                self.assertTrue(all(row['shared_stock'] is True and row['sale_barcodes'] == [] for row in links))
                self.assertEqual(len({row['product_id'] for row in links}), 1)
                self.assertEqual(json.loads(archive.read('manifest.json'))['counts']['tracking'], 0)

    async def test_local_only_snapshot_distinguishes_omitted_catalog_from_an_empty_shop(self):
        metadata = {"source": "local_only", "catalog_included": False}
        async with self.sessions() as db:
            output = await service.build_export(db, [], metadata, "xtrek")
            with output, zipfile.ZipFile(output) as archive:
                manifest = json.loads(archive.read("manifest.json"))
                self.assertEqual(manifest["remote"], metadata)
                self.assertEqual(manifest["counts"]["products"], 1)
                self.assertEqual(manifest["counts"]["upgates_products"], 0)
                self.assertEqual(archive.read("upgates_products.jsonl"), b"")
                self.assertIn("Remote catalog was not read or included.", manifest["notes"])
