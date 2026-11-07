"""Diagnostic privacy, exact paths, untouched journals and protected GET."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from inventory_hub import config_io
from inventory_hub.database import get_session
from inventory_hub.routers import product_consolidation as routes
from inventory_hub.services import product_consolidation_diagnostics as diagnostics
from inventory_hub.services import catalog_import
from inventory_hub.settings import settings


def variables():
    return {'tokens': ['SHOP-DISPLAY', 'SYN-LEAF'], 'ids': [41], 'id_strings': ['41'],
            'mapping_ids': [51], 'mapping_strings': ['51'], 'native_ids': [61, 62], 'native_strings': ['61', '62']}


class ExplanationTests(unittest.TestCase):
    def test_display_label_and_true_target_are_both_explained_without_any_exemption(self):
        row = {'document': {'shops': [{'code': 'synthetic', 'name': 'SHOP-DISPLAY'}],
                'rows': [{'values': {'code': 'SYN-LEAF', 'ean': '0727670929720',
                    'description_html': 'PRIVATE-CONTENT', 'purchase_net': 'PRIVATE-PRICE'},
                    'hub_product_id': 41}], 'operator_token': 'PRIVATE-CREDENTIAL'}}
        result = diagnostics.explain(row, variables(), 'product_import_drafts')
        paths = {match['path']: match for match in result['matched_paths']}
        self.assertEqual(paths['/document/shops/0/name']['role'], 'shop_display_label')
        self.assertEqual(paths['/document/rows/0/values/code']['role'], 'draft_selected_row_identity')
        self.assertIn('/document/rows/0/hub_product_id', paths)
        self.assertTrue(result['complete'])
        self.assertEqual(result['classification'], 'diagnostic_only_no_guard_exemption')
        self.assertNotIn('PRIVATE-', json.dumps(result))

    def test_supplier_and_native_product_ids_are_not_called_hub_ids(self):
        for kind, expected in [('catalog', 'supplier_catalog_ids_or_staging_source_ids'),
                               ('shop', 'native_shop_product_ids')]:
            row = {'context': {'source_kind': kind, 'product_ids': [41], 'update_preview': {'state': 'uncertain'}}}
            result = diagnostics.explain(row, variables(), 'ai_content_jobs')
            match = next(m for m in result['matched_paths'] if m['path'] == '/context/product_ids/0')
            self.assertEqual(match['namespace'], expected)
            self.assertIn({'path': '/context/update_preview/state', 'value': 'uncertain'}, result['intent_states'])

    def test_atomer_selected_codes_exclude_only_exact_guard_sets_and_preserve_intent(self):
        row = {'guards': {'rows': {'SYN-LEAF': {}, 'EXCLUDED': {}, 'BLOCKED': {}},
                         'excluded': {'EXCLUDED': 'PRIVATE-NOTE'}, 'blocked': {'BLOCKED': 'PRIVATE-NOTE'},
                         'scope': {'SYN-LEAF': 'biketrek'}},
               'document': {'payload': {'variants': [{'code': 'SYN-LEAF', 'native_variant_id': 62}]}},
               'receipt': {'dispatched': True}, 'stage': 'observe'}
        result = diagnostics.explain(row, variables(), 'atomer_catalog_jobs')
        self.assertEqual(result['selected_atomer_leaves'], [{'code': 'SYN-LEAF', 'scope': 'biketrek'}])
        self.assertIn({'path': '/receipt/dispatched', 'value': True}, result['intent_states'])
        self.assertNotIn('PRIVATE-NOTE', json.dumps(result))

    def test_rfc6901_keys_limits_and_strings_only_journal_matching(self):
        row = {'a/b~c': {'code': 'SYN-LEAF'}, 'hub_product_id': 41}
        result = diagnostics.explain(row, variables(), 'catalog_import_journal', strings_only=True)
        self.assertEqual([r['path'] for r in result['matched_paths']], ['/a~1b~0c/code'])
        with patch.object(diagnostics, 'MAX_MATCHES', 1):
            bounded = diagnostics.explain({'code': ['SYN-LEAF', 'SYN-LEAF']}, variables(), 'unknown')
        self.assertFalse(bounded['complete'])
        self.assertIn('match_limit', bounded['limits'])

    def test_malformed_context_and_selection_scope_are_incomplete_without_private_object_echo(self):
        for context in (None, 'unexpected', ['unexpected']):
            result = diagnostics.explain({'context': context, 'code': 'SYN-LEAF'}, variables(), 'ai_content_jobs')
            self.assertFalse(result['complete'])
            self.assertIn('ai_context_shape_unverified', result['limits'])
        row = {'guards': {'rows': {'SYN-LEAF': {}}, 'excluded': {}, 'blocked': {},
                         'scope': {'SYN-LEAF': {'private': 'PRIVATE-SCOPE-OBJECT'}}}}
        result = diagnostics.explain(row, variables(), 'atomer_catalog_jobs')
        self.assertFalse(result['complete'])
        self.assertEqual(result['selected_atomer_leaves'], [{'code': 'SYN-LEAF', 'scope': None, 'scope_unverified': True}])
        self.assertNotIn('PRIVATE-SCOPE-OBJECT', json.dumps(result))
        for guards in (None, {}, {'rows': {}, 'excluded': {}, 'blocked': {}}):
            result = diagnostics.explain({'stage': 'prepared', 'guards': guards,
                'document': {'payload': {'code': 'SYN-LEAF'}}}, variables(), 'atomer_catalog_jobs')
            self.assertFalse(result['complete'])
            self.assertIn('atomer_selection_shape_unverified', result['limits'])
            self.assertIsNone(result['selected_atomer_leaves'])


class JournalReadTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        patcher = patch.object(config_io, 'DATA_ROOT', Path(self.directory.name))
        patcher.start(); self.addCleanup(patcher.stop)
        self.path = config_io.shop_path('biketrek').parent / 'catalog-imports' / ('c' * 32 + '.json')
        self.path.parent.mkdir(parents=True)
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        item = {'code': 'SYN-LEAF', 'product_ids': [999], 'status': 'ready', 'errors': [], 'warnings': [],
                'payload': {'code': 'SYN-LEAF', 'descriptions': [{'long_description': 'PRIVATE-DESCRIPTION'}]}}
        self.doc = {'preview': {'items': [item]}, 'sources': [{'code': 'SYN-LEAF'}],
                    'result': {'status': 'queued', 'updated_at': old, 'items': [item]}}
        self.path.write_text(json.dumps(self.doc))
        self.path.with_suffix('.result.json').write_text(json.dumps(self.doc['result']))
        self.path.with_suffix('.events.jsonl').write_text(json.dumps({'code': 'SYN-LEAF',
            'updated_at': datetime.now(timezone.utc).isoformat(), 'status': 'uncertain',
            'errors': [], 'warnings': [], 'product_write_attempted': True,
            'shop_url': None, 'shop_admin_url': None}) + '\n')

    def test_old_queued_journal_and_uncertain_event_are_read_without_recovery_write(self):
        before = {p.name: p.read_bytes() for p in self.path.parent.iterdir()}
        with patch.object(catalog_import, 'import_result', side_effect=AssertionError('recovery is a mutation')), \
             patch.object(catalog_import, '_write', side_effect=AssertionError('file write forbidden')):
            records, blockers, problems = diagnostics._journals(['biketrek'], variables())
        self.assertEqual(problems, [])
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0]['complete'])
        self.assertTrue(records[0]['pending_by_existing_guard'])
        self.assertTrue(blockers)
        self.assertIn({'path': '/result/items/0/status', 'value': 'uncertain'}, records[0]['current_intent_states'])
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.path.parent.iterdir()})
        self.assertNotIn('PRIVATE-DESCRIPTION', json.dumps(records))

    def test_changed_journal_between_scanner_and_details_is_explicitly_incomplete(self):
        original = catalog_import._load_job
        calls = 0
        def changed(path):
            nonlocal calls
            calls += 1
            result = original(path)
            if calls >= 2:
                result = deepcopy(result)
                result['result']['items'][0]['status'] = 'created'
            return result
        with patch.object(catalog_import, '_load_job', side_effect=changed):
            records, _, problems = diagnostics._journals(['biketrek'], variables())
        self.assertFalse(records[0]['complete'])
        self.assertIn('journal_explanation_incomplete', problems)


class DiagnosticRouteTests(unittest.TestCase):
    def test_operator_only_no_store_and_uuid_validation_without_service_access(self):
        calls = []
        async def session():
            calls.append(True)
            yield SimpleNamespace()
        app = FastAPI()
        app.include_router(routes.router)
        app.dependency_overrides[get_session] = session
        token = 'synthetic-diagnostics-' + 'x' * 24
        with TestClient(app) as client, patch.object(settings, 'AI_CONTENT_ACCESS_TOKEN', SecretStr(token)), \
             patch.object(diagnostics, 'inspect', AsyncMock(return_value={'read_only': True})) as inspect:
            url = '/product-consolidations/' + str(uuid4()) + '/dependency-diagnostics'
            denied = client.get(url)
            self.assertEqual(denied.status_code, 401)
            self.assertEqual(calls, [])
            inspect.assert_not_awaited()
            good = client.get(url, headers={'Authorization': 'Bearer ' + token})
            self.assertEqual(good.status_code, 200)
            self.assertEqual(good.headers['cache-control'], 'no-store')
            inspect.assert_awaited_once()
            bad = client.get('/product-consolidations/not-a-uuid/dependency-diagnostics',
                             headers={'Authorization': 'Bearer ' + token})
            self.assertEqual(bad.status_code, 422)
            self.assertNotIn('not-a-uuid', bad.text)
