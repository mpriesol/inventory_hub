"""Complete operator selection, with no native calls or local mutations."""
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from inventory_hub.database import get_session
from inventory_hub.routers import product_consolidation as routes
from inventory_hub.services import product_consolidation_selection as selection
from inventory_hub.services.product_links import LinkError
from inventory_hub.settings import settings


class SelectionRouteTests(unittest.TestCase):
    def setUp(self):
        self.db_calls = []
        async def session():
            self.db_calls.append(True)
            yield SimpleNamespace()
        app = FastAPI()
        app.include_router(routes.router)
        app.dependency_overrides[get_session] = session
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        token = 'synthetic-selection-' + 'x' * 24
        configured = patch.object(settings, 'AI_CONTENT_ACCESS_TOKEN', SecretStr(token))
        configured.start()
        self.addCleanup(configured.stop)
        self.headers = {'Authorization': 'Bearer ' + token}
        self.url = '/product-consolidations/selection'

    def test_operator_auth_precedes_service_and_static_route_returns_no_store(self):
        with patch.object(selection, 'inspect', AsyncMock(return_value={'products': [], 'mappings': [], 'blockers': []})) as inspect:
            params = [('product_ids', 2), ('product_ids', 1)]
            response = self.client.get(self.url, params=params)
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.headers['cache-control'], 'no-store')
            self.assertEqual(self.db_calls, [])
            inspect.assert_not_awaited()
            response = self.client.get(self.url, params=params, headers=self.headers)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.headers['cache-control'], 'no-store')
            self.assertEqual(inspect.await_args.args[1], [2, 1])

    def test_invalid_query_is_bounded_and_private_values_are_not_echoed(self):
        with patch.object(selection, 'inspect', AsyncMock()) as inspect:
            for values in ([], [1], list(range(1, 12)), [0, 2], [-1, 2], [selection.MAX_ID + 1, 2],
                           ['PRIVATE-NOT-AN-ID', 2]):
                with self.subTest(values=values):
                    response = self.client.get(self.url, params=[('product_ids', value) for value in values], headers=self.headers)
                    self.assertEqual(response.status_code, 422, response.text)
                    self.assertEqual(response.json(), {'detail': {'code': 'consolidation_invalid_request'}})
                    self.assertEqual(response.headers['cache-control'], 'no-store')
                    self.assertNotIn('PRIVATE-NOT-AN-ID', response.text)
                    inspect.assert_not_awaited()

    def test_incomplete_selection_errors_are_no_store_without_partial_response(self):
        for code, status in (('consolidation_product_missing', 404), ('consolidation_selection_mapping_limit', 409)):
            with patch.object(selection, 'inspect', AsyncMock(side_effect=LinkError(code, status))):
                response = self.client.get(self.url, params=[('product_ids', 1), ('product_ids', 2)], headers=self.headers)
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.json(), {'detail': {'code': code}})
                self.assertEqual(response.headers['cache-control'], 'no-store')


class SelectionInputTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_calls_reject_invalid_or_duplicate_ids_before_database(self):
        db = SimpleNamespace(execute=AsyncMock(), scalars=AsyncMock())
        for values in (None, '1,2', [1], [1, 1], [True, 2], ['1', 2], [1.0, 2], [0, 2],
                       [-1, 2], [selection.MAX_ID + 1, 2], list(range(1, 12))):
            with self.subTest(values=values), self.assertRaisesRegex(LinkError, 'consolidation_invalid_request'):
                await selection.inspect(db, values)
            db.execute.assert_not_awaited()
            db.scalars.assert_not_awaited()
