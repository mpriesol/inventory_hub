"""Fail-closed dependency inventory for the narrow identity-only workflow.

SQL hashes preserve original full-precision records without transferring order
or invoice contents into a preview. Unknown nonempty product dependencies and
unknown operation documents block apply. Historical INITIAL/upgates_import is
the only stock history admitted, with no tracking or FIFO anywhere in a group.
"""
import hashlib
import json
import re
from sqlalchemy import text
from inventory_hub import config_io
from inventory_hub.services.product_links import digest


PRESERVED = {'product_identifiers', 'product_variant_attributes', 'stock_balances', 'stock_movements',
             'shop_products', 'product_editor_audit'}
KEEPER_ONLY = {'product_supply_sources', 'product_editor_overrides', 'product_sku_identities'}
PREVIEWS = {'product_link_previews', 'shared_stock_sale_previews', 'supplier_card_link_previews',
            'order_stock_previews', 'product_consolidations'}
# These facts cannot execute a frozen product write. Typed references are still
# inspected independently; a new/unknown JSON table is never implicitly exempt.
JSON_READ_ONLY = PRESERVED | KEEPER_ONLY | {
    'products', 'supplier_products', 'supplier_feeds', 'supplier_feed_items_raw', 'supplier_feed_runs',
    'supplier_availability_observations', 'shop_product_content', 'config_versions', 'sync_log',
    'product_editor_saves', 'receiving_scan_requests', 'invoice_payment_operations',
    'uploaded_invoices', 'uploaded_invoice_lines', 'ai_rule_versions', 'ai_content_revisions',
    'product_feed_mapping_revisions', 'product_feed_mappings', 'order_collection_runs',
    'order_stock_policies', 'order_webhook_settings', 'stock_shop_settings', 'stock_warehouse_settings',
    'atomer_progress_sources', 'atomer_progress_runs', 'atomer_progress_items',
    'atomer_review_targets', 'atomer_review_checks', 'atomer_review_events',
    'atomer_catalog_proofs', 'atomer_catalog_source_cards',
}
SETTLED = ('completed', 'verified', 'expired', 'cancelled', 'rejected', 'abandoned')
REQUIRED_TABLES = {
    'products', 'shop_products', 'product_identifiers', 'stock_balances', 'stock_movements',
    'stock_tracking', 'fifo_layers', 'fifo_states', 'fifo_cutovers', 'fifo_receipts',
    'shop_orders', 'shop_order_items', 'reservations', 'receiving_lines', 'inventory_count_lines',
    'scan_events', 'stock_native_lines', 'stock_native_events', 'stock_sync_checkpoints',
    'stock_sync_dirty', 'shop_sync_outbox', 'stock_sync_items', 'stock_publication_items',
    'fifo_cost_publications', 'product_editor_publications', 'product_import_drafts',
    'product_link_previews', 'shared_stock_sale_previews', 'supplier_card_link_previews',
    'product_consolidations', 'product_canonical_aliases',
}
IDENTIFIER = re.compile(r'^[a-z_][a-z0-9_]*$')


def quoted(value):
    if not IDENTIFIER.fullmatch(value):
        raise ValueError('Unsupported schema identifier')
    return '"' + value + '"'


async def _summary(db, table, condition, params):
    # All identifiers/conditions originate in this module or the local catalog,
    # and values remain SQL parameters. No production row data is logged.
    row = (await db.execute(text(f'''SELECT count(*) AS count,
        md5(coalesce(string_agg(to_jsonb(t)::text,E'\\n' ORDER BY to_jsonb(t)::text),'')) AS hash
        FROM {quoted(table)} t WHERE {condition}'''), params)).mappings().one()
    return dict(row)


def _open_condition(table, columns):
    if table in PREVIEWS:
        return "t.status NOT IN ('completed','expired') AND (t.status<>'prepared' OR t.expires_at>now())"
    # A finished AI generation can still own a prepared/sending publication.
    # V1 blocks any matching job whose frozen update has not settled explicitly.
    if table == 'ai_content_jobs':
        return "coalesce(t.context->'update_preview'->>'state','') NOT IN ('verified','completed','expired','cancelled')"
    if table == 'atomer_catalog_jobs':
        return "t.stage NOT IN ('verified','excluded')"
    if table == 'atomer_catalog_campaigns':
        return "t.state IN ('running','waiting')"
    if table == 'stock_publication_holds':
        return 't.active'
    if table == 'stock_sync_settings':
        return 't.enabled OR t.authorized'
    for name in ('status', 'state'):
        if name in columns:
            terminal = ','.join("'" + value + "'" for value in SETTLED)
            return f"coalesce(t.{quoted(name)}::text,'') NOT IN ({terminal})"
    return 'true'


def _mentions_sql():
    paths = ["$.** ? (@.type() == \"string\" && @ == $tokens[*])"]
    for key in ('product_id', 'hub_id', 'hub_product_id', 'expected_hub_product_id',
                'expected_product_id', 'keeper_product_id', 'canonical_product_id'):
        paths.append(f'$.**.{key} ? (@ == $ids[*] || @ == $id_strings[*])')
    for key in ('product_ids', 'retired_product_ids'):
        paths.append(f'$.**.{key}[*] ? (@ == $ids[*] || @ == $id_strings[*])')
    for key in ('mapping_id', 'sale_mapping_id', 'link_id'):
        paths.append(f'$.**.{key} ? (@ == $mapping_ids[*] || @ == $mapping_strings[*])')
    for key in ('native_product_id', 'native_variant_id', 'external_product_id', 'external_variant_id'):
        paths.append(f'$.**.{key} ? (@ == $native_ids[*] || @ == $native_strings[*])')
    return '(' + ' OR '.join("jsonb_path_exists(to_jsonb(t), '" + path + "', CAST(:vars AS jsonb))"
                             for path in paths) + ')'


async def inspect(db, *, ids, keeper_id, products, identifiers, mappings, targets, request_id,
                  keeper_dependencies=None):
    ids = sorted(ids)
    retired = [pid for pid in ids if pid != keeper_id]
    blockers, facts = [], {}
    if keeper_dependencies is not None:
        blockers.extend(keeper_dependencies['blockers'])
    catalog = (await db.execute(text('''SELECT c.relname AS table_name,a.attname AS column_name,
        format_type(a.atttypid,a.atttypmod) AS data_type FROM pg_class c
        JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_attribute a ON a.attrelid=c.oid
        WHERE n.nspname=current_schema() AND c.relkind IN ('r','p') AND a.attnum>0 AND NOT a.attisdropped
        ORDER BY c.relname,a.attnum'''))).mappings().all()
    columns = {}
    for row in catalog:
        columns.setdefault(row['table_name'], {})[row['column_name']] = row['data_type']
    for missing in sorted(REQUIRED_TABLES - columns.keys()):
        blockers.append({'code': 'consolidation_schema_incomplete', 'table': missing})
    foreign_keys = (await db.execute(text('''SELECT c.conrelid::regclass::text AS table_name,
        a.attname AS column_name,cardinality(c.conkey) AS arity,
        EXISTS(SELECT 1 FROM pg_trigger tr WHERE tr.tgrelid=c.conrelid
         AND tr.tgname='trg_retired_reference_' || a.attname AND tr.tgenabled IN ('O','A')) AS guarded
        FROM pg_constraint c JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=c.conkey[1]
        WHERE c.contype='f' AND c.confrelid='products'::regclass
        ORDER BY c.conrelid::regclass::text,a.attname'''))).mappings().all()
    params = {'ids': ids, 'retired': retired}
    for ref in foreign_keys:
        table, column = ref['table_name'], ref['column_name']
        if table == 'product_canonical_aliases':
            continue
        if ref['arity'] != 1 or not IDENTIFIER.fullmatch(table) or not IDENTIFIER.fullmatch(column):
            blockers.append({'code': 'consolidation_unknown_reference', 'table': table})
            continue
        if not ref['guarded']:
            blockers.append({'code': 'consolidation_reference_guard_missing', 'table': table})
        condition = f't.{quoted(column)}=ANY(CAST(:ids AS bigint[]))'
        summary = await _summary(db, table, condition, params)
        facts[table + '.' + column] = summary
        if not summary['count'] or table in PRESERVED:
            continue
        if table in KEEPER_ONLY:
            summary = await _summary(db, table, f't.{quoted(column)}=ANY(CAST(:retired AS bigint[]))', params)
        elif keeper_dependencies is not None and (table, column) in (
                ('uploaded_invoice_lines', 'matched_product_id'), ('receiving_lines', 'product_id')):
            # Only complete server-discovered, independently proven rows receive
            # an exception. A new/missing/retired FK remains a normal blocker.
            key = 'allowed_invoice_line_ids' if table == 'uploaded_invoice_lines' else 'allowed_receiving_line_ids'
            summary = await _summary(db, table, condition + ' AND NOT (t.id=ANY(CAST(:allowed AS bigint[])))',
                {**params, 'allowed': keeper_dependencies[key]})
        elif table in PREVIEWS:
            summary = await _summary(db, table, condition + ' AND ' + _open_condition(table, columns[table]), params)
        elif table == 'product_editor_publications':
            summary = await _summary(db, table, condition + ' AND ' + _open_condition(table, columns[table]), params)
        if summary['count']:
            blockers.append({'code': 'consolidation_dependency_in_use', 'table': table, 'count': summary['count']})
    if not (REQUIRED_TABLES - columns.keys()):
        invalid_history = await db.scalar(text('''SELECT count(*) FROM stock_movements m JOIN products p ON p.id=m.product_id
            WHERE m.product_id=ANY(CAST(:ids AS bigint[])) AND (
              m.movement_type::text='INITIAL' AND m.reference_type IS NOT DISTINCT FROM 'upgates_import'
              AND m.created_by='upgates_import' AND m.reference_source IN ('upgates:biketrek','upgates:xtrek')
              AND m.reference_id IS NOT DISTINCT FROM p.sku AND p.created_from_source=m.reference_source) IS NOT TRUE'''), params)
        if invalid_history:
            blockers.append({'code': 'consolidation_nonlegacy_stock_history', 'count': invalid_history})
        reserved = await db.scalar(text('''SELECT count(*) FROM stock_balances
            WHERE product_id=ANY(CAST(:ids AS bigint[])) AND (qty_reserved<>0 OR qty_quarantined<>0)'''), params)
        if reserved:
            blockers.append({'code': 'consolidation_reserved_or_quarantined_stock', 'count': reserved})
    tokens = {row['sku'] for row in products}
    tokens.update(row['value'] for row in identifiers if row['value'])
    tokens.update(value for row in mappings for value in (row.get('external_code'), row.get('parent_code')) if value)
    tokens.update(value for row in targets for value in (row['code'], row['parent_code']) if value)
    tokens.update(f'product:{pid}' for pid in ids)
    # Former/current sale keys and typed native identities are included; an
    # operation cannot hide behind an old code or a JSON-only product field.
    mapping_ids = [row['id'] for row in mappings]
    native_strings = sorted({str(value) for row in mappings for value in
        (row.get('external_product_id'), row.get('external_variant_id')) if value} |
        {str(value) for row in targets for value in (row['native_product_id'], row.get('native_variant_id')) if value})
    tokens.update(f'{kind}:{native}' for native in native_strings for kind in ('product', 'variant'))
    variables = {'ids': ids, 'id_strings': list(map(str, ids)), 'tokens': sorted(tokens),
        'mapping_ids': mapping_ids, 'mapping_strings': list(map(str, mapping_ids)),
        'native_strings': native_strings, 'native_ids': [int(value) for value in native_strings if value.isdigit()]}
    params.update(vars=json.dumps(variables), request_id=str(request_id), tokens=sorted(tokens))
    # Any unresolved order line using an old/current sales code is a blocker,
    # including lines not yet assigned to a Hub card.
    if 'shop_order_items' in columns:
        summary = await _summary(db, 'shop_order_items',
            'lower(t.external_product_code)=ANY(CAST(:folded_tokens AS text[])) OR t.sale_mapping_id=ANY(CAST(:mapping_ids AS bigint[]))',
            {**params, 'folded_tokens': sorted({value.casefold() for value in tokens}), 'mapping_ids': mapping_ids})
        facts['order_sales'] = summary
        if summary['count']:
            blockers.append({'code': 'consolidation_order_sale_history', 'count': summary['count']})
    for table, fields in sorted(columns.items()):
        if table in JSON_READ_ONLY or 'jsonb' not in fields.values():
            continue
        condition = _mentions_sql() + ' AND (' + _open_condition(table, fields) + ')'
        if table == 'product_consolidations':
            condition += ' AND t.id<>:request_id'
        summary = await _summary(db, table, condition, params)
        if summary['count']:
            facts['pending:' + table] = summary
            blockers.append({'code': 'consolidation_pending_document', 'table': table, 'count': summary['count']})
    # A campaign may have selected a whole source family/brand before concrete
    # product IDs exist. Do not guess that it excludes this group.
    if 'atomer_catalog_campaigns' in columns:
        count = await db.scalar(text("SELECT count(*) FROM atomer_catalog_campaigns WHERE state IN ('running','waiting')"))
        if count:
            blockers.append({'code': 'consolidation_catalog_worker_active', 'count': count})
    return {'blockers': sorted(blockers, key=lambda item: (item['code'], item.get('table', ''))),
            'facts': facts, 'schema_hash': digest([list(map(dict, foreign_keys)), list(map(dict, catalog))]),
            'tokens': sorted(tokens),
            'history_hash': digest({key: value for key, value in facts.items()
                if key.startswith(('stock_', 'fifo_', 'reservations.', 'shop_order_', 'receiving_', 'inventory_count_'))})}


def catalog_journals(shop_codes, tokens):
    """Bounded filesystem journal check; no secrets/configuration values read.

    Unsettled matching journals block. Broken/unbounded journals fail closed.
    The caller repeats this check in the DB critical section; catalog writers
    must also claim the shared identity lock before their sending intent.
    """
    records, blockers = [], []
    try:
        seen = set()
        total = 0
        for shop_code in sorted(shop_codes):
            directory = config_io.shop_path(shop_code).parent / 'catalog-imports'
            if directory in seen or not directory.exists():
                continue
            seen.add(directory)
            paths = sorted(directory.glob('*.json'))
            if len(paths) > 1000:
                raise ValueError('journal limit')
            for path in paths:
                if '.result.' in path.name or path.name.endswith('.result.json'):
                    continue
                if path.is_symlink():
                    raise ValueError('journal symlink')
                size = path.stat().st_size
                total += size
                if size > 10_000_000 or total > 30_000_000:
                    raise ValueError('journal limit')
                raw = path.read_bytes()
                document = json.loads(raw)
                # Exact JSON string values, not title substrings or inferred IDs.
                def mentions(value):
                    if isinstance(value, dict): return any(mentions(v) for v in value.values())
                    if isinstance(value, list): return any(mentions(v) for v in value)
                    return isinstance(value, str) and value in tokens
                if not mentions(document):
                    continue
                for sidecar in (path.with_suffix('.result.json'), path.with_suffix('.events.jsonl')):
                    if sidecar.is_symlink():
                        raise ValueError('journal symlink')
                    if sidecar.exists():
                        sidecar_size = sidecar.stat().st_size
                        total += sidecar_size
                        if sidecar_size > 10_000_000 or total > 30_000_000:
                            raise ValueError('journal limit')
                from inventory_hub.services.catalog_import import _load_job
                job = _load_job(path)
                states = {item.get('status') for item in (job.get('result') or {}).get('items', [])}
                # Completed/verified imports replay their stored result; all
                # uncertain/partial/failed or unstarted matching plans block.
                settled = bool(states) and states <= {'created', 'exists', 'skipped', 'linked', 'verified', 'completed'}
                if not settled:
                    blockers.append({'code': 'consolidation_catalog_journal_pending', 'shop_code': shop_code,
                        'journal_id': path.stem})
                records.append({'shop_code': shop_code, 'journal_id': path.stem,
                    'hash': hashlib.sha256(json.dumps(job, sort_keys=True, separators=(',', ':')).encode()).hexdigest()})
    except (OSError, ValueError, TypeError, KeyError):
        blockers.append({'code': 'consolidation_catalog_journal_unverified'})
    return {'records': records, 'blockers': blockers}
