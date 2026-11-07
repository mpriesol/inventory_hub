"""Reviewed same-native-ID parent-code repair, with no import or cache writes.

Only ordinary, already verified mappings are supported. The sole business
column updated is parent_code; PostgreSQL retains its normal updated_at audit
trigger. Existing link UUIDs, frozen previews and failed-apply fences are reused.
"""
from copy import copy
from datetime import timedelta
import time

from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import DBAPIError

from inventory_hub.db_models import Product
from inventory_hub.db_models_ext import ShopProduct
from inventory_hub.product_link_models import ProductLinkPreview
from inventory_hub.services import product_links as links, product_link_recovery as recovery
from inventory_hub.services.product_identity import IDENTITY_WRITE_LOCK, RemoteIdentity, barcode_keys, load_identity_index


OPERATION = 'reviewed_parent_rename'
MAX_OBSERVATION_SECONDS = 60


def _need(condition, code='product_links_parent_rename_changed'):
    if not condition:
        raise links.LinkError(code)


def _fresh(observed):
    started = observed.get('observation_started_monotonic')
    _need(type(started) in (float, int) and 0 <= time.monotonic() - started <= MAX_OBSERVATION_SECONDS,
          'product_links_source_observation_expired')


def _manual(payload):
    return {item.product_id: item.model_dump(mode='json') for item in payload.manual_matches}


def _families(manual):
    result = {}
    for item in manual.values():
        review = item['reviewed_parent_rename']
        key = str(review['native_product_id'])
        value = {'parent_id': key, 'parent_code': item['parent_code'],
                 'expected_parent_code': review['expected_parent_code']}
        _need(key not in result or result[key] == value, 'product_links_parent_family_changed')
        result[key] = value
    _need(len({v['expected_parent_code'] for v in result.values()}) == len(result)
          and len({v['parent_code'] for v in result.values()}) == len(result),
          'product_links_parent_family_changed')
    return result


def _remote_snapshot(leaf):
    return {**links.remote_snapshot(leaf),
            'raw_barcodes': sorted(leaf.get('raw_barcodes', [])),
            'invalid_barcodes': sorted(leaf.get('invalid_barcodes', []))}


def _observation(manual, observed):
    families = _families(manual)
    leaves = observed['family_leaves']
    _need(len({leaf['external_id'] for leaf in leaves}) == len(leaves), 'source_duplicate_variant_id')
    _need(len({leaf['code'].casefold() for leaf in leaves}) == len(leaves), 'ambiguous_remote_match')
    by_native = {leaf['external_id']: leaf for leaf in leaves}
    _need({leaf['parent_id'] for leaf in leaves} == set(families), 'source_parent_family_incomplete')
    for leaf in leaves:
        _need(leaf['is_variant'] is True and leaf['parent_code'] == families[leaf['parent_id']]['parent_code'],
              'source_parent_changed')
    occupied = observed['occupied_codes']
    for family in families.values():
        found = [v for v in occupied if v['code'].casefold() == family['parent_code'].casefold()]
        _need(len(found) == 1 and found[0]['code'] == family['parent_code'] and found[0]['kind'] == 'parent'
              and found[0]['native_id'] == family['parent_id'], 'product_links_parent_code_conflict')
        _need(not any(v['code'].casefold() == family['expected_parent_code'].casefold() for v in occupied),
              'product_links_parent_code_conflict')
    selected = {}
    for pid, item in manual.items():
        review = item['reviewed_parent_rename']
        remote = by_native.get(str(review['native_variant_id']))
        _need(remote is not None and remote['parent_id'] == str(review['native_product_id'])
              and remote['code'] == item['code'] and remote['parent_code'] == item['parent_code'],
              'product_links_recovery_pin_changed')
        code_occupancy = [v for v in occupied if v['code'].casefold() == item['code'].casefold()]
        _need(len(code_occupancy) == 1 and code_occupancy[0]['kind'] == 'variant'
              and code_occupancy[0]['code'] == item['code']
              and code_occupancy[0]['native_id'] == remote['external_id']
              and code_occupancy[0]['parent_id'] == remote['parent_id']
              and code_occupancy[0]['parent_code'] == remote['parent_code'], 'ambiguous_remote_match')
        exact = [v for v in observed['leaves'] if v['code'] == item['code']]
        _need(len(exact) == 1 and _remote_snapshot(exact[0]) == _remote_snapshot(remote),
              'product_links_remote_changed')
        _need(bool(remote['eans']) and not remote.get('invalid_barcodes'),
              'product_links_parent_rename_ean_unverified')
        selected[pid] = exact[0]
    proof = {'fingerprint': observed['fingerprint'],
             'families': sorted(families.values(), key=lambda v: v['parent_id']),
             'native_leaves': sorted((_remote_snapshot(v) for v in leaves),
                                     key=lambda v: (v['parent_id'], v['external_id']))}
    return selected, proof


def _identity(shop_id, mapping, remote, parent_code):
    return RemoteIdentity(shop_id, remote['code'], True, remote['external_id'], parent_code,
                          tuple(remote['eans']), expected_product_id=mapping.product_id,
                          external_product_id=remote['parent_id'])


async def _raw_mappings(db, ids):
    return list((await db.execute(text('''SELECT to_jsonb(sp) AS row FROM shop_products sp
        WHERE sp.id=ANY(CAST(:ids AS bigint[])) ORDER BY sp.id'''), {'ids': sorted(ids)})).scalars())


async def _local(db, shop, manual, selected, observed):
    """Freeze the whole local family; NOWAIT prevents lock-order inversions."""
    families = _families(manual)
    codes = {v[key].casefold() for v in families.values() for key in ('parent_code', 'expected_parent_code')}
    # Identity writers already serialize on the advisory lock. This additional
    # table fence also prevents an uncoordinated sibling/owner phantom. Row
    # lockers are compatible with this table mode, so every row lock is NOWAIT.
    await db.execute(text('LOCK TABLE shop_products IN SHARE ROW EXCLUSIVE MODE NOWAIT'))
    await db.execute(select(Product.id).where(Product.id.in_(manual)).order_by(Product.id)
                     .with_for_update(read=True, nowait=True))
    _fresh(observed)
    rows = list(await db.scalars(select(ShopProduct).where(ShopProduct.shop_id == shop.id,
        ShopProduct.retired_at.is_(None), or_(
            ShopProduct.is_variant.is_(True) & ShopProduct.external_product_id.in_(families),
            func.lower(ShopProduct.parent_code).in_(codes),
            ShopProduct.is_variant.is_(False) & func.lower(ShopProduct.external_code).in_(codes)))
        .order_by(ShopProduct.id).with_for_update(nowait=True).execution_options(populate_existing=True)))
    _need(bool(rows) and len(rows) <= links.source.MAX_LEAVES, 'product_links_parent_family_changed')
    by_id = {row.id: row for row in rows}
    selected_ids = {item['reviewed_parent_rename']['mapping_id'] for item in manual.values()}
    _need(len(selected_ids) == len(manual), 'product_links_parent_family_changed')
    for pid, item in manual.items():
        review, remote = item['reviewed_parent_rename'], selected[pid]
        mapping = by_id.get(review['mapping_id'])
        recovery.pin_target(review, remote, mapping)
        _need(mapping.product_id == pid and mapping.shop_id == shop.id and mapping.is_listed
              and mapping.is_variant and recovery.ordinary(mapping)
              and mapping.external_product_id == str(review['native_product_id'])
              and mapping.external_variant_id == str(review['native_variant_id'])
              and mapping.external_code == item['code'] and mapping.parent_code == review['expected_parent_code'],
              'product_links_parent_rename_not_allowed')
    # Every stale mapping in the named old family must be selected. Existing
    # correct siblings remain in the full frozen proof and are never rewritten.
    old_rows = {row.id for row in rows if row.parent_code is not None
                and row.parent_code.casefold() in {v['expected_parent_code'].casefold() for v in families.values()}}
    _need(old_rows == selected_ids, 'product_links_parent_family_incomplete')
    by_native = {leaf['external_id']: leaf for leaf in observed['family_leaves']}
    identities = []
    for mapping in rows:
        remote = by_native.get(mapping.external_variant_id)
        family = families.get(mapping.external_product_id)
        _need(family is not None and remote is not None and mapping.is_variant
              and mapping.external_product_id == remote['parent_id'] and mapping.external_code == remote['code']
              and mapping.parent_code in (family['parent_code'], family['expected_parent_code']),
              'product_links_parent_family_changed')
        identities.append(_identity(shop.id, mapping, remote, mapping.parent_code))
    index = await load_identity_index(db, shop.id, identities)
    for mapping, identity in zip(rows, identities):
        resolution = index.resolve(identity)
        _need(resolution.status == 'mapped' and resolution.product_id == mapping.product_id
              and resolution.mapping_id == mapping.id, 'product_links_parent_rename_owner_conflict')
    # Validate the proposed parent spelling without discarding any mapping or
    # owner evidence. Only separately proven mappings receive an in-memory copy.
    prospective = index.without_mappings(selected_ids)
    for mapping in rows:
        if mapping.id in selected_ids:
            replacement = copy(mapping)
            replacement.__dict__ = dict(mapping.__dict__)
            replacement.__dict__['parent_code'] = families[mapping.external_product_id]['parent_code']
            prospective.add_mapping(replacement)
    for mapping in rows:
        remote = by_native[mapping.external_variant_id]
        resolution = prospective.resolve(_identity(shop.id, mapping, remote, remote['parent_code']))
        _need(resolution.status == 'mapped' and resolution.product_id == mapping.product_id
              and resolution.mapping_id == mapping.id, 'product_links_parent_rename_owner_conflict')
    facts = await links._facts(db, list(manual))
    for pid, remote in selected.items():
        fact = facts[pid]
        _need(fact['is_active'], 'inactive_product')
        incoming = barcode_keys(remote['eans'])
        for values in (fact['eans'], fact['manual_eans']):
            known = barcode_keys(values or [])
            _need(not known or incoming <= known, 'product_links_parent_rename_ean_conflict')
        local_axes, native_axes = links.axes(fact['attributes']), links.axes(remote['attributes'])
        _need(not any(local_axes[k] != native_axes[k] for k in local_axes.keys() & native_axes.keys()),
              'variant_axes_conflict')
        mapping = by_id[manual[pid]['reviewed_parent_rename']['mapping_id']]
        await links._require_recovery_order_idle(db, shop, pid, 'product_links_identity_in_use')
        await links._require_mapping_write_available(db, shop, mapping,
            {mapping.parent_code, remote['parent_code'], remote['code']}, 'product_links_identity_in_use')
    proof = {'mappings': await _raw_mappings(db, by_id),
             'local_identity_hashes': {str(pid): links._facts_hash(fact) for pid, fact in facts.items()}}
    return facts, proof


async def _locked_local(*args):
    try:
        return await _local(*args)
    except DBAPIError as error:
        if getattr(error.orig, 'sqlstate', None) == '55P03':
            raise links.LinkError('product_links_parent_rename_busy') from None
        raise


async def _observe(shop_code, manual, fingerprint=None):
    families = _families(manual)
    codes = {item['code'] for item in manual.values()} | {
        family[key] for family in families.values() for key in ('parent_code', 'expected_parent_code')}
    observed = await links._source_call(links.source.inspect_parent_rename(shop_code, sorted(codes),
        sorted(families), fingerprint, max_requests=links.MAX_REQUESTS))
    _fresh(observed)
    return observed


async def preview(db, payload, request_hash, *, actor=None):
    manual = _manual(payload)
    shop = await links._shop(db, payload.shop_code)
    shop_id = shop.id
    await db.commit()
    observed = await _observe(payload.shop_code, manual)
    selected, remote_proof = _observation(manual, observed)
    await db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': IDENTITY_WRITE_LOCK})
    existing = await db.get(ProductLinkPreview, str(payload.request_id), populate_existing=True)
    if existing is not None:
        _need(existing.request_hash == request_hash, 'product_links_request_changed')
        return links.public(existing)
    shop = await links._shop(db, payload.shop_code)
    _need(shop.id == shop_id, 'product_links_identity_changed')
    facts, local_proof = await _locked_local(db, shop, manual, selected, observed)
    _need(await links._source_call(links.source.current_fingerprint(shop.code)) == observed['fingerprint'],
          'source_connection_changed')
    _fresh(observed)
    rows = [{'product_id':pid, 'sku':facts[pid]['sku'], 'name':facts[pid]['name'],
             'status':'ready', 'match_by':'same_native_id_parent_rename', 'remote':selected[pid],
             'local_eans':facts[pid]['eans'], 'warnings':['renames_parent_metadata_only'], 'errors':[],
             'parent_rename':{'request':manual[pid], 'family_product_ids':sorted(manual)}} for pid in sorted(manual)]
    document = {'operation':OPERATION, 'shop_code':shop.code, 'fingerprint':observed['fingerprint'], 'rows':rows,
                'remote_proof':remote_proof, 'local_proof':local_proof, 'remote_requests':observed['requests'],
                'created_by':actor or {'source':'operator_token'}}
    at = links.now()
    row = ProductLinkPreview(id=str(payload.request_id), shop_id=shop.id, request_hash=request_hash,
        preview_hash=links.digest(document), document=document, status='prepared', created_at=at,
        expires_at=at + timedelta(minutes=links.PREVIEW_MINUTES))
    db.add(row)
    await db.flush()
    _fresh(observed)
    result = links.public(row)
    await db.commit()
    return result


async def apply_prepared(db, identifier, payload, row, *, actor=None):
    selected_rows = links._apply_selection(row, payload)
    manual = {item['product_id']:item['parent_rename']['request'] for item in selected_rows}
    shop_code, fingerprint = row.document['shop_code'], row.document['fingerprint']
    frozen_remote, frozen_local = row.document['remote_proof'], row.document['local_proof']
    await db.commit()
    observed = await _observe(shop_code, manual, fingerprint)
    selected, remote_proof = _observation(manual, observed)
    _need(remote_proof == frozen_remote, 'product_links_remote_changed')
    await db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': IDENTITY_WRITE_LOCK})
    row = await db.scalar(select(ProductLinkPreview).where(ProductLinkPreview.id == str(identifier))
                          .with_for_update().execution_options(populate_existing=True))
    _need(row is not None, 'product_links_not_found')
    if links._apply_selection(row, payload) is None:
        return links.public(row)
    shop = await links._shop(db, shop_code)
    _need(shop.id == row.shop_id, 'product_links_identity_changed')
    _, local_proof = await _locked_local(db, shop, manual, selected, observed)
    _need(local_proof == frozen_local, 'product_links_parent_rename_changed')
    _need(await links._source_call(links.source.current_fingerprint(shop_code)) == fingerprint,
          'source_connection_changed')
    _fresh(observed)
    before = {item['id']:item for item in local_proof['mappings']}
    changed = {}
    for pid, item in sorted(manual.items()):
        review = item['reviewed_parent_rename']
        result = await db.execute(text('''UPDATE shop_products SET parent_code=:parent
            WHERE id=:id AND shop_id=:shop AND product_id=:product AND retired_at IS NULL
              AND is_variant AND external_product_id=:native_parent AND external_variant_id=:native_variant
              AND external_code=:code AND parent_code=:previous RETURNING id'''), {
            'parent':item['parent_code'], 'id':review['mapping_id'], 'shop':shop.id, 'product':pid,
            'native_parent':str(review['native_product_id']), 'native_variant':str(review['native_variant_id']),
            'code':item['code'], 'previous':review['expected_parent_code']})
        _need(result.scalar_one_or_none() == review['mapping_id'], 'product_links_parent_rename_changed')
        changed[review['mapping_id']] = item['parent_code']
    after = await _raw_mappings(db, before)
    for current in after:
        previous = before[current['id']]
        expected = {**previous, 'parent_code':changed[current['id']], 'updated_at':current['updated_at']} \
            if current['id'] in changed else previous
        _need(current == expected, 'product_links_parent_rename_unexpected_write')
    _need(len(after) == len(before), 'product_links_parent_rename_unexpected_write')
    _fresh(observed)
    row.status, row.completed_at = 'completed', links.now()
    row.result = {'product_ids':sorted(manual), 'linked_product_ids':[], 'already_linked_product_ids':[],
        'parent_renamed_product_ids':sorted(manual), 'completed_at':row.completed_at.isoformat(),
        'applied_by':actor or {'source':'operator_token'}, 'remote_requests':observed['requests'],
        'mapping_before_hash':links.digest(local_proof['mappings']), 'mapping_after_hash':links.digest(after),
        'renamed_mappings':[{'mapping_id':item['id'], 'product_id':item['product_id'],
            'previous_parent_code':before[item['id']]['parent_code'], 'parent_code':item['parent_code'],
            'previous_updated_at':before[item['id']]['updated_at'], 'updated_at':item['updated_at']}
            for item in after if item['id'] in changed]}
    await db.flush()
    _fresh(observed)
    result = links.public(row)
    await db.commit()
    return result
