"""Application-wide access checks, including endpoints without route guards."""
import unittest
from contextlib import asynccontextmanager
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr

from inventory_hub.routers import auth as routes
from inventory_hub.services import auth
from inventory_hub.settings import settings

TOKEN = 'synthetic-security-boundary-token-123456'
USER = {'id': 7, 'username': 'fixture', 'display_name': 'Fixture', 'role': 'operator', 'active': True}


class BoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        Path(self.directory.name, 'private.json').write_text('{"secret":"not-public"}')
        self.called = []
        self.app = FastAPI(root_path='/api')
        routes.install(self.app)

        @self.app.get('/health')
        def health():
            return {'status': 'ok'}

        @self.app.api_route('/legacy', methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD'])
        def legacy():
            self.called.append('legacy')
            return {'ok': True}

        class SecretInput(BaseModel):
            password: str = Field(min_length=30)

        @self.app.post('/validated')
        def validated(body: SecretInput):
            self.called.append('validated')
            return {'ok': True}

        self.app.mount('/data', StaticFiles(directory=self.directory.name))

        @asynccontextmanager
        async def sessions():
            yield object()

        self.patches = [
            patch.object(settings, 'AI_CONTENT_ACCESS_TOKEN', SecretStr(TOKEN)),
            patch.object(settings, 'USE_POSTGRES', True),
            patch.object(routes, 'get_session_context', sessions),
            patch.object(auth, 'session_user', AsyncMock(side_effect=lambda db, token: USER if token == 'valid-cookie' else None)),
        ]
        for item in self.patches:
            item.start()

    async def asyncTearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.directory.cleanup()

    def client(self, **kwargs):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='https://hub.test', **kwargs)

    async def test_anonymous_requests_never_reach_legacy_routes_or_private_assets(self):
        async with self.client() as client:
            for method in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD'):
                response = await client.request(method, '/legacy', content=b'invalid input')
                self.assertEqual(response.status_code, 401, method)
                self.assertEqual(response.headers['cache-control'], 'no-store')
            for path in ('/data/private.json', '/docs', '/openapi.json', '/auth/users', '/health/private',
                         '/api/legacy', '/api/auth/users', '/api/data/private.json', '/auth/session/extra'):
                self.assertEqual((await client.get(path)).status_code, 401, path)
            # Reject before request-body validation can echo any submitted value.
            rejected = await client.post('/validated', json={'password': 'confidential-input'})
            self.assertEqual(rejected.status_code, 401)
            self.assertNotIn('confidential-input', rejected.text)
        self.assertEqual(self.called, [])

    async def test_public_bootstrap_is_exact_and_does_not_expose_business_routes(self):
        async with self.client() as client:
            self.assertEqual((await client.get('/health')).status_code, 200)
            self.assertEqual((await client.get('/api/health')).status_code, 200)
            self.assertIsNone((await client.get('/auth/session')).json()['user'])
            self.assertEqual((await client.post('/health')).status_code, 401)
            for path in ('/auth/login', '/auth/logout', '/auth/token-session'):
                self.assertEqual((await client.post(path, json={})).status_code, 403 if path != '/auth/login' else 422)
            self.assertEqual((await client.get('/auth/login')).status_code, 401)

    async def test_valid_cookie_can_read_but_all_legacy_writes_require_csrf(self):
        async with self.client(cookies={auth.COOKIE: 'valid-cookie'}) as client:
            self.assertEqual((await client.get('/legacy')).status_code, 200)
            self.assertEqual((await client.get('/api/data/private.json')).status_code, 200)
            for method in ('POST', 'PUT', 'PATCH', 'DELETE'):
                for headers in ({}, {'X-Hub-Request': '1', 'Origin': 'https://attacker.test'},
                                {'X-Hub-Request': '1', 'Sec-Fetch-Site': 'cross-site'}):
                    self.assertEqual((await client.request(method, '/legacy', headers=headers)).status_code, 403)
                response = await client.request(method, '/legacy', headers={
                    'X-Hub-Request': '1', 'Origin': 'https://hub.test', 'Sec-Fetch-Site': 'same-origin'})
                self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.called), 5)

    async def test_bearer_integrations_work_but_invalid_credentials_fail_closed(self):
        async with self.client(headers={'Authorization': 'Bearer ' + TOKEN}) as client:
            self.assertEqual((await client.post('/legacy')).status_code, 200)
            self.assertEqual((await client.get('/docs')).status_code, 200)
            rejected = await client.post('/validated', json={'password': 'confidential-input'})
            self.assertEqual(rejected.status_code, 422)
            self.assertNotIn('confidential-input', rejected.text)
        for headers, cookies in (({'Authorization': 'Bearer wrong'}, {}), ({}, {auth.COOKIE: 'revoked-cookie'})):
            async with self.client(headers=headers, cookies=cookies) as client:
                self.assertEqual((await client.get('/legacy')).status_code, 401)
        with patch.object(settings, 'AI_CONTENT_ACCESS_TOKEN', SecretStr('')):
            async with self.client() as client:
                self.assertEqual((await client.get('/legacy')).status_code, 503)

    async def test_legacy_storage_mode_has_no_anonymous_bypass(self):
        with patch.object(settings, 'USE_POSTGRES', False):
            async with self.client(cookies={auth.COOKIE: 'valid-cookie'}) as client:
                self.assertEqual((await client.get('/legacy')).status_code, 401)
                self.assertEqual((await client.get('/legacy', headers={'Authorization': 'Bearer ' + TOKEN})).status_code, 200)
