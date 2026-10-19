"""Availability envelopes share requests without broadening the writable fields."""
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from inventory_hub.services import product_publication_source as service


def identity(code, pid, variant=None, parent=None):
    return {'code':code,'parent_code':parent or code,'variant_code':code if variant else None,
            'product_id':pid,'variant_id':variant}


class BatchAvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.items=[{'identity':identity('S',1),'availability':'vypredané'},
                    {'identity':identity('V1',2,21,'P'),'availability':'do 5 dní'},
                    {'identity':identity('V2',2,22,'P'),'availability':'SKLADOM'}]
        self.ack={'products':[{'code':'S','product_id':1,'updated_yn':True},
            {'code':'P','product_id':2,'updated_yn':True,'variants':[
                {'code':'V1','variant_id':21,'updated_yn':True}, {'code':'V2','variant_id':22,'updated_yn':True}]}]}

    def write(self, ack):
        client=SimpleNamespace(session=Mock())
        with patch.object(service.source,'_client',return_value=client), patch.object(service.source,'_request',return_value=ack) as request:
            try: service._write_availability_batch('biketrek',self.items,'a'*64)
            finally: client.session.close.assert_called_once()
        return request

    def test_one_put_grouped_variants_and_only_availability(self):
        request=self.write(self.ack)
        request.assert_called_once()
        self.assertEqual(request.call_args.args[1:3],('put','products'))
        self.assertEqual(request.call_args.kwargs['payload'],{'products':[
            {'code':'S','availability':'vypredané'}, {'code':'P','variants':[
                {'code':'V1','availability':'do 5 dní'},{'code':'V2','availability':'SKLADOM'}]}]})

    def test_partial_conflicting_or_unconfirmed_ack_is_uncertain(self):
        cases=[]
        value=copy.deepcopy(self.ack);value['products'].pop();cases.append(value)
        value=copy.deepcopy(self.ack);value['products'][1]['variants'].pop();cases.append(value)
        value=copy.deepcopy(self.ack);value['products'][1]['variants'][1]['variant_id']=999;cases.append(value)
        value=copy.deepcopy(self.ack);value['products'][0]['updated_yn']=False;cases.append(value)
        value=copy.deepcopy(self.ack);value['messages']=[{'level':'error'}];cases.append(value)
        for value in cases:
            with self.subTest(value=value),self.assertRaises(service.source.SourceError) as error:self.write(value)
            self.assertTrue(error.exception.uncertain)

    def test_bad_scope_rejected_before_client(self):
        cases=[[],self.items*8, self.items+[self.items[0]],
            [{'identity':identity('P',2),'availability':'SKLADOM'},self.items[1]],
            [{'identity':identity('S',1),'availability':'bad\nlabel'}]]
        with patch.object(service.source,'_client') as client:
            for items in cases:
                with self.subTest(items=items),self.assertRaises(service.source.SourceError):
                    service._write_availability_batch('biketrek',items,'a'*64)
            client.assert_not_called()
