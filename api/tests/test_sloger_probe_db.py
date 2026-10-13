"""Historical lookup records remain readable after retiring the paid workflow."""
import unittest
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import func, select

import test_catalog_db as catalog_db
from inventory_hub.ai_content_models import AiBatch, AiJob
from inventory_hub.services import sloger_probe as service


@unittest.skipUnless(catalog_db.TEST_URL, 'Dedicated localhost *_catalog_test database required')
class HistoricalProbeDatabaseTests(unittest.IsolatedAsyncioTestCase):
    write_feed = catalog_db.CatalogDatabaseTests.write_feed

    async def asyncSetUp(self):
        await catalog_db.CatalogDatabaseTests.asyncSetUp(self)
        async with self.engine.begin() as connection:
            raw = await connection.get_raw_connection()
            await raw.driver_connection.execute(
                (Path(__file__).resolve().parents[2] / 'infra/db-init/005_ai_content.sql').read_text())

    async def asyncTearDown(self):
        await catalog_db.CatalogDatabaseTests.asyncTearDown(self)

    async def test_history_reads_preserve_result_model_cost_and_row_count(self):
        identifier = 'a' * 32
        async with self.sessions() as db:
            db.add(AiBatch(id=identifier, request_hash='b' * 64))
            await db.flush()
            db.add(AiJob(id=identifier, batch_id=identifier, kind='supplier_lookup', status='lookup_completed',
                context={'model': 'saved-test-model'}, output={'sku': 'BT-001'}, actual_usd=Decimal('0.000293')))
            await db.commit()

        @asynccontextmanager
        async def sessions():
            async with self.sessions() as db:
                yield db

        with patch.object(service, 'get_session_context', sessions):
            result = await service.read(identifier)
            self.assertEqual(await service.read(identifier), result)
        self.assertEqual(result['model'], 'saved-test-model')
        self.assertEqual(result['result'], {'sku': 'BT-001'})
        self.assertEqual(Decimal(result['actual_usd']), Decimal('0.000293'))
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(AiBatch)), 1)
            self.assertEqual(await db.scalar(select(func.count()).select_from(AiJob)), 1)
