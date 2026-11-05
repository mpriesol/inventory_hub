"""A keep decision is atomic, auditable and scoped to the selected native identities."""
import os
import unittest
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from inventory_hub.services.atomer_review import KeepSelection, extras, keep_selection
from inventory_hub.services.atomer_review_targets import ReviewError

TEST_URL = os.environ.get("CATALOG_TEST_DATABASE_URL", "")


@unittest.skipUnless(TEST_URL, "Requires a dedicated localhost *_catalog_test database")
class AtomerKeepSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_atomic_keep_preserves_notes_scopes_and_replays_without_new_events(self):
        parsed = urlsplit(TEST_URL)
        if parsed.hostname not in ("localhost", "127.0.0.1") or not parsed.path.endswith("_catalog_test"):
            self.fail("A dedicated localhost *_catalog_test database is required")
        schema = "atomer_keep_" + uuid4().hex
        conn = await asyncpg.connect(TEST_URL)
        engine = create_async_engine(TEST_URL.replace("postgresql://", "postgresql+asyncpg://", 1),
                                     connect_args={"server_settings": {"search_path": schema}}, poolclass=NullPool)
        try:
            await conn.execute(f'CREATE SCHEMA "{schema}"')
            await conn.execute(f'SET search_path TO "{schema}"')
            root = Path(__file__).resolve().parents[2] / "infra" / "db-init"
            for sql in ("037_atomer_progress.sql", "038_atomer_review.sql"):
                await conn.execute((root / sql).read_text())
            await conn.execute("""
                INSERT INTO atomer_progress_sources(sha256,source_rows,identities) VALUES(repeat('a',64),1,'[]');
                INSERT INTO atomer_progress_runs(source_sha256,checked_at,totals) VALUES(repeat('a',64),now(),'{}');
                INSERT INTO atomer_review_targets(key,identity) VALUES('1/2','{}'),('1/3','{}'),('1/4','{}');
                INSERT INTO atomer_review_checks(run_id,key,family_key,detail)
                    SELECT 1,key,'v:P','{"signature":"test"}' FROM atomer_review_targets;
                INSERT INTO atomer_review_work(kind,key,revision,status,scope,decision,note,instruction,result,updated_by)
                    VALUES ('extra','1/3',2,'open','xtrek','undecided','keep this note','','','operator'),
                           ('extra','1/4',1,'working','both','undecided','','separate task','','operator');
            """)
            sessions = async_sessionmaker(engine)
            async with sessions() as db:
                page = await extras(db,1,"","undecided","all",0,25)
                self.assertEqual(page["keep_selection"], {"1/2":0, "1/3":2})
                for selection, error in [({"1/2":0,"1/3":1}, "review_changed"),
                                         ({"1/2":0,"1/4":1}, "review_changed"),
                                         ({"1/2":0,"1/9":0}, "subject_missing")]:
                    with self.assertRaisesRegex(ReviewError,error):
                        await keep_selection(db,KeepSelection(run_id=1,revisions=selection),"actor")
                    await db.rollback()
                self.assertEqual(await conn.fetchval("SELECT count(*) FROM atomer_review_events"),0)
                request = KeepSelection(run_id=1,revisions=page["keep_selection"])
                self.assertEqual(await keep_selection(db,request,"actor"), {"kept":2,"changed":2})
                self.assertEqual(await keep_selection(db,request,"actor"), {"kept":2,"changed":0})
                counts = await db.scalar(text("SELECT count(*) FROM atomer_review_events WHERE command='keep' AND actor='actor' AND detail->>'decision'='no'"))
                self.assertEqual(counts,2)
                kept = await conn.fetchrow("SELECT status,revision,note,scope FROM atomer_review_work WHERE key='1/3'")
                self.assertEqual(tuple(kept), ("done",3,"keep this note","xtrek"))
                self.assertEqual(await conn.fetchval("SELECT status FROM atomer_review_work WHERE key='1/4'"), "working")
        finally:
            await engine.dispose()
            await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            await conn.close()
