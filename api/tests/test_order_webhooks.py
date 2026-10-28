"""Notification authentication, bounded ingress and no blind registration retries."""
import base64
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from inventory_hub.order_webhook_types import WebhookConfigure
from inventory_hub.routers import order_webhooks as routes
from inventory_hub.routers.auth import install
from inventory_hub.services import order_webhooks as service, order_webhook_credentials as credentials
from inventory_hub.services.order_collection_source import CollectionSourceError
from inventory_hub.settings import settings

NOW = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
TOKEN = 't' * 64
AUTH = 'Basic ' + base64.b64encode(('biketrek:' + TOKEN).encode()).decode()


def payload(number='1', event='update', stamp=None):
    key = {'create': 'creation_time', 'update': 'last_update_time', 'delete': 'deletion_time'}[event]
    return {'project_name': 'not-an-authentication-credential',
            'orders': [{'order_number': number, key: stamp or NOW.isoformat(), 'status_id': 1}]}


class NotificationTests(unittest.TestCase):
    def test_basic_requires_dedicated_ascii_secret_and_scoped_shop(self):
        self.assertEqual(service.basic_credentials(AUTH), ('biketrek', TOKEN))
        for invalid in (None, '', 'Bearer ' + TOKEN, 'Basic !!!', 'Basic ' + base64.b64encode(b'biketrek:short').decode(),
                        'Basic ' + base64.b64encode(('../secret:' + TOKEN).encode()).decode()):
            with self.subTest(value=invalid), self.assertRaises(service.WebhookError) as raised:
                service.basic_credentials(invalid)
            self.assertEqual(raised.exception.status, 401)

    def test_hints_keep_no_stock_customer_status_or_project_facts(self):
        value = payload()
        value['orders'][0].update({'stock': -99, 'quantity': 12, 'email': 'private@example.test'})
        with patch.object(service, 'now', return_value=NOW):
            rows = service.notifications(value, 'update')
        self.assertEqual(set(rows[0]), {'order_number', 'event', 'notification_at', 'notification_hash'})
        self.assertNotIn('private', json.dumps(rows, default=str))

    def test_malformed_batch_is_rejected_whole_and_duplicate_numbers_coalesce(self):
        with patch.object(service, 'now', return_value=NOW):
            for value in ({}, {'orders': []}, {'orders': [{}]}, payload('A;B'), payload(' A'),
                          payload(stamp='bad'), payload(stamp=(NOW + timedelta(days=1)).isoformat()),
                          {'orders': payload()['orders'] * 5001}):
                with self.subTest(value=str(value)[:60]), self.assertRaises(service.WebhookError):
                    service.notifications(value, 'update')
            repeated = payload()
            repeated['orders'] += payload(stamp=(NOW - timedelta(minutes=1)).isoformat())['orders']
            rows = service.notifications(repeated, 'update')
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['notification_at'], NOW)

    def test_config_rejects_non_https_credential_or_ambiguous_urls(self):
        valid = {'shop_code': 'biketrek', 'confirmed': True, 'enabled': True,
                 'public_base_url': 'https://hub.example.test/api', 'catchup_interval_seconds': 3600}
        WebhookConfigure(**valid)
        for url in ('http://hub.example.test/api', 'https://user:pass@hub.example.test/api',
                    'https://hub.example.test/api?token=secret', 'https://hub.example.test/a/../api',
                    'https://hub.example.test/api\n'):
            with self.subTest(url=url), self.assertRaises(ValidationError):
                WebhookConfigure(**{**valid, 'public_base_url': url})
        for seconds in (True, 299, 86401, '3600'):
            with self.subTest(seconds=seconds), self.assertRaises(ValidationError):
                WebhookConfigure(**{**valid, 'catchup_interval_seconds': seconds})


class IngressTests(unittest.TestCase):
    def setUp(self):
        self.db = object()
        self.committed = False
        self.fail_commit = False
        @asynccontextmanager
        async def context():
            yield self.db
            if self.fail_commit:
                raise RuntimeError('private database value')
            self.committed = True
        self.patches = [patch.object(routes, 'get_session_context', context),
                        patch.object(settings, 'USE_POSTGRES', False),
                        patch.object(settings, 'AI_CONTENT_ACCESS_TOKEN', SecretStr('operator-' + 'x' * 40)),
                        patch.object(service, 'now', return_value=NOW)]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        app = FastAPI()
        install(app)
        app.include_router(routes.router)
        app.include_router(routes.ingress_router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_only_three_exact_post_routes_bypass_operator_token_and_keep_dedicated_auth(self):
        with patch.object(service, 'authenticate', AsyncMock(side_effect=service.WebhookError('order_webhook_access_required', 401))) as authenticate:
            for event in service.EVENTS:
                response = self.client.post('/webhooks/upgates/orders/' + event, json=payload(event=event))
                self.assertEqual(response.status_code, 401)
            self.assertEqual(authenticate.await_count, 3)
            for method, url in [('GET', '/webhooks/upgates/orders/create'), ('POST', '/webhooks/upgates/orders/create/extra'),
                                ('POST', '/order-webhooks/configure'), ('GET', '/order-webhooks/status?shop_code=biketrek')]:
                response = self.client.request(method, url)
                self.assertEqual(response.status_code, 401, response.text)
            self.assertEqual(authenticate.await_count, 3)

    def test_ack_follows_commit_and_does_not_wait_for_remote_reads(self):
        with patch.object(service, 'authenticate', AsyncMock(return_value=(SimpleNamespace(id=1), object()))), \
             patch.object(service, 'accept', AsyncMock()) as accept, \
             patch.object(service, 'fetch_current', AsyncMock()) as upstream:
            response = self.client.post('/webhooks/upgates/orders/update', json=payload(), headers={'Authorization': AUTH})
            self.assertEqual(response.status_code, 204, response.text)
            self.assertTrue(self.committed)
            self.assertEqual(response.headers['cache-control'], 'no-store')
            accept.assert_awaited_once()
            upstream.assert_not_awaited()
            self.fail_commit = True
            response = self.client.post('/webhooks/upgates/orders/update', json=payload(), headers={'Authorization': AUTH})
            self.assertEqual(response.status_code, 503)
            self.assertNotIn('private database', response.text)

    def test_body_limit_and_invalid_payload_never_reach_queue(self):
        with patch.object(service, 'authenticate', AsyncMock(return_value=(SimpleNamespace(id=1), object()))), \
             patch.object(service, 'accept', AsyncMock()) as accept:
            response = self.client.post('/webhooks/upgates/orders/update', content=b' ' * (service.MAX_BODY + 1),
                                       headers={'Authorization': AUTH, 'Content-Type': 'application/json'})
            self.assertEqual(response.status_code, 413)
            response = self.client.post('/webhooks/upgates/orders/update', json={'orders': []}, headers={'Authorization': AUTH})
            self.assertEqual(response.status_code, 422)
            accept.assert_not_awaited()


class CredentialsTests(unittest.TestCase):
    def test_generated_secret_is_private_versioned_and_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(settings, 'SUPPLIER_CREDENTIALS_ROOT', root / 'private'), \
                 patch.object(settings, 'INVENTORY_DATA_ROOT', root / 'public'):
                digest = credentials.create(12)
                token = credentials.load(12, digest)
                self.assertEqual(len(token), 64)
                self.assertEqual(credentials.digest(token), digest)
                path = root / 'private' / credentials._filename(12, digest)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                old = path.read_text()
                second = credentials.create(12)
                self.assertNotEqual(second, digest)
                self.assertEqual(path.read_text(), old)
                path.chmod(0o644)
                with self.assertRaises(ValueError):
                    credentials.load(12, digest)

    def test_private_mount_cannot_be_inside_served_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(settings, 'SUPPLIER_CREDENTIALS_ROOT', root / 'private'), \
                 patch.object(settings, 'INVENTORY_DATA_ROOT', root):
                with self.assertRaises(ValueError):
                    credentials.create(1)


class RegistrationTests(unittest.TestCase):
    def plan(self):
        return {'shop_id': 1, 'shop_code': 'biketrek', 'secret_hash': 'a' * 64,
                'public_base_url': 'https://hub.example.test/api', 'target_fingerprint': 'b' * 64, 'enabled': True}

    def test_retry_lists_existing_registrations_without_duplicate_posts(self):
        stored, calls = [], []
        def request(client, method, body=None):
            calls.append(method)
            if method == 'GET':
                return {'webhooks': list(stored)}
            row = {**body, 'id': len(stored) + 1}
            stored.append(row)
            return {'webhook': row}
        with patch.object(credentials, 'load', return_value=TOKEN), patch.object(service, '_client', return_value=Mock()), \
             patch.object(service, '_request', side_effect=request):
            first = service.reconcile_registration(self.plan(), lambda event: None)
            second = service.reconcile_registration(self.plan(), lambda event: None)
        self.assertEqual(first, {'create': 1, 'update': 2, 'delete': 3})
        self.assertEqual(second, first)
        self.assertEqual(calls, ['GET', 'POST', 'POST', 'POST', 'GET'])
        self.assertEqual(stored[0]['url'], 'https://biketrek:' + TOKEN + '@hub.example.test/api/webhooks/upgates/orders/create')

    def test_ambiguous_remote_names_do_not_write_or_delete(self):
        import hashlib
        name = 'inventory-hub-' + hashlib.sha256(('a' * 64).encode()).hexdigest()[:12] + '-biketrek-create'
        with patch.object(credentials, 'load', return_value=TOKEN), patch.object(service, '_client', return_value=Mock()), \
             patch.object(service, '_request', return_value={'webhooks': [{'id': 1, 'name': name}, {'id': 2, 'name': name}]}) as request:
            with self.assertRaises(service.WebhookError) as raised:
                service.reconcile_registration(self.plan(), lambda event: None)
        self.assertEqual(raised.exception.code, 'order_webhook_registration_ambiguous')
        self.assertEqual(request.call_count, 1)


    def test_uncertain_create_absence_never_repeats_post(self):
        plan = {**self.plan(), 'registration_intents': {'create': {'started_at': NOW.isoformat()}}}
        with patch.object(credentials, 'load', return_value=TOKEN), patch.object(service, '_client', return_value=Mock()), \
             patch.object(service, '_request', return_value={'webhooks': []}) as request:
            before = Mock()
            with self.assertRaises(service.WebhookError) as raised:
                service.reconcile_registration(plan, before)
        self.assertEqual(raised.exception.code, 'order_webhook_registration_uncertain')
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args[1], 'GET')
        before.assert_not_called()

    def test_create_intent_is_durable_before_first_network_write(self):
        order = []
        def request(client, method, body=None):
            order.append(method)
            if method == 'GET':
                return {'webhooks': []}
            self.assertEqual(order[-2], 'persist-' + body['event'].split('.')[-1])
            return {'webhook': {**body, 'id': len(order)}}
        with patch.object(credentials, 'load', return_value=TOKEN), patch.object(service, '_client', return_value=Mock()), \
             patch.object(service, '_request', side_effect=request):
            service.reconcile_registration(self.plan(), lambda event: order.append('persist-' + event))
        self.assertEqual(order, ['GET', 'persist-create', 'POST', 'persist-update', 'POST', 'persist-delete', 'POST'])


class TargetedReadTests(unittest.TestCase):
    def test_only_notified_numbers_are_read_and_missing_membership_uses_deleted_query(self):
        from uuid import uuid4
        raw = lambda number: {'order_number': number, 'uuid': str(uuid4()),
            'creation_time': (NOW - timedelta(days=1)).isoformat(), 'last_update_time': NOW.isoformat(),
            'status_id': 1, 'origin': 'frontend'}
        replies = [{'orders': [raw('A')], 'current_page': 1, 'current_page_items': 1,
                    'number_of_pages': 1, 'number_of_items': 1},
                   {'orders': [raw('B')], 'current_page': 1, 'current_page_items': 1,
                    'number_of_pages': 1, 'number_of_items': 1}]
        plan = {'shop_code': 'biketrek', 'target_fingerprint': 'f' * 64,
                'events': [{'order_number': 'A'}, {'order_number': 'B'}]}
        with patch.object(service, '_fetch_page', side_effect=replies) as fetch, patch.object(service, 'now', return_value=NOW):
            result = service._fetch_current(plan)
        self.assertEqual(set(result), {'A', 'B'})
        self.assertFalse(result['A']['deleted'])
        self.assertTrue(result['B']['deleted'])
        self.assertEqual(fetch.call_args_list[0].args[1], {'order_numbers': 'A;B', 'page': 1})
        self.assertEqual(fetch.call_args_list[1].args[1], {'order_numbers': 'B', 'page': 1, 'deleted_yn': 'true'})

    def test_response_for_unrequested_order_is_rejected(self):
        from uuid import uuid4
        reply = {'orders': [{'order_number': 'WRONG', 'uuid': str(uuid4()),
            'creation_time': (NOW - timedelta(days=1)).isoformat(), 'last_update_time': NOW.isoformat(),
            'status_id': 1, 'origin': 'frontend'}], 'current_page': 1, 'current_page_items': 1,
            'number_of_pages': 1, 'number_of_items': 1}
        plan = {'shop_code': 'biketrek', 'target_fingerprint': 'f' * 64, 'events': [{'order_number': 'A'}]}
        with patch.object(service, '_fetch_page', return_value=reply), patch.object(service, 'now', return_value=NOW):
            with self.assertRaises(CollectionSourceError):
                service._fetch_current(plan)


class RegistrationCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancellation_waits_for_network_thread_before_releasing_worker_scope(self):
        import asyncio
        import threading
        from inventory_hub.services import order_webhook_worker as worker
        started, release = threading.Event(), threading.Event()
        def write(plan, before_create):
            started.set()
            release.wait(timeout=2)
            return {'create': 1, 'update': 2, 'delete': 3}
        with patch.object(service, 'reconcile_registration', side_effect=write), \
             patch.object(service, 'finish_registration', AsyncMock()) as finish:
            task = asyncio.create_task(worker.register({'shop_id': 1}))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 1))
                task.cancel()
                await asyncio.sleep(0.02)
                self.assertFalse(task.done())
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                finish.assert_not_awaited()
            finally:
                release.set()
                if not task.done():
                    await asyncio.gather(task, return_exceptions=True)
