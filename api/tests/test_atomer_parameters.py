"""Prevent decimal splitting, loss of unrelated values and wording changes."""
from copy import deepcopy
from unittest import TestCase
from unittest.mock import patch

from inventory_hub.adapters.atomer_content import AtomerSourceError, html_fingerprint
from inventory_hub.atomer_content_types import AtomerContentBatch
from inventory_hub.services.atomer_parameters import canonical_parameters, format_for_upgates, source_parameters
from inventory_hub.services import atomer_content_source as transport
from test_atomer_content_transport import parent, response


def parameter(name='Rozmer', value='180 x 60 x 0,8 cm', language='sk'):
    return {'descriptions': [{'language': language, 'name': name}],
            'values': [{'descriptions': [{'language': language, 'value': value}]}]}


def selected(value='180 x 60 x 0,8 cm'):
    return {'rows': [{'parent_code': '', 'raw_xml': '<PRODUCT><PROPERTIES><PROPERTY_1>'
                     + value + '</PROPERTY_1></PROPERTIES></PRODUCT>'}]}


class AtomerParameterTests(TestCase):
    def test_original_decimal_and_comma_material_are_not_split(self):
        for value in ('180 x 60 x 0,8 cm', 'Penový materiál EVA, alumíniová fólia'):
            result = source_parameters(selected(value), {'PROPERTY_1': 'Rozmer'}, [])
            self.assertEqual(result, [parameter(value=value)])

    def test_source_name_replaces_only_that_parameter_and_preserves_extras(self):
        old = parameter(value='180 x 60 x 0')
        old['values'].append({'descriptions': [{'language': 'sk', 'value': '8'}]})
        extra = parameter('Unrelated', 'Original English', 'en')
        result = source_parameters(selected(), {'PROPERTY_1': 'Rozmer'}, [old, extra])
        self.assertIn(extra, result)
        self.assertIn(parameter(), result)
        self.assertEqual(len(result), 2)

    def test_other_language_on_replaced_value_is_not_silently_erased(self):
        old = parameter()
        old['values'][0]['descriptions'].append({'language': 'en', 'value': '180 x 60 x 0.8 cm'})
        with self.assertRaises(AtomerSourceError):
            source_parameters(selected(), {'PROPERTY_1': 'Rozmer'}, [old])

    def test_missing_mapping_duplicate_source_or_variant_family_refuse(self):
        duplicate = selected(); duplicate['rows'][0]['raw_xml'] = '<PRODUCT><PROPERTIES><PROPERTY_1>A</PROPERTY_1><PROPERTY_1>B</PROPERTY_1></PROPERTIES></PRODUCT>'
        variant = selected(); variant['rows'][0]['parent_code'] = 'FAMILY'
        for group, names in ((selected(), {}), (duplicate, {'PROPERTY_1': 'Name'}), (variant, {'PROPERTY_1': 'Name'})):
            with self.subTest(group=group), self.assertRaises(AtomerSourceError):
                source_parameters(group, names, [])

    def test_parameter_order_is_irrelevant_but_a_changed_unit_is_not(self):
        values = [parameter(), parameter('Materiál', 'EVA, hliník')]
        self.assertEqual(canonical_parameters(values), canonical_parameters(list(reversed(values))))
        self.assertTrue(transport.compare_values({'parameters': values}, {'parameters': list(reversed(values))}))
        changed = deepcopy(values); changed[0]['values'][0]['descriptions'][0]['value'] = '180 x 60 x 0,8 mm'
        self.assertFalse(transport.compare_values({'parameters': values}, {'parameters': changed}))

    def test_arbitrary_keys_empty_values_and_duplicate_names_refuse(self):
        cases = [[{**parameter(), 'stock': 5}], [parameter(value='')], [parameter(), parameter(value='other')]]
        for values in cases:
            with self.subTest(values=values), self.assertRaises(AtomerSourceError):
                canonical_parameters(values)

    def test_formatting_keeps_words_bold_decimal_and_table_structure(self):
        value = '<p class="old" style="text-align: justify; color: #993300"><strong>Opis:</strong> 0,8 cm</p><table class="old"><tr><td>0,24 kg</td><td>180 x 60</td></tr></table>'
        formatted = format_for_upgates(value)
        self.assertNotIn('class=', formatted); self.assertNotIn('text-align', formatted)
        self.assertIn('<strong>Opis:</strong>', formatted)
        self.assertIn('0,8 cm', formatted); self.assertIn('0,24 kg', formatted)
        self.assertIn('<table>', formatted); self.assertEqual(formatted.count('<td>'), 2)
        self.assertEqual(html_fingerprint(formatted), html_fingerprint(format_for_upgates(formatted)))

    def test_style_based_emphasis_is_retained_and_unsafe_html_is_not_cleaned(self):
        formatted = format_for_upgates('<p style="font-weight: bold; text-align: justify">Exact words</p>')
        self.assertIn('font-weight: bold', formatted)
        with self.assertRaises(AtomerSourceError):
            format_for_upgates('<p onclick="run()">Exact words</p>')

    def test_read_parameters_checks_exact_native_identity(self):
        row = parent(); row.update(code='ONE', variants_exists_yn=False, variants=[])
        identity = {'code': 'ONE', 'parent_code': 'ONE', 'product_id': 101, 'variant_code': None, 'parameters_yn': True}
        detail = {'code': 'ONE', 'product_id': 101, 'parameters_new': [parameter()]}
        with patch.object(transport.source, '_request', side_effect=[response(row), response(detail)]) as request:
            remote = transport._read(object(), identity)
        self.assertEqual(request.call_args.args[2], 'products/parameters')
        self.assertEqual(transport.field_values(remote, ['parameters']), {'parameters': [parameter()]})
        detail['product_id'] = 999
        with patch.object(transport.source, '_request', side_effect=[response(row), response(detail)]), self.assertRaises(transport.source.SourceError):
            transport._read(object(), identity)

    def test_parameter_payload_requires_frozen_standalone_identity(self):
        row = parent(); row.update(code='ONE', variants_exists_yn=False, variants=[])
        identity = {'code': 'ONE', 'parent_code': 'ONE', 'product_id': 101, 'variant_code': None, 'parameters_yn': True}
        with patch.object(transport.source, '_request', side_effect=[response(row), response({'code': 'ONE', 'product_id': 101, 'parameters_new': [parameter()]})]):
            remote = transport._read(object(), identity)
        patch_value = transport.build_patch({'parameters': [parameter(value='Original, unsplit')]}, remote, ['parameters'])
        self.assertEqual(set(patch_value['payload']['products'][0]), {'code', 'parameters'})
        broken = deepcopy(remote['identity']); broken.pop('parameters_yn')
        with self.assertRaises(transport.source.SourceError):
            transport._validate_payload(patch_value['payload'], broken)
        changed = deepcopy(remote); changed['parent']['stock'] = 999
        self.assertNotEqual(transport.protected_observation(remote, ['parameters']), transport.protected_observation(changed, ['parameters']))

    def test_parameter_mapping_is_explicit_and_bounded(self):
        item = {'request_id': '0784ed03-ae8b-4946-817f-9b44f1ea637e', 'hub_sku': 'ONE', 'source_code': 'ONE', 'fields': ['parameters']}
        for mapping in ({}, {'PROPERTY_1': ''}, {'arbitrary': 'Name'}):
            with self.subTest(mapping=mapping), self.assertRaises(ValueError):
                AtomerContentBatch(items=[item], parameter_names=mapping)
        request = AtomerContentBatch(items=[item], parameter_names={'PROPERTY_1': 'Rozmer'}, formatting='upgates')
        self.assertEqual(request.parameter_names['PROPERTY_1'], 'Rozmer')
