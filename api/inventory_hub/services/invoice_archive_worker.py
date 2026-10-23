"""Resume local invoice indexing automatically; never download or receive goods."""
import asyncio
import hashlib
import json
import logging
from pathlib import Path
import time
from uuid import uuid4

from sqlalchemy import text

from inventory_hub import database
from inventory_hub.database import get_session_context
from inventory_hub.file_access import code_component, confined_path
from inventory_hub.invoice_archive_types import InvoiceReconcileInput
from inventory_hub.settings import settings

logger = logging.getLogger(__name__)
WORKER_LOCK = 691432233


def directory(supplier):
    return confined_path(settings.INVENTORY_DATA_ROOT, f'suppliers/{code_component(supplier)}/invoices/archive-state')


def read(path):
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except FileNotFoundError:
        return {}


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def enqueue_upload(supplier, result, invoice_number):
    # File-first intake retains the exact local operation before trying the DB.
    payload = {'result': result, 'invoice_number': invoice_number}
    key = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    path = directory(supplier) / ('upload-' + key + '.json')
    if not path.exists():
        write(path, {**payload, 'status': 'pending'})
    return path


def suppliers():
    root = Path(settings.INVENTORY_DATA_ROOT) / 'suppliers'
    return sorted(path.name for path in root.iterdir() if path.is_dir() and not path.is_symlink()
                  and (path / 'invoices').is_dir()) if root.is_dir() else []


def status():
    result = []
    for supplier in suppliers():
        folder = directory(supplier)
        index = confined_path(settings.INVENTORY_DATA_ROOT, f'suppliers/{supplier}/invoices/index.latest.json')
        saved = read(folder / 'index.json')
        stamp = [index.stat().st_mtime_ns, index.stat().st_size] if index.is_file() else None
        if stamp and saved.get('stamp') != stamp:
            saved = {'status': 'pending', 'conflicts': []}
        pending = []
        for path in sorted(folder.glob('upload-*.json')):
            item = read(path)
            pending.append({'source_key': item.get('invoice_number') or item.get('result', {}).get('filename'),
                            'code': item.get('code') or next((row.get('code') for row in item.get('conflicts', [])), None), 'status': item.get('status', 'pending')})
        if saved.get('status') in ('pending', 'attention') or pending:
            result.append({'supplier_code': supplier, 'status': saved.get('status', 'pending'),
                           'scanned': saved.get('scanned', 0), 'conflicts': saved.get('conflicts', []), 'uploads': pending})
    return {'items': result}


async def process_supplier(supplier):
    from inventory_hub.services import invoice_archive as archive
    folder = directory(supplier)
    now = time.time()
    attempted = 0
    for path in sorted(folder.glob('upload-*.json')):
        item = read(path)
        if item.get('retry_at', 0) > now:
            continue
        if attempted >= 20:
            break
        attempted += 1
        result = await archive.archive_after_upload(supplier, item['result'], item.get('invoice_number'), queued=True)
        if result['status'] == 'completed':
            path.unlink(missing_ok=True)
        else:
            write(path, {**item, 'status': result['status'], 'code': result.get('code'),
                         'retry_at': now + 60, 'conflicts': result.get('conflicts', [])})
    index = confined_path(settings.INVENTORY_DATA_ROOT, f'suppliers/{supplier}/invoices/index.latest.json')
    if not index.is_file():
        return
    stamp = [index.stat().st_mtime_ns, index.stat().st_size]
    state_path = folder / 'index.json'
    state = read(state_path)
    if state.get('stamp') != stamp:
        state = {'stamp': stamp, 'status': 'pending', 'after_key': None, 'scanned': 0, 'conflicts': []}
    if state.get('status') == 'completed' or state.get('retry_at', 0) > now:
        return
    if state.get('after_key') is None:
        state.update(conflicts=[], scanned=0)
    try:
        async with get_session_context() as db:
            report = await archive.reconcile_supplier(db, InvoiceReconcileInput(
                supplier_code=supplier, after_key=state.get('after_key'), limit=100))
        # Cursor advances only after the complete batch has committed. A crash
        # before this file write safely replays the same idempotent DB batch.
        conflicts = state.get('conflicts', []) + report['conflicts']
        state.update(after_key=report['next_after_key'], scanned=state.get('scanned', 0) + report['scanned'],
                     conflicts=conflicts, status='pending' if report['next_after_key'] else 'attention' if conflicts else 'completed')
        if not report['next_after_key'] and conflicts:
            state.update(retry_at=now + 300, after_key=None)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        # Never log exception bodies: source paths or document data may be private.
        state.update(status='attention', retry_at=now + 60,
                     conflicts=[{'source_key': None, 'code': getattr(error, 'code', 'invoice_archive_unavailable')}])
        logger.warning('Local invoice indexing pending (%s)', type(error).__name__)
    write(state_path, state)


async def cycle():
    if database._engine is None:
        await database.init_db()
    async with database._engine.connect() as connection:
        if not await connection.scalar(text('SELECT pg_try_advisory_lock(:key)'), {'key': WORKER_LOCK}):
            return
        try:
            await connection.commit()
            for supplier in suppliers():
                try:
                    await process_supplier(supplier)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    logger.warning('Local invoice recovery pending (%s)', type(error).__name__)
        finally:
            await connection.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': WORKER_LOCK})
            await connection.commit()


async def run():
    while True:
        try:
            await cycle()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning('Invoice archive worker unavailable (%s)', type(error).__name__)
        await asyncio.sleep(5)
