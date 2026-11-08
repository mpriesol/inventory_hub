"""Atomer writer scope across completed reviews, old receipts and changed parents."""
from collections import defaultdict
from copy import deepcopy
from io import BytesIO
import unittest

from inventory_hub.services import atomer_progress as progress
from inventory_hub.services import product_dependency_atomer_scope as atomer
from inventory_hub.services import product_dependency_scope as scope
from inventory_hub.services.product_identity import IdentityIndex, barcode_keys


CODE = 'SYN-UNRELATED'
FAMILY = 's:synthetic-scope'
EAN = '0727670929720'


def fixture(*, changed=True, codes=(CODE,), saved_scope='default', current_scope='default'):
    guards = {'group': [0, 'open', 'default'],
        'rows': {code: [2, 'working', saved_scope] for code in codes},
        'scope': {code: saved_scope for code in codes},
        'own': [['item', code, 2] for code in codes], 'excluded': {}, 'blocked': {}}
    raw = {'campaign': 'f' * 64, 'family_key': FAMILY, 'stage': 'blocked', 'guards': guards,
           'receipt': {}, 'document': None}
    family = {code: {'detail': {'code': code, 'kind': 'standalone', 'family_key': FAMILY,
        'family_name': 'Synthetic name', 'hub_id': None, 'ean': None, 'target_code': None,
        'target_parent': None, 'product_id': None, 'variant_id': None},
        'revision': 3 if changed else 2, 'status': 'blocked' if changed else 'working',
        'scope': current_scope if changed else saved_scope, 'family_guard': [0, 'open', 'default']}
        for code in codes}
    campaign = {'id': raw['campaign'], 'state': 'paused', 'selection': {}, 'checked_run_id': 7,
                'source_sha': 'a' * 64, 'admin_sha': 'b' * 64}
    head = {'id': 7, 'source_sha256': 'a' * 64, 'admin_export_sha256': 'b' * 64}
    return raw, family, campaign, {}, head


def inspect(data, **selected):
    values = dict(hubs={1, 2}, mapping_ids=set(), skus={'syn-keep', 'syn-old'},
        barcodes=barcode_keys([EAN]), aliases=set(), sales=set(), parents=set(), native=set(), native_parents=set())
    values.update(selected)
    proof = scope.Proof(scope.Scope(**values))
    atomer.inspect(proof, *data)
    return proof.result()


def evidence(data, *, code=CODE, pid=100, vid=None, parent='SYN-PRIOR-PARENT', current_parent=None):
    raw, _, _, claims, _ = data
    leaf = {'product_id': str(pid), 'variant_id': str(vid) if vid else None, 'code': code,
            'parent': parent, 'link_id': 70}
    raw['receipt']['cards'] = {code: {'state': 'verified', 'evidence': {
        'status': 'verified', 'card_id': 90, 'hub_sku': code, 'xtrek': leaf, 'biketrek': None}}}
    claims[70] = {'id': 70, 'product_id': 90, 'shop_code': 'xtrek', 'external_product_id': str(pid),
        'external_variant_id': str(vid) if vid else None, 'external_code': code, 'is_variant': vid is not None,
        'parent_code': current_parent or parent, 'retired_at': None}
    return data


class AtomerScopeTests(unittest.TestCase):
    def test_actual_progress_writer_family_name_is_only_a_display_label(self):
        _, _, source = progress.parse_source(BytesIO(b'<PRODUCTS><PRODUCT><PRODUCT_ID>SYN-UNRELATED</PRODUCT_ID>'
            b'<PRODUCT_NAME>SYN-OLD</PRODUCT_NAME></PRODUCT></PRODUCTS>'))
        detail, = progress.reconcile(source, {}, IdentityIndex(2), [], {}, defaultdict(list), {})
        self.assertEqual(detail['family_name'], 'SYN-OLD')
        self.assertNotIn('family_title', detail)
        data = fixture(changed=False)
        data[0]['family_key'] = detail['family_key']
        data[1][CODE]['detail'] = detail
        self.assertEqual(inspect(data)['classification'], 'disjoint')

    def test_finish_review_advance_uses_complete_union_without_terminal_status_exception(self):
        for stage in ('blocked', 'partial', 'pending', 'cards', 'verified', 'excluded'):
            for state in ('paused', 'running', 'done', 'source_changed'):
                with self.subTest(stage=stage, state=state):
                    data = fixture()
                    data[0]['stage'], data[2]['state'] = stage, state
                    result = inspect(data)
                    self.assertEqual(result['classification'], 'context_only')
                    self.assertIn('saved_and_current_atomer_review_scope_union',
                        {item['reason'] for item in result['context_paths']})

    def test_changed_reviews_keep_affected_hub_ids_barcodes_and_old_selections(self):
        for key, value, namespace in [('hub_id', 2, 'hub_product_id'), ('candidates', [2], 'hub_product_id'),
                ('ean', EAN, 'barcode'), ('target_ean', EAN, 'barcode'), ('hub_sku', 'SYN-OLD', 'canonical_sku')]:
            with self.subTest(field=key):
                data = fixture(); data[1][CODE]['detail'][key] = value
                result = inspect(data)
                self.assertEqual(result['classification'], 'reference')
                self.assertIn(namespace, {item['namespace'] for item in result['matched_paths']})
        self.assertEqual(inspect(fixture(codes=('SYN-OLD',)))['classification'], 'reference')

    def test_formerly_excluded_or_blocked_sibling_can_reenter_current_selection(self):
        for key in ('excluded', 'blocked'):
            with self.subTest(key=key):
                data = fixture(codes=(CODE, 'SYN-OLD'))
                data[0]['guards'][key]['SYN-OLD'] = 'Prior operator decision'
                unchanged = deepcopy(data)
                for row in unchanged[1].values():
                    row.update(revision=2, status='working')
                self.assertNotEqual(inspect(unchanged)['classification'], 'reference')
                result = inspect(data)
                self.assertEqual(result['classification'], 'reference')
                self.assertIn('/family/SYN-OLD/code', {item['path'] for item in result['matched_paths']})

    def test_current_and_saved_shop_scopes_both_keep_possible_parent_writes(self):
        for saved, current in [('biketrek', 'xtrek'), ('xtrek', 'biketrek'), ('default', 'biketrek')]:
            with self.subTest(saved=saved, current=current):
                data = fixture(saved_scope=saved, current_scope=current)
                data[1][CODE]['detail'].update(kind='variant', product_id=101, variant_id=102,
                                              target_code=CODE, target_parent='SYN-CURRENT-PARENT')
                result = inspect(data, native_parents={('xtrek', 101)})
                self.assertEqual(result['classification'], 'reference')
                self.assertIn('native_parent_write_id', {item['namespace'] for item in result['matched_paths']})
        data = fixture(saved_scope='biketrek', current_scope='biketrek')
        data[1][CODE]['detail'].update(kind='variant', product_id=101, variant_id=102, target_code=CODE)
        self.assertNotEqual(inspect(data, native_parents={('xtrek', 101)})['classification'], 'reference')

    def test_changed_group_scope_applies_to_default_children_and_checks_all_siblings(self):
        data = fixture(changed=False, codes=(CODE, 'SYN-SIBLING'), saved_scope='default')
        data[0]['guards']['group'] = [1, 'open', 'biketrek']
        data[0]['guards']['scope'] = {key: 'biketrek' for key in data[1]}
        for row in data[1].values():
            row['family_guard'] = [2, 'open', 'xtrek']
        data[1]['SYN-SIBLING']['detail'].update(kind='variant', product_id=101, variant_id=102,
                                                target_code='SYN-SIBLING', target_parent='SYN-CURRENT-PARENT')
        result = inspect(data, parents={('xtrek', 'syn-current-parent')})
        self.assertEqual(result['classification'], 'reference')

    def test_future_content_phase_is_not_limited_to_current_job_stage(self):
        data = fixture(changed=False)
        data[0]['stage'] = 'pending'
        data[1][CODE]['detail'].update(kind='variant', product_id=101, variant_id=102,
                                     target_code=CODE, target_parent='SYN-CURRENT-PARENT')
        self.assertEqual(inspect(data, native_parents={('xtrek', 101)})['classification'], 'reference')

    def test_changed_parent_keeps_frozen_receipt_and_current_mapping_and_report_parents(self):
        data = evidence(fixture(), vid=103, current_parent='SYN-MAPPING-PARENT')
        data[1][CODE]['detail'].update(kind='variant', product_id=201, variant_id=202,
                                      target_code=CODE, target_parent='SYN-CURRENT-PARENT')
        for parent in ('syn-prior-parent', 'syn-mapping-parent', 'syn-current-parent'):
            with self.subTest(parent=parent):
                self.assertEqual(inspect(data, parents={('xtrek', parent)})['classification'], 'reference')
        for pid in (100, 201):
            with self.subTest(pid=pid):
                self.assertEqual(inspect(data, native_parents={('xtrek', pid)})['classification'], 'reference')

    def test_frozen_ack_receipt_and_content_result_ids_cannot_be_erased_by_review_drift(self):
        for container, values in [('', {'product_id': 101, 'variants': {CODE: 102}}),
                ('ack', {'product_id': 101, 'variant_ids': {CODE: 102}, 'inserted': True, 'issues': []}),
                ('content_result', {'product_id': 101, 'variants': {CODE: 102}, 'status': 'partial'})]:
            with self.subTest(container=container):
                data = fixture()
                data[0]['receipt'].update({container: values} if container else values)
                result = inspect(data, native_parents={('xtrek', 101)})
                self.assertEqual(result['classification'], 'reference')
                self.assertIn('native_parent_write_id', {item['namespace'] for item in result['matched_paths']})

    def test_frozen_document_root_and_leaves_survive_review_drift(self):
        data = fixture(saved_scope='biketrek', current_scope='xtrek')
        data[0]['document'] = {'payload': {'code': 'SYN-PRIOR-PARENT', 'variants': [{'code': CODE}]}}
        self.assertEqual(inspect(data, parents={('xtrek', 'syn-prior-parent')})['classification'], 'reference')
        data[0]['document']['payload']['variants'][0]['code'] = 'SYN-OLD'
        self.assertEqual(inspect(data, sales={('xtrek', 'syn-old')})['classification'], 'reference')

    def test_all_current_scope_corrections_include_pinned_owner(self):
        data = fixture()
        data[0]['guards']['excluded'][CODE] = 'Old exclusion'
        data[2]['selection']['barcode_corrections'] = {CODE: {'required_card_id': 2}}
        result = inspect(data)
        self.assertIn('hub_product_id', {item['namespace'] for item in result['matched_paths']})

    def test_all_guard_shapes_are_validated_even_when_one_review_changed(self):
        malformed = [None, {}, [], [2, 'working'], [True, 'working', 'default'], [-1, 'working', 'default'],
                     ['2', 'working', 'default'], [2, 'unknown', 'default'], [2, 'working', 'future-shop']]
        for guard in malformed:
            for target in ('saved_group', 'saved_row', 'current_group'):
                with self.subTest(target=target, guard=guard):
                    data = fixture(codes=(CODE, 'SYN-SIBLING'))
                    if target == 'saved_group':
                        data[0]['guards']['group'] = guard
                    elif target == 'saved_row':
                        data[0]['guards']['rows']['SYN-SIBLING'] = guard
                    else:
                        data[1]['SYN-SIBLING']['family_guard'] = guard
                    with self.assertRaises(scope.Unverified):
                        inspect(data)
        for key, value in [('revision', True), ('revision', -1), ('status', ''), ('scope', 'future-shop')]:
            with self.subTest(current_field=key):
                data = fixture(); data[1][CODE][key] = value
                with self.assertRaises(scope.Unverified):
                    inspect(data)

    def test_inconsistent_group_effective_scope_and_owner_guards_stay_blocked(self):
        mutations = [
            lambda d: d[0]['guards']['scope'].update({CODE: 'biketrek'}),
            lambda d: d[0]['guards']['scope'].update({CODE: None}),
            lambda d: d[0]['guards']['own'].append(['item', CODE, 2]),
            lambda d: d[0]['guards']['own'].append(['item', 'OUTSIDE', 2]),
            lambda d: d[0]['guards']['own'].__setitem__(0, ['item', CODE, 3]),
            lambda d: d[0]['guards']['own'].__setitem__(0, ['family', 'OTHER-FAMILY', 0]),
            lambda d: d[1]['SYN-SIBLING'].update(family_guard=[1, 'open', 'default']),
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutations.index(mutation)):
                data = fixture(codes=(CODE, 'SYN-SIBLING')); mutation(data)
                with self.assertRaises(scope.Unverified):
                    inspect(data)

    def test_exact_current_family_and_frozen_native_membership_remain_required(self):
        mutations = [
            lambda d: d[1].pop(CODE),
            lambda d: d[1].update(OUTSIDE=deepcopy(d[1][CODE])),
            lambda d: d[1][CODE]['detail'].update(family_key='s:other-family'),
            lambda d: d[0]['receipt'].update(product_id=101, variants={'OUTSIDE': 102}),
            lambda d: d[0]['receipt'].update(variants={CODE: 102}),
            lambda d: d[0]['receipt'].update(product_id=101, variants={CODE: None}),
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutations.index(mutation)):
                data = fixture(); mutation(data)
                with self.assertRaises(scope.Unverified):
                    inspect(data)

    def test_unknown_schema_unproven_history_and_pending_intents_never_gain_drift_exemption(self):
        mutations = [
            lambda d: d[0]['guards'].update(future_intent={}),
            lambda d: d[1][CODE]['detail'].update(future_native_id=101),
            lambda d: d[1][CODE]['detail'].update(family_name={'product_id': 2}),
            lambda d: d[0]['receipt'].update(future_intent={}),
            lambda d: d[0]['receipt'].update(ack={'future_native_id': 101}),
            lambda d: d[0]['receipt'].update(content_result={'future_native_id': 101}),
            lambda d: d[0]['receipt'].update(previous_guards=deepcopy(d[0]['guards'])),
            lambda d: d[0]['receipt'].update(previous_receipt={'product_id': 101}),
            lambda d: d[0]['receipt'].update(previous_document={'payload': {'code': 'SYN-OLD'}}),
            lambda d: d[0]['receipt'].update(content_intent={}),
            lambda d: d[0]['receipt'].update(cards={CODE: {'state': 'verified',
                'evidence': {'source_card': {'card_id': 2}}}}),
            lambda d: d[2]['selection'].update(barcode_corrections={CODE: {'future_owner': 2}}),
            lambda d: d[0].update(document={'payload': {'code': CODE}, 'extension_plan': {}}),
            lambda d: d[0].update(stage='future-stage'),
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutations.index(mutation)):
                data = fixture(); mutation(data)
                with self.assertRaises(scope.Unverified):
                    inspect(data)

    def test_current_source_header_must_match_executor_run_and_pinned_source(self):
        for key, value in [('id', 8), ('source_sha256', 'c' * 64), ('admin_export_sha256', 'd' * 64)]:
            with self.subTest(field=key):
                data = fixture(); data[4][key] = value
                with self.assertRaisesRegex(scope.Unverified, 'atomer_current_run_changed'):
                    inspect(data)

    def test_saved_native_owner_lineage_still_has_to_match_after_drift(self):
        data = evidence(fixture())
        data[3][70]['product_id'] = 91
        with self.assertRaisesRegex(scope.Unverified, 'atomer_saved_mapping_lineage_unverified'):
            inspect(data)
        data = fixture(); data[0]['stage'] = 'fidelity'
        with self.assertRaisesRegex(scope.Unverified, 'atomer_pending_content_owner_unverified'):
            inspect(data)


if __name__ == '__main__':
    unittest.main()
