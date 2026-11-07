"""Fill an empty xTrek standalone barcode from its original Atomer and physical card.

A committed intent can only be observed after a restart or lost HTTP reply.
The two native identities, Hub card, physical history and both links are sealed;
neither a claimed SKU nor a submitted preview document is an identity authority.
"""
import asyncio
from copy import deepcopy
from typing import Annotated

from pydantic import Field
from sqlalchemy import select, text

from inventory_hub.product_editor_models import AtomerVariantRepair, ProductEditorOverride
from inventory_hub.services import atomer_card_batch as cards
from inventory_hub.services import atomer_catalog_observation as observation, atomer_conflict_resolution as conflicts
from inventory_hub.services import atomer_generations as gen, atomer_new_native as native
from inventory_hub.services import atomer_progress_target as evidence, atomer_review as work, atomer_source_state
from inventory_hub.services import atomer_variant_repair as repair, stock_publication_source as transport
from inventory_hub.services.atomer_review_targets import ReviewError
from inventory_hub.services.order_collection import target_fingerprint
from inventory_hub.services.product_identity import IDENTITY_WRITE_LOCK, RemoteIdentity, barcode_keys, load_identity_index, verified_barcodes
from inventory_hub.services.product_publication_source import plain

DIGEST = Annotated[str, Field(pattern='^[0-9a-f]{64}$')]
ID = Annotated[int, Field(gt=0, le=9_007_199_254_740_991)]
OP = 'atomer_fill_empty_native_ean'


class Item(gen.Strict):
    code: Annotated[str, Field(min_length=1, max_length=100)]
    source_id: repair.Token
    ean: Annotated[str, Field(pattern='^[0-9]{8,14}$')]
    card: ID
    xtrek_id: ID
    xtrek_link: ID
    bike_id: ID
    bike_variant: ID | None = None
    bike_link: ID


class Selection(gen.Strict):
    run_id: ID
    source_sha: DIGEST
    admin_sha: DIGEST
    items: list[Item] = Field(min_length=1, max_length=12)
    preview_sha: DIGEST | None = None


def _require(ok, code):
    if not ok:
        raise ReviewError(code)


def _validate(items):
    _require(all(r.ean in verified_barcodes([r.ean]) and transport._code(r.code) == r.code for r in items)
             and all(len({getattr(r, k) for r in items}) == len(items)
                     for k in ('code', 'card', 'xtrek_id', 'xtrek_link', 'bike_link', 'source_id'))
             and len(set.union(*(barcode_keys([r.ean]) for r in items))) == len(items), 'missing_ean_invalid_pins')


def _source(rows, admin, items):
    source = {r['code']: r for r in rows}
    for r in items:
        base, sale = admin.get(r.code), source.get(r.code)
        key = barcode_keys([r.ean])
        aliases = {c for c, entry in admin.items() if barcode_keys([c, entry.get('ean'), *entry.get('all_eans', [])]) & key}
        buyers = {entry['code'] for entry in rows if barcode_keys([entry.get('code'), entry.get('ean')]) & key}
        _require(base and sale and base.get('internal_id') == r.source_id and base.get('has_catalog_code') is True
                 and base.get('parent') in ('', None) and not base.get('conflict') and base.get('ean') == r.ean
                 and sale.get('parent') in ('', None, r.code) and not sale.get('conflict') and sale.get('ean') == r.ean
                 and aliases == buyers == {r.code} and not key.intersection(
                     barcode_keys(getattr(admin, 'quarantined_identifiers', []))), 'missing_ean_source_conflict')


async def _lock(db, s):
    await work._lock_current_run(db, s.run_id)
    head = (await db.execute(text('SELECT source_sha256,admin_export_sha256 FROM atomer_progress_runs WHERE id=:i'),
                             {'i': s.run_id})).one()
    _require(tuple(head) == (s.source_sha, s.admin_sha), 'missing_ean_source_changed')
    await db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': IDENTITY_WRITE_LOCK})


def _guard_review(row):
    _require(row['status'] in ('open', 'blocked') and row['scope'] != 'biketrek', 'missing_ean_operator_review_changed')
    return {'revision': row['revision'], 'status': row['status'], 'scope': row['scope']}


async def _local(db, s):
    result = {}
    identities = [RemoteIdentity(0, r.code, barcodes=(r.ean,)) for r in s.items]
    local = await load_identity_index(db, 0, identities, local=True)
    ids = [r.card for r in s.items]
    activity = await conflicts.card_activity(db, ids)
    moves = dict((await db.execute(text("""
        SELECT product_id,md5(string_agg(md5(to_jsonb(m)::text),'' ORDER BY id))
        FROM stock_movements m WHERE product_id=ANY(CAST(:ids AS bigint[])) GROUP BY product_id
    """), {'ids': ids})).all())
    for r, identity in zip(s.items, identities):
        detail = await db.scalar(text("""
          SELECT detail FROM atomer_progress_items WHERE run_id=:run AND code=:sku AND kind='standalone'
        """), {'run': s.run_id, 'sku': r.code})
        _require(detail and detail['ean'] == r.ean and str(detail.get('product_id')) == str(r.xtrek_id)
                 and not detail.get('variant_id') and detail.get('target_code') == r.code
                 and detail.get('hub_id') == r.card, 'missing_ean_report_changed')
        work_states = [_guard_review(await work._record(db, kind, key)) for kind, key in
                       [('item', r.code), ('family', detail['family_key'])]]
        card = local.products.get(r.card)
        _require(card and card.sku == r.code and card.is_active and card.item_type == 'stock'
                 and local.resolve(identity).product_id == r.card
                 and barcode_keys(local.product_barcodes.get(r.card, [])) == barcode_keys([r.ean]),
                 'missing_ean_card_changed')
        override = await db.get(ProductEditorOverride, card.id, populate_existing=True)
        eans = ((override.data if override else {}) or {}).get('variant', {}).get('eans')
        _require(eans is None or barcode_keys(eans) == barcode_keys([r.ean]), 'missing_ean_card_override_changed')
        bindings = (await db.execute(text("""
          SELECT s.code AS shop,to_jsonb(sp) AS row FROM shop_products sp JOIN shops s ON s.id=sp.shop_id
          WHERE sp.retired_at IS NULL AND (sp.product_id=:card OR (s.code='xtrek' AND sp.external_product_id=:pid)
             OR (s.code='biketrek' AND sp.external_product_id=:bp AND sp.external_variant_id IS NOT DISTINCT FROM CAST(:bv AS text)))
          ORDER BY sp.id FOR UPDATE OF sp
        """), dict(card=r.card, pid=str(r.xtrek_id), bp=str(r.bike_id), bv=str(r.bike_variant) if r.bike_variant else None))).all()
        _require(all(link['product_id'] == r.card for _, link in bindings), 'missing_ean_link_owner_changed')
        active = [(shop, link) for shop, link in bindings if link.get('is_listed') is True]
        links = dict(active)
        _require(len(active) == 2 and set(links) == {'xtrek', 'biketrek'}, 'missing_ean_link_owner_changed')
        for shop, pid, vid, pk in [('xtrek', r.xtrek_id, None, r.xtrek_link),
                                     ('biketrek', r.bike_id, r.bike_variant, r.bike_link)]:
            link = links[shop]
            _require(link['id'] == pk and link['product_id'] == r.card and link['external_code'] == r.code
                     and link['external_product_id'] == str(pid) and link.get('external_variant_id') == (str(vid) if vid else None)
                     and bool(link.get('is_variant')) == bool(vid) and not link.get('push_pending'), 'missing_ean_link_changed')
        stats = activity[r.card]
        _require(not any(stats[k] for k in ('orders', 'native_orders', 'reservations', 'stock_work')), 'missing_ean_open_activity')
        protected = [{k:v for k,v in link.items() if k not in ('updated_at', 'last_pull_at', 'last_push_at')}
                     for _, link in bindings]
        result[r.code] = plain(dict(card=card.id, sku=card.sku, eans=sorted(local.product_barcodes.get(card.id, [])),
                                    links=protected, movement_digest=moves.get(card.id), activity=stats, work=work_states))
    return result


def _ownership(parents, items):
    expected = {r.code: r for r in items}
    skus = {c.casefold(): c for c in expected}
    gtins = {next(iter(barcode_keys([r.ean]))): r.code for r in items}
    owners = {r.code: set() for r in items}
    for parent in parents:
        pid = parent['product_id']
        for row in [parent, *(parent.get('variants') or [])]:
            location = (pid, row.get('variant_id'))
            sku = skus.get((row.get('code') or '').casefold())
            if sku:
                owners[sku].add(location)
            for key in barcode_keys([row.get('code'), row.get('ean')]):
                if key in gtins:
                    owners[gtins[key]].add(location)
    for r in items:
        _require(owners[r.code] == {(r.xtrek_id, None)}, 'missing_ean_native_identifier_occupied')


def _targets(items, fp, bike_fp):
    with native._connection(fp) as client:
        docs = {r.code: {'payload': {'code': r.code}} for r in items}
        targets = native._read(client, docs, {r.code: {'product_id': r.xtrek_id} for r in items})
    bike = transport._client('biketrek', bike_fp)
    try:
        roots = native._by_id(native._sparse(bike, {r.bike_id for r in items}, 'products/simple',
                              field='product_ids', filters={'variants_yn': 'false'}), 'product_id')
        variant_skus = {r.code for r in items if r.bike_variant}
        variants = native._by_id(native._sparse(bike, variant_skus, 'products/variants', 'variants', 'variant_codes'), 'variant_id')
    finally:
        bike.session.close()
    _require(roots.keys() == {r.bike_id for r in items}
             and variants.keys() == {r.bike_variant for r in items if r.bike_variant}, 'missing_ean_bike_identity_changed')
    out = {}
    for r in items:
        x, base = targets.get(r.code), roots[r.bike_id]
        leaf = variants[r.bike_variant] if r.bike_variant else base
        _require(x and x['code'] == r.code and not x.get('variants_exists_yn') and not x['variants']
                 and not x.get('set_yn') and not x.get('archived_yn') and x.get('ean') in (None, '', r.ean),
                 'missing_ean_xtrek_identity_changed')
        _require(base.get('variants_exists_yn') is bool(r.bike_variant) and leaf.get('code') == r.code
                 and (not r.bike_variant or leaf.get('product_id') == r.bike_id and leaf.get('product_code') == base['code'])
                 and (not leaf.get('ean') or barcode_keys([leaf['ean']]) == barcode_keys([r.ean])),
                 'missing_ean_bike_identity_changed')
        b = {k: leaf.get(k) for k in ('code', 'ean', 'product_id', 'variant_id', 'product_code', 'stock', 'stocks')}
        out[r.code] = plain(dict(xtrek=x, biketrek=b))
    return out


def _same_protected(before, after):
    def protected(value):
        snapshot = deepcopy(value)
        snapshot['xtrek'].pop('last_update_time', None)
        snapshot['xtrek'].pop('ean', None)
        return snapshot
    return protected(before) == protected(after)


def _journal_key(s, r, fp, bike_fp):
    return repair.digest([OP, s.source_sha, s.admin_sha, fp, bike_fp, r.model_dump()])


async def _jobs(db, keys):
    return {code: await db.get(AtomerVariantRepair, key, with_for_update=True, populate_existing=True)
            for code, key in keys.items()}


async def _scope(db, s, fp, bfp):
    await _lock(db, s)
    _require(fp and bfp and fp == target_fingerprint('xtrek') and bfp == target_fingerprint('biketrek'),
             'missing_ean_connection_changed')
    return await _local(db, s)


async def process(db, s, mode, actor):
    _validate(s.items)
    fp, bfp = target_fingerprint('xtrek'), target_fingerprint('biketrek')
    local = await _scope(db, s, fp, bfp)
    xml, admin = await evidence.read_sources(db, s.source_sha, s.admin_sha)
    await asyncio.to_thread(_source, atomer_source_state.catalog_with_admin(xml, admin, True), admin, s.items)
    keys = {r.code: _journal_key(s, r, fp, bfp) for r in s.items}
    saved = await _jobs(db, keys)
    other = await db.scalar(text("""
       SELECT id FROM atomer_variant_repairs WHERE parent_code=ANY(CAST(:codes AS text[]))
         AND state IN ('sending','uncertain') AND NOT (id=ANY(CAST(:ids AS text[]))) LIMIT 1
    """), {'codes': list(keys), 'ids': list(keys.values())})
    _require(not other, 'missing_ean_another_intent')
    catalog = await cards.load_shop_catalog('xtrek')
    _require(catalog.fingerprint == fp, 'missing_ean_connection_changed')
    await asyncio.to_thread(_ownership, catalog, s.items)
    remote = await asyncio.to_thread(_targets, s.items, fp, bfp)
    signature = repair.digest([OP, s.run_id, keys, local, remote])
    pending, sent, adopted = [], [], []
    receipts = []
    for r in s.items:
        job = saved[r.code]
        if job is not None:
            doc = job.document
            _require(doc.get('operation') == OP and doc['source_id'] == r.source_id
                     and doc['card_id'] == r.card and doc['ean'] == r.ean, 'missing_ean_intent_changed')
            if job.state == 'verified':
                _require(remote[r.code]['xtrek'].get('ean') == r.ean, 'missing_ean_verified_identity_changed')
                receipts.append(dict(code=r.code, product_id=r.xtrek_id, card=r.card, ean=r.ean, outcome='already_present'))
                continue
            if job.state in ('sending', 'uncertain'):
                sent.append(r)
                continue
            raise ReviewError('missing_ean_intent_changed')
        if remote[r.code]['xtrek'].get('ean') == r.ean:
            receipts.append(dict(code=r.code, product_id=r.xtrek_id, card=r.card, ean=r.ean, outcome='already_present'))
            adopted.append(r)
        else:
            pending.append(r)
    if mode == 'preview':
        return dict(status='ready' if pending or sent else 'verified', count=len(s.items),
                    preview_sha=signature, rows=receipts, observed_ids={r.code:r.xtrek_id for r in s.items})
    _require(not pending or s.preview_sha == signature, 'missing_ean_preview_changed')
    for r in adopted:
        doc = dict(operation=OP, actor=actor, run_id=s.run_id, source_sha=s.source_sha, admin_sha=s.admin_sha,
                   source_id=r.source_id, card_id=r.card, ean=r.ean, pins=r.model_dump(), adopted=True,
                   hub=local[r.code], observed=remote[r.code], at=catalog.checked.isoformat())
        db.add(AtomerVariantRepair(id=keys[r.code], parent_code=r.code, state='verified', document=doc))
    if pending:
        for r in pending:
            doc = dict(operation=OP, actor=actor, run_id=s.run_id, source_sha=s.source_sha, admin_sha=s.admin_sha,
                       source_id=r.source_id, card_id=r.card, ean=r.ean, pins=r.model_dump(), preview_sha=s.preview_sha,
                       before=remote[r.code], hub=local[r.code], at=catalog.checked.isoformat())
            db.add(AtomerVariantRepair(id=keys[r.code], parent_code=r.code, state='sending', document=doc))
        await db.commit()
        try:
            current_local = await _scope(db, s, fp, bfp)
            fresh = await asyncio.to_thread(_targets, s.items, fp, bfp)
            _require(current_local == local and fresh == remote, 'missing_ean_preview_changed')
            # Only an unchanged native catalog can extend the earlier complete
            # identifier census over the short interval immediately before PUT.
            def fence():
                with native._before_write(fp) as client:
                    observation.verify_read_window(client, catalog.checked)
            await asyncio.to_thread(fence)
        except Exception:
            # The request has not been dispatched. Only these fresh intents can be released.
            for r in pending:
                row = await db.get(AtomerVariantRepair, keys[r.code], with_for_update=True, populate_existing=True)
                if row and row.state == 'sending':
                    await db.delete(row)
            await db.commit()
            raise
        try:
            await asyncio.to_thread(gen.send, [{'code': r.code, 'ean': r.ean} for r in pending], fp)
        except transport.SourceError as exc:
            if exc.request_sent is False:
                for r in pending:
                    row = await db.get(AtomerVariantRepair, keys[r.code], with_for_update=True, populate_existing=True)
                    await db.delete(row)
                await db.commit()
                raise
            # Ambiguous writes are observed; never dispatch them again.
        sent.extend(pending)
    if pending:
        after_catalog = await cards.load_shop_catalog('xtrek')
        _require(after_catalog.fingerprint == fp, 'missing_ean_connection_changed')
        await asyncio.to_thread(_ownership, after_catalog, s.items)
    final = await asyncio.to_thread(_targets, s.items, fp, bfp) if pending else remote
    stable_hub = await _scope(db, s, fp, bfp) if pending else local
    live_jobs = await _jobs(db, keys)
    for r in sent:
        row = live_jobs[r.code]
        doc = row.document
        confirmed = (_same_protected(doc['before'], final[r.code]) and doc['hub'] == stable_hub[r.code]
                     and final[r.code]['xtrek'].get('ean') == r.ean)
        row.state = 'verified' if confirmed else 'uncertain'
        receipt = dict(code=r.code, product_id=r.xtrek_id, card=r.card, ean=r.ean,
                       outcome='verified' if confirmed else 'readback_required')
        row.document = {**doc, 'observed': final[r.code], 'receipt': receipt}
        receipts.append(receipt)
    await db.commit()
    complete = all(r['outcome'] in ('already_present', 'verified') for r in receipts)
    return dict(status='verified' if complete else 'uncertain', count=len(s.items), rows=receipts,
                reason='' if complete else 'Natívny EAN alebo chránené údaje sa nepotvrdili. Opakovať možno iba čítanie.')
