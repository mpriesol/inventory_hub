"""Retirement rejects fresh work and keeps read-only successful UUID recovery."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from inventory_hub.services import zookee_code_reconciliation as service


class RetiredCodeReconciliationTests(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_request_returns_410_without_committing_or_remote_work(self):
        db = AsyncMock()
        db.get.return_value = None
        with self.assertRaises(service.editor.EditorError) as failure:
            await service.reconcile_one(db, 'zookee', 7, uuid4())
        self.assertEqual((failure.exception.status, failure.exception.code), (410, 'code_rename_retired'))
        db.commit.assert_not_awaited()
        db.execute.assert_not_awaited()

    async def test_saved_uuid_replays_original_result_and_wrong_product_is_rejected(self):
        result = {'request_id': str(uuid4()), 'product_id': 7, 'status': 'completed'}
        db = AsyncMock()
        db.get.return_value = SimpleNamespace(input_hash=service._hash(7), result=result)
        self.assertEqual(await service.read_result(db, 'zookee', result['request_id']), result)
        self.assertEqual(await service.reconcile_one(db, 'zookee', 7, result['request_id']), result)
        with self.assertRaisesRegex(service.editor.EditorError, 'product_editor_request_reused'):
            await service.reconcile_one(db, 'zookee', 8, result['request_id'])
        db.commit.assert_not_awaited()

    async def test_read_unknown_uuid_returns_missing_not_an_instruction_to_restart(self):
        db = AsyncMock()
        db.get.return_value = None
        with self.assertRaises(service.editor.EditorError) as failure:
            await service.read_result(db, 'zookee', uuid4())
        self.assertEqual((failure.exception.status, failure.exception.code), (404, 'code_rename_result_missing'))
        db.commit.assert_not_awaited()
