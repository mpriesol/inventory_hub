"""Synthetic Silvini feeds; no account data, supplier snapshots or shop writes."""
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from inventory_hub.adapters.silvini_catalog import parse_catalog, inspection_categories
from inventory_hub.catalog_types import ShopImportOptions
from inventory_hub.feed_mapping_types import MappingDefinition
from inventory_hub.services.catalog_import import build_item
from inventory_hub.services.feed_mapping import apply_definition, inspect_source, native_source_path, parse_mapped
from inventory_hub.services.supplier_availability_source import AvailabilityError, configuration, parse as parse_stock


def variant(code='001-M', size='M', purchase='20.00', discount='12.00', flag='1', ean='0012345678905'):
    return f'''<VARIANT><CODE>{code}</CODE><EXTERNAL_CODE>{code}</EXTERNAL_CODE><EAN>{ean}</EAN>
    <CURRENCY>EUR</CURRENCY><PRICE>49.90</PRICE><STANDARD_PRICE>49.90</STANDARD_PRICE>
    <PURCHASE_PRICE>{purchase}</PURCHASE_PRICE><DISCOUNT_PURCHASE_PRICE>{discount}</DISCOUNT_PURCHASE_PRICE>
    <DISCOUNT_PURCHASE_YESNO>{flag}</DISCOUNT_PURCHASE_YESNO><STOCK><AMOUNT>0</AMOUNT></STOCK>
    <PARAMETERS><PARAMETER><NAME>Velikost</NAME><VALUE>{size}</VALUE></PARAMETER>
    <PARAMETER><NAME>Barva</NAME><VALUE>blue</VALUE></PARAMETER></PARAMETERS></VARIANT>'''


def item(variants=None, pair='001', category='Pánské &gt;&gt; Dresy', weight='0.1', image='first'):
    return f'''<SHOPITEM><NAME>Test jersey</NAME><PAIRCODE>{pair}</PAIRCODE><STYLE>MODEL01</STYLE>
    <MANUFACTURER>SILVINI</MANUFACTURER><DESCRIPTION>&lt;p&gt;Opis&lt;/p&gt;</DESCRIPTION>
    <SHORT_DESCRIPTION>Krátky opis</SHORT_DESCRIPTION><WEIGHT>{weight}</WEIGHT><COMPOSITION>Polyester</COMPOSITION>
    <CATEGORIES><DEFAULT_CATEGORY>{category}</DEFAULT_CATEGORY></CATEGORIES>
    <IMAGES><IMAGE>https://supplier.example.test/{image}.jpg</IMAGE></IMAGES>
    <VARIANTS>{variant() if variants is None else variants}</VARIANTS></SHOPITEM>'''


class SilviniTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'feed.xml'
        self.cfg = {'product_code_prefix':'SIL-', 'adapter_settings':{'vat_rate':23},
                    'feeds':{'sources':{'products':{'catalog_parser':'silvini'},'stock':{'type':'stock'}}}}

    def write(self, content):
        self.path.write_text('<SHOP>'+content+'</SHOP>')

    def products(self, content):
        self.write(content)
        rows=parse_catalog(self.path,'silvini',self.cfg)
        for i,(p,_) in enumerate(rows,1):p.id=i
        return rows

    def confirm_prices(self):
        self.cfg['adapter_settings']['catalog']={'purchase_price_basis':'net','retail_price_basis':'gross'}

    def test_unknown_price_basis_does_not_invent_vat_semantics_or_accept_listing_zeros(self):
        p,raw=self.products(item())[0]
        self.assertEqual((p.code,p.shop_code,p.eans),('001-M','SIL-001-M',['0012345678905']))
        self.assertIsNone(p.prices.purchase_net)
        self.assertIsNone(p.prices.retail_gross)
        self.assertIn('price_basis_unconfirmed',p.warnings)
        self.assertIn('supplier_stock_unverified',p.warnings)
        self.assertIsNone(p.supplier_stock)
        self.assertIsNone(p.supplier_stock_raw)
        self.assertIsNone(p.supplier_available)
        self.assertEqual(raw['fields']['VARIANTS'][0]['VARIANT'][0]['STOCK'][0]['AMOUNT'],['0'])
        self.assertEqual(build_item([p],ShopImportOptions(),self.cfg,True).status,'invalid')

    def test_verified_price_configuration_honors_purchase_discount_without_reducing_regular_moc(self):
        self.confirm_prices()
        p,_=self.products(item())[0]
        self.assertEqual(p.prices.purchase_net,Decimal('12'))
        self.assertEqual(p.prices.purchase_gross,Decimal('14.76'))
        self.assertEqual(p.prices.retail_gross,Decimal('49.90'))
        self.assertEqual(p.prices.retail_net,Decimal('40.57'))
        result=build_item([p],ShopImportOptions(category_code='jersey'),self.cfg,True)
        self.assertEqual(result.status,'ready',result.errors)
        self.assertEqual(result.payload['prices'][0]['price_purchase'],14.76)
        self.assertNotIn('variants',result.payload)
        self.assertNotIn('stock',result.payload)
        self.assertFalse(result.payload['active_yn'])

    def test_no_promotion_uses_regular_purchase_and_bad_active_discount_never_falls_back(self):
        self.confirm_prices()
        p,_=self.products(item(variant(flag='0')))[0]
        self.assertEqual(p.prices.purchase_net,Decimal('20'))
        p,_=self.products(item(variant(discount='bad')))[0]
        self.assertIsNone(p.prices.purchase_net)
        self.assertIn('invalid_number:DISCOUNT_PURCHASE_PRICE',p.warnings)
        with self.assertRaises(ValueError):self.products(item(variant(flag='maybe')))
        self.cfg['adapter_settings']['catalog']['purchase_price_basis']='guess'
        with self.assertRaises(ValueError):self.products(item())

    def test_repeated_families_deduplicate_exact_leaves_and_do_not_assign_parent_weight(self):
        leaves=variant()+variant('001-L','L',ean='0012345678912')
        rows=self.products(item(leaves)+item(leaves,weight='0.2',image='second'))
        self.assertEqual(len(rows),2)
        for p,raw in rows:
            self.assertEqual(p.group_code,'001')
            self.assertEqual(p.variant_relationship,'explicit')
            self.assertEqual([a.name for a in p.variant_attributes],['Velikost','Barva'])
            self.assertEqual(p.images,['https://supplier.example.test/first.jpg','https://supplier.example.test/second.jpg'])
            self.assertEqual(p.import_blockers,[])
            self.assertEqual(raw['parent_weights'],['0.1','0.2'])
            self.assertNotIn('WEIGHT',raw['fields'])
            self.assertEqual(len(raw['fields']['VARIANTS'][0]['VARIANT']),1)
        self.confirm_prices()
        rows=self.products(item(leaves))
        result=build_item([p for p,_ in rows],ShopImportOptions(category_code='jersey'),self.cfg,True)
        self.assertEqual(result.status,'ready',result.errors)
        self.assertEqual(len(result.payload['variants']),2)
        self.assertTrue(all(not v['main_yn'] for v in result.payload['variants']))

    def test_conflicting_same_code_blocks_that_identity_instead_of_choosing_ean_price_or_category(self):
        for other in (item(variant(ean='different')),item(variant(discount='10')),item(category='Other')):
            rows=self.products(item()+other+item(variant('SAFE','L'),pair='SAFE-GROUP'))
            self.assertEqual(len(rows),2)
            self.assertEqual(rows[0][0].import_blockers,['duplicate_supplier_code'])
            self.assertEqual(rows[1][0].import_blockers,[])

    def test_explicit_pair_not_similar_style_or_name_drives_group_and_singletons_stay_fixed(self):
        rows=self.products(item()+item(variant('002-L','L'),pair='002'))
        self.assertEqual(len(rows),2)
        for p,_ in rows:
            self.assertIsNone(p.group_code)
            self.assertEqual(p.variant_attributes,[])
            self.assertTrue(any(a.name=='Velikost' for a in p.parameters))
        rows=self.products(item(variant()+variant('NO-PAIR','L'),pair=''))
        self.assertTrue(all(p.group_code is None for p,_ in rows))

    def test_source_category_counts_are_for_unique_skus_and_match_saved_rules(self):
        self.write(item()+item())
        report=inspection_categories(self.path)
        self.assertEqual(report['source_category_options'],[{'source':'Pánské >> Dresy','code':'Pánské >> Dresy',
            'label':'Pánské >> Dresy','path':'Pánské >> Dresy','count':1}])
        definition=MappingDefinition(category_rules=[{'source':'Pánské >> Dresy','target_code':'jerseys'}])
        rows=parse_mapped(self.path,'silvini',self.cfg,'products',MappingDefinition(),1,parse_catalog)
        p=apply_definition(rows[0][0],rows[0][1]['fields'],definition,'silvini','products',self.cfg,shop='biketrek')
        self.assertEqual(p.target_category_code,'jerseys')
        self.assertTrue(native_source_path(MappingDefinition(record_path='SHOP/SHOPITEM'),parse_catalog))
        self.assertFalse(native_source_path(MappingDefinition(record_path='SHOP/SHOPITEM/VARIANTS/VARIANT'),parse_catalog))

    def test_native_inspection_keeps_unique_category_counts(self):
        import asyncio
        self.write(item()+item())
        with patch('inventory_hub.services.feed_mapping.source_path',AsyncMock(return_value=self.path)),patch('inventory_hub.services.catalog.supplier_config',return_value=self.cfg):
            result=asyncio.run(inspect_source(None,'silvini','products',MappingDefinition()))
        self.assertEqual(result['source_category_options'][0]['count'],1)

    def test_listing_never_substitutes_for_update_feed_and_info_is_not_an_empty_success(self):
        self.write(item())
        with self.assertRaises(ValueError):parse_catalog(self.path,'silvini',self.cfg,'stock')
        with self.assertRaises(AvailabilityError):parse_stock(self.path,'silvini',self.cfg,'products','silvini')
        with patch('inventory_hub.services.catalog.supplier_config',return_value=self.cfg):
            with self.assertRaises(AvailabilityError):configuration('silvini','stock')
        self.path.write_text('<INFO>Ask your supplier for update data</INFO>')
        with self.assertRaises(ValueError):parse_catalog(self.path,'silvini',self.cfg)
        self.path.write_text('<!DOCTYPE SHOP [<!ENTITY x "unsafe">]><SHOP/>')
        with self.assertRaises(ValueError):parse_catalog(self.path,'silvini',self.cfg)

    def test_invalid_identity_and_structure_are_rejected_before_catalog_install(self):
        with self.assertRaises(ValueError):self.products(item(variant(code='')))
        with self.assertRaises(ValueError):self.products(item(variants=''))
        with self.assertRaises(ValueError):self.products(item().replace('<NAME>Test jersey</NAME>',''))
        with self.assertRaises(ValueError):self.products(item(variant().replace('<CURRENCY>EUR</CURRENCY>','<CURRENCY>BAD</CURRENCY>')))
