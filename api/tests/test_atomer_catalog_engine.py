import json
import os
from datetime import datetime, timezone
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
from inventory_hub.services import atomer_catalog_fidelity as fidelity, atomer_catalog_recheck as recheck
from inventory_hub.services import atomer_catalog_source_cards as local
from inventory_hub.services import atomer_catalog_meter as meter
from inventory_hub.services.stock_publication_source import SourceError

TEST_URL=os.environ.get('CATALOG_TEST_DATABASE_URL','')


def selection(**changes):
    return engine.Selection(**{'run_id':1,'source_sha':'a'*64,'admin_sha':'b'*64,
                               'scope':'families','families':['v:P'],**changes})


class EngineSelectionTests(unittest.TestCase):
    def test_request_accounting_never_retains_hosts_ids_and_ignores_unrelated_work(self):
        from datetime import timedelta
        c=dict(phase='prepared')
        with patch.object(meter,'targets',return_value={'test':'xtrek'}):
            with meter.measure(c) as events:
                meter.record('test','get','https://example.invalid/api/v2/availabilities/981?secret=test','sent')
                meter.record('test','post','https://example.invalid/api/v2/products','deferred')
            meter.record('test','get','https://private.invalid/api/v2/products','sent')
        self.assertEqual(events,{('prepared','xtrek','availabilities/by_id','GET','sent'):1,
                                 ('prepared','xtrek','products','POST','deferred'):1})
        start=datetime(2026,11,5,19,15,tzinfo=timezone.utc)
        self.assertEqual(meter.blocked_until(dict(hour_remaining=0,day_remaining=0,observed_hour=start.replace(minute=0)),start),
                         start.replace(minute=0)+timedelta(hours=1,seconds=1))
        self.assertIsNone(meter.blocked_until(dict(hour_remaining=0,day_remaining=4,observed_hour=start.replace(minute=0)),start))
    def test_local_card_evidence_preserves_raw_ean_and_isolates_a_bad_sibling(self):
        from types import SimpleNamespace
        data={k:[('Interné číslo',n),('Viditeľný','1'),('Názov tovaru',k),('EAN kód',ean),('Váha','0,25')]
              for k,n,ean in [('P','20',''),('a','21','001'),('bad','22','002')]}
        index=SimpleNamespace(canonical=lambda k:k,family=lambda _: (data,{}),leaves=lambda _:('a','bad'),
                 parent_for=lambda _:'P',sku_owners=lambda k:(k,),gtin_owners=lambda k:('a',) if k=='001' else ())
        rows={k:{'detail':{'ean':v}} for k,v in [('a','001'),('bad','invalidated')]}
        inputs,errors=local.source_details(index,'v:P',rows,rows,{})
        self.assertEqual(list(inputs),['a'])
        self.assertEqual(inputs['a']['source_card']['weight_g'],250)
        self.assertEqual(inputs['a']['source_card']['native_id'],'21')
        self.assertEqual(inputs['a']['source_card']['gtin_owners'],['a'])
        self.assertIn('bad',errors)
    def test_apply_wakes_fixed_failure_without_bypassing_an_upstream_deadline(self):
        waiting=dict(state='waiting',problem='stock_publication_source_unavailable',failures=4)
        self.assertTrue(engine._wake(waiting,False))
        self.assertTrue(engine._wake({**waiting,'state':'paused'},False))
        self.assertTrue(engine._wake({**waiting,'problem':'registry','failures':0},True))
        self.assertFalse(engine._wake({**waiting,'problem':'Limit Upgates. stock_publication_rate_limited'},True))
        self.assertFalse(engine._wake({**waiting,'problem':'Čaká sa na spätnú kontrolu.','failures':0},False))
    def test_recovery_pins_run_family_and_current_operator_revisions(self):
        j=dict(campaign='c',family_key='v:P',stage='blocked',updated_at=datetime.now(timezone.utc))
        group=dict(revision=0); rows={'a':dict(revision=3,status='blocked')}
        rev=recheck.revision(j,7,rows,group)
        self.assertNotEqual(rev,recheck.revision(j,8,rows,group))
        self.assertNotEqual(rev,recheck.revision(j,7,{'a':dict(revision=4,status='done')},group))
        self.assertEqual(selection(requeue={'v:P':rev}).families,['v:P'])
        with self.assertRaises(ValidationError):
            selection(scope='catalog',requeue={'v:P':rev})
        with self.assertRaises(ValidationError):
            selection(requeue={'v:another':rev})
        for receipt in ({},{'dispatched':True}):
            self.assertEqual(recheck.next_phase(dict(document={'payload':{}},receipt=receipt)),'observe')
        self.assertEqual(recheck.next_phase(dict(document={'payload':{}},receipt={'dispatched':False})),'pending')
        self.assertEqual(recheck.next_phase(dict(document={'payload':{}},receipt={'content_intent':{'pid':42}})),'updating')

    def test_material_verification_anchors_aliases_and_reserves_bike_only_siblings(self):
        g=dict(rows={'a':[],'b':[]},excluded={},blocked={},scope={'a':'default','b':'biketrek'})
        j=dict(guards=g,receipt=dict(cards={'a':dict(evidence=dict(status='verified',card_id=12,
                xtrek=dict(product_id=42,variant_id=43,code='existing-a')))}))
        rows={'a':dict(detail=dict(kind='variant',target_parent='published-P',target_code='existing-a',
                                   product_id=42,variant_id=43)),'b':dict(detail={})}
        codes,ids=fidelity.targets(j,rows)
        self.assertEqual(codes,{'a'})
        self.assertEqual(ids,dict(product_id=42,product_code='published-P',variant_ids={'a':43},
                                 variant_codes={'a':'existing-a'},allow_parent_write=False))
        rows['a']['detail']['variant_id']=99
        with self.assertRaises(engine.ReviewError):
            fidelity.targets(j,rows)
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
        corrected=dict(status='open',revision=4,command='barcode_owner_verified')
        self.assertIsNone(engine._blocked_work(corrected,'item','A',{}))
        self.assertIsNotNone(engine._blocked_work(corrected,'family','v:A',{}))
        self.assertIsNotNone(engine._blocked_work({**corrected,'command':'reopen'},'item','A',{}))


class CorrectedSiblingTests(unittest.IsolatedAsyncioTestCase):
    async def test_validated_missing_sibling_is_only_appended_to_the_observed_pid(self):
        fresh=dict(payload=dict(code='P',variants=[{'code':'a'},{'code':'b'}]),deferred_leaves={})
        prior=dict(payload=dict(code='P',variants=[{'code':'a'}]),deferred_leaves={'b':{'reason':'unverified'}})
        j=dict(family_key='v:P',document=prior,receipt={'catalog_deferred':prior['deferred_leaves']},guards={})
        evidence=dict(identity_verified=True,product_id=41,native=dict(product_id=41,code='P',variants=[
                                  {'code':'a','variant_id':99}]))
        c=dict(id='c',selection={},checked_run_id=7)
        with patch.object(engine,'_source',new=AsyncMock()),patch.object(engine,'_options',new=AsyncMock()),\
             patch.object(engine,'_root_index',new=AsyncMock()),patch.object(engine,'_family',new=AsyncMock(return_value=({},{}))),\
             patch.object(engine,'_make_document',return_value=fresh),patch.object(engine,'_job',new=AsyncMock()) as write:
            self.assertTrue(await engine.expand_rechecked(None,c,j,evidence))
            doc=write.await_args.kwargs['document']
            self.assertEqual(write.await_args.args[-1],'prepared')
            self.assertEqual(doc['extension'],dict(product_id=41,existing={'a':dict(product_id=41,variant_id=99)}))
            self.assertEqual(write.await_args.kwargs['receipt']['catalog_deferred'],{})
            with self.assertRaisesRegex(engine.ReviewError,'catalog_bound_parent_changed'):
                await engine.expand_rechecked(None,c,j,{**evidence,'product_id':202})


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
        for name in ('024_upgates_api_budget.sql','037_atomer_progress.sql','038_atomer_review.sql','040_atomer_catalog_campaign.sql',
                     '041_atomer_catalog_content.sql','043_atomer_source_cards.sql','045_atomer_catalog_usage.sql'):
            await self.conn.execute((root/name).read_text())
        await self.conn.execute("""
            CREATE TABLE products(id bigint PRIMARY KEY);
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

    async def test_recovery_observes_an_uncertain_original_intent_and_respects_review_edits(self):
        async with self.factory() as db:
            await engine.process(db,selection(),'apply','operator')
            c=dict(await engine._one(db,'SELECT * FROM atomer_catalog_campaigns'))
            rows,group=await engine._family(db,1,'v:P')
            job=dict(await engine._one(db,"SELECT * FROM atomer_catalog_jobs WHERE family_key='v:P'"))
            job['guards']=await engine._claim(db,c,job,rows,group)
            await engine._job(db,c['id'],'v:P','sending',guards=job['guards'],document=dict(payload=dict(code='P')),
                              receipt=dict(dispatched=True))
            await engine._finish(db,c,job,{k:('blocked',dict(reason='Neistá odpoveď')) for k in rows})
            await db.commit()
            pin=(await engine.details(db,'v:P'))['recheck_revision']
            await db.execute(text("""INSERT INTO atomer_progress_runs(source_sha256,admin_export_sha256,checked_at,totals)
                    VALUES(repeat('a',64),repeat('b',64),now(),'{}');
                    """))
            await db.execute(text("""INSERT INTO atomer_progress_items(run_id,code,kind,state,family_key,detail)
                    SELECT 2,code,kind,state,family_key,detail FROM atomer_progress_items WHERE run_id=1"""))
            await db.commit()
            with self.assertRaisesRegex(engine.ReviewError,'catalog_recheck_changed'):
                await engine.process(db,selection(run_id=2,requeue={'v:P':pin}),'apply','operator')
            await db.rollback()
            newer=(await engine.details(db,'v:P'))['recheck_revision']
            await engine.process(db,selection(run_id=2,requeue={'v:P':newer}),'preview','operator')
            await engine.process(db,selection(run_id=2,requeue={'v:P':newer}),'apply','operator')
            got=await engine._one(db,"SELECT stage,receipt FROM atomer_catalog_jobs WHERE family_key='v:P'")
            self.assertEqual(got['stage'],'observe')
            self.assertEqual(got['receipt']['last_recheck']['run_id'],2)
            self.assertEqual(await db.scalar(text("SELECT count(*) FROM atomer_review_events WHERE command='catalog_recheck'")),2)

    async def test_source_card_receipt_is_partial_and_idempotent_before_native_read(self):
        async with self.factory() as db:
            await engine.process(db,selection(),'apply','operator')
            c=dict(await engine._one(db,'SELECT * FROM atomer_catalog_campaigns'))
            await db.execute(text('INSERT INTO products VALUES(142)'))
            initial=dict(status='card_ready',action='created',created=True,card_id=142,hub_sku='a')
            await local._receipts(db,c,'v:P',{'a':initial})
            await local._receipts(db,c,'v:P',{'a':{**initial,'action':'reused','created':False}})
            await db.commit()
            totals=(await engine.status(db))['counts']
            self.assertEqual((totals['hub_cards'],totals['cards_created'],totals['cards_reused']),(1,1,0))
            self.assertEqual((totals['verified'],totals['source_card_ready'],totals['partial']),(0,1,1))
            self.assertEqual((await engine.details(db,'v:P'))['source_cards'][0]['card_id'],142)
            await local._receipts(db,c,'v:P',{'a':dict(status='blocked',reason='owner_recheck')})
            held=(await engine.details(db,'v:P'))['source_cards'][0]
            self.assertEqual((held['card_id'],held['state']),(142,'blocked'))
            self.assertEqual(held['evidence']['creation_evidence'],initial)

    async def test_updated_local_evidence_only_retries_the_exact_unresolved_card(self):
        async with self.factory() as db:
            await engine.process(db,selection(scope='catalog'),'apply','operator')
            c=dict(await engine._one(db,'SELECT * FROM atomer_catalog_campaigns'))
            await local._receipts(db,c,'v:P',{'b':dict(status='blocked',reason='atomer_source_ean_unverified')})
            await db.execute(text("UPDATE atomer_catalog_jobs SET source_card_stage='blocked'"))
            old = await local.blocked_reasons(db,c)
            self.assertEqual((old[0]['leaves'], old[0]['reason']), (1,'atomer_source_ean_unverified'))
            config = dict(barcode_corrections={'b':{'ean':'123'}},zero_barcode_sentinels={})
            await local.retry_new_evidence(db,c['id'],config,{'b'})
            stages = dict((await db.execute(text("SELECT family_key,source_card_stage FROM atomer_catalog_jobs"))).all())
            self.assertEqual(stages,{'v:P':'pending','s:closed':'blocked'})
            await local._receipts(db,c,'v:P',{'b':dict(status='blocked',reason='atomer_other_owner',
                        source_card_input=local.input_token(config,'b'))})
            await db.execute(text("UPDATE atomer_catalog_jobs SET source_card_stage='blocked'"))
            await local.retry_new_evidence(db,c['id'],config,{'b'})
            self.assertEqual(await db.scalar(text("SELECT count(*) FROM atomer_catalog_jobs WHERE source_card_stage='pending'")),0)
            self.assertEqual(await db.scalar(text("SELECT count(*) FROM atomer_catalog_source_cards")),1)

    async def test_waiting_campaign_source_cards_release_the_native_lane(self):
        from types import SimpleNamespace
        from inventory_hub.services import atomer_card_batch as cards
        data={k:[('Interné číslo',str(n)),('Viditeľný','1'),('Názov tovaru',k)]
              for n,k in enumerate(('P','a','b'),10)}
        index=SimpleNamespace(canonical=lambda k:k,family=lambda _: (data,{}),leaves=lambda _:('a','b'),
                   parent_for=lambda _:'P',sku_owners=lambda k:(k,),gtin_owners=lambda k:())
        async with self.factory() as db:
            await engine.process(db,selection(),'apply','operator')
            c=dict(await engine._one(db,'SELECT * FROM atomer_catalog_campaigns'))
            async def write_cards(session,run,details,**kwargs):
                self.assertEqual(sorted(details),['a','b'])
                self.assertEqual(await session.scalar(text("SELECT count(*) FROM atomer_review_work WHERE status='working'")),2)
                await session.execute(text('INSERT INTO products VALUES(142),(143)'))
                return {k:dict(status='card_ready',action='created',card_id=pid,hub_sku=k)
                        for k,pid in (('a',142),('b',143))}
            with patch.object(engine,'_prepare',new=AsyncMock()) as native:
                self.assertFalse(await engine._step(db,c))
                native.assert_not_awaited()
                self.assertEqual(await db.scalar(text('SELECT state FROM atomer_catalog_campaigns')),'waiting')
                with patch.object(engine,'_source',new=AsyncMock(return_value=index)),patch.object(cards,'source_cards',side_effect=write_cards):
                    await local.prepare(db,c,local.choose(await local.due(db,c)))
                self.assertEqual(await db.scalar(text("SELECT source_card_stage FROM atomer_catalog_jobs")),'checked')
                self.assertTrue(await engine._step(db,c))
                native.assert_awaited_once()
            counts=(await engine.status(db))['counts']
            self.assertEqual((counts['cards_created'],counts['verified'],counts['partial']),(2,0,2))

    async def test_unpublished_sibling_never_gets_a_parent_card_or_a_completed_receipt(self):
        from inventory_hub.services import atomer_card_batch as cards
        async with self.factory() as db:
            await engine.process(db,selection(partial_barcodes=True),'apply','operator')
            c=dict(await engine._one(db,'SELECT * FROM atomer_catalog_campaigns'))
            j=dict(await engine._one(db,"SELECT * FROM atomer_catalog_jobs WHERE family_key='v:P'"))
            rows,g=await engine._family(db,1,'v:P')
            j['guards']=await engine._claim(db,c,j,rows,g)
            j['document']=dict(payload=dict(code='P',variants=[{'code':'a'}]),deferred_leaves={
                'b':dict(reason='new_leaf_ean_unverified',source_ean='wrong')})
            j['receipt']=dict(identity_verified=True,verified_fields=True,product_id=9,variants={'a':19})
            await engine._job(db,c['id'],'v:P','cards',guards=j['guards'],document=j['document'],receipt=j['receipt'])
            with patch.object(cards,'reconcile',new=AsyncMock(return_value={'a':dict(status='verified',card_id=8,
                                  xtrek=dict(product_id=9,variant_id=19),biketrek=None)})) as reconciled:
                await engine._cards(db,c,[j])
                self.assertEqual(reconciled.await_args.args[2],['a'])
                self.assertEqual(reconciled.await_args.kwargs['native_ids'],{'a':(9,19)})
            await db.commit()
            self.assertEqual(await db.scalar(text("SELECT stage FROM atomer_catalog_jobs WHERE family_key='v:P'")),'partial')
            proof=await engine._one(db,"SELECT state,evidence FROM atomer_catalog_proofs WHERE code='b'")
            self.assertEqual((proof['state'],proof['evidence']['reason']),('blocked','new_leaf_ean_unverified'))
            self.assertIsNone(proof['evidence'].get('card_id'))
