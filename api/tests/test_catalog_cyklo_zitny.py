"""Synthetic Cyklo Zitny feeds; no credentials, live feeds or shop writes."""
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

from inventory_hub.adapters.cyklo_zitny_catalog import parse_catalog, inspection_categories
from inventory_hub.catalog_types import ShopImportOptions
from inventory_hub.feed_mapping_types import MappingDefinition
from inventory_hub.services.catalog_import import build_item
from inventory_hub.services.feed_mapping import apply_definition, inspect_source, native_source_path, parse_mapped
from inventory_hub.services.feed_mapping_source import inspect_records, read_records
from inventory_hub.services.supplier_availability_source import parse as parse_stock


CATEGORIES = '<item><id>1</id><parent_id>0</parent_id><name>Výrobca</name></item><item><id>2</id><parent_id>1</parent_id><name>Gripy</name></item>'


def item(code='00.001', extra='', purchase='4.03', retail='8.38', stock='6'):
    return f'''<item><id>426</id><code>{code}</code><name>Test gripy čierne</name><manufacturer>Test</manufacturer>
    <ean>0710845000645</ean><vat>21</vat><price_buy_novat>98.19</price_buy_novat>
    <price_buy_novat_eur>{purchase}</price_buy_novat_eur><price_moc_eur>{retail}</price_moc_eur>
    <price_moc_novat_eur>6.92</price_moc_novat_eur><store_count>{stock}</store_count><delivery_date>0</delivery_date>
    <description>Krátky opis</description><description_full>&lt;p&gt;Úplný opis&lt;/p&gt;</description_full>
    <images><url>https://supplier.example.test/image.jpg</url><url>javascript:invalid</url></images>
    <categories><id>1</id><id>999</id><id>2</id></categories>
    <categorytext>Výrobca</categorytext><categorytext/><categorytext>Výrobca/Gripy</categorytext>{extra}</item>'''


class CykloZitnyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'feed.xml'
        self.cfg = {'product_code_prefix':'CZ-', 'adapter_settings':{'vat_rate':23},
                    'feeds':{'sources':{'products':{'catalog_parser':'cyklo-zitny'},'stock':{'type':'stock'}}}}

    def write(self, products, categories=CATEGORIES):
        self.path.write_text('<export><items type="categories">'+categories+'</items><items type="products">'+products+'</items></export>')

    def products(self, products):
        self.write(products)
        rows = parse_catalog(self.path,'cyklo-zitny',self.cfg)
        for i,(product,_) in enumerate(rows,1):product.id=i
        return rows

    def test_eur_net_cost_and_gross_moc_survive_with_destination_vat_and_exact_identity(self):
        product, raw = self.products(item())[0]
        self.assertEqual((product.code,product.shop_code,product.eans),('00.001','CZ-00.001',['0710845000645']))
        self.assertEqual(product.prices.currency,'EUR')
        self.assertEqual(product.prices.purchase_net,Decimal('4.03'))
        self.assertEqual(product.prices.purchase_gross,Decimal('4.96'))
        self.assertEqual(product.prices.retail_gross,Decimal('8.38'))
        self.assertEqual(product.prices.retail_net,Decimal('6.81'))
        self.assertEqual(product.prices.vat_percent,23)
        self.assertEqual(raw['fields']['vat'],['21'])
        self.assertEqual(raw['fields']['price_buy_novat'],['98.19'])
        self.assertEqual(product.description,'<p>Úplný opis</p>')
        self.assertEqual(product.images,['https://supplier.example.test/image.jpg'])
        for with_vat, purchase in ((True,4.96),(False,4.03)):
            result=build_item([product],ShopImportOptions(category_code='grips'),self.cfg,with_vat)
            self.assertEqual(result.status,'ready',result.errors)
            self.assertEqual(result.payload['prices'][0]['price_purchase'],purchase)
            self.assertEqual(result.payload['vats'],{'sk':23.0})
            self.assertFalse(result.payload['active_yn'])
            self.assertNotIn('variants',result.payload)
            self.assertNotIn('stock',result.payload)
            self.assertNotIn('stock_increment',result.payload)

    def test_explicit_alternate_retail_policy_recalculates_from_eur_net(self):
        self.cfg['adapter_settings']['catalog']={'retail_vat_policy':'preserve_net'}
        product,_=self.products(item())[0]
        self.assertEqual(product.prices.retail_gross,Decimal('8.51'))
        self.assertEqual(product.prices.purchase_net,Decimal('4.03'))
        self.cfg['adapter_settings']['catalog']['retail_vat_policy']='guess'
        with self.assertRaises(ValueError):self.products(item())

    def test_missing_eur_price_never_borrows_the_czk_column_or_invents_zero(self):
        product,_=self.products(item(purchase='',retail=''))[0]
        self.assertIsNone(product.prices.purchase_net)
        self.assertIsNone(product.prices.retail_gross)
        self.assertIn('missing_retail_price',product.warnings)
        product,_=self.products(item(purchase='NaN',retail='0'))[0]
        self.assertIsNone(product.prices.purchase_net)
        self.assertIn('invalid_number:price_buy_novat_eur',product.warnings)
        self.assertEqual(build_item([product],ShopImportOptions(),self.cfg,True).status,'invalid')
        self.cfg['adapter_settings']={}
        product,_=self.products(item())[0]
        self.assertIsNone(product.prices.vat_percent)
        self.assertIn('missing_vat',product.warnings)

    def test_products_are_standalone_and_internal_id_or_similar_name_does_not_group_them(self):
        rows=self.products(item('001')+item('002'))
        self.assertEqual(len(rows),2)
        for product,_ in rows:
            self.assertIsNone(product.group_code)
            self.assertEqual(product.variant_attributes,[])
            self.assertEqual(product.variant_relationship,'not_provided')
        with self.assertRaises(ValueError):self.products(item('001')+item('001'))
        with self.assertRaises(ValueError):self.products(item(''))

    def test_category_dictionary_drives_one_exact_deepest_source_not_parallel_array_positions(self):
        product,_=self.products(item())[0]
        self.assertEqual((product.category_code,product.category),('2','Výrobca / Gripy'))
        report=inspection_categories(self.path)
        self.assertEqual(report['source_category_options'],[{'source':'2','code':'2','label':'Výrobca / Gripy','path':'Výrobca / Gripy','count':1}])
        mapped=apply_definition(product,{},MappingDefinition(category_rules=[{'source':'2','target_code':'target-grips'}]),'cyklo-zitny','products',self.cfg,shop='biketrek')
        self.assertEqual(mapped.target_category_code,'target-grips')
        self.write(item(),'<item><id>1</id><parent_id>2</parent_id><name>A</name></item><item><id>2</id><parent_id>1</parent_id><name>B</name></item>')
        product,_=parse_catalog(self.path,'cyklo-zitny',self.cfg)[0]
        self.assertIsNone(product.category_code)
        self.assertIn('missing_category',product.warnings)

    def test_automatic_and_explicit_inspection_exclude_the_sibling_dictionary(self):
        self.write(item('001')+item('002'))
        for definition in (MappingDefinition(),MappingDefinition(record_path='export/items/item')):
            records,kind,path=read_records(self.path,definition)
            self.assertEqual(len(records),2)
            self.assertEqual(path,'export/items/item')
            self.assertEqual(inspect_records(records,kind,path)['total_records'],2)
            self.assertTrue(native_source_path(definition,parse_catalog))
            self.assertEqual(len(parse_mapped(self.path,'cyklo-zitny',self.cfg,'products',definition,1,parse_catalog)),2)
        self.assertFalse(native_source_path(MappingDefinition(record_path='export/other/item'),parse_catalog))

    def test_stock_reader_keeps_quantities_and_identity_without_price_or_catalog_dependencies(self):
        self.write(item('GOOD',purchase='NaN',retail='',stock='20')+item('ZERO',stock='0')+
                   item('BAD',stock='bad')+item('UNKNOWN',stock='')+item('DUP')+item('DUP'))
        result=parse_stock(self.path,'cyklo-zitny',self.cfg,'stock','cyklo-zitny')
        self.assertEqual([(o.sku,o.quantity) for o in result],[('GOOD',Decimal('20')),('ZERO',Decimal('0'))])
        self.assertEqual(result.report(),{'total':6,'accepted':2,'rejected':3,'unknown':1,'reasons':{
            'supplier_availability_invalid_quantity':1,'supplier_availability_unknown_quantity':1,'supplier_availability_duplicate_identity':2}})
        self.assertTrue(result[0].available)
        self.assertFalse(result[1].available)

    def test_missing_stock_identity_is_reported_locally_and_ambiguous_values_are_refused(self):
        self.write(item('GOOD')+item().replace('<code>00.001</code>',''))
        result=parse_stock(self.path,'cyklo-zitny',self.cfg,'stock','cyklo-zitny')
        self.assertEqual([o.sku for o in result],['GOOD'])
        self.assertEqual(result.issues[0]['code'],'supplier_availability_invalid_identity')
        with self.assertRaises(ValueError):self.products(item(extra='<price_buy_novat_eur>9</price_buy_novat_eur>'))
        self.write(item('GOOD')+item(extra='<store_count>9</store_count>'))
        result=parse_stock(self.path,'cyklo-zitny',self.cfg,'stock','cyklo-zitny')
        self.assertEqual([o.sku for o in result],['GOOD'])
        self.assertEqual(result.issues[0]['code'],'supplier_availability_invalid_quantity')
        self.write(item(),CATEGORIES+CATEGORIES)
        self.assertEqual(len(parse_stock(self.path,'cyklo-zitny',self.cfg,'stock','cyklo-zitny')),1)
        with self.assertRaises(ValueError):parse_catalog(self.path,'cyklo-zitny',self.cfg)

    def test_availability_id_is_not_a_sku_and_delivery_zero_is_not_a_quantity(self):
        self.path.write_text('<export><items type="availability"><item><id>426</id><delivery_date>0</delivery_date></item></items></export>')
        with self.assertRaises(ValueError):parse_catalog(self.path,'cyklo-zitny',self.cfg)
        with self.assertRaises(ValueError):parse_stock(self.path,'cyklo-zitny',self.cfg,'stock','cyklo-zitny')
        product,_=self.products(item(stock=''))[0]
        self.assertIsNone(product.supplier_available)
        self.assertIsNone(product.supplier_stock)
        self.path.write_text('<!DOCTYPE export [<!ENTITY x "unsafe">]><export/>')
        with self.assertRaises(ValueError):parse_catalog(self.path,'cyklo-zitny',self.cfg)

    def test_native_inspection_uses_the_same_category_codes_as_catalog_normalization(self):
        self.write(item('001')+item('002'))
        import asyncio
        with patch('inventory_hub.services.feed_mapping.source_path',AsyncMock(return_value=self.path)),patch('inventory_hub.services.catalog.supplier_config',return_value=self.cfg):
            result=asyncio.run(inspect_source(None,'cyklo-zitny','products',MappingDefinition()))
        self.assertEqual(result['total_records'],2)
        self.assertEqual(result['source_category_options'][0]['source'],'2')
        self.assertEqual(result['source_category_options'][0]['count'],2)
