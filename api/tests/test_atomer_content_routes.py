"""Authenticated source uploads cannot become arbitrary product payloads."""
import json
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from pydantic import SecretStr

from inventory_hub.database import get_session
from inventory_hub.routers import atomer_content as routes
from inventory_hub.settings import settings


class AtomerContentRouteTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(routes.router)
        self.db = AsyncMock()
        self.sessions = []
        async def session():
            self.sessions.append(True)
            yield self.db
        self.app.dependency_overrides[get_session] = session
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        token = 'atomer-route-test-only-' + 'x' * 30
        self.headers = {'Authorization': 'Bearer ' + token}
        mocked = patch.object(settings, 'AI_CONTENT_ACCESS_TOKEN', SecretStr(token))
        mocked.start(); self.addCleanup(mocked.stop)

    def item(self, source='A'):
        return {'request_id': str(uuid4()), 'source_code': source, 'hub_sku': source, 'fields': ['seo_title']}

    def upload(self, items, xml=b'<PRODUCTS><PRODUCT><PRODUCT_ID>A</PRODUCT_ID><SEO><SEO_TITLE>New title</SEO_TITLE></SEO></PRODUCT></PRODUCTS>', headers=None):
        return self.client.post('/migration/atomer-content/preview',
            data={'selection': json.dumps({'items': items})},
            files={'source_file': ('buyers.xml', xml, 'application/xml')},
            headers=self.headers if headers is None else headers)

    def test_anonymous_upload_never_parses_or_opens_database(self):
        with patch.object(routes, 'parse_selected') as parse:
            response = self.upload([self.item()], headers={})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertEqual(self.sessions, [])
        parse.assert_not_called()

    def test_cookie_write_requires_same_origin_header(self):
        @self.app.middleware('http')
        async def principal(request: Request, next_handler):
            request.state.hub_user = {'id': 1, 'role': 'operator'}
            return await next_handler(request)
        with patch.object(routes, 'parse_selected') as parse:
            response = self.upload([self.item()], headers={})
        self.assertEqual(response.status_code, 403)
        parse.assert_not_called()

    def test_inventory_or_arbitrary_payload_is_rejected_before_source_read(self):
        for changes in ({'fields': ['stock']}, {'payload': {'stock': 5}}, {'shop_code': 'biketrek'}):
            with self.subTest(changes=changes), patch.object(routes, 'parse_selected') as parse:
                response = self.upload([{**self.item(), **changes}])
            self.assertEqual(response.status_code, 422)
            self.assertEqual(response.json()['detail']['code'], 'atomer_content_invalid_selection')
            parse.assert_not_called()

    def test_truncated_xml_never_creates_a_partial_preview(self):
        with patch.object(routes.service, 'preview', AsyncMock()) as preview:
            response = self.upload([self.item()], xml=b'<PRODUCTS><PRODUCT><PRODUCT_ID>A</PRODUCT_ID></PRODUCT>')
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        preview.assert_not_awaited()

    def test_source_provenance_and_item_errors_are_explicit(self):
        snapshot = {'sha256': 'a' * 64, 'size_bytes': 123, 'total_rows': 2, 'groups': {}}
        with patch.object(routes, 'parse_selected', return_value=snapshot), \
                patch.object(routes.service, 'preview', AsyncMock(side_effect=[
                    {'id': 'saved', 'state': 'ready'}, routes.service.ERROR('atomer_content_family_mapping_required')])) as preview:
            response = self.upload([self.item('A'), self.item('B')])
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result['source']['sha256'], 'a' * 64)
        self.assertEqual(result['items'][0]['publication']['id'], 'saved')
        self.assertEqual(result['items'][1]['error']['code'], 'atomer_content_family_mapping_required')
        self.assertEqual(preview.await_count, 2)
        self.db.rollback.assert_awaited_once()

    def test_history_and_rollback_are_authenticated_and_no_store(self):
        with patch.object(routes.service, 'history', AsyncMock(return_value={'items': []})) as history:
            self.assertEqual(self.client.get('/migration/atomer-content/publications').status_code, 401)
            response = self.client.get('/migration/atomer-content/publications', headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        history.assert_awaited_once()
        with patch.object(routes.service, 'rollback_preview', AsyncMock(return_value={'state': 'ready'})) as rollback:
            response = self.client.post('/migration/atomer-content/publications/' + str(uuid4()) + '/rollback-preview',
                headers=self.headers, json={'request_id': str(uuid4()), 'stock': 10})
        self.assertEqual(response.status_code, 422)
        rollback.assert_not_awaited()
