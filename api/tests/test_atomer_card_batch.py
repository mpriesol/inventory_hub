import json
import os
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from current_schema_fixture import apply_current_core_schema
from inventory_hub.db_models import IdentifierType, Product, ProductIdentifier
from inventory_hub.db_models_ext import ShopProduct
from inventory_hub.services import atomer_card_batch as batch


GTIN = '5901234123457'
OTHER = '4006381333931'


def parent(code,pid,variants):
    return dict(code=code,product_id=pid,variants_exists_yn=bool(variants),set_yn=False,
                descriptions=[dict(language='sk',title=code)],manufacturer='Example',
                variants=[dict(code=c,variant_id=v,product_id=pid,product_code=code,ean=e,parameters=[])
                          for c,v,e in variants])


def item(code='AT-CODE',ean=GTIN):
    return dict(code=code,name=code,ean=ean,parent='AT-PARENT',family_key='v:AT-PARENT',kind='variant',
                state='needs_both',reasons=[],hub_id=None,hub_sku=None,product_id=None,variant_id=None,
                target_code=None,target_parent=None,target_ean=None)


class CardIdentityTest(unittest.TestCase):
    def test_cross_shop_alias_with_same_gtin_in_different_family(self):
        target=parent('OTHER-FAMILY',900,[('BIKE-123',81,GTIN)])
        catalog=batch.Catalog.build(1,[target])
        self.assertEqual(batch.choose_bike(item(),catalog),('900','81'))

    def test_contradictory_code_and_barcode_and_duplicate_gtin_are_held(self):
        for first in [OTHER,GTIN]:
            target=parent('POSITIONS',900,[('AT-CODE',81,first),('BIKE-ALT',82,GTIN)])
            with self.assertRaisesRegex(batch.CardConflict,'multiple_native_candidates'):
                batch.choose_bike(item(),batch.Catalog.build(1,[target]))

    def test_missing_ean_cannot_change_parent_or_sellable_kind_by_sku_alone(self):
        self.assertIsNone(batch.choose_bike(item(ean=''),batch.Catalog.build(1,[])))
        target=parent('TILL',900,[('AT-CODE',81,'')])
        with self.assertRaisesRegex(batch.CardConflict,'exact_identity_required'):
            batch.choose_bike(item(ean=''),batch.Catalog.build(1,[target]))

    def test_both_and_default_never_lift_operator_single_shop_exception(self):
        for req in ['both','default',None]:
            self.assertEqual(batch.effective_scope({'scope':'default'},{'scope':'xtrek'},req),'xtrek')
        with self.assertRaisesRegex(batch.CardConflict,'scope_changed'):
            batch.effective_scope({'scope':'default'},{'scope':'xtrek'},'biketrek')
        with self.assertRaisesRegex(batch.CardConflict,'closed_or_blocked'):
            batch.effective_scope({'status':'done'},{'status':'open'},'both')

    def test_same_native_id_changed_kind_or_code_is_not_a_target(self):
        family=parent('AT-PARENT',81,[('CHANGED',82,GTIN)])
        with self.assertRaisesRegex(batch.CardConflict,'code_or_kind_changed'):
            batch.choose_x(item(),batch.Catalog.build(2,[family]),(81,82))

    def test_batch_rejects_both_claimants_of_one_card(self):
        plans={'ONE':{'card':42,'keys':{'xtrek':('1',None)}},'TWO':{'card':42,'keys':{'xtrek':('2',None)}}}
        results={}
        batch._reject_collisions(plans,results)
        self.assertEqual(plans,{})
        self.assertEqual({v['status'] for v in results.values()},{'blocked'})

    def test_placeholder_is_sku_only_and_original_is_recorded(self):
        result=batch.barcode_subject(item('A-123','A-123'))
        self.assertEqual(result['ean'],'')
        self.assertEqual(result['barcode_evidence'],{'before':'A-123','type':'source_repeats_sku'})
        with self.assertRaisesRegex(batch.CardConflict,'source_ean_unverified'):
            batch.barcode_subject(item('A-123','bad'))

    def test_independent_barcode_proof_is_pinned_to_source_and_parent(self):
        proof=dict(before='missing_check_digit',parent='AT-PARENT',ean=OTHER,
                   manufacturer_or_supplier='https://example.org/workbook',
                   evidence='Exact manufacturer model and size, published GTIN.')
        self.assertEqual(batch.barcode_subject(item(ean=proof['before']),proof)['ean'],OTHER)
        for src in [item(ean='other-invalid'),item(ean=GTIN),{**item(ean=proof['before']),'parent':'CHANGED'}]:
            with self.assertRaisesRegex(batch.CardConflict,'evidence_changed'):
                batch.barcode_subject(src,proof)
        with self.assertRaisesRegex(batch.CardConflict,'evidence_changed'):
            batch.barcode_subject(item(ean=''),{**proof,'before':''})

    def test_existing_hub_gtin_finds_bike_when_source_has_only_a_different_sku(self):
        target=parent('LEGACY',7,[('OLD-123',8,GTIN)])
        catalog=batch.Catalog.build(1,[target])
        self.assertEqual(batch.choose_bike(item(ean=''),catalog,card_eans=(GTIN,)),('7','8'))


TEST_URL=os.environ.get('CATALOG_TEST_DATABASE_URL','')


@unittest.skipUnless(TEST_URL,'isolated localhost *_catalog_test database required')
class AtomerCardDatabaseTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        url=urlsplit(TEST_URL)
        if url.hostname not in ('localhost','127.0.0.1') or not url.path.endswith('_catalog_test'):
            raise RuntimeError('Refusing a non-test database')
        self.schema='atomer_cards_'+uuid4().hex
        root=Path(__file__).resolve().parents[2]/'infra'/'db-init'
        connection=await asyncpg.connect(TEST_URL)
        try:
            await connection.execute(f'CREATE SCHEMA "{self.schema}"')
            await connection.execute(f'SET search_path TO "{self.schema}"')
            for name in ('001_schema.sql','004_shop_product_content.sql','007_order_stock.sql','011_fifo.sql',
                         '012_product_editor.sql','014_supplier_availability.sql','019_stock_tracking.sql',
                         '037_atomer_progress.sql','038_atomer_review.sql'):
                await connection.execute((root/name).read_text())
            await apply_current_core_schema(connection)
            await connection.execute("INSERT INTO atomer_progress_sources(sha256,source_rows,identities) VALUES(repeat('a',64),1,'[]')")
            await connection.execute("INSERT INTO atomer_progress_runs(source_sha256,checked_at,totals) VALUES(repeat('a',64),now(),'{}')")
            for code,ean in [('AT-CODE',GTIN),('NEW-CODE',OTHER)]:
                await connection.execute("""
                    INSERT INTO atomer_progress_items(run_id,code,kind,state,family_key,detail)
                    VALUES(1,$1,'variant','needs_both','v:AT-PARENT',$2::jsonb)
                """,code,json.dumps(item(code,ean)))
            await connection.execute("""INSERT INTO atomer_review_work(kind,key,revision,status,scope,decision,note,instruction,result,updated_by)
                VALUES ('family','v:AT-PARENT',2,'working','default','undecided','','create xTrek family','','operator')""")
        finally:
            await connection.close()
        self.engine=create_async_engine(TEST_URL.replace('postgresql://','postgresql+asyncpg://',1),poolclass=NullPool,
                                connect_args={'server_settings':{'search_path':self.schema}})
        self.sessions=async_sessionmaker(self.engine,expire_on_commit=False,autoflush=False)

    async def asyncTearDown(self):
        await self.engine.dispose()
        connection=await asyncpg.connect(TEST_URL)
        try:
            await connection.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        finally:
            await connection.close()

    async def test_preserves_existing_card_and_links_new_leaf_in_both_shops_once(self):
        catalogs={'xtrek':[parent('AT-PARENT',71,[('AT-CODE',11,GTIN),('NEW-CODE',12,OTHER)])],
                  'biketrek':[parent('BIKE-PARENT',72,[('LEGACY-BIKE',21,GTIN),('BIKE-NEW',22,OTHER)])]}
        def selected(s,ids):
            return {str(r['product_id']):deepcopy(r) for r in catalogs[s] if str(r['product_id']) in ids}
        async with self.sessions() as db:
            card=Product(sku='LEGACY-BIKE',name='Original label',brand='Example')
            db.add(card);await db.flush()
            db.add(ProductIdentifier(product_id=card.id,identifier_type=IdentifierType.ean,value=GTIN,is_primary=True))
            shops=dict((await db.execute(text('SELECT code,id FROM shops'))).all())
            db.add(ShopProduct(shop_id=shops['biketrek'],product_id=card.id,external_code='LEGACY-BIKE',
                               external_product_id='72',external_variant_id='21',parent_code='BIKE-PARENT',is_variant=True))
            await db.commit()
            with patch.object(batch,'_selected',side_effect=selected):
                args=dict(actor='operator',catalogs=catalogs,include_managed=True,
                          native_ids={'AT-CODE':(71,11),'NEW-CODE':(71,12)})
                first=await batch.reconcile(db,1,['AT-CODE','NEW-CODE'],apply=True,**args)
                await db.commit()
                again=await batch.reconcile(db,1,['AT-CODE','NEW-CODE'],apply=True,**args)
                await db.commit()
            self.assertEqual({c['status'] for c in first.values()},{'verified'})
            self.assertEqual(first,again)
            self.assertEqual(first['AT-CODE']['card_id'],card.id)
            self.assertEqual(first['AT-CODE']['hub_sku'],'LEGACY-BIKE')
            self.assertTrue(all(first[key][s]['link_id'] for key in first for s in batch.SHOPS))
            self.assertEqual(first['NEW-CODE']['biketrek']['variant_id'],'22')
            self.assertEqual(await db.scalar(text('SELECT count(*) FROM products')),2)
            self.assertEqual(await db.scalar(text('SELECT count(*) FROM stock_movements')),0)
            self.assertEqual(await db.scalar(text('SELECT count(*) FROM stock_balances')),0)
            self.assertEqual(await db.scalar(select(Product.name).where(Product.id==card.id)),'Original label')
