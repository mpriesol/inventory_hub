import json
import os
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from inventory_hub.services import atomer_catalog_engine as engine
from inventory_hub.services.stock_publication_source import SourceError

TEST_URL=os.environ.get('CATALOG_TEST_DATABASE_URL','')


def selection(**changes):
    return engine.Selection(**{'run_id':1,'source_sha':'a'*64,'admin_sha':'b'*64,
                               'scope':'families','families':['v:P'],**changes})


class EngineSelectionTests(unittest.TestCase):
    def test_large_families_are_isolated_and_boundaries_are_per_leaf(self):
        def job(n,stage='prepared'):
            return dict(leaf_count=n,stage=stage)
        self.assertEqual(len(engine._ready_batch([job(418),job(2)])),1)
        self.assertEqual(len(engine._ready_batch([job(70),job(6),job(20)])),2)
        self.assertEqual(len(engine._ready_batch([job(3),job(3,'pending')])),1)

    def test_no_unspecified_selection_and_no_foreign_claims(self):
        with self.assertRaises(ValidationError):
            selection(families=[])
        with self.assertRaises(ValidationError):
            selection(working={'SKU':2})
        with self.assertRaises(ValidationError):
            selection(families=['P'])
        self.assertEqual(engine._campaign_id(selection()),engine._campaign_id(selection(scope='catalog')))

    def test_foreign_and_withdrawn_tasks_are_reserved(self):
        self.assertIsNotNone(engine._blocked_work(dict(status='working',revision=3),'item','A',{'item:A':2}))
        self.assertIsNotNone(engine._blocked_work(dict(status='open',revision=4),'item','A',{}))
        self.assertIsNone(engine._blocked_work(dict(status='working',revision=3),'item','A',{'item:A':3}))


@unittest.skipUnless(TEST_URL,'Requires dedicated localhost *_catalog_test database')
class CampaignIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        url=urlsplit(TEST_URL)
        self.assertIn(url.hostname,('127.0.0.1','localhost'))
        self.assertTrue(url.path.endswith('_catalog_test'))
        self.schema='campaign_'+uuid4().hex
        self.conn=await asyncpg.connect(TEST_URL)
        await self.conn.execute(f'CREATE SCHEMA "{self.schema}"')
        await self.conn.execute(f'SET search_path TO "{self.schema}"')
        self.pg=create_async_engine(TEST_URL.replace('postgresql://','postgresql+asyncpg://',1),
                    connect_args={'server_settings':{'search_path':self.schema}},poolclass=NullPool)
        root=Path(__file__).resolve().parents[2]/'infra'/'db-init'
        for name in ('037_atomer_progress.sql','038_atomer_review.sql','040_atomer_catalog_campaign.sql'):
            await self.conn.execute((root/name).read_text())
        await self.conn.execute("""
            INSERT INTO atomer_progress_sources(sha256,source_rows,identities) VALUES(repeat('a',64),2,'[]');
            INSERT INTO atomer_review_exports(sha256,format,compressed) VALUES(repeat('b',64),'admin_csv','');
            INSERT INTO atomer_progress_runs(source_sha256,admin_export_sha256,checked_at,totals)
             VALUES(repeat('a',64),repeat('b',64),now(),'{}');
            INSERT INTO atomer_progress_items(run_id,code,kind,state,family_key,detail) VALUES
            (1,'a','variant','needs_both','v:P','{"code":"a","state":"needs_both","card_state":"missing","reasons":[],"bike_state":"no_card"}'),
            (1,'b','variant','needs_both','v:P','{"code":"b","state":"needs_both","card_state":"missing","reasons":[],"bike_state":"no_card"}'),
            (1,'closed','standalone','needs_target','s:closed','{"code":"closed","state":"needs_target","card_state":"exists","reasons":[],"bike_state":"unlinked"}');
            INSERT INTO atomer_review_work(kind,key,revision,status,scope,decision,note,instruction,result,updated_by)
                VALUES ('item','closed',2,'done','biketrek','undecided','keep','','manual decision','operator');
            INSERT INTO atomer_review_events(kind,key,revision,command,actor,detail)
                VALUES ('item','closed',2,'done','operator','{}');
        """)
        self.factory=async_sessionmaker(self.pg,expire_on_commit=False)
        self.patch=patch.object(engine.repair,'target_fingerprint',return_value='x'*64)
        self.patch.start()

    async def asyncTearDown(self):
        self.patch.stop()
        await self.pg.dispose()
        await self.conn.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        await self.conn.close()

    async def test_preview_scope_extension_claim_and_ambiguous_post(self):
        async with self.factory() as db:
            pre=await engine.process(db,selection(),'preview','operator')
            self.assertEqual(pre['count'],2)
            self.assertEqual((await engine.status(db))['state'],'idle')
            await engine.process(db,selection(),'apply','operator')
            await engine.process(db,selection(scope='catalog'),'apply','operator')
            one=await engine.status(db)
            self.assertEqual(one['counts']['total'],3)
            c=dict(await engine._one(db,'SELECT * FROM atomer_catalog_campaigns'))
            await engine._lock_current(db,c)
            j=dict(await engine._one(db,"SELECT * FROM atomer_catalog_jobs WHERE family_key='v:P'"))
            rows,group=await engine._family(db,1,'v:P')
            guards=await engine._claim(db,c,j,rows,group);j['guards']=guards
            doc=dict(payload=dict(code='P',variants=[{'code':'a'},{'code':'b'}]))
            await engine._job(db,c['id'],'v:P','prepared',guards=guards,document=doc)
            await db.commit()
            j.update(document=doc,stage='prepared')
            from inventory_hub.services import atomer_new_native as native
            with patch.object(native,'preflight',return_value={'P':[]}),patch.object(native,'send',
                        side_effect=SourceError('lost_response',uncertain=True,request_sent=True)) as outbound:
                await engine._dispatch(db,c,[j]); await db.commit()
                self.assertEqual(outbound.call_count,1)
                self.assertEqual(await db.scalar(text("SELECT stage FROM atomer_catalog_jobs WHERE family_key='v:P'")),'observe')
            self.assertEqual(await db.scalar(text("SELECT count(*) FROM atomer_review_events WHERE command='claim'")),2)
            closed=dict(await engine._one(db,"SELECT * FROM atomer_catalog_jobs WHERE family_key='s:closed'"))
            await engine._prepare(db,c,[closed]); await db.commit()
            checked=await engine.status(db)
            self.assertEqual(checked['counts']['excluded'],1)
            self.assertEqual(await db.scalar(text("SELECT revision FROM atomer_review_work WHERE key='closed'")),2)
