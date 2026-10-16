"""Synthetic export contracts; never call the supplier or production shops."""
import asyncio
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

from inventory_hub.adapters.bike_brothers_catalog import parse_catalog, inspection_categories
from inventory_hub.catalog_types import ShopImportOptions
from inventory_hub.feed_mapping_types import MappingDefinition
from inventory_hub.services.catalog_import import build_item
from inventory_hub.services.feed_mapping import inspect_source, native_source_path, parse_mapped
from inventory_hub.services.supplier_availability_source import parse as parse_stock


def item(identity='1', code='0007', name='Test výrobok', group='', stock='5', purchase='2.10', retail='4.30',
         ean='0012345678905', extra=''):
    return f'''<SHOPITEM><id>{identity}</id><kod>{code}</kod><nazev>{name}</nazev>
    <mastergroup>{group}</mastergroup><ean>{ean}</ean><kategorie>Značky | Test</kategorie><vyrobce>Test</vyrobce>
    <cena_bez_dph_voc>900</cena_bez_dph_voc><cena_bez_dph_moc>1800</cena_bez_dph_moc>
    <cena_bez_dph_eurvoc>{purchase}</cena_bez_dph_eurvoc><cena_bez_dph_eurmoc>{retail}</cena_bez_dph_eurmoc>
    <stav_zasob>{stock}</stav_zasob><dostupnost_dny>0</dostupnost_dny>
    <popis_1>&lt;p&gt;Zdrojový opis&lt;/p&gt;</popis_1><popis_2>&lt;p&gt;Technické údaje&lt;/p&gt;</popis_2>
    <popis_3>&lt;p&gt;Technické údaje&lt;/p&gt;</popis_3>
    <obrazek_hlavni>https://supplier.example.test/main.jpg</obrazek_hlavni>
    <obrazek>https://supplier.example.test/other.jpg</obrazek><obrazek>javascript:invalid</obrazek>{extra}</SHOPITEM>'''


class BikeBrothersTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'feed.xml'
        self.cfg = {'product_code_prefix':'BRO-', 'adapter_settings':{'vat_rate':23},
                    'feeds':{'sources':{'products':{'catalog_parser':'bike-brothers'}, 'stock':{'type':'stock'}}}}

    def write(self, body):
        self.path.write_text('<SHOP>'+body+'</SHOP>')

    def products(self, body):
        self.write(body)
        records = parse_catalog(self.path, 'bike-brothers', self.cfg)
        for index, (product, _) in enumerate(records, 1):
            product.id = index
        return records

    def test_exact_sku_ean_and_eur_net_prices_convert_at_explicit_destination_vat(self):
        product, raw = self.products(item())[0]
        self.assertEqual((product.shop_code, product.eans), ('BRO-0007', ['0012345678905']))
        self.assertEqual(product.prices.currency, 'EUR')
        self.assertEqual((product.prices.purchase_net, product.prices.retail_net), (Decimal('2.10'), Decimal('4.30')))
        self.assertEqual((product.prices.purchase_gross, product.prices.retail_gross), (Decimal('2.58'), Decimal('5.29')))
        self.assertEqual(raw['fields']['cena_bez_dph_voc'], ['900'])
        for gross, expected in ((True, 2.58), (False, 2.10)):
            result = build_item([product], ShopImportOptions(category_code='test'), self.cfg, gross)
            self.assertEqual(result.status, 'ready', result.errors)
            self.assertEqual(result.payload['prices'][0]['price_purchase'], expected)
            self.assertFalse(result.payload['active_yn'])
            self.assertNotIn('stock', result.payload)
            self.assertNotIn('stock_increment', result.payload)
            self.assertEqual(result.payload['vats'], {'sk':23.0})

    def test_missing_bad_eur_or_missing_vat_cannot_borrow_czk_or_parent_prices(self):
        product, _ = self.products(item(purchase='NaN', retail=''))[0]
        self.assertIsNone(product.prices.purchase_net)
        self.assertIsNone(product.prices.retail_gross)
        self.assertIn('missing_retail_price', product.warnings)
        self.assertEqual(build_item([product], ShopImportOptions(), self.cfg, True).status, 'invalid')
        self.cfg['adapter_settings'] = {}
        product, _ = self.products(item())[0]
        self.assertIsNone(product.prices.retail_gross)
        self.assertIn('missing_vat', product.warnings)

    def test_parents_are_context_only_and_never_an_extra_product_or_stock_observation(self):
        records = self.products(item('900', 'PARENT', stock='999', ean='')+
                                item('1', '001', 'Test A', '900', stock='2')+
                                item('2', '002', 'Test B', '900', stock='3', ean='0012345678906'))
        self.assertEqual([p.code for p, _ in records], ['001', '002'])
        self.assertTrue(all(p.group_code == '900' and p.group_name == 'Test výrobok' for p, _ in records))
        self.assertTrue(records[0][1]['parent_xml'].startswith('<SHOPITEM>'))
        result = build_item([p for p, _ in records], ShopImportOptions(), self.cfg, True)
        self.assertEqual(result.status, 'ready', result.errors)
        self.assertEqual(result.code, 'BRO-G-900')
        self.assertEqual([v['code'] for v in result.payload['variants']], ['BRO-001', 'BRO-002'])
        stock = parse_stock(self.path, 'bike-brothers', self.cfg, 'stock', 'bike-brothers')
        self.assertEqual([(o.sku, o.quantity) for o in stock], [('001', Decimal('2')), ('002', Decimal('3'))])
        self.assertEqual(inspection_categories(self.path)['source_category_options'][0]['count'], 2)

    def test_explicit_families_keep_title_labels_without_inventing_size_or_colour(self):
        rows = self.products(item('1', '001', 'Model - varianta A', 'missing')+
                             item('2', '002', 'Model - varianta B', 'missing', ean='0012345678906'))
        self.assertEqual(rows[0][0].variant_attributes[0].name, 'Variant')
        self.assertEqual(rows[0][0].variant_attributes[0].value, 'Model - varianta A')
        self.assertIn('variant_label_from_title', rows[0][0].warnings)
        result = build_item([p for p, _ in rows], ShopImportOptions(), self.cfg, True)
        self.assertEqual(result.status, 'ready', result.errors)
        rows = self.products(item('1', '001')+item('2', '002', ean='0012345678906'))
        self.assertTrue(all(p.group_code is None for p, _ in rows))
        singleton, _ = self.products(item(group='solo'))[0]
        self.assertIsNone(singleton.group_code)
        self.assertEqual(singleton.variant_attributes, [])

    def test_parent_text_can_fill_gaps_but_leaf_identity_price_and_stock_stay_independent(self):
        leaf = item('1', '001', group='900', purchase='', stock='', ean='')
        for fragment in ('<kategorie>Značky | Test</kategorie>', '<popis_1>&lt;p&gt;Zdrojový opis&lt;/p&gt;</popis_1>'):
            leaf = leaf.replace(fragment, '')
        product, raw = self.products(item('900', 'PARENT', stock='999')+leaf)[0]
        self.assertEqual(product.category, 'Značky | Test')
        self.assertIn('Zdrojový opis', product.description)
        self.assertEqual(product.description.count('Technické údaje'), 1)
        self.assertEqual(product.eans, [])
        self.assertIsNone(product.prices.purchase_net)
        self.assertIsNone(product.supplier_stock)
        self.assertIsNone(product.supplier_available)
        self.assertEqual(inspection_categories(self.path)['source_category_options'][0]['count'], 1)

    def test_source_czech_text_is_retained_with_actionable_warning_not_mislabelled_as_translated(self):
        product, raw = self.products(item(name='Český zvonek'))[0]
        self.assertEqual(product.name, 'Český zvonek')
        self.assertEqual(raw['source_language'], 'cs')
        self.assertIn('source_language_cs', product.warnings)
        self.cfg['adapter_settings']['catalog'] = {'source_language':'sk'}
        product, _ = self.products(item(name='Slovenský zvonček'))[0]
        self.assertNotIn('source_language_cs', product.warnings)

    def test_stock_partial_acceptance_has_no_price_dependency_or_delivery_day_substitution(self):
        self.write(item('1', 'GOOD', stock='8', purchase='bad', retail='')+item('2', 'ZERO', stock='0')+
                   item('3', 'BAD', stock='bad')+item('4', 'UNKNOWN', stock='')+
                   item('5', 'DUP')+item('6', 'DUP'))
        result = parse_stock(self.path, 'bike-brothers', self.cfg, 'stock', 'bike-brothers')
        self.assertEqual([(o.sku, o.quantity) for o in result], [('GOOD', Decimal('8')), ('ZERO', Decimal('0'))])
        self.assertEqual(result.report(), {'total':6, 'accepted':2, 'rejected':3, 'unknown':1, 'reasons':{
            'supplier_availability_invalid_quantity':1, 'supplier_availability_unknown_quantity':1,
            'supplier_availability_duplicate_identity':2}})

    def test_duplicate_quantity_or_sku_is_a_local_stock_issue_but_structure_is_not_partial(self):
        self.write(item('1', 'GOOD')+item('2', '', extra='<stav_zasob>99</stav_zasob>')+
                   item('3', 'AMBIGUOUS', extra='<stav_zasob>99</stav_zasob>'))
        result = parse_stock(self.path, 'bike-brothers', self.cfg, 'stock', 'bike-brothers')
        self.assertEqual([o.sku for o in result], ['GOOD'])
        self.assertEqual(result.report()['rejected'], 2)
        for body in (item('1')+item('1'), item('1', group='1')):
            with self.assertRaises(ValueError): self.products(body)
        self.path.write_text('<!DOCTYPE SHOP [<!ENTITY x "unsafe">]><SHOP/>')
        with self.assertRaises(ValueError): parse_catalog(self.path, 'bike-brothers', self.cfg)

    def test_shared_ean_blocks_conflicting_leaf_codes_without_borrowing_parent_ean(self):
        rows = self.products(item('1', '001')+item('2', '002'))
        self.assertTrue(all('duplicate_supplier_ean' in p.import_blockers for p, _ in rows))
        self.assertEqual(build_item([rows[0][0]], ShopImportOptions(), self.cfg, True).status, 'invalid')

    def test_native_mapping_preserves_leaf_semantics_and_graphic_category_counts(self):
        self.write(item('900', 'PARENT')+item('1', '001', group='900', ean='0012345678906'))
        definition = MappingDefinition()
        self.assertTrue(native_source_path(definition, parse_catalog))
        self.assertEqual(len(parse_mapped(self.path, 'bike-brothers', self.cfg, 'products', definition, 1, parse_catalog)), 1)
        with patch('inventory_hub.services.feed_mapping.source_path', AsyncMock(return_value=self.path)), patch(
                'inventory_hub.services.catalog.supplier_config', return_value=self.cfg):
            report = asyncio.run(inspect_source(None, 'bike-brothers', 'products', definition))
        self.assertEqual(report['source_category_options'][0]['source'], 'Značky | Test')
        self.assertEqual(report['source_category_options'][0]['count'], 1)
