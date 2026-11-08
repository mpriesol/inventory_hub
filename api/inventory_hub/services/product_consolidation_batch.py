"""One explicitly reviewed, atomic DB-only identity migration.

This separate operator route accepts retained complete-catalog evidence. It
does not claim a fresh native observation or a clear global publication graph.
The ordinary interactive consolidation admission remains unchanged.
"""
import asyncio
import json
from copy import deepcopy
from datetime import timedelta
from uuid import UUID, uuid5

from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from inventory_hub.db_models import Product, Warehouse
from inventory_hub.product_consolidation_models import ProductConsolidation
from inventory_hub.product_consolidation_types import ProductConsolidationPreviewRequest
from inventory_hub.product_consolidation_batch_types import BatchPreviewRequest
from inventory_hub.services import product_consolidation as core
from inventory_hub.services import product_links as links
from inventory_hub.services import atomer_catalog_control as campaigns
from inventory_hub.services import atomer_catalog_job_control as job_controls
from inventory_hub.services import atomer_catalog_source_cards as source_cards
from inventory_hub.services.atomer_review_targets import ReviewError
from inventory_hub.services.stock_publication_gate import require_stock_write_allowed, StockPublicationHoldError


VERSION = 'one-off-product-consolidation-v1'
MAX_BYTES = 5_000_000
MAX_SECONDS = 180
MAX_HISTORY_ROWS = 100_000
MAX_CATALOG_AGE = timedelta(hours=4)
META = 'one_off_batch'
TABLES = {
    'products', 'product_consolidations', 'product_canonical_aliases', 'shops',
    'supplier_products', 'suppliers', 'supplier_feeds', 'shop_product_content',
    'atomer_catalog_campaigns', 'atomer_catalog_jobs', 'atomer_catalog_proofs',
    'atomer_catalog_source_cards', 'atomer_catalog_category_restores',
    'stock_sync_settings', 'stock_sync_items', 'stock_publication_items',
    'fifo_cost_publications', 'ai_content_jobs', 'product_import_drafts',
    'receiving_sessions', 'uploaded_invoices', 'stock_publication_holds',
}
FORBIDDEN_STOCK = {
    'stock_tracking', 'fifo_layers', 'fifo_states', 'fifo_cutovers', 'fifo_receipts',
    'fifo_allocations', 'fifo_returns', 'fifo_releases', 'reservations',
    'shop_order_items', 'stock_native_lines', 'stock_native_events', 'stock_sync_checkpoints',
}
GUARDS = {
    ('product_consolidations', 'trg_consolidation_audit_guard'): ('hub_consolidation_audit_guard', False),
    ('product_canonical_aliases', 'trg_canonical_alias_guard'): ('hub_canonical_alias_guard', False),
    ('product_canonical_aliases', 'trg_canonical_alias_complete_guard'): ('hub_canonical_alias_complete_guard', True),
    ('products', 'trg_retired_product_guard'): ('hub_retired_product_guard', False),
    ('products', 'trg_shared_card_unit_guard'): ('hub_shared_card_unit_guard', False),
    ('shop_products', 'trg_retired_mapping_guard'): ('hub_retired_mapping_guard', False),
    ('shop_products', 'trg_shared_sale_group_guard'): ('hub_shared_sale_group_guard', True),
    ('shop_products', 'trg_shared_sale_history_guard'): ('hub_shared_sale_history_guard', False),
}


def _need(condition, code):
    if not condition:
        raise links.LinkError(code)


def _child_id(batch_id, raw):
    return str(uuid5(UUID(str(batch_id)), 'consolidation/' + str(raw['keeper_product_id']) + '/'
        + ','.join(map(str, sorted(raw['retired_product_ids'])))))


def _request(payload):
    raw = payload.model_dump(mode='json')
    groups = []
    for group in payload.groups:
        data = group.model_dump(mode='json', exclude={'request_id'})
        data['request_id'] = _child_id(payload.request_id, data)
        groups.append(core._request(ProductConsolidationPreviewRequest(**data)))
    raw['groups'] = sorted(groups, key=lambda g: (g['keeper_product_id'], g['retired_product_ids']))
    for catalog in raw['native_catalogs']:
        catalog['leaves'].sort(key=lambda leaf: (leaf['is_variant'], leaf['external_id']))
        catalog['absent_native'].sort(key=lambda leaf: (leaf['is_variant'], leaf['parent_id'], leaf['external_id']))
        catalog['absent_codes'].sort()
    raw['native_catalogs'].sort(key=lambda catalog: catalog['shop_code'])
    _need(len(json.dumps(raw, ensure_ascii=False).encode()) <= MAX_BYTES,
          'consolidation_batch_payload_limit')
    return raw


def _absence(row):
    return (row['is_variant'], str(row['external_product_id']),
            str(row['external_variant_id'] if row['is_variant'] else row['external_product_id']))


def _native(raw, groups):
    catalogs = {catalog['shop_code']: catalog for catalog in raw['native_catalogs']}
    wanted = {shop['code'] for group in groups for shop in group['local']['shops']}
    _need(set(catalogs) == wanted, 'consolidation_batch_catalog_scope_changed')
    used, absent, codes = {k: set() for k in catalogs}, {k: set() for k in catalogs}, {k: set() for k in catalogs}
    for group in groups:
        selected = {}
        targets = core._targets(group['request'])
        for shop in group['local']['shops']:
            code, catalog = shop['code'], catalogs[shop['code']]
            leaves = []
            for target in [t for t in targets if t['shop_code'] == code]:
                matches = [leaf for leaf in catalog['leaves'] if (leaf['is_variant'], leaf['external_id']) == core._key(target)[1:]
                    and leaf['parent_id'] == str(target['native_product_id'])
                    and leaf['code'] == target['code'] and leaf['parent_code'] == target['parent_code']]
                _need(len(matches) == 1, 'consolidation_batch_native_identity_changed')
                leaf = matches[0]
                effective = deepcopy(leaf)
                if leaf.get('reviewed_attributes') is not None:
                    effective['attributes'] = deepcopy(leaf['reviewed_attributes'])
                    effective['attributes_basis'] = 'operator_reviewed_retained_evidence'
                    effective['catalog_attributes'] = deepcopy(leaf['attributes'])
                else:
                    effective['attributes_basis'] = 'operator_reviewed_catalog_snapshot'
                leaves.append(effective)
                used[code].add((leaf['is_variant'], leaf['external_id']))
            old = group['plan']['absence'].get(code, [])
            absent[code].update(_absence(row) for row in old)
            unverified = group['plan'].get('unverified_code_absence', {}).get(code, [])
            codes[code].update(row['external_code'] for row in unverified)
            selected[code] = dict(fingerprint=catalog['fingerprint'], leaves=leaves, requests=0,
                absent_mapping_ids=[row['id'] for row in old],
                evidence_basis='operator_reviewed_unfiltered_products_export',
                captured_at=catalog['captured_at'], export_sha256=catalog['export_sha256'])
            if unverified:
                selected[code]['unverified_code_absence'] = dict(codes=sorted(row['external_code'] for row in unverified),
                    mapping_ids=sorted(row['id'] for row in unverified), requests=0,
                    evidence_basis='operator_reviewed_unfiltered_products_export',
                    old_native_identity_status='unverified', native_id_absence_proven=False)
        group['native'] = selected
    for code, catalog in catalogs.items():
        _need(used[code] == {(leaf['is_variant'], leaf['external_id']) for leaf in catalog['leaves']}
            and absent[code] == {(leaf['is_variant'], leaf['parent_id'], leaf['external_id']) for leaf in catalog['absent_native']}
            and codes[code] == set(catalog['absent_codes']), 'consolidation_batch_catalog_scope_changed')


async def _references(db):
    rows = [dict(row) for row in (await db.execute(text('''SELECT c.conrelid::regclass::text AS table_name,
        a.attname AS column_name,cardinality(c.conkey) AS arity,
        EXISTS(SELECT 1 FROM pg_trigger tr WHERE tr.tgrelid=c.conrelid
          AND tr.tgname='trg_retired_reference_' || a.attname AND tr.tgenabled IN ('O','A')
          AND tr.tgfoid='hub_retired_reference_guard'::regproc) AS guarded
        FROM pg_constraint c JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=c.conkey[1]
        WHERE c.contype='f' AND c.confrelid='products'::regclass
        ORDER BY c.conrelid::regclass::text,a.attname'''))).mappings()]
    _need(bool(rows), 'consolidation_batch_schema_incomplete')
    for row in rows:
        if row['table_name'] == 'product_canonical_aliases':
            continue
        _need(row['arity'] == 1 and row['guarded']
            and bool(core.dependencies.IDENTIFIER.fullmatch(row['table_name']))
            and bool(core.dependencies.IDENTIFIER.fullmatch(row['column_name'])),
            'consolidation_batch_reference_guard_missing')
    return rows


async def _guards(db):
    rows = [dict(row) for row in (await db.execute(text('''SELECT c.relname AS table_name,
        t.tgname AS trigger_name,p.proname AS function_name,t.tgdeferrable AS deferred,
        t.tginitdeferred AS initially_deferred,pg_get_triggerdef(t.oid) AS definition
        FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_proc p ON p.oid=t.tgfoid
        JOIN pg_namespace pn ON pn.oid=p.pronamespace
        WHERE n.nspname=current_schema() AND pn.nspname=current_schema()
          AND t.tgenabled IN ('O','A') AND t.tgname=ANY(CAST(:names AS text[]))
        ORDER BY c.relname,t.tgname'''), {'names': [name for _, name in GUARDS]})).mappings()]
    present = {(row['table_name'], row['trigger_name']): (row['function_name'], row['deferred']) for row in rows}
    _need(present == GUARDS and all(not row['deferred'] or row['initially_deferred'] for row in rows),
          'consolidation_batch_schema_guard_missing')
    return rows


async def _summary(db, table, condition, params, *, omit=()):
    name = core.dependencies.quoted(table)
    expression = 'to_jsonb(t)' + ''.join("-'" + key + "'" for key in omit)
    # Only fixed-size hashes are aggregated; original row payloads stay in DB.
    row_hash = f"encode(sha256(convert_to(({expression})::text,'UTF8')),'hex')"
    row = dict((await db.execute(text(f'''SELECT count(*) AS count,
        CASE WHEN count(*)<=:limit THEN encode(sha256(convert_to(coalesce(
          string_agg({row_hash},E'\\n' ORDER BY {row_hash}),''),'UTF8')),'hex') END AS hash
        FROM {name} t WHERE {condition}'''), {**params, 'limit': MAX_HISTORY_ROWS})).mappings().one())
    _need(row['count'] <= MAX_HISTORY_ROWS, 'consolidation_batch_history_limit')
    return row


async def _history(db, raw, refs):
    ids = sorted(pid for group in raw['groups'] for pid in core._ids(group))
    params = {'ids': ids}
    result = {}
    for ref in refs:
        table, column = ref['table_name'], ref['column_name']
        if table in ('shop_products', 'product_canonical_aliases'):
            continue
        result[table + '.' + column] = await _summary(db, table,
            f't.{core.dependencies.quoted(column)}=ANY(CAST(:ids AS bigint[]))', params)
    result['products.preserved'] = await _summary(db, 'products', 't.id=ANY(CAST(:ids AS bigint[]))', params,
        omit=('is_active', 'updated_at'))
    mapping_ids = sorted(action['mapping_id'] for group in raw['groups'] for action in group['mapping_actions'])
    result['shop_products.original_observations'] = await _summary(db, 'shop_products',
        't.id=ANY(CAST(:ids AS bigint[]))', {'ids': mapping_ids},
        omit=('retired_at', 'retired_by_consolidation_id', 'updated_at', 'external_product_id', 'external_variant_id',
              'external_id', 'external_code', 'parent_code', 'is_variant', 'is_listed', 'shared_stock',
              'sale_to_stock_factor', 'sale_barcodes'))
    return result


async def _admission(db, raw, groups, history):
    ids = sorted(pid for group in raw['groups'] for pid in core._ids(group))
    shops = sorted({shop['id'] for group in groups for shop in group['local']['shops']})
    tokens = sorted({p['sku'] for group in groups for p in group['local']['products']}
        | {value for group in raw['groups'] for t in core._targets(group)
           for value in (t['code'], t['parent_code']) if value}
        | {value for group in groups for m in group['local']['mappings']
           for value in (m['external_code'], m['parent_code']) if value})
    params = {'ids': ids, 'shops': shops, 'tokens': tokens}
    for key, fact in history.items():
        _need(key.split('.', 1)[0] not in FORBIDDEN_STOCK or fact['count'] == 0,
              'consolidation_batch_confirmed_stock_dependency')
    # A later SKU rename preserves the import's original reference. Only an
    # exact unscoped internal alias on that same raw card can attest it; another
    # owner (even in this group) must not make the old reference ambiguous.
    invalid = await db.scalar(text('''SELECT count(*) FROM stock_movements m JOIN products p ON p.id=m.product_id
        WHERE m.product_id=ANY(CAST(:ids AS bigint[])) AND (
          m.movement_type::text='INITIAL' AND m.reference_type IS NOT DISTINCT FROM 'upgates_import'
          AND m.created_by='upgates_import' AND m.reference_source IN ('upgates:biketrek','upgates:xtrek')
          AND (m.reference_id IS NOT DISTINCT FROM p.sku OR (
            EXISTS (SELECT 1 FROM product_identifiers i WHERE i.product_id=m.product_id
              AND i.identifier_type='internal_sku' AND i.supplier_id IS NULL AND i.value=m.reference_id)
            AND NOT EXISTS (SELECT 1 FROM products other WHERE other.id<>m.product_id
              AND lower(other.sku)=lower(m.reference_id))
            AND NOT EXISTS (SELECT 1 FROM product_identifiers other WHERE other.product_id<>m.product_id
              AND other.identifier_type='internal_sku' AND lower(other.value)=lower(m.reference_id))))
          AND p.created_from_source=m.reference_source) IS NOT TRUE'''), params)
    _need(not invalid, 'consolidation_nonlegacy_stock_history')
    _need(not await db.scalar(text('''SELECT count(*) FROM stock_balances WHERE product_id=ANY(CAST(:ids AS bigint[]))
        AND (qty_reserved<>0 OR qty_quarantined<>0)'''), params), 'consolidation_reserved_or_quarantined_stock')
    conditions = {
        'product_editor_publications': "product_id=ANY(CAST(:ids AS bigint[])) AND state NOT IN ('completed','rejected','resolved')",
        'supplier_availability_deliveries': "product_id=ANY(CAST(:ids AS bigint[])) AND state NOT IN ('completed','rejected','resolved')",
        'shop_sync_outbox': "product_id=ANY(CAST(:ids AS bigint[])) AND status::text NOT IN ('completed','cancelled')",
        'stock_sync_settings': 'shop_id=ANY(CAST(:shops AS bigint[])) AND (enabled OR authorized)',
        'stock_sync_items': "shop_id=ANY(CAST(:shops AS bigint[])) AND sku=ANY(CAST(:tokens AS text[])) AND status NOT IN ('verified','skipped')",
        'stock_publication_items': "sku=ANY(CAST(:tokens AS text[])) AND status NOT IN ('verified','cancelled')",
        'fifo_cost_publications': "shop_id=ANY(CAST(:shops AS bigint[])) AND status NOT IN ('verified','skipped','resolved')",
        'ai_content_jobs': "context->>'code'=ANY(CAST(:tokens AS text[])) AND context->'update_preview' IS NOT NULL AND context->'update_preview'<>'null'::jsonb AND coalesce(context->'update_preview'->>'state','') NOT IN ('verified','completed','expired','cancelled','rejected','resolved')",
    }
    for table, condition in conditions.items():
        _need(not await db.scalar(text('SELECT count(*) FROM ' + core.dependencies.quoted(table) + ' WHERE ' + condition), params),
              'consolidation_batch_pending_publication_' + table)
    paused = [dict(row) for row in (await db.execute(text('''SELECT id,state,operator_paused,operator_control_revision,
        checked_run_id,source_sha,admin_sha FROM atomer_catalog_campaigns ORDER BY id'''))).mappings()]
    _need(all(row['operator_paused'] and row['state'] == 'paused' or row['state'] in ('done', 'source_changed', 'review')
              for row in paused), 'consolidation_batch_catalog_not_paused')
    return paused


async def _capture(db, raw, *, lock=False):
    refs = await _references(db)
    guards = await _guards(db)
    groups = []
    for request in raw['groups']:
        local = await core._local(db, request, lock=lock)
        groups.append(dict(request=request, local=local, plan=core._plan(request, local)))
    _native(raw, groups)
    for catalog in raw['native_catalogs']:
        from datetime import datetime
        age = links.now() - datetime.fromisoformat(catalog['captured_at'])
        _need(timedelta(minutes=-2) <= age <= MAX_CATALOG_AGE, 'consolidation_batch_catalog_stale')
        _need(await links._source_call(links.source.current_fingerprint(catalog['shop_code'])) == catalog['fingerprint'],
              'consolidation_shop_connection_changed')
    for group in groups:
        group['identity'] = await core._identity(db, group['request'], group['local'], group['native'])
    history = await _history(db, raw, refs)
    paused = await _admission(db, raw, groups, history)
    proof = dict(schema=VERSION, request_hash=links.digest(raw), groups=groups, history=history,
                 reference_schema=refs, guards=guards, campaigns=paused)
    _need(len(json.dumps(proof, ensure_ascii=False).encode()) <= MAX_BYTES, 'consolidation_batch_payload_limit')
    return proof


def _public_group(group):
    clean = lambda row: {key: value for key, value in row.items() if not key.startswith('_')}
    plan = deepcopy(group['plan'])
    for key in ('absence', 'unverified_code_absence'):
        if key in plan:
            plan[key] = {shop: [clean(row) for row in rows] for shop, rows in plan[key].items()}
    return dict(id=group['request']['request_id'], keeper_product_id=group['request']['keeper_product_id'],
        retired_product_ids=group['request']['retired_product_ids'], products=group['local']['products'],
        existing_mappings=[clean(row) for row in group['local']['mappings']], plan=plan, identity=group['identity'])


def _preview_result(raw, proof):
    return dict(schema=VERSION, request_id=raw['request_id'], status='review', eligible=True,
        request_hash=proof['request_hash'], preview_hash=links.digest(proof),
        approved_scope_sha256=raw['approved_scope_sha256'], group_count=len(proof['groups']),
        groups=[_public_group(group) for group in proof['groups']],
        preserved_rows=proof['history'], history_hash=links.digest(proof['history']), campaigns=proof['campaigns'],
        native_evidence_basis='operator_reviewed_unfiltered_products_export',
        historical_native_effects='not_determined', source_publication_completed=False,
        physical_stock_changed=False, remote_requests=0, remote_write_performed=False)


async def _receipt(db, batch_id, expected_hash=None):
    rows = list(await db.scalars(select(ProductConsolidation).where(
        ProductConsolidation.document['dependencies'][META]['id'].astext == str(batch_id)).order_by(ProductConsolidation.id).limit(65)
        .execution_options(populate_existing=True)))
    if not rows:
        return None
    meta = rows[0].document['dependencies'][META]
    _need(expected_hash is None or meta['request_hash'] == expected_hash, 'consolidation_batch_request_changed')
    _need(len(rows) == meta['group_count'] <= 64 and sorted(row.id for row in rows) == meta['child_ids'],
          'consolidation_batch_receipt_incomplete')
    saved = [row.document['dependencies']['one_off_request'] for row in rows if 'one_off_request' in row.document['dependencies']]
    _need(len(saved) == 1 and links.digest(saved[0]) == meta['request_hash'], 'consolidation_batch_receipt_changed')
    expected_ids = sorted(_child_id(batch_id, request) for request in saved[0]['groups'])
    _need(expected_ids == meta['child_ids'], 'consolidation_batch_receipt_changed')
    by_id = {request['request_id']: request for request in saved[0]['groups']}
    for row in rows:
        _need(row.status == 'completed' and row.result is not None and row.completed_at is not None
            and row.document['dependencies'][META] == meta and links.digest(row.document) == row.preview_hash
            and row.document['request'] == by_id[row.id] and links.digest(row.document['request']) == row.request_hash
            and row.result.get('one_off_batch_id') == str(batch_id)
            and row.result.get('one_off_batch_request_hash') == meta['request_hash'],
            'consolidation_batch_receipt_changed')
    return dict(schema=VERSION, request_id=str(batch_id), status='completed', **{key: meta[key]
        for key in ('request_hash', 'preview_hash', 'approved_scope_sha256', 'group_count', 'history_hash')},
        groups=[dict(id=row.id, preview_hash=row.preview_hash, result=deepcopy(row.result)) for row in rows],
        native_evidence_basis='operator_reviewed_unfiltered_products_export',
        historical_native_effects='not_determined', source_publication_completed=False,
        physical_stock_changed=False, remote_requests=0, remote_write_performed=False)


async def get(db, batch_id):
    result = await _receipt(db, batch_id)
    if result is None:
        raise links.LinkError('consolidation_batch_not_found', 404)
    return result


async def preview(db, payload):
    raw = _request(payload)
    try:
        await db.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
        saved = await _receipt(db, raw['request_id'], links.digest(raw))
        if saved:
            return saved
        async with asyncio.timeout(MAX_SECONDS):
            proof = await _capture(db, raw)
        return _preview_result(raw, proof)
    except TimeoutError:
        raise links.LinkError('consolidation_batch_timeout') from None
    finally:
        await db.rollback()


async def _lock(db, raw):
    await campaigns._lock(db)
    for key, shared in ((job_controls.REFRESH_LOCK, True), (source_cards.LOCAL_LOCK, False)):
        suffix = '_shared' if shared else ''
        _need(await db.scalar(text('SELECT pg_try_advisory_xact_lock' + suffix + '(:key)'), {'key': key}),
              'consolidation_batch_source_worker_busy')
    for warehouse_id in await db.scalars(select(Warehouse.id).order_by(Warehouse.id)):
        await require_stock_write_allowed(db, warehouse_id)
    ids = sorted(pid for group in raw['groups'] for pid in core._ids(group))
    await db.execute(select(Product.id).where(Product.id.in_(ids)).order_by(Product.id).with_for_update(nowait=True))
    refs = await _references(db)
    tables = TABLES | {row['table_name'] for row in refs}
    await db.execute(text('LOCK TABLE ' + ','.join(core.dependencies.quoted(table) for table in sorted(tables))
                          + ' IN SHARE MODE NOWAIT'))


async def apply(db, payload, *, actor=None):
    raw = _request(payload.request)
    request_hash = links.digest(raw)
    try:
        saved = await _receipt(db, raw['request_id'], request_hash)
        if saved:
            _need(saved['preview_hash'] == payload.preview_hash, 'consolidation_batch_preview_changed')
            return saved
        async with asyncio.timeout(MAX_SECONDS):
            await _lock(db, raw)
            saved = await _receipt(db, raw['request_id'], request_hash)
            if saved:
                _need(saved['preview_hash'] == payload.preview_hash, 'consolidation_batch_preview_changed')
                await db.rollback()
                return saved
            child_ids = sorted(group['request_id'] for group in raw['groups'])
            _need(not await db.scalar(select(ProductConsolidation.id).where(ProductConsolidation.id.in_(child_ids)).limit(1)),
                  'consolidation_batch_child_exists')
            proof = await _capture(db, raw, lock=True)
            _need(links.digest(proof) == payload.preview_hash, 'consolidation_batch_preview_changed')
            meta = dict(id=raw['request_id'], schema=VERSION, request_hash=request_hash, preview_hash=payload.preview_hash,
                approved_scope_sha256=raw['approved_scope_sha256'], group_count=len(child_ids), child_ids=child_ids,
                history_hash=links.digest(proof['history']))
            for index, group in enumerate(proof['groups']):
                deps = dict(blockers=[], facts={}, history_hash=meta['history_hash'],
                    admission='explicit_operator_db_only_migration', **{META: meta})
                if index == 0:
                    deps['one_off_request'] = raw
                document = {**group, 'dependencies': deps, 'created_by': actor or {'source': 'operator_token'}}
                row = ProductConsolidation(id=group['request']['request_id'],
                    request_hash=links.digest(group['request']), preview_hash=links.digest(document), document=document,
                    status='prepared', expires_at=links.now() + timedelta(minutes=links.PREVIEW_MINUTES))
                db.add(row)
                await db.flush()
                await core._mutate(db, row, group['request'], group['local'], group['native'], deps, actor=actor,
                    result_metadata=dict(one_off_batch_id=raw['request_id'], one_off_batch_request_hash=request_hash))
            _need(await _history(db, raw, proof['reference_schema']) == proof['history'], 'consolidation_history_changed')
            _need(await _admission(db, raw, proof['groups'], proof['history']) == proof['campaigns'],
                  'consolidation_batch_campaign_changed')
            for catalog in raw['native_catalogs']:
                _need(await links._source_call(links.source.current_fingerprint(catalog['shop_code'])) == catalog['fingerprint'],
                      'consolidation_shop_connection_changed')
            # Validate the normal047 deferred alias/shared-sale constraints while
            # the whole batch can still be rolled back as one unit.
            await db.execute(text('SET CONSTRAINTS ALL IMMEDIATE'))
            result = await _receipt(db, raw['request_id'], request_hash)
            _need(result is not None, 'consolidation_batch_receipt_incomplete')
        await db.commit()
        return result
    except Exception as error:
        await db.rollback()
        if isinstance(error, links.LinkError):
            raise
        if isinstance(error, (ReviewError, StockPublicationHoldError)):
            code = error.code
        elif isinstance(error, TimeoutError):
            code = 'consolidation_batch_timeout'
        elif isinstance(error, DBAPIError) and getattr(error.orig, 'sqlstate', None) in ('55P03', '40001', '40P01', '23505', '23514'):
            code = 'consolidation_batch_conflict'
        else:
            raise
        failure = links.LinkError(code)
        raise failure from None
