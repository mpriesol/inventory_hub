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
