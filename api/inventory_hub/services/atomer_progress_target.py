"""Retain complete, account-bound private xTrek evidence for source-only report rebuilds."""
import asyncio
from contextlib import closing
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from io import BytesIO
import json
import sqlite3
import zlib

from sqlalchemy import text

from inventory_hub.services import atomer_catalog_observation as observation
from inventory_hub.services import atomer_progress as progress
from inventory_hub.services import migration_upgates_export as export
from inventory_hub.services.order_collection import target_fingerprint
from inventory_hub.services.product_publication_source import plain

MAX_RAW = 200_000_000
MAX_LEAVES = 250_000


def _require(ok, reason='atomer_pinned_target_invalid'):
    if not ok:
        raise export.ExportError(reason)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(',', ':')).encode()


def _ids(rows):
    _require(isinstance(rows, list) and 0 < len(rows) <= MAX_LEAVES)
    variants, products, types = set(), set(), {}
    for row in rows:
        pid, vid = row['pid'], row['vid']
        _require(isinstance(pid, str) and pid.isascii() and pid.isdecimal() and int(pid) > 0)
        _require(isinstance(row['code'], str) and isinstance(row['axes'], dict)
                 and ('ean' in row and 'brand' in row and 'availability' in row and 'stock' in row))
        _require(pid not in types or types[pid] == (vid is not None))
        types[pid] = vid is not None
        if vid is not None:
            _require(isinstance(vid, str) and vid.isascii() and vid.isdecimal() and int(vid) > 0 and vid not in variants)
            variants.add(vid)
        else:
            _require(pid not in products)
            products.add(pid)


def pack(rows, source_sha, admin_sha, fp, observed_at):
    _ids(rows)
    _require(fp and observed_at.utcoffset() is not None, 'atomer_target_connection_changed')
    when = observed_at.astimezone(timezone.utc).isoformat()
    # The hashes, observed-at and account are included in the content address.
    # No mutable, in-process matching indexes from reconcile() enter this seal.
    raw = _json(dict(version=1, source=source_sha, admin=admin_sha, fingerprint=fp,
                     observed_at=when, rows=plain(rows)))
    _require(len(raw) <= MAX_RAW, 'atomer_pinned_target_size')
    return dict(digest=sha256(raw).hexdigest(), source=source_sha, admin=admin_sha, fp=fp,
                observed=observed_at, count=len(rows), blob=zlib.compress(raw, 3))


def unpack(stored, source_sha, admin_sha, fp):
    _require(stored and stored['source_sha'] == source_sha and stored['admin_sha'] == admin_sha
             and stored['shop_fingerprint'] == fp and bool(fp), 'atomer_pinned_target_scope')
    try:
        stream = zlib.decompressobj()
        raw = stream.decompress(bytes(stored['compressed']), MAX_RAW + 1)
        _require(len(raw) <= MAX_RAW and stream.eof and not stream.unused_data
                 and sha256(raw).hexdigest() == stored['digest'])
        doc = json.loads(raw)
        observed = datetime.fromisoformat(doc['observed_at'])
        _require(doc['version'] == 1 and doc['source'] == source_sha and doc['admin'] == admin_sha
                 and doc['fingerprint'] == fp and observed == stored['observed_at']
                 and observed.utcoffset() is not None and observed <= datetime.now(timezone.utc))
        rows = doc['rows']
        _ids(rows)
        _require(len(rows) == stored['item_count'])
        return rows, observed
    except (KeyError, TypeError, zlib.error, UnicodeError, json.JSONDecodeError):
        raise export.ExportError('atomer_pinned_target_invalid') from None


def _parameter_key(parent, cutoff):
    # Two changes in one Upgates timestamp second must not share an unchecked
    # cache slot. Only reuse revisions old enough to predate this read window.
    try:
        updated = datetime.fromisoformat(parent['last_update_time'])
    except (KeyError, ValueError, TypeError):
        return None
    if updated.utcoffset() is None or updated >= cutoff - timedelta(seconds=2):
        return None
    identity = [(v['variant_id'], v['code'], v.get('ean')) for v in parent['variants']]
    return sha256(_json([parent['product_id'], parent['code'], updated.isoformat(), sorted(identity)])).hexdigest()


def _parameter_rows(client, products, fp, cutoff, directory):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    file = directory / (fp + '.sqlite3')
    with closing(sqlite3.connect(file, timeout=30)) as cache:
        file.chmod(0o600)
        cache.execute('CREATE TABLE IF NOT EXISTS parameter_rows(id INTEGER PRIMARY KEY, revision TEXT, payload TEXT)')
        parents = observation.indexed(products, 'product_id')
        by_id, missing = {}, []
        all_ids = set()
        for pid, parent in parents.items():
            current = parent.get('variants')
            _require(isinstance(current, list) and bool(current) is parent.get('variants_exists_yn'),
                     'upgates_export_catalog_changed')
            if not current:
                continue
            ids = observation.indexed(current, 'variant_id')
            _require(all_ids.isdisjoint(ids), 'upgates_export_duplicate_or_missing_identity')
            all_ids.update(ids)
            token = _parameter_key(parent, cutoff)
            saved = cache.execute('SELECT payload FROM parameter_rows WHERE id=? AND revision=?', (pid, token)).fetchone() if token else None
            if saved:
                by_id[pid] = json.loads(saved[0])
            else:
                missing.append((pid, token))
        _require(len(all_ids) == observation.total(client, 'products/variants', 'variants'), 'upgates_export_catalog_changed')
        for start in range(0, len(missing), 100):
            group = dict(missing[start:start + 100])
            params = observation.indexed(observation.page_rows(client, 'products/parameters', {
                'product_ids': ';'.join(str(pid) for pid in group)
            }), 'product_id')
            _require(params.keys() == group.keys(), 'upgates_export_family_incomplete')
            for pid, token in group.items():
                parent, observed = parents[pid], params[pid]
                _require(observed.get('code') == parent['code'], 'upgates_export_catalog_changed')
                variants = observation.indexed(observed.get('variants') or [], 'variant_id')
                native = observation.indexed(parent['variants'], 'variant_id')
                _require(variants.keys() == native.keys(), 'upgates_export_family_incomplete')
                by_id[pid] = []
                for vid, leaf in native.items():
                    r = variants[vid]
                    _require(r.get('code') == leaf['code'] and leaf.get('product_id') == pid
                             and leaf.get('product_code') == parent['code'] and isinstance(r.get('parameters'), list),
                             'upgates_export_catalog_changed')
                    by_id[pid].append(dict(product_id=pid, variant_id=vid, code=leaf['code'],
                                           ean=leaf.get('ean'), parameters=r['parameters']))
                if token:
                    cache.execute('INSERT OR REPLACE INTO parameter_rows VALUES (?,?,?)',
                                  (pid, token, _json(by_id[pid]).decode()))
            # Independent native GET pages survive an hourly API admission stop.
            cache.commit()
        return [r for group in by_id.values() for r in group]


async def read_sources(db, source_sha, admin_sha):
    """Reparse the exact archived originals with today's canonical grouping rules."""
    from inventory_hub.services.atomer_new_source import hash_source
    from inventory_hub.services import atomer_source_state as state
    matches = (await db.execute(text("""
        SELECT sha256,format,compressed FROM atomer_review_exports WHERE sha256 IN (:s,:a)
    """), dict(s=source_sha, a=admin_sha))).all()
    blobs = {sha: (fmt, bytes(blob)) for sha, fmt, blob in matches}
    _require(set(blobs) == {source_sha, admin_sha} and blobs[source_sha][0] == 'buyers'
             and blobs[admin_sha][0] == 'admin_csv', 'atomer_reindex_source_archive_missing')
    def parse():
        xml = hash_source(blobs[source_sha][1], source_sha)
        csv = hash_source(blobs[admin_sha][1], admin_sha)
        digest, _, identities = progress.parse_source(BytesIO(xml))
        _require(digest == source_sha, 'atomer_reindex_source_changed')
        return identities, state.parse_admin(csv)
    return await asyncio.to_thread(parse)


def _complete_xtrek(parents, fp):
    from inventory_hub.routers.upgates_sync import _cache_path
    from inventory_hub.services import stock_publication_source as transport
    client = transport._client('xtrek', fp)
    try:
        directory = _cache_path('xtrek').parent / 'atomer-progress-parameters-v1'
        params = _parameter_rows(client, parents, fp, parents.checked, directory)
        _require(len(parents) == observation.total(client, 'products', 'products'), 'upgates_export_catalog_changed')
        observation.verify_read_window(client, parents.checked)
        return progress.flatten_catalog(parents, params)
    finally:
        client.session.close()


async def read_target(db, last, source_sha, admin_sha, mode):
    from inventory_hub.services import atomer_card_batch as cards
    fp = target_fingerprint('xtrek')
    _require(fp and source_sha == last['source_sha256'] and admin_sha and admin_sha == last['admin_export_sha256'],
             'atomer_pinned_target_scope')
    if mode == 'pinned_source':
        key = last['totals'].get('xtrek_target_snapshot')
        _require(key, 'atomer_pinned_target_missing_use_certified_checkpoint')
        stored = (await db.execute(text('SELECT * FROM atomer_progress_target_snapshots WHERE digest=:d'),
                                   {'d': key})).mappings().first()
        rows, observed = await asyncio.to_thread(unpack, stored, source_sha, admin_sha, fp)
    else:
        _require(mode == 'certified_checkpoint', 'atomer_target_mode_invalid')
        parents = await cards.load_shop_catalog('xtrek')
        _require(parents.fingerprint == fp, 'atomer_target_connection_changed')
        rows = await asyncio.to_thread(_complete_xtrek, parents, fp)
        observed = parents.checked
        sealed = await asyncio.to_thread(pack, rows, source_sha, admin_sha, fp, observed)
        key = sealed['digest']
        await db.execute(text("""
            INSERT INTO atomer_progress_target_snapshots
                (digest,source_sha,admin_sha,shop_fingerprint,observed_at,item_count,compressed)
            VALUES (:digest,:source,:admin,:fp,:observed,:count,:blob) ON CONFLICT(digest) DO NOTHING
        """), sealed)
    # Card ownership and links use the report transaction's local snapshot.
    # BIKE is independently observed; none of its freshness promotes xTrek.
    bike = await cards.load_shop_catalog('biketrek')
    _require(fp == target_fingerprint('xtrek') and bike.fingerprint == target_fingerprint('biketrek'),
             'atomer_target_connection_changed')
    targets = await asyncio.to_thread(progress.bike_catalog, bike)
    times = dict(xtrek_target_snapshot=key, xtrek_observed_at=observed.isoformat(), bike_observed_at=bike.checked.isoformat(),
                 target_mode=mode)
    return rows, targets, observed, times
