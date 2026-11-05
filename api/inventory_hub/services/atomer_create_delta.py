"""Private resumable SKU/GTIN candidate index shared between xTrek create batches.

Candidates are a conservative superset (old tokens need not be removed). A
positive candidate is always fetched again by native ID. An absence is only
returned after a complete delta from the pinned report or an independently
certified entire xTrek catalog, including a current tail after multi-page scans.
"""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import sqlite3

from inventory_hub.services import migration_upgates_export as export
from inventory_hub.services.product_identity import barcode_keys


def _require(ok, reason='upgates_export_invalid_pagination'):
    if not ok:
        raise export.ExportError(reason)


def _now():
    return datetime.now(timezone.utc)


def _tokens(parent):
    pid = parent.get('product_id')
    leaves = parent.get('variants')
    if leaves is None and parent.get('variants_exists_yn') is False:
        leaves = []
    _require(type(pid) is int and pid > 0 and isinstance(leaves, list) and bool(leaves) is parent.get('variants_exists_yn'),
             'upgates_export_catalog_changed')
    _require(all(type(v.get('variant_id')) is int and v['variant_id'] > 0 and v.get('product_id') == pid
                 and v.get('product_code') == parent.get('code') for v in leaves), 'upgates_export_catalog_changed')
    for row in [parent, *leaves]:
        sku = row.get('code')
        _require(isinstance(sku, str), 'upgates_export_catalog_changed')
        yield pid, 'code', sku.casefold()
        for gtin in barcode_keys([row.get('ean'), sku]):
            yield pid, 'ean', gtin


INSERT = 'INSERT OR IGNORE INTO tokens(product,kind,value) VALUES(?,?,?)'


def _seed(cache, checkpoint, baseline):
    if not checkpoint.exists():
        return None
    from inventory_hub.services import atomer_catalog_checkpoint
    read_certificate = getattr(atomer_catalog_checkpoint, 'archived_parents', None)
    if read_certificate:
        parents, stamp = read_certificate(checkpoint.parent, checkpoint.stem)
        if not parents or not stamp:
            return None
        observed = datetime.fromisoformat(stamp)
        if observed.utcoffset() is None or not baseline < observed <= _now():
            return None
        for pid, parent in parents.items():
            _require(str(parent.get('product_id')) == str(pid), 'upgates_export_catalog_changed')
            cache.executemany(INSERT, _tokens(parent))
        return observed
    with closing(sqlite3.connect(checkpoint.as_uri() + '?mode=ro', uri=True, timeout=15)) as old:
        old.execute('BEGIN')
        tables = {r[0] for r in old.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'cursor', 'parents'} <= tables:
            return None
        saved = old.execute('SELECT document FROM cursor WHERE id=1').fetchone()
        if not saved:
            return None
        state = json.loads(saved[0])
        before_delta = (state.get('phase') == 'delta' and state.get('page') == 1 and state.get('delta_count') is None
                        and state.get('phase_before_delta') == 'validate')
        if (not before_delta and state.get('phase') != 'ready' or not state.get('final_started')
                or state.get('base_started') != state['final_started']):
            return None
        observed = datetime.fromisoformat(state['base_started'])
        now = _now()
        if observed.utcoffset() is None or observed <= baseline or observed > now:
            return None
        count = old.execute('SELECT count(*) FROM parents').fetchone()[0]
        _require(count == state['base_count'] and count > 0, 'upgates_export_catalog_changed')
        for pid, payload in old.execute('SELECT id,payload FROM parents'):
            parent = json.loads(payload)
            _require(parent['product_id'] == pid, 'upgates_export_catalog_changed')
            cache.executemany(INSERT, _tokens(parent))
        return observed


def _page(client, cursor):
    data = export._read(client, 'products/simple', dict(last_update_time_from=cursor['since'], page=cursor['page'],
                                                       current_page_items=100))
    total, pages = export._integer(data, 'number_of_items'), export._integer(data, 'number_of_pages')
    rows = data.get('products')
    _require(isinstance(rows, list) and pages in ({0, 1} if not total else {(total + 99) // 100})
             and pages <= export.MAX_PAGES and export._integer(data, 'current_page') == cursor['page']
             and export._integer(data, 'current_page_items') == len(rows)
             and len(rows) == max(0, min(100, total - (cursor['page']-1) * 100)))
    return rows, total, pages


def _proof(db, interests):
    db.execute('CREATE TEMP TABLE IF NOT EXISTS interests(kind TEXT,value TEXT, PRIMARY KEY(kind,value))')
    db.execute('DELETE FROM interests')
    db.executemany('INSERT INTO interests VALUES(?,?)', interests)
    return {r[0] for r in db.execute('SELECT DISTINCT t.product FROM interests i JOIN tokens t USING(kind,value)')}


def _cursor(since):
    return dict(since=since.isoformat(timespec='seconds'), began=_now().isoformat(), page=1, expected=None)


def candidates(client, fp, checked_at, interests, directory):
    _require(len(fp) == 64 and all(c in '0123456789abcdef' for c in fp) and checked_at.utcoffset() is not None,
             'upgates_export_target_changed')
    started = _now()
    checked_at = checked_at.astimezone(timezone.utc)
    _require(checked_at <= started, 'upgates_export_update_time_unknown')
    folder = directory / 'atomer-create-identities-v1'
    folder.mkdir(parents=True, mode=0o700, exist_ok=True)
    path = folder / (fp + '.sqlite3')
    with closing(sqlite3.connect(path, timeout=30)) as db:
        path.chmod(0o600)
        db.execute('CREATE TABLE IF NOT EXISTS meta(id INTEGER PRIMARY KEY, state TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS tokens(product INTEGER,kind TEXT,value TEXT,PRIMARY KEY(kind,value,product))')
        db.execute('CREATE TABLE IF NOT EXISTS seen(product INTEGER PRIMARY KEY)')
        db.commit()
        restarts = 0
        for _ in range(export.MAX_PAGES * 3 + 12):
            db.execute('BEGIN IMMEDIATE')
            saved = db.execute('SELECT state FROM meta WHERE id=1').fetchone()
            state = json.loads(saved[0]) if saved else dict(baseline=checked_at.isoformat(), watermark=checked_at.isoformat())
            if not state.get('complete_base') and checked_at < datetime.fromisoformat(state['baseline']):
                # Older source runs can also use the index, once their missing interval is covered.
                state = dict(baseline=checked_at.isoformat(), watermark=checked_at.isoformat())
            if state.get('complete_at') and datetime.fromisoformat(state['complete_at']) >= started:
                return _proof(db, interests)
            if 'cursor' not in state:
                previous = datetime.fromisoformat(state['watermark'])
                if not state.get('complete_base') and (started - previous) > timedelta(minutes=10):
                    seed = _seed(db, directory / 'atomer-card-catalog-v1' / (fp + '.sqlite3'), previous)
                    if seed:
                        previous = seed
                        state.update(complete_base=True, watermark=seed.isoformat())
                lower = previous - timedelta(minutes=2)
                if not state.get('complete_base'):
                    lower = max(datetime.fromisoformat(state['baseline']) - timedelta(seconds=1), lower)
                state['cursor'] = _cursor(lower)
                db.execute('DELETE FROM seen')
            cursor = state['cursor']
            rows, count, pages = _page(client, cursor)
            changed = cursor['expected'] is not None and count != cursor['expected']
            for row in rows:
                db.executemany(INSERT, _tokens(row))
                inserted = db.execute('INSERT OR IGNORE INTO seen VALUES(?)', (row['product_id'],)).rowcount
                changed = changed or inserted != 1
            done = cursor['page'] >= pages
            if done:
                changed = changed or db.execute('SELECT count(*) FROM seen').fetchone()[0] != count
            if changed:
                # Retain positive candidates, never advance an incomplete absence proof.
                state.pop('cursor')
                restarts += 1
            elif not done:
                cursor.update(page=cursor['page'] + 1, expected=count)
            else:
                began = datetime.fromisoformat(cursor['began'])
                state['watermark'] = began.isoformat()
                if cursor['page'] > 1:
                    # New or updated identities during earlier pages (including
                    # a resumed hourly scan) must be included before admitting a write.
                    state['cursor'] = _cursor(began - timedelta(seconds=1))
                    db.execute('DELETE FROM seen')
                else:
                    state.pop('cursor')
                    state['complete_at'] = _now().isoformat()
            db.execute('INSERT OR REPLACE INTO meta VALUES(1,?)', (json.dumps(state),))
            db.commit()
            _require(restarts < 3, 'upgates_export_catalog_changed')
            if done and not changed and 'cursor' not in state:
                return _proof(db, interests)
    raise export.ExportError('upgates_export_catalog_changed')
