"""Read-only, complete current-writer scope for a retained Atomer job.

Historical card IDs are never redirected or rewritten. Both ends of canonical
aliases, every conflicting current owner, and all raw sale mappings enter the
closure. This proves current intent coverage, not absence of historical effects.
"""
from dataclasses import asdict
import json

from sqlalchemy import text

from inventory_hub.services import product_consolidation_dependencies as dependencies
from inventory_hub.services import product_dependency_scan as scan
from inventory_hub.services import product_dependency_scope as semantic
from inventory_hub.services import product_dependency_atomer_scope as atomer_scope
from inventory_hub.services import atomer_catalog_job_control as controls
from inventory_hub.services.atomer_review_targets import ReviewError
from inventory_hub.services.product_identity import barcode_keys
from inventory_hub.services.identifiers import ProductIdentifierService


MAX_ROWS = 5000
MAX_BYTES = 5_000_000


class ScopeIssue(ReviewError):
    def __init__(self, code, **details):
        super().__init__(code)
        self.blocker = dict(code=code, **details)


# These are retained local facts, not outbound instructions. Unknown FK tables
# remain blocking. In particular no stock/FIFO history has to be rewritten or
# eligible for consolidation merely to stop this catalog plan.
LOCAL_FACTS = dependencies.JSON_READ_ONLY | {
    'product_canonical_aliases', 'product_attributes', 'fifo_layers', 'fifo_states',
    'fifo_cutovers', 'fifo_receipts', 'fifo_allocations', 'fifo_layer_adjustments',
    'stock_tracking', 'stock_native_lines', 'stock_native_events',
    'stock_sync_checkpoints', 'stock_delta_events', 'opening_stock_lines',
    'receiving_lines', 'scan_events', 'inventory_count_lines', 'reservations',
    'shop_order_items', 'shop_orders', 'fifo_returns', 'fifo_releases', 'fifo_cost_revisions',
    'stock_sync_baseline_events', 'atomer_catalog_controls', 'atomer_catalog_job_controls',
    'atomer_review_events', 'atomer_review_work',
}
TERMINALS = {
    'receiving_sessions': ('status', ('completed', 'cancelled')),
    'opening_stock_batches': ('status', ('completed',)),
    'stock_adjustments': ('status', ('completed',)),
    'order_processing_jobs': ('status', ('completed',)),
    'shop_sync_outbox': ('status', ('completed', 'cancelled')),
    'stock_publication_batches': ('status', ('completed', 'cancelled')),
    'stock_publication_items': ('status', ('verified', 'cancelled')),
    'stock_sync_runs': ('status', ('completed',)),
    'stock_sync_items': ('status', ('verified', 'skipped')),
    'fifo_cost_publications': ('status', ('verified', 'skipped', 'resolved')),
    'product_editor_publications': ('state', ('completed', 'rejected', 'resolved')),
    'supplier_availability_deliveries': ('state', ('completed', 'rejected', 'resolved')),
}
WRITER_JSON = {
    'receiving_sessions': {'session_data'}, 'opening_stock_batches': {'preview_data', 'result'},
    'stock_adjustments': {'preview_data', 'result'}, 'order_processing_jobs': {'result'},
    'shop_sync_outbox': {'payload', 'response_data'},
    'stock_publication_batches': {'preview_data', 'result'},
    'stock_publication_items': {'target', 'observation', 'acknowledgement', 'resolution'},
    'stock_sync_runs': {'selected_skus'}, 'stock_sync_items': {'target', 'desired', 'before', 'after', 'resolution', 'delta_snapshot'},
    'fifo_cost_publications': {'source', 'target', 'document', 'before', 'after', 'resolution'},
    'product_editor_publications': {'document'}, 'supplier_availability_deliveries': {'desired', 'confirmed'},
    'atomer_variant_repairs': {'document'}, 'atomer_catalog_category_restores': {'intent', 'proof', 'ack'},
    'atomer_catalog_campaigns': {'selection', 'options'},
    'stock_publication_holds': {'assertions', 'close_result'}, 'stock_sync_settings': {'authority_snapshot'},
    'product_link_previews': {'document', 'result'}, 'supplier_card_link_previews': {'document', 'result'},
    'shared_stock_sale_previews': {'document', 'result'}, 'order_stock_previews': {'preview_data', 'result'},
}
KNOWN_REQUIRED = scan.TABLES | {
    'products', 'product_identifiers', 'product_sku_identities', 'shop_products', 'shops', 'product_canonical_aliases',
    'atomer_catalog_proofs', 'atomer_catalog_source_cards', 'atomer_catalog_job_controls',
    'product_editor_audit', 'product_editor_publications', 'stock_publication_items',
    'fifo_cost_publications', 'stock_sync_dirty', 'shop_sync_outbox', 'stock_sync_items',
    'atomer_variant_repairs', 'atomer_catalog_category_restores',
}


async def schema(db):
    rows = (await db.execute(text('''SELECT c.relname AS table_name,a.attname AS column_name,
        format_type(a.atttypid,a.atttypmod) AS data_type FROM pg_class c
        JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_attribute a ON a.attrelid=c.oid
        WHERE n.nspname=current_schema() AND c.relkind IN ('r','p') AND a.attnum>0 AND NOT a.attisdropped
        ORDER BY c.relname,a.attnum'''))).mappings().all()
    columns = {}
    for row in rows:
        columns.setdefault(row['table_name'], {})[row['column_name']] = row['data_type']
    missing = (KNOWN_REQUIRED | set(scan.LOCK_TABLES) | WRITER_JSON.keys()) - columns.keys()
    if missing:
        raise ReviewError('catalog_handoff_schema_incomplete')
    return columns


async def lock_inputs(db, columns):
    # Fence all scanned inputs and phantom inserts in discovered JSON/FK
    # writers, without blocking unrelated immutable stock/invoice history.
    # Take the campaign/review/identity locks first. No HTTP occurs.
    tables = set(scan.LOCK_TABLES) | {'products', 'product_identifiers', 'product_sku_identities', 'shop_products',
        'product_canonical_aliases', 'product_editor_audit', 'atomer_catalog_proofs',
        'atomer_catalog_source_cards', 'atomer_catalog_job_controls'}
    tables.update(t for t, fields in columns.items() if t not in LOCAL_FACTS and {'json', 'jsonb'} & set(fields.values()))
    foreign = await db.scalars(text('''SELECT DISTINCT c.conrelid::regclass::text FROM pg_constraint c
        WHERE c.contype='f' AND c.confrelid='products'::regclass'''))
    tables.update(t for t in foreign if t not in LOCAL_FACTS)
    if tables - columns.keys():
        raise ReviewError('catalog_handoff_schema_incomplete')
    await db.execute(text('LOCK TABLE ' + ','.join(dependencies.quoted(t) for t in sorted(tables))
                          + ' IN SHARE MODE NOWAIT'))


async def rows(db, query, **params):
    result = [dict(row) for row in (await db.execute(text(query + ' LIMIT :handoff_limit'),
        {**params, 'handoff_limit': MAX_ROWS + 1})).mappings()]
    if len(result) > MAX_ROWS or len(json.dumps(result, default=str).encode()) > MAX_BYTES:
        raise ReviewError('catalog_handoff_scope_limit')
    return result


async def _summary(db, table, condition, params):
    # Count the whole selection, then hash only an admitted complete inventory.
    # Aggregate fixed-size row hashes, never concatenate all document bodies.
    table = dependencies.quoted(table)
    row_hash = "encode(sha256(convert_to(to_jsonb(t)::text,'UTF8')),'hex')"
    result = dict((await db.execute(text(f'''SELECT census.n AS count,
        CASE WHEN census.n<=:handoff_rows THEN (
          SELECT encode(sha256(convert_to(coalesce(string_agg({row_hash},E'\\n' ORDER BY {row_hash}),''),'UTF8')),'hex')
          FROM {table} t WHERE {condition}) ELSE NULL END AS hash
        FROM (SELECT count(*) AS n FROM {table} t WHERE {condition}) census'''),
        {**params, 'handoff_rows': MAX_ROWS})).mappings().one())
    result['complete'] = result['count'] <= MAX_ROWS
    return result


def seed(data, codes):
    ids, tokens, barcodes, targets, mapping_ids = set(), set(codes), set(), [], set()
    family = data['job']['family_key']
    if family.startswith(('v:', 's:')):
        tokens.add(family[2:])

    def identity(value, namespace, fallback=None):
        if not value:
            return
        if value.get('link_id') is not None:
            mapping_ids.add(value['link_id'])
        pid, vid = value.get('product_id'), value.get('variant_id')
        tokens.update(value[k] for k in ('code', 'target_code', 'parent', 'target_parent') if value.get(k))
        code = value.get('target_code') or value.get('code') or fallback
        parent = value.get('target_parent') or value.get('parent') or ''
        if code:
            tokens.add(code)
        if parent:
            tokens.add(parent)
        if pid is None and vid is None:
            return
        variant = value.get('is_variant', value.get('kind') == 'variant')
        if (not controls._native_id(pid) or type(variant) is not bool
                or variant and not controls._native_id(vid) or not variant and vid is not None):
            raise ReviewError('catalog_handoff_native_identity_unverified')
        targets.append(dict(shop_code=namespace, code=code, parent_code=parent,
            native_product_id=int(pid), native_variant_id=int(vid) if variant else None, is_variant=variant))

    def local(value):
        if not isinstance(value, dict):
            return
        if value.get('card_id') is not None:
            ids.add(value['card_id'])
        if value.get('hub_sku'):
            tokens.add(value['hub_sku'])
        for key in ('ean', 'source_ean', 'gtin_added'):
            if value.get(key):
                barcodes.add(value[key])
        barcodes.update(value.get('gtins') or [])
        correction = value.get('barcode_evidence') or {}
        barcodes.update(correction[k] for k in ('before', 'ean') if correction.get(k))
        if correction.get('required_card_id') is not None:
            ids.add(correction['required_card_id'])
        if correction.get('parent'):
            tokens.add(correction['parent'])
        cached = value.get('cached_native_target')
        if cached:
            tokens.add(cached['sku'])
            barcodes.update(cached.get('gtins') or [])
            # The source writer's cached target is explicitly an xTrek leaf.
            identity({**cached, 'code': cached['sku'], 'is_variant': cached.get('variant_id') is not None}, 'xtrek')
        for key in ('source_card', 'creation_evidence'):
            if key in value:
                local(value[key])

    for code, row in data['rows'].items():
        detail = row['detail']
        if detail.get('hub_id') is not None:
            ids.add(detail['hub_id'])
        ids.update(detail.get('candidates') or [])
        if detail.get('hub_sku'):
            tokens.add(detail['hub_sku'])
        barcodes.update(detail[k] for k in ('ean', 'target_ean') if detail.get(k))
        identity(detail, 'xtrek', code)
        identity(detail.get('bike'), 'biketrek', code)
    for collection in ('proofs', 'source_cards'):
        for row in data[collection]:
            if row.get('card_id') is not None:
                ids.add(row['card_id'])
            local(row['evidence'])
    corrections = data['campaign']['selection'].get('barcode_corrections') or {}
    for code in set(codes) | {family[2:]}:
        value = corrections.get(code) or {}
        barcodes.update(value[k] for k in ('before', 'ean') if value.get(k))
        if value.get('required_card_id') is not None:
            ids.add(value['required_card_id'])
        if value.get('parent'):
            tokens.add(value['parent'])
    for row in data['source_allocations']:
        ids.add(row['id']); tokens.add(row['sku'])
    for row in data['source_allocation_audits']:
        ids.add(row['product_id'])
        tokens.update(row[k] for k in ('before_sku', 'after_sku') if row[k])
    tokens.update(barcodes)
    return ids, tokens, barcodes, targets, mapping_ids


async def closure(db, data, codes):
    ids, tokens, barcodes, targets, mapping_ids = seed(data, codes)
    # A sales code can be present in either configured shop even when the
    # current operator scope excludes it. Neither scope suppresses history.
    namespaces = {'xtrek', 'biketrek'} | {t['shop_code'] for t in targets}
    products = identifiers = mappings = aliases = sku_identities = []
    previous = None
    for _ in range(32):
        if any(len(v) > MAX_ROWS for v in (ids, tokens, barcodes, targets)):
            raise ReviewError('catalog_handoff_scope_limit')
        # Use the shared exact split/checksum/equivalence contract. Retain raw
        # and unverified member tokens too; never strip digits or drop a member.
        audit = semantic.Proof(semantic.Scope.build([], [], [], [], []))
        try:
            for raw in sorted(barcodes):
                atomer_scope._barcodes(audit, raw, '/retained_barcodes', source_code=raw if raw in codes else '')
        except semantic.Unverified as error:
            raise ScopeIssue('catalog_handoff_barcode_scope_unverified', reason=error.reason) from None
        members = {value for raw in barcodes for value, _ in ProductIdentifierService.split_compound_ean(raw)}
        barcodes.update(members)
        tokens.update(members)
        folded = sorted({v.casefold() for v in tokens})
        barcode_tokens = sorted(barcodes | {value for raw in barcodes
            for value in ProductIdentifierService.gtin_equivalents(raw)})
        tokens.update(barcode_tokens)
        params = dict(ids=sorted(ids), codes=folded, barcodes=barcode_tokens,
            targets=json.dumps(targets), namespaces=sorted(namespaces), mapping_ids=sorted(mapping_ids))
        products = await rows(db, '''SELECT to_jsonb(p) AS data FROM products p WHERE
            p.id=ANY(CAST(:ids AS bigint[])) OR lower(p.sku)=ANY(CAST(:codes AS text[])) OR EXISTS (
              SELECT 1 FROM product_identifiers i WHERE i.product_id=p.id AND
               (lower(i.value)=ANY(CAST(:codes AS text[])) OR
                (i.identifier_type::text IN ('ean','upc') AND i.value=ANY(CAST(:barcodes AS text[]))))) OR EXISTS (
              SELECT 1 FROM product_sku_identities i WHERE i.product_id=p.id AND
               (i.source_code_fold=ANY(CAST(:codes AS text[])) OR lower(i.allocated_sku)=ANY(CAST(:codes AS text[]))))
            ORDER BY p.id''', **params)
        products = [r['data'] for r in products]
        ids.update(p['id'] for p in products)
        params['ids'] = sorted(ids)
        aliases = await rows(db, '''SELECT to_jsonb(t) AS data FROM product_canonical_aliases t WHERE
            retired_product_id=ANY(CAST(:ids AS bigint[])) OR canonical_product_id=ANY(CAST(:ids AS bigint[]))
            ORDER BY retired_product_id''', **params)
        aliases = [r['data'] for r in aliases]
        ids.update(a[k] for a in aliases for k in ('retired_product_id', 'canonical_product_id'))
        mappings = await rows(db, '''SELECT to_jsonb(m)||jsonb_build_object('shop_code',s.code) AS data
            FROM shop_products m JOIN shops s ON s.id=m.shop_id WHERE
             m.product_id=ANY(CAST(:ids AS bigint[])) OR m.id=ANY(CAST(:mapping_ids AS bigint[])) OR
             (s.code=ANY(CAST(:namespaces AS text[])) AND
              (lower(m.external_code)=ANY(CAST(:codes AS text[])) OR lower(m.parent_code)=ANY(CAST(:codes AS text[])))) OR
             EXISTS(SELECT 1 FROM jsonb_array_elements(CAST(:targets AS jsonb)) v
              WHERE s.code=v->>'shop_code' AND m.external_product_id=(v->>'native_product_id'))
            ORDER BY m.id''', **params)
        mappings = [{**r['data'], 'legacy_external_id': r['data'].get('external_id')} for r in mappings]
        for mapping in mappings:
            if (not controls._native_id(mapping.get('external_product_id'))
                    or mapping['is_variant'] and not controls._native_id(mapping.get('external_variant_id'))
                    or not mapping['is_variant'] and mapping.get('external_variant_id') is not None):
                raise ScopeIssue('catalog_handoff_native_identity_unverified',
                    mapping_id=mapping['id'], shop_code=mapping['shop_code'])
        ids.update(m['product_id'] for m in mappings)
        params['ids'] = sorted(ids)
        identifiers = await rows(db, '''SELECT to_jsonb(t) AS data FROM product_identifiers t
            WHERE product_id=ANY(CAST(:ids AS bigint[])) ORDER BY id''', **params)
        identifiers = [{**r['data'], 'type': r['data']['identifier_type']} for r in identifiers]
        sku_identities = await rows(db, '''SELECT to_jsonb(t) AS data FROM product_sku_identities t
            WHERE product_id=ANY(CAST(:ids AS bigint[])) ORDER BY id''', **params)
        sku_identities = [r['data'] for r in sku_identities]
        tokens.update(p['sku'] for p in products)
        tokens.update(i['value'] for i in identifiers if i['value'])
        tokens.update(i[k] for i in sku_identities for k in ('source_code', 'allocated_sku'))
        barcodes.update(i['value'] for i in identifiers if i['type'] in ('ean', 'upc') and i['value'])
        tokens.update(m[k] for m in mappings for k in ('external_code', 'parent_code') if m[k])
        namespaces.update(m['shop_code'] for m in mappings)
        # Include renamed/current native parent owners and all their siblings.
        native = {(t['shop_code'], t['native_product_id']) for t in targets}
        for m in mappings:
            pid = m.get('external_product_id')
            if pid is not None:
                if not controls._native_id(pid):
                    raise ReviewError('catalog_handoff_native_identity_unverified')
                key = (m['shop_code'], int(pid))
                if key not in native:
                    targets.append(dict(shop_code=key[0], native_product_id=key[1], native_variant_id=None,
                        is_variant=False, code=m['parent_code'] or m['external_code'], parent_code=m['parent_code']))
                    native.add(key)
        state = controls._digest([sorted(ids), sorted(tokens), sorted(barcodes), targets])
        if state == previous:
            break
        previous = state
    else:
        raise ReviewError('catalog_handoff_scope_limit')
    if ids - {p['id'] for p in products}:
        raise ReviewError('catalog_handoff_retained_card_missing')
    if mapping_ids - {m['id'] for m in mappings}:
        raise ScopeIssue('catalog_handoff_retained_mapping_missing')
    scope = semantic.Scope.build(ids, products, identifiers, mappings, targets)
    scope.skus.update(v.casefold() for v in tokens)
    scope.aliases.update(v.casefold() for v in tokens)
    scope.barcodes.update(barcode_keys(barcodes))
    scope.sales.update((namespace, code.casefold()) for namespace in namespaces for code in tokens)
    scope.parents.update(scope.sales)
    snapshot = dict(products=products, identifiers=identifiers, sku_identities=sku_identities, mappings=mappings, aliases=aliases,
        targets=targets, tokens=sorted(tokens), scope={k: sorted(v, key=lambda x: json.dumps(x)) for k, v in asdict(scope).items()})
    if len(json.dumps(snapshot).encode()) > MAX_BYTES:
        raise ReviewError('catalog_handoff_scope_limit')
    return scope, snapshot


def open_condition(table, fields):
    # Future lifecycle values remain open. Only explicit persisted terminal
    # states are accepted; TTL alone cannot clear a possibly dispatched write.
    if table == 'atomer_variant_repairs':
        return '''(t.state<>'verified' OR
            jsonb_path_exists(t.document,'$.**.state ? (@ == "prepared" || @ == "sending" || @ == "uncertain" || @ == "deferred")') OR
            jsonb_path_exists(t.document,'$.**.status ? (@ == "prepared" || @ == "sending" || @ == "uncertain" || @ == "deferred")'))'''
    if table == 'atomer_catalog_category_restores':
        return "t.stage<>'verified'"
    if table in dependencies.PREVIEWS:
        return "t.status NOT IN ('completed','expired')"
    if table in TERMINALS:
        column, terminal = TERMINALS[table]
        return f"coalesce(t.{dependencies.quoted(column)}::text,'') NOT IN (" + ','.join("'" + v + "'" for v in terminal) + ')'
    if table == 'shop_product_availability':
        return 't.sync_required'
    if table in scan.TABLES | {'atomer_catalog_campaigns', 'stock_publication_holds', 'stock_sync_settings'}:
        return dependencies._open_condition(table, fields)
    # A new table's completed-looking label is not a known lifecycle contract.
    return 'true'


async def writers(db, columns, scope, identities, candidate):
    ids, mappings = sorted(scope.hubs), sorted(scope.mapping_ids)
    native = sorted({p for _, p in scope.native_parents} | {v for _, _, v in scope.native if v is not None})
    tokens = set(identities['tokens']) | {f'product:{p}' for p in ids}
    tokens.update(f'{kind}:{n}' for n in native for kind in ('product', 'variant'))
    variables = dict(ids=sorted(set(ids) | set(native)), id_strings=list(map(str, sorted(set(ids) | set(native)))),
        mapping_ids=mappings, mapping_strings=list(map(str, mappings)), tokens=sorted(tokens),
        native_ids=native, native_strings=list(map(str, native)))
    params = dict(ids=ids, mappings=mappings, vars=json.dumps(variables), tokens=sorted({t.casefold() for t in tokens}))
    # Known repair documents use unqualified native product_id/variant_id.
    # Their numeric namespace is deliberately included conservatively, while
    # the typed scanner below retains exact shop-qualified semantics.
    mentions = dependencies._mentions_sql()[:-1] + ''' OR
        jsonb_path_exists(to_jsonb(t),'$.**.variant_id ? (@ == $native_ids[*] || @ == $native_strings[*])',CAST(:vars AS jsonb)) OR
        EXISTS(SELECT 1 FROM jsonb_path_query(to_jsonb(t),'$.** ? (@.type() == "string")') value
               WHERE lower(value#>>'{}')=ANY(CAST(:tokens AS text[]))))'''
    facts, blockers = {}, []
    fks = (await db.execute(text('''SELECT c.conrelid::regclass::text AS table_name,a.attname AS column_name,
        cardinality(c.conkey) AS arity FROM pg_constraint c
        JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=c.conkey[1]
        WHERE c.contype='f' AND c.confrelid='products'::regclass ORDER BY 1,2'''))).mappings().all()
    for ref in fks:
        table, column = ref['table_name'], ref['column_name']
        if table in LOCAL_FACTS or table in scan.TABLES:
            continue
        if ref['arity'] != 1 or table not in columns:
            blockers.append(dict(code='catalog_handoff_writer_schema_unverified', table=table))
            continue
        condition = f't.{dependencies.quoted(column)}=ANY(CAST(:ids AS bigint[])) AND ({open_condition(table, columns[table])})'
        facts['fk:' + table + '.' + column] = await _summary(db, table, condition, params)
    for table, fields in sorted(columns.items()):
        if table in LOCAL_FACTS or table in scan.TABLES or not {'json', 'jsonb'} & set(fields.values()):
            continue
        recognized = (table in WRITER_JSON and 'json' not in fields.values()
            and {k for k, v in fields.items() if v == 'jsonb'} == WRITER_JSON[table])
        # Unknown numeric keys and completed-looking labels cannot hide a new
        # writer. Only explicitly interpreted schemas get a disjoint/terminal
        # exception; unknown nonempty JSON tables hold globally.
        condition = mentions + ' AND (' + open_condition(table, fields) + ')' if recognized else 'true'
        facts['json:' + table] = await _summary(db, table, condition, params)
        if not recognized and facts['json:' + table]['count']:
            blockers.append(dict(code='catalog_handoff_writer_schema_unverified', table=table))
    # A category restore can affect any product; it has no product FK. An open
    # native category instruction is not certified disjoint by product tokens.
    facts['category_intents'] = await _summary(db, 'atomer_catalog_category_restores',
        "t.stage<>'verified' AND t.intent IS NOT NULL", {})
    facts['active_catalog_campaigns'] = await _summary(db, 'atomer_catalog_campaigns',
        "t.state IN ('running','waiting')", {})
    facts['unscoped_stock_sync_runs'] = await _summary(db, 'stock_sync_runs',
        "t.status<>'completed' AND t.selected_skus IS NULL", {})
    for key, value in facts.items():
        if value['count']:
            blockers.append(dict(code='catalog_handoff_pending_writer', table=key, count=value['count']))
        if not value['complete']:
            blockers.append(dict(code='catalog_handoff_writer_inventory_limit', table=key))
    for table in scan.TABLES:
        if 'json' in columns[table].values():
            blockers.append(dict(code='catalog_handoff_writer_schema_unverified', table=table))
    typed = await scan.inspect(db, scope=scope, columns=columns, open_condition=open_condition,
        request_id=None, handoff_candidate=candidate)
    blockers.extend(typed['blockers'])
    # Timing/cache telemetry never enters a fresh confirmation hash. Complete
    # manifests/counts cover all rows even when human-readable samples truncate.
    stable = {key: value for key, value in typed.items() if key not in ('work', 'records', 'catalog_journals')}
    return dict(complete=typed['complete'] and all(v['complete'] for v in facts.values()),
        blockers=blockers, facts=facts, typed=stable, writer_manifest_encoding='postgresql-jsonb-text-sha256-rows-v1',
        diagnostics=dict(records=typed['records'], catalog_journals=typed['catalog_journals']),
        schema_hash=controls._digest([columns, list(map(dict, fks))]))
