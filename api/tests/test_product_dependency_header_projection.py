"""Fixed interpreter headers retain full source manifests and correction scope."""
from copy import deepcopy
import unittest

import test_product_dependency_atomer_scope as atomer_fixture
from inventory_hub.services import product_dependency_inventory as inventory
from inventory_hub.services import product_dependency_scope as scope
from inventory_hub.services.product_dependency_reuse import ResolutionMemo


class HeaderProjectionTests(unittest.TestCase):
    def row(self, document):
        raw = inventory.encoded(document)
        return {'key': [document['id']], 'bytes': len(raw),
                'hash': scope.digest(document), 'document': deepcopy(document)}

    def test_barcode_corrections_and_unknown_correction_fields_survive_projection(self):
        data = atomer_fixture.fixture(changed=False)
        data[2]['selection'] = {'barcode_corrections': {atomer_fixture.CODE: {
            'before': atomer_fixture.EAN, 'ean': atomer_fixture.EAN, 'required_card_id': 2}}}
        data[2]['options'] = {'large_display_metadata': 'x' * 100000}
        before = atomer_fixture.inspect(data)
        fields = inventory.header_fields('atomer-campaign-v1', 'atomer_campaign', 'atomer_catalog_campaigns')
        projected = inventory.project_headers([self.row(data[2])], fields)[0]
        smaller = list(deepcopy(data))
        smaller[2] = projected['document']
        self.assertEqual(atomer_fixture.inspect(smaller), before)
        self.assertEqual(before['classification'], 'reference')
        self.assertEqual(smaller[2]['selection'], data[2]['selection'])
        smaller[2]['selection']['barcode_corrections'][atomer_fixture.CODE]['future_owner'] = 99
        with self.assertRaisesRegex(scope.Unverified, 'unknown_schema_field'):
            atomer_fixture.inspect(smaller)

    def test_omitted_header_metadata_still_changes_full_source_manifest(self):
        data = atomer_fixture.fixture()[2]
        data['options'] = {'display': 'first'}
        fields = inventory.header_fields('atomer-campaign-v1', 'atomer_campaign', 'atomer_catalog_campaigns')
        full = self.row(data)
        projected = inventory.project_headers([full], fields)[0]
        original, reduced = inventory.Manifest('header'), inventory.Manifest('header')
        original.row(full); reduced.row(projected)
        self.assertEqual(original.result(), reduced.result())
        data['options']['display'] = 'second'
        changed = inventory.project_headers([self.row(data)], fields)[0]
        self.assertEqual(changed['document'], projected['document'])
        different = inventory.Manifest('header'); different.row(changed)
        self.assertNotEqual(different.result()['hash'], original.result()['hash'])
        self.assertEqual(projected['bytes'], full['bytes'])

    def test_profile_cannot_be_used_for_other_roles_tables_or_partial_headers(self):
        for profile, role, table in (
            ('future-v2', 'atomer_campaign', 'atomer_catalog_campaigns'),
            ('atomer-campaign-v1', 'atomer_family', 'atomer_progress_items'),
            ('atomer-campaign-v1', 'future_role', 'atomer_catalog_campaigns'),
        ):
            with self.subTest(profile=profile, role=role, table=table):
                with self.assertRaisesRegex(scope.Unverified, 'unknown_linked_header_projection'):
                    inventory.header_fields(profile, role, table)
        fields = inventory.header_fields('atomer-campaign-v1', 'atomer_campaign', 'atomer_catalog_campaigns')
        row = self.row(atomer_fixture.fixture()[2])
        del row['document']['selection']
        with self.assertRaisesRegex(scope.Unverified, 'linked_header_projection_incomplete'):
            inventory.project_headers([row], fields)
        row = self.row(atomer_fixture.fixture()[2])
        row['document']['unexpected_cached_field'] = 1
        with self.assertRaisesRegex(scope.Unverified, 'linked_header_projection_incomplete'):
            inventory.project_headers([row], fields, cached=True)

    def test_full_selection_remains_mutation_isolated_in_bounded_memo(self):
        fields = inventory.header_fields('atomer-campaign-v1', 'atomer_campaign', 'atomer_catalog_campaigns')
        data = atomer_fixture.fixture()[2]
        data['selection'] = {'barcode_corrections': {atomer_fixture.CODE: {'required_card_id': 2}}}
        rows = inventory.project_headers([self.row(data)], fields)
        memo = ResolutionMemo(16000000, sort_keys=False)
        key = ('atomer_catalog_campaigns', 't.id=:id', 'params', 'atomer-campaign-v1')
        memo.put(key, rows)
        first = memo.get(key)
        first[0]['document']['selection']['barcode_corrections'][atomer_fixture.CODE]['required_card_id'] = 999
        self.assertEqual(memo.get(key), rows)
        self.assertIsNone(memo.get(key[:-1]))

    def test_current_run_binding_keeps_all_three_inputs(self):
        data = list(atomer_fixture.fixture())
        data[4]['summary'] = {'display': 'x' * 100000}
        fields = inventory.header_fields('atomer-current-run-v1', 'atomer_current_run', 'atomer_progress_runs')
        before = atomer_fixture.inspect(data)
        data[4] = inventory.project_headers([self.row(data[4])], fields)[0]['document']
        self.assertEqual(atomer_fixture.inspect(data), before)
        data[4]['source_sha256'] = 'c' * 64
        with self.assertRaisesRegex(scope.Unverified, 'atomer_current_run_changed'):
            atomer_fixture.inspect(data)
