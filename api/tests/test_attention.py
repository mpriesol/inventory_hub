from unittest import TestCase
from unittest.mock import AsyncMock, Mock, patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from inventory_hub.attention_types import AttentionEvent, AttentionRuleInput, SupplierDetails
from inventory_hub.routers.attention import router
from inventory_hub.services import attention
from inventory_hub.settings import settings


class AttentionContractTests(TestCase):
    def test_legacy_partial_b2b_result_cannot_resolve_a_partial_incident(self):
        import asyncio
        from inventory_hub.services import supplier_attention
        db = Mock()
        row = {
            "code": "sloger", "id": 12, "status": "completed", "error_message": "supplier_availability_partial",
            "error_details": {"succeeded": 1, "failed": 2}, "items_fetched": 3, "items_updated": 1, "items_error": 2}
        db.execute = AsyncMock(return_value=Mock(mappings=lambda: Mock(all=lambda: [row])))
        with patch.object(attention, "report", AsyncMock()) as report:
            asyncio.run(supplier_attention.reconcile(db))
        partial = report.await_args_list[-1].args[1]
        self.assertEqual(partial.kind, "supplier_availability.partial")
        self.assertTrue(partial.active)
        self.assertEqual(partial.details["rejected"], 2)
        self.assertEqual(partial.details["accepted"], 1)

    def test_payloads_are_bounded_and_never_accept_raw_payloads_or_urls(self):
        for data in ({'run_id': 1, 'password': 'private'}, {'run_id': 1, 'error_code': 'https://private/?token=x'},
                     {'run_id': 1, 'accepted': -1}, {'run_id': 1, 'accepted': True},
                     {'run_id': 1, 'warnings': ['x'] * 31}):
            with self.assertRaises(ValidationError):
                SupplierDetails.model_validate(data)
        for path in ('https://example.test/', '//example.test', '/api/secret', '/invoices\\evil', '/invoices\n'):
            with self.assertRaises(attention.AttentionError):
                attention._link(path)
        self.assertEqual(attention.supplier_link('husky'), '/settings/availability?supplier=husky')
        self.assertEqual(attention.supplier_log('husky', {'run_id': 5}), '/settings/logs?supplier=husky&run_id=5')
        for value in (0, 101, True, '2'):
            with self.assertRaises(ValidationError):
                AttentionRuleInput(expected_revision=0, enabled=True, min_occurrences=value)

    def test_authentication_and_no_store_apply_to_reads_events_and_mutations(self):
        app = FastAPI(); app.include_router(router)
        with patch.object(settings, 'AI_CONTENT_ACCESS_TOKEN', SecretStr('a' * 32)):
            with TestClient(app) as client:
                for method, url, data in [('GET', '/attention', None), ('GET', '/attention/rules', None),
                    ('POST', '/attention/events', {}), ('POST', '/attention/1/action', {}),
                    ('PUT', '/attention/rules/supplier_availability.partial', {})]:
                    response = client.request(method, url, json=data)
                    self.assertEqual(response.status_code, 401)
                    self.assertEqual(response.headers['cache-control'], 'no-store')
                response = client.post('/attention/events', headers={'Authorization': 'Bearer ' + 'a' * 32},
                    json={'kind': 'x', 'scope': 'x', 'sequence': 1, 'active': True, 'url': 'secret'})
                self.assertEqual(response.status_code, 422)
                self.assertNotIn('secret', response.text)

    def test_notification_failure_does_not_fail_business_processing(self):
        import asyncio
        from inventory_hub.services import supplier_availability_worker as worker
        with patch.object(worker, 'get_session_context', side_effect=RuntimeError('private-connection')), \
             patch.object(worker.operation_log, 'emit') as emit:
            asyncio.run(worker.reconcile_attention())
        self.assertEqual(emit.call_args.args, ('attention_reconcile_failed',))
        self.assertNotIn('private', str(emit.call_args))
