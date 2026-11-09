"""Receipt recovery is reachable through the real router and always operator-only."""
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from inventory_hub.database import get_session
from inventory_hub.routers import receiving_db as routes
from inventory_hub.services import receiving_operations as service
from inventory_hub.settings import settings


class ReceivingOperationRouteTests(unittest.TestCase):
    def setUp(self):
        self.sessions = []
        async def session():
            self.sessions.append(True)
            yield SimpleNamespace()
        app = FastAPI()
        app.include_router(routes.router)
        app.dependency_overrides[get_session] = session
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        token = 'receiving-operation-fixture-' + 'x' * 32
        patcher = patch.object(settings, 'AI_CONTENT_ACCESS_TOKEN', SecretStr(token))
        patcher.start(); self.addCleanup(patcher.stop)
        self.headers = {'Authorization': 'Bearer ' + token}
        identifier = '11111111-1111-4111-8111-111111111111'
        base = '/suppliers/test/receiving/sessions/1/operations'
        self.requests = [
            ('POST', base + '/preview', {'request_id': identifier, 'action': 'archive', 'reason': 'Archive test receipt'}, 'preview'),
            ('GET', base + '/' + identifier, None, 'get'),
            ('POST', base + '/' + identifier + '/apply', {'preview_hash': 'a' * 64, 'confirmed': True}, 'apply'),
            ('GET', base, None, 'history'),
        ]

    def test_recovery_is_registered_and_denies_access_before_database(self):
        for method, path, body, name in self.requests:
            with patch.object(service, name, AsyncMock()) as action:
                response = self.client.request(method, path, json=body)
                self.assertEqual(response.status_code, 401, response.text)
                self.assertEqual(response.headers['cache-control'], 'no-store')
                action.assert_not_awaited()
        self.assertEqual(self.sessions, [])

    def test_authorized_recovery_dispatch_and_errors_never_cache(self):
        for method, path, body, name in self.requests:
            with patch.object(service, name, AsyncMock(return_value={'ok': True})) as action:
                response = self.client.request(method, path, json=body, headers=self.headers)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.headers['cache-control'], 'no-store')
                action.assert_awaited_once()
            with patch.object(service, name, AsyncMock(side_effect=service.ReceivingOperationError('receiving_operation_stale'))):
                response = self.client.request(method, path, json=body, headers=self.headers)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json()['detail']['code'], 'receiving_operation_stale')

    def test_reason_confirmation_and_extra_fields_are_strict(self):
        for index, changes in ((0, {'reason': ' '}), (0, {'reason': 'do\nnot'}), (0, {'reason': 123}),
                               (2, {'confirmed': 1}), (2, {'confirmed': 'true'}), (2, {'confirmed': False}),
                               (2, {'arbitrary_status': 'completed'})):
            method, path, body, name = self.requests[index]
            with patch.object(service, name, AsyncMock()) as action:
                response = self.client.request(method, path, json={**body, **changes}, headers=self.headers)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(response.headers['cache-control'], 'no-store')
                action.assert_not_awaited()
