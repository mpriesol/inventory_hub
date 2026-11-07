"""Configuration leases never synchronously block their own asyncio loop."""
import asyncio
import json
import os
from pathlib import Path
import signal
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from inventory_hub import config_io
from inventory_hub.routers import receiving_db
from inventory_hub.services.product_consolidation_keeper_dependencies import configurations
from inventory_hub.supplier_prefix import SupplierPrefixError


class SupplierConfigurationLeaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.config_patch = patch.object(config_io, 'DATA_ROOT', self.root)
        self.config_patch.start()
        config_io.save_supplier('synthetic', {'product_code_prefix': 'SYN-', 'note': 'before'})

    async def asyncTearDown(self):
        self.config_patch.stop()
        self.directory.cleanup()

    def snapshot(self):
        return {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob('*.json')}

    async def test_async_load_save_claim_rotate_fail_before_any_data_change(self):
        before = self.snapshot()
        with configurations() as lease:
            lease.get('synthetic')
            operations = [lambda: config_io.load_supplier('synthetic'),
                lambda: config_io.save_supplier('synthetic', {'note': 'must not be written'}),
                lambda: config_io.claim_supplier_prefix('synthetic', 'SYN-'),
                lambda: config_io.rotate_supplier_prefix('synthetic', 'SYN-', 'NEW-')]
            for operation in operations:
                with self.assertRaises(SupplierPrefixError) as error:
                    operation()
                self.assertEqual((error.exception.code, error.exception.status), ('supplier_configuration_busy', 409))
                self.assertEqual(self.snapshot(), before)
            with self.assertRaises(HTTPException) as error:
                receiving_db._product_code_prefix('synthetic')
            self.assertEqual(error.exception.status_code, 409)
            lease.verify()
        self.assertEqual(config_io.load_supplier('synthetic')['note'], 'before')

    async def test_shared_lease_over_await_keeps_loop_ticking_and_releases(self):
        # A watchdog interrupts a regressed synchronous flock, which asyncio's
        # own timeout cannot interrupt while its event loop is blocked.
        def timeout(*_):
            raise TimeoutError('A synchronous configuration lock blocked the event loop')
        previous = signal.signal(signal.SIGALRM, timeout)
        signal.setitimer(signal.ITIMER_REAL, 3)
        ticks = []
        async def ticker():
            for i in range(4):
                ticks.append(i)
                await asyncio.sleep(.005)
        async def competitor():
            await asyncio.sleep(.002)
            with self.assertRaises(SupplierPrefixError) as error:
                config_io.load_supplier('synthetic', write_back_on_load=False)
            self.assertEqual(error.exception.code, 'supplier_configuration_busy')
        try:
            with configurations() as lease:
                lease.get('synthetic')
                await asyncio.gather(ticker(), competitor())
                lease.verify()
            self.assertEqual(ticks, [0, 1, 2, 3])
            self.assertEqual(config_io.load_supplier('synthetic')['note'], 'before')
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)

    async def test_synchronous_thread_keeps_waiting_semantics_until_release(self):
        started = asyncio.Event()
        loop = asyncio.get_running_loop()
        def writer():
            loop.call_soon_threadsafe(started.set)
            return config_io.save_supplier('synthetic', {'note': 'after'})
        before = self.snapshot()
        with configurations() as lease:
            lease.get('synthetic')
            task = asyncio.create_task(asyncio.to_thread(writer))
            await asyncio.wait_for(started.wait(), 1)
            await asyncio.sleep(.03)
            self.assertFalse(task.done())
            self.assertEqual(self.snapshot(), before)
        result = await asyncio.wait_for(task, 3)
        self.assertEqual(result['note'], 'after')

    async def test_other_process_writer_cannot_change_configuration_during_lease(self):
        code = """from inventory_hub import config_io
print('started', flush=True)
config_io.save_supplier('synthetic', {'note': 'other-process'})
print('completed', flush=True)
"""
        before = self.snapshot()
        with configurations() as lease:
            lease.get('synthetic')
            process = await asyncio.create_subprocess_exec(sys.executable, '-c', code,
                env={**os.environ, 'INVENTORY_DATA_ROOT': str(self.root)},
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            self.assertEqual((await asyncio.wait_for(process.stdout.readline(), 3)).strip(), b'started')
            await asyncio.sleep(.03)
            self.assertIsNone(process.returncode)
            self.assertEqual(self.snapshot(), before)
        stdout, stderr = await asyncio.wait_for(process.communicate(), 3)
        self.assertEqual(process.returncode, 0, stderr.decode())
        self.assertEqual(stdout.strip(), b'completed')
        self.assertEqual(config_io.load_supplier('synthetic')['note'], 'other-process')

    async def test_global_prefix_error_preserves_status_without_echoing_private_message(self):
        from inventory_hub.main import supplier_prefix_error_response
        for status, code in [(409, 'supplier_configuration_busy'), (404, 'supplier_not_found'),
                             (422, 'supplier_prefix_invalid')]:
            response = await supplier_prefix_error_response(None, SupplierPrefixError(code, 'private-value-do-not-echo', status))
            self.assertEqual(response.status_code, status)
            self.assertEqual(json.loads(response.body)['detail']['code'], code)
            self.assertNotIn(b'private-value-do-not-echo', response.body)
            self.assertEqual(response.headers['cache-control'], 'no-store')
