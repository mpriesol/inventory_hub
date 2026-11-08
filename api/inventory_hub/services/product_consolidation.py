"""Reviewed identity-only consolidation, with no stock or remote mutations.

One complete retail unit remains one stock piece. Every former ID/SKU and all
stock/history rows remain on their original owners. Only explicitly reviewed
current native relationships are retained or rebound onto the selected keeper.
"""
from collections import defaultdict
from contextlib import nullcontext
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import DBAPIError
from inventory_hub.db_models import Product, ProductIdentifier, Shop, Warehouse
from inventory_hub.db_models_ext import ShopProduct
from inventory_hub.product_consolidation_models import ProductCanonicalAlias, ProductConsolidation
from inventory_hub.services import product_links as links
from inventory_hub.services import product_consolidation_dependencies as dependencies
from inventory_hub.services import product_consolidation_keeper_dependencies as keeper_dependencies
from inventory_hub.services import product_consolidation_supplier_identity as supplier_identity
from inventory_hub.services.canonical_products import aliases_for
from inventory_hub.services.product_identity import IDENTITY_WRITE_LOCK, RemoteIdentity, barcode_keys, load_identity_index
from inventory_hub.services.shared_stock_sales import _replacement_snapshot
from inventory_hub.services.stock_publication_gate import require_stock_write_allowed, StockPublicationHoldError


def _request(payload):
    raw = payload.model_dump(mode='json')
    # Preserve the request/hash shape of already frozen reviews and UUID replays.
    if not raw.get(keeper_dependencies.FLAG):
        raw.pop(keeper_dependencies.FLAG, None)
    if not raw.get(supplier_identity.FLAG):
        raw.pop(supplier_identity.FLAG, None)
    raw['retired_product_ids'].sort()
    raw['mapping_actions'].sort(key=lambda row: row['mapping_id'])
    raw['additional_sales'].sort(key=lambda row: (row['shop_code'], row['code']))
    return raw


def _ids(raw):
    return {raw['keeper_product_id'], *raw['retired_product_ids']}


def _targets(raw):
    return [row['target'] for row in raw['mapping_actions'] if row['target']] + raw['additional_sales']


def _key(target):
    return (target['shop_code'], target['is_variant'],
            str(target['native_variant_id'] if target['is_variant'] else target['native_product_id']))


def _snapshot(remote, *, supplier=False):
    value = {**links.remote_snapshot(remote), 'raw_barcodes': remote.get('raw_barcodes', []),
            'invalid_barcodes': remote.get('invalid_barcodes', [])}
    if supplier:
        value['supplier_code'] = remote.get('supplier_code')
    return value


async def _local(db, raw, *, lock=False):
    ids = sorted(_ids(raw))
    query = select(Product).where(Product.id.in_(ids)).order_by(Product.id)
    if lock:
        query = query.with_for_update()
    cards = list(await db.scalars(query.execution_options(populate_existing=True)))
    if len(cards) != len(ids):
        raise links.LinkError('consolidation_product_missing', 404)
    if any(card.item_type != 'stock' or card.stock_unit != 'pcs' for card in cards):
        raise links.LinkError('consolidation_whole_piece_cards_required')
    if not next(card for card in cards if card.id == raw['keeper_product_id']).is_active:
        raise links.LinkError('consolidation_keeper_inactive')
    if await aliases_for(db, ids):
        # Extending or reversing a previous consolidation needs a later design.
        raise links.LinkError('consolidation_existing_alias_group_unsupported')
    query = select(ShopProduct).where(ShopProduct.product_id.in_(ids)).order_by(ShopProduct.id)
    if lock:
        query = query.with_for_update()
    mappings = list(await db.scalars(query.execution_options(populate_existing=True)))
    if any(row.push_pending or row.shared_stock or row.sale_to_stock_factor != Decimal(1) for row in mappings):
        raise links.LinkError('consolidation_existing_mapping_mode_unsupported')
    if {row.id for row in mappings} != {row['mapping_id'] for row in raw['mapping_actions']}:
        raise links.LinkError('consolidation_existing_mapping_omitted')
    shop_ids = {row.shop_id for row in mappings}
    target_shops = {row['shop_code'] for row in _targets(raw)}
    shops = list(await db.scalars(select(Shop).where(or_(Shop.id.in_(shop_ids), Shop.code.in_(target_shops)))
                                  .order_by(Shop.id).execution_options(populate_existing=True)))
    if (not target_shops <= {row.code for row in shops}
            or any(not row.is_active or row.platform != 'upgates' for row in shops)):
        raise links.LinkError('consolidation_shop_unavailable')
    by_shop_id = {row.id: row.code for row in shops}
    # Legacy native claims survive030 as evidence. They can prove an exact
    # obsolete standalone claim absent, never establish current ownership.
    legacy = dict((await db.execute(text('''SELECT id,to_jsonb(sp)->>'external_id' AS legacy_id
        FROM shop_products sp WHERE sp.id=ANY(CAST(:ids AS bigint[]))'''),
        {'ids': [row.id for row in mappings]})).all()) if mappings else {}
    mapping_rows = [{**_replacement_snapshot(row), 'shop_code': by_shop_id[row.shop_id],
        'legacy_external_id': legacy.get(row.id)} for row in mappings]
    unverified_ids = {row['mapping_id'] for row in raw['mapping_actions']
                      if row['action'] == 'retire_unverified_code_absent'}
    if unverified_ids:
        # A missing JSON key is not SQL NULL. This action requires the physical
        # legacy column and a direct SQL null check; it never reconstructs it.
        present = await db.scalar(text('''SELECT EXISTS (SELECT 1 FROM pg_attribute
            WHERE attrelid='shop_products'::regclass AND attname='external_id'
              AND attnum>0 AND NOT attisdropped)'''))
        if not present:
            raise links.LinkError('consolidation_legacy_native_column_missing')
        nulls = dict((await db.execute(text('''SELECT id, external_id IS NULL
            FROM shop_products WHERE id=ANY(CAST(:ids AS bigint[]))'''),
            {'ids': sorted(unverified_ids)})).all())
        for item in mapping_rows:
            if item['id'] in unverified_ids:
                item['legacy_external_id_column_present'] = True
                item['legacy_external_id_sql_null'] = nulls.get(item['id']) is True
    identifiers = [{'id': row.id, 'product_id': row.product_id,
        'type': getattr(row.identifier_type, 'value', row.identifier_type), 'value': row.value,
        'supplier_id': row.supplier_id, 'is_primary': row.is_primary}
        for row in await db.scalars(select(ProductIdentifier).where(ProductIdentifier.product_id.in_(ids))
            .order_by(ProductIdentifier.id).execution_options(populate_existing=True))]
    # Installed migration032 rechecks raw scoped supplier ownership on replay.
    # Preserve that invariant without editing deployed SQL or moving aliases.
    # Supporting a retired supplier alias requires a later forward design.
    if any(item['type'] == 'supplier_sku' and item['product_id'] != raw['keeper_product_id']
           for item in identifiers):
        raise links.LinkError('consolidation_retired_supplier_alias_unsupported')
    facts = await links._facts(db, ids)
    product_rows = [{'id': card.id, 'sku': card.sku, 'name': card.name, 'is_active': card.is_active,
        'item_type': card.item_type, 'stock_unit': card.stock_unit, 'created_from_source': card.created_from_source,
        'identity': facts[card.id]} for card in cards]
    product_hash = await db.scalar(text('''SELECT md5(string_agg(to_jsonb(p)::text,E'\\n' ORDER BY p.id))
        FROM products p WHERE p.id=ANY(CAST(:ids AS bigint[]))'''), {'ids': ids})
    result = {'products': product_rows, 'product_hash': product_hash, 'identifiers': identifiers,
        'mappings': mapping_rows, 'shops': [{'id': shop.id, 'code': shop.code} for shop in shops]}
    if raw.get(supplier_identity.FLAG):
        if lock:
            await supplier_identity.lock_inputs(db)
        keeper = next(card for card in cards if card.id == raw['keeper_product_id'])
        result[supplier_identity.LOCAL_KEY] = await supplier_identity.snapshot(
            db, keeper, facts[keeper.id], identifiers, lock=lock)
    return result


def _previous_native(row):
    previous = deepcopy(row)
    if not previous['is_variant'] and previous['external_product_id'] is None:
        legacy = previous.get('legacy_external_id')
        if isinstance(legacy, str) and legacy.isascii() and legacy.isdigit() and 0 < int(legacy) <= 9007199254740991:
            previous['external_product_id'] = legacy
            previous['external_id'] = legacy
            previous['absence_basis'] = 'preserved_legacy_standalone_claim'
    if (not previous['external_product_id'] or previous['is_variant'] and not previous['external_variant_id']):
        raise links.LinkError('consolidation_old_native_identity_unverified')
    return previous


def _same_native(row, target):
    return (row['is_variant'] == target['is_variant']
        and row['external_product_id'] == str(target['native_product_id'])
        and row['external_variant_id'] == (str(target['native_variant_id']) if target['is_variant'] else None))


def _unverified_code_claim(row):
    code = row.get('external_code')
    if (not isinstance(code, str) or not code or code != code.strip() or len(code) > 100
            or ';' in code or any(ord(c) < 32 or ord(c) == 127 for c in code)
            or row['is_variant'] is not False or row['parent_code'] is not None
            or row['external_product_id'] is not None or row['external_variant_id'] is not None
            or 'legacy_external_id' not in row or row['legacy_external_id'] is not None
            or row.get('legacy_external_id_column_present') is not True
            or row.get('legacy_external_id_sql_null') is not True):
        raise links.LinkError('consolidation_unverified_standalone_claim_required')
    return {**row, 'old_native_identity_status': 'unverified', 'native_id_absence_proven': False}


def _plan(raw, local):
    originals = {row['id']: row for row in local['mappings']}
    absent, unverified, actions = defaultdict(list), defaultdict(list), []
    target_codes = {(row['shop_code'], row['code'].casefold()) for row in _targets(raw)}
    for action in raw['mapping_actions']:
        row, target = originals[action['mapping_id']], action['target']
        if action['action'] == 'retire_unverified_code_absent':
            claim = _unverified_code_claim(row)
            if (row['shop_code'], row['external_code'].casefold()) in target_codes:
                raise links.LinkError('consolidation_absent_code_selected_as_target')
            unverified[row['shop_code']].append(claim)
        elif target is None:
            absent[row['shop_code']].append(_previous_native(row))
        else:
            if row['shop_code'] != target['shop_code']:
                raise links.LinkError('consolidation_mapping_shop_changed')
            exact_code = row['external_code'] == target['code'] and row['is_variant'] == target['is_variant']
            exact_parent = row['parent_code'] == (target['parent_code'] if target['is_variant'] else None)
            same = _same_native(row, target)
            if action['action'] == 'retain':
                if row['product_id'] != raw['keeper_product_id'] or not same or not exact_code or not exact_parent:
                    raise links.LinkError('consolidation_retained_mapping_changed')
            elif same:
                if not exact_code or not exact_parent:
                    raise links.LinkError('consolidation_existing_native_identity_changed')
            elif row['external_product_id'] is None and row['external_variant_id'] is None:
                # A true null-ID mapping may bind only its unchanged exact
                # code/type/parent; discovery must return one such live leaf.
                if not exact_code or not exact_parent:
                    raise links.LinkError('consolidation_legacy_binding_changed')
            elif (row['is_variant'] and row['external_product_id'] is None
                    and row['external_variant_id'] == str(target['native_variant_id'])
                    and exact_code and exact_parent):
                pass  # Same pinned variant; the fresh source supplies its parent ID.
            else:
                absent[row['shop_code']].append(_previous_native(row))
        actions.append(action)
    plan = {'actions': actions, 'additional_sales': raw['additional_sales'], 'absence': dict(absent)}
    if unverified:
        plan['unverified_code_absence'] = dict(unverified)
    return plan


async def _native(raw, local, *, frozen=None):
    plan = _plan(raw, local)
    grouped = defaultdict(list)
    for target in _targets(raw):
        grouped[target['shop_code']].append(target)
    result = {}
    supplier = bool(raw.get(supplier_identity.FLAG))
    source_options = {'include_supplier_identity': True} if supplier else {}
    for shop in local['shops']:
        code = shop['code']
        expected_fingerprint = frozen[code]['fingerprint'] if frozen else None
        targets = grouped[code]
        if targets:
            if frozen:
                read = await links._source_call(links.source.verify(code, frozen[code]['leaves'],
                    expected_fingerprint, max_requests=links.MAX_REQUESTS, **source_options))
            else:
                read = await links._source_call(links.source.discover(code, [row['code'] for row in targets],
                    max_requests=links.MAX_REQUESTS, **source_options))
            leaves = []
            for target in targets:
                matches = [row for row in read['leaves'] if links.remote_key(row) == _key(target)[1:]
                    and row['code'] == target['code'] and row['parent_code'] == target['parent_code']
                    and row['parent_id'] == str(target['native_product_id'])]
                # Equal codes cannot silently choose a different parent/leaf.
                if len(matches) != 1 or len([row for row in read['leaves']
                                            if row['code'].casefold() == target['code'].casefold()]) != 1:
                    raise links.LinkError('consolidation_native_identity_ambiguous')
                leaves.append(matches[0])
            fingerprint, requests = read['fingerprint'], read['requests']
        else:
            fingerprint = await links._source_call(links.source.current_fingerprint(code))
            requests, leaves = 0, []
        if expected_fingerprint is not None and fingerprint != expected_fingerprint:
            raise links.LinkError('consolidation_shop_connection_changed')
        if frozen and [_snapshot(row, supplier=supplier) for row in leaves] != [
                _snapshot(row, supplier=supplier) for row in frozen[code]['leaves']]:
            raise links.LinkError('consolidation_native_identity_changed')
        old = plan['absence'].get(code, [])
        if old:
            proof = await links._verify_replacement_absence(code, old, fingerprint,
                max_requests=links.MAX_REQUESTS-requests)
            if proof['existing_mapping_ids']:
                raise links.LinkError('consolidation_original_native_still_exists')
            requests += proof['requests']
        result[code] = {'fingerprint': fingerprint, 'leaves': leaves, 'requests': requests,
            'absent_mapping_ids': [row['id'] for row in old]}
        unverified = plan.get('unverified_code_absence', {}).get(code, [])
        if unverified:
            proof = await links._source_call(links.source.verify_unoccupied_codes(code,
                [row['external_code'] for row in unverified], fingerprint,
                max_requests=links.MAX_REQUESTS-requests))
            result[code]['requests'] += proof['requests']
            result[code]['unverified_code_absence'] = {**proof,
                'mapping_ids': [row['id'] for row in unverified],
                'old_native_identity_status': 'unverified', 'native_id_absence_proven': False,
                'exact_code_absence_proven': True}
    return result


async def _identity(db, raw, local, native):
    ids = _ids(raw)
    keeper = next(row for row in local['products'] if row['id'] == raw['keeper_product_id'])
    gtins = barcode_keys(keeper['identity']['eans'])
    supplier_proof = supplier_identity.prove(raw, local, native) if raw.get(supplier_identity.FLAG) else None
    if supplier_proof is not None:
        gtins = {supplier_proof['verified_gtin_key']}
    if len(gtins) != 1:
        raise links.LinkError('consolidation_keeper_verified_gtin_required')
    axes = supplier_identity.physical_axes if supplier_proof is not None else links.axes
    physical_axes = {}
    for row in local['products']:
        if not barcode_keys(row['identity']['eans']) <= gtins:
            raise links.LinkError('consolidation_card_gtin_conflict')
        for axis, value in axes(row['identity']['attributes']).items():
            if axis in physical_axes and physical_axes[axis] != value:
                raise links.LinkError('consolidation_physical_axes_conflict')
            physical_axes[axis] = value
    raw_identifiers = {row['value'] for row in local['identifiers']}
    identities = [RemoteIdentity(0, code=row['sku'], expected_product_id=row['id'],
        barcodes=tuple(row['identity']['eans'])) for row in local['products']]
    identities += [RemoteIdentity(0, supplier_id=row['supplier_id'], supplier_sku=row['value'])
        for row in local['identifiers'] if row['type'] == 'supplier_sku']
    identities += [RemoteIdentity(0, code=row['value']) for row in local['identifiers'] if row['type'] == 'internal_sku']
    for observation in native.values():
        for leaf in observation['leaves']:
            if not barcode_keys(leaf['eans']) <= gtins:
                raise links.LinkError('consolidation_native_gtin_conflict')
            if set(leaf.get('invalid_barcodes', [])) - raw_identifiers:
                raise links.LinkError('consolidation_unregistered_legacy_barcode')
            for axis, value in axes(leaf['attributes']).items():
                if axis in physical_axes and physical_axes[axis] != value:
                    raise links.LinkError('consolidation_physical_axes_conflict')
                physical_axes[axis] = value
            identities.append(RemoteIdentity(0, code=leaf['code'], barcodes=tuple(leaf['eans'])))
    index = await load_identity_index(db, 0, identities, local=True)
    # Raw retained identifiers may only collapse through an existing audited
    # redirect. This group itself has no redirects before apply; every foreign
    # (including inactive) owner is still a hard conflict.
    if set(index.products) - ids:
        raise links.LinkError('consolidation_foreign_identity_owner')
    known_mapping_ids = {row['id'] for row in local['mappings']}
    shops = {row['code']: row['id'] for row in local['shops']}
    for target in _targets(raw):
        query = select(ShopProduct.id).where(ShopProduct.shop_id == shops[target['shop_code']], or_(
            func.lower(ShopProduct.external_code) == target['code'].lower(),
            (ShopProduct.is_variant == target['is_variant']) &
                (ShopProduct.native_id == _key(target)[2])))
        if set(await db.scalars(query)) - known_mapping_ids:
            raise links.LinkError('consolidation_foreign_native_owner')
    unverified_ids = {row['mapping_id'] for row in raw['mapping_actions']
                      if row['action'] == 'retire_unverified_code_absent'}
    for claim in local['mappings']:
        if claim['id'] not in unverified_ids:
            continue
        query = select(ShopProduct.id).where(ShopProduct.shop_id == shops[claim['shop_code']],
            func.lower(ShopProduct.external_code) == claim['external_code'].lower())
        if set(await db.scalars(query)) - known_mapping_ids:
            raise links.LinkError('consolidation_foreign_native_owner')
    result = {'verified_gtin_keys': sorted(gtins), 'physical_axes': physical_axes,
            'same_complete_retail_unit_factor': 1}
    if supplier_proof is not None:
        result[supplier_identity.PROOF_KEY] = supplier_proof
    return result


async def _dependencies(db, raw, local, *, keeper_inputs=None):
    preserved = await keeper_dependencies.inspect(db, raw, local, keeper_inputs) if keeper_inputs is not None else None
    result = await dependencies.inspect(db, ids=_ids(raw), keeper_id=raw['keeper_product_id'],
        products=local['products'], identifiers=local['identifiers'], mappings=local['mappings'],
        targets=_targets(raw), request_id=raw['request_id'], keeper_dependencies=preserved)
    if preserved is not None:
        result['keeper_dependencies'] = preserved
    result.pop('tokens')
    result['catalog_journals'] = result['facts']['typed_catalog_scope']['catalog_journals']
    return result


def public(row):
    local = row.document['local']
    plan = deepcopy(row.document['plan'])
    for name in ('absence', 'unverified_code_absence'):
        if name in plan:
            plan[name] = {shop: [{key: value for key, value in item.items() if not key.startswith('_')}
                                 for item in rows] for shop, rows in plan[name].items()}
    result = {'id': row.id, 'status': row.status, 'preview_hash': row.preview_hash,
        'expires_at': row.expires_at.isoformat(), 'eligible': not row.document['dependencies']['blockers'],
        'keeper_product_id': row.document['request']['keeper_product_id'],
        'retired_product_ids': row.document['request']['retired_product_ids'],
        'identity_evidence': row.document['request']['identity_evidence'],
        'products': local['products'],
        'existing_mappings': [{key: value for key, value in item.items() if not key.startswith('_')}
                              for item in local['mappings']],
        'plan': plan, 'native': row.document['native'],
        'dependencies': row.document['dependencies'], 'result': row.result}
    if row.document['request'].get(supplier_identity.FLAG):
        result[supplier_identity.PROOF_KEY] = deepcopy(row.document['identity'][supplier_identity.PROOF_KEY])
    return result


async def get(db, identifier):
    row = await db.get(ProductConsolidation, str(identifier))
    if row is None:
        raise links.LinkError('consolidation_not_found', 404)
    return public(row)


async def discard(db, identifier, payload):
    """Settle an exact unused review before preparing a replacement.

    The same identity/row fences as apply ensure that an in-flight application
    either completed first or will observe the committed expired state. Blockers,
    elapsed time and an obsolete parser version do not prevent discarding a review;
    its original proof must still be intact. Completed operations are only replayed.
    """
    await db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': IDENTITY_WRITE_LOCK})
    row = await db.scalar(select(ProductConsolidation).where(ProductConsolidation.id == str(identifier))
        .with_for_update().execution_options(populate_existing=True))
    if row is None:
        raise links.LinkError('consolidation_not_found', 404)
    if (payload.preview_hash != row.preview_hash or links.digest(row.document) != row.preview_hash
            or row.status not in ('prepared', 'expired', 'completed')):
        raise links.LinkError('consolidation_preview_changed')
    if row.status == 'prepared':
        row.status, row.expires_at = 'expired', min(row.expires_at, links.now())
    result = public(row)
    await db.commit()
    return result


async def preview(db, payload, *, actor=None):
    try:
        return await _preview(db, payload, actor=actor)
    except DBAPIError as error:
        state = getattr(error.orig, 'sqlstate', None)
        constraint = (getattr(error.orig, 'constraint_name', None)
            or getattr(getattr(error.orig, '__cause__', None), 'constraint_name', None))
        if state != '40001' and not (state == '23505' and constraint == 'product_consolidations_pkey'):
            raise
        await db.rollback()
        code = 'consolidation_dependency_snapshot_changed'
        if state == '23505':
            # The unique collision is only this review UUID. A fresh RC read
            # may replay the already committed identical review, never generate
            # a new preview or repeat any native request.
            row = await db.get(ProductConsolidation, str(payload.request_id), populate_existing=True)
            if row is not None:
                if row.request_hash == links.digest(_request(payload)):
                    result = public(row)
                    await db.commit()
                    return result
                code = 'consolidation_request_changed'
            else:
                code = 'consolidation_preview_retry_required'
            await db.rollback()
        failure = links.LinkError(code)
        failure.known_unapplied = True
        raise failure from None


async def _preview(db, payload, *, actor=None):
    raw = _request(payload)
    request_hash = links.digest(raw)
    existing = await db.get(ProductConsolidation, str(payload.request_id))
    if existing:
        if existing.request_hash != request_hash:
            raise links.LinkError('consolidation_request_changed')
        return public(existing)
    local = await _local(db, raw)
    plan = _plan(raw, local)
    await db.commit()
    native = await _native(raw, local)
    # This is the first statement of the post-native transaction. Every page
    # and linked read shares one snapshot; selected-row races fail explicitly.
    await db.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ'))
    await db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': IDENTITY_WRITE_LOCK})
    existing = await db.get(ProductConsolidation, str(payload.request_id), populate_existing=True)
    if existing:
        if existing.request_hash != request_hash:
            raise links.LinkError('consolidation_request_changed')
        return public(existing)
    enabled = raw.get(keeper_dependencies.FLAG, False)
    with keeper_dependencies.configurations() if enabled else nullcontext(None) as keeper_inputs:
        # No network occurs under these row locks. A preview never changes cards,
        # mapping ownership, stock flags, history or cached native content.
        current = await _local(db, raw, lock=True)
        if current != local:
            raise links.LinkError('consolidation_context_changed')
        if enabled:
            await keeper_dependencies.lock_inputs(db)
        identity = await _identity(db, raw, current, native)
        deps = await _dependencies(db, raw, current, keeper_inputs=keeper_inputs)
        document = {'request': raw, 'local': current, 'plan': plan, 'native': native,
            'identity': identity, 'dependencies': deps, 'created_by': actor or {'source': 'operator_token'}}
        row = ProductConsolidation(id=str(payload.request_id), request_hash=request_hash,
            preview_hash=links.digest(document), document=document, status='prepared',
            expires_at=links.now()+timedelta(minutes=links.PREVIEW_MINUTES))
        db.add(row)
        await db.flush()
        result = public(row)
        await db.commit()
        return result


def _check(row, payload, *, expired=False):
    if row is None:
        raise links.LinkError('consolidation_not_found', 404)
    if payload.preview_hash != row.preview_hash or links.digest(row.document) != row.preview_hash:
        raise links.LinkError('consolidation_preview_changed')
    if row.status == 'completed':
        return False
    version = row.document.get('dependencies', {}).get('facts', {}).get('typed_catalog_scope', {}).get('parser_version')
    if version != dependencies.semantic_scope.s.VERSION:
        raise links.LinkError('consolidation_dependency_inventory_version_changed')
    if row.status != 'prepared' or not expired and row.expires_at <= links.now():
        raise links.LinkError('consolidation_preview_expired')
    if row.document['dependencies']['blockers']:
        raise links.LinkError('consolidation_dependencies_blocked')
    return True


async def _lock_identity_owners(db):
    """Freeze foreign ownership after group row locks, without waiting.

    A foreign card is outside our Product row lock set. Table SHARE prevents a
    new SKU, GTIN, scoped supplier owner or native claim from appearing between
    the final scan and commit. NOWAIT is essential: an existing writer may
    already hold a table DML lock while waiting on one of our Product rows.
    Reads continue, and no HTTP happens while these short locks are held.
    """
    try:
        await db.execute(text('''LOCK TABLE products, product_identifiers, shop_products,
            product_supply_sources, product_sku_identities IN SHARE MODE NOWAIT'''))
        await dependencies.semantic_scope.lock_inputs(db)
    except DBAPIError as error:
        if getattr(error.orig, 'sqlstate', None) == '55P03':
            raise links.LinkError('consolidation_identity_writer_busy') from None
        raise


async def apply(db, identifier, payload, *, actor=None):
    row = await db.get(ProductConsolidation, str(identifier))
    if not _check(row, payload, expired=True):
        return public(row)
    try:
        return await _apply(db, identifier, payload, row, actor=actor)
    except links.LinkError as error:
        if error.code in ('consolidation_not_found', 'consolidation_preview_changed'):
            raise
        await db.rollback()
        await db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': IDENTITY_WRITE_LOCK})
        row = await db.scalar(select(ProductConsolidation).where(ProductConsolidation.id == str(identifier))
            .with_for_update().execution_options(populate_existing=True))
        if row and row.status == 'completed' and row.preview_hash == payload.preview_hash:
            result = public(row)
            await db.commit()
            return result
        if row and row.status == 'prepared' and row.preview_hash == payload.preview_hash:
            row.status, row.expires_at = 'expired', min(row.expires_at, links.now())
        await db.commit()
        error.known_unapplied = True
        raise


async def _apply(db, identifier, payload, row, *, actor=None):
    _check(row, payload)
    document = deepcopy(row.document)
    raw, local = document['request'], document['local']
    if raw.get(supplier_identity.FLAG) and await _local(db, raw) != local:
        raise links.LinkError('consolidation_context_changed')
    await db.commit()
    native = await _native(raw, local, frozen=document['native'])
    await db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': IDENTITY_WRITE_LOCK})
    row = await db.scalar(select(ProductConsolidation).where(ProductConsolidation.id == str(identifier))
        .with_for_update().execution_options(populate_existing=True))
    if not _check(row, payload):
        result = public(row)
        await db.commit()
        return result
    # Existing physical writers take warehouse then Product locks. Taking every
    # warehouse in stable order also covers cards with no prior balance row.
    warehouse_ids = list(await db.scalars(select(Warehouse.id).order_by(Warehouse.id)))
    for warehouse_id in warehouse_ids:
        try:
            await require_stock_write_allowed(db, warehouse_id)
        except StockPublicationHoldError as error:
            raise links.LinkError(error.code) from None
    current = await _local(db, raw, lock=True)
    if current != local:
        raise links.LinkError('consolidation_context_changed')
    await _lock_identity_owners(db)
    enabled = raw.get(keeper_dependencies.FLAG, False)
    if enabled:
        await keeper_dependencies.lock_inputs(db)
    with keeper_dependencies.configurations() if enabled else nullcontext(None) as keeper_inputs:
        if await _identity(db, raw, current, native) != document['identity']:
            raise links.LinkError('consolidation_identity_changed')
        deps = await _dependencies(db, raw, current, keeper_inputs=keeper_inputs)
        if deps['blockers'] or deps != document['dependencies']:
            raise links.LinkError('consolidation_dependencies_changed')
        for shop_code, observation in native.items():
            # Configuration-only read; no HTTP runs while DB locks are held.
            if await links._source_call(links.source.current_fingerprint(shop_code)) != observation['fingerprint']:
                raise links.LinkError('consolidation_shop_connection_changed')
        timestamp = links.now()
        originals = {item['id']: item for item in current['mappings']}
        targets = _targets(raw)
        counts = {shop['code']: sum(target['shop_code'] == shop['code'] for target in targets) for shop in local['shops']}
        shops = {shop['code']: shop['id'] for shop in local['shops']}
        observed = {(code, *links.remote_key(leaf)): leaf for code, result in native.items() for leaf in result['leaves']}
        current_rows, retired_mapping_ids = [], []
        # Retire obsolete mapping rows first; original owner, IDs, SKU, price and
        # native stock observations remain byte-for-byte, apart from audit stamps.
        for action in raw['mapping_actions']:
            mapping = await db.get(ShopProduct, action['mapping_id'])
            if action['action'] == 'retain':
                current_rows.append((mapping, action['target']))
            else:
                mapping.retired_at = timestamp
                mapping.retired_by_consolidation_id = row.id
                retired_mapping_ids.append(mapping.id)
        await db.flush()
        for action in raw['mapping_actions']:
            if action['action'] != 'bind':
                continue
            target = action['target']
            # Fresh identity reads contain no price/quantity observations. Preserve
            # those on the retired row; never copy them to a recreated native leaf.
            mapping = ShopProduct(shop_id=shops[target['shop_code']], product_id=raw['keeper_product_id'])
            db.add(mapping)
            current_rows.append((mapping, target))
        for target in raw['additional_sales']:
            mapping = ShopProduct(shop_id=shops[target['shop_code']], product_id=raw['keeper_product_id'])
            db.add(mapping)
            current_rows.append((mapping, target))
        for mapping, target in current_rows:
            leaf = observed[_key(target)]
            mapping.external_product_id = leaf['parent_id']
            mapping.external_variant_id = leaf['external_id'] if leaf['is_variant'] else None
            mapping.external_code, mapping.is_variant = leaf['code'], leaf['is_variant']
            mapping.parent_code = leaf['parent_code'] if leaf['is_variant'] else None
            mapping.is_listed = True
            mapping.shared_stock = counts[target['shop_code']] > 1
            mapping.sale_to_stock_factor = Decimal(1)
            mapping.sale_barcodes = leaf['eans'] if mapping.shared_stock else None
        await db.flush()
        for product_id in raw['retired_product_ids']:
            product = await db.get(Product, product_id)
            product.is_active = False
        await db.flush()
        db.add_all(ProductCanonicalAlias(retired_product_id=product_id,
            canonical_product_id=raw['keeper_product_id'], consolidation_id=row.id)
            for product_id in raw['retired_product_ids'])
        await db.flush()
        if keeper_inputs is not None:
            after = await keeper_dependencies.inspect(db, raw, current, keeper_inputs)
            if after != deps['keeper_dependencies']:
                raise links.LinkError('consolidation_keeper_dependencies_changed')
        # Verify original stock/history hashes without any canonical aggregation.
        for key, before in deps['facts'].items():
            if key.startswith(('stock_', 'fifo_', 'reservations.', 'shop_order_', 'receiving_', 'inventory_count_')) and '.' in key:
                table, column = key.split('.', 1)
                after = await dependencies._summary(db, table,
                    f't.{dependencies.quoted(column)}=ANY(CAST(:ids AS bigint[]))', {'ids': sorted(_ids(raw))})
                if after != before:
                    raise links.LinkError('consolidation_history_changed')
        for target in targets:
            owners = list(await db.scalars(select(ShopProduct).where(ShopProduct.shop_id == shops[target['shop_code']],
                ShopProduct.is_variant == target['is_variant'], ShopProduct.native_id == _key(target)[2])))
            if len(owners) != 1 or owners[0].product_id != raw['keeper_product_id']:
                raise links.LinkError('consolidation_result_ownership_changed')
        row.status, row.completed_at = 'completed', timestamp
        row.result = {'keeper_product_id': raw['keeper_product_id'], 'retired_product_ids': raw['retired_product_ids'],
            'retired_mapping_ids': retired_mapping_ids,
            'current_mappings': [{**links._mapping(mapping), 'product_id': mapping.product_id,
                'shop_code': target['shop_code'], 'shared_stock': mapping.shared_stock, 'sale_to_stock_factor': 1}
                for mapping, target in current_rows],
            'history_hash_unchanged': deps['history_hash'], 'physical_stock_changed': False,
            'remote_write_performed': False, 'applied_by': actor or {'source': 'operator_token'}}
        unverified_proofs = {code: observation['unverified_code_absence'] for code, observation in native.items()
                             if 'unverified_code_absence' in observation}
        if unverified_proofs:
            # Keep apply's fresh negative code observations apart from the frozen
            # preview. Neither observation claims an unknown native ID was deleted.
            row.result = {**row.result, 'unverified_code_absence': unverified_proofs}
        if keeper_inputs is not None:
            keeper_inputs.verify()
            row.result['keeper_dependencies_hash'] = deps['keeper_dependencies']['preserved_rows_hash']
        await db.flush()
        result = public(row)
        await db.commit()
        return result
