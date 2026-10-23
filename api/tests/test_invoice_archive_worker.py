"""Automatic local recovery retains work until a database commit succeeds."""
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

from inventory_hub.services import invoice_archive as archive
from inventory_hub.services import invoice_archive_worker as worker


class InvoiceArchiveWorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = self.enterContext(TemporaryDirectory())
        self.root = Path(temporary)
        self.enterContext(patch.object(worker.settings, 'INVENTORY_DATA_ROOT', self.root))
        self.enterContext(patch.object(worker.settings, 'USE_POSTGRES', True))
        self.folder = self.root / 'suppliers/fixture/invoices'
        self.folder.mkdir(parents=True)
        self.index = self.folder / 'index.latest.json'
        self.index.write_text('{}')
        self.committed = 0
        self.fail_commit = False

        @asynccontextmanager
        async def session():
            yield object()
            if self.fail_commit:
                raise RuntimeError('Synthetic unavailable database')
            self.committed += 1
        self.enterContext(patch.object(worker, 'get_session_context', session))

    async def test_cursor_is_saved_after_commit_and_all_pages_resume_automatically(self):
        reports = [dict(scanned=100, conflicts=[], next_after_key='100'),
                   dict(scanned=100, conflicts=[], next_after_key='200'),
                   dict(scanned=7, conflicts=[], next_after_key=None)]
        with patch.object(archive, 'reconcile_supplier', AsyncMock(side_effect=reports)) as reconcile:
            self.assertEqual(worker.status()['items'][0]['status'], 'pending')
            for count in (100, 200, 207):
                await worker.process_supplier('fixture')
                self.assertEqual(worker.read(worker.directory('fixture') / 'index.json')['scanned'], count)
            self.assertEqual([call.args[1].after_key for call in reconcile.await_args_list], [None, '100', '200'])
            await worker.process_supplier('fixture')
            self.assertEqual(reconcile.await_count, 3)
        self.assertEqual(self.committed, 3)
        self.assertEqual(worker.status(), {'items': []})

    async def test_failed_commit_never_advances_cursor_and_recovers_without_click(self):
        report = dict(scanned=100, conflicts=[], next_after_key='100')
        self.fail_commit = True
        with patch.object(archive, 'reconcile_supplier', AsyncMock(return_value=report)) as reconcile:
            await worker.process_supplier('fixture')
            state = worker.read(worker.directory('fixture') / 'index.json')
            self.assertIsNone(state['after_key'])
            self.assertEqual(state['status'], 'attention')
            self.fail_commit = False
            with patch.object(worker.time, 'time', return_value=state['retry_at'] + 1):
                await worker.process_supplier('fixture')
            self.assertEqual([call.args[1].after_key for call in reconcile.await_args_list], [None, None])
        self.assertEqual(worker.read(worker.directory('fixture') / 'index.json')['after_key'], '100')

    async def test_upload_is_durable_before_attempt_and_removed_only_after_success(self):
        result = {'filename': 'original.pdf', 'path': 'invoices/raw/original.pdf'}
        (self.folder / 'raw').mkdir()
        original = self.folder / 'raw/original.pdf'
        original.write_bytes(b'Synthetic source bytes')
        with patch.object(archive, '_archive_upload', AsyncMock(return_value={'status': 'pending'})):
            await archive.archive_after_upload('fixture', result, 'INV-1')
        queued = list(worker.directory('fixture').glob('upload-*.json'))
        self.assertEqual(len(queued), 1)
        self.assertEqual(worker.read(queued[0])['result'], result)
        with patch.object(archive, '_archive_upload', AsyncMock(return_value={'status': 'completed'})), patch.object(
                archive, 'reconcile_supplier', AsyncMock(return_value=dict(scanned=0, conflicts=[], next_after_key=None))):
            await worker.process_supplier('fixture')
        self.assertFalse(queued[0].exists())
        self.assertEqual(original.read_bytes(), b'Synthetic source bytes')

    async def test_waiting_uploads_do_not_starve_later_pending_work(self):
        for index in range(22):
            worker.write(worker.directory('fixture') / f'upload-{index:03}.json',
                         {'result': {}, 'retry_at': worker.time.time() + 600 if index < 21 else 0})
        with patch.object(archive, 'archive_after_upload', AsyncMock(return_value={'status': 'completed'})) as upload, patch.object(
                archive, 'reconcile_supplier', AsyncMock(return_value=dict(scanned=0, conflicts=[], next_after_key=None))):
            await worker.process_supplier('fixture')
        upload.assert_awaited_once()
        self.assertFalse((worker.directory('fixture') / 'upload-021.json').exists())

    async def test_conflict_retries_keep_visible_issue_and_detect_changed_index(self):
        conflict = {'source_key': 'INV-1', 'code': 'invoice_index_ambiguous'}
        with patch.object(archive, 'reconcile_supplier', AsyncMock(return_value=dict(scanned=1, conflicts=[conflict], next_after_key=None))) as reconcile:
            await worker.process_supplier('fixture')
            state = worker.read(worker.directory('fixture') / 'index.json')
            self.assertEqual(worker.status()['items'][0]['conflicts'], [conflict])
            self.assertEqual(worker.status()['items'][0]['scanned'], 1, 'The completed pass remains visible while awaiting retry')
            await worker.process_supplier('fixture')
            self.assertEqual(reconcile.await_count, 1)
            with patch.object(worker.time, 'time', return_value=state['retry_at'] + 1):
                await worker.process_supplier('fixture')
            self.assertEqual(worker.status()['items'][0]['conflicts'], [conflict], 'Retries do not multiply the same conflict')
            self.index.write_text('{"new": 1}')
            self.assertEqual(worker.status()['items'][0]['status'], 'pending')
            await worker.process_supplier('fixture')
            self.assertEqual(reconcile.await_count, 3, 'New intake bypasses the old retry delay')
