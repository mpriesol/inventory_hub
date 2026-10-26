"""Northfinder source spelling and category-scoped semantic mappings."""
import unittest
from copy import deepcopy
from types import SimpleNamespace

from test_ai_content import content, context
from test_product_import import row
from inventory_hub.ai_content_types import ParameterDefinition
from inventory_hub.services.ai_content_validation import validate_content, overlay
from inventory_hub.services.product_import import merge_ai_values
from inventory_hub.services import ai_content


class ParameterMappingTests(unittest.TestCase):
    def fixture(self, source='FARBA'):
        ctx = context()
        ctx['facts'] = [{'id': 1, 'variant_attributes': [{'name': source, 'value': 'alloverprint'}, {'name': 'VELKOST', 'value': 'XS'}]}]
        ctx['resolved']['category']['parameters'] = [ParameterDefinition(name=name, scope='choice', required=True).model_dump() for name in ('Farba', 'Veľkosť')]
        output = content(parameters=[{'name': 'Farba', 'product_id': 1, 'values': ['alloverprint'], **({'source_name': source} if source == 'Colour' else {})}, {'name': 'Veľkosť', 'product_id': 1, 'values': ['XS']}])
        return ctx, output

    def test_northfinder_case_accents_and_export_keep_exact_values_and_identifiers(self):
        ctx, output = self.fixture()
        checks = validate_content(output, ctx)
        self.assertEqual(checks['errors'], [])
        self.assertEqual(checks['parameter_mapping'], {'FARBA': 'Farba', 'VELKOST': 'Veľkosť'})
        payload = {'descriptions': [{'language': 'sk'}], 'variants': [{'code': 'NF-1', 'ean': '00123', 'parameters': [
            {'descriptions': [{'language': 'sk', 'name': 'FARBA'}], 'values': [{'descriptions': [{'language': 'sk', 'value': 'alloverprint'}]}]}]}]}
        result = overlay(SimpleNamespace(payload=deepcopy(payload)), {'active_after_import': False, 'content': output.model_dump(), 'parameter_mapping': checks['parameter_mapping'], 'registered_parameters': True}, 'sk')
        self.assertEqual(result.payload['variants'][0]['parameters'][0]['descriptions'][0]['name'], 'Farba')
        self.assertEqual(result.payload['variants'][0]['code'], 'NF-1')
        self.assertEqual(result.payload['variants'][0]['ean'], '00123')
        self.assertEqual(result.payload['variants'][0]['parameters'][0]['values'], payload['variants'][0]['parameters'][0]['values'])
        self.assertEqual(ctx['facts'][0]['variant_attributes'][0]['name'], 'FARBA')

    def test_ai_semantic_name_mapping_requires_source_and_rejects_changed_values(self):
        ctx, output = self.fixture('Colour')
        self.assertEqual(validate_content(output, ctx)['errors'], [])
        output.parameters[0].source_name = None
        self.assertIn('ai_unregistered_variant_axis:Colour', validate_content(output, ctx)['errors'])
        output.parameters[0].source_name = 'Colour'
        output.parameters[0].values = ['black']
        self.assertIn('ai_variant_identity_change:Farba', validate_content(output, ctx)['errors'])

    def test_conflicting_source_axes_and_unregistered_targets_are_blocked(self):
        ctx, output = self.fixture('Colour')
        ctx['facts'][0]['variant_attributes'].append({'name': 'Farba', 'value': 'red'})
        self.assertTrue(any(e.startswith('ai_parameter_mapping_conflict:') for e in validate_content(output, ctx)['errors']))
        ctx, output = self.fixture()
        output.parameters[0].source_name = 'VELKOST'
        self.assertIn('ai_parameter_mapping_conflict:Farba', validate_content(output, ctx)['errors'])
        output.parameters[0].name = 'Invented'
        self.assertIn('ai_unregistered_parameter:Invented', validate_content(output, ctx)['errors'])

    def test_staged_mapping_preserves_manual_content_and_repeat_application(self):
        ctx, output = self.fixture()
        item = row(variant_attributes=deepcopy(ctx['facts'][0]['variant_attributes']))
        item['manual_fields'] = ['description_html']
        item['values']['description_html'] = '<p>Manual text</p>'
        item['ai_job_id'] = 'job'
        ctx['staging_automation_version'] = 1
        merge_ai_values(item, output, ctx)
        self.assertEqual(item['values']['variant_attributes'], [{'name': 'Farba', 'value': 'alloverprint'}, {'name': 'Veľkosť', 'value': 'XS'}])
        self.assertEqual(item['values']['description_html'], '<p>Manual text</p>')
        item['ai_policy'] = {'job_id': 'job', 'content_digest': ai_content.digest(output.model_dump())}
        ctx['staging_applied_digest'] = item['ai_policy']['content_digest']
        merge_ai_values(item, output, ctx)
        self.assertEqual(item['values']['code'], item['ai_baseline']['code'])
        self.assertEqual(item['values']['eans'], item['ai_baseline']['eans'])

    def test_new_optional_provenance_preserves_old_content_digests(self):
        value = content(parameters=[{'name': 'Farba', 'product_id': None, 'values': ['red']}])
        self.assertNotIn('source_name', value.model_dump()['parameters'][0])


class NeutralTitleVariantTests(unittest.TestCase):
    def fixture(self):
        ctx = context(supplier='bike-brothers')
        ctx['facts'] = [dict(id=i, name=f'TEST Frame pack ({volume}l)', group_name='TEST Frame pack',
            code=f'TT-F{i}', eans=[f'0012{i}'], variant_attributes=[
                {'name': 'Variant', 'value': f'TEST Frame pack ({volume}l)'}])
            for i, volume in ((1, '1'), (2, '2,4'))]
        ctx['resolved']['category']['parameters'] = [
            ParameterDefinition(name='Objem', scope='choice').model_dump()]
        return ctx, content(parameters=[{'name': 'Objem', 'product_id': None, 'values': ['1 l', '2,4 l']}])

    def test_exact_supplier_choices_pass_and_survive_staging_and_shop_overlay(self):
        ctx, output = self.fixture()
        checks = validate_content(output, ctx)
        self.assertEqual(checks['errors'], [])
        self.assertTrue(checks['automatic_ready'])
        self.assertEqual(checks['parameter_mapping'], {})
        ctx['staging_automation_version'] = 1
        variants = []
        for fact in ctx['facts']:
            item = row(fact['id'], code=fact['code'], eans=fact['eans'], name=fact['name'],
                       variant_attributes=deepcopy(fact['variant_attributes']))
            item['ai_job_id'] = 'job'
            merge_ai_values(item, output, ctx)
            for key in ('code', 'eans', 'variant_attributes', 'sale_gross', 'purchase_net'):
                self.assertEqual(item['values'][key], item['ai_baseline'][key])
            variants.append({'code': fact['code'], 'ean': fact['eans'][0], 'parameters': [
                {'descriptions': [{'language': 'sk', 'name': 'Variant'}], 'values': [
                    {'descriptions': [{'language': 'sk', 'value': fact['name']}]}]}]})
        payload = {'descriptions': [{'language': 'sk'}], 'variants': variants}
        result = overlay(SimpleNamespace(payload=deepcopy(payload)), {
            'active_after_import': True, 'content': output.model_dump(),
            'parameter_mapping': checks['parameter_mapping'], 'registered_parameters': True}, 'sk')
        self.assertEqual(result.payload['variants'], variants)
        self.assertEqual(result.payload['parameters'][0]['descriptions'][0]['name'], 'Objem')
        # A selected subset can contain only one leaf of a real supplier family.
        ctx['facts'] = ctx['facts'][:1]
        self.assertEqual(validate_content(output, ctx)['errors'], [])

    def test_unrelated_or_modified_source_axes_still_require_registration(self):
        for mutation in ('supplier', 'name', 'value', 'family', 'extra_axis', 'axis_name'):
            ctx, output = self.fixture()
            first = ctx['facts'][0]
            if mutation == 'supplier': ctx['supplier'] = 'other'
            elif mutation == 'name': first['name'] = ''
            elif mutation == 'value': first['variant_attributes'][0]['value'] = '1 l'
            elif mutation == 'family': first['group_name'] = None
            elif mutation == 'extra_axis': first['variant_attributes'].append({'name': 'Farba', 'value': 'black'})
            else: first['variant_attributes'][0]['name'] = 'Model'
            with self.subTest(mutation=mutation):
                self.assertTrue(any(e.startswith('ai_unregistered_variant_axis:')
                                    for e in validate_content(output, ctx)['errors']))

    def test_neutral_title_cannot_be_reinterpreted_as_a_technical_axis(self):
        ctx, output = self.fixture()
        output.parameters[0].product_id = 1
        output.parameters[0].source_name = 'Variant'
        output.parameters[0].values = [ctx['facts'][0]['name']]
        checks = validate_content(output, ctx)
        self.assertIn('ai_parameter_mapping_conflict:Objem', checks['errors'])
        self.assertNotIn('Variant', checks['parameter_mapping'])

    def test_registry_and_output_parameter_safety_checks_are_not_exempted(self):
        ctx, output = self.fixture()
        output.parameters[0].name = 'Unknown'
        self.assertIn('ai_unregistered_parameter:Unknown', validate_content(output, ctx)['errors'])
        ctx, output = self.fixture()
        ctx['resolved']['category']['parameters'].append(
            ParameterDefinition(name='Variant', scope='variant', required=True).model_dump())
        self.assertIn('ai_required_parameter:Variant', validate_content(output, ctx)['errors'])
        output.parameters.append(type(output.parameters[0])(name='Variant', product_id=1, values=['changed']))
        self.assertIn('ai_variant_identity_change:Variant', validate_content(output, ctx)['errors'])
        ctx['resolved']['category']['parameters'][-1]['scope'] = 'parent'
        self.assertIn('ai_unregistered_variant_axis:Variant', validate_content(output, ctx)['errors'])
