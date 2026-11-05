from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import Mock, patch

from inventory_hub.services import atomer_new_native as native
from inventory_hub.services.atomer_generations import parameters
from inventory_hub.services.atomer_review_targets import ReviewError
from inventory_hub.services.catalog import CatalogError


def creation():
    price = dict(language='sk',price_common=15.0,pricelists=[dict(name='Retail',price_original=13.0)])
    desc = dict(language='sk',active_yn=True,title='Batoh',seo_title='Batoh',seo_description='Opis',
                short_description='<p>Krátky</p>',long_description='<p><strong>Opis</strong></p>')
    image=dict(url='https://www.xtrek.sk/user/1/upload/stuff/photo.jpg',main_yn=True,list_yn=True,position=1,
               titles=[dict(language='sk',title='Hlavný')])
    leaf=dict(code='BLUE',ean='4006381333931',active_yn=True,main_yn=True,availability_id=4,
              weight=123,prices=[price],parameters=parameters({'Farba':'Modrá'}),images=[image])
    root=dict(code='BAG',active_yn=True,weight=140,prices=[price],vats={'sk':23},
              manufacturer='Brand',descriptions=[desc],categories=[{'category_id':3,'main_yn':True}],
              images=[image],variants=[leaf],related=['OTHER'])
    return dict(payload=root,related=['OTHER'],flags={'BAG':[],'BLUE':[]},availability={'BLUE':'3 dni'},missing_related=[])


def live(doc=None):
    doc=doc or creation()
    parent=deepcopy(doc['payload'])
    parent.update(product_id=101,archived_yn=False,set_yn=False,variants_exists_yn=True,parameters_new=[],labels=[])
    parent['descriptions'][0].pop('active_yn')
    for row in [parent,*parent['variants']]:
        row['images'][0].update(file_id=33,url='https://cdn.example/aa-photo.jpg')
        row['prices'][0].update(vat=23,currency='EUR')
        row['prices'][0]['pricelists'][0]['price_with_vat']=13.0
    leaf=parent['variants'][0]
    leaf.update(variant_id=202,product_id=101,product_code='BAG',parameters_new=leaf['parameters'],
                stock=0,availability='3 dni',labels=[])
    return parent


class NativeCreationTests(unittest.TestCase):
    def test_observation_requires_native_identity_and_all_mapped_fields(self):
        doc=creation()
        result=native._family_result(doc,live(doc),{'product_id':101,'variant_ids':{'BLUE':202}})
        self.assertEqual(result['status'],'verified')
        self.assertEqual(result['variants'],{'BLUE':202})
        self.assertTrue(result['identity_verified'])
        edit=live(doc)
        edit['descriptions'][0]['long_description']='<p>Opis</p>'
        edit['variants'][0]['prices'][0]['pricelists'][0]['price_with_vat']=12
        edit['variants'][0]['active_yn']=False
        edit['related']=[]
        edit['images']=[]
        observed=native._family_result(doc,edit,None)
        self.assertEqual(observed['status'],'partial')
        self.assertTrue(observed['identity_verified'])
        self.assertFalse(observed['verified_fields'])
        self.assertCountEqual(observed['deferred'],['images','related'])
        for expected in ('long_description','BLUE · cena alebo DPH','BLUE · viditeľnosť'):
            self.assertIn(expected,observed['issues'])

    def test_sibling_ean_or_axis_cannot_be_rebound(self):
        doc=creation()
        for field,wrong in [('ean',''),('parameters_new',parameters({'Farba':'Iná'})),('product_code','X')]:
            with self.subTest(field=field):
                remote=live()
                remote['variants'][0][field]=wrong
                if field=='product_code':
                    with patch.object(native,'_sparse',return_value=[remote]),self.assertRaises(ReviewError):
                        native._read(Mock(),{'BAG':doc},{})
                    continue
                result=native._family_result(doc,remote,None)
                self.assertEqual(result['status'],'conflict')
                self.assertFalse(result['identity_verified'])
        self.assertEqual(native._family_result(doc,live(),{'product_id':99})['status'],'conflict')

    def test_preflight_checks_new_parent_and_variant_code_namespaces_and_other_gtins(self):
        doc=creation()
        changed=dict(code='OTHER',product_id=20,variants_exists_yn=True,variants=[dict(code='ANOTHER',ean='4006381333931',variant_id=21)])
        occupied=dict(code='blue',product_id=22,variants_exists_yn=False,variants=[])
        variant=dict(code='BAG',product_id=30,variant_id=31)
        with patch.object(native.transport,'_client',return_value=Mock()),\
             patch.object(native,'_delta',return_value=[(20,[('ANOTHER',21,list(native._tokens(changed['variants'][0])))])]),\
             patch.object(native,'_sparse',side_effect=[[occupied],[variant],[changed]]):
            issues=native.preflight([doc],'fp',datetime.now(timezone.utc))['BAG']
        self.assertTrue(any('20/21' in x and 'ean' in x for x in issues))
        self.assertTrue(any('22' in x and 'code' in x for x in issues))
        self.assertTrue(any('30/31' in x and 'code' in x for x in issues))

    def test_foreign_rows_from_an_ignored_filter_fail_closed(self):
        with patch.object(native.transport,'_client',return_value=Mock()),\
             patch.object(native,'_delta',return_value=[]),\
             patch.object(native,'_sparse',side_effect=[[dict(code='FOREIGN')],[]]),self.assertRaises(ReviewError):
            native.preflight([creation()],'fp',datetime.now(timezone.utc))
        with patch.object(native,'_sparse',side_effect=[[dict(code='FOREIGN')],[]]),self.assertRaises(ReviewError):
            native._read(Mock(),{'BAG':creation()},{})

    def test_incremental_identity_scan_retains_previous_pages_and_recovers_after_a_failed_scan(self):
        started = datetime.now(timezone.utc) - timedelta(hours=6)
        first = dict(code='FIRST', product_id=1, variants=[], variants_exists_yn=False)
        second = dict(code='SECOND', product_id=2, variants=[], variants_exists_yn=False)

        def incomplete():
            yield second
            raise native.ExportError('partial')

        with patch.object(native, '_delta_observation', None), patch.object(
            native.catalog, 'page_rows', side_effect=[[first], incomplete(), [second]]
        ) as pages:
            self.assertEqual({pid for pid, _ in native._delta(Mock(), 'fp', started)}, {1})
            checkpoint = native._delta_observation[1]
            with self.assertRaises(native.transport.SourceError):
                native._delta(Mock(), 'fp', started)
            self.assertEqual(native._delta_observation[1], checkpoint)
            self.assertEqual(set(native._delta_observation[2]), {1})
            self.assertEqual({pid for pid, _ in native._delta(Mock(), 'fp', started)}, {1, 2})
            self.assertEqual(pages.call_args_list[1].args[2], pages.call_args_list[2].args[2])
            since = datetime.fromisoformat(pages.call_args_list[1].args[2]['last_update_time_from'])
            self.assertGreater(since, started)
            self.assertLess(since, checkpoint)

    def test_source_related_aliases_and_unresolved_fields_are_verified_independently(self):
        doc = creation()
        doc.update(related=['SOURCE-ALIAS', 'NOT-HERE'], missing_related=['NOT-HERE'],
                   missing_categories=['Additional'], managed_label_ids=[20, 21])
        candidate = live(doc)
        candidate['labels'] = [dict(label_id=85, active_currently_yn=True)]
        result = native._family_result(doc, candidate, None)
        self.assertTrue(result['identity_verified'])
        self.assertFalse(result['verified_fields'])
        self.assertEqual(result['status'], 'partial')
        self.assertCountEqual(result['deferred'], ['missing_categories', 'missing_related'])
        doc.update(missing_categories=[], missing_related=[])
        self.assertEqual(native._family_result(doc, candidate, None)['status'], 'verified')

    def test_send_returns_hints_does_not_retry_and_rejects_stock(self):
        document=creation()
        ack=dict(products=[dict(code='BAG',product_id=101,inserted_yn=True,
            variants=[dict(code='BLUE',variant_id=202,inserted_yn=True)])])
        with patch.object(native.transport,'_client',return_value=Mock()),patch.object(native,'_post',return_value=ack) as send:
            result=native.send([document],'fp')
            self.assertEqual(result['BAG'],dict(product_id=101,variant_ids={'BLUE':202},issues=[]))
            self.assertEqual(send.call_count,1)
            document['payload']['variants'][0]['stock']=5
            with self.assertRaises(CatalogError):
                native.send([document],'fp')
            self.assertEqual(send.call_count,1)

    def test_related_backfill_only_appends_live_references_and_excludes_changed_products(self):
        doc=creation()
        remote=live()
        remote['related']=[]
        ids={'BAG':dict(product_id=101,variant_ids={'BLUE':202})}
        with patch.object(native.transport,'_client',return_value=Mock()),\
             patch.object(native,'_read',return_value={'BAG':remote}),\
             patch.object(native,'_sparse',return_value=[{'code':'OTHER'}]),\
             patch.object(native.transport,'_request',return_value={'products':[]}) as send:
            result=native.complete_related([doc],'fp',ids)
            self.assertEqual(result['BAG']['status'],'sent')
            self.assertEqual(send.call_args.kwargs['payload'],{'products':[{'code':'BAG','related':['OTHER']}]})
            remote['related']=['MANUAL']
            self.assertEqual(native.complete_related([doc],'fp',ids)['BAG']['status'],'conflict')
            self.assertEqual(send.call_count,1)


class ExistingFamilyTests(unittest.TestCase):
    def setUp(self):
        self.doc=creation()
        self.old=live(self.doc)
        self.addition=deepcopy(self.doc['payload']['variants'][0])
        self.addition.update(code='BLACK',ean='5901234123457',parameters=parameters({'Farba':'Čierna'}),main_yn=False)
        self.doc['payload']['variants'].append(self.addition)
        self.doc['flags']['BLACK']=[]
        self.doc['availability']['BLACK']='3 dni'
        self.ids={'BAG':{'product_id':101,'variant_ids':{'BLUE':202}}}

    def plan(self):
        with patch.object(native.transport,'_client',return_value=Mock()),\
             patch.object(native,'_read',return_value={'BAG':deepcopy(self.old)}),\
             patch.object(native,'_check_absent',return_value={'BAG':[]}):
            return native.prepare_extensions([self.doc],'fp',datetime.now(timezone.utc),self.ids,{'BAG':['BLACK']})

    def test_extending_keeps_extra_legacy_generation_and_old_leaf(self):
        legacy=deepcopy(self.old['variants'][0])
        legacy.update(code='GREY-v1',variant_id=301,ean='9780306406157',main_yn=False,
                      parameters_new=parameters({'Farba':'Sivá'}))
        self.old['variants'].append(legacy)
        plans=self.plan()
        self.assertEqual(plans['BAG']['status'],'ready')
        self.assertEqual(plans['BAG']['payload'],{'code':'BAG','variants':[self.addition]})
        self.assertFalse(plans['BAG']['payload']['variants'][0]['main_yn'])
        live_now=deepcopy(self.old)
        added=deepcopy(live_now['variants'][0])
        added.update(code='BLACK',ean=self.addition['ean'],variant_id=303,parameters_new=self.addition['parameters'],main_yn=False)
        live_now['variants'].append(added)
        with patch.object(native.transport,'_client',return_value=Mock()),\
             patch.object(native,'_read',return_value={'BAG':live_now}):
            result=native.observe_extensions([self.doc],'fp',plans)['BAG']
            self.assertEqual(result['status'],'verified')
            self.assertEqual(len(result['native']['variants']),3)
            self.assertEqual(result['variants'],{'BLUE':202,'BLACK':303})
            live_now['variants'][1]['stock']=99
            result=native.observe_extensions([self.doc],'fp',plans)['BAG']
            self.assertEqual(result['status'],'conflict')
            self.assertFalse(result['identity_verified'])

    def test_occupied_axes_prevent_sending(self):
        self.doc['payload']['variants'][1]['parameters']=parameters({'Farba':'Modrá'})
        plans=self.plan()
        self.assertEqual(plans['BAG']['status'],'conflict')
        self.assertIn('new_existing_axis_occupied',plans['BAG']['issues'])

    def test_related_backfill_on_extended_product_preserves_legacy_relations(self):
        self.old['related'] = ['LEGACY']
        plans = self.plan()
        doc = {**self.doc, 'extension_plan': plans['BAG']}
        current = deepcopy(self.old)
        leaf = deepcopy(current['variants'][0])
        leaf.update(code='BLACK', ean=self.addition['ean'], variant_id=303,
                    parameters_new=self.addition['parameters'], main_yn=False)
        current['variants'].append(leaf)
        ids = {'BAG': dict(product_id=101, variant_ids={'BLUE': 202, 'BLACK': 303})}
        with patch.object(native.transport, '_client', return_value=Mock()), \
             patch.object(native, '_read', return_value={'BAG': current}), \
             patch.object(native, '_sparse', return_value=[{'code': 'OTHER'}]), \
             patch.object(native.transport, '_request', return_value={'products': []}) as put:
            self.assertEqual(native.complete_related([doc], 'fp', ids)['BAG']['status'], 'sent')
            self.assertEqual(put.call_args.kwargs['payload']['products'][0]['related'], ['LEGACY', 'OTHER'])
            current['related'].append('OTHER')
            self.assertEqual(native.observe([doc], 'fp', ids)['BAG']['status'], 'verified')
            current['related'].append('MANUAL-SINCE-PREVIEW')
            self.assertEqual(native.observe([doc], 'fp', ids)['BAG']['status'], 'conflict')
            self.assertEqual(native.complete_related([doc], 'fp', ids)['BAG']['status'], 'conflict')
            self.assertEqual(put.call_count, 1)

    def test_changed_sibling_between_preview_and_send_excludes_family(self):
        plans=self.plan()
        self.old['variants'][0]['weight']=888
        with patch.object(native.transport,'_client',return_value=Mock()),\
             patch.object(native,'_read',return_value={'BAG':self.old}),\
             patch('inventory_hub.services.atomer_generations.send') as send:
            self.assertEqual(native.send_extensions(plans,'fp')['BAG']['status'],'conflict')
            send.assert_not_called()

    def test_large_family_can_attach_a_tranche_without_closing_the_source(self):
        remaining=[]
        for i in range(105):
            item=deepcopy(self.addition)
            item.update(code=f'CODE-{i}',ean='',parameters=parameters({'Farba':f'F-{i}'}))
            self.doc['payload']['variants'].append(item)
            remaining.append(item['code'])
        with self.assertRaises(ReviewError):
            native.send([self.doc],'fp')
        plans=self.plan()
        self.assertEqual(plans['BAG']['status'],'ready')
        current=deepcopy(self.old)
        leaf=deepcopy(current['variants'][0])
        leaf.update(code='BLACK',ean=self.addition['ean'],variant_id=303,parameters_new=self.addition['parameters'],main_yn=False)
        current['variants'].append(leaf)
        with patch.object(native.transport,'_client',return_value=Mock()),\
             patch.object(native,'_read',return_value={'BAG':current}):
            result=native.observe_extensions([self.doc],'fp',plans)['BAG']
        self.assertEqual(result['status'],'verified')
        self.assertFalse(result['complete_family'])
        self.assertCountEqual(result['remaining_variants'],remaining)
        self.assertTrue(result['identity_verified'])
