"""Account/session HTTP contracts against an isolated, guarded test database."""
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import Depends, FastAPI
from pydantic import SecretStr
from sqlalchemy import select

import test_catalog_db as catalog_db
from inventory_hub.access import operator_access
from inventory_hub.auth_models import HubSession, HubUser
from inventory_hub.routers import auth as routes
from inventory_hub.services import auth
from inventory_hub.settings import settings


@unittest.skipUnless(catalog_db.TEST_URL, 'Dedicated localhost *_catalog_test database required')
class AccountDatabaseTests(unittest.IsolatedAsyncioTestCase):
    write_feed = catalog_db.CatalogDatabaseTests.write_feed

    async def asyncSetUp(self):
        await catalog_db.CatalogDatabaseTests.asyncSetUp(self)
        async with self.engine.begin() as connection:
            raw = await connection.get_raw_connection()
            sql = (Path(__file__).resolve().parents[2] / 'infra/db-init/020_user_sessions.sql').read_text()
            await raw.driver_connection.execute(sql)
            await raw.driver_connection.execute(sql)  # Safe deploy replay.
        @asynccontextmanager
        async def sessions():
            async with self.sessions() as db:
                yield db
                await db.commit()
        self.patches = [patch.object(routes, 'get_session_context', sessions),
            patch.object(settings, 'AI_CONTENT_ACCESS_TOKEN', SecretStr('synthetic-operator-token-for-tests'))]
        for item in self.patches:
            item.start()
        self.app = FastAPI(root_path='/api')
        routes.install(self.app)
        @self.app.post('/protected', dependencies=[Depends(operator_access)])
        def protected():
            return {'ok': True}
        self.headers = {'X-Hub-Request': '1', 'Origin': 'https://hub.test', 'Sec-Fetch-Site': 'same-origin'}
        self.admin = self.client({'Authorization': 'Bearer synthetic-operator-token-for-tests'})
        self.password = 'synthetic-password-123'

    def client(self, extra=None):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='https://hub.test',
                                headers={**self.headers, **(extra or {})})

    async def asyncTearDown(self):
        await self.admin.aclose()
        for item in reversed(self.patches):
            item.stop()
        await catalog_db.CatalogDatabaseTests.asyncTearDown(self)

    async def create(self, name='test-user', role='operator'):
        response = await self.admin.post('/auth/users', json={'username': name, 'display_name': 'Test user',
            'password': self.password, 'role': role})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn(self.password, response.text)
        return response.json()

    async def test_persistent_login_logout_and_role_boundary(self):
        account = await self.create()
        async with self.client() as client:
            self.assertEqual((await client.post('/protected')).status_code, 401)
            login = await client.post('/auth/login', json={'username': 'TEST-USER', 'password': self.password})
            self.assertEqual(login.status_code, 200, login.text)
            self.assertEqual(login.json()['user']['id'], account['id'])
            self.assertNotIn('password', login.text)
            cookie = client.cookies.get(auth.COOKIE)
            self.assertTrue(cookie)
            self.assertNotIn(cookie, login.text)
            self.assertEqual((await client.post('/protected')).status_code, 200)
            self.assertEqual((await client.get('/auth/users')).status_code, 403)
            self.assertEqual((await client.post('/auth/users', json={'username': 'forbidden', 'display_name': 'X',
                'password': self.password, 'role': 'admin'})).status_code, 403)
            # New browser runtime sends only the cookie; no operator token.
            async with self.client() as refreshed:
                refreshed.cookies.update(client.cookies)
                self.assertEqual((await refreshed.get('/auth/session')).json()['user']['id'], account['id'])
                self.assertEqual((await refreshed.post('/protected')).status_code, 200)
                self.assertEqual((await refreshed.post('/protected', headers={'Origin': 'https://attacker.test'})).status_code, 403)
                self.assertEqual((await refreshed.post('/auth/logout')).status_code, 200)
                self.assertFalse(refreshed.cookies.get(auth.COOKIE))
            # A copied, revoked cookie cannot resume the session.
            self.assertEqual((await client.post('/protected')).status_code, 401)
        async with self.sessions() as db:
            row = await db.get(HubUser, account['id'])
            self.assertNotEqual(row.password_hash, self.password)
            self.assertIsNone(await db.get(HubSession, auth.fingerprint(cookie)))

    async def test_bearer_upgrade_rotation_and_no_secret_validation_output(self):
        response = await self.admin.get('/auth/session')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()['user']['token_login'])
        self.assertIn('HttpOnly', response.headers['set-cookie'])
        async with self.client() as client:
            client.cookies.update(self.admin.cookies)
            self.assertEqual((await client.post('/protected')).status_code, 200)
            with patch.object(settings, 'AI_CONTENT_ACCESS_TOKEN', SecretStr('different-synthetic-operator-token')):
                self.assertIsNone((await client.get('/auth/session')).json()['user'])
                self.assertEqual((await client.post('/protected')).status_code, 401)
        invalid = await self.admin.post('/auth/users', json={'username': 'x', 'display_name': 'X', 'password': 'secret'})
        self.assertEqual(invalid.status_code, 422)
        self.assertNotIn('secret', invalid.text)
        self.assertEqual(invalid.headers['cache-control'], 'no-store')

    async def test_disabled_accounts_revoke_sessions_and_login_limits_persist(self):
        account = await self.create()
        async with self.client() as client:
            self.assertEqual((await client.post('/auth/login', json={'username': 'test-user', 'password': self.password})).status_code, 200)
            self.assertEqual((await self.admin.put('/auth/users/' + str(account['id']), json={'active': False})).status_code, 200)
            self.assertIsNone((await client.get('/auth/session')).json()['user'])
            self.assertEqual((await client.post('/protected')).status_code, 401)
        for _ in range(5):
            async with self.client() as client:
                response = await client.post('/auth/login', json={'username': 'test-user', 'password': self.password})
                self.assertEqual(response.status_code, 401)
        async with self.client() as client:
            response = await client.post('/auth/login', json={'username': 'test-user', 'password': self.password})
            self.assertEqual(response.status_code, 429)

    async def test_administrator_cannot_disable_own_active_account(self):
        account = await self.create(role='admin')
        async with self.client() as client:
            await client.post('/auth/login', json={'username': 'test-user', 'password': self.password})
            response = await client.get('/auth/users')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers['cache-control'], 'no-store')
            self.assertEqual((await client.put('/auth/users/' + str(account['id']), json={'active': False})).status_code, 409)
