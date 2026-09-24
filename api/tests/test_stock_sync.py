"""The regular transport never broadens a leaf payload or retries a PUT."""
import unittest
from unittest.mock import Mock, patch
from pydantic import ValidationError
from inventory_hub.stock_sync_types import ShopSyncInput, SyncRunInput, SyncResolveInput
from inventory_hub.services import stock_sync_source as source
from inventory_hub.services.stock_sync import matches


class StockSyncContractTests(unittest.TestCase):
    def test_activation_does_not_accept_truthy_strings(self):
        base = dict(shop_code='biketrek', expected_revision=0, enabled=False, authorized=False, confirmed=True)
        for field in ('enabled','authorized','confirmed','hub_is_stock_authority','external_stock_writers_disabled','orders_reconciled'):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                ShopSyncInput(**{**base, field: 'true'})
        for value in (1, 'true', False):
            with self.assertRaises(ValidationError):
                SyncResolveInput(confirmed=True, external_requests_finished=value)

    def test_manual_scope_is_exact_and_bounded(self):
        for skus in ([], ['A','A'], [' A'], ['A\n'], ['A'] * 101):
            with self.assertRaises(ValidationError):
                SyncRunInput(shop_code='biketrek', confirmed=True, skus=skus)
        self.assertEqual(SyncRunInput(shop_code='xtrek', confirmed=True, skus=['SKU-01']).skus, ['SKU-01'])

    def test_variant_payload_does_not_touch_umbrella_or_siblings(self):
        identity = {'code':'SKU-A','parent_code':'xTrek','variant_code':'SKU-A','product_id':9,'variant_id':8}
        desired = {'stock':'4','availability':'do 7 dní','can_add_to_basket_yn':True}
        client = Mock()
        ack = {'products':[{'code':'xTrek','product_id':9,'updated_yn':True,
                           'variants':[{'code':'SKU-A','variant_id':8,'updated_yn':True}]}]}
        with patch.object(source.transport, '_client', return_value=client), patch.object(source.transport, '_request', return_value=ack) as request:
            source._write('biketrek', identity, desired, 'a'*64)
        self.assertEqual(request.call_args.kwargs['payload'], {'products':[{'code':'xTrek','variants':[
            {'code':'SKU-A','stock':4,'availability':'do 7 dní','can_add_to_basket_yn':True}]}]})
        client.session.close.assert_called_once()

    def test_ambiguous_ack_never_retries(self):
        identity = {'code':'A','parent_code':'A','variant_code':None,'product_id':9,'variant_id':None}
        with patch.object(source.transport,'_client',return_value=Mock()), patch.object(source.transport,'_request',return_value={}) as request:
            with self.assertRaises(source.SourceError) as raised:
                source._write('xtrek',identity,{'stock':'0','availability':'overíme','can_add_to_basket_yn':True},'a'*64)
        self.assertTrue(raised.exception.uncertain)
        request.assert_called_once()

    def test_readback_checks_orderability_and_availability_not_only_stock(self):
        desired = {'stock':'0','availability':'overíme','can_add_to_basket_yn':True}
        self.assertFalse(matches({'stock':'0'},desired))
        self.assertFalse(matches({**desired,'can_add_to_basket_yn':False},desired))
        self.assertTrue(matches({**desired,'identity':{}},desired))

    def test_outgoing_fields_are_whitelisted_and_unknown_stock_is_not_zero(self):
        valid = {'stock':'0','availability':'overíme','can_add_to_basket_yn':True}
        for value in ({**valid,'stock':None},{**valid,'price':1},{**valid,'can_add_to_basket_yn':False}):
            with self.assertRaises(source.SourceError):
                source._fields(value)
