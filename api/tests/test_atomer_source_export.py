"""Private pinned source recovery, bounded decoding and read-only HTTP access."""
from hashlib import sha256
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit
from uuid import uuid4
import zlib

import asyncpg
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from inventory_hub.database import get_session
from inventory_hub.routers import atomer_progress as routes
from inventory_hub.services import atomer_source_export as service
from inventory_hub.services.atomer_review_targets import ReviewError
from inventory_hub.settings import settings

URL = os.getenv('CATALOG_TEST_DATABASE_URL', '')


class SourceExportRoutes(unittest.TestCase):
    def test_auth_fixed_selector_no_store_and_exact_attachment(self):
        app = FastAPI()
        app.include_router(routes.router)
        db_calls = []
        async def session():
            db_calls.append(True)
            yield SimpleNamespace()
        app.dependency_overrides[get_session] = session
        token = 'synthetic-atomer-export-' + 'x' * 32
        raw = b'<PRODUCTS><PRODUCT/></PRODUCTS>'
        result = {'body': raw, 'sha256': sha256(raw).hexdigest(), 'run_id': 7,
                  'filename': 'atomer-buyers-run-7.xml', 'media_type': 'application/xml'}
        with TestClient(app) as client, patch.object(settings, 'AI_CONTENT_ACCESS_TOKEN', SecretStr(token)), \
                patch.object(service, 'download', AsyncMock(return_value=result)) as download:
            url = '/migration/atomer-progress/source-export/buyers?run_id=7'
            denied = client.get(url)
            self.assertEqual(denied.status_code, 401)
            self.assertEqual(denied.headers['cache-control'], 'no-store')
            self.assertEqual(db_calls, [])
            download.assert_not_awaited()
            headers = {'Authorization': 'Bearer ' + token}
            reply = client.get(url, headers=headers)
            self.assertEqual(reply.content, raw)
            self.assertEqual(reply.headers['cache-control'], 'no-store')
            self.assertEqual(reply.headers['x-content-sha256'], sha256(raw).hexdigest())
            self.assertEqual(reply.headers['x-atomer-run-id'], '7')
            self.assertEqual(reply.headers['x-content-type-options'], 'nosniff')
            self.assertIn('attachment;', reply.headers['content-disposition'])
            self.assertEqual(download.call_args.args[1:], (7, 'buyers'))
            for suffix in ('other?run_id=7', 'buyers', 'buyers?run_id=0', 'buyers?run_id=9223372036854775808'):
                self.assertEqual(client.get('/migration/atomer-progress/source-export/' + suffix,
                                            headers=headers).status_code, 422)
            self.assertEqual(download.await_count, 1)
            download.side_effect = ReviewError('atomer_source_export_invalid', 409)
            rejected = client.get(url, headers=headers)
            self.assertEqual(rejected.status_code, 409)
            self.assertEqual(rejected.json(), {'detail': {'code': 'atomer_source_export_invalid'}})
            self.assertEqual(rejected.headers['cache-control'], 'no-store')


@unittest.skipUnless(URL, 'Set CATALOG_TEST_DATABASE_URL to an isolated localhost *_catalog_test DB')
class SourceExportDatabase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        parsed = urlsplit(URL)
        self.assertIn(parsed.hostname, ('localhost', '127.0.0.1'))
        self.assertTrue(parsed.path.endswith('_catalog_test'))
        self.schema = 'source_export_' + uuid4().hex
        self.conn = await asyncpg.connect(URL)
        await self.conn.execute(f'CREATE SCHEMA "{self.schema}"; SET search_path TO "{self.schema}"')
        root = Path(__file__).resolve().parents[2] / 'infra' / 'db-init'
        for filename in ('037_atomer_progress.sql', '038_atomer_review.sql'):
            await self.conn.execute((root / filename).read_text())
        self.pg = create_async_engine(URL.replace('postgresql://', 'postgresql+asyncpg://', 1), poolclass=NullPool,
            connect_args={'server_settings': {'search_path': self.schema}})
        self.sessions = async_sessionmaker(self.pg, expire_on_commit=False)
        self.xml = b'<PRODUCTS><PRODUCT><PRODUCT_ID>EXACT-1</PRODUCT_ID></PRODUCT></PRODUCTS>'
        self.csv = 'Interne cislo;Katalogove cislo;Pocet kusov\n17;EXACT-1;3\n'.encode()
        self.xml_sha, self.csv_sha = sha256(self.xml).hexdigest(), sha256(self.csv).hexdigest()
        for fmt, raw, digest in (('buyers', self.xml, self.xml_sha), ('admin_csv', self.csv, self.csv_sha)):
            await self.conn.execute('INSERT INTO atomer_review_exports(sha256,format,compressed) VALUES($1,$2,$3)',
                                    digest, fmt, zlib.compress(raw, 3))
        await self.conn.execute("INSERT INTO atomer_progress_sources VALUES($1,1,'[]')", self.xml_sha)
        await self.conn.execute("""INSERT INTO atomer_progress_runs(source_sha256,admin_export_sha256,checked_at,totals)
                                  VALUES($1,$2,now(),'{}')""", self.xml_sha, self.csv_sha)

    async def asyncTearDown(self):
        await self.pg.dispose()
        await self.conn.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        await self.conn.close()

    async def read(self, kind='buyers', run=1):
        async with self.sessions() as db:
            await db.execute(text('SET TRANSACTION READ ONLY'))
            result = await service.download(db, run, kind)
            self.assertFalse(db.new or db.dirty or db.deleted)
            return result

    async def test_exact_original_bytes_are_pinned_to_run_and_fixed_format(self):
        for kind, raw, digest in (('buyers', self.xml, self.xml_sha), ('admin', self.csv, self.csv_sha)):
            actual = await self.read(kind)
            self.assertEqual((actual['body'], actual['sha256'], actual['run_id']), (raw, digest, 1))
        other = b'<PRODUCTS><PRODUCT>Newer</PRODUCT></PRODUCTS>'
        other_sha = sha256(other).hexdigest()
        await self.conn.execute("INSERT INTO atomer_progress_sources VALUES($1,1,'[]')", other_sha)
        await self.conn.execute("INSERT INTO atomer_review_exports(sha256,format,compressed) VALUES($1,'buyers',$2)",
                                other_sha, zlib.compress(other))
        await self.conn.execute("""INSERT INTO atomer_progress_runs(source_sha256,checked_at,totals)
                                  VALUES($1,now(),'{}')""", other_sha)
        self.assertEqual((await self.read(run=1))['body'], self.xml)
        self.assertEqual((await self.read(run=2))['body'], other)
        with self.assertRaises(ReviewError) as missing:
            await self.read('admin', 2)
        self.assertEqual((missing.exception.code, missing.exception.status), ('atomer_source_export_missing', 404))
        with self.assertRaises(ReviewError):
            await self.read(run=99)

    async def test_truncated_trailing_hash_mismatch_and_oversize_never_download(self):
        blob = zlib.compress(self.xml)
        for damaged in (blob[:-2], blob + b'private-trailing-data', zlib.compress(b'changed'), b'bad-zlib'):
            await self.conn.execute('UPDATE atomer_review_exports SET compressed=$1 WHERE sha256=$2', damaged, self.xml_sha)
            with self.assertRaises(ReviewError) as rejected:
                await self.read()
            self.assertEqual(rejected.exception.code, 'atomer_source_export_invalid')
        await self.conn.execute('UPDATE atomer_review_exports SET compressed=$1 WHERE sha256=$2', blob, self.xml_sha)
        with patch.object(service.source, 'MAX_ARCHIVE', 16):
            with self.assertRaises(ReviewError) as rejected:
                await self.read()
            self.assertEqual(rejected.exception.code, 'atomer_source_export_invalid')
            await self.conn.execute('UPDATE atomer_review_exports SET compressed=$1 WHERE sha256=$2', b'x' * 70_000, self.xml_sha)
            with self.assertRaises(ReviewError) as rejected:
                await self.read()
            self.assertEqual(rejected.exception.code, 'atomer_source_export_size')

    async def test_archive_missing_or_wrong_format_is_not_substituted(self):
        await self.conn.execute("UPDATE atomer_review_exports SET format='admin_csv' WHERE sha256=$1", self.xml_sha)
        with self.assertRaises(ReviewError) as wrong_format:
            await self.read()
        self.assertEqual(wrong_format.exception.code, 'atomer_source_export_missing')
        with self.assertRaises(ReviewError) as bad_kind:
            await self.read('arbitrary')
        self.assertEqual((bad_kind.exception.code, bad_kind.exception.status), ('atomer_source_export_kind_invalid', 422))
