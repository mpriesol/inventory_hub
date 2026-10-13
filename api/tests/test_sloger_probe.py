"""The retired paid probe is read-only; ordinary B2B reads never need AI."""
import unittest
from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI
from pydantic import SecretStr

from inventory_hub.ai_content_types import JobAction
from inventory_hub.routers.sloger_probe import router
from inventory_hub.services import ai_content, ai_content_provider, sloger_b2b, sloger_probe as service
from inventory_hub.services.catalog import CatalogError
from inventory_hub.settings import settings


class HistoricalProbeTests(unittest.IsolatedAsyncioTestCase):
    def job(self, **changes):
        return SimpleNamespace(id='a' * 32, kind='supplier_lookup', status='lookup_completed',
            output={'sku': 'TEST-1'}, usage={'input_tokens': 12}, actual_usd=Decimal('0.000293'),
            error=None, context={'model': 'historical-model'}, **changes)

    async def test_historical_result_uses_saved_model_and_never_creates_work(self):
        job = self.job()
        db = SimpleNamespace(get=AsyncMock(return_value=job), add=AsyncMock())

        @asynccontextmanager
        async def sessions():
            yield db

        with patch.object(service, 'get_session_context', sessions), \
                patch.object(ai_content_provider, 'generate', AsyncMock()) as generate:
            result = await service.read(job.id)
        self.assertEqual(result['model'], 'historical-model')
        self.assertEqual(result['actual_usd'], '0.000293')
        self.assertEqual(result['result'], job.output)
        db.add.assert_not_called()
        generate.assert_not_awaited()

    async def test_unrelated_or_missing_jobs_are_not_returned(self):
        db = SimpleNamespace(get=AsyncMock())

        @asynccontextmanager
        async def sessions():
            yield db

        with patch.object(service, 'get_session_context', sessions):
            for job in (None, SimpleNamespace(kind='product')):
                db.get.return_value = job
                with self.assertRaises(CatalogError) as error:
                    await service.read('a' * 32)
                self.assertEqual(error.exception.code, 'sloger_probe_not_found')

    async def test_historical_lookup_cannot_enter_product_ai_workflow(self):
        job = SimpleNamespace(kind='supplier_lookup', revision=1, status='lookup_running')
        for operation in (lambda: ai_content.start(None, job), lambda: ai_content.prepare_import(None, job),
                          lambda: ai_content.action(None, job, JobAction(expected_revision=1, action='cancel'))):
            with self.assertRaises(CatalogError) as error:
                await operation()
            self.assertEqual(error.exception.code, 'ai_job_kind_unsupported')


class ProbeRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_protected_connection_and_retired_post_never_call_ai_or_supplier_login(self):
        app = FastAPI()
        app.include_router(router)
        connection = {'reachable': True, 'login_form_found': True, 'diagnostic': None}
        token = 'fixture-operator-token-123456789'
        headers = {'Authorization': 'Bearer ' + token}
        with patch.object(settings, 'AI_CONTENT_ACCESS_TOKEN', SecretStr(token)), \
                patch.object(sloger_b2b, 'connection', AsyncMock(return_value=connection)) as check, \
                patch.object(sloger_b2b, 'lookup_product', AsyncMock()) as lookup, \
                patch.object(ai_content_provider, 'generate', AsyncMock()) as generate:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://hub.example.test') as client:
                for method, path in (('GET', 'connection'), ('POST', 'preview')):
                    response = await client.request(method, '/suppliers/sloger/lookup/' + path)
                    self.assertEqual(response.status_code, 401)
                    self.assertEqual(response.headers['cache-control'], 'no-store')
                check.assert_not_awaited()
                response = await client.get('/suppliers/sloger/lookup/connection', headers=headers)
                self.assertEqual(response.json(), connection)
                self.assertEqual(response.headers['cache-control'], 'no-store')
                retired = await client.post('/suppliers/sloger/lookup/preview', headers=headers,
                    json={'password': 'fixture-never-echo', 'username': 'fixture-private-user'})
                self.assertEqual(retired.status_code, 410)
                self.assertEqual(retired.json()['detail']['code'], 'sloger_probe_retired')
                self.assertNotIn('fixture-never-echo', retired.text)
                self.assertNotIn('fixture-private-user', retired.text)
                self.assertEqual(retired.headers['cache-control'], 'no-store')
                generate.assert_not_awaited()
                lookup.assert_not_awaited()
