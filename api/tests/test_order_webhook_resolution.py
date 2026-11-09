"""Absent-delete recovery must never turn a failed source read into evidence."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4
import unittest

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from inventory_hub.database import get_session
from inventory_hub.order_webhook_types import NotificationResolutionApply, NotificationResolutionPreview
from inventory_hub.routers import order_webhooks as routes
from inventory_hub.services import order_webhooks, order_webhook_resolution as service
from inventory_hub.services.order_collection_source import CollectionSourceError
from inventory_hub.settings import settings


NOW = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)


def body(**changes):
    return {'request_id': str(uuid4()), 'shop_code': 'biketrek', 'order_number': 'MISSING',
            'action': service.ACKNOWLEDGE, 'note': 'Verified missing notification without local history.', **changes}


class ResolutionInputTests(unittest.TestCase):
    def test_exact_identifier_note_uuid_and_real_confirmation(self):
        self.assertEqual(NotificationResolutionPreview(**body(note='  Sufficient evidence.  ')).note, 'Sufficient evidence.')
        for changes in ({'note': '   '}, {'note': 'short'}, {'order_number': 'A;B'}, {'order_number': ' A'},
                        {'order_number': 'A\n'}, {'order_number': '\ud800'}, {'action': 'ignore'}, {'stock': 0}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                NotificationResolutionPreview(**body(**changes))
        for confirmed in (False, 'true', 1, None):
            with self.subTest(confirmed=confirmed), self.assertRaises(ValidationError):
                NotificationResolutionApply(shop_code='biketrek', preview_hash='a' * 64, confirmed=confirmed)


class ResolutionSourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_proof_requires_both_exact_successful_queries(self):
        empty = {'orders': [], 'current_page': 1, 'current_page_items': 0, 'number_of_pages': 0, 'number_of_items': 0}
        with patch.object(order_webhooks, '_fetch_page', side_effect=[empty, empty]) as read:
            result = await service._absence('biketrek', 'MISSING', {'target_fingerprint': 'a' * 64})
        self.assertTrue(all(result[key] for key in ('active_absent', 'deleted_absent', 'no_local_history')))
        self.assertEqual(read.call_count, 2)
        self.assertEqual(read.call_args_list[0].args, ('biketrek', {'order_numbers': 'MISSING', 'page': 1}, 'a' * 64))
        self.assertEqual(read.call_args_list[1].args, ('biketrek', {'order_numbers': 'MISSING', 'page': 1, 'deleted_yn': 'true'}, 'a' * 64))

    async def test_failure_or_found_order_never_counts_as_absence(self):
        for error in (CollectionSourceError('order_collection_rate_limited', 429),
                      CollectionSourceError('order_collection_invalid_response'), RuntimeError('private upstream body')):
            with self.subTest(error=type(error).__name__), patch.object(order_webhooks, 'fetch_current', AsyncMock(side_effect=error)):
                with self.assertRaises((order_webhooks.WebhookError, CollectionSourceError)) as raised:
                    await service._absence('biketrek', 'MISSING', {'target_fingerprint': 'a' * 64})
                self.assertNotIn('private', str(raised.exception))
        for result in ({'MISSING': {'deleted': False}}, {'MISSING': {'deleted': True}}, []):
            with self.subTest(result=result), patch.object(order_webhooks, 'fetch_current', AsyncMock(return_value=result)):
                with self.assertRaises(order_webhooks.WebhookError):
                    await service._absence('biketrek', 'MISSING', {'target_fingerprint': 'a' * 64})


class ResolutionRouteTests(unittest.TestCase):
    def setUp(self):
        self.token = 'resolution-test-' + 't' * 30
        self.patch = patch.object(settings, 'AI_CONTENT_ACCESS_TOKEN', SecretStr(self.token))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.db = object()
        async def db():
            yield self.db
        app = FastAPI()
        @app.middleware('http')
        async def principal(request: Request, call_next):
            if request.headers.get('x-test-role'):
                request.state.hub_user = {'id': 17, 'role': request.headers['x-test-role']}
                request.state.hub_bearer = True
            return await call_next(request)
        app.dependency_overrides[get_session] = db
        app.include_router(routes.router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_admin_actor_validation_and_no_store_routes(self):
        request = body()
        path = '/order-webhooks/notifications/preview'
        with patch.object(service, 'preview', AsyncMock(return_value={'safe': True})) as preview:
            self.assertEqual(self.client.post(path, json=request).status_code, 401)
            self.assertEqual(self.client.post(path, json=request, headers={'x-test-role': 'operator'}).status_code, 403)
            response = self.client.post(path, json=request, headers={'x-test-role': 'admin'})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers['cache-control'], 'no-store')
            self.assertEqual(preview.await_args.args[2], 'user:17')
            response = self.client.post(path, json={**request, 'actor': 'forged'}, headers={'x-test-role': 'admin'})
            self.assertEqual(response.status_code, 422)
        with patch.object(service, 'apply', AsyncMock(side_effect=service._error('stale'))) as apply:
            response = self.client.post(f"/order-webhooks/notifications/operations/{request['request_id']}/apply",
                json={'shop_code': 'biketrek', 'preview_hash': 'a' * 64, 'confirmed': True},
                headers={'Authorization': 'Bearer ' + self.token})
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.headers['cache-control'], 'no-store')
            self.assertEqual(apply.await_args.args[3], 'operator-token')
        with patch.object(service, 'get_operation', AsyncMock(return_value={'safe': True})), \
             patch.object(service, 'history', AsyncMock(return_value={'operations': []})):
            for url in (f"/order-webhooks/notifications/operations/{request['request_id']}?shop_code=biketrek",
                        '/order-webhooks/notifications/history?shop_code=biketrek&order_number=MISSING'):
                response = self.client.get(url, headers={'x-test-role': 'admin'})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers['cache-control'], 'no-store')
